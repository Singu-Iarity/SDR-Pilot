"""GNU Radio Companion integration: block catalog, flowgraph builder/validator,
.grc save/load and Python generation - all through GRC's own core library so
the files open cleanly in GNU Radio Companion."""
from __future__ import annotations

import json
import os
import re
import threading
import time
from typing import Any

_PLATFORM = None
_PLATFORM_LOCK = threading.Lock()
GRC_LOCK = threading.RLock()  # GRC core isn't thread-safe: builds and dashboard renders take turns
_DESCRIBE_CACHE: dict = {}

COMMON_PARAMS = {"id", "alias", "affinity", "minoutbuf", "maxoutbuf", "comment"}

PSEUDO_BLOCKS = {
    "sdr_source": "Live IQ from the radio (SDRangel via SDR Pilot's ZeroMQ fan-out). Complex output at samp_rate, "
                  "centred on center_freq. No parameters needed.",
    "capture_source": "Play back a capture file (complex64 .cfile). Params: path (required), repeat (default False). "
                      "samp_rate/center_freq variables are taken from the capture's .json sidecar.",
}


# one-liners for common blocks whose GNU Radio docstring is empty or unhelpful
BLOCK_SUMMARIES = {
    "blocks_multiply_const_vxx": "Multiplies every sample by a constant (gain / scaling).",
    "blocks_multiply_xx": "Multiplies its inputs together sample by sample (mixing, gating).",
    "blocks_add_xx": "Adds its inputs together sample by sample.",
    "blocks_add_const_vxx": "Adds a constant to every sample (offset).",
    "blocks_throttle": "Limits the sample rate when nothing else sets the pace (files, simulations).",
    "blocks_throttle2": "Limits the sample rate when nothing else sets the pace (files, simulations).",
    "blocks_file_sink": "Writes the raw samples to a file.",
    "blocks_file_source": "Reads raw samples from a file.",
    "blocks_message_debug": "Prints (or stores) every message it receives - handy to see decoder output in the log.",
    "blocks_complex_to_mag": "Magnitude (envelope) of complex samples.",
    "blocks_complex_to_real": "Real (I) part of complex samples.",
    "blocks_float_to_complex": "Combines real I and Q streams into complex samples.",
    "blocks_keep_one_in_n": "Keeps one sample in every n (crude decimation, no filtering).",
    "analog_quadrature_demod_cf": "FM demodulator: turns complex IQ into the instantaneous frequency (audio / data).",
    "analog_wfm_rcv": "Broadcast (wideband) FM receiver: demodulates and de-emphasises to mono audio.",
    "analog_nbfm_rx": "Narrowband FM receiver (two-way radio): demodulates to audio.",
    "freq_xlating_fir_filter_xxx": "Shifts a channel to 0 Hz, filters it and decimates - picks one signal out of the band.",
    "low_pass_filter": "Low-pass filter: keeps frequencies below the cutoff (optionally decimating).",
    "band_pass_filter": "Band-pass filter: keeps only the frequencies between the two cutoffs.",
    "rational_resampler_xxx": "Changes the sample rate by a ratio interpolation/decimation.",
    "mmse_resampler_xx": "Changes the sample rate by any (non-integer) ratio.",
    "zeromq_sub_source": "Receives samples over ZeroMQ.",
    "epy_block": "A Python block written for this flowgraph.",
}


_LIB_SIG = {"sig": None, "t": 0.0}


def _block_dirs_signature(p) -> tuple:
    """Cheap fingerprint of GRC's block folders: number of .yml files and the newest change."""
    n, newest = 0, 0.0
    try:
        paths = list(p.config.block_paths)
    except Exception:
        paths = []
    for d in paths:
        try:
            for e in os.scandir(d):
                if e.name.endswith(".yml"):
                    n += 1
                    try:
                        newest = max(newest, e.stat().st_mtime)
                    except OSError:
                        pass
        except OSError:
            continue
    return n, round(newest, 1)


def refresh_if_changed(force: bool = False) -> list[str]:
    """Reload GRC's block library when blocks were installed or changed since it was built (an OOT module installed
    by another chat, by hand, or with conda). Checked at most every few seconds. Returns the new block ids."""
    import time as _t
    p = platform()
    if not force and _t.time() - _LIB_SIG["t"] < 5:
        return []
    _LIB_SIG["t"] = _t.time()
    sig = _block_dirs_signature(p)
    if _LIB_SIG["sig"] is None:
        _LIB_SIG["sig"] = sig
        if not force:
            return []
    if sig == _LIB_SIG["sig"] and not force:
        return []
    before = set(p.blocks)
    with GRC_LOCK:
        p.build_library()
        _DESCRIBE_CACHE.clear()
    _LIB_SIG["sig"] = sig
    return sorted(set(p.blocks) - before)


def platform():
    global _PLATFORM
    with _PLATFORM_LOCK:
        if _PLATFORM is None:
            import sys
            from gnuradio import gr
            from gnuradio.grc.core import Messages
            from gnuradio.grc.core.platform import Platform
            # GRC registers sys.stdout as a messenger - that is the MCP channel. Send to stderr instead.
            import logging
            _glog = logging.getLogger("sdrpilot.grc")

            def _msg(text):
                sys.stderr.write(text)
                if text.strip():
                    _glog.info(text.strip())

            Messages.MESSENGERS_LIST[:] = [_msg]
            kwargs = dict(name="SDR Pilot", prefs=gr.prefs(), version=gr.version(),
                          version_parts=(gr.major_version(), gr.api_version(), gr.minor_version()))
            prefix = os.environ.get("SDRPILOT_GRC_PREFIX") or gr.prefix()
            try:
                # newer GRC (incl. conda-forge's GNU Radio 3.10.12 build) requires install_prefix
                p = Platform(install_prefix=prefix, **kwargs)
            except TypeError:
                p = Platform(**kwargs)  # older GRC (3.10.9 and earlier) doesn't accept it
            p.build_library()
            _PLATFORM = p
        return _PLATFORM


# ------------------------------------------------------------------ catalog

def _doc(block_cls) -> str:
    d = getattr(block_cls, "documentation", None) or {}
    if isinstance(d, dict):
        txt = "\n".join(v for v in d.values() if isinstance(v, str) and v.strip())
    else:
        txt = str(d)
    return txt.strip()


def search_blocks(query: str, limit: int = 25) -> list[dict]:
    try:
        refresh_if_changed()
    except Exception:
        pass
    p = platform()
    words = [w for w in re.split(r"\s+", query.lower().strip()) if w]
    results = []
    for key, B in p.blocks.items():
        if key in ("options",) or getattr(B, "is_deprecated", False):
            pass
        label = str(B.label)
        cat = "/".join(B.category) if isinstance(B.category, (list, tuple)) else str(B.category)
        hay_main = f"{key} {label}".lower()
        hay_all = f"{hay_main} {cat.lower()} {_doc(B)[:400].lower()}"
        if not words:
            score = 1
        else:
            if not all(w in hay_all for w in words):
                continue
            score = sum(3 if w in hay_main else 1 for w in words)
            if query.lower().strip() == key:
                score += 10
            # a module's own blocks ('lte' -> lte_pss_sync...) before words that merely contain it ('filter')
            score += sum(6 for w in words if key.lower().startswith(w + "_") or
                         re.search(rf"(^|[^a-z0-9]){re.escape(w)}([^a-z0-9]|$)", hay_main))
        results.append((score, {"type": key, "label": label, "category": cat}))
    results.sort(key=lambda r: (-r[0], r[1]["type"]))
    out = [r[1] for r in results[:limit]]
    for name, desc in PSEUDO_BLOCKS.items():
        if not words or any(w in (name + " " + desc).lower() for w in words):
            out.insert(0, {"type": name, "label": "SDR Pilot: " + name, "category": "SDR Pilot", "note": desc})
    return out[:limit]


def similar(block_type: str, n: int = 8) -> list[str]:
    import difflib
    keys = list(platform().blocks.keys())
    out = difflib.get_close_matches(block_type, keys, n=n, cutoff=0.5)
    parts = [w for w in block_type.split("_") if len(w) > 2]
    for k in keys:
        if len(out) >= n:
            break
        if k not in out and parts and sum(w in k for w in parts) >= max(1, len(parts) - 1):
            out.append(k)
    return out


# SDR Pilot is receive-only: radio hardware sinks (transmitters) are refused everywhere
TX_BLOCK_RE = re.compile(r"^(osmosdr_sink|uhd_usrp_sink|uhd_rfnoc_tx.*|soapy_.*sink|iio_.*sink|limesdr_sink|"
                         r"bladerf_sink|hackrf_sink|plutosdr_sink|fmcomms.*sink|.*_tx_sink)$", re.I)
TX_CODE_RE = re.compile(r"osmosdr\.sink|soapy\.sink|soapy\.\w+_sink|uhd\.usrp_sink|iio\.\w*sink|limesdr\.sink|"
                        r"bladeRF\.sink|hackrf_device_list|hackrf_start_tx|SoapySDR.*SOAPY_SDR_TX", re.I)


def is_tx_block(btype: str) -> bool:
    return bool(TX_BLOCK_RE.match(str(btype or "")))


