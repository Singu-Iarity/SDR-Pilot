"""Plot anything against anything: the AI's general-purpose graphing for deeper analysis.

Data comes from IQ (live radio or a capture: time-domain quantities, a spectrum, a spectrogram), decoded records
(decoded.jsonl from a decoder job - with bearing/range/elevation from the station when records carry positions),
a CSV/JSON/NPY file, or inline numbers. The AI picks the plot kind, what goes on each axis, each axis' scale
(linear / log / dB / symlog / time...), limits, and which fields drive colour, opacity and marker size.
Everything is drawn with matplotlib (Agg) and comes back as a PNG plus a numeric summary for models without vision.
"""
from __future__ import annotations

import ast
import csv
import io
import json
import math
import os

import numpy as np

# ------------------------------------------------------------------ safe expressions ("derive" / "where")
_FUNCS = {
    "abs": np.abs, "sqrt": np.sqrt, "log": np.log, "log10": np.log10, "log2": np.log2, "exp": np.exp,
    "sin": np.sin, "cos": np.cos, "tan": np.tan, "arctan2": np.arctan2, "atan2": np.arctan2, "arcsin": np.arcsin,
    "arccos": np.arccos, "arctan": np.arctan, "deg": np.degrees, "rad": np.radians, "degrees": np.degrees,
    "radians": np.radians, "floor": np.floor, "ceil": np.ceil, "round": np.round, "sign": np.sign,
    "minimum": np.minimum, "maximum": np.maximum, "clip": np.clip, "where": np.where, "isnan": np.isnan,
    "unwrap": np.unwrap, "diff": lambda a: np.concatenate([[np.nan], np.diff(a)]), "cumsum": np.cumsum,
    "mean": np.nanmean, "median": np.nanmedian, "std": np.nanstd, "min": np.nanmin, "max": np.nanmax,
    "db": lambda a: 10 * np.log10(np.maximum(np.abs(a), 1e-30)),
    "db20": lambda a: 20 * np.log10(np.maximum(np.abs(a), 1e-30)),
    "movavg": lambda a, n=10: np.convolve(np.nan_to_num(a), np.ones(int(n)) / int(n), mode="same"),
    "wrap180": lambda a: (np.asarray(a) + 180) % 360 - 180, "wrap360": lambda a: np.asarray(a) % 360,
}
_CONSTS = {"pi": math.pi, "e": math.e, "c": 299792458.0, "nan": float("nan"), "True": True, "False": False}
_OK_NODES = (ast.Expression, ast.BinOp, ast.UnaryOp, ast.BoolOp, ast.Compare, ast.Call, ast.Name, ast.Load,
             ast.Constant, ast.IfExp, ast.Subscript, ast.Slice, ast.Tuple, ast.List,
             ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod, ast.Pow, ast.USub, ast.UAdd, ast.Not,
             ast.And, ast.Or, ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE, ast.BitAnd, ast.BitOr,
             ast.Invert, ast.In, ast.NotIn)


SCALARS = "__scalars__"  # key in a table holding single values (fs, fc, noise_floor_db, AI variables...)


def nrows(cols: dict) -> int:
    for k, v in cols.items():
        if k != SCALARS:
            return len(v)
    return 0


def evaluate(expr: str, cols: dict) -> np.ndarray:
    """Evaluate a small math expression over the table's columns. Only arithmetic, comparisons, and/or/not and the
    functions in _FUNCS - no attributes, no imports, nothing else."""
    src = str(expr).strip()
    src = src.replace("&&", " and ").replace("||", " or ")
    tree = ast.parse(src, mode="eval")
    for node in ast.walk(tree):
        if not isinstance(node, _OK_NODES):
            raise ValueError(f"expression '{expr}': {type(node).__name__} isn't allowed (use column names, numbers, "
                             f"+ - * / ** %, comparisons, and/or/not, and {sorted(_FUNCS)})")
        if isinstance(node, ast.Call) and not (isinstance(node.func, ast.Name) and node.func.id in _FUNCS):
            raise ValueError(f"expression '{expr}': unknown function - available: {sorted(_FUNCS)}")
        if isinstance(node, ast.Name) and node.id not in cols and node.id not in _FUNCS and node.id not in _CONSTS \
                and node.id not in (cols.get(SCALARS) or {}):
            raise ValueError(f"expression '{expr}': no column '{node.id}'. Columns: "
                             f"{sorted(k for k in cols if k != SCALARS)[:60]}; values: {sorted(cols.get(SCALARS) or {})}")

    class _BoolFix(ast.NodeTransformer):  # 'a > 1 and b < 2' on arrays -> elementwise
        def visit_BoolOp(self, node):
            self.generic_visit(node)
            op = ast.BitAnd() if isinstance(node.op, ast.And) else ast.BitOr()
            out = node.values[0]
            for v in node.values[1:]:
                out = ast.BinOp(left=out, op=op, right=v)
            return out

        def visit_UnaryOp(self, node):
            self.generic_visit(node)
            if isinstance(node.op, ast.Not):
                return ast.UnaryOp(op=ast.Invert(), operand=node.operand)
            return node

        def visit_Compare(self, node):
            self.generic_visit(node)
            if len(node.ops) == 1 and isinstance(node.ops[0], (ast.In, ast.NotIn)):
                call = ast.Call(func=ast.Name(id="__isin", ctx=ast.Load()), args=[node.left, node.comparators[0]],
                                keywords=[])
                return call if isinstance(node.ops[0], ast.In) else ast.UnaryOp(op=ast.Invert(), operand=call)
            if len(node.ops) > 1:  # a < b < c
                parts, left = [], node.left
                for op, right in zip(node.ops, node.comparators):
                    parts.append(ast.Compare(left=left, ops=[op], comparators=[right]))
                    left = right
                out = parts[0]
                for p in parts[1:]:
                    out = ast.BinOp(left=out, op=ast.BitAnd(), right=p)
                return out
            return node

    tree = ast.fix_missing_locations(_BoolFix().visit(tree))
    env = {"__builtins__": {}}
    env.update(_FUNCS)
    env.update(_CONSTS)
    env["__isin"] = lambda a, vals: np.isin(np.asarray(a, dtype=object).astype(str),
                                            [str(v) for v in (vals if isinstance(vals, (list, tuple)) else [vals])])
    env.update({k: v for k, v in cols.items() if k != SCALARS})
    env.update(cols.get(SCALARS) or {})
    with np.errstate(all="ignore"):
        return eval(compile(tree, "<expr>", "eval"), env)  # noqa: S307 - AST checked above


# ------------------------------------------------------------------ tables
def _num(a) -> np.ndarray:
    """A column as floats where possible (NaN for missing / non-numeric)."""
    arr = np.asarray(a)
    if arr.dtype.kind in "fiub":
        return arr.astype(float)
    out = np.full(len(arr), np.nan)
    for i, v in enumerate(arr):
        try:
            out[i] = float(v)
        except (TypeError, ValueError):
            pass
    return out


