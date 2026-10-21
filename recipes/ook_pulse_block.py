"""OOK / ASK pulse decoder (GNU Radio embedded Python block) - the rtl_433-style "analyze" step.

Input: the signal's magnitude (float) at samp_rate, e.g. freq_xlating_fir_filter -> complex_to_mag.
For every transmission it measures pulse and gap widths, groups them into rows (split by long gaps), guesses the
line code and prints/writes the bits:
  PWM (pulse width carries the bit: long pulse = 1)   - most 433 MHz sensors, remotes
  PPM (gap width carries the bit: long gap = 1)
  Manchester (all pulses/gaps are 1T or 2T)
Each burst is printed and appended to out_file as JSON."""
import json
import time

import numpy as np
from gnuradio import gr


def _bits_hex(bits):
    s = "".join(str(int(b)) for b in bits)
    pad = s + "0" * (-len(s) % 8)
    return " ".join(f"{int(pad[i:i + 8], 2):02x}" for i in range(0, len(pad), 8)), s


def _two_levels(w):
    """Split widths into short/long by the largest jump between sorted values; None if they're all alike."""
    w = np.sort(np.asarray(w, dtype=float))
    if len(w) < 4:
        return None
    ratios = w[1:] / np.maximum(w[:-1], 1e-12)
    i = int(np.argmax(ratios))
    if ratios[i] < 1.5:
        return None
    return float(np.median(w[: i + 1])), float(np.median(w[i + 1:])), float((w[i] + w[i + 1]) / 2)


class blk(gr.sync_block):
    def __init__(self, samp_rate=250e3, threshold_db=12.0, end_gap_ms=12.0, min_pulses=8, out_file="ook_bursts.jsonl"):
        gr.sync_block.__init__(self, name="OOK pulse decoder", in_sig=[np.float32], out_sig=None)
        self.fs = float(samp_rate)
        self.thr_db = float(threshold_db)
        self.end_gap = int(end_gap_ms * 1e-3 * self.fs)
        self.min_pulses = int(min_pulses)
        self.out_file = out_file
        self.noise = None
        self.state = False
        self.run = 0          # length of the current on/off run (samples)
        self.edges = []       # [(is_on, length_samples)] of the burst being collected
        self.idle = 0
        self.count = 0
        self.t_burst = 0.0
        self.pos = 0
        self.k = max(1, int(round(self.fs * 20e-6)))
        self.tail = np.zeros(0, dtype=np.float32)

    def work(self, input_items, output_items):
        x = input_items[0]
        n = len(x)
        # light smoothing (~20 us) so noise spikes don't count as pulses
        k = self.k
        xx = np.concatenate([self.tail, x]) if len(self.tail) else x
        y = np.convolve(xx, np.ones(k, dtype=np.float32) / k, mode="valid") if k > 1 else xx
        self.tail = xx[-(k - 1):] if k > 1 else self.tail
        # noise floor: median of the smoothed magnitude; follows drops quickly and rises slowly
        med = float(np.median(y)) if len(y) else 0.0
        if self.noise is None or med < self.noise:
            self.noise = med
        else:
            self.noise = 0.995 * self.noise + 0.005 * med
        hi = max(self.noise, 1e-9) * 10 ** (self.thr_db / 20)
        lo = max(hi * 0.5, self.noise * 2)  # hysteresis
        on = self.state
        for v in y.tolist():  # simple and robust; rates here are low after decimation
            if on:
                if v < lo:
                    self.edges.append((True, self.run))
                    on, self.run = False, 0
            elif v > hi:
                if self.edges:
                    self.edges.append((False, self.run))
                else:
                    self.t_burst = time.time()
                on, self.run = True, 0
            self.run += 1
            if not on and self.edges and self.run > self.end_gap:
                self._finish()
                self.run = 0
        self.state = on
        self.pos += n
        return n

    def _finish(self):
        edges, self.edges = self.edges, []
        pulses = [l for o, l in edges if o]
        if len(pulses) < self.min_pulses:
            return
        us = 1e6 / self.fs
        # split into rows at gaps much longer than the typical gap
        all_gaps = [l for o, l in edges if not o]
        # gaps much longer than the data gaps and pulses separate rows (sync gaps / repeats)
        row_gap = max(3 * float(np.median(all_gaps)), 2.5 * float(max(pulses))) if all_gaps else 1e18
        gaps = [g for g in all_gaps if g <= row_gap]
        gl = _two_levels(gaps) if gaps else None
        rows, cur = [], []
        for o, l in edges:
            if not o and l > row_gap:
                if cur:
                    rows.append(cur)
                cur = []
            else:
                cur.append((o, l))
        if cur:
            rows.append(cur)
        pl = _two_levels(pulses)
        out_rows = []
        for r in rows:
            rp = [l for o, l in r if o]
            rg = [l for o, l in r if not o]
            row = {"pulses": len(rp)}
            if pl:
                row["pwm"] = _bits_hex([1 if l > pl[2] else 0 for l in rp])[0]
            if gl:
                row["ppm"] = _bits_hex([1 if l > gl[2] else 0 for l in rg])[0]
            out_rows.append(row)
        widths = sorted(set([round(p * us, -1) for p in pulses] + [round(g * us, -1) for g in gaps]))
        similar = bool(pl and gl and abs(gl[0] - pl[0]) < 0.35 * pl[0] and abs(gl[1] - pl[1]) < 0.35 * pl[1]
                       and abs(pl[1] / pl[0] - 2) < 0.4)
        coding = "Manchester-like" if similar else "PWM" if pl else "PPM" if gl else "unknown"
        self.count += 1
        rec = {"n": self.count, "time": time.strftime("%H:%M:%S", time.localtime(self.t_burst)), "coding_guess": coding,
               "pulses": len(pulses), "rows": len(rows),
               "short_pulse_us": round(pl[0] * us) if pl else round(float(np.median(pulses)) * us),
               "long_pulse_us": round(pl[1] * us) if pl else None,
               "short_gap_us": round(gl[0] * us) if gl else None, "long_gap_us": round(gl[1] * us) if gl else None,
               "row_bits": out_rows[:6], "distinct_widths_us": widths[:12]}
        print("SPDATA " + json.dumps({"kind": "ook", "t": round(time.time(), 3), **rec}), flush=True)
        try:
            with open(self.out_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec) + "\n")
        except OSError:
            pass

    def stop(self):
        if self.edges:
            self._finish()
        print(f"OOK summary: {self.count} bursts", flush=True)
        return True