def _reload_if_new_hier(p, btype: str) -> bool:
    """A reusable block made by another SDR Pilot copy (another chat) since this one loaded the library."""
    try:
        d = p.config.hier_block_lib_dir
        if os.path.exists(os.path.join(d, f"{btype}.block.yml")):
            p.build_library()
            _DESCRIBE_CACHE.clear()
            return btype in p.blocks
        # ...or any block installed since the library was loaded (an OOT module installed by another chat / conda)
        refresh_if_changed()
        return btype in p.blocks
    except Exception:
        pass
    return False


def block_info(block_type: str) -> dict:
    if block_type in PSEUDO_BLOCKS:
        return {"type": block_type, "description": PSEUDO_BLOCKS[block_type],
                "outputs": [{"port": "0", "dtype": "complex"}]}
    p = platform()
    B = p.blocks.get(block_type)
    if B is None and _reload_if_new_hier(p, block_type):
        B = p.blocks.get(block_type)
    if B is None:
        raise KeyError(f"Unknown block type '{block_type}'. Similar: {similar(block_type)}")
    params = []
    for pd in B.parameters_data:
        if pd.get("id") in COMMON_PARAMS:
            continue
        e = {"id": pd.get("id"), "label": pd.get("name") or pd.get("label"), "dtype": pd.get("dtype", "raw"),
             "default": pd.get("default", "")}
        if pd.get("options"):
            e["options"] = pd["options"]
            if e["default"] in ("", None):
                e["default"] = pd["options"][0]
            if pd.get("option_labels"):
                e["option_labels"] = pd["option_labels"]
        if pd.get("hide") == "all":
            e["hidden"] = True
        params.append(e)

    def ports(data):
        return [{k: v for k, v in d.items() if k in ("id", "label", "domain", "dtype", "vlen", "optional", "multiplicity")}
                for d in data]

    t = B.templates or {}
    return {
        "type": block_type, "label": str(B.label),
        "category": "/".join(B.category) if isinstance(B.category, (list, tuple)) else str(B.category),
        "parameters": params,
        "inputs": ports(B.inputs_data),
        "outputs": ports(B.outputs_data),
        "python": {"imports": t.get("imports", ""), "make": t.get("make", ""), "callbacks": t.get("callbacks", [])},
        "documentation": _doc(B)[:2500],
        "note": "Port dtypes like ${type} follow the block's 'type' parameter. Message ports have domain 'message'.",
    }


# ------------------------------------------------------------------ build

def sanitize(name: str) -> str:
    s = re.sub(r"[^A-Za-z0-9_]", "_", name.strip())
    if not s or not s[0].isalpha():
        s = "fg_" + s
    return s[:60]


def _val(v: Any) -> str:
    if isinstance(v, bool):
        return "True" if v else "False"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, (list, tuple, dict)):
        return repr(v)
    return str(v)


def _epy_defaults(src: str) -> dict:
    """Default values of the epy block class's __init__ arguments, as GRC expressions."""
    import ast
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "blk":
            for fn in node.body:
                if isinstance(fn, ast.FunctionDef) and fn.name == "__init__":
                    args = fn.args.args[1:]
                    defs = fn.args.defaults
                    out = {}
                    for a, d in zip(args[len(args) - len(defs):], defs):
                        try:
                            out[a.arg] = ast.unparse(d)
                        except Exception:
                            pass
                    return out
    return {}


# ------------------------------------------------------------------ connections
# Models write connections every which way: "src:0 -> fm:0", "src.out0 -> fm.in0", "src[0] => fm", "src -> fm -> wav",
# ["src", 0, "fm", 0], {"from": "src", "to": "fm"} ... All of them mean the same thing, so all of them are accepted.

_ARROW_RE = re.compile(r"\s*(?:-+>|=+>|\u2192|\u27f6|\u21d2|\s+to\s+)\s*")
_PORT_RE = re.compile(r"(?:out(?:put)?|in(?:put)?|o|i|port|p|src|source|sink|stream|ch|chan)?[\s_\-]*(\d+)", re.I)
_DEFAULT_PORTS = {"", "out", "in", "output", "input", "o", "i", "port", "default"}


def split_conns(c) -> list[tuple[str, str]]:
    """One connection (any common notation; a chain "a -> b -> c" gives several) -> [(from_endpoint, to_endpoint)]."""
    if isinstance(c, dict):
        def get(*ks):
            return next((c[k] for k in ks if c.get(k) not in (None, "")), None)
        a = get("from", "src", "source", "from_block", "src_block", "source_block", "output", "out")
        b = get("to", "dst", "sink", "dest", "to_block", "dst_block", "sink_block", "dest_block", "input", "in")
        ap = get("from_port", "src_port", "source_port", "out_port", "output_port")
        bp = get("to_port", "dst_port", "sink_port", "dest_port", "in_port", "input_port")
        if a is None or b is None:
            raise ValueError(f"bad connection {c!r}: needs 'from' and 'to'")
        return [(f"{a}:{ap}" if ap is not None else str(a), f"{b}:{bp}" if bp is not None else str(b))]
    if isinstance(c, (list, tuple)):
        if len(c) == 4:
            return [(f"{c[0]}:{c[1]}", f"{c[2]}:{c[3]}")]
        if len(c) == 2:
            return [(str(c[0]), str(c[1]))]
        if len(c) > 2 and all(isinstance(x, str) for x in c):  # a chain of block names
            return [(str(c[i]), str(c[i + 1])) for i in range(len(c) - 1)]
        raise ValueError(f"bad connection {c!r}; write 'src:0 -> dst:0'")
    s = str(c).strip()
    parts = [x.strip() for x in _ARROW_RE.split(s) if x.strip()]
    if len(parts) < 2:
        parts = [x for x in re.split(r"\s*[,;]\s*|\s+", s) if x]
    if len(parts) < 2:
        raise ValueError(f"bad connection {c!r}; write 'src:0 -> dst:0' (block:port -> block:port)")
    return [(parts[i], parts[i + 1]) for i in range(len(parts) - 1)]


def norm_port(p) -> str:
    """'out0', 'in_1', 'port2', '[3]', 3 -> the digit; '' -> ''; message port names stay as they are."""
    p = str(p if p is not None else "").strip().strip("[]() :.'\"")
    m = _PORT_RE.fullmatch(p)
    return m.group(1) if m else p


def parse_endpoint(s, known=None, types: dict | None = None) -> tuple[str, str]:
    """'fm.out1' / 'fm:1' / 'fm[1]' / 'fm out1' / 'fm' -> ('fm', port). known: block names in the flowgraph (the
    longest matching name wins, so 'fm_rx.out0' isn't read as block 'fm'). types: {block name: type} - an endpoint
    that names a block TYPE used by exactly one block (e.g. 'sdr_source' for a block called 'src') maps to it."""
    s = str(s).strip().strip("'\"` ")
    names = [str(n) for n in (known or [])]
    if names:
        low = {n.lower(): n for n in names}
        if s.lower() in low:
            return low[s.lower()], ""
        for n in sorted(names, key=len, reverse=True):
            if s.lower().startswith(n.lower()) and len(s) > len(n):
                rest = s[len(n):]
                if rest[0] in ".:[(/ " or (rest[0] in "_-" and _PORT_RE.fullmatch(rest[1:].strip())):
                    return n, rest[1:].strip().strip("[]()")
    blk, port = s, ""
    m = re.fullmatch(r"(.+?)\s*\[\s*(\w+)\s*\]", s)
    if m:
        blk, port = m.group(1), m.group(2)
    elif ":" in s:
        blk, port = s.rsplit(":", 1)
    elif "." in s:
        blk, port = s.rsplit(".", 1)
    elif " " in s.strip():
        blk, port = s.strip().rsplit(" ", 1)
    blk, port = blk.strip(), port.strip()
    if names and blk not in names:
        hit = {n.lower(): n for n in names}.get(blk.lower())
        if hit:
            blk = hit
        elif types:
            by_type = [n for n, t in types.items() if t and str(t).lower() == blk.lower()]
            if len(by_type) == 1:
                blk = by_type[0]
    return blk, port


def _find_port(block, port: str, direction: str, prefer_domain: str | None = None):
    ports = list(block.sources if direction == "out" else block.sinks)
    raw = str(port if port is not None else "").strip()
    for pt in ports:
        if str(pt.key) == raw:
            return pt
    for pt in ports:
        if str(pt.key).lower() == raw.lower():
            return pt
    n = norm_port(raw)
    stream = [pt for pt in ports if pt.domain == "stream"]
    msg = [pt for pt in ports if pt.domain != "stream"]
    pool = (msg or stream) if prefer_domain and prefer_domain != "stream" else (stream or msg)
    if n.isdigit() and int(n) < len(pool):
        return pool[int(n)]
    if n.lower() in _DEFAULT_PORTS and pool:
        return pool[0]
    avail = [f"{pt.key}({pt.domain},{pt.dtype})" for pt in ports]
    raise ValueError(f"block '{block.name}' has no {'output' if direction == 'out' else 'input'} port '{port}'. "
                     f"Available: {avail or 'none - it has no ' + ('outputs' if direction == 'out' else 'inputs')}")


def canon_conn(c, known=None, types=None) -> list[tuple[str, str, str, str]]:
    """Normalised (from_block, from_port, to_block, to_port) tuples for comparing connections; ports '' -> '0'."""
    out = []
    for a, b in split_conns(c):
        sb, sp = parse_endpoint(a, known, types)
        db, dp = parse_endpoint(b, known, types)
        sp, dp = norm_port(sp), norm_port(dp)
        out.append((sb, "0" if sp.lower() in _DEFAULT_PORTS else sp, db, "0" if dp.lower() in _DEFAULT_PORTS else dp))
    return out


