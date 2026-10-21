"""Signal analysis used by the AI's "eyes": spectra, peak finding, burst finding,
modulation fingerprinting and output-file inspection.  numpy only."""
from __future__ import annotations

import math
import re
import os
import wave

import numpy as np

from . import plot


# ---------------------------------------------------------------- spectra

def stft_db(x: np.ndarray, nfft: int, max_frames: int | None = None) -> np.ndarray:
    """Non-overlapping Hann-windowed FFT frames, fftshifted, in dBFS (full-scale tone = 0 dB)."""
    nfr = len(x) // nfft
    if max_frames and nfr > max_frames:
        # take evenly spaced frames to bound CPU
        idx = np.linspace(0, nfr - 1, max_frames).astype(int)
        fr = np.stack([x[i * nfft:(i + 1) * nfft] for i in idx])
    else:
        fr = x[: nfr * nfft].reshape(nfr, nfft)
    w = np.hanning(nfft).astype(np.float32)
    X = np.fft.fftshift(np.fft.fft(fr * w, axis=1), axes=1)
    p = (np.abs(X) ** 2) / (w.sum() ** 2)
    return 10 * np.log10(p + 1e-20)


def _segments(mask: np.ndarray, max_gap: int = 1) -> list[tuple[int, int]]:
    """Contiguous True runs [a, b) in a 1-D mask, bridging gaps <= max_gap."""
    idx = np.flatnonzero(mask)
    if idx.size == 0:
        return []
    breaks = np.flatnonzero(np.diff(idx) > max_gap + 1)
    starts = np.concatenate([[idx[0]], idx[breaks + 1]])
    ends = np.concatenate([idx[breaks], [idx[-1]]]) + 1
    return list(zip(starts.tolist(), ends.tolist()))


