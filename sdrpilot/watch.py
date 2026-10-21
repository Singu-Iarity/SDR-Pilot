"""Continuous spectrum recorder + LLM-readable views of it.

Every incoming IQ sample goes through an FFT (no gaps), and is reduced to one row per time slice
(default 50 ms) holding, per frequency bin, the PEAK power and the MEAN power in that slice. A few minutes of
history are kept. Queries turn any window of that history into:

  * a text waterfall (characters = dB above the noise floor, peak-held so short bursts are never lost)
  * a transmitter table (frequency, bandwidth, strength, duty cycle, bursts, burst length, repeat interval)
  * a cursor so the next query returns only what is new
"""
from __future__ import annotations

import threading
import time
from datetime import datetime

import numpy as np
from . import deadline

SCALE = [(3, " "), (6, "."), (10, ":"), (15, "-"), (20, "="), (25, "+"), (30, "*"), (40, "#")]
TOP_CHAR = "@"
LEGEND = "dB above noise floor:  ' '<3  .3-6  :6-10  -10-15  =15-20  +20-25  *25-30  #30-40  @>40"


def _char(db: float) -> str:
    for lim, ch in SCALE:
        if db < lim:
            return ch
    return TOP_CHAR


def _fmt_mhz(f: float, step: float) -> str:
    dec = 3 if step < 0.05e6 else 2 if step < 0.5e6 else 1
    return f"{f / 1e6:.{dec}f}"


class SpectrumRecorder:
    def __init__(self, radio, nfft: int = 1024, bins: int = 512, slice_s: float = 0.05, history_s: float = 300.0):
        self.radio = radio
        self.nfft, self.bins, self.slice_s = nfft, bins, slice_s
        self.max_rows = int(history_s / slice_s)
        self.peak = np.full((self.max_rows, bins), -150, dtype=np.float16)
        self.mean = np.full((self.max_rows, bins), -150, dtype=np.float16)
        self.t = np.zeros(self.max_rows)          # wall-clock start time of each row
        self.fc = np.zeros(self.max_rows)         # centre frequency for each row
        self.fs = np.zeros(self.max_rows)         # sample rate for each row
        self.n = 0                                # rows ever written (row id = cursor)
        self.lock = threading.Lock()
        self.stop_ev = threading.Event()
        self.thread: threading.Thread | None = None
        self.started = None
        self.cpu_s = 0.0

    # ------------------------------------------------------------ recording
    def start(self) -> None:
        if self.thread and self.thread.is_alive():
            return
        self.stop_ev.clear()
        self.started = time.time()
        self.thread = threading.Thread(target=self._run, daemon=True, name="sdrpilot-spectrum-recorder")
        self.thread.start()

    def stop(self) -> None:
        self.stop_ev.set()

    @property
    def running(self) -> bool:
        return bool(self.thread and self.thread.is_alive())

    def _run(self) -> None:
        r = self.radio
        ring = r.ring
        pos = ring.total
        win = np.hanning(self.nfft).astype(np.float32)
        norm = float(win.sum() ** 2)
        epoch = r.epoch
        while not self.stop_ev.is_set():
            fs = r.sample_rate
            if not fs or not r.connected:
                deadline.sleep(0.2)
                pos = ring.total
                continue
            per_slice = max(self.nfft, int(round(fs * self.slice_s / self.nfft)) * self.nfft)
            if not ring.wait_until(pos + per_slice, 1.0):
                continue
            if r.epoch != epoch or pos < r.settle_mark:
                epoch = r.epoch
                pos = max(pos, r.settle_mark)
                if ring.total < pos + per_slice:
                    continue
            if ring.total - pos > ring.size // 2:  # fell far behind (very slow PC): skip ahead, don't lag
                pos = ring.total - per_slice
            t0 = time.time()
            x = ring.read(pos, per_slice)
            row_time = time.time() - (ring.total - pos) / fs
            pos += per_slice
            fr = x.reshape(-1, self.nfft) * win
            p = np.abs(np.fft.fftshift(np.fft.fft(fr, axis=1), axes=1)) ** 2 / norm
            k = self.nfft // self.bins
            if k > 1:
                p = p.reshape(p.shape[0], self.bins, k).max(axis=2)
            pk = 10 * np.log10(p.max(axis=0) + 1e-20)
            mn = 10 * np.log10(p.mean(axis=0) + 1e-20)
            with self.lock:
                i = self.n % self.max_rows
                self.peak[i] = pk
                self.mean[i] = mn
                self.t[i] = row_time
                self.fc[i] = r.center_freq
                self.fs[i] = fs
                self.n += 1
            self.cpu_s += time.time() - t0

    # ------------------------------------------------------------ queries
    def wait_for(self, until: float, timeout: float) -> None:
        end = time.time() + timeout
        while time.time() < end:
            with self.lock:
                last_t = self.t[(self.n - 1) % self.max_rows] if self.n else 0
            if last_t >= until:
                return
            deadline.sleep(0.05)

    def rows(self, t_from: float | None = None, since_cursor: int | None = None):
        """Rows of the most recent tuning only (a retune starts a new segment)."""
        with self.lock:
            n = self.n
            lo = max(0, n - self.max_rows)
            if since_cursor is not None:
                lo = max(lo, since_cursor)
            idx = np.arange(lo, n)
            if idx.size == 0:
                return None
            ring_i = idx % self.max_rows
            t = self.t[ring_i].copy()
            fc = self.fc[ring_i].copy()
            fs = self.fs[ring_i].copy()
            peak = self.peak[ring_i].astype(np.float32)
            mean = self.mean[ring_i].astype(np.float32)
        same = (fc == fc[-1]) & (fs == fs[-1])
        # keep the trailing run with the current tuning
        first = len(same) - np.argmin(same[::-1]) if not same.all() else 0
        sel = slice(first, None)
        t, peak, mean, ids = t[sel], peak[sel], mean[sel], idx[sel]
        if t_from is not None:
            m = t >= t_from
            t, peak, mean, ids = t[m], peak[m], mean[m], ids[m]
        if len(t) == 0:
            return None
        return {"t": t, "peak": peak, "mean": mean, "ids": ids, "fc": float(fc[-1]), "fs": float(fs[-1]),
                "retuned_in_window": not same.all()}