# parameter names models reach for that mean a real GRC parameter
PARAM_ALIASES = {"path": "file", "filename": "file", "file_name": "file", "file_path": "file", "filepath": "file",
                 "output_file": "file", "outfile": "file", "sample_rate": "samp_rate", "samplerate": "samp_rate",
                 "fs": "samp_rate", "rate": "samp_rate", "sr": "samp_rate", "freq": "frequency",
                 "center_freq": "freq", "decimation": "decim", "interpolation": "interp", "taps": "taps",
                 "num_channels": "nchan", "channels": "nchan", "data_type": "type", "dtype": "type",
                 "vector_length": "vlen", "cutoff": "cutoff_freq", "transition": "transition_width",
                 "transition_bw": "transition_width", "center_frequency": "center_freq", "offset_hz": "offset",
                 "frequency_offset": "offset", "freq_offset": "offset"}


def match_param(key: str, valid) -> str | None:
    """The real parameter a near-miss name means ('path' -> 'file', 'Samp_Rate' -> 'samp_rate', 'sample_rate' ->
    'samp_rate'), or None when it's ambiguous or nothing is close."""
    valid = [v for v in valid if v not in COMMON_PARAMS]
    if key in valid:
        return key
    squash = lambda t: re.sub(r"[^a-z0-9]", "", str(t).lower())
    hits = [v for v in valid if squash(v) == squash(key)]
    if len(hits) == 1:
        return hits[0]
    a = PARAM_ALIASES.get(key.lower())
    if a and a in valid:
        return a
    import difflib
    close = difflib.get_close_matches(key, valid, n=2, cutoff=0.82)
    return close[0] if len(close) == 1 else None


OPEN_PORT_MSG = "Port is not connected."
# set by the server: which block types are one-block receivers from OOT modules that haven't been justified
blocked_types = lambda types: []  # noqa: E731


def build_flowgraph(spec: dict, out_dir: str, radio_state: dict | None = None,
                    zmq_address: str = "tcp://127.0.0.1:5600", draft: bool = False,
                    history: list | None = None) -> dict:
    with GRC_LOCK:
        return _build_flowgraph(spec, out_dir, radio_state, zmq_address, draft, history)


def resolve_type(btype: str) -> str:
    """Map the block names models invent from module names to the real GRC block id:
    'gr-adsb::adsb_demod', 'adsb::demod', 'adsb.demod', 'gnuradio.adsb.demod', 'ADSB_Demod' -> 'adsb_demod'."""
    if not btype or btype in ("sdr_source", "capture_source"):
        return btype
    p = platform()
    if btype in p.blocks:
        return btype
    t = btype.strip()
    parts = [x for x in re.split(r"::|/|:|\.|\s+", t) if x]
    cands = [t, parts[-1] if parts else t, "_".join(parts)]
    if len(parts) >= 2:
        cands.append(parts[-2] + "_" + parts[-1])
    more = []
    for c in cands:
        c2 = c.lower().replace("-", "_")
        more += [c2, re.sub(r"^(gnuradio_|gr_)", "", c2)]
        if c2.startswith("gr_"):
            more.append(c2[3:])
    for c in cands + more:
        c = re.sub(r"^(gnuradio_|gr_)", "", c) if c not in p.blocks else c
        if c in p.blocks:
            return c
    return btype


def check_block(btype: str, params: dict | None) -> list[str]:
    """Quick checks before adding one block: does the type exist, are the parameter names real."""
    if btype in ("sdr_source", "capture_source"):
        return []
    if is_tx_block(btype):
        return [f"{btype} is a transmitter. SDR Pilot is receive-only - transmit blocks are not allowed"]
    p = platform()
    if btype not in p.blocks:
        _reload_if_new_hier(p, btype)
    if btype not in p.blocks:
        return [f"unknown block type '{btype}'. Similar: {similar(btype)}"]
    if btype == "epy_block":
        return []
    try:
        valid = {pd["id"] for pd in p.blocks[btype].parameters_data}
    except Exception:
        return []
    bad = [k for k in (params or {}) if k not in valid and k not in COMMON_PARAMS]
    if bad:
        return [f"{btype} has no parameter(s) {bad}. Valid: {sorted(x for x in valid if x not in COMMON_PARAMS)}"]
    return []