def _smooth_db(frames_db: np.ndarray, kt: int = 2, kf: int = 3) -> np.ndarray:
    """Box-average power over kt frames x kf bins (reduces noise speckle before thresholding)."""
    p = 10 ** (frames_db / 10)
    if kf > 1:
        c = np.cumsum(np.pad(p, ((0, 0), (kf // 2 + 1, kf // 2)), mode="edge"), axis=1)
        p = (c[:, kf:] - c[:, :-kf]) / kf
    if kt > 1 and p.shape[0] >= kt:
        c = np.cumsum(np.pad(p, ((kt - 1, 0), (0, 0)), mode="edge"), axis=0)
        p = (c[kt - 1:] - np.vstack([np.zeros((1, p.shape[1])), c[:-kt]])) / kt
    return 10 * np.log10(p + 1e-20)


def _merge_close(segs: list[tuple[int, int]], frac: float = 0.15) -> list[tuple[int, int]]:
    """Merge segments separated by less than frac x the wider neighbour (spectral sidelobes, FSK tones)."""
    cur = [list(x) for x in segs]
    while True:
        out: list[list[int]] = []
        for a, b in cur:
            if out and a - out[-1][1] <= frac * max(b - a, out[-1][1] - out[-1][0]):
                out[-1][1] = max(b, out[-1][1])
            else:
                out.append([a, b])
        if len(out) == len(cur):
            return [(a, b) for a, b in out]
        cur = out


def find_peaks(frames_db: np.ndarray, fs: float, fc: float, threshold_db: float = 8.0,
               max_peaks: int = 12) -> tuple[list[dict], dict]:
    nfft = frames_db.shape[1]
    bin_hz = fs / nfft
    sm = _smooth_db(frames_db, 1, 3)
    floor_bin = np.median(frames_db, axis=0)
    floor = float(np.median(floor_bin))
    hold = sm.max(axis=0)
    hold_noise = float(np.median(hold))  # most bins are noise: this is the noise peak-hold level
    avg = 10 * np.log10(np.mean(10 ** (frames_db / 10), axis=0))
    mask = hold > hold_noise + threshold_db
    segs = _merge_close(_segments(mask, max_gap=max(1, nfft // 1024)))
    duty_thr = floor + threshold_db + 6
    peaks = []
    for a, b in segs:
        pw = 10 ** (hold[a:b] / 10)
        centroid = a + float(np.sum(np.arange(b - a) * pw) / pw.sum())
        k = a + int(np.argmax(hold[a:b]))
        off = (centroid - nfft / 2) * bin_hz
        seg_frames = sm[:, a:b].max(axis=1)
        duty = float(np.mean(seg_frames > duty_thr))
        peaks.append({
            "freq_hz": round(fc + off, 1),
            "offset_hz": round(off, 1),
            "bandwidth_hz": round((b - a) * bin_hz, 1),
            "peak_hold_dbfs": round(float(hold[k]), 1),
            "avg_dbfs": round(float(avg[a:b].max()), 1),
            "snr_db": round(float(hold[k] - floor), 1),
            "duty_cycle": round(duty, 4),
            "kind": "continuous" if duty > 0.9 else ("intermittent" if duty > 0.02 else "rare burst"),
            "power_db": float(hold[k]),
        })
        if b - a <= 8 and abs(off) < 4 * bin_hz:
            peaks[-1]["note"] = "at exact center - probably the SDR's DC spike, not a real signal"
    peaks.sort(key=lambda p: -p["snr_db"])
    peaks = peaks[:max_peaks]
    info = {"noise_floor_dbfs": round(floor, 1), "bin_hz": round(bin_hz, 2), "frames": int(frames_db.shape[0]),
            "avg": avg, "hold": hold}
    return peaks, info


def zoom(x: np.ndarray, fs: float, offset_hz: float, span_hz: float) -> tuple[np.ndarray, float]:
    """Close-up of one part of the band: shift offset_hz to 0, keep +/- span/2 (ideal FFT filter), decimate.
    Returns the new samples and sample rate (= the span shown)."""
    span_hz = float(min(max(span_hz, fs / 4096), fs))
    dec = max(1, int(fs // span_hz))
    n = len(x)
    if offset_hz:
        x = x * np.exp(-2j * np.pi * offset_hz * np.arange(n) / fs).astype(np.complex64)
    if dec == 1:
        return x.astype(np.complex64), fs
    X = np.fft.fft(x)
    f = np.fft.fftfreq(n, 1 / fs)
    X[np.abs(f) > fs / dec / 2] = 0
    y = np.fft.ifft(X)[::dec].astype(np.complex64)
    return y, fs / dec


def spectrum_report(x: np.ndarray, fs: float, fc: float, nfft: int = 4096, threshold_db: float = 8.0,
                    max_peaks: int = 12, title: str = "", annotate: bool = False, detail: str = "normal",
                    dc_hz: float | None = None, img: dict | None = None, ann=None,
                    spur_freqs: list | None = None) -> tuple[dict, bytes]:
    img = img or {}
    frames = stft_db(x, nfft, max_frames=max(4000, int(img.get("rows") or 0) * 2))
    peaks, info = find_peaks(frames, fs, fc, threshold_db, max_peaks)
    rows = min(int(img.get("rows") or 300), frames.shape[0])
    # aggregate waterfall rows by max so short bursts stay visible
    edges = np.linspace(0, frames.shape[0], rows + 1).astype(int)
    wf = np.maximum.reduceat(frames, edges[:-1], axis=0)
    trace = np.maximum(info["avg"], info["hold"] - 0)  # show peak-hold so bursts are visible
    big = detail == "high"
    on = parse_annotations(ann) if ann is not None else (set(ANNOTATIONS) if annotate else set())
    feats = detect_features(frames, fs, fc, peaks, spur_freqs)
    png = plot.waterfall_plot(wf, fc - fs / 2, fc + fs / 2, len(x) / fs, info["hold"], peaks,
                              title or f"{fc / 1e6:.4f} MHZ  {fs / 1e6:.3f} MSPS  PEAK HOLD",
                              min_db=img.get("db_min", info["noise_floor_dbfs"] - 3), max_db=img.get("db_max"),
                              width=int(img.get("width") or (1800 if big else 1100)),
                              wf_h=int(img.get("height") or (480 if big else 300)),
                              sp_h=int(img.get("spectrum_height") or (280 if big else 200)), annotate=annotate,
                              dc_hz=dc_hz, features=feats, ann=on, n_frames=frames.shape[0])
    del trace
    for p in peaks:
        p.pop("power_db", None)
    dc = None
    if dc_hz is not None:  # this SDR's own DC spike is not a signal: keep it out of the list
        tol = max(4 * fs / nfft, 2e3)
        for p in list(peaks):
            if abs(p["freq_hz"] - dc_hz) <= tol and p["bandwidth_hz"] <= max(8 * fs / nfft, 6e3):
                peaks.remove(p)
                dc = {"freq_hz": p["freq_hz"], "peak_hold_dbfs": p["peak_hold_dbfs"],
                      "note": "the SDR's own DC spike at the tuning centre - NOT a transmitter. A real signal "
                              "there would be wider; tune ~200 kHz off to check"}
    summary = {"center_freq_hz": fc, "sample_rate": fs, "span_hz": fs, "seconds": round(len(x) / fs, 4),
               "noise_floor_dbfs": info["noise_floor_dbfs"], "bin_hz": info["bin_hz"],
               "threshold_db": threshold_db, "signals": peaks}
    if dc:
        summary["dc_spike"] = dc
    summary["features"] = _feature_summary(feats, len(x) / fs, frames.shape[0])
    summary["annotations"] = {"shown": sorted(on), "available": list(ANNOTATIONS)}
    return summary, png


def _feature_summary(feats: dict, span_s: float, n_frames: int) -> dict:
    """The detected features in the result text (transient frames as times)."""
    out = {k: v for k, v in feats.items() if v and k in ("wideband", "images", "edges", "spurs")}
    if feats.get("transients"):
        out["transients"] = [{"t_s": round(t["frame_from"] / max(n_frames, 1) * span_s, 5),
                              "duration_s": round(max(1, t["frame_to"] - t["frame_from"]) / max(n_frames, 1) * span_s, 5),
                              "rise_db": t["rise_db"],
                              "note": "the whole band jumped - impulse noise, a strong wideband burst or a stream gap"}
                             for t in feats["transients"]]
    return out


# ---------------------------------------------------------------- bursts / events

def find_events(x: np.ndarray, fs: float, fc: float, nfft: int = 256, threshold_db: float = 10.0,
                merge_gap_s: float = 0.002, min_duration_s: float = 0.0, max_events: int = 200,
                t_offset: float = 0.0, floor_bin: np.ndarray | None = None) -> tuple[list[dict], dict]:
    """Time-frequency blob detection: returns discrete transmissions (bursts) with time and frequency."""
    frames = stft_db(x, nfft)
    if frames.shape[0] == 0:
        return [], {"frames": frames}
    sm = _smooth_db(frames, 2, 3)
    if floor_bin is None:
        floor_bin = np.median(sm, axis=0)
    floor = float(np.median(floor_bin))
    # per-bin floor: continuous carriers (and the DC spike) are part of the floor, so only
    # transmissions that come and go are reported
    mask = sm > floor_bin + threshold_db
    c = nfft // 2
    mask[:, max(0, c - 2):c + 3] = False  # the zero-IF DC spike is never a transmission
    dt = nfft / fs
    bin_hz = fs / nfft
    gap_frames = max(1, int(round(merge_gap_s / dt)))
    open_ev: list[dict] = []
    done: list[dict] = []
    active = np.flatnonzero(mask.any(axis=1))
    for ti in active.tolist():
        # close events that have been silent for too long
        still = []
        for ev in open_ev:
            (still if ti - ev["t1"] <= gap_frames else done).append(ev)
        open_ev = still
        row = mask[ti]
        if True:
            for a, b in _merge_close(_segments(row, max_gap=max(2, nfft // 64)), 0.5):
                pk = float(frames[ti, a:b].max())
                pw = 10 ** (frames[ti, a:b] / 10)
                cen = a + float(np.sum(np.arange(b - a) * pw) / pw.sum())
                hits = [ev for ev in open_ev if a <= ev["b"] + 2 and b >= ev["a"] - 2]
                if hits:
                    match = hits[0]
                    for other in hits[1:]:  # segment bridges two events -> merge them
                        match["a"], match["b"] = min(match["a"], other["a"]), max(match["b"], other["b"])
                        match["t0"] = min(match["t0"], other["t0"])
                        match["pk"] = max(match["pk"], other["pk"])
                        match["wsum"] += other["wsum"]
                        match["csum"] += other["csum"]
                        match["frames"] += other["frames"]
                        open_ev.remove(other)
                    match["a"], match["b"] = min(match["a"], a), max(match["b"], b)
                    if match["t1"] != ti:
                        match["frames"] += 1
                    match["t1"] = ti
                    match["pk"] = max(match["pk"], pk)
                    match["wsum"] += pw.sum()
                    match["csum"] += cen * pw.sum()
                else:
                    open_ev.append({"a": a, "b": b, "t0": ti, "t1": ti, "pk": pk, "wsum": pw.sum(),
                                    "csum": cen * pw.sum(), "frames": 1})
    nfr = frames.shape[0]
    for ev in open_ev:
        ev["open_at_end"] = ev["t1"] >= nfr - 1 - gap_frames
    done.extend(open_ev)
    events = []
    for ev in sorted(done, key=lambda e: e["t0"]):
        dur = (ev["t1"] - ev["t0"] + 1) * dt
        if dur < min_duration_s or ev["frames"] * (ev["b"] - ev["a"]) < 4:
            continue
        cen = ev["csum"] / ev["wsum"]
        off = (cen - nfft / 2) * bin_hz
        events.append({
            "start_s": round(t_offset + ev["t0"] * dt, 6),
            "duration_s": round(dur, 6),
            "freq_hz": round(fc + off, 1),
            "offset_hz": round(off, 1),
            "bandwidth_hz": round((ev["b"] - ev["a"]) * bin_hz, 1),
            "peak_dbfs": round(ev["pk"], 1),
            "snr_db": round(ev["pk"] - floor, 1),
            "fill": round(ev["frames"] / (ev["t1"] - ev["t0"] + 1), 3),
        })
        if ev.get("open_at_end"):
            events[-1]["still_on_at_end"] = True
        if ev["t0"] == 0:
            events[-1]["already_on_at_start"] = True
    total = len(events)
    if total > max_events:
        events = sorted(events, key=lambda e: -e["snr_db"])[:max_events]
        events.sort(key=lambda e: e["start_s"])
    return events, {"frames": frames, "noise_floor_dbfs": round(floor, 1), "frame_s": dt, "bin_hz": bin_hz,
                    "total_events": total, "floor_bin": floor_bin, "block_floor_bin": np.median(sm, axis=0)}


def capture_report(x: np.ndarray, fs: float, fc: float, nfft: int = 256, threshold_db: float = 10.0,
                   merge_gap_s: float = 0.002, max_events: int = 60, title: str = "",
                   img: dict | None = None, ann=None, dc_hz: float | None = None,
                   spur_freqs: list | None = None) -> tuple[dict, bytes]:
    img = img or {}
    events, info = find_events(x, fs, fc, nfft, threshold_db, merge_gap_s, max_events=max_events)
    frames = info["frames"]
    rows = min(int(img.get("rows") or 300), max(1, frames.shape[0]))
    edges = np.linspace(0, frames.shape[0], rows + 1).astype(int)
    wf = np.maximum.reduceat(frames, edges[:-1], axis=0) if frames.shape[0] else np.zeros((1, nfft))
    hold = frames.max(axis=0) if frames.shape[0] else np.zeros(nfft)
    marks = [(e["start_s"], e["start_s"] + e["duration_s"], i) for i, e in enumerate(events)]
    pk = [{"freq_hz": e["freq_hz"], "power_db": e["peak_dbfs"], "bandwidth_hz": e.get("bandwidth_hz", 0),
           "peak_hold_dbfs": e["peak_dbfs"]} for e in events[:12]]
    on = parse_annotations(ann) if ann is not None else {"wideband", "transients", "edges"}
    feats = detect_features(frames, fs, fc, None, spur_freqs) if frames.shape[0] else {}
    png = plot.waterfall_plot(wf, fc - fs / 2, fc + fs / 2, len(x) / fs, hold, pk,
                              title or f"CAPTURE {fc / 1e6:.4f} MHZ  {len(x) / fs:.3f} S",
                              min_db=img.get("db_min", info.get("noise_floor_dbfs", -100) - 3), max_db=img.get("db_max"),
                              markers=marks, width=int(img.get("width") or 1100), wf_h=int(img.get("height") or 300),
                              sp_h=int(img.get("spectrum_height") or 200), features=feats, ann=on,
                              n_frames=frames.shape[0], dc_hz=dc_hz)
    # group events by frequency to summarize repeated transmitters
    groups_l: list[list[dict]] = []
    for e in sorted(events, key=lambda e: e["freq_hz"]):
        if groups_l:
            gbw = float(np.median([g["bandwidth_hz"] for g in groups_l[-1]]))
            tol = max(4 * fs / nfft, 0.25 * min(e["bandwidth_hz"], gbw))
        if groups_l and abs(e["freq_hz"] - np.mean([g["freq_hz"] for g in groups_l[-1]])) < tol:
            groups_l[-1].append(e)
        else:
            groups_l.append([e])
    groups = {i: sorted(g, key=lambda e: e["start_s"]) for i, g in enumerate(groups_l)}
    summary_groups = []
    for g in groups.values():
        starts = [e["start_s"] for e in g]
        summary_groups.append({
            "freq_hz": round(float(np.mean([e["freq_hz"] for e in g])), 1),
            "count": len(g),
            "typical_duration_s": round(float(np.median([e["duration_s"] for e in g])), 6),
            "typical_bandwidth_hz": round(float(np.median([e["bandwidth_hz"] for e in g])), 1),
            "max_snr_db": max(e["snr_db"] for e in g),
            "typical_interval_s": round(float(np.median(np.diff(starts))), 6) if len(starts) > 1 else None,
        })
    summary_groups.sort(key=lambda g: -g["count"])
    return {"center_freq_hz": fc, "sample_rate": fs, "seconds": round(len(x) / fs, 4),
            "noise_floor_dbfs": info.get("noise_floor_dbfs"), "frame_s": info.get("frame_s"),
            "events_total": info.get("total_events", 0), "transmitters": summary_groups,
            "events": events, "features": _feature_summary(feats, len(x) / fs, frames.shape[0]) if feats else {},
            "annotations": {"shown": sorted(on), "available": list(ANNOTATIONS)}}, png


# ---------------------------------------------------------------- modulation fingerprinting

def shift_filter(x: np.ndarray, fs: float, offset_hz: float, bw_hz: float | None) -> tuple[np.ndarray, float]:
    n = len(x)
    t = np.arange(n) / fs
    y = x * np.exp(-2j * np.pi * offset_hz * t).astype(np.complex64)
    fs2 = fs
    if bw_hz and bw_hz < fs * 0.9:
        Y = np.fft.fft(y)
        f = np.abs(np.fft.fftfreq(n, 1 / fs))
        w = np.clip((bw_hz / 2 * 1.15 - f) / (bw_hz / 2 * 0.3), 0, 1)  # raised-cosine edges
        y = np.fft.ifft(Y * (0.5 - 0.5 * np.cos(np.pi * w))).astype(np.complex64)
        dec = max(1, int(fs // (4 * bw_hz)))
        if dec > 1:
            y = y[::dec]
            fs2 = fs / dec
    return y, fs2


def _runs(s: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Run lengths and values of a boolean sequence."""
    if len(s) == 0:
        return np.array([], int), np.array([], bool)
    ch = np.flatnonzero(np.diff(s.astype(np.int8)) != 0) + 1
    bounds = np.concatenate([[0], ch, [len(s)]])
    return np.diff(bounds), s[bounds[:-1]]


def _deglitch(sig: np.ndarray) -> np.ndarray:
    """Remove runs much shorter than the typical run (noise glitches at transitions)."""
    s = sig.astype(bool).copy()
    for _ in range(3):
        runs, vals = _runs(s)
        if len(runs) < 5:
            return s
        g = max(1.0, 0.3 * float(np.median(runs)))
        short = np.flatnonzero(runs <= g)
        if short.size == 0:
            return s
        starts = np.concatenate([[0], np.cumsum(runs)[:-1]])
        for i in short:
            if 0 < i < len(runs) - 1:
                s[starts[i]:starts[i] + runs[i]] = not vals[i]
    return s


def _fit_error(runs: np.ndarray, T: float) -> float:
    k = np.maximum(np.round(runs / T), 1)
    return float(np.mean(((runs / T) - k) ** 2))


def _spectral_candidates(v: np.ndarray, fs: float, k: int = 6) -> list[float]:
    """Symbol-rate candidates from spectral lines of the transition energy |dv/dt|."""
    if len(v) < 32:
        return []
    e = np.abs(np.diff(v.astype(float)))
    e = e - e.mean()
    n = 1 << int(np.ceil(np.log2(len(e) * 4)))
    E = np.abs(np.fft.rfft(e * np.hanning(len(e)), n))
    f = np.fft.rfftfreq(n, 1 / fs)
    m = np.flatnonzero((f > fs * 0.005) & (f < fs * 0.45))
    if m.size == 0:
        return []
    order = m[np.argsort(E[m])[::-1]]
    out: list[float] = []
    for i in order:
        if all(abs(f[i] - c) > fs * 0.01 for c in out):
            out.append(float(f[i]))
        if len(out) >= k:
            break
    return out


def _estimate_symbol_period(runs: np.ndarray, v: np.ndarray | None = None, fs: float = 1.0) -> float | None:
    runs = runs[1:-1] if len(runs) > 4 else runs
    runs = runs[runs > 1]
    if len(runs) < 4:
        return None
    cands = []
    tmin = float(np.percentile(runs, 15))
    for T in np.linspace(0.6 * tmin, 1.4 * tmin, 120):
        cands.append(T)
    if v is not None:
        cands += [fs / r for r in _spectral_candidates(v, fs)]
    cands = [T for T in cands if T >= 1.5 and T <= np.max(runs) * 1.05]
    if not cands:
        return None
    scored = sorted(((T, _fit_error(runs, T)) for T in cands), key=lambda t: -t[0])
    good = [T for T, e in scored if e < 0.02]
    T = good[0] if good else min(scored, key=lambda t: t[1])[0]
    k = np.maximum(np.round(runs / T), 1)
    return float(np.sum(runs) / np.sum(k))  # least-squares refine


def _slice_bits(s: np.ndarray, T: float) -> np.ndarray:
    """Sample a binary waveform at symbol rate, re-syncing on every transition."""
    bits = []
    n = len(s)
    ch = np.flatnonzero(np.diff(s.astype(np.int8)) != 0) + 1
    if len(ch) == 0:
        return np.array([], np.uint8)
    t = ch[0] + T / 2
    ci = 0
    while t < n:
        bits.append(1 if s[int(t)] else 0)
        nxt = t + T
        while ci < len(ch) and ch[ci] <= t:
            ci += 1
        if ci < len(ch) and ch[ci] < nxt - T / 2 + T * 0.5 and ch[ci] > t:
            nxt = ch[ci] + T / 2
        t = nxt
    return np.array(bits, dtype=np.uint8)


def bits_to_hex(bits: np.ndarray, lsb_first: bool) -> str:
    m = len(bits) // 8 * 8
    if m == 0:
        return ""
    return np.packbits(bits[:m], bitorder="little" if lsb_first else "big").tobytes().hex()


def analyze_signal(x: np.ndarray, fs: float, fc: float, offset_hz: float, bw_hz: float | None,
                   title: str = "") -> tuple[dict, bytes]:
    y, fs2 = shift_filter(x, fs, offset_hz, bw_hz)
    env = np.abs(y)
    lo, hi = float(np.percentile(env, 10)), float(np.percentile(env, 99))
    thr = lo + 0.35 * (hi - lo)
    on = env > thr
    # smooth on-mask a little
    on_runs, on_vals = _runs(on)
    res: dict = {"analyzed_offset_hz": offset_hz, "analysis_bandwidth_hz": bw_hz, "analysis_rate": fs2,
                 "samples": int(len(y)), "on_fraction": round(float(on.mean()), 4),
                 "snr_db_est": round(20 * math.log10((hi + 1e-12) / (lo + 1e-12)), 1)}
    if on.sum() < 16:
        res["classification"] = "no signal above noise in this window"
        return res, plot.series_plot([{"x": np.arange(len(env)) / fs2 * 1e3, "ys": [("ENVELOPE", env)],
                                       "xlabel": "MS", "ylabel": "AMPL"}], title or "NO SIGNAL")
    # instantaneous frequency
    d = y[1:] * np.conj(y[:-1])
    inst = np.angle(d) * fs2 / (2 * np.pi)
    inst = np.concatenate([inst, inst[-1:]])
    L = max(1, int(round(fs2 / max(bw_hz or fs2, 1) * 0.5)))
    if L > 1:
        inst_s = np.convolve(inst, np.ones(L) / L, mode="same")
    else:
        inst_s = inst
    f_on = inst_s[on]
    f_mean = float(np.mean(f_on))
    # slicing threshold = midpoint of the extreme frequency excursions: unaffected by unequal 1s/0s
    f_mid = 0.5 * (float(np.percentile(f_on, 97)) + float(np.percentile(f_on, 3)))
    res["carrier_offset_from_analysis_center_hz"] = round(f_mean, 1)
    res["absolute_carrier_hz"] = round(fc + offset_hz + f_mean, 1)
    # amplitude behaviour
    env_on = env[on]
    amp_cv = float(np.std(env_on) / (np.mean(env_on) + 1e-12))
    pulses = [(int(r), bool(v)) for r, v in zip(on_runs, on_vals)]
    on_pulses = [r for r, v in pulses if v and r > 2]
    off_gaps = [r for r, v in pulses[1:-1] if not v and r > 2]
    res["amplitude_variation"] = round(amp_cv, 3)
    res["pulse_count"] = len(on_pulses)
    # frequency histogram (levels)
    span = max(np.percentile(np.abs(f_on - f_mean), 99), 1.0)
    hist, edges = np.histogram(f_on - f_mean, bins=61, range=(-1.25 * span, 1.25 * span))
    gk = np.exp(-0.5 * (np.arange(-6, 7) / 2.0) ** 2)
    hs = np.convolve(hist, gk / gk.sum(), mode="same")
    cand = [i for i in range(1, len(hs) - 1) if hs[i] >= hs[i - 1] and hs[i] > hs[i + 1] and hs[i] > 0.25 * hs.max()]
    levels: list[int] = []
    for i in sorted(cand, key=lambda i: -hs[i]):
        if all(abs(i - j) >= 6 for j in levels):
            # require a real dip between this and existing levels (prominence)
            ok = True
            for j in levels:
                lo_i, hi_i = sorted((i, j))
                if hs[lo_i:hi_i + 1].min() > 0.8 * min(hs[i], hs[j]):
                    ok = False
            if ok:
                levels.append(i)
    levels.sort()
    fo = f_on - f_mean
    m2 = np.mean(fo ** 2) + 1e-12
    kurt = float(np.mean(fo ** 4) / m2 ** 2)
    skew = float(np.mean(fo ** 3) / m2 ** 1.5)
    res["bimodality_coefficient"] = round((skew ** 2 + 1) / kurt, 3)
    centers = 0.5 * (edges[:-1] + edges[1:])
    level_hz = [round(float(centers[i]), 1) for i in levels]
    res["frequency_levels_hz"] = level_hz
    res["frequency_std_hz"] = round(float(np.std(f_on)), 1)

    # classification + symbol timing
    ook_like = len(on_pulses) >= 3 and len(off_gaps) >= 2 and res["on_fraction"] < 0.85
    if ook_like:
        cls = "OOK / ASK (on-off keyed pulses)"
        sig = on
    elif len(levels) == 4 and amp_cv < 0.3 and res["bimodality_coefficient"] < 0.66 and (
            abs(level_hz[2] - level_hz[1]) / max(abs(level_hz[3] - level_hz[0]), 1e-9) < 0.5):
        # equally spaced levels (inner pair ~1/3 of outer span) = true 4-FSK; GFSK ISI gives inner ~0.7
        cls = "4-FSK"
        sig = inst_s > f_mid
        res["deviation_hz_outer"] = round((level_hz[-1] - level_hz[0]) / 2, 1)
    elif (len(levels) == 2 or res["bimodality_coefficient"] > 0.6) and amp_cv < 0.3:
        cls = "2-FSK / GFSK"
        sig = inst_s > f_mid
        res["center_frequency_estimate_hz"] = round(fc + offset_hz + f_mid, 1)
        hi_f, lo_f = f_on[f_on > f_mid] - f_mid, f_on[f_on <= f_mid] - f_mid
        res["deviation_hz"] = round(float((np.median(hi_f) - np.median(lo_f)) / 2), 1) if len(hi_f) and len(lo_f) else None
    elif amp_cv < 0.15 and res["frequency_std_hz"] < 0.02 * (bw_hz or fs2):
        cls = "unmodulated carrier, or PSK (check phase plot)"
        sig = None
    elif amp_cv < 0.2:
        cls = "constant-envelope, continuous frequency modulation (analog FM?)"
        sig = None
        res["fm_deviation_est_hz"] = round(float(np.percentile(np.abs(f_on - f_mean), 99)), 1)
    else:
        cls = "amplitude-varying (AM, OFDM, noise-like or multi-signal)"
        sig = None
    res["classification"] = cls
    if sig is not None:
        sig_on = _deglitch(sig)
        if not ook_like:
            # restrict to the on region (first..last on sample)
            idx = np.flatnonzero(on)
            sig_on = sig_on[idx[0]:idx[-1] + 1]
        runs, vals = _runs(sig_on)
        T = _estimate_symbol_period(runs, None if ook_like else inst_s[np.flatnonzero(on)[0]:np.flatnonzero(on)[-1] + 1], fs2)
        if ook_like:
            res["pulse_widths_us"] = _top_values(np.array(on_pulses) / fs2 * 1e6)
            res["gap_widths_us"] = _top_values(np.array(off_gaps) / fs2 * 1e6)
        if T:
            res["symbol_rate_est"] = round(fs2 / T, 1)
            res["symbol_period_us"] = round(T / fs2 * 1e6, 3)
            bits = _slice_bits(sig_on, T)
            res["bits_count"] = int(len(bits))
            res["bits_preview"] = "".join(map(str, bits[:400].tolist()))
            res["hex_msb_first"] = bits_to_hex(bits[:512], False)
            res["hex_lsb_first"] = bits_to_hex(bits[:512], True)
            if ook_like and res.get("pulse_widths_us") and len(res["pulse_widths_us"]) >= 2:
                res["hint"] = ("Two distinct pulse widths -> likely PWM: short pulse = one bit value, long pulse = the "
                               "other. NRZ bits above are raw on/off chips at the chip rate.")
            if not ook_like:
                s = res["bits_preview"]
                for pat, name in (("01010101", "alternating preamble 0x55/0xAA"),):
                    if pat in s[:64]:
                        res["hint"] = f"bit stream starts with an {name} - typical packet radio preamble"
    # plot
    tt = np.arange(len(y)) / fs2 * 1e3
    panels = [
        {"x": tt, "ys": [("ENVELOPE", env)], "xlabel": "MS", "ylabel": "AMPLITUDE"},
        {"x": tt, "ys": [("INST FREQ KHZ", np.where(on, inst_s, np.nan) / 1e3)], "xlabel": "MS", "ylabel": "KHZ",
         "ylim": (float(np.nanmin(np.where(on, inst_s, np.nan)) / 1e3) - 1, float(np.nanmax(np.where(on, inst_s, np.nan)) / 1e3) + 1)},
        {"x": centers / 1e3, "ys": [("FREQ HISTOGRAM (ON SAMPLES)", hist)], "xlabel": "KHZ FROM MEAN", "ylabel": "COUNT", "kind": "bars"},
    ]
    png = plot.series_plot(panels, title or f"SIGNAL @ {(fc + offset_hz) / 1e6:.4f} MHZ: {cls.upper()[:40]}")
    return res, png


def _top_values(v: np.ndarray, k: int = 4) -> list[float]:
    if len(v) == 0:
        return []
    v = np.sort(v)
    clusters: list[list[float]] = [[float(v[0])]]
    for x in v[1:]:
        if x <= clusters[-1][-1] * 1.25 + 1:
            clusters[-1].append(float(x))
        else:
            clusters.append([float(x)])
    clusters.sort(key=lambda c: -len(c))
    return [round(float(np.median(c)), 2) for c in clusters[:k]]


# ---------------------------------------------------------------- file inspection

def read_iq(path: str, dtype: str = "complex64", start: int = 0, count: int = -1) -> np.ndarray:
    dt = {"complex64": np.complex64, "fc32": np.complex64, "cf32": np.complex64,
          "complex128": np.complex128}.get(dtype)
    if dt is not None:
        item = np.dtype(dt).itemsize
        with open(path, "rb") as f:
            f.seek(start * item)
            return np.fromfile(f, dtype=dt, count=count).astype(np.complex64)
    if dtype in ("cu8", "uint8iq"):
        with open(path, "rb") as f:
            f.seek(start * 2)
            a = np.fromfile(f, dtype=np.uint8, count=count * 2 if count > 0 else -1).astype(np.float32)
        a = (a - 127.5) / 127.5
        return (a[0::2] + 1j * a[1::2]).astype(np.complex64)
    if dtype in ("cs16", "int16iq"):
        with open(path, "rb") as f:
            f.seek(start * 4)
            a = np.fromfile(f, dtype="<i2", count=count * 2 if count > 0 else -1).astype(np.float32) / 32768
        return (a[0::2] + 1j * a[1::2]).astype(np.complex64)
    raise ValueError(f"unsupported IQ dtype {dtype}")


def inspect_file(path: str, dtype: str | None = None, sample_rate: float | None = None,
                 max_seconds: float = 5.0) -> tuple[dict, bytes | None]:
    size = os.path.getsize(path)
    ext = os.path.splitext(path)[1].lower()
    info: dict = {"path": path, "bytes": size}
    if ext == ".wav":
        with wave.open(path, "rb") as w:
            ch, sw, rate, n = w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes()
            raw = w.readframes(min(n, int(rate * max_seconds)))
        dtp = {1: np.uint8, 2: "<i2", 4: "<i4"}[sw]
        a = np.frombuffer(raw, dtype=dtp).astype(np.float32)
        if sw == 1:
            a = (a - 128) / 128
        else:
            a /= float(2 ** (8 * sw - 1))
        a = a.reshape(-1, ch)[:, 0] if ch > 1 else a
        info.update({"kind": "wav audio", "channels": ch, "sample_rate": rate, "duration_s": round(n / rate, 3)})
        return _describe_real(a, rate, info, "AUDIO " + os.path.basename(path))
    if ext in (".txt", ".log", ".json", ".csv", ".md", ".yml", ".yaml", ".grc", ".py") or dtype == "text":
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            txt = f.read(20000)
        info.update({"kind": "text", "lines": txt.count("\n"), "head": txt[:6000]})
        return info, None
    dtype = dtype or ("complex64" if ext in (".cfile", ".iq", ".cf32", ".fc32") else
                      "float32" if ext in (".f32", ".float", ".raw") else "uint8")
    info["dtype"] = dtype
    if dtype in ("complex64", "cu8", "cs16"):
        fs = sample_rate or 1.0
        x = read_iq(path, dtype, 0, int(fs * max_seconds) if sample_rate else 2_000_000)
        info.update({"kind": "complex IQ", "samples": int(len(x)), "rms_dbfs": round(10 * math.log10(np.mean(np.abs(x) ** 2) + 1e-20), 1)})
        if sample_rate:
            s, png = capture_report(x, sample_rate, 0.0, title="IQ FILE " + os.path.basename(path))
            info["events"] = s["events"][:20]
            info["transmitters"] = s["transmitters"]
            return info, png
        return info, plot.series_plot([{"x": np.arange(len(x)), "ys": [("I", x.real), ("Q", x.imag)], "xlabel": "SAMPLE"}],
                                      "IQ " + os.path.basename(path))
    if dtype == "float32":
        a = np.fromfile(path, dtype=np.float32, count=int((sample_rate or 48000) * max_seconds))
        info.update({"kind": "float32 stream", "samples": int(len(a))})
        if len(a) == 0:
            return info, None
        return _describe_real(a, sample_rate or 1.0, info, "FLOAT " + os.path.basename(path))
    data = np.fromfile(path, dtype=np.uint8, count=65536)
    info["kind"] = "bytes"
    uniq = np.unique(data)
    if len(uniq) <= 2 and set(uniq.tolist()) <= {0, 1}:
        info["kind"] = "unpacked bits (one bit per byte)"
        info["bits_preview"] = "".join(map(str, data[:600].tolist()))
        info["ones_fraction"] = round(float(data.mean()), 3)
        return info, None
    info["hex_head"] = data[:512].tobytes().hex()
    printable = bytes(c if 32 <= c < 127 else 46 for c in data[:512].tolist())
    info["ascii_head"] = printable.decode()
    return info, None


def _describe_real(a: np.ndarray, rate: float, info: dict, title: str) -> tuple[dict, bytes]:
    rms = float(np.sqrt(np.mean(a ** 2)) + 1e-12)
    info["rms_dbfs"] = round(20 * math.log10(rms), 1)
    info["peak"] = round(float(np.max(np.abs(a))), 4)
    n = min(len(a), 1 << 17)
    seg = a[:n] * np.hanning(n)
    sp = np.abs(np.fft.rfft(seg))
    f = np.fft.rfftfreq(n, 1 / rate)
    spdb = 20 * np.log10(sp / (sp.max() + 1e-12) + 1e-9)
    # top tones
    order = np.argsort(sp)[::-1]
    tones = []
    for i in order:
        if f[i] < 20:
            continue
        if all(abs(f[i] - t) > rate / n * 6 for t in tones):
            tones.append(float(f[i]))
        if len(tones) >= 6:
            break
    info["dominant_frequencies_hz"] = [round(t, 1) for t in tones]
    # fraction of near-silent 20 ms blocks
    blk = max(1, int(rate * 0.02))
    m = len(a) // blk
    if m:
        lev = np.sqrt(np.mean(a[: m * blk].reshape(m, blk) ** 2, axis=1))
        info["silent_fraction"] = round(float(np.mean(lev < 0.01)), 3)
    t = np.arange(len(a)) / rate
    png = plot.series_plot([
        {"x": t, "ys": [("WAVEFORM", a)], "xlabel": "S", "ylabel": "AMPL"},
        {"x": f / 1e3, "ys": [("SPECTRUM", spdb)], "xlabel": "KHZ", "ylabel": "DB", "ylim": (-90, 3)},
    ], title)
    return info, png


# ---------------------------------------------------------------- fine time resolution / wideband helpers

def nfft_for(fs: float, time_resolution_s: float = 0.0, freq_resolution_hz: float = 0.0, default: int = 256) -> int:
    """FFT size for a requested time step (short frames = fine timing) or bin width (long frames = fine frequency)."""
    if time_resolution_s:
        n = fs * time_resolution_s
    elif freq_resolution_hz:
        n = fs / freq_resolution_hz
    else:
        n = default
    return int(2 ** round(math.log2(max(8, min(1 << 17, n)))))


def refine_edges(x: np.ndarray, fs: float, events: list[dict], frame: int = 256) -> None:
    """Sample-accurate start/end for each burst (find_events only knows them to one FFT frame): filter to the
    burst's own band, take the envelope and find where it crosses half-way between noise and the burst level."""
    n = len(x)
    for e in events:
        a = max(0, int(e["start_s"] * fs) - 2 * frame)
        b = min(n, int((e["start_s"] + e["duration_s"]) * fs) + 2 * frame)
        if b - a < 32:
            continue
        seg = x[a:b]
        bw = max(e.get("bandwidth_hz") or fs / 50, fs / 2000)
        try:
            y, fs2 = shift_filter(seg, fs, e.get("offset_hz", 0.0), min(bw * 1.3, fs))
        except Exception:
            continue
        env = np.abs(y)
        k = max(1, int(round(fs2 * 2e-6)))  # ~2 us smoothing
        if k > 1:
            env = np.convolve(env, np.ones(k) / k, mode="same")
        lo, hi = float(np.percentile(env, 5)), float(np.percentile(env, 99))
        if hi <= lo * 1.5:
            continue
        on = np.flatnonzero(env > lo + 0.5 * (hi - lo))
        if not len(on):
            continue
        s0, s1 = a + on[0] * (fs / fs2), a + (on[-1] + 1) * (fs / fs2)
        e["start_s"] = round(s0 / fs, 9)
        e["duration_s"] = round((s1 - s0) / fs, 9)
        e["start_us"] = round(s0 / fs * 1e6, 3)
        e["duration_us"] = round((s1 - s0) / fs * 1e6, 3)


def spectrogram(x: np.ndarray, fs: float, fc: float, f_lo: float | None = None, f_hi: float | None = None,
                nfft: int = 256, overlap: float = 0.75, t0_s: float = 0.0, title: str = "",
                max_rows: int = 700, img: dict | None = None, ann=None) -> tuple[dict, bytes]:
    """Overlapping short-time FFT for a close look in TIME: hop = nfft*(1-overlap) so steps of a few microseconds
    are possible at high sample rates. Optional frequency window [f_lo, f_hi] (absolute Hz)."""
    hop = max(1, int(nfft * (1 - overlap)))
    nfr = max(0, (len(x) - nfft) // hop + 1)
    if nfr == 0:
        raise ValueError(f"window too short for a {nfft}-point FFT ({len(x)} samples)")
    if nfr > max_rows * 8:  # bound CPU: pick evenly spaced frames
        idx = np.linspace(0, nfr - 1, max_rows * 8).astype(int)
    else:
        idx = np.arange(nfr)
    w = np.hanning(nfft).astype(np.float32)
    fr = np.stack([x[i * hop:i * hop + nfft] for i in idx]) * w
    X = np.fft.fftshift(np.fft.fft(fr, axis=1), axes=1)
    db = 10 * np.log10(np.abs(X) ** 2 / (w.sum() ** 2) + 1e-20)
    f = np.fft.fftshift(np.fft.fftfreq(nfft, 1 / fs)) + fc
    lo = f_lo if f_lo is not None else f[0]
    hi = f_hi if f_hi is not None else f[-1]
    sel = (f >= lo) & (f <= hi)
    if sel.sum() < 2:
        raise ValueError("frequency window has fewer than 2 bins - widen it or raise freq resolution")
    db = db[:, sel]
    rows = min(max_rows, db.shape[0])
    edges = np.linspace(0, db.shape[0], rows + 1).astype(int)
    wf = np.maximum.reduceat(db, edges[:-1], axis=0)
    hold = db.max(axis=0)
    floor = float(np.median(db))
    span = len(x) / fs
    on = parse_annotations(ann) if ann is not None else {"wideband", "transients"}
    try:
        feats = detect_features(db, fs * sel.sum() / len(sel), float((f[sel][0] + f[sel][-1]) / 2 + fs / len(sel) / 2))
    except Exception:  # pragma: no cover
        feats = {}
    png = plot.waterfall_plot(wf, float(f[sel][0]), float(f[sel][-1]), span, hold, [],
                              title or f"SPECTROGRAM {fc / 1e6:.4f} MHZ  {span * 1e3:.3f} MS  STEP "
                                        f"{hop / fs * 1e6:.2f} US", min_db=(img or {}).get("db_min", floor - 3),
                              max_db=(img or {}).get("db_max"), width=int((img or {}).get("width") or 1300),
                              wf_h=int((img or {}).get("height") or 520),
                              sp_h=int((img or {}).get("spectrum_height") or 170), features=feats, ann=on,
                              n_frames=db.shape[0])
    # per-step activity: when the power in the window is above the noise
    act = db.max(axis=1) > floor + 10
    runs = []
    if act.any():
        on = np.flatnonzero(np.diff(np.concatenate([[0], act.astype(int), [0]])))
        for s0, s1 in zip(on[0::2], on[1::2]):
            t_a = t0_s + idx[s0] * hop / fs
            t_b = t0_s + (idx[min(s1, len(idx) - 1)] * hop + nfft) / fs
            runs.append({"start_us": round(t_a * 1e6, 3), "duration_us": round((t_b - t_a) * 1e6, 3)})
    info = {"center_freq_hz": fc, "sample_rate": fs, "window_s": round(span, 9), "start_s": t0_s, "fft_size": nfft,
            "time_step_us": round(hop / fs * 1e6, 4), "frame_us": round(nfft / fs * 1e6, 4),
            "bin_hz": round(fs / nfft, 2), "freq_window_hz": [round(float(f[sel][0]), 1), round(float(f[sel][-1]), 1)],
            "noise_floor_dbfs": round(floor, 1), "active_periods": runs[:60], "active_periods_total": len(runs)}
    if feats:
        info["features"] = _feature_summary(feats, span, db.shape[0])
    return info, png


def lowpass_taps(cutoff_norm: float, ntaps: int) -> np.ndarray:
    """Windowed-sinc low-pass; cutoff as a fraction of the sample rate (0..0.5)."""
    n = np.arange(ntaps) - (ntaps - 1) / 2
    h = 2 * cutoff_norm * np.sinc(2 * cutoff_norm * n) * np.blackman(ntaps)
    return (h / h.sum()).astype(np.float32)


def channelize_file(src: str, dst: str, fs: float, offset_hz: float, bw_hz: float, start: int = 0,
                    count: int = -1, chunk: int = 1 << 21) -> tuple[float, int]:
    """Cut one channel out of a wideband capture: shift offset_hz to 0, low-pass to bw_hz, decimate - streamed in
    chunks (any file size) with a continuous filter state and oscillator phase. Returns (new rate, samples)."""
    dec = max(1, int(fs // (bw_hz * 1.25)))
    fs2 = fs / dec
    ntaps = int(min(4001, max(31, 10 * dec + 1))) | 1
    h = lowpass_taps(min(0.45, 0.5 * bw_hz / fs * 1.1), ntaps)
    hist = np.zeros(ntaps - 1, dtype=np.complex64)
    phase0 = 0
    keep_phase = 0
    written = 0
    total = os.path.getsize(src) // 8 - start
    if count >= 0:
        total = min(total, count)
    with open(src, "rb") as fi, open(dst, "wb") as fo:
        fi.seek(start * 8)
        left = total
        while left > 0:
            n = min(chunk, left)
            x = np.fromfile(fi, dtype=np.complex64, count=n)
            if not len(x):
                break
            left -= len(x)
            t = (phase0 + np.arange(len(x))) / fs
            y = x * np.exp(-2j * np.pi * offset_hz * t).astype(np.complex64)
            phase0 += len(x)
            buf = np.concatenate([hist, y])
            f = np.convolve(buf, h, mode="valid").astype(np.complex64)  # len == len(y)
            hist = buf[-(ntaps - 1):] if ntaps > 1 else hist
            out = f[keep_phase::dec]
            keep_phase = (keep_phase - len(f)) % dec
            out.tofile(fo)
            written += len(out)
    return fs2, written


# ---------------------------------------------------------------- digital data: bits and packets

def _bitstr(v) -> str:
    """Accept '0101...', hex ('a5f0' / '0xA5F0'), or a list of 0/1 -> '0101...'."""
    if isinstance(v, (list, tuple)):
        return "".join("1" if int(b) else "0" for b in v)
    s = str(v).strip().replace(" ", "").replace("_", "")
    if s and set(s) <= {"0", "1"} and len(s) > 8:
        return s
    if s.lower().startswith("0x"):
        s = s[2:]
    try:
        return bin(int(s, 16))[2:].zfill(len(s) * 4) if s else ""
    except ValueError:
        raise ValueError(f"not bits or hex: {str(v)[:40]!r}")


def _bytes(bits: str, lsb_first: bool = False) -> bytes:
    out = bytearray()
    for i in range(0, len(bits) - 7, 8):
        b = bits[i:i + 8]
        out.append(int(b[::-1] if lsb_first else b, 2))
    return bytes(out)


CRCS = [  # name, width, poly, init, refin, refout, xorout
    ("CRC-8", 8, 0x07, 0x00, False, False, 0x00), ("CRC-8/MAXIM (Dallas 1-Wire)", 8, 0x31, 0x00, True, True, 0x00),
    ("CRC-8/SAE-J1850", 8, 0x1D, 0xFF, False, False, 0xFF), ("CRC-8/WCDMA", 8, 0x9B, 0x00, True, True, 0x00),
    ("CRC-16/CCITT-FALSE", 16, 0x1021, 0xFFFF, False, False, 0x0000), ("CRC-16/XMODEM", 16, 0x1021, 0x0000, False, False, 0),
    ("CRC-16/KERMIT", 16, 0x1021, 0x0000, True, True, 0x0000), ("CRC-16/X-25 (HDLC/AX.25)", 16, 0x1021, 0xFFFF, True, True, 0xFFFF),
    ("CRC-16/ARC (IBM)", 16, 0x8005, 0x0000, True, True, 0x0000), ("CRC-16/MODBUS", 16, 0x8005, 0xFFFF, True, True, 0x0000),
    ("CRC-16/DNP (wM-Bus)", 16, 0x3D65, 0x0000, True, True, 0xFFFF), ("CRC-16/EN-13757 (wM-Bus)", 16, 0x3D65, 0x0000, False, False, 0xFFFF),
    ("CRC-32", 32, 0x04C11DB7, 0xFFFFFFFF, True, True, 0xFFFFFFFF),
]


def _crc(data: bytes, width, poly, init, refin, refout, xorout) -> int:
    top = 1 << (width - 1)
    mask = (1 << width) - 1
    c = init
    for b in data:
        if refin:
            b = int(f"{b:08b}"[::-1], 2)
        c ^= b << (width - 8)
        for _ in range(8):
            c = ((c << 1) ^ poly) & mask if c & top else (c << 1) & mask
    if refout:
        c = int(f"{c:0{width}b}"[::-1], 2)
    return c ^ xorout


def find_crc(payload: bytes) -> list[dict]:
    """Does the packet end in a known CRC of the bytes before it (after any leading bytes)? Tries every start."""
    hits = []
    for name, w, poly, init, ri, ro, xo in CRCS:
        nb = w // 8
        if len(payload) < nb + 2:
            continue
        for order in ("big", "little"):
            want = int.from_bytes(payload[-nb:], order)
            for start in range(0, min(8, len(payload) - nb - 1)):
                if _crc(payload[start:-nb], w, poly, init, ri, ro, xo) == want:
                    hits.append({"crc": name, "covers_bytes": f"{start}..{len(payload) - nb - 1}",
                                 "byte_order": order})
                    break
    return hits[:6]


def bits_analyze(packets: list, sync: str = "") -> dict:
    """Structure of one or more demodulated packets: preamble, sync word, line codes (Manchester / NRZI),
    byte views, CRC search, and - across several packets - which bits stay constant (headers, IDs) and which
    change (counters, readings, checksums)."""
    pk = [_bitstr(p) for p in packets if str(p).strip()]
    if not pk:
        raise ValueError("give at least one packet (bits like '0101...' or hex)")
    out: dict = {"packets": len(pk), "lengths_bits": [len(p) for p in pk[:20]]}
    first = pk[0]
    # preamble: leading alternating run
    m = 0
    while m + 1 < len(first) and first[m] != first[m + 1]:
        m += 1
    if m >= 8:
        out["preamble"] = {"bits": m + 1, "pattern": first[:min(m + 1, 32)]}
    # sync word: given, or the common bits right after the preamble
    if not sync and len(pk) > 1:
        st = (m + 1) if m >= 8 else 0
        common = ""
        for i in range(st, min(len(p) for p in pk)):
            col = {p[i] for p in pk}
            if len(col) > 1:
                break
            common += first[i]
        if len(common) >= 8:
            out["common_after_preamble"] = common[:64]
    def manchester(s: str, ieee: bool):
        out_b, bad = [], 0
        for i in range(0, len(s) - 1, 2):
            a = s[i:i + 2]
            if a in ("01", "10"):
                out_b.append(("1" if a == "01" else "0") if ieee else ("0" if a == "01" else "1"))
            else:
                bad += 1
        return "".join(out_b), bad
    views = {}
    for name, s in (("raw", first), ("inverted", "".join("1" if c == "0" else "0" for c in first))):
        views[name] = {"hex_msb_first": _bytes(s).hex(), "hex_lsb_first": _bytes(s, True).hex()}
    for off in (0, 1):
        mb, bad = manchester(first[off:], True)
        if bad <= max(1, len(first) // 40):
            views[f"manchester_ieee_offset{off}"] = {"bits": mb[:256], "hex": _bytes(mb).hex(), "invalid_pairs": bad}
            mb2, _ = manchester(first[off:], False)
            views[f"manchester_thomas_offset{off}"] = {"hex": _bytes(mb2).hex()}
    nrzi = "".join("0" if first[i] != first[i - 1] else "1" for i in range(1, len(first)))
    views["nrzi_decoded"] = {"hex": _bytes(nrzi).hex()}
    out["views"] = views
    # CRC search on byte-aligned payloads after a sync/preamble
    starts = {0}
    if sync:
        k = first.find(_bitstr(sync) if not set(sync) <= {"0", "1"} else sync)
        if k >= 0:
            starts.add(k + len(sync))
    if out.get("preamble"):
        starts.add(out["preamble"]["bits"])
    def hits_for(bits: str) -> set:
        found = set()
        for s0 in sorted(starts):
            for shift in range(8):
                for lsb in (False, True):
                    for h in find_crc(_bytes(bits[s0 + shift:], lsb)):
                        found.add((h["crc"], h["covers_bytes"].split("..")[0], h["byte_order"], s0 + shift, lsb))
        return found
    common = hits_for(first)
    for other in pk[1:6]:
        common &= hits_for(other)
    crc_hits = []
    for name, cstart, order, bstart, lsb in sorted(common, key=lambda h: (-("32" in h[0]) - ("16" in h[0]), h[3])):
        wide = "16" in name or "32" in name
        if len(pk) == 1 and not wide:
            continue  # an 8-bit CRC matches by chance 1 time in 256: only trust it when several packets agree
        crc_hits.append({"crc": name, "data_starts_at_byte": int(cstart), "crc_byte_order": order,
                         "bit_start": bstart, "lsb_first_bytes": lsb,
                         "confidence": "high" if wide or len(pk) >= 3 else "medium"})
    if crc_hits:
        out["crc_matches"] = crc_hits[:6]
    elif len(pk) == 1:
        out["crc_note"] = "no CRC-16/32 found; give 2+ packets to test 8-bit CRCs reliably"
    # field analysis across packets
    if len(pk) > 1:
        L = min(len(p) for p in pk)
        mask = "".join("=" if len({p[i] for p in pk}) == 1 else "x" for i in range(L))
        out["constant_vs_changing"] = {"legend": "= bit is the same in every packet, x = it changes",
                                       "mask": mask[:512]}
        fields, cur, start = [], mask[0], 0
        for i in range(1, L + 1):
            if i == L or mask[i] != cur:
                fields.append({"bits": f"{start}-{i - 1}", "len": i - start,
                               "kind": "constant" if cur == "=" else "changing",
                               "values": sorted({p[start:i] for p in pk})[:6] if cur == "x" and i - start <= 32 else None})
                if i < L:
                    cur, start = mask[i], i
        out["fields"] = [{k: v for k, v in f.items() if v is not None} for f in fields[:40]]
    return out


# ------------------------------------------------------------------ features for annotations
ANNOTATIONS = ("grid", "signals", "dc", "wideband", "transients", "images", "spurs", "edges")


def parse_annotations(spec) -> set:
    """'all' / 'none' / ['wideband', '-grid'] / {'grid': False} / 'signals,wideband' -> the set to draw."""
    if spec is None or spec == "" or spec == "all" or spec is True:
        return set(ANNOTATIONS)
    if spec == "none" or spec is False:
        return set()
    if isinstance(spec, dict):
        on = set(ANNOTATIONS)
        for k, v in spec.items():
            (on.add if v else on.discard)(str(k))
        return on & set(ANNOTATIONS)
    items = [s.strip() for s in (spec.split(",") if isinstance(spec, str) else spec) if str(s).strip()]
    if items and all(str(i).startswith("-") for i in items):
        return set(ANNOTATIONS) - {str(i)[1:] for i in items}
    return {str(i).lstrip("+") for i in items if not str(i).startswith("-")} & set(ANNOTATIONS)


def _movavg(a: np.ndarray, k: int) -> np.ndarray:
    k = max(1, int(k))
    if k == 1:
        return a
    pad = np.pad(a, (k // 2, k - 1 - k // 2), mode="edge")
    return np.convolve(pad, np.ones(k) / k, mode="valid")


def detect_features(frames_db: np.ndarray, fs: float, fc: float, peaks: list[dict] | None = None,
                    spur_freqs: list[float] | None = None) -> dict:
    """What a person would point out on a waterfall besides the narrow carriers:
      wideband    - spread-spectrum / OFDM / noise-like blocks: a wide stretch raised a few dB above the floor
                    (LTE/5G, WiFi, DVB, wideband FM's skirts, digital TV, broadband interference)
      transients  - moments when the WHOLE band jumps (impulse noise, a strong wideband burst, a stream glitch)
      images      - a weak copy of a strong signal mirrored about the centre (IQ imbalance) - not a real signal
      edges       - the receiver's filter roll-off at the band edges (low floor there is the filter, not quiet)
      spurs       - peaks at this SDR's known clock harmonics"""
    n_fr, nfft = frames_db.shape
    bin_hz = fs / nfft
    lin = 10 ** (frames_db / 10)
    avg = 10 * np.log10(np.mean(lin, axis=0) + 1e-30)
    freqs = fc + (np.arange(nfft) - nfft / 2) * bin_hz
    out: dict = {"wideband": [], "transients": [], "images": [], "edges": [], "spurs": []}
    if nfft < 32:
        return out
    k = max(3, nfft // 200)
    sm = _movavg(avg, k)
    c = nfft // 2
    sm_dc = sm.copy()
    sm_dc[max(0, c - k):c + k] = np.median(sm)
    # filter roll-off at the edges: the outer bins well below the in-band floor
    inner = sm_dc[nfft // 10: nfft - nfft // 10]
    floor = float(np.percentile(inner, 20))
    edge_lo, edge_hi = 0, nfft
    for i in range(nfft // 4):
        if sm_dc[i] < floor - 3:
            edge_lo = i + 1
        else:
            break
    for i in range(nfft - 1, nfft - nfft // 4, -1):
        if sm_dc[i] < floor - 3:
            edge_hi = i
        else:
            break
    if edge_lo > nfft // 100:
        out["edges"].append({"side": "low", "below_hz": round(float(freqs[edge_lo]), 1),
                             "note": "receiver filter roll-off - the floor drops here, not a quiet band"})
    if edge_hi < nfft - nfft // 100:
        out["edges"].append({"side": "high", "above_hz": round(float(freqs[min(edge_hi, nfft - 1)]), 1),
                             "note": "receiver filter roll-off - the floor drops here, not a quiet band"})
    # wideband blocks: wide stretches raised above the floor (narrow carriers removed first)
    narrow = sm_dc.copy()
    for p in peaks or []:
        if p.get("bandwidth_hz", 0) < max(30 * bin_hz, 0.01 * fs):
            a = int((p["freq_hz"] - fc) / bin_hz + nfft / 2 - p.get("bandwidth_hz", 0) / bin_hz - 2)
            b = int((p["freq_hz"] - fc) / bin_hz + nfft / 2 + p.get("bandwidth_hz", 0) / bin_hz + 3)
            a, b = max(0, a), min(nfft, b)
            if b > a:
                narrow[a:b] = np.interp(np.arange(a, b), [a - 1 if a > 0 else a, b if b < nfft else b - 1],
                                        [narrow[max(a - 1, 0)], narrow[min(b, nfft - 1)]])
    wide_k = max(k, nfft // 60)
    base = _movavg(narrow, wide_k)
    rise = base - floor
    min_w = max(int(0.012 * nfft), int(100e3 / bin_hz), 6)
    mask = rise > 2.0
    mask[:edge_lo] = False
    mask[edge_hi:] = False
    for a, b in _merge_close(_segments(mask, max_gap=max(2, min_w // 3))):
        if b - a < min_w:
            continue
        r = rise[a:b]
        seg_frames = 10 * np.log10(np.mean(lin[:, a:b], axis=1) + 1e-30)
        on = float(np.mean(seg_frames > floor + max(1.5, float(np.median(r)) / 2)))
        flat = float(np.std(base[a + (b - a) // 8: b - (b - a) // 8] if b - a > 8 else base[a:b]))
        out["wideband"].append({
            "from_hz": round(float(freqs[a]), 1), "to_hz": round(float(freqs[b - 1]), 1),
            "center_hz": round(float((freqs[a] + freqs[b - 1]) / 2), 1),
            "bandwidth_hz": round(float((b - a) * bin_hz), 1), "rise_db": round(float(np.median(r)), 1),
            "peak_rise_db": round(float(r.max()), 1), "occupancy": round(on, 3),
            "shape": "flat-topped (OFDM / spread-spectrum)" if flat < 1.5 else "uneven (several signals or a hump)",
            "kind": "continuous" if on > 0.9 else "intermittent" if on > 0.05 else "rare"})
    out["wideband"].sort(key=lambda w: -w["bandwidth_hz"] * max(w["rise_db"], 0.1))
    out["wideband"] = out["wideband"][:6]
    # whole-band transients: frames whose median level jumps (median ignores narrow carriers)
    if n_fr >= 8:
        a_, b_ = max(edge_lo, nfft // 20), min(edge_hi, nfft - nfft // 20)
        lvl = np.median(frames_db[:, a_:b_], axis=1)
        ref = float(np.median(lvl))
        spread = float(np.median(np.abs(lvl - ref))) + 0.3
        hot = lvl > ref + max(3.0, 6 * spread)
        if 0 < hot.sum() < 0.2 * n_fr:
            for a, b in _segments(hot, max_gap=1):
                out["transients"].append({"frame_from": int(a), "frame_to": int(b),
                                          "rise_db": round(float(lvl[a:b].max() - ref), 1)})
            out["transients"] = sorted(out["transients"], key=lambda t: -t["rise_db"])[:8]
    # IQ images: a strong narrow signal at +f with a much weaker copy at -f
    for i, p in enumerate((peaks or [])[:12]):
        off = p["freq_hz"] - fc
        if abs(off) < 4 * bin_hz or p.get("bandwidth_hz", 0) > 0.05 * fs:
            continue
        for j, q in enumerate((peaks or [])[:12]):
            if j == i:
                continue
            if abs((q["freq_hz"] - fc) + off) <= max(3 * bin_hz, 0.25 * p.get("bandwidth_hz", 0)):
                d = p.get("peak_hold_dbfs", p.get("power_db", 0)) - q.get("peak_hold_dbfs", q.get("power_db", 0))
                if 15 <= d <= 70:
                    out["images"].append({"signal": j + 1, "image_of": i + 1, "freq_hz": q["freq_hz"],
                                          "below_db": round(float(d), 1),
                                          "note": "mirror of a stronger signal about the centre (IQ imbalance) - "
                                                  "probably not a real transmission"})
    for i, p in enumerate((peaks or [])[:12]):
        for f in spur_freqs or []:
            if abs(p["freq_hz"] - f) <= max(3 * bin_hz, 3e3):
                out["spurs"].append({"signal": i + 1, "freq_hz": p["freq_hz"], "note": "this SDR's own clock harmonic"})
    out["noise_floor_db"] = round(floor, 1)
    return out


# ---------------------------------------------------------------------------------- automatic burst demodulation
def _two_width_levels(w) -> tuple[float, float, float] | None:
    """Split pulse/gap widths into short/long at the largest jump between sorted values (None: all alike)."""
    w = np.sort(np.asarray(w, dtype=float))
    if len(w) < 4:
        return None
    ratios = w[1:] / np.maximum(w[:-1], 1e-12)
    i = int(np.argmax(ratios))
    if ratios[i] < 1.5:
        return None
    return float(np.median(w[: i + 1])), float(np.median(w[i + 1:])), float((w[i] + w[i + 1]) / 2)


def manchester(bits: str) -> tuple[str, str] | None:
    """Decode a chip string as Manchester at whichever alignment works best: -> (IEEE 802.3 01=1, G.E.Thomas 10=1)
    or None if neither alignment is (nearly) clean."""
    best = None
    for off in (0, 1):
        s = bits[off:]
        pairs = [s[i:i + 2] for i in range(0, len(s) - 1, 2)]
        if len(pairs) < 8:
            continue
        bad = sum(p in ("00", "11") for p in pairs)
        # stop at the first long run of invalid pairs (end of the coded part)
        if best is None or bad < best[0]:
            best = (bad, pairs)
    if best is None or best[0] > max(1, 0.08 * len(best[1])):
        return None
    ieee = "".join("1" if p == "01" else "0" if p == "10" else "?" for p in best[1])
    thomas = "".join("1" if p == "10" else "0" if p == "01" else "?" for p in best[1])
    return ieee, thomas


def _hex(bits: str) -> str:
    b = bits.replace("?", "0")
    if len(b) < 8:
        return ""
    b = b[: len(b) // 8 * 8]
    return bytes(int(b[i:i + 8], 2) for i in range(0, len(b), 8)).hex()


def _preamble(bits: str) -> dict:
    m = re.match(r"((?:01){4,}|(?:10){4,}|1{8,}|0{8,})", bits)
    if not m:
        return {}
    p = m.group(1)
    kind = "alternating" if p[:2] in ("01", "10") else "constant"
    return {"preamble_bits": len(p), "preamble": kind, "payload_bits": bits[len(p):]}


def demod_one(y: np.ndarray, fs2: float) -> dict:
    """Demodulate one channelised burst (already shifted to ~0 Hz and filtered): OOK or FSK, symbol rate, coding,
    bits."""
    env = np.abs(y)
    lo, hi = float(np.percentile(env, 10)), float(np.percentile(env, 99))
    if hi <= lo * 1.5:
        return {"modulation": "no clear signal"}
    on = env > lo + 0.35 * (hi - lo)
    out: dict = {"snr_db_est": round(20 * math.log10((hi + 1e-12) / (lo + 1e-12)), 1)}
    runs, vals = _runs(_deglitch(on))
    idx = np.flatnonzero(on)
    pulses = [int(r) for r, v in zip(runs, vals) if v and r > 2]
    gaps = [int(r) for r, v in list(zip(runs, vals))[1:-1] if not v and r > 2]
    on_frac = float(on[idx[0]:idx[-1] + 1].mean()) if idx.size else 0.0
    d = y[1:] * np.conj(y[:-1])
    inst = np.concatenate([np.angle(d), [0.0]]) * fs2 / (2 * np.pi)
    us = 1e6 / fs2
    if len(pulses) >= 4 and len(gaps) >= 3 and on_frac < 0.85:
        out["modulation"] = "OOK/ASK"
        sig = _deglitch(on)[idx[0]:idx[-1] + 1]
        pl, gl = _two_width_levels(pulses), _two_width_levels(gaps)
        out["pulses"] = len(pulses)
        out["short_pulse_us"] = round((pl[0] if pl else float(np.median(pulses))) * us, 1)
        if pl:
            out["long_pulse_us"] = round(pl[1] * us, 1)
        if gl:
            out["short_gap_us"], out["long_gap_us"] = round(gl[0] * us, 1), round(gl[1] * us, 1)
        sym_runs, _v = _runs(sig)
        T = _estimate_symbol_period(sym_runs, None, fs2)
        chips = "".join(map(str, _slice_bits(sig, T).tolist())) if T else ""
        similar = bool(pl and gl and abs(gl[0] - pl[0]) < 0.35 * pl[0] and abs(gl[1] - pl[1]) < 0.35 * pl[1]
                       and abs(pl[1] / pl[0] - 2) < 0.45)
        cand: dict = {}
        if pl:
            cand["pwm"] = "".join("1" if p > pl[2] else "0" for p in pulses)
        if gl:
            cand["ppm"] = "".join("1" if g > gl[2] else "0" for g in gaps)
        if chips:
            cand["nrz"] = chips
            mc = manchester(chips)
            if mc:
                cand["manchester"] = mc[0]
                cand["manchester_inverted"] = mc[1]
        coding = ("manchester" if similar and "manchester" in cand else "pwm" if pl and (not gl or
                  len(set(round(p * us, -1) for p in pulses)) > 1 and float(np.std(gaps)) < 0.25 * float(np.mean(gaps)))
                  else "ppm" if gl else "manchester" if "manchester" in cand else "nrz")
        if coding not in cand:
            coding = next(iter(cand), "nrz")
        out["coding_guess"] = coding
        if T:
            out["symbol_rate"] = round(fs2 / T, 1)
        bits = cand.get(coding, "")
    else:
        f_on = inst[on]
        if f_on.size < 16:
            return {"modulation": "too short to demodulate", **out}
        f_mid = 0.5 * (float(np.percentile(f_on, 97)) + float(np.percentile(f_on, 3)))
        hi_f, lo_f = f_on[f_on > f_mid], f_on[f_on <= f_mid]
        dev = float((np.median(hi_f) - np.median(lo_f)) / 2) if hi_f.size and lo_f.size else 0.0
        amp_cv = float(np.std(env[on]) / (np.mean(env[on]) + 1e-12))
        if dev < 0.002 * fs2 or amp_cv > 0.35:
            return {"modulation": "not OOK or 2-FSK (carrier, PSK, analog or wideband) - use analyze_signal", **out}
        out["modulation"] = "2-FSK/GFSK"
        out["deviation_hz"] = round(dev, 1)
        out["carrier_offset_hz"] = round(f_mid, 1)
        L = max(1, int(round(fs2 / max(4 * dev, 1) * 0.5)))
        inst_s = np.convolve(inst, np.ones(L) / L, mode="same") if L > 1 else inst
        sig = _deglitch(inst_s > f_mid)[idx[0]:idx[-1] + 1]
        sym_runs, _v = _runs(sig)
        T = _estimate_symbol_period(sym_runs, inst_s[idx[0]:idx[-1] + 1], fs2)
        if not T:
            return {**out, "note": "couldn't find a symbol rate"}
        out["symbol_rate"] = round(fs2 / T, 1)
        chips = "".join(map(str, _slice_bits(sig, T).tolist()))
        cand = {"nrz": chips}
        mc = manchester(chips)
        if mc:
            cand["manchester"], cand["manchester_inverted"] = mc
        out["coding_guess"] = "manchester" if mc else "nrz"
        bits = cand[out["coding_guess"]]
    out["bits"] = bits[:1024]
    out["nbits"] = len(bits)
    out["hex"] = _hex(bits)
    out.update({k: v for k, v in _preamble(bits).items() if k != "payload_bits"})
    pre = _preamble(bits)
    if pre.get("payload_bits"):
        out["payload_hex"] = _hex(pre["payload_bits"])
    out["alternatives"] = {k: {"bits": v[:512], "hex": _hex(v)} for k, v in cand.items() if v != bits}
    return out


def demodulate_bursts(x: np.ndarray, fs: float, fc: float, offset_hz: float | None = None,
                      bandwidth_hz: float | None = None, threshold_db: float = 10.0, max_bursts: int = 12,
                      merge_gap_s: float = 0.004) -> dict:
    """Find the bursts in x and demodulate each: OOK (PWM / PPM / Manchester / NRZ) or 2-FSK (NRZ / Manchester),
    with symbol rate and bits - no recipe needed. offset_hz/bandwidth_hz restrict it to one channel; repeats of the
    same message are grouped."""
    nfft = 256 if fs > 1e6 else 128
    events, info = find_events(x, fs, fc, nfft, threshold_db, merge_gap_s, 0.0, 400)
    if offset_hz is not None:
        bw = bandwidth_hz or 200e3
        events = [e for e in events if abs(e["offset_hz"] - offset_hz) <= max(bw / 2, e["bandwidth_hz"] / 2)]
    events = [e for e in events if not e.get("already_on_at_start") or e["duration_s"] < 0.5]
    total = len(events)
    events = sorted(events, key=lambda e: -e["snr_db"])[:max_bursts]
    events.sort(key=lambda e: e["start_s"])
    bursts = []
    for e in events:
        off = offset_hz if offset_hz is not None else e["offset_hz"]
        bw = bandwidth_hz or min(fs * 0.9, max(1.5 * e["bandwidth_hz"], 40e3))
        pad = max(0.0005, 0.1 * e["duration_s"])
        a = max(0, int((e["start_s"] - pad) * fs))
        b = min(len(x), int((e["start_s"] + e["duration_s"] + pad) * fs))
        if b - a < 64:
            continue
        y, fs2 = shift_filter(x[a:b], fs, off, bw)
        r = demod_one(y, fs2)
        bursts.append({"t_ms": round(e["start_s"] * 1e3, 2), "duration_ms": round(e["duration_s"] * 1e3, 3),
                       "freq_hz": round(fc + off + float(r.get("carrier_offset_hz") or 0.0), 0),
                       "bandwidth_hz": e["bandwidth_hz"], **r})
    # the same message sent several times (typical for TPMS, remotes, sensors): group identical bit strings
    groups: dict = {}
    for i, bu in enumerate(bursts):
        if bu.get("bits"):
            groups.setdefault(bu["bits"][:256], []).append(i)
    repeats = [{"bursts": ix, "count": len(ix), "hex": bursts[ix[0]]["hex"], "coding": bursts[ix[0]].get("coding_guess")}
               for k, ix in groups.items() if len(ix) > 1]
    return {"bursts_found": total, "demodulated": len(bursts), "bursts": bursts,
            "repeated_messages": sorted(repeats, key=lambda g: -g["count"]),
            "noise_floor_dbfs": info.get("noise_floor_dbfs")}