def _tracks(excess: np.ndarray, thr: float, gap_rows: int = 1):
    """Blob tracking on a (rows x bins) dB-above-floor matrix -> bursts (row0,row1,bin0,bin1,peak)."""
    mask = excess > thr
    open_t: list[dict] = []
    done: list[dict] = []
    for r in range(mask.shape[0]):
        row = mask[r]
        segs = []
        if row.any():
            idx = np.flatnonzero(row)
            br = np.flatnonzero(np.diff(idx) > 3)
            starts = np.concatenate([[idx[0]], idx[br + 1]])
            ends = np.concatenate([idx[br], [idx[-1]]]) + 1
            segs = list(zip(starts.tolist(), ends.tolist()))
        still = []
        for tr in open_t:
            (still if r - tr["r1"] <= gap_rows + 1 else done).append(tr)
        open_t = still
        for a, b in segs:
            pk = float(excess[r, a:b].max())
            hit = next((tr for tr in open_t if a <= tr["b1"] + 2 and b >= tr["b0"] - 2), None)
            if hit:
                hit["b0"], hit["b1"] = min(hit["b0"], a), max(hit["b1"], b)
                hit["r1"], hit["peak"] = r, max(hit["peak"], pk)
                hit["rows"] += 1
                w = excess[r, a:b].clip(min=0)
                hit["wf"] += float((np.arange(a, b) * w).sum())
                hit["w"] += float(w.sum())
            else:
                w = excess[r, a:b].clip(min=0)
                open_t.append({"r0": r, "r1": r, "b0": a, "b1": b, "peak": pk, "rows": 1,
                               "wf": float((np.arange(a, b) * w).sum()), "w": float(w.sum())})
    done.extend(open_t)
    return done