def _build_flowgraph(spec: dict, out_dir: str, radio_state: dict | None = None,
                     zmq_address: str = "tcp://127.0.0.1:5600", draft: bool = False,
                     history: list | None = None) -> dict:
    """spec = {name, title?, description?, gui?, variables{}, imports[], blocks[{name,type,params,note?}], connections[]}
    draft=True: a flowgraph still being built block by block - unconnected ports are reported as open_ports
    instead of errors."""
    p = platform()
    name = sanitize(spec.get("name") or "flowgraph")
    folder = os.path.join(out_dir, name)
    os.makedirs(folder, exist_ok=True)
    fg = p.make_flow_graph()
    opt = fg.options_block
    gui = bool(spec.get("gui"))
    opt.params["id"].set_value(name)
    opt.params["title"].set_value(spec.get("title") or name)
    if "description" in opt.params:
        opt.params["description"].set_value((spec.get("description") or "")[:400])
    if "author" in opt.params:
        opt.params["author"].set_value("SDR Pilot (AI-built)")
    hier = spec.get("hier")
    opt.params["generate_options"].set_value("hb" if hier else "qt_gui" if gui else "no_gui")
    if hier and "category" in opt.params:
        opt.params["category"].set_value(f"[{hier.get('category') or 'SDR Pilot'}]")
    if "run_options" in opt.params:
        opt.params["run_options"].set_value("prompt" if gui else "run")

    fg.rewrite()  # evaluate the options block first (embedded python module names use the flowgraph id)
    notes: list[str] = []
    variables = dict(spec.get("variables") or {})
    blocks_spec = list(spec.get("blocks") or [])
    uses_live = False
    capture_meta = None
    expanded = []
    src_names: set = set()
    explicit: dict[str, set] = {}  # params the AI set itself, per block (the rest SDR Pilot may adapt)
    for b in blocks_spec:
        btype = b.get("type")
        if btype in ("sdr_source", "capture_source"):
            src_names.add(b.get("name"))
        params = dict(b.get("params") or {})
        if btype in ("sdr_source", "capture_source") and hier:
            raise ValueError(f"'{b.get('name')}': a reusable block can't contain {btype}; give it an input port "
                             f"instead (inputs=[{{'to': '<first block>:0', 'type': 'complex'}}])")
        if btype in ("sdr_source",) and params:
            notes.append(f"'{b.get('name')}': sdr_source takes no parameters (rate and frequency come from the radio - "
                         f"use radio_tune); ignored {sorted(params)}")
        if btype == "sdr_source":
            uses_live = True
            variables.setdefault("sdr_zmq_address",
                                 f"__import__('os').environ.get('SDRPILOT_ZMQ', {zmq_address!r})")
            expanded.append({"name": b["name"], "type": "zeromq_sub_source",
                             "params": {"type": "complex", "address": "sdr_zmq_address", "timeout": "100",
                                        "pass_tags": "False", "hwm": "-1", "key": "''"}})
        elif btype == "capture_source":
            path = params.get("path") or params.get("file")
            if not path or not os.path.exists(path):
                raise ValueError(f"capture_source: file not found: {path}")
            side = os.path.splitext(path)[0] + ".json"
            if os.path.exists(side):
                with open(side, encoding="utf-8") as f:
                    capture_meta = json.load(f)
            expanded.append({"name": b["name"], "type": "blocks_file_source",
                             "params": {"type": "complex", "file": repr(path),
                                        "repeat": _val(params.get("repeat", False)), "vlen": "1",
                                        "begin_tag": "pmt.PMT_NIL", "offset": "0", "length": "0"}})
        else:
            expanded.append(b)

    expanded, spec = _rename_reserved(expanded, spec, notes)
    blocks_spec = list(spec.get("blocks") or [])
    if uses_live and radio_state:
        variables.setdefault("samp_rate", radio_state.get("sample_rate"))
        variables.setdefault("center_freq", radio_state.get("center_freq_hz"))
    if capture_meta:
        variables.setdefault("samp_rate", capture_meta.get("sample_rate"))
        variables.setdefault("center_freq", capture_meta.get("center_freq_hz"))
    hier_params = dict((hier or {}).get("parameters") or {})
    if "samp_rate" not in hier_params:
        variables.setdefault("samp_rate", 32000)
    if VAR_PROVIDER is not None:  # the AI's saved number variables (analysis_variable) work in flowgraphs too
        text = " ".join(str(v) for b in blocks_spec for v in (b.get("params") or {}).values()) + " " + \
            " ".join(str(v) for v in variables.values())
        wanted = set(re.findall(r"[A-Za-z_]\w*", text)) - set(variables) - set(hier_params)
        try:
            extra = VAR_PROVIDER(wanted) if wanted else {}
        except Exception:  # pragma: no cover
            extra = {}
        for k, v in extra.items():
            variables[k] = repr(v)
        if extra:
            notes.append(f"used your saved variable(s) {sorted(extra)} = {extra} (analysis_variable)")

    created = {}
    # variables first
    for vname, vval in variables.items():
        vb = fg.new_block("variable")
        vb.params["id"].set_value(sanitize(vname) if vname != sanitize(vname) else vname)
        vb.params["value"].set_value(_val(vval))
        created[vname] = vb
    if spec.get("imports"):
        ib = fg.new_block("import")
        ib.params["id"].set_value("imports_0")
        ib.params["imports"].set_value("\n".join(spec["imports"]))
        created["imports_0"] = ib

    errors: list[str] = []
    extra_conns: list[str] = []
    pad_names: list[str] = []
    if hier:  # reusable (hierarchical) block: parameters + input/output pads
        for pname, pval in hier_params.items():
            pb = fg.new_block("parameter")
            pb.params["id"].set_value(pname)
            pb.params["label"].set_value(pname.replace("_", " ").title())
            pb.params["type"].set_value("")
            pb.params["value"].set_value(_val(pval))
            created[pname] = pb
        for kind, key, pad_type in (("inputs", "to", "pad_source"), ("outputs", "from", "pad_sink")):
            for i, port in enumerate(hier.get(kind) or []):
                if isinstance(port, str):
                    port = {key: port}
                pid = f"{'in' if kind == 'inputs' else 'out'}_{i}"
                pb = fg.new_block(pad_type)
                pb.params["id"].set_value(pid)
                pb.params["label"].set_value(port.get("label") or pid)
                pb.params["type"].set_value(port.get("type") or "complex")
                if port.get("vlen"):
                    pb.params["vlen"].set_value(_val(port["vlen"]))
                if port.get("optional") and "optional" in pb.params:
                    pb.params["optional"].set_value("True")
                created[pid] = pb
                pad_names.append(pid)
                ends = port.get(key) or []
                for e in ([ends] if isinstance(ends, str) else ends):
                    msg = (port.get("type") == "message")
                    extra_conns.append(f"{pid}:{'out' if msg else 0} -> {e}" if kind == "inputs"
                                       else f"{e} -> {pid}:{'in' if msg else 0}")
                if not ends:
                    errors.append(f"{kind}[{i}] needs '{key}': the block port it connects to, e.g. 'filt:0'")
        fg.rewrite()
    for b in expanded:
        bname, btype = b.get("name"), b.get("type")
        rt = resolve_type(btype) if btype else btype
        if rt != btype:
            notes.append(f"'{bname}': block type '{btype}' doesn't exist - used '{rt}' (write it that way next time)")
            btype = rt
        if not bname or not btype:
            errors.append(f"block entry needs 'name' and 'type': {b}")
            continue
        if is_tx_block(btype):
            errors.append(f"'{bname}': {btype} is a transmitter. SDR Pilot is receive-only - transmit blocks are "
                          "not allowed")
            continue
        try:
            if blocked_types([btype]):
                errors.append(f"'{bname}': {btype} is a one-block receiver from an out-of-tree module. The user wants "
                              "decoders built from component blocks; use the module's component blocks, or justify "
                              f"this one with oot_justify(block_type='{btype}', justification='...why building it "
                              "from components would be extremely complicated...')")
                continue
        except Exception:  # pragma: no cover
            pass
        if btype not in p.blocks and _reload_if_new_hier(p, btype):
            pass
        if btype not in p.blocks:
            errors.append(f"unknown block type '{btype}' for '{bname}'. Similar: {similar(btype)}")
            continue
        blk = fg.new_block(btype)
        if blk is None:
            errors.append(f"could not create block '{btype}'")
            continue
        blk.params["id"].set_value(bname)
        params = dict(b.get("params") or {})
        # epy_block: source code first, then rewrite so its parameters/ports exist
        if btype == "epy_block" and "_source_code" in params:
            src = params.pop("_source_code")
            blk.params["_source_code"].set_value(src)
            blk.rewrite()
            # GRC leaves the block's arguments empty unless set: fill them from blk.__init__'s defaults
            for k, v in _epy_defaults(src).items():
                if k in blk.params and k not in params and not str(blk.params[k].get_value()).strip():
                    blk.params[k].set_value(v)
        for k, v in params.items():
            if k not in blk.params:
                real = match_param(k, list(blk.params))
                if real and real not in params:
                    notes.append(f"'{bname}': there's no parameter '{k}' on {btype} - used '{real}'")
                    k = real
                else:
                    errors.append(f"'{bname}' ({btype}) has no parameter '{k}'. Valid: "
                                  f"{[x for x in blk.params if x not in COMMON_PARAMS]}")
                    continue
            blk.params[k].set_value(_val(v))
            explicit.setdefault(bname, set()).add(k)
        if b.get("disabled"):
            blk.state = "disabled"
        if b.get("note") and "comment" in blk.params:  # GRC shows the comment under the block
            blk.params["comment"].set_value(str(b["note"])[:200])
        created[bname] = blk

    fg.rewrite()
    blocks_known = [n for n in created if n not in variables and n != "imports_0"]
    types = {b.get("name"): b.get("type") for b in blocks_spec if b.get("name")}
    canon: list[str] = []      # connections as made, in canonical 'a:port -> b:port' form (saved)
    pending: list[str] = []    # connections to blocks that aren't in the flowgraph yet (made once they are)
    seen: set = set()
    made: list = []
    own = list(spec.get("connections") or [])
    for i, c in enumerate(own + extra_conns):
        mine = i < len(own)
        try:
            pairs = split_conns(c)
        except Exception as e:
            errors.append(f"connection {c!r}: {e}")
            continue
        for a_s, b_s in pairs:
            label = f"{a_s} -> {b_s}"
            try:
                sb, sp = parse_endpoint(a_s, blocks_known, types)
                db, dp = parse_endpoint(b_s, blocks_known, types)
                missing = [x for x in (sb, db) if x not in created]
                if missing:
                    if mine:
                        pending.append(f"{sb}:{sp or 0} -> {db}:{dp or 0}")
                    if not draft or not mine:
                        errors.append(f"connection '{label}': there's no block named {missing[0]!r} in this flowgraph. "
                                      f"Blocks: {blocks_known}. Write it as 'block:port -> block:port' with those names")
                    continue
                if sb == db:
                    errors.append(f"connection '{label}' connects '{sb}' to itself - dropped")
                    continue
                so = _find_port(created[sb], sp, "out")
                if (norm_port(dp).lower() in _DEFAULT_PORTS or norm_port(dp) == "0") and so.domain != "stream" and \
                        getattr(created[db], "key", "") == "blocks_message_debug":
                    dp = "print"
                si = _find_port(created[db], dp, "in", prefer_domain=so.domain)
                key = (sb, str(so.key), db, str(si.key))
                if key in seen:
                    continue
                seen.add(key)
                made.append((key, so, si, mine))
            except Exception as e:
                errors.append(f"connection '{label}': {e} - not saved; send it again corrected")
    # a stream input takes exactly one upstream: when two connections feed the same one, the later one wins
    last_into: dict = {}
    for i, (key, so, si, mine) in enumerate(made):
        if si.domain == "stream":
            last_into[(key[2], key[3])] = i
    for i, (key, so, si, mine) in enumerate(made):
        if si.domain == "stream" and last_into[(key[2], key[3])] != i:
            k2 = made[last_into[(key[2], key[3])]][0]
            notes.append(f"{key[2]}:{key[3]} had two inputs: kept {k2[0]}:{k2[1]} -> {k2[2]}:{k2[3]}, dropped "
                         f"{key[0]}:{key[1]} -> {key[2]}:{key[3]}")
            continue
        fg.connect(so, si)
        if mine:
            canon.append(f"{key[0]}:{key[1]} -> {key[2]}:{key[3]}")
    pending = list(dict.fromkeys(pending))
    spec = {**spec, "connections": canon + pending}
    _fix_source_rates(created, made, src_names, variables, notes)

    fg.rewrite()
    _match_types(fg, created, explicit, notes)
    fg.validate()
    if not draft and not hier:
        _terminate_outputs(fg, created, notes, canon)
    errors += _band_checks(created, variables, radio_state, notes)
    open_ports: list[str] = []
    for elem, msg in fg.iter_error_messages():
        where = ""
        if getattr(elem, "is_port", False) and str(msg).strip() == OPEN_PORT_MSG:
            try:
                open_ports.append(f"{elem.parent_block.name}:{elem.key} ({'output' if elem.is_source else 'input'})")
            except Exception:
                pass
            if draft:
                continue
        try:
            if getattr(elem, "is_block", False):
                where = f"block '{elem.name}': "
            elif getattr(elem, "is_param", False):
                where = f"block '{elem.parent_block.name}' param '{elem.key}': "
            elif getattr(elem, "is_connection", False):
                where = f"connection {elem.source_block.name}:{elem.source_port.key} -> {elem.sink_block.name}:{elem.sink_port.key}: "
            elif getattr(elem, "is_port", False):
                where = f"block '{elem.parent_block.name}' port '{elem.key}': "
        except Exception:
            pass
        errors.append(where + str(msg))
    errors = list(dict.fromkeys(errors))

    _layout(fg, created, {**spec, "connections": canon + extra_conns,
                          "blocks": [{"name": n} for n in pad_names if n.startswith("in_")] + list(spec.get("blocks") or [])
                          + [{"name": n} for n in pad_names if n.startswith("out_")]})
    grc_path = os.path.join(folder, name + ".grc")
    try:
        p.save_flow_graph(grc_path, fg)
    except Exception as e:
        errors.append(f"saving .grc failed: {e}")
    py_path = None
    complete = not errors and not (draft and (open_ports or pending))
    if complete:
        try:
            gen = p.Generator(fg, folder)
            gen.write()
            py_path = gen.file_path
        except Exception as e:
            errors.append(f"code generation failed: {e}")
    if hier and not errors and hier.get("reload", True):
        # GRC wrote <name>.py + <name>.block.yml into its hier-block folder (~/.grc_gnuradio); reload the
        # library so the new block can be used right away (GNU Radio Companion lists it under its category too)
        p.build_library()
        _DESCRIBE_CACHE.clear()
        if name not in p.blocks:
            errors.append(f"generated the block but GRC didn't load it from {p.config.hier_block_lib_dir}")
    valid = not errors and not (draft and (open_ports or pending))
    meta = {"name": name, "spec": spec, "uses_live_radio": uses_live, "built": time.time(),
            "built_center_freq": variables.get("center_freq"), "built_samp_rate": variables.get("samp_rate"),
            "gui": gui, "grc": grc_path, "py": py_path, "valid": valid,
            "explicit_vars": sorted((spec.get("variables") or {}).keys()),
            "kind": "hier" if hier else "grc", "hier": bool(hier),
            "draft": bool(draft and not (complete and py_path)), "open_ports": open_ports,
            "history": list(history or [])[-80:], "builder_rev": BUILDER_REV}
    from . import fglib
    meta["spec_digest"] = fglib.spec_digest(spec)
    meta_path = os.path.join(folder, "sdrpilot.json")
    old_meta = None
    try:
        with open(meta_path, encoding="utf-8") as f:
            old_meta = json.load(f)
    except (OSError, ValueError):
        pass
    fglib.carry_health(old_meta, meta)
    if old_meta and old_meta.get("built_for"):
        meta["built_for"] = old_meta["built_for"]
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2, default=str)
    if not hier:
        try:
            undoc = missing_docs(spec)
        except Exception:  # pragma: no cover
            undoc = []
        if undoc:
            notes.append(f"describe your blocks: {undoc} {'is' if len(undoc) == 1 else 'are'} yours (Python / AI-made) "
                         f"and {'has' if len(undoc) == 1 else 'have'} no description. The user sees a block's description "
                         "when hovering over it on the dashboard - add one line each: gnuradio_edit_flowgraph(name='"
                         f"{name}', set_docs={{'{undoc[0]}': 'what it does - input: ..., output: ...'}}) (or a class "
                         "docstring in the Python block)")
    out = {"name": name, "valid": valid, "errors": errors, "grc_file": grc_path, "python_file": py_path,
           "uses_live_radio": uses_live, "variables": {k: _val(v) for k, v in variables.items()},
           "blocks": len(created), "notes": notes}
    if pending and draft:
        out["waiting_for"] = [f"{c} (made as soon as that block is added)" for c in pending]
    if draft:
        out["open_ports"] = open_ports
        out["complete"] = valid and not pending
    out["connections"] = canon
    return out


