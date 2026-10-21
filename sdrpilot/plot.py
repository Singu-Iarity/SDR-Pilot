"""Dependency-free plotting (numpy + zlib) so the server works on a bare
radioconda install without matplotlib.  Produces PNG bytes the AI can look at."""
from __future__ import annotations

import struct
import zlib

import numpy as np

# ---------------------------------------------------------------- PNG output

def png_bytes(img: np.ndarray) -> bytes:
    """img: HxWx3 uint8."""
    img = np.ascontiguousarray(img, dtype=np.uint8)
    h, w, _ = img.shape
    raw = np.empty((h, 1 + w * 3), dtype=np.uint8)
    raw[:, 0] = 0
    raw[:, 1:] = img.reshape(h, w * 3)

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw.tobytes(), 6)) + chunk(b"IEND", b""))


# ---------------------------------------------------------------- colormap
_ANCH = np.array([[0, 0, 4], [40, 11, 84], [101, 21, 110], [159, 42, 99], [212, 72, 66],
                  [245, 125, 21], [250, 193, 39], [252, 255, 164]], dtype=float)


def colormap(x: np.ndarray) -> np.ndarray:
    x = np.clip(np.nan_to_num(x), 0, 1) * (len(_ANCH) - 1)
    i = np.minimum(x.astype(int), len(_ANCH) - 2)
    f = (x - i)[..., None]
    return (_ANCH[i] * (1 - f) + _ANCH[i + 1] * f).astype(np.uint8)


# ---------------------------------------------------------------- 5x7 font
_G = {
    "0": "01110,10001,10011,10101,11001,10001,01110", "1": "00100,01100,00100,00100,00100,00100,01110",
    "2": "01110,10001,00001,00010,00100,01000,11111", "3": "11110,00001,00001,01110,00001,00001,11110",
    "4": "00010,00110,01010,10010,11111,00010,00010", "5": "11111,10000,11110,00001,00001,10001,01110",
    "6": "00110,01000,10000,11110,10001,10001,01110", "7": "11111,00001,00010,00100,01000,01000,01000",
    "8": "01110,10001,10001,01110,10001,10001,01110", "9": "01110,10001,10001,01111,00001,00010,01100",
    ".": "00000,00000,00000,00000,00000,01100,01100", "-": "00000,00000,00000,11111,00000,00000,00000",
    "+": "00000,00100,00100,11111,00100,00100,00000", ":": "00000,01100,01100,00000,01100,01100,00000",
    "/": "00001,00010,00010,00100,01000,01000,10000", " ": "00000,00000,00000,00000,00000,00000,00000",
    "%": "11001,11010,00010,00100,01000,01011,10011", "=": "00000,00000,11111,00000,11111,00000,00000",
    "(": "00010,00100,01000,01000,01000,00100,00010", ")": "01000,00100,00010,00010,00010,00100,01000",
    "#": "01010,11111,01010,01010,01010,11111,01010", "_": "00000,00000,00000,00000,00000,00000,11111",
    "A": "01110,10001,10001,11111,10001,10001,10001", "B": "11110,10001,10001,11110,10001,10001,11110",
    "C": "01110,10001,10000,10000,10000,10001,01110", "D": "11110,10001,10001,10001,10001,10001,11110",
    "E": "11111,10000,10000,11110,10000,10000,11111", "F": "11111,10000,10000,11110,10000,10000,10000",
    "G": "01110,10001,10000,10111,10001,10001,01111", "H": "10001,10001,10001,11111,10001,10001,10001",
    "I": "01110,00100,00100,00100,00100,00100,01110", "J": "00111,00010,00010,00010,00010,10010,01100",
    "K": "10001,10010,10100,11000,10100,10010,10001", "L": "10000,10000,10000,10000,10000,10000,11111",
    "M": "10001,11011,10101,10101,10001,10001,10001", "N": "10001,10001,11001,10101,10011,10001,10001",
    "O": "01110,10001,10001,10001,10001,10001,01110", "P": "11110,10001,10001,11110,10000,10000,10000",
    "Q": "01110,10001,10001,10001,10101,10010,01101", "R": "11110,10001,10001,11110,10100,10010,10001",
    "S": "01111,10000,10000,01110,00001,00001,11110", "T": "11111,00100,00100,00100,00100,00100,00100",
    "U": "10001,10001,10001,10001,10001,10001,01110", "V": "10001,10001,10001,10001,10001,01010,00100",
    "W": "10001,10001,10001,10101,10101,10101,01010", "X": "10001,10001,01010,00100,01010,10001,10001",
    "Y": "10001,10001,01010,00100,00100,00100,00100", "Z": "11111,00001,00010,00100,01000,10000,11111",
}
_FONT = {k: np.array([[c == "1" for c in row] for row in v.split(",")], dtype=bool) for k, v in _G.items()}

