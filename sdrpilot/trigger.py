"""Signal-triggered recording: watch a frequency range of the live stream and record IQ only when something
transmits there - with pre-trigger samples from the ring buffer, and stopping on a timer or when the signal drops
back below the threshold. Detection runs on short FFT frames (tens of microseconds at high sample rates), so
fast, intermittent transmissions (WiFi frames, key fobs, telemetry bursts) are caught from their first moment."""
from __future__ import annotations

import json
import math
import os
import threading
import time
from datetime import datetime

import numpy as np

from . import dsp


class TriggeredRecorder:
    _n = 0

    def __init__(self, radio, board, p: dict):
        TriggeredRecorder._n += 1
        self.id = f"T{TriggeredRecorder._n}"
        self.radio, self.board, self.p = radio, board, p
        self.events: list[dict] = []
        self.state = "starting"
        self.error = ""
        self.started = time.time()
        self.noise_db: float | None = None
        self.level_db: float | None = None
        self.frames = 0
        self.stop_ev = threading.Event()
        self.first_done = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True, name=f"sdrpilot-trigger-{self.id}")
        self._lease = None

    # ------------------------------------------------------------------ public
    def start(self) -> "TriggeredRecorder":
        r = self.radio
        self._lease = r.lease.hold(f"triggered recording {self.id}", center=r.center_freq, rate=r.sample_rate)
        self.thread.start()
        return self

    def stop(self) -> None:
        self.stop_ev.set()
        self.thread.join(timeout=5)

    def summary(self) -> dict:
        p = self.p
        out = {"id": self.id, "state": self.state, "band_mhz": [round(p["f_lo"] / 1e6, 6), round(p["f_hi"] / 1e6, 6)],
               "threshold": (f"{p['threshold_dbfs']} dBFS" if p.get("threshold_dbfs") is not None else
                             f"{p['threshold_db']} dB above the band's noise"),
               "noise_dbfs": None if self.noise_db is None else round(self.noise_db, 1),
               "level_now_dbfs": None if self.level_db is None else round(self.level_db, 1),
               "detection_step_us": round(p.get("frame_us", 0), 2), "events": self.events[-30:],
               "events_total": len(self.events), "running_s": round(time.time() - self.started, 1)}
        if self.error:
            out["error"] = self.error
        return out

    # ------------------------------------------------------------------ worker
    def _setup(self):
        r, p = self.radio, self.p
        fs, fc = float(r.sample_rate), float(r.center_freq)
        lo, hi = p["f_lo"] - fc, p["f_hi"] - fc
        if hi < -fs / 2 or lo > fs / 2:
            raise ValueError(f"{p['f_lo'] / 1e6:.4f}-{p['f_hi'] / 1e6:.4f} MHz is outside the tuned band "
                             f"({(fc - fs / 2) / 1e6:.4f}-{(fc + fs / 2) / 1e6:.4f} MHz) - radio_tune first")
        lo, hi = max(lo, -fs / 2), min(hi, fs / 2)
        bw = max(hi - lo, fs / 4096)
        # frame size: fine time resolution, but at least ~4 bins across the band
        nfft = int(2 ** math.ceil(math.log2(max(16, min(4096, 4 * fs / bw)))))
        if p.get("time_resolution_us"):
            nfft = int(2 ** round(math.log2(max(16, min(65536, fs * p["time_resolution_us"] / 1e6)))))
        f = np.fft.fftshift(np.fft.fftfreq(nfft, 1 / fs))
        sel = (f >= lo) & (f <= hi)
        if not sel.any():
            sel[np.argmin(np.abs(f - (lo + hi) / 2))] = True
        # the DC spike is not a signal
        dc = r.dc_spike_freq() if hasattr(r, "dc_spike_freq") else None
        if dc is not None:
            sel &= np.abs(f - (dc - fc)) > 2 * fs / nfft
        p["frame_us"] = nfft / fs * 1e6
        win = np.hanning(nfft).astype(np.float32)
        norm = float(np.sum(win) ** 2)
        return fs, fc, nfft, sel, win, norm

    def _band_db(self, x, nfft, sel, win, norm):
        n = len(x) // nfft
        if n == 0:
            return np.zeros(0)
        X = np.fft.fftshift(np.fft.fft(x[: n * nfft].reshape(n, nfft) * win, axis=1), axes=1)
        pw = (np.abs(X[:, sel]) ** 2).sum(axis=1) / norm
        # a few bins in one short frame is a very noisy estimate: average over enough frames that ~32 bin-frames
        # go into each value (noise stays ~1.5 dB steady, so noise doesn't trip the threshold or hold it open)
        k = int(max(1, min(64, math.ceil(32 / max(1, int(sel.sum()))))))
        if k > 1:
            c = np.cumsum(np.concatenate([self._tail if getattr(self, "_tail", None) is not None and
                                          len(self._tail) == k - 1 else np.full(k - 1, pw[0]), pw]))
            self._tail = pw[-(k - 1):].copy()
            pw = (c[k - 1:] - np.concatenate([[0.0], c[:-k]])) / k
        return 10 * np.log10(pw + 1e-20)

    def _run(self):
        r, p, ring = self.radio, self.p, self.radio.ring
        try:
            fs, fc, nfft, sel, win, norm = self._setup()
        except Exception as e:
            self.error, self.state = str(e), "failed"
            self.first_done.set()
            self._release()
            return
        epoch = r.epoch
        pos = max(ring.total, r.settle_mark)
        hist: list[float] = []
        quiet_needed = min(3000, max(8, int(0.02 * fs / nfft)))
        self.state = "learning noise" if p.get("threshold_dbfs") is None else "armed"
        cap = None
        end_by = time.time() + p["timeout_s"]
        chunk = int(max(nfft * 16, fs * 0.02)) // nfft * nfft
        try:
            while not self.stop_ev.is_set():
                if r.epoch != epoch:
                    self.error = "the radio was retuned - recording stopped"
                    break
                if cap is None and (time.time() > end_by or len(self.events) >= p["max_events"]):
                    break
                if not ring.wait_until(pos + chunk, 0.5):
                    continue
                if ring.total - pos > ring.size * 0.6:  # fell behind: skip ahead (can't happen often)
                    pos = ring.total - chunk
                x = ring.read(pos, chunk)
                lv = self._band_db(x, nfft, sel, win, norm)
                self.frames += len(lv)
                self.level_db = float(lv.max()) if len(lv) else self.level_db
                if p.get("threshold_dbfs") is not None:
                    thr = float(p["threshold_dbfs"])
                else:
                    if cap is None:
                        hist.extend(lv.tolist())
                        del hist[:-8000]
                    if len(hist) < quiet_needed:
                        pos += chunk
                        continue
                    self.noise_db = float(np.percentile(hist, 50))
                    thr = self.noise_db + float(p["threshold_db"])
                    if self.state == "learning noise":
                        self.state = "armed"
                if cap is None:
                    hits = np.flatnonzero(lv > thr)
                    if len(hits):
                        k = int(hits[0])
                        t_samp = pos + k * nfft
                        pre = int(p["pre_s"] * fs)
                        start = max(t_samp - pre, ring.total - int(ring.size * 0.9), r.settle_mark)
                        cap = self._open(start, t_samp, fs, fc, thr, float(lv[k]))
                        self.state = "recording"
                        self.board.note(f"Trigger {self.id}: signal in {p['f_lo'] / 1e6:.4f}-{p['f_hi'] / 1e6:.4f} MHz "
                                        f"({lv[k]:.1f} dBFS) - recording", "event")
                if cap is not None:
                    self._write_until(cap, pos + chunk)
                    above = lv > thr - p["hysteresis_db"]
                    if above.any():
                        cap["last_above"] = pos + int(np.flatnonzero(above)[-1]) * nfft + nfft
                        cap["peak_db"] = max(cap["peak_db"], float(lv.max()))
                    stop_at = self._stop_point(cap, fs)
                    if stop_at is not None and ring.total >= stop_at:
                        self._write_until(cap, stop_at)
                        self._close(cap, fs, fc)
                        cap = None
                        self.state = "armed" if len(self.events) < p["max_events"] else "done"
                        hist = hist[-200:]
                pos += chunk
        except Exception as e:  # pragma: no cover
            self.error = f"{type(e).__name__}: {e}"
        finally:
            if cap is not None:
                try:
                    self._close(cap, fs, fc, cut=True)
                except Exception:
                    pass
            if self.state not in ("failed",):
                self.state = "stopped" if self.stop_ev.is_set() else ("done" if self.events else "timed out")
            self.first_done.set()
            self._release()

    def _stop_point(self, cap, fs):
        p = self.p
        if cap["written"] - cap["start"] >= int(p["max_record_s"] * fs):
            return cap["start"] + int(p["max_record_s"] * fs)
        if p["stop"] == "timer":
            return cap["trigger"] + int(p["record_s"] * fs)
        quiet_for = int(p["hold_s"] * fs)
        if self.radio.ring.total - cap["last_above"] >= quiet_for:
            return cap["last_above"] + int(p["post_s"] * fs)
        return None

    def _open(self, start, trig, fs, fc, thr, lvl):
        p = self.p
        ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
        safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in p.get("label") or "")[:30]
        folder = os.path.join(self.radio.cfg.data_root, "captures")
        os.makedirs(folder, exist_ok=True)
        base = f"{ts}_{(p['f_lo'] + p['f_hi']) / 2e6:.4f}MHz_trig{len(self.events) + 1}" + (f"_{safe}" if safe else "")
        path = os.path.join(folder, base + ".cfile")
        return {"path": path, "f": open(path + ".part", "wb"), "start": start, "trigger": trig, "written": start,
                "last_above": trig, "peak_db": lvl, "threshold": thr, "t_wall": time.time()}

    def _write_until(self, cap, upto):
        ring = self.radio.ring
        upto = min(upto, ring.total)
        while cap["written"] < upto:
            n = min(upto - cap["written"], 1 << 20)
            ring.read(cap["written"], n).tofile(cap["f"])
            cap["written"] += n

    def _close(self, cap, fs, fc, cut=False):
        p = self.p
        cap["f"].close()
        n = cap["written"] - cap["start"]
        part = cap["path"] + ".part"
        out_fs, out_fc, path = fs, fc, cap["path"]
        bw = p["f_hi"] - p["f_lo"]
        if p.get("narrow") and bw < fs / 3 and n * 8 < 1.2e9:
            x = np.fromfile(part, dtype=np.complex64)
            off = (p["f_lo"] + p["f_hi"]) / 2 - fc
            y, out_fs = dsp.zoom(x, fs, off, bw * 1.25)
            out_fc = fc + off
            y.tofile(path)
            os.remove(part)
        else:
            os.replace(part, path)
        pre_s = (cap["trigger"] - cap["start"]) / fs
        ev = {"n": len(self.events) + 1, "path": path, "time": datetime.fromtimestamp(cap["t_wall"] - pre_s).strftime(
              "%H:%M:%S.%f")[:-3], "duration_s": round(n / fs, 6), "pre_trigger_s": round(pre_s, 6),
              "signal_s": round(max(0, cap["last_above"] - cap["trigger"]) / fs, 6),
              "peak_dbfs": round(cap["peak_db"], 1), "threshold_dbfs": round(cap["threshold"], 1),
              "sample_rate": out_fs, "center_freq_hz": out_fc}
        if cut:
            ev["note"] = "cut short (recorder stopped)"
        with open(os.path.splitext(path)[0] + ".json", "w", encoding="utf-8") as f:
            json.dump({"path": path, "center_freq_hz": out_fc, "sample_rate": out_fs,
                       "samples": int(os.path.getsize(path) // 8), "format": "complex64",
                       "time": ev["time"], "trigger": {k: v for k, v in ev.items() if k != "path"},
                       "band_hz": [p["f_lo"], p["f_hi"]], "source": "record_on_signal"}, f, indent=2)
        self.events.append(ev)
        self.first_done.set()
        self.board.note(f"Trigger {self.id}: saved {ev['duration_s']:.3f} s → {os.path.basename(path)}", "event")

    def _release(self):
        if self._lease:
            try:
                self.radio.lease.release(self._lease)
            except Exception:
                pass
            self._lease = None