# bump when a builder fix changes the generated code: older flowgraphs are rebuilt from their spec before running
# (2: unconnected optional outputs of hier blocks get null sinks; source-fed samp_rate literals fixed)
BUILDER_REV = 2

NULL_SINK_TYPES = {"complex": "complex", "float": "float", "int": "int", "short": "short", "byte": "byte",
                   "fc32": "complex", "f32": "float", "s32": "int", "s16": "short", "s8": "byte", "u8": "byte"}


VAR_PROVIDER = None  # set by the server: names -> {name: number} from the AI's saved variables

TYPE_PARAMS = ("type", "io_type", "itype", "otype", "input_type", "output_type", "in_type", "out_type")
CONVERTIBLE = ("complex", "float", "int", "short", "byte")


def _pvlen(pt) -> int:
    try:
        return int(pt.vlen)
    except Exception:
        return 1


def _same(so, si) -> bool:
    return str(so.dtype) == str(si.dtype) and _pvlen(so) == _pvlen(si)


def _retype(fg, blk, port, want_dtype: str, want_vlen: int, explicit: dict, depth: int = 0,
            seen: set | None = None) -> list[str] | None:
    """Pick the variant of a multi-type block (the `type` parameter of *_xx / *_vxx blocks, sp_convert's in/out
    type...) whose port matches what it is connected to - if the AI didn't choose that parameter itself. When that
    breaks the block's other connections, the neighbours there are re-typed the same way (a chain of
    add/multiply/file blocks all follow the source). Returns the changes made, or None (everything undone)."""
    seen = set(seen or ()) | {blk.name}
    side = "sinks" if port in blk.sinks else "sources"
    idx = list(getattr(blk, side)).index(port)
    for pk in TYPE_PARAMS:
        prm = blk.params.get(pk)
        if prm is None or pk in explicit.get(blk.name, set()) or getattr(prm, "dtype", "") != "enum":
            continue
        orig = prm.get_value()
        for opt in list(prm.options):
            if opt == orig:
                continue
            prm.set_value(opt)
            fg.rewrite()
            ports = list(getattr(blk, side))
            if not (idx < len(ports) and str(ports[idx].dtype) == want_dtype and _pvlen(ports[idx]) == want_vlen):
                continue
            changes = [f"{blk.name}.{pk}={opt}"]
            ok = True
            for c in list(fg.connections):
                so, si = c.source_port, c.sink_port
                if so.domain != "stream" or _same(so, si):
                    continue
                if so.parent_block is blk and si.parent_block.name not in seen and depth < 6:
                    sub = _retype(fg, si.parent_block, si, str(so.dtype), _pvlen(so), explicit, depth + 1, seen)
                elif si.parent_block is blk and so.parent_block.name not in seen and depth < 6:
                    sub = _retype(fg, so.parent_block, so, str(si.dtype), _pvlen(si), explicit, depth + 1, seen)
                elif so.parent_block is blk or si.parent_block is blk:
                    sub = None
                else:
                    continue
                if sub is None:
                    ok = False
                    break
                changes += sub
            if ok:
                return changes
            _undo(fg, changes[1:])
        prm.set_value(orig)
        fg.rewrite()
    return None


def _undo(fg, changes: list[str]) -> None:
    """Put back neighbours re-typed by a _retype attempt that failed further on (their previous value is the
    block's default, since only parameters the AI didn't set are ever changed)."""
    for ch in reversed(changes):
        bname, rest = ch.split(".", 1)
        pk = rest.split("=", 1)[0]
        for b in fg.blocks:
            if b.name == bname and pk in b.params:
                b.params[pk].set_value(b.params[pk].default)
    fg.rewrite()


def _match_types(fg, created: dict, explicit: dict, notes: list) -> None:
    """Make every stream connection's types agree, the way a person would in GRC: first by choosing the matching
    variant of a block that comes in several types, else by putting a converter (sp_convert, or stream<->vector)
    between the two ports. Converters are re-derived on every build, like the null sinks."""
    n_conv = 0
    for c in list(fg.connections):
        so, si = c.source_port, c.sink_port
        if so.domain != "stream" or si.domain != "stream" or _same(so, si):
            continue
        sb, db = so.parent_block, si.parent_block
        if _same(so, si):  # fixed while re-typing an earlier connection's neighbours
            continue
        got = _retype(fg, db, si, str(so.dtype), _pvlen(so), explicit) or \
            _retype(fg, sb, so, str(si.dtype), _pvlen(si), explicit)
        if got:
            notes.append(f"blocks that come in several types were set to match what they're connected to: "
                         f"{', '.join(got)} (set the type yourself to override)")
            continue
        sd, dd, sv, dv = str(so.dtype), str(si.dtype), _pvlen(so), _pvlen(si)
        conv, params, what = None, {}, ""
        if sd == dd and sv != dv and sd in CONVERTIBLE + ("byte",):
            if sv == 1:
                conv, params = "blocks_stream_to_vector", {"type": sd, "num_items": str(dv)}
                what = f"stream -> vectors of {dv}"
            elif dv == 1:
                conv, params = "blocks_vector_to_stream", {"type": sd, "num_items": str(sv)}
                what = f"vectors of {sv} -> stream"
        elif sd in CONVERTIBLE and dd in CONVERTIBLE and sv == dv and "sp_convert" in platform().blocks:
            conv, params = "sp_convert", {"in_type": sd, "out_type": dd, "vlen": str(sv)}
            what = f"{sd} -> {dd}" + (" (magnitude)" if sd == "complex" else "")
        if not conv or conv not in platform().blocks:
            continue  # GRC reports the mismatch
        if conv == "blocks_stream_to_vector" or conv == "blocks_vector_to_stream":
            params["type"] = {"byte": "byte"}.get(sd, sd)
        name = f"conv_{sb.name}_{db.name}"[:60]
        while name in created:
            name += "_"
        nb = fg.new_block(conv)
        nb.params["id"].set_value(name)
        for k, v in params.items():
            if k in nb.params:
                nb.params[k].set_value(v)
        fg.rewrite()
        fg.remove_element(c)
        fg.connect(so, nb.sinks[0])
        fg.connect(nb.sources[0], si)
        try:
            nb.states["coordinate"] = ((sb.states.get("coordinate", (0, 0))[0] + db.states.get("coordinate", (0, 0))[0]) / 2,
                                       max(sb.states.get("coordinate", (0, 0))[1], db.states.get("coordinate", (0, 0))[1]) + 90)
        except Exception:
            pass
        created[name] = nb
        n_conv += 1
        hint = (" If you meant the real part, the phase or an FM/AM demodulator, put that block (or sp_convert with "
                "mode='real'/'imag'/'arg') there yourself.") if sd == "complex" and dd != "complex" else ""
        notes.append(f"'{sb.name}' outputs {sd}{'' if sv == 1 else f'[{sv}]'} but '{db.name}' takes "
                     f"{dd}{'' if dv == 1 else f'[{dv}]'}: put a converter '{name}' ({what}) between them.{hint}")
    if n_conv:
        fg.rewrite()