def render(rec: SpectrumRecorder, seconds: float = 10.0, since_cursor: int | None = None,
           freq_min_hz: float | None = None, freq_max_hz: float | None = None, cols: int = 80, rows: int = 24,
           threshold_db: float = 8.0, end_time: float | None = None) -> dict:
    t_from = None if since_cursor is not None else (end_time or time.time()) - seconds
    d = rec.rows(t_from=t_from, since_cursor=since_cursor)
    if d is None:
        return {"error": "no spectrum data yet - is the radio connected?", "cursor": rec.n}
    t, peak, mean, fc, fs = d["t"], d["peak"], d["mean"], d["fc"], d["fs"]
    nb = peak.shape[1]
    freqs = fc + (np.arange(nb) - nb / 2 + 0.5) * fs / nb
    bin_hz = fs / nb
    lo = 0 if freq_min_hz is None else int(np.clip(np.searchsorted(freqs, freq_min_hz), 0, nb - 1))
    hi = nb if freq_max_hz is None else int(np.clip(np.searchsorted(freqs, freq_max_hz), lo + 1, nb))
    peak, mean, freqs = peak[:, lo:hi], mean[:, lo:hi], freqs[lo:hi]
    # robust global noise floor (continuous signals stay visible; per-bin floors would hide them)
    floor = float(np.median(np.percentile(mean, 20, axis=0)))          # average noise level (reported)
    floor_pk = float(np.median(np.percentile(peak, 20, axis=0)))       # noise level of peak-held rows
    excess = peak - floor_pk
    # blank the zero-IF DC spike (exact centre +-1 bin) for detection
    c = int(np.argmin(np.abs(freqs - fc)))
    dc = slice(max(0, c - 1), c + 2) if abs(freqs[c] - fc) < bin_hz else slice(0, 0)
    det = excess.copy()
    det[:, dc] = np.minimum(det[:, dc], 0)

    # ---- text waterfall: peak-hold into rows x cols cells
    R = min(rows, len(t))
    C = min(cols, peak.shape[1])
    redges = np.linspace(0, len(t), R + 1).astype(int)
    cedges = np.linspace(0, peak.shape[1], C + 1).astype(int)
    grid = np.maximum.reduceat(np.maximum.reduceat(excess, redges[:-1], axis=0), cedges[:-1], axis=1)
    # bigger cells peak-hold more noise: re-reference so plain noise prints as blanks
    grid = grid - float(np.clip(np.median(grid), 0, 6))
    span = freqs[-1] - freqs[0] + bin_hz
    col_hz = span / C
    lines = []
    # frequency ruler: a label every ~16 columns
    ruler = [" "] * C
    ticks = []
    for ci in range(0, C, 16):
        lab = _fmt_mhz(freqs[cedges[ci]], col_hz)
        ticks.append(f"col {ci}={lab}")
        for k, ch in enumerate("|" + lab):
            if ci + k < C:
                ruler[ci + k] = ch
    lines.append("time(s)  " + "".join(ruler))
    t_end = t[-1] + rec.slice_s
    for ri in range(R):
        ts = t[redges[ri]] - t_end
        lines.append(f"{ts:7.1f}  " + "".join(_char(v) for v in grid[ri]))
    dc_col = int((fc - freqs[0]) / col_hz) if freqs[0] <= fc <= freqs[-1] else None

    # ---- transmitters
    bursts = _tracks(det, threshold_db, gap_rows=0)
    nrows = len(t)
    groups: list[dict] = []
    for b in sorted(bursts, key=lambda b: (b["wf"] / max(b["w"], 1e-9))):
        cen = b["wf"] / max(b["w"], 1e-9)
        f = float(np.interp(cen, np.arange(len(freqs)), freqs))
        bw = (b["b1"] - b["b0"]) * bin_hz
        g = groups[-1] if groups else None
        if g and abs(f - g["f"]) < max(bin_hz * 4, 0.3 * min(bw, g["bw"])):
            g["items"].append(b)
            g["f"] = float(np.mean([g["f"], f]))
            g["bw"] = max(g["bw"], bw)
        else:
            groups.append({"f": f, "bw": bw, "items": [b]})
    # on-time inside each burst, estimated from mean vs peak power (a burst lasting a fraction d of a row raises
    # the row's mean by ~d x its peak) - gives sub-row burst lengths without storing every FFT frame
    P = 10 ** (peak / 10)
    M = 10 ** (mean / 10)
    N = 10 ** (floor / 10)
    for b in bursts:
        sl = (slice(b["r0"], b["r1"] + 1), slice(b["b0"], b["b1"]))
        num = float(np.clip(M[sl] - N, 0, None).sum())
        den = float(np.clip(P[sl] - N, 1e-30, None).sum())
        b["on_s"] = min(1.0, num / den) * (b["r1"] - b["r0"] + 1) * rec.slice_s
    tx = []
    for g in groups:
        it = sorted(g["items"], key=lambda b: b["r0"])
        covered = set()
        for b in it:
            covered.update(range(b["r0"], b["r1"] + 1))
        rows_on = len(covered)
        starts = [t[b["r0"]] for b in it]
        durs = [(b["r1"] - b["r0"] + 1) * rec.slice_s for b in it]
        duty_rows = rows_on / nrows
        on_total = sum(b["on_s"] for b in it)
        duty = min(1.0, on_total / (nrows * rec.slice_s))
        cont = duty_rows > 0.9 and duty > 0.5
        entry = {
            "freq_mhz": round(g["f"] / 1e6, 5),
            "bandwidth_khz": round(g["bw"] / 1e3, 1),
            "peak_db_above_floor": round(max(b["peak"] for b in it), 1),
            "kind": "continuous" if cont else "intermittent",
            "duty_pct": round(100 * duty, 2),
            "bursts": len(it),
            "burst_span_ms": None if cont else (f"<{rec.slice_s * 1e3:g}" if max(durs) <= rec.slice_s
                                                else round(float(np.median(durs)) * 1e3)),
            "on_time_ms_est": None if cont else round(float(np.median([b["on_s"] for b in it])) * 1e3, 2),
            "repeat_every_s": round(float(np.median(np.diff(starts))), 2) if len(starts) > 2 else None,
            "first_seen": datetime.fromtimestamp(starts[0]).strftime("%H:%M:%S.%f")[:-4],
            "last_seen": datetime.fromtimestamp(t[it[-1]["r1"]]).strftime("%H:%M:%S.%f")[:-4],
        }
        tx.append(entry)
    tx.sort(key=lambda e: -e["peak_db_above_floor"])
    return {
        "center_freq_mhz": round(fc / 1e6, 6),
        "sample_rate": fs,
        "view_mhz": [round(freqs[0] / 1e6, 4), round((freqs[-1] + bin_hz) / 1e6, 4)],
        "window_s": round(nrows * rec.slice_s, 2),
        "row_s": round(nrows * rec.slice_s / R, 3),
        "col_khz": round(col_hz / 1e3, 2),
        "noise_floor_dbfs": round(floor, 1),
        "peak_noise_dbfs": round(floor_pk, 1),
        "threshold_db": threshold_db,
        "retuned_during_window": d["retuned_in_window"],
        "cursor": int(d["ids"][-1]) + 1,
        "legend": LEGEND + ("" if dc_col is None else f"   (col {dc_col} = tuning centre/DC spike)"),
        "waterfall": "\n".join(lines),
        "transmitters": tx[:30],
        "note": "every sample was analysed; each character is the PEAK in its time/frequency cell, so even "
                "sub-millisecond bursts show up. burst_span_ms has 50 ms resolution; on_time_ms_est is the energy-"
                "based on-time per burst (approx.; low for FSK, = pulse on-time for OOK). For exact timing use "
                "iq_samples(trigger_db=...) or capture_iq. Pass cursor back as since_cursor to get only newer data.",
    }