def _flatten(rec: dict, prefix: str = "", out: dict | None = None) -> dict:
    out = {} if out is None else out
    for k, v in rec.items():
        key = f"{prefix}{k}"
        if isinstance(v, dict) and len(key) < 40:
            _flatten(v, key + "_", out)
        elif isinstance(v, (list, tuple)):
            out[key] = json.dumps(v)[:200]
        else:
            out[key] = v
    return out


def records_table(records: list[dict]) -> dict:
    recs = [_flatten(r) for r in records if isinstance(r, dict)]
    keys: list[str] = []
    for r in recs:
        for k in r:
            if k not in keys:
                keys.append(k)
    cols = {}
    for k in keys:
        vals = [r.get(k) for r in recs]
        nums = _num(np.array(vals, dtype=object))
        numeric = sum(1 for v in vals if v not in (None, "")) and np.isfinite(nums).sum() >= 0.8 * sum(
            1 for v in vals if v not in (None, ""))
        cols[k] = nums if numeric else np.array(["" if v is None else str(v) for v in vals], dtype=object)
    if "t" in cols and np.isfinite(cols["t"]).any():
        t0 = np.nanmin(cols["t"])
        cols["time_s"] = cols["t"] - t0
        cols["time"] = cols["t"]  # unix seconds: use scale 'time' for clock labels
    cols["index"] = np.arange(len(recs), dtype=float)
    return cols


def load_records_file(path: str) -> list[dict]:
    out = []
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            if line.startswith("SPDATA "):
                line = line[7:]
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if isinstance(r, dict):
                out.append(r)
    return out


def file_table(path: str) -> dict:
    ext = os.path.splitext(path)[1].lower()
    if ext in (".jsonl", ".ndjson", ".log", ".txt"):
        return records_table(load_records_file(path))
    if ext == ".json":
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        return data_table(d)
    if ext == ".npy":
        a = np.load(path, allow_pickle=False)
        if a.ndim == 1:
            return {"index": np.arange(len(a), dtype=float), "value": a.astype(float)}
        return {f"c{i}": a[:, i].astype(float) for i in range(a.shape[1])} | {"index": np.arange(len(a), dtype=float)}
    with open(path, encoding="utf-8", errors="replace", newline="") as f:  # csv / tsv
        sample = f.read(4096)
        f.seek(0)
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=",;\t ")
        except csv.Error:
            dialect = csv.excel
        rows = list(csv.DictReader(f, dialect=dialect))
    return records_table(rows)


def data_table(d) -> dict:
    """Inline data: {"col": [...], ...} or [{"col": v, ...}, ...] or a bare list of numbers."""
    if isinstance(d, dict):
        n = max((len(v) for v in d.values() if isinstance(v, (list, tuple))), default=0)
        cols = {}
        for k, v in d.items():
            if isinstance(v, (list, tuple)) and len(v) == n:
                nums = _num(np.array(v, dtype=object))
                cols[k] = nums if np.isfinite(nums).sum() >= 0.8 * n else np.array([str(x) for x in v], dtype=object)
        cols.setdefault("index", np.arange(n, dtype=float))
        return cols
    if isinstance(d, list) and d and all(isinstance(r, dict) for r in d):
        return records_table(d)
    if isinstance(d, list):
        return {"index": np.arange(len(d), dtype=float), "value": _num(np.array(d, dtype=object))}
    raise ValueError("data must be {column: [values]}, a list of records, or a list of numbers")


# ------------------------------------------------------------------ IQ -> tables
IQ_FIELDS = {
    "time": "seconds from the window start", "time_ms": "milliseconds", "time_us": "microseconds",
    "sample": "sample index", "i": "in-phase (real) part", "q": "quadrature (imag) part",
    "amplitude": "|x| (linear, full scale = 1)", "power": "|x|^2", "power_db": "dBFS",
    "phase": "radians, wrapped", "phase_unwrapped": "radians", "phase_deg": "degrees, wrapped",
    "inst_freq": "instantaneous frequency, Hz (FM/FSK demod)", "envelope_db": "smoothed power, dBFS",
    "inst_freq_khz": "kHz", "freq_dev": "inst_freq minus its median (FSK deviation), Hz", "envelope": "smoothed |x|",
    "am": "AM demod: amplitude / mean - 1", "snr_db": "power above the noise floor, dB", "i_ac": "I without DC",
    "q_ac": "Q without DC", "phase_rate": "phase change per sample, rad", "fm_smooth": "inst_freq low-passed, Hz",
}
IQ_SCALARS = "fs, fc, n, duration_s, noise_floor_db, mean_power_db, peak_power_db, papr_db, dc_i, dc_q"
SPEC_FIELDS = {"frequency": "absolute Hz", "frequency_mhz": "absolute MHz", "offset_hz": "Hz from the centre",
               "psd_db": "dBFS per bin", "psd": "linear power per bin", "max_db": "peak-hold dBFS", "min_db": "minimum dBFS",
               "snr_db": "psd_db above the noise floor", "psd_dbhz": "dBFS/Hz", "cum_power": "cumulative power share 0..1 "
               "(occupied bandwidth: where it crosses 0.005 and 0.995)"}
SPEC_SCALARS = "fs, fc, nfft, bin_hz, frames, noise_floor_db, total_power_db, peak_freq_hz, obw99_hz"


def iq_table(x: np.ndarray, fs: float, smooth: int = 0) -> dict:
    n = len(x)
    t = np.arange(n) / fs
    p = (x.real.astype(np.float64) ** 2 + x.imag.astype(np.float64) ** 2)
    ph = np.angle(x).astype(np.float64)
    inst = np.concatenate([[np.nan], np.angle(x[1:] * np.conj(x[:-1])) * fs / (2 * np.pi)]) if n > 1 else np.array([0.0])
    cols = {"time": t, "time_ms": t * 1e3, "time_us": t * 1e6, "sample": np.arange(n, dtype=float),
            "i": x.real.astype(np.float64), "q": x.imag.astype(np.float64), "amplitude": np.sqrt(p), "power": p,
            "power_db": 10 * np.log10(np.maximum(p, 1e-20)), "phase": ph, "phase_unwrapped": np.unwrap(ph),
            "phase_deg": np.degrees(ph), "inst_freq": inst, "index": np.arange(n, dtype=float)}
    k = int(smooth) if smooth else max(1, int(fs * 20e-6))
    env = np.convolve(p, np.ones(k) / k, mode="same") if k > 1 else p
    cols["envelope_db"] = 10 * np.log10(np.maximum(env, 1e-20))
    cols["envelope"] = np.sqrt(env)
    amp = cols["amplitude"]
    mean_amp = float(np.mean(amp)) or 1e-12
    cols["am"] = amp / mean_amp - 1
    cols["inst_freq_khz"] = inst / 1e3
    med = float(np.nanmedian(inst)) if n > 1 else 0.0
    cols["freq_dev"] = inst - med
    kf = max(1, k)
    cols["fm_smooth"] = np.convolve(np.nan_to_num(inst), np.ones(kf) / kf, mode="same") if kf > 1 else inst
    cols["phase_rate"] = np.concatenate([[np.nan], np.angle(x[1:] * np.conj(x[:-1]))]) if n > 1 else np.array([0.0])
    dci, dcq = float(np.mean(x.real)), float(np.mean(x.imag))
    cols["i_ac"], cols["q_ac"] = cols["i"] - dci, cols["q"] - dcq
    floor = float(np.percentile(env, 10)) if n else 1e-20
    cols["snr_db"] = 10 * np.log10(np.maximum(env, 1e-20) / max(floor, 1e-20))
    mp = float(np.mean(p)) if n else 1e-20
    cols[SCALARS] = {"fs": float(fs), "n": n, "duration_s": n / fs, "noise_floor_db": 10 * math.log10(max(floor, 1e-20)),
                     "mean_power_db": 10 * math.log10(max(mp, 1e-20)),
                     "peak_power_db": 10 * math.log10(max(float(np.max(p)) if n else 1e-20, 1e-20)),
                     "papr_db": 10 * math.log10(max(float(np.max(p)) if n else 1e-20, 1e-20) / max(mp, 1e-20)),
                     "dc_i": dci, "dc_q": dcq}
    return cols