def _is_hier(blk) -> bool:
    """GRC hier blocks (SDR Pilot's sp_* receivers, anything from ~/.grc_gnuradio): an output marked optional is
    still wired to an inner block, and GNU Radio refuses to start when that inner output goes nowhere
    ("insufficient connected output ports")."""
    key = str(getattr(blk, "key", ""))
    cat = str(getattr(blk, "category", ""))
    plain = key in ("sp_data_sink", "sp_convert") or key.startswith("sp_radio_")
    return key.startswith("sp_") and not plain or "Hier" in cat or "SDR Pilot" in cat and not plain


def _terminate_outputs(fg, created: dict, notes: list, canon: list) -> None:
    """An unconnected stream OUTPUT (e.g. sp_wbfm_receiver's audio when only RDS is wanted) makes GRC refuse the whole
    flowgraph. Give each one a null sink, say so, and carry on."""
    added = []
    for bname, blk in list(created.items()):
        for pt in getattr(blk, "sources", []):
            if pt.domain != "stream" or getattr(pt, "hidden", False):
                continue
            try:
                if list(pt.connections()):
                    continue
            except Exception:
                continue
            if getattr(pt, "optional", False) and not _is_hier(blk):
                continue  # a real block with an optional output runs fine without it; a hier block doesn't
            dtype = NULL_SINK_TYPES.get(str(pt.dtype))
            if not dtype:
                continue
            ns = fg.new_block("blocks_null_sink")
            nid = f"{bname}_out{pt.key}_null"
            ns.params["id"].set_value(nid)
            ns.params["type"].set_value(dtype)
            try:
                vlen = int(pt.vlen)
            except Exception:
                vlen = 1
            if vlen > 1:
                ns.params["vlen"].set_value(str(vlen))
            fg.rewrite()
            fg.connect(pt, ns.sinks[0])
            created[nid] = ns
            canon.append(f"{bname}:{pt.key} -> {nid}:0")
            added.append(f"{bname}:{pt.key}")
    if added:
        fg.rewrite()
        fg.validate()
        notes.append(f"unconnected output(s) {added} were each given a null sink (they'd stop GRC from running the "
                     "flowgraph); connect them to something instead if you need that output")


RESERVED_IDS = {"blocks", "analog", "digital", "filter", "fft", "gr", "pmt", "zeromq", "audio", "channels", "pdu",
                "network", "qtgui", "uhd", "soapy", "osmosdr", "math", "numpy", "np", "window", "firdes", "rds",
                "adsb", "ieee802_11", "ieee802_15_4", "lora_sdr", "satellites", "dect2", "iridium", "inspector", "os",
                "sys", "time", "sip", "Qt", "eng_notation", "trellis", "vocoder", "dtv", "fec", "iio", "epy_block",
                "signal", "threading", "json", "struct", "wave", "random", "Template", "logging"}


def _reserved() -> set:
    try:
        from gnuradio.grc.core.params import dtypes
        return set(dtypes.ID_BLACKLIST) | RESERVED_IDS
    except Exception:  # pragma: no cover
        import builtins
        import keyword
        return RESERVED_IDS | set(dir(builtins)) | set(keyword.kwlist)


def _rename_reserved(expanded: list, spec: dict, notes: list) -> tuple[list, dict]:
    """Block names GRC refuses (Python keywords/builtins, module names like 'fft', 'blocks', 'filter', top_block
    attributes like 'start' or 'connect') get a '_0' suffix - connections follow - instead of failing the build."""
    bad = _reserved()
    taken = {b.get("name") for b in expanded} | set((spec.get("variables") or {}).keys())
    ren = {}
    for b in expanded:
        n = b.get("name")
        if n and (n in bad or n.startswith("_")):
            new = n.strip("_") + "_0" if n.strip("_") else "blk_0"
            while new in taken or new in bad:
                new = new[:-1] + str(int(new[-1]) + 1) if new[-1].isdigit() else new + "_0"
            ren[n] = new
            taken.add(new)
    if not ren:
        return expanded, spec
    expanded = [{**b, "name": ren.get(b.get("name"), b.get("name"))} for b in expanded]
    conns = []
    names = [b.get("name") for b in spec.get("blocks") or []]
    for c in spec.get("connections") or []:
        try:
            for a, z in split_conns(c):
                sb, sp_ = parse_endpoint(a, names)
                db, dp = parse_endpoint(z, names)
                conns.append(f"{ren.get(sb, sb)}:{sp_ or 0} -> {ren.get(db, db)}:{dp or 0}")
        except Exception:
            conns.append(c)
    blocks = [{**b, "name": ren.get(b.get("name"), b.get("name"))} for b in spec.get("blocks") or []]
    notes.append("renamed block(s) whose names GNU Radio reserves: "
                 + ", ".join(f"'{k}' -> '{v}'" for k, v in ren.items()) + " (use the new names from now on)")
    return expanded, {**spec, "blocks": blocks, "connections": conns}


def _fix_source_rates(created: dict, made: list, src_names: set, variables: dict, notes: list) -> None:
    """A block fed straight from the radio/capture runs at the source's rate. A typed-in number that differs
    (e.g. samp_rate=2400000 copied from a recipe default while the radio delivers 2 MS/s) silently breaks every
    filter, demodulator gain and resampler inside it - point it at the samp_rate variable instead."""
    try:
        fs = float(eval(str(variables.get("samp_rate")), {"__builtins__": {}}, {}))
    except Exception:
        return
    for key, _so, _si, _mine in made:
        if key[0] not in src_names or key[2] not in created:
            continue
        p = created[key[2]].params.get("samp_rate")
        if p is None:
            continue
        raw = str(p.get_value()).strip()
        try:
            v = float(raw)
        except ValueError:
            continue  # an expression (samp_rate, samp_rate/4...) - leave it
        if abs(v - fs) > 1:
            p.set_value("samp_rate")
            notes.append(f"'{key[2]}': samp_rate was {v:g} but it is fed straight from '{key[0]}', which delivers "
                         f"{fs:g} S/s - set it to samp_rate (a wrong rate breaks its filters, demodulator and "
                         f"resampler)")


def _band_checks(created: dict, variables: dict, radio_state: dict | None, notes: list) -> list[str]:
    """A channel offset outside the tuned band is the commonest reason a decoder 'runs but decodes nothing'
    (e.g. a recipe's default station frequency left at 100.1 MHz while the radio sits at 93.2 MHz). Catch it at
    build time, with the numbers."""
    errs = []
    cf = None
    try:
        cf = float(created["center_freq"].params["value"].get_evaluated()) if "center_freq" in created else None
    except Exception:
        cf = None
    rcv = (radio_state or {}).get("receiver") or {}
    dc_block = int(((radio_state or {}).get("device_settings") or {}).get("dcBlock") or 0) or rcv.get("dc_block_on")
    spike = rcv.get("dc_spike_at_center") is True and not dc_block
    for bname, blk in created.items():
        if bname in variables:
            continue
        key = "offset" if "offset" in blk.params else ("center_freq" if getattr(blk, "key", "").startswith(
            "freq_xlating") and "center_freq" in blk.params else None)
        if not key or "samp_rate" not in blk.params:
            continue
        try:
            off = float(blk.params[key].get_evaluated())
            fs = float(blk.params["samp_rate"].get_evaluated())
        except Exception:
            continue
        if fs <= 0:
            continue
        where = f" = {(cf + off) / 1e6:.4f} MHz" if cf else ""
        if abs(off) > fs / 2:
            errs.append(f"block '{bname}': {key} {off / 1e3:+.1f} kHz{where} is outside the band the radio delivers "
                        f"(samp_rate {fs / 1e6:g} MS/s covers +/-{fs / 2e3:g} kHz"
                        + (f" around {cf / 1e6:.4f} MHz" if cf else "") + "). Nothing would be decoded. Set the "
                        "frequency variable to the signal you want (e.g. variables={'station': <Hz>} / "
                        "{'chan_freq': <Hz>}) or radio_tune near it first")
        elif abs(off) > 0.45 * fs:
            notes.append(f"'{bname}': the channel is at the very edge of the band ({off / 1e3:+.1f} kHz of +/-"
                         f"{fs / 2e3:g} kHz) - the band-edge filter roll-off will weaken it; retune closer")
        elif spike and abs(off) < 5e3:
            notes.append(f"'{bname}': (minor) the channel is centred on the tuning frequency{where}, where this "
                         f"receiver has a small DC spike and dcBlock is off. Fine for wideband signals like broadcast "
                         f"FM; for a narrow signal turn dcBlock on or retune ~50-100 kHz away")
    return errs


def _visible_params(b) -> list[tuple[str, str]]:
    out = []
    for pp in b.params.values():
        if pp.key in ("id", "comment", "alias", "affinity", "minoutbuf", "maxoutbuf", "_source_code"):
            continue
        try:
            if pp.hide != "none":
                continue
        except Exception:
            pass
        out.append((str(pp.name), _short_val(pp.get_value())))
    return out[:8]