FG = (230, 232, 238)
DIM = (120, 126, 140)
GRID = (44, 48, 60)
BG = (14, 16, 22)
ACCENT = (80, 220, 170)
WARN = (255, 170, 60)
SERIES = [(80, 220, 170), (120, 170, 255), (255, 170, 60), (240, 110, 150), (200, 200, 90)]


class Canvas:
    def __init__(self, w: int, h: int, bg=BG):
        self.img = np.zeros((h, w, 3), dtype=np.uint8)
        self.img[:] = bg
        self.w, self.h = w, h

    def rect(self, x0, y0, x1, y1, color):
        x0, x1 = sorted((int(max(0, x0)), int(min(self.w, x1))))
        y0, y1 = sorted((int(max(0, y0)), int(min(self.h, y1))))
        self.img[y0:y1, x0:x1] = color

    def hline(self, y, x0, x1, color):
        self.rect(x0, y, x1, y + 1, color)

    def vline(self, x, y0, y1, color):
        self.rect(x, y0, x + 1, y1, color)

    def text(self, x, y, s, color=FG, scale=1):
        cx = int(x)
        for ch in str(s).upper():
            g = _FONT.get(ch)
            if g is None:
                g = _FONT[" "]
            if scale > 1:
                g = np.kron(g, np.ones((scale, scale), dtype=bool))
            gh, gw = g.shape
            ys, xs = int(y), cx
            if 0 <= ys and ys + gh <= self.h and 0 <= xs and xs + gw <= self.w:
                region = self.img[ys:ys + gh, xs:xs + gw]
                region[g] = color
            cx += gw + scale
        return cx

    @staticmethod
    def text_width(s, scale=1):
        return len(str(s)) * 6 * scale

    def polyline(self, xs, ys, color):
        xs = np.asarray(xs, dtype=float)
        ys = np.asarray(ys, dtype=float)
        if len(xs) < 2:
            return
        for i in range(len(xs) - 1):
            if not (np.isfinite(xs[i]) and np.isfinite(xs[i + 1]) and np.isfinite(ys[i]) and np.isfinite(ys[i + 1])):
                continue
            n = int(max(abs(xs[i + 1] - xs[i]), abs(ys[i + 1] - ys[i]), 1)) + 1
            px = np.linspace(xs[i], xs[i + 1], n).round().astype(int)
            py = np.linspace(ys[i], ys[i + 1], n).round().astype(int)
            ok = (px >= 0) & (px < self.w) & (py >= 0) & (py < self.h)
            self.img[py[ok], px[ok]] = color

    def blit(self, x, y, rgb):
        h, w, _ = rgb.shape
        self.img[y:y + h, x:x + w] = rgb[: self.h - y, : self.w - x]

    def png(self) -> bytes:
        return png_bytes(self.img)


def nice_ticks(lo: float, hi: float, n: int = 6) -> list[float]:
    if not np.isfinite(lo) or not np.isfinite(hi) or hi <= lo:
        return [lo]
    raw = (hi - lo) / max(n, 1)
    mag = 10 ** np.floor(np.log10(raw))
    step = min((s * mag for s in (1, 2, 2.5, 5, 10) if s * mag >= raw), default=raw)
    start = np.ceil(lo / step) * step
    return [float(v) for v in np.arange(start, hi + step * 1e-9, step)]