def spectrum_table(x: np.ndarray, fs: float, fc: float, fft_size: int = 1024, average: str = "mean",
                   window: str = "hann") -> dict:
    n = max(16, min(int(fft_size), len(x)))
    frames = max(1, len(x) // n)
    fr = x[: frames * n].reshape(frames, n)
    w = {"hann": np.hanning(n), "hamming": np.hamming(n), "blackman": np.blackman(n),
         "rect": np.ones(n), "flattop": _flattop(n)}.get(window, np.hanning(n))
    P = np.abs(np.fft.fftshift(np.fft.fft(fr * w, axis=1), axes=1)) ** 2 / float(w.sum() ** 2)
    if average == "max":
        p = P.max(axis=0)
    elif average == "min":
        p = P.min(axis=0)
    elif average == "median":
        p = np.median(P, axis=0)
    else:
        p = P.mean(axis=0)
    off = (np.arange(n) - n / 2) * fs / n
    psd_db = 10 * np.log10(np.maximum(p, 1e-20))
    floor = float(np.median(psd_db))
    cum = np.cumsum(p) / max(float(np.sum(p)), 1e-30)
    lo_i, hi_i = int(np.searchsorted(cum, 0.005)), int(np.searchsorted(cum, 0.995))
    bin_hz = fs / n
    return {"frequency": fc + off, "frequency_mhz": (fc + off) / 1e6, "offset_hz": off, "psd": p, "psd_db": psd_db,
            "max_db": 10 * np.log10(np.maximum(P.max(axis=0), 1e-20)),
            "min_db": 10 * np.log10(np.maximum(P.min(axis=0), 1e-20)), "index": np.arange(n, dtype=float),
            "snr_db": psd_db - floor, "psd_dbhz": psd_db - 10 * math.log10(bin_hz), "cum_power": cum,
            SCALARS: {"fs": float(fs), "fc": float(fc), "nfft": n, "bin_hz": bin_hz, "frames": frames,
                      "noise_floor_db": floor, "total_power_db": 10 * math.log10(max(float(np.sum(p)), 1e-30)),
                      "peak_freq_hz": float(fc + off[int(np.argmax(p))]),
                      "obw99_hz": float(max(0, hi_i - lo_i) * bin_hz)}}


def _flattop(n: int) -> np.ndarray:
    a = [0.21557895, 0.41663158, 0.277263158, 0.083578947, 0.006947368]
    k = np.arange(n) / max(n - 1, 1)
    return sum(((-1) ** i) * a[i] * np.cos(2 * np.pi * i * k) for i in range(5))


# ------------------------------------------------------------------ positions (bearing / range / elevation)
LAT_KEYS = ("lat", "latitude", "position_lat", "pos_lat")
LON_KEYS = ("lon", "lng", "longitude", "position_lon", "pos_lon")


def add_geo(cols: dict, station: dict | None, altitude_unit: str = "ft") -> list[str]:
    """bearing_deg / range_km / elevation_deg / ground_km from the station for records with positions."""
    lat_k = next((k for k in LAT_KEYS if k in cols and cols[k].dtype.kind == "f"), None)
    lon_k = next((k for k in LON_KEYS if k in cols and cols[k].dtype.kind == "f"), None)
    if not (lat_k and lon_k) or not station or station.get("lat") is None:
        return []
    la1, lo1 = math.radians(float(station["lat"])), math.radians(float(station["lon"]))
    la2, lo2 = np.radians(cols[lat_k]), np.radians(cols[lon_k])
    dlo = lo2 - lo1
    y = np.sin(dlo) * np.cos(la2)
    xx = math.cos(la1) * np.sin(la2) - math.sin(la1) * np.cos(la2) * np.cos(dlo)
    cols["bearing_deg"] = (np.degrees(np.arctan2(y, xx)) + 360) % 360
    a = np.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * np.cos(la2) * np.sin(dlo / 2) ** 2
    ground = 2 * 6371.0088 * np.arcsin(np.sqrt(np.clip(a, 0, 1)))
    cols["ground_km"] = ground
    cols["range_km"] = ground
    added = ["bearing_deg", "ground_km", "range_km"]
    alt_k = next((k for k in ("altitude_m", "alt_m", "altitude", "alt", "altitude_ft") if k in cols
                  and cols[k].dtype.kind == "f"), None)
    if alt_k:
        alt = cols[alt_k] * (0.3048 if (alt_k.endswith("_ft") or (altitude_unit == "ft" and not alt_k.endswith("_m")))
                             else 1.0)
        h = alt - float(station.get("alt_m") or 0)
        ground_m = ground * 1000
        # elevation over a curved earth (4/3 radius for radio refraction)
        re = 6371008.8 * 4 / 3
        cols["elevation_deg"] = np.degrees(np.arctan2(h - ground_m ** 2 / (2 * re), ground_m))
        cols["slant_km"] = np.sqrt(ground_m ** 2 + h ** 2) / 1000
        cols["range_km"] = cols["slant_km"]
        added += ["elevation_deg", "slant_km"]
    return added


# ------------------------------------------------------------------ plotting
def _axis(spec, default_field: str | None = None) -> dict:
    if spec is None:
        return {"field": default_field} if default_field else {}
    if isinstance(spec, str):
        return {"field": spec}
    if isinstance(spec, dict):
        d = dict(spec)
        if "field" not in d:
            d["field"] = d.pop("expr", None) or d.pop("column", None) or d.pop("name", None) or default_field
        return d
    raise ValueError(f"axis must be a field name or {{'field':..., 'scale':..., 'min':..., 'max':...}}, got {spec!r}")


def _col(cols: dict, spec_field) -> np.ndarray:
    if spec_field is None:
        raise ValueError("an axis has no field")
    if isinstance(spec_field, (int, float)):
        return np.full(nrows(cols), float(spec_field))
    f = str(spec_field)
    if f in cols and f != SCALARS:
        return cols[f]
    v = np.asarray(evaluate(f, cols))
    return np.full(nrows(cols), float(v)) if v.ndim == 0 else v


SCALES = ("linear", "log", "symlog", "logit", "db", "db20", "time", "sqrt", "reverse")


def _apply_scale(ax, which: str, spec: dict, vals: np.ndarray):
    """Set an axis' scale. 'db'/'db20'/'sqrt' transform the values (returned); log/symlog/logit/time are matplotlib
    scales on the original values."""
    sc = str(spec.get("scale") or "linear").lower()
    if sc not in SCALES:
        raise ValueError(f"scale '{sc}' unknown - use one of {SCALES}")
    setter = ax.set_xscale if which == "x" else ax.set_yscale
    if sc == "db":
        vals = 10 * np.log10(np.maximum(np.abs(vals.astype(float)), 1e-30))
    elif sc == "db20":
        vals = 20 * np.log10(np.maximum(np.abs(vals.astype(float)), 1e-30))
    elif sc == "sqrt":
        vals = np.sqrt(np.maximum(vals.astype(float), 0))
    elif sc == "log":
        setter("log")
    elif sc == "symlog":
        setter("symlog", linthresh=float(spec.get("linthresh") or 1.0))
    elif sc == "logit":
        setter("logit")
    elif sc == "time":
        import matplotlib.dates as mdates
        from datetime import datetime
        off = datetime.now().astimezone().utcoffset().total_seconds()  # clock labels in the PC's local time
        vals = np.array([np.datetime64(int((v + off) * 1e6), "us") if np.isfinite(v) else np.datetime64("NaT")
                         for v in vals.astype(float)])
        (ax.xaxis if which == "x" else ax.yaxis).set_major_formatter(mdates.DateFormatter("%H:%M:%S"))
    return vals, sc


def _numify(v, cols: dict):
    """A number given as text inside a plot spec ('noise_floor_db + 30', 'center_freq/1e6', '=baud*2')."""
    if v is None or isinstance(v, (int, float)):
        return v
    t = str(v).strip().lstrip("=")
    try:
        return float(t)
    except ValueError:
        r = np.asarray(evaluate(t, cols))
        if r.ndim:
            raise ValueError(f"'{v}' must work out to one number")
        return float(r)


def _numify_spec(spec, cols: dict):
    if isinstance(spec, dict):
        out = dict(spec)
        for k in ("min", "max", "linthresh", "vmin", "vmax"):
            if k in out:
                out[k] = _numify(out[k], cols)
        if isinstance(out.get("range"), (list, tuple)):
            out["range"] = [_numify(r, cols) for r in out["range"]]
        return out
    if isinstance(spec, list):
        return [_numify_spec(s, cols) for s in spec]
    return spec


def _limits(ax, which: str, spec: dict):
    lo, hi = spec.get("min"), spec.get("max")
    if lo is not None or hi is not None:
        (ax.set_xlim if which == "x" else ax.set_ylim)(lo, hi)
    if spec.get("invert"):
        (ax.invert_xaxis if which == "x" else ax.invert_yaxis)()


def _label(spec: dict, field) -> str:
    lab = spec.get("label") or str(field)
    unit = spec.get("unit")
    sc = spec.get("scale")
    if unit:
        lab += f" ({unit})"
    if sc in ("db", "db20") and "db" not in lab.lower():
        lab += " [dB]"
    return lab


def _norm(v: np.ndarray, lo=None, hi=None) -> np.ndarray:
    v = v.astype(float)
    a = np.nanmin(v) if lo is None else float(lo)
    b = np.nanmax(v) if hi is None else float(hi)
    if not np.isfinite(a) or not np.isfinite(b) or b <= a:
        return np.ones_like(v)
    return np.clip((v - a) / (b - a), 0, 1)


def _mapping(spec, cols, n, kind: str):
    """colour / opacity / size: a constant, a field name/expression, or {field, min, max, scale, range}."""
    if spec is None or spec == "":
        return None, {}
    if isinstance(spec, (int, float)) and not isinstance(spec, bool):
        return np.full(n, float(spec)), {"constant": spec}
    d = {"field": spec} if isinstance(spec, str) else dict(spec)
    if kind == "color" and isinstance(d.get("field"), str) and d["field"].startswith("#"):
        return d["field"], {"constant": d["field"]}
    v = _col(cols, d.get("field"))
    if v.dtype.kind not in "fiub":  # categories -> codes
        cats, codes = np.unique(v.astype(str), return_inverse=True)
        return codes.astype(float), {"field": d.get("field"), "categories": cats.tolist()[:20]}
    v = v.astype(float)
    if d.get("scale") in ("log", "db"):
        v = np.log10(np.maximum(v, 1e-30))
    info = {"field": d.get("field"), "min": float(np.nanmin(v)) if np.isfinite(v).any() else None,
            "max": float(np.nanmax(v)) if np.isfinite(v).any() else None}
    if kind == "opacity":
        r = d.get("range") or [0.08, 1.0]
        return float(r[0]) + _norm(v, d.get("min"), d.get("max")) * (float(r[1]) - float(r[0])), info
    if kind == "size":
        r = d.get("range") or [4, 60]
        return float(r[0]) + _norm(v, d.get("min"), d.get("max")) * (float(r[1]) - float(r[0])), info
    return v, info | {"vmin": d.get("min"), "vmax": d.get("max")}


def _stats(name: str, v: np.ndarray) -> dict:
    if v.dtype.kind not in "fiub":
        u, c = np.unique(v.astype(str), return_counts=True)
        order = np.argsort(-c)[:8]
        return {"field": name, "categories": {str(u[i]): int(c[i]) for i in order}}
    f = v[np.isfinite(v)]
    if not len(f):
        return {"field": name, "valid": 0}
    return {"field": name, "valid": int(len(f)), "min": _r(np.min(f)), "max": _r(np.max(f)), "mean": _r(np.mean(f)),
            "median": _r(np.median(f)), "p5": _r(np.percentile(f, 5)), "p95": _r(np.percentile(f, 95))}


def _r(v) -> float:
    v = float(v)
    if v == 0 or not math.isfinite(v):
        return v
    return float(f"{v:.6g}")


def _decimate_xy(x: np.ndarray, y: np.ndarray, max_points: int, kind: str):
    """Keep plots fast without losing what matters: lines keep each chunk's min and max (short bursts survive),
    scatters are thinned evenly."""
    n = len(x)
    if n <= max_points:
        return x, y, 1
    if kind in ("line", "step"):
        k = int(math.ceil(n / (max_points / 2)))
        m = (n // k) * k
        xr, yr = x[:m].reshape(-1, k), y[:m].reshape(-1, k)
        imin, imax = np.nanargmin(np.nan_to_num(yr, nan=np.inf), axis=1), np.nanargmax(np.nan_to_num(yr, nan=-np.inf), axis=1)
        rows = np.arange(len(xr))
        first = np.minimum(imin, imax)
        second = np.maximum(imin, imax)
        xs = np.stack([xr[rows, first], xr[rows, second]], 1).ravel()
        ys = np.stack([yr[rows, first], yr[rows, second]], 1).ravel()
        return xs, ys, k
    idx = np.linspace(0, n - 1, max_points).astype(int)
    return x[idx], y[idx], int(math.ceil(n / max_points))


KINDS = ("line", "scatter", "step", "bar", "histogram", "hist2d", "spectrogram", "polar", "polar_hist",
         "constellation", "eye", "heatmap")


def render(cols: dict, kind: str, x=None, y=None, z=None, color=None, opacity=None, size=None, group_by: str = "",
           where: str = "", derive: dict | None = None, bins=60, polar: dict | None = None,
           samples_per_symbol: float = 0.0, marks: list | None = None, title: str = "", subtitle: str = "",
           width_px: int = 1100, height_px: int = 600, colormap: str = "viridis", max_points: int = 200000,
           spectrogram_data: dict | None = None, line_width: float = 1.0, marker: str = "o",
           legend: bool = True, grid: bool = True) -> tuple[bytes, dict]:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    kind = (kind or "line").lower()
    if kind not in KINDS:
        raise ValueError(f"kind '{kind}' unknown - use one of {KINDS}")
    for name, expr in (derive or {}).items():  # new columns (or single values) from expressions
        v = np.asarray(evaluate(str(expr), cols))
        if v.ndim == 0 or (v.ndim == 1 and len(v) != nrows(cols)):
            cols.setdefault(SCALARS, {})[str(name)] = float(v) if v.ndim == 0 else v.tolist()
        else:
            cols[str(name)] = v
    n_all = nrows(cols)
    if where:
        m = np.asarray(evaluate(where, cols)).astype(bool)
        if m.shape != (n_all,):
            raise ValueError(f"where '{where}' must give one true/false per row")
        cols = {k: (v if k == SCALARS else v[m]) for k, v in cols.items()}
    n = nrows(cols)
    x, y, z, polar = _numify_spec(x, cols), _numify_spec(y, cols), _numify_spec(z, cols), _numify_spec(polar, cols)
    color, opacity, size = _numify_spec(color, cols), _numify_spec(opacity, cols), _numify_spec(size, cols)
    if isinstance(polar, dict) and isinstance(polar.get("r"), dict):
        polar["r"] = _numify_spec(polar["r"], cols)
    marks = [{k: (_numify(v, cols) if k in ("x", "y", "theta") else v) for k, v in mk.items()}
             for mk in (marks or []) if isinstance(mk, dict)]
    summary: dict = {"kind": kind, "rows": n, "rows_before_filter": n_all}
    if cols.get(SCALARS):
        summary["values"] = {k: (_r(v) if isinstance(v, (int, float)) else v) for k, v in cols[SCALARS].items()}
    if n == 0 and kind != "spectrogram":
        raise ValueError("nothing to plot: no rows" + (f" after where='{where}'" if where else ""))

    plt.rcParams.update({"figure.facecolor": "#0f131b", "axes.facecolor": "#151a24", "axes.edgecolor": "#3a4152",
                         "axes.labelcolor": "#c9cfdb", "xtick.color": "#9aa3b5", "ytick.color": "#9aa3b5",
                         "text.color": "#e6e8ee", "grid.color": "#2a3140", "grid.alpha": 0.8, "font.size": 10.5,
                         "legend.facecolor": "#151a24", "legend.edgecolor": "#3a4152"})
    dpi = 100
    is_polar = kind in ("polar", "polar_hist")
    fig = plt.figure(figsize=(max(4, width_px / dpi), max(3, height_px / dpi)), dpi=dpi)
    ax = fig.add_subplot(111, projection="polar" if is_polar else None)
    xs, ys = _axis(x), _axis(y[0] if isinstance(y, list) and y else (None if isinstance(y, list) else y))
    cmap = plt.get_cmap(colormap)

    if kind == "spectrogram":
        sd = spectrogram_data or {}
        img = sd.get("db")
        if img is None:
            raise ValueError("spectrogram needs an IQ source (live or capture)")
        zs = _axis(z, "psd_db")
        vmin = zs.get("min", np.percentile(img, 5))
        vmax = zs.get("max", np.percentile(img, 99.7))
        t0, t1, f0, f1 = sd["t0"], sd["t1"], sd["f0"], sd["f1"]
        fscale = 1e6 if (xs.get("unit") or ys.get("unit") or "MHz").lower() == "mhz" else 1.0
        if (xs.get("field") or "time") in ("frequency", "freq", "offset_hz"):
            im = ax.imshow(img, aspect="auto", origin="lower", cmap=cmap, vmin=vmin, vmax=vmax,
                           extent=[f0 / fscale, f1 / fscale, t0, t1])
            ax.set_xlabel("frequency" + (" (MHz)" if fscale == 1e6 else " (Hz)"))
            ax.set_ylabel("time (s)")
        else:
            im = ax.imshow(img.T, aspect="auto", origin="lower", cmap=cmap, vmin=vmin, vmax=vmax,
                           extent=[t0, t1, f0 / fscale, f1 / fscale])
            ax.set_xlabel("time (s)")
            ax.set_ylabel("frequency" + (" (MHz)" if fscale == 1e6 else " (Hz)"))
        cb = fig.colorbar(im, ax=ax, pad=0.01)
        cb.set_label("power (dBFS)")
        _limits(ax, "x", xs)
        _limits(ax, "y", ys)
        summary["image"] = {"frames": int(img.shape[0]), "bins": int(img.shape[1]), "db_range": [_r(vmin), _r(vmax)]}
    elif is_polar:
        ps = dict(polar or {})
        th_field = ps.get("theta") or xs.get("field") or "bearing_deg"
        r_spec = _axis(ps.get("r") if ps.get("r") is not None else (y if y is not None else None), "range_km")
        th = _col(cols, th_field).astype(float)
        unit = str(ps.get("theta_unit") or ("rad" if str(th_field).endswith("rad") or th_field == "phase" else "deg"))
        thr = np.radians(th) if unit.startswith("deg") else th
        ax.set_theta_zero_location(str(ps.get("zero") or "N").upper()[:1] if str(ps.get("zero") or "N").upper()[:1]
                                   in "NESW" else "N")
        ax.set_theta_direction(-1 if str(ps.get("direction") or "clockwise").startswith("c") else 1)
        if kind == "polar_hist":
            nb = int(ps.get("sectors") or (bins if isinstance(bins, int) else 36))
            edges = np.linspace(0, 2 * np.pi, nb + 1)
            thr2 = np.mod(thr[np.isfinite(thr)], 2 * np.pi)
            stat = str(ps.get("stat") or ("count" if r_spec.get("field") in (None, "count") else "max"))
            if stat == "count" or r_spec.get("field") in (None, "count"):
                vals, _ = np.histogram(thr2, edges)
                rlab = "count"
            else:
                rv = _col(cols, r_spec["field"]).astype(float)[np.isfinite(thr)]
                idx = np.clip(np.digitize(thr2, edges) - 1, 0, nb - 1)
                fn = {"max": np.nanmax, "mean": np.nanmean, "median": np.nanmedian, "min": np.nanmin,
                      "p90": lambda a: np.nanpercentile(a, 90), "sum": np.nansum}.get(stat, np.nanmax)
                vals = np.array([fn(rv[idx == i]) if np.any(idx == i) else 0.0 for i in range(nb)])
                rlab = f"{stat} {r_spec['field']}"
            ax.bar(edges[:-1], vals, width=2 * np.pi / nb, align="edge",
                   color=cmap(_norm(vals)), alpha=float(opacity) if isinstance(opacity, (int, float)) else 0.85,
                   edgecolor="#0f131b", linewidth=0.5)
            sec_deg = 360 / nb
            summary["sectors"] = [{"from_deg": round(i * sec_deg, 1), "to_deg": round((i + 1) * sec_deg, 1),
                                   rlab: _r(vals[i])} for i in range(nb)]
            ax.set_title(rlab, fontsize=9, color="#9aa3b5", pad=18)
        else:
            rv = _col(cols, r_spec.get("field")).astype(float)
            rv_plot, sc = _apply_scale(ax, "y", r_spec, rv) if r_spec.get("scale") in ("db", "db20", "sqrt") else (rv, "linear")
            if r_spec.get("scale") == "log":
                ax.set_rscale("log")
            cv, cinfo = _mapping(color, cols, n, "color")
            av, ainfo = _mapping(opacity, cols, n, "opacity")
            sv, sinfo = _mapping(size, cols, n, "size")
            ok = np.isfinite(thr) & np.isfinite(rv_plot)
            kw = {"s": sv[ok] if isinstance(sv, np.ndarray) else 14, "marker": marker}
            if isinstance(cv, np.ndarray):
                kw.update(c=cv[ok], cmap=cmap, vmin=cinfo.get("vmin"), vmax=cinfo.get("vmax"))
            else:
                kw["color"] = cv or "#5ab0ff"
            sca = ax.scatter(thr[ok], rv_plot[ok], **kw)
            if isinstance(av, np.ndarray):
                sca.set_alpha(None)
                fc = sca.get_facecolors()
                if len(fc) == ok.sum():
                    fc[:, 3] = av[ok]
                    sca.set_facecolors(fc)
                else:
                    sca.set_alpha(float(np.nanmean(av)))
            if isinstance(cv, np.ndarray):
                fig.colorbar(sca, ax=ax, pad=0.08).set_label(str(cinfo.get("field")))
            if r_spec.get("min") is not None or r_spec.get("max") is not None:
                ax.set_rlim(r_spec.get("min"), r_spec.get("max"))
            summary["r"] = _stats(str(r_spec.get("field")), rv[ok])
            # coverage by direction: how far out each sector reaches (antenna pattern / obstructions)
            nb = int(ps.get("sectors") or 12)
            idx = np.clip((np.mod(np.degrees(thr[ok]), 360) // (360 / nb)).astype(int), 0, nb - 1)
            summary["by_sector"] = [{"from_deg": round(i * 360 / nb), "to_deg": round((i + 1) * 360 / nb),
                                     "points": int(np.sum(idx == i)),
                                     "max_r": _r(np.nanmax(rv[ok][idx == i])) if np.any(idx == i) else None,
                                     "median_r": _r(np.nanmedian(rv[ok][idx == i])) if np.any(idx == i) else None}
                                    for i in range(nb)]
            summary["mappings"] = {k: v for k, v in (("color", cinfo), ("opacity", ainfo), ("size", sinfo)) if v}
        if ps.get("r_label") or r_spec.get("label"):
            ax.set_ylabel(ps.get("r_label") or r_spec.get("label"), labelpad=24)
        summary["theta"] = {"field": th_field, "unit": unit, "zero": ps.get("zero") or "N",
                            "direction": ps.get("direction") or "clockwise"}
    elif kind == "eye":
        sps = float(samples_per_symbol)
        if sps < 2:
            raise ValueError("eye diagram needs samples_per_symbol >= 2 (sample rate / symbol rate)")
        yv = _col(cols, ys.get("field") or "inst_freq").astype(float)
        span = int(round(2 * sps))
        starts = np.arange(0, len(yv) - span, sps).astype(int)[:3000]
        segs = np.stack([yv[s:s + span] for s in starts]) if len(starts) else np.zeros((0, span))
        tt = np.arange(span) / sps
        alpha = float(opacity) if isinstance(opacity, (int, float)) else max(0.02, min(0.5, 30 / max(1, len(segs))))
        ax.plot(tt, segs.T, color="#5ab0ff", alpha=alpha, linewidth=line_width * 0.8)
        ax.set_xlabel("symbols")
        ax.set_ylabel(_label(ys, ys.get("field") or "inst_freq"))
        _limits(ax, "y", ys)
        summary["traces"] = int(len(segs))
        summary["y"] = _stats(str(ys.get("field") or "inst_freq"), yv)
    else:
        if kind == "constellation":
            xs = xs or {"field": "i"}
            ys = ys or {"field": "q"}
            xs.setdefault("field", "i")
            ys.setdefault("field", "q")
        if kind == "histogram":
            xv = _col(cols, xs.get("field") or ys.get("field") or "amplitude")
            xv_plot, _sc = _apply_scale(ax, "x", xs, xv.astype(float)) if xs.get("scale") in ("db", "db20", "sqrt") \
                else (xv.astype(float), xs.get("scale"))
            if xs.get("scale") == "log":
                ax.set_xscale("log")
                b = np.logspace(np.log10(max(np.nanmin(xv_plot[xv_plot > 0]), 1e-30)),
                                np.log10(np.nanmax(xv_plot)), int(bins) + 1) if np.any(xv_plot > 0) else int(bins)
            else:
                b = bins
            ok = np.isfinite(xv_plot)
            groups = [("", ok)]
            if group_by:
                gv = _col(cols, group_by).astype(str)
                groups = [(g, ok & (gv == g)) for g in np.unique(gv[ok])[:12]]
            for gname, gm in groups:
                ax.hist(xv_plot[gm], bins=b, alpha=float(opacity) if isinstance(opacity, (int, float)) else
                        (0.55 if len(groups) > 1 else 0.85), label=str(gname) or None,
                        color=None if len(groups) > 1 else "#5ab0ff", log=ys.get("scale") == "log")
            ax.set_xlabel(_label(xs, xs.get("field") or "value"))
            ax.set_ylabel("count")
            _limits(ax, "x", xs)
            summary["x"] = _stats(str(xs.get("field")), xv)
            if len(groups) > 1 and legend:
                ax.legend(fontsize=8.5)
        elif kind in ("hist2d", "heatmap"):
            xv = _col(cols, xs.get("field")).astype(float)
            yv = _col(cols, ys.get("field")).astype(float)
            xv, _ = _apply_scale(ax, "x", xs, xv) if xs.get("scale") in ("db", "db20", "sqrt") else (xv, None)
            yv, _ = _apply_scale(ax, "y", ys, yv) if ys.get("scale") in ("db", "db20", "sqrt") else (yv, None)
            ok = np.isfinite(xv) & np.isfinite(yv)
            wv = None
            if z is not None:
                wv = _col(cols, _axis(z).get("field")).astype(float)[ok]
            bx = bins if not isinstance(bins, (list, tuple)) else bins[0]
            by = bins if not isinstance(bins, (list, tuple)) else bins[1]
            from matplotlib.colors import LogNorm
            h = ax.hist2d(xv[ok], yv[ok], bins=[int(bx), int(by)], weights=wv, cmap=cmap,
                          norm=LogNorm() if _axis(z).get("scale") == "log" or z is None else None)
            fig.colorbar(h[3], ax=ax, pad=0.01).set_label("count" if z is None else str(_axis(z).get("field")))
            ax.set_xlabel(_label(xs, xs.get("field")))
            ax.set_ylabel(_label(ys, ys.get("field")))
            _limits(ax, "x", xs)
            _limits(ax, "y", ys)
            summary["x"] = _stats(str(xs.get("field")), xv[ok])
            summary["y"] = _stats(str(ys.get("field")), yv[ok])
        else:  # line / step / scatter / bar / constellation: one or more y series
            xv_raw = _col(cols, xs.get("field") or "index")
            yspecs = [(_axis(v) if not isinstance(v, str) else {"field": v}) for v in y] if isinstance(y, list) \
                else [ys if ys.get("field") else {"field": "amplitude"}]
            xv, _ = _apply_scale(ax, "x", xs, xv_raw.astype(float) if xv_raw.dtype.kind in "fiub" else xv_raw)
            cv, cinfo = _mapping(color, cols, n, "color")
            av, ainfo = _mapping(opacity, cols, n, "opacity")
            sv, sinfo = _mapping(size, cols, n, "size")
            groups = [("", np.ones(n, bool))]
            if group_by:
                gv = _col(cols, group_by).astype(str)
                uniq = np.unique(gv)
                groups = [(g, gv == g) for g in uniq[:20]]
                summary["groups"] = {str(g): int(np.sum(gv == g)) for g in uniq[:40]}
            ystats = []
            yscale = (yspecs[0].get("scale") or "linear")
            for yi, yspec in enumerate(yspecs):
                yv_raw = _col(cols, yspec.get("field")).astype(float)
                sp = dict(yspec)
                sp.setdefault("scale", yscale)
                yv, _ = _apply_scale(ax, "y", sp, yv_raw) if yi == 0 or sp.get("scale") in ("db", "db20", "sqrt") \
                    else (yv_raw, None)
                ystats.append(_stats(str(yspec.get("field")), yv_raw))
                for gi, (gname, gm) in enumerate(groups):
                    m = gm & np.isfinite(yv) & (np.isfinite(xv) if np.asarray(xv).dtype.kind == "f" else True)
                    label = " · ".join(s for s in (str(yspec.get("label") or yspec.get("field")) if len(yspecs) > 1 else "",
                                                   str(gname)) if s) or None
                    if kind in ("line", "step"):
                        px, py, dec = _decimate_xy(np.asarray(xv)[m], yv[m], max_points, kind)
                        summary["decimated_by"] = max(summary.get("decimated_by", 1), dec)
                        style = {"linewidth": line_width, "label": label,
                                 "alpha": float(opacity) if isinstance(opacity, (int, float)) else 0.95}
                        if isinstance(cv, str):
                            style["color"] = cv
                        if kind == "step":
                            ax.step(px, py, where="post", **style)
                        else:
                            ax.plot(px, py, **style)
                    elif kind == "bar":
                        ax.bar(np.asarray(xv)[m], yv[m], label=label,
                               alpha=float(opacity) if isinstance(opacity, (int, float)) else 0.85,
                               color=cv if isinstance(cv, str) else None)
                    else:  # scatter / constellation
                        idx = np.flatnonzero(m)
                        if len(idx) > max_points:
                            idx = idx[np.linspace(0, len(idx) - 1, max_points).astype(int)]
                            summary["decimated_by"] = int(math.ceil(m.sum() / max_points))
                        kw = {"s": sv[idx] if isinstance(sv, np.ndarray) else (4 if len(idx) > 20000 else 12),
                              "marker": marker, "label": label, "linewidths": 0}
                        if isinstance(cv, np.ndarray):
                            kw.update(c=cv[idx], cmap=cmap, vmin=cinfo.get("vmin"), vmax=cinfo.get("vmax"))
                        elif isinstance(cv, str):
                            kw["color"] = cv
                        elif len(groups) == 1 and len(yspecs) == 1:
                            kw["color"] = "#5ab0ff"
                        if not isinstance(av, np.ndarray):
                            kw["alpha"] = float(opacity) if isinstance(opacity, (int, float)) else \
                                (0.25 if len(idx) > 20000 else 0.8 if len(idx) > 2000 else 1.0)
                        sca = ax.scatter(np.asarray(xv)[idx], yv[idx], **kw)
                        if isinstance(av, np.ndarray):
                            fc = sca.get_facecolors()
                            if len(fc) == len(idx):
                                fc[:, 3] = av[idx]
                                sca.set_facecolors(fc)
                        if isinstance(cv, np.ndarray) and yi == 0 and gi == 0:
                            cb = fig.colorbar(sca, ax=ax, pad=0.01)
                            cb.set_label(str(cinfo.get("field")))
                            if cinfo.get("categories"):
                                cb.set_ticks(range(len(cinfo["categories"])))
                                cb.set_ticklabels(cinfo["categories"])
            ax.set_xlabel(_label(xs, xs.get("field") or "index"))
            ax.set_ylabel(_label(yspecs[0], yspecs[0].get("field")) if len(yspecs) == 1 else
                          (yspecs[0].get("label") or ", ".join(str(s.get("field")) for s in yspecs)))
            _limits(ax, "x", xs)
            _limits(ax, "y", yspecs[0])
            if kind == "constellation":
                ax.set_aspect("equal", adjustable="datalim")
                ax.axhline(0, color="#3a4152", linewidth=0.8)
                ax.axvline(0, color="#3a4152", linewidth=0.8)
            if legend and (len(yspecs) > 1 or len(groups) > 1):
                ax.legend(fontsize=8.5, markerscale=2, loc="best")
            summary["x"] = _stats(str(xs.get("field") or "index"),
                                  xv_raw.astype(float) if xv_raw.dtype.kind in "fiub" else xv_raw)
            summary["y"] = ystats if len(ystats) > 1 else ystats[0]
            summary["mappings"] = {k: v for k, v in (("color", cinfo), ("opacity", ainfo), ("size", sinfo)) if v}
    for mk in marks or []:  # reference lines / labels
        try:
            if "x" in mk and not is_polar:
                ax.axvline(float(mk["x"]), color=mk.get("color", "#ffb347"), linestyle="--", linewidth=1)
                if mk.get("label"):
                    ax.annotate(str(mk["label"]), (float(mk["x"]), 1), xycoords=("data", "axes fraction"),
                                xytext=(3, -12), textcoords="offset points", color=mk.get("color", "#ffb347"),
                                fontsize=8.5)
            if "y" in mk and not is_polar:
                ax.axhline(float(mk["y"]), color=mk.get("color", "#ffb347"), linestyle="--", linewidth=1)
                if mk.get("label"):
                    ax.annotate(str(mk["label"]), (0, float(mk["y"])), xycoords=("axes fraction", "data"),
                                xytext=(4, 3), textcoords="offset points", color=mk.get("color", "#ffb347"),
                                fontsize=8.5)
            if "theta" in mk and is_polar:
                th = math.radians(float(mk["theta"]))
                ax.plot([th, th], [0, ax.get_rmax()], color=mk.get("color", "#ffb347"), linestyle="--", linewidth=1)
        except (TypeError, ValueError, KeyError):
            continue
    if not is_polar and kind != "spectrogram":
        for axis in ("x", "y"):  # show real values (2402.015), not '+2.402e3' offsets
            try:
                ax.ticklabel_format(axis=axis, useOffset=False, style="plain")
            except (AttributeError, ValueError):
                pass
    if grid:
        ax.grid(True)
    if title or subtitle:
        fig.suptitle(title or "", fontsize=12.5, color="#e6e8ee", x=0.01, ha="left")
        if subtitle:
            fig.text(0.01, 0.935, subtitle, fontsize=9, color="#8a91a3", ha="left")
    fig.tight_layout(rect=(0, 0, 1, 0.93 if (title or subtitle) else 1))
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=dpi, facecolor=fig.get_facecolor())
    plt.close(fig)
    return buf.getvalue(), summary


def spectrogram_grid(x: np.ndarray, fs: float, fc: float, fft_size: int = 512, max_frames: int = 1500,
                     overlap: float = 0.5) -> dict:
    n = int(fft_size)
    hop = max(1, int(n * (1 - min(max(overlap, 0.0), 0.95))))
    frames = max(1, (len(x) - n) // hop + 1)
    step = max(1, int(math.ceil(frames / max_frames)))
    w = np.hanning(n)
    starts = np.arange(0, frames, step) * hop
    P = np.empty((len(starts), n), np.float32)
    for i, s in enumerate(starts):  # frames are reduced by peak-holding over each step: short bursts stay
        seg = [x[s + j * hop: s + j * hop + n] for j in range(step) if s + j * hop + n <= len(x)]
        F = np.abs(np.fft.fftshift(np.fft.fft(np.stack(seg) * w, axis=1), axes=1)) ** 2 / float(w.sum() ** 2)
        P[i] = F.max(axis=0)
    db = 10 * np.log10(np.maximum(P, 1e-20))
    return {"db": db, "t0": 0.0, "t1": len(x) / fs, "f0": fc - fs / 2, "f1": fc + fs / 2}


def export_csv(cols: dict, path: str, fields: list[str] | None = None, max_rows: int = 2_000_000) -> int:
    keys = [k for k in (fields or list(cols)) if k in cols and k != SCALARS]
    n = min(len(cols[keys[0]]), max_rows) if keys else 0
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(keys)
        for i in range(n):
            w.writerow([cols[k][i] for k in keys])
    return n


# ------------------------------------------------------------------ the AI's own variables
# Saved definitions (state/analysis_vars.json, shared by every SDR Pilot copy) that are added to every table the
# plot tool builds - and, for plain numbers, offered to flowgraphs as variables:
#   formula: an expression over the table's columns/values ("freq_dev / 1e3", "snr_db > 10", "fs / baud")
#   python:  code defining  compute(cols, values)  that returns an array (one value per row) or a single number;
#            numpy (np), math and scipy are available.
VARS_PATH: str | None = None
_PY_CACHE: dict = {}


def load_vars() -> dict:
    if not VARS_PATH or not os.path.exists(VARS_PATH):
        return {}
    try:
        with open(VARS_PATH, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def save_vars(d: dict) -> None:
    if not VARS_PATH:
        raise RuntimeError("no place to save variables")
    os.makedirs(os.path.dirname(VARS_PATH), exist_ok=True)
    tmp = VARS_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, indent=2)
    os.replace(tmp, VARS_PATH)


def check_formula(expr: str) -> None:
    tree = ast.parse(str(expr).replace("&&", " and ").replace("||", " or "), mode="eval")
    for node in ast.walk(tree):
        if not isinstance(node, _OK_NODES):
            raise ValueError(f"{type(node).__name__} isn't allowed in a formula (use python= for real code)")
        if isinstance(node, ast.Call) and not (isinstance(node.func, ast.Name) and node.func.id in _FUNCS):
            raise ValueError(f"unknown function in formula - available: {sorted(_FUNCS)}")


def _py_fn(name: str, code: str):
    key = (name, hash(code))
    if key not in _PY_CACHE:
        ns: dict = {"np": np, "numpy": np, "math": math}
        try:
            import scipy  # noqa: F401
            import scipy.signal  # noqa: F401
            ns["scipy"] = scipy
        except ImportError:
            pass
        exec(compile(code, f"<variable {name}>", "exec"), ns)  # noqa: S102 - the AI's own analysis code, like epy blocks
        fn = ns.get("compute")
        if not callable(fn):
            raise ValueError("python code must define compute(cols, values)")
        _PY_CACHE[key] = fn
    return _PY_CACHE[key]


def apply_vars(cols: dict, only: list[str] | None = None, base: dict | None = None) -> tuple[list[str], dict]:
    """Add every saved variable that can be computed from this table (base: extra single values such as the radio's
    centre frequency, used where the table has none of its own). Returns (added, {name: why skipped})."""
    added, skipped = [], {}
    if base:
        cols[SCALARS] = {**base, **(cols.get(SCALARS) or {})}
    n = nrows(cols)
    for name, d in load_vars().items():
        if only is not None and name not in only:
            continue
        try:
            if d.get("python"):
                vals = dict(cols.get(SCALARS) or {})
                v = _py_fn(name, d["python"])({k: c for k, c in cols.items() if k != SCALARS}, vals)
            else:
                v = evaluate(d.get("formula", ""), cols)
            v = np.asarray(v)
            if v.ndim == 0:
                cols.setdefault(SCALARS, {})[name] = float(v) if v.dtype.kind in "fiub" else v.item()
            elif v.ndim == 1 and len(v) == n:
                cols[name] = v
            else:
                skipped[name] = f"gave {v.shape}, not one value per row ({n}) or a single number"
                continue
            added.append(name)
        except Exception as e:  # missing inputs for this kind of data is normal: just not available here
            skipped[name] = f"{type(e).__name__}: {str(e)[:160]}"
    return added, skipped


def flowgraph_values(names_used: set[str]) -> dict:
    """Saved variables that are plain numbers (formulas over numbers / other such variables), for flowgraphs."""
    out: dict = {}
    defs = load_vars()
    for name, d in defs.items():
        if name not in names_used or d.get("python"):
            continue
        try:
            v = np.asarray(evaluate(d.get("formula", ""), {SCALARS: dict(out)}))
            if v.ndim == 0:
                out[name] = float(v)
        except Exception:
            continue
    return out