def _est_size(b) -> tuple[float, float]:
    """Approximate drawn size of a block (same rule the dashboard uses; close to GRC's own)."""
    params = _visible_params(b)
    lines = [str(b.label)] + [f"{k}: {v}" for k, v in params]
    w = max(150.0, min(390.0, max(len(t) for t in lines) * 6.6 + 26))
    nports = max(len([p for p in b.sinks if not getattr(p, "hidden", False)]),
                 len([p for p in b.sources if not getattr(p, "hidden", False)]))
    h = max(30 + len(params) * 15 + 10, 18 + nports * 20 + 6, 50)
    return w, h


def _layout(fg, created: dict, spec: dict) -> None:
    """Left-to-right layered layout, spaced by each block's real size, so the .grc reads well in GRC."""
    names = [b.get("name") for b in spec.get("blocks") or [] if b.get("name") in created]
    edges = []
    for c in spec.get("connections") or []:
        try:
            for a, b in split_conns(c):
                edges.append((parse_endpoint(a, list(created))[0], parse_endpoint(b, list(created))[0]))
        except Exception:
            pass
    depth = {n: 0 for n in names}
    for _ in range(len(names)):
        changed = False
        for a, b in edges:
            if a in depth and b in depth and depth[b] < depth[a] + 1 and depth[a] < len(names):
                depth[b] = depth[a] + 1
                changed = True
        if not changed:
            break
    sizes = {}
    for n in names:
        try:
            sizes[n] = _est_size(created[n])
        except Exception:
            sizes[n] = (200.0, 80.0)
    ncols = max(depth.values(), default=0) + 1
    col_w = [0.0] * ncols
    for n in names:
        col_w[depth[n]] = max(col_w[depth[n]], sizes[n][0])
    col_x = [40.0]
    for i in range(1, ncols):
        col_x.append(col_x[-1] + col_w[i - 1] + 70)
    # variables / imports row on top
    x = 40.0
    top_h = 0.0
    for vname, blk in created.items():
        if vname in names:
            continue
        try:
            w, h = _est_size(blk)
            blk.states["coordinate"] = (round(x), 20)
            x += w + 30
            top_h = max(top_h, h)
        except Exception:
            pass
    y0 = 20 + top_h + 60
    col_y = [y0] * ncols
    for n in names:
        pos = next((b.get("pos") for b in spec.get("blocks") or [] if b.get("name") == n and b.get("pos")), None)
        d = depth.get(n, 0)
        try:
            if pos:
                created[n].states["coordinate"] = tuple(pos)
            else:
                created[n].states["coordinate"] = (round(col_x[d]), round(col_y[d]))
                col_y[d] += sizes[n][1] + 50
        except Exception:
            pass


def load_flowgraph(grc_path: str) -> dict:
    """Read a .grc and return it in build_flowgraph's spec format (non-default params only)."""
    import yaml
    with open(grc_path, encoding="utf-8") as f:
        head = f.read(400)
        f.seek(0)
        data = None if "<flow_graph>" in head else yaml.safe_load(f)
    if data is None:  # a GNU Radio 3.7 XML flowgraph: GRC converts it
        with GRC_LOCK:
            data = platform().parse_flow_graph(grc_path)
    side = os.path.join(os.path.dirname(grc_path), "sdrpilot.json")
    if os.path.exists(side):
        with open(side, encoding="utf-8") as f:
            meta = json.load(f)
        if meta.get("spec"):
            return {"source": "sdrpilot spec (as built)", "spec": meta["spec"]}
    p = platform()
    opts = data.get("options", {}).get("parameters", {})
    spec = {"name": opts.get("id", "flowgraph"), "title": opts.get("title", ""), "gui": opts.get("generate_options") != "no_gui",
            "variables": {}, "blocks": [], "connections": []}
    spec["imports"] = []
    for b in data.get("blocks", []):
        params = b.get("parameters", {}) or {}
        if b["id"] == "variable":
            spec["variables"][b["name"]] = params.get("value")
            continue
        if b["id"] == "import":
            spec["imports"] += [l for l in str(params.get("imports", "")).splitlines() if l.strip()]
            continue
        defaults = {}
        B = p.blocks.get(b["id"])
        if B is not None:
            defaults = {pd["id"]: str(pd.get("default", "")) for pd in B.parameters_data}
        keep = {k: v for k, v in params.items() if k not in COMMON_PARAMS and str(v) != defaults.get(k, None)}
        entry = {"name": b["name"], "type": b["id"], "params": keep}
        if str(params.get("comment") or "").strip():
            entry["note"] = str(params["comment"]).strip()
        st = b.get("states") or {}
        if st.get("state") == "disabled":
            entry["disabled"] = True
        if st.get("coordinate"):
            entry["pos"] = list(st["coordinate"])[:2]
        spec["blocks"].append(entry)
    for c in data.get("connections", []):
        spec["connections"].append(f"{c[0]}:{c[1]} -> {c[2]}:{c[3]}")
    return {"source": "parsed .grc", "spec": spec}


# ------------------------------------------------------------------ editing

def apply_ops(spec: dict, set_params: dict | None = None, set_variables: dict | None = None,
              add_blocks: list | None = None, remove_blocks: list | None = None,
              add_connections: list | None = None, remove_connections: list | None = None,
              replace_block_types: dict | None = None) -> tuple[dict, list[str]]:
    """Return a modified copy of a flowgraph spec plus a list of human-readable changes."""
    import copy
    spec = copy.deepcopy(spec)
    changes: list[str] = []
    errors: list[str] = []
    blocks = spec.setdefault("blocks", [])
    by_name = {b["name"]: b for b in blocks}
    for name in remove_blocks or []:
        if name in by_name:
            blocks.remove(by_name.pop(name))
            before = len(spec.get("connections") or [])
            names = [b["name"] for b in blocks] + [name]
            keep = []
            for c in spec.get("connections") or []:
                try:
                    if any(name in (t[0], t[2]) for t in canon_conn(c, names)):
                        continue
                except Exception:
                    pass
                keep.append(c)
            spec["connections"] = keep
            changes.append(f"removed block {name} (+{before - len(spec['connections'])} connections)")
        elif name in (spec.get("variables") or {}):
            spec["variables"].pop(name)
            changes.append(f"removed variable {name}")
        else:
            errors.append(f"remove_blocks: no block named '{name}'")
    for name, new_type in (replace_block_types or {}).items():
        if name not in by_name:
            errors.append(f"replace_block_types: no block named '{name}'")
            continue
        by_name[name]["type"] = new_type
        by_name[name]["params"] = {}
        changes.append(f"{name} is now a {new_type} (params reset)")
    for b in add_blocks or []:
        if not b.get("name") or not b.get("type"):
            errors.append(f"add_blocks entry needs name and type: {b}")
            continue
        if b["name"] in by_name:
            errors.append(f"add_blocks: '{b['name']}' already exists (use set_params or remove it first)")
            continue
        blocks.append(dict(b))
        by_name[b["name"]] = blocks[-1]
        changes.append(f"added {b['type']} '{b['name']}'")
    for name, params in (set_params or {}).items():
        if name in by_name:
            by_name[name].setdefault("params", {}).update(params or {})
            changes.append(f"{name}: " + ", ".join(f"{k}={str(v)[:40]}" for k, v in (params or {}).items()))
        elif name in (spec.get("variables") or {}) and isinstance(params, dict) and "value" in params:
            spec["variables"][name] = params["value"]
            changes.append(f"variable {name} = {params['value']}")
        else:
            errors.append(f"set_params: no block named '{name}'. Blocks: {sorted(by_name)}")
    for k, v in (set_variables or {}).items():
        spec.setdefault("variables", {})[k] = v
        changes.append(f"variable {k} = {v}")
    names = [b["name"] for b in blocks]
    types = {b["name"]: b.get("type") for b in blocks}
    conns: list[tuple] = []
    for c in spec.get("connections") or []:
        try:
            conns += canon_conn(c, names, types)
        except Exception:
            pass
    for c in remove_connections or []:
        try:
            ts = canon_conn(c, names, types)
        except Exception as e:
            errors.append(f"remove_connections: {e}")
            continue
        for t in ts:
            hit = t if t in conns else next((x for x in conns if (x[0], x[2]) == (t[0], t[2])), None)
            if hit:
                conns.remove(hit)
                changes.append(f"disconnected {hit[0]}:{hit[1]} -> {hit[2]}:{hit[3]}")
            else:
                errors.append(f"remove_connections: {t[0]}:{t[1]} -> {t[2]}:{t[3]} isn't a connection here")
    for c in add_connections or []:
        try:
            ts = canon_conn(c, names, types)
        except Exception as e:
            errors.append(f"add_connections: {e}")
            continue
        for t in ts:
            if t not in conns:
                conns.append(t)
                changes.append(f"connected {t[0]}:{t[1]} -> {t[2]}:{t[3]}")
    spec["connections"] = [f"{a}:{b} -> {c}:{d}" for a, b, c, d in conns]
    if errors and not changes:
        raise ValueError("; ".join(errors))
    changes += [f"skipped: {e}" for e in errors]
    return spec, changes


# ------------------------------------------------------------------ dashboard rendering