def fmt_step(v: float, step: float) -> str:
    """Format a tick value with just enough decimals to distinguish ticks `step` apart."""
    if step <= 0 or not np.isfinite(step):
        return fmt(v)
    dec = max(0, int(-np.floor(np.log10(step) + 1e-9)))
    if step / 10 ** -dec not in (1.0, 2.0, 5.0, 10.0) and round(step, dec) != step:
        dec += 1
    return f"{v:.{min(dec, 6)}f}"


def fmt(v: float) -> str:
    if v == 0:
        return "0"
    a = abs(v)
    if a >= 100:
        return f"{v:.0f}"
    if a >= 10:
        return f"{v:.1f}".rstrip("0").rstrip(".")
    if a >= 1:
        return f"{v:.2f}".rstrip("0").rstrip(".")
    return f"{v:.3g}"


def axes(c: Canvas, x0, y0, x1, y1, xlim, ylim, xlabel="", ylabel="", xticks=True, yticks=True):
    """Draw frame, grid and tick labels; return a mapping function data->pixels."""
    c.rect(x0, y0, x1, y1, (20, 23, 31))
    if xticks:
        xt = nice_ticks(*xlim, n=max(2, (x1 - x0) // 90))
        xstep = xt[1] - xt[0] if len(xt) > 1 else 0
        for t in xt:
            px = x0 + (t - xlim[0]) / (xlim[1] - xlim[0]) * (x1 - x0)
            c.vline(int(px), y0, y1, GRID)
            lab = fmt_step(t, xstep)
            c.text(int(px - Canvas.text_width(lab) / 2), y1 + 4, lab, DIM)
    if yticks:
        yt = nice_ticks(*ylim, n=max(2, (y1 - y0) // 40))
        ystep = yt[1] - yt[0] if len(yt) > 1 else 0
        for t in yt:
            py = y1 - (t - ylim[0]) / (ylim[1] - ylim[0]) * (y1 - y0)
            c.hline(int(py), x0, x1, GRID)
            lab = fmt_step(t, ystep)
            c.text(x0 - Canvas.text_width(lab) - 4, int(py - 3), lab, DIM)
    c.hline(y1, x0, x1, DIM)
    c.vline(x0, y0, y1, DIM)
    if xlabel:
        c.text(x1 - Canvas.text_width(xlabel), y1 + 14, xlabel, FG)
    if ylabel:
        c.text(x0 + 4, y0 + 3, ylabel, FG)

    def to_px(x, y):
        x = np.asarray(x, dtype=float)
        y = np.asarray(y, dtype=float)
        px = x0 + (x - xlim[0]) / (xlim[1] - xlim[0]) * (x1 - x0)
        py = y1 - (np.clip(y, ylim[0], ylim[1]) - ylim[0]) / (ylim[1] - ylim[0]) * (y1 - y0)
        return px, py

    return to_px


def downsample_minmax(x: np.ndarray, y: np.ndarray, width: int):
    """Envelope-preserving decimation for plotting long series."""
    n = len(y)
    if n <= width * 2:
        return x, y
    edges = np.linspace(0, n, width + 1).astype(int)  # every point lands in a column (none dropped at the end)
    edges = edges[:-1][np.diff(edges) > 0]
    y = np.asarray(y, dtype=float)
    lo, hi = np.minimum.reduceat(y, edges), np.maximum.reduceat(y, edges)
    xx = np.asarray(x)[edges]
    return np.repeat(xx, 2), np.column_stack([lo, hi]).ravel()


def series_plot(panels: list[dict], title: str = "", width: int = 1000, panel_h: int = 170) -> bytes:
    """panels: [{x, ys:[(label, y)], xlabel, ylabel, ylim?, kind:'line'|'hist'}]"""
    h = 30 + len(panels) * (panel_h + 34)
    c = Canvas(width, h)
    c.text(12, 10, title, FG, 2 if len(title) < 70 else 1)
    y = 36
    for p in panels:
        x = np.asarray(p["x"], dtype=float)
        all_y = np.concatenate([np.asarray(v, dtype=float) for _, v in p["ys"]]) if p["ys"] else np.zeros(1)
        finite = all_y[np.isfinite(all_y)]
        ylim = p.get("ylim") or (float(finite.min()) if finite.size else 0, float(finite.max()) if finite.size else 1)
        if ylim[1] <= ylim[0]:
            ylim = (ylim[0] - 1, ylim[1] + 1)
        pad = 0.05 * (ylim[1] - ylim[0])
        ylim = (ylim[0] - pad, ylim[1] + pad) if not p.get("ylim") else ylim
        xlim = (float(x.min()), float(x.max())) if x.size > 1 else (0, 1)
        if xlim[1] <= xlim[0]:
            xlim = (xlim[0], xlim[0] + 1)
        to_px = axes(c, 70, y, width - 20, y + panel_h, xlim, ylim, p.get("xlabel", ""), p.get("ylabel", ""))
        lx = 80
        for i, (label, yy) in enumerate(p["ys"]):
            col = SERIES[i % len(SERIES)]
            xs, ys = downsample_minmax(x, np.asarray(yy, dtype=float), width - 90)
            px, py = to_px(xs, ys)
            if p.get("kind") == "bars":
                base = to_px(xs, np.full_like(ys, max(ylim[0], 0)))[1]
                for a, b, cpy in zip(px, py, base):
                    c.vline(int(a), int(min(b, cpy)), int(max(b, cpy)) + 1, col)
            else:
                c.polyline(px, py, col)
            if label:
                lx = c.text(lx, y + 14, label, col) + 14
        y += panel_h + 34
    return c.png()


BAD = (255, 107, 129)
LABEL_BG = (6, 7, 10)


def _label(c, x, y, s, color, scale=1):
    """Text on a dark box so it stays readable over the waterfall."""
    w = Canvas.text_width(s, scale) + 4
    x = int(min(max(x, 0), c.w - w))
    c.rect(x, y - 2, x + w, y + 7 * scale + 2, LABEL_BG)
    c.text(x + 2, y, s, color, scale)


WIDE = (90, 205, 255)    # wideband / spread-spectrum blocks
TRANS = (255, 110, 210)  # whole-band transients


def waterfall_plot(wf_db: np.ndarray, f_lo_hz: float, f_hi_hz: float, t_span_s: float,
                   avg_db: np.ndarray, peaks: list[dict], title: str, max_db=None, min_db=None,
                   time_label: str = "TIME (S)", markers: list[tuple] | None = None,
                   width: int = 1100, wf_h: int = 300, sp_h: int = 200, annotate: bool = False,
                   dc_hz: float | None = None, features: dict | None = None, ann: set | None = None,
                   n_frames: int = 0) -> bytes:
    """Waterfall (time downwards) + averaged/peak-hold spectrum trace + peak markers.
    annotate: frequency grid with MHz labels across the waterfall, a labelled line through every detected signal,
    and a DC marker at dc_hz - so a frequency can be read straight off the picture."""
    if ann is None:  # old behaviour: annotate switches grid + signal lines + DC together
        ann = {"grid", "signals", "dc"} if annotate else set()
    features = features or {}
    annotate = bool(ann & {"grid", "signals", "dc"})
    left, right = 70, width - 20
    pw = right - left
    top = 54 if annotate else 36
    h = top + 4 + wf_h + 40 + sp_h + 40
    c = Canvas(width, h)
    c.text(12, 10, title, FG, 2 if len(title) < 75 else 1)

    lo = np.nanpercentile(wf_db, 5) if min_db is None else min_db
    hi = np.nanmax(wf_db) if max_db is None else max_db
    hi = max(hi, lo + 10)
    # resample waterfall to plot size
    rows = wf_db.shape[0]
    ri = np.linspace(0, rows - 1, wf_h).round().astype(int)
    cols = wf_db.shape[1]
    ci = np.linspace(0, cols - 1, pw)
    # max-pool across bins so narrow signals survive horizontal shrink
    if cols > pw:
        edges = np.linspace(0, cols, pw + 1).astype(int)
        pooled = np.maximum.reduceat(wf_db, edges[:-1], axis=1)
        img = pooled[ri]
    else:
        img = wf_db[ri][:, ci.round().astype(int)]
    rgb = colormap((img - lo) / (hi - lo))
    y0 = top
    c.blit(left, y0, rgb)
    fl, fh = f_lo_hz / 1e6, f_hi_hz / 1e6
    to_x = lambda mhz: left + (mhz - fl) / max(fh - fl, 1e-12) * pw  # noqa: E731
    img_of = {im["signal"]: im["image_of"] for im in features.get("images", [])} if "images" in ann else {}
    spur_of = {sp["signal"] for sp in features.get("spurs", [])} if "spurs" in ann else set()
    legend = []
    if "grid" in ann:
        ticks = nice_ticks(fl, fh, max(4, pw // 130))
        step = ticks[1] - ticks[0] if len(ticks) > 1 else (fh - fl)
        for t in ticks:  # dotted frequency grid + MHz labels above the waterfall
            x = int(round(to_x(t)))
            if left <= x < right:
                c.img[y0:y0 + wf_h:5, x] = (150, 156, 172)
                lab = fmt_step(t, step)
                c.text(int(x - Canvas.text_width(lab) / 2), y0 - 12, lab, FG)
    if "dc" in ann and dc_hz is not None and fl <= dc_hz / 1e6 <= fh:
        x = int(round(to_x(dc_hz / 1e6)))
        c.img[y0:y0 + wf_h:9, x] = BAD  # sparse dots + edge ticks: the data under it stays visible
        c.img[max(0, y0 - 5):y0, x] = BAD
        c.img[y0 + wf_h:y0 + wf_h + 4, x] = BAD
        _label(c, x + 4, y0 + wf_h - 14, "DC SPIKE (CENTRE)", BAD)
    if "signals" in ann:
        for i, pk in enumerate(peaks[:12]):  # a labelled line through each detected signal
            x = int(round(to_x(pk["freq_hz"] / 1e6)))
            if not left <= x < right or "DC spike" in str(pk.get("note", "")):
                continue
            col = DIM if (i + 1) in img_of else WARN
            c.img[y0:y0 + wf_h:7, x] = col
            c.img[y0 + 1:y0 + wf_h:7, x] = col
            c.img[max(0, y0 - 5):y0, x] = col
            lab = f"{i + 1}: {pk['freq_hz'] / 1e6:.4f}"
            if (i + 1) in img_of:
                lab += f" IMAGE OF {img_of[i + 1]}?"
            if (i + 1) in spur_of:
                lab += " SPUR"
            _label(c, x + 3, y0 + 4 + (i % 4) * 13, lab, col)
    if "wideband" in ann:  # spread-spectrum / OFDM blocks: dashed edges on the waterfall (no fill), bracket below
        for wi, w in enumerate(features.get("wideband", [])):
            xa, xb = int(round(to_x(w["from_hz"] / 1e6))), int(round(to_x(w["to_hz"] / 1e6)))
            xa, xb = max(left, xa), min(right - 1, xb)
            if xb <= xa:
                continue
            for x in (xa, xb):
                c.img[y0:y0 + wf_h:4, x] = WIDE
                c.img[y0 + 1:y0 + wf_h:4, x] = WIDE
            yb = y0 + wf_h + 6
            c.hline(yb, xa, xb, WIDE)
            c.vline(xa, yb - 4, yb + 1, WIDE)
            c.vline(xb, yb - 4, yb + 1, WIDE)
            lab = f"W{wi + 1} WIDEBAND {w['from_hz'] / 1e6:.2f}-{w['to_hz'] / 1e6:.2f} +{w['rise_db']:.0f}DB"
            if Canvas.text_width(lab) > xb - xa - 4:
                lab = f"W{wi + 1} +{w['rise_db']:.0f}DB"
            c.text(int(max(left, min((xa + xb) / 2 - Canvas.text_width(lab) / 2, right - Canvas.text_width(lab)))),
                   yb + 4, lab, WIDE)
        if features.get("wideband"):
            legend.append("W = WIDEBAND/SPREAD BLOCK")
    if "transients" in ann and n_frames:
        for tr in features.get("transients", []):
            ya = y0 + tr["frame_from"] / n_frames * wf_h
            yb2 = y0 + tr["frame_to"] / n_frames * wf_h
            c.rect(left - 8, int(ya), left - 2, int(max(yb2, ya + 2)), TRANS)
        if features.get("transients"):
            legend.append("PINK TICK = WHOLE-BAND TRANSIENT")
    if "edges" in ann:
        for e in features.get("edges", []):
            f = e.get("below_hz") or e.get("above_hz")
            x = int(round(to_x(f / 1e6)))
            if left <= x < right:
                c.img[y0:y0 + wf_h:6, x] = DIM
                _label(c, x + (3 if e["side"] == "low" else -64), y0 + wf_h - 28, "FILTER EDGE", DIM)
    for t in nice_ticks(0, t_span_s, 5):
        py = y0 + t / max(t_span_s, 1e-12) * wf_h
        lab = fmt(t)
        c.text(left - Canvas.text_width(lab) - 4, int(py - 3), lab, DIM)
    c.text(left - 60, y0 + wf_h + 4, time_label, DIM)
    if markers:
        for (t0, t1, label) in markers:
            pa = y0 + t0 / max(t_span_s, 1e-12) * wf_h
            pb = y0 + t1 / max(t_span_s, 1e-12) * wf_h
            c.rect(right + 2, pa, right + 8, max(pb, pa + 2), WARN)

    sy0 = y0 + wf_h + 30
    freqs = np.linspace(fl, fh, len(avg_db))
    ylo, yhi = float(np.nanpercentile(avg_db, 2)) - 5, float(np.nanmax(avg_db)) + 5
    to_px = axes(c, left, sy0, right, sy0 + sp_h, (fl, fh), (ylo, yhi), "MHZ", "DB")
    xs, ys = downsample_minmax(freqs, avg_db, pw)
    px, py = to_px(xs, ys)
    c.polyline(px, py, ACCENT)
    for i, p in enumerate(peaks[:12]):
        fx, fy = to_px(p["freq_hz"] / 1e6, p["power_db"])
        c.vline(int(fx), int(fy) - 10, int(fy) - 2, WARN)
        c.text(int(fx) - 3, int(fy) - 20, str(i + 1), WARN)
        c.vline(int(fx), y0, y0 + 4, WARN)
    if "wideband" in ann:
        for wi, w in enumerate(features.get("wideband", [])):
            fa, fb = w["from_hz"] / 1e6, w["to_hz"] / 1e6
            (xa, ya_), (xb, _yb) = to_px(fa, yhi - 1), to_px(fb, yhi - 1)
            ya_ = int(sy0 + 6 + (wi % 2) * 8)
            c.hline(ya_, int(max(left, xa)), int(min(right - 1, xb)), WIDE)
            c.vline(int(max(left, xa)), ya_, ya_ + 4, WIDE)
            c.vline(int(min(right - 1, xb)), ya_, ya_ + 4, WIDE)
            c.text(int(max(left, xa)) + 2, ya_ + 2, f"W{wi + 1}", WIDE)
    c.text(left, h - 16, f"SPAN {fh - fl:.3f} MHZ   COLOR {lo:.0f}..{hi:.0f} DB" +
           ("   " + "   ".join(legend) if legend else ""), DIM)
    return c.png()