def _short_val(v, n: int = 34) -> str:
    v = " ".join(str(v).split())
    return v if len(v) <= n else v[: n - 1] + "…"


_PYDOC_CACHE: dict[str, str] = {}


def _port_info(pt) -> dict:
    d = {"key": str(pt.key), "dtype": str(pt.dtype), "domain": str(pt.domain)}
    try:
        lab = str(getattr(pt, "name", "") or getattr(pt, "label", "") or "")
        if lab and lab not in ("in", "out") and lab != d["key"]:
            d["label"] = lab
    except Exception:
        pass
    try:
        v = int(pt.vlen)
        if v > 1:
            d["vlen"] = v
    except Exception:
        pass
    if getattr(pt, "optional", False):
        d["optional"] = True
    return d


def _tidy_doc(text: str, limit: int = 280) -> str:
    """First useful paragraph(s) of a docstring / GRC documentation, on one line."""
    t = str(text or "").replace("SDR Pilot (AI-built)", "")
    t = re.sub(r"(?m)^\s*\S+[\\/]\S+\.(py|yml)\s*$", "", t)  # GRC appends the hier block's file path
    t = re.split(r"Constructor Specific Documentation|\n\s*Args:|\n\s*Parameters\b", t)[0]
    paras = [" ".join(x.split()) for x in re.split(r"\n\s*\n", t)]
    paras = [x for x in paras if x and not re.match(r"^[\w.]+\(.*\)$", x)]  # drop bare signatures
    out = ""
    for x in paras:
        if len(out) + len(x) > limit and out:
            break
        out = (out + " " + x).strip()
    return out if len(out) <= limit else out[:limit - 1].rsplit(" ", 1)[0] + "…"


def _python_doc(bt) -> str:
    """The docstring of the Python/C++ class a GRC block makes (GRC's own docs are often empty)."""
    key = str(getattr(bt, "key", ""))
    if key in _PYDOC_CACHE:
        return _PYDOC_CACHE[key]
    doc = ""
    try:
        t = bt.templates
        make = str(t.get("make") or "").strip()
        m = re.match(r"(?:self\.)?([A-Za-z_][\w.]*)(\$\{[^}]*\})?", make)
        if m:
            ns: dict = {}
            for line in str(t.get("imports") or "").splitlines():
                line = line.strip()
                if line.startswith(("from gnuradio", "import gnuradio", "from gnuradio.", "import ")) and "${" not in line:
                    try:
                        exec(line, ns)  # noqa: S102 - the block's own import line, from installed block files
                    except Exception:
                        pass
            parts = m.group(1).split(".")
            obj = ns.get(parts[0])
            for a in (parts[1:-1] if m.group(2) else parts[1:]):
                obj = getattr(obj, a, None)
            if m.group(2) and obj is not None:
                cands = sorted(n for n in dir(obj) if n.startswith(parts[-1]))
                obj = getattr(obj, cands[0], None) if cands else None
            doc = _tidy_doc(getattr(obj, "__doc__", "") or "") if obj is not None else ""
    except Exception:
        doc = ""
    _PYDOC_CACHE[key] = doc
    return doc


def epy_docstring(code: str) -> str:
    """The docstring of an epy block's class (what the AI wrote it for), or ''."""
    import ast
    try:
        tree = ast.parse(str(code))
    except SyntaxError:
        return ""
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            d = ast.get_docstring(node) or ""
            if d and not d.lower().startswith(("embedded python block", "docstring")):
                return _tidy_doc(d)
    return ""


def _ai_made_hier(bt) -> bool:
    from . import library
    key = str(getattr(bt, "key", ""))
    doc = (getattr(bt, "documentation", None) or {}).get("", "") if isinstance(getattr(bt, "documentation", None), dict) else ""
    return "SDR Pilot (AI-built)" in str(doc) and key not in library.specs()


def block_summary(blk_or_type, spec_doc: str = "") -> tuple[str, str]:
    """(one-line description, who wrote it): the AI's own description for this block if it gave one, else the
    block's documentation. who = 'ai' / 'gnuradio' / 'sdr pilot' / '' (nothing known)."""
    if spec_doc:
        return _tidy_doc(spec_doc, 400), "ai"
    bt = blk_or_type
    key = str(getattr(bt, "key", ""))
    if key == "epy_block":
        try:
            d = epy_docstring(bt.params["_source_code"].get_value())
        except Exception:
            d = ""
        return (d, "ai") if d else ("", "")
    docs = getattr(bt, "documentation", None)
    raw = docs.get("", "") if isinstance(docs, dict) else str(docs or "")
    if "SDR Pilot (AI-built)" in raw:
        d = _tidy_doc(raw, 400)
        return (d, "ai" if _ai_made_hier(bt) else "sdr pilot") if d else ("", "")
    if key in BLOCK_SUMMARIES:
        return BLOCK_SUMMARIES[key], "gnuradio"
    d = _tidy_doc(raw) or _python_doc(bt)
    return (d, "gnuradio") if d else ("", "")


def missing_docs(spec: dict) -> list[str]:
    """Blocks the AI wrote itself (Python blocks, its own hier blocks) that have no description yet."""
    p = platform()
    out = []
    for b in spec.get("blocks") or []:
        if b.get("doc") or b.get("description"):
            continue
        t = b.get("type")
        if t == "epy_block":
            if not epy_docstring((b.get("params") or {}).get("_source_code", "")):
                out.append(b.get("name"))
        elif t in p.blocks and _ai_made_hier(p.blocks[t]):
            docs = getattr(p.blocks[t], "documentation", None) or {}
            if not _tidy_doc(docs.get("", "") if isinstance(docs, dict) else ""):
                out.append(b.get("name"))
    return [n for n in out if n]


def describe_flowgraph(grc_path: str) -> dict:
    """Blocks (position, label, visible params, ports) + connections + errors, for drawing it like GRC does."""
    mtime = os.path.getmtime(grc_path)
    hit = _DESCRIBE_CACHE.get(grc_path)
    if hit and hit[0] == mtime:
        return hit[1]
    with GRC_LOCK:
        p = platform()
        fg = p.make_flow_graph()
        fg.import_data(p.parse_flow_graph(grc_path))
        fg.rewrite()
        fg.validate()
        blocks = []
        errors_by_block: dict[str, list[str]] = {}
        open_by_block: dict[str, list[str]] = {}
        other_errors: list[str] = []
        for elem, msg in fg.iter_error_messages():
            if getattr(elem, "is_port", False) and str(msg).strip() == OPEN_PORT_MSG:
                try:
                    open_by_block.setdefault(elem.parent_block.name, []).append(
                        ("out:" if elem.is_source else "in:") + str(elem.key))
                    continue
                except Exception:
                    pass
            blk = None
            for attr in ("parent_block",):
                blk = getattr(elem, attr, None)
            if getattr(elem, "is_block", False):
                blk = elem
            if getattr(elem, "is_connection", False):
                blk = elem.sink_block
            if blk is not None:
                errors_by_block.setdefault(blk.name, []).append(str(msg))
            else:
                other_errors.append(str(msg))
        title = ""
        for b in fg.blocks:
            if b.key == "options":
                title = str(b.params["title"].get_value()) if "title" in b.params else ""
                continue
            coord = b.states.get("coordinate") or (0, 0)
            params = []
            for pp in b.params.values():
                if pp.key in ("id", "comment", "alias", "affinity", "minoutbuf", "maxoutbuf", "_source_code"):
                    continue
                try:
                    hide = pp.hide
                except Exception:
                    hide = "none"
                if hide != "none":
                    continue
                params.append([str(pp.name), _short_val(pp.get_value())])
            label = str(b.label)
            if b.key == "epy_block":
                m = re.search(r"\bname\s*=\s*[\"']([^\"']+)", str(b.params["_source_code"].get_value()))
                label = "Python: " + (m.group(1) if m else "custom block")
            try:
                summ, who = block_summary(b)
            except Exception:  # pragma: no cover
                summ, who = "", ""
            blocks.append({
                "name": b.name, "type": b.key, "label": label, "x": float(coord[0]), "y": float(coord[1]),
                "doc": summ, "doc_by": who, "category": str(getattr(b, "category", "") or "").strip("[]'"),
                "enabled": b.state == "enabled" if hasattr(b, "state") else True,
                "variable": bool(getattr(b, "is_variable", False)) or b.key in ("parameter", "import"),
                "params": params[:8],
                "inputs": [_port_info(pt) for pt in b.sinks if not getattr(pt, "hidden", False)],
                "outputs": [_port_info(pt) for pt in b.sources if not getattr(pt, "hidden", False)],
                "errors": errors_by_block.get(b.name, [])[:3],
                "open": open_by_block.get(b.name, []),
                "note": _short_val(b.params["comment"].get_value(), 90) if "comment" in b.params else "",
            })
        conns = []
        for c in fg.connections:
            try:
                conns.append({"src": c.source_block.name, "src_port": str(c.source_port.key),
                              "dst": c.sink_block.name, "dst_port": str(c.sink_port.key),
                              "domain": str(c.source_port.domain), "dtype": str(c.source_port.dtype)})
            except Exception:
                pass
        out = {"path": grc_path, "mtime": mtime, "title": title, "valid": fg.is_valid(), "blocks": blocks,
               "connections": conns, "errors": other_errors[:5],
               "open_ports": sum(len(v) for v in open_by_block.values())}
    _DESCRIBE_CACHE[grc_path] = (mtime, out)
    return out
