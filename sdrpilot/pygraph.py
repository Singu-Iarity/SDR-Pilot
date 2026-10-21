"""Diagrams for hand-written GNU Radio Python flowgraphs (gnuradio_write_python), so the dashboard can draw them
like GRC flowgraphs.

Two sources, merged:
- static: the script's code (ast) - which blocks it creates (self.x = module.block(args)) and how it connects them
  (self.connect / self.msg_connect). Works before the script has ever run.
- runtime: sdrpilot.runner records every connect() the top block really makes, with each port's item size, and
  saves it as sdrpilot_graph.json next to the script. Exact, including loops and computed connections.
"""
from __future__ import annotations

import ast
import json
import os

GRAPH_FILE = "sdrpilot_graph.json"
SIZE_DTYPE = {8: "complex", 4: "float", 2: "short", 1: "byte"}


# ------------------------------------------------------------------ runtime capture (used by runner.py)
class ConnectRecorder:
    """Patches gr.hier_block2.connect / msg_connect to record the top block's connections."""

    def __init__(self, gr):
        self.gr = gr
        self.edges: list[tuple] = []
        self.msg_edges: list[tuple] = []
        self.owner = None
        self._orig = None

    def install(self) -> None:
        hb = self.gr.hier_block2
        orig_connect, orig_msg = hb.connect, hb.msg_connect
        rec = self

        def connect(this, *points):
            try:
                if rec.owner is None or this is rec.owner:
                    pts = [p if isinstance(p, tuple) else (p, 0) for p in points]
                    for (a, pa), (b, pb) in zip(pts, pts[1:]):
                        rec.edges.append((this, a, int(pa), b, int(pb)))
            except Exception:
                pass
            return orig_connect(this, *points)

        def msg_connect(this, *args):
            try:
                if rec.owner is None or this is rec.owner:
                    if len(args) == 4:
                        rec.msg_edges.append((this, args[0], str(args[1]), args[2], str(args[3])))
                    elif len(args) == 2:
                        (a, pa), (b, pb) = args
                        rec.msg_edges.append((this, a, str(pa), b, str(pb)))
            except Exception:
                pass
            return orig_msg(this, *args)

        self._orig = (orig_connect, orig_msg)
        hb.connect, hb.msg_connect = connect, msg_connect

    def uninstall(self) -> None:
        if self._orig:
            self.gr.hier_block2.connect, self.gr.hier_block2.msg_connect = self._orig

    def save(self, tb, folder: str) -> str | None:
        """Write sdrpilot_graph.json for top block `tb`."""
        names: dict[int, str] = {}
        for k, v in vars(tb).items():
            if hasattr(v, "input_signature") or hasattr(v, "output_signature"):
                names.setdefault(id(v), k)
        blocks: dict[str, dict] = {}

        def bname(b) -> str:
            if b is tb:
                return "self"
            n = names.get(id(b))
            if not n:
                try:
                    n = b.alias() or b.name()
                except Exception:
                    n = type(b).__name__
                names[id(b)] = n
            if n not in blocks:
                info = {"type": f"{type(b).__module__.split('.')[1] if type(b).__module__.startswith('gnuradio.') else ''}"
                                f"{'.' if type(b).__module__.startswith('gnuradio.') else ''}{type(b).__name__}",
                        "gr_name": _safe(lambda: b.name()), "in": {}, "out": {}}
                blocks[n] = info
            return n

        conns = []
        for owner, a, pa, b, pb in self.edges:
            if owner is not tb:
                continue
            an, bn = bname(a), bname(b)
            size = _safe(lambda: a.output_signature().sizeof_stream_item(pa)) or \
                _safe(lambda: b.input_signature().sizeof_stream_item(pb))
            dt = SIZE_DTYPE.get(size, f"{size}B" if size else "")
            if an in blocks:
                blocks[an]["out"][str(pa)] = dt
            if bn in blocks:
                blocks[bn]["in"][str(pb)] = dt
            conns.append({"src": an, "src_port": str(pa), "dst": bn, "dst_port": str(pb), "dtype": dt, "domain": "stream"})
        for owner, a, pa, b, pb in self.msg_edges:
            if owner is not tb:
                continue
            an, bn = bname(a), bname(b)
            blocks[an]["out"][pa] = "message"
            blocks[bn]["in"][pb] = "message"
            conns.append({"src": an, "src_port": pa, "dst": bn, "dst_port": pb, "dtype": "message", "domain": "message"})
        path = os.path.join(folder, GRAPH_FILE)
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"top_block": type(tb).__name__, "blocks": blocks, "connections": conns}, f, indent=1)
            return path
        except OSError:
            return None


def _safe(fn):
    try:
        return fn()
    except Exception:
        return None


# ------------------------------------------------------------------ static analysis
def _endpoint(node) -> tuple[str, str] | None:
    """self.x / x / (self.x, 1) / (x, 'port') -> (name, port)."""
    port = "0"
    if isinstance(node, ast.Tuple) and len(node.elts) == 2:
        node, p = node.elts
        if isinstance(p, ast.Constant):
            port = str(p.value)
        else:
            port = _unparse(p, 12)
    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "self":
        return node.attr, port
    if isinstance(node, ast.Name):
        return ("self" if node.id == "self" else node.id), port
    return None


def _unparse(node, n: int = 40) -> str:
    try:
        s = ast.unparse(node)
    except Exception:
        s = "…"
    s = " ".join(s.split())
    return s if len(s) <= n else s[: n - 1] + "…"


def static_graph(code: str) -> dict:
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        return {"error": f"syntax error line {e.lineno}: {e.msg}", "blocks": {}, "connections": []}
    classes = {c.name for c in ast.walk(tree) if isinstance(c, ast.ClassDef)}
    blocks: dict[str, dict] = {}
    conns: list[dict] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call) and len(node.targets) == 1:
            ep = _endpoint(node.targets[0]) if not isinstance(node.targets[0], ast.Tuple) else None
            if not ep:
                continue
            fn = _unparse(node.value.func, 60)
            last = fn.split(".")[-1]
            looks_block = fn in classes or "." in fn and fn.split(".")[0] in (
                "blocks", "analog", "digital", "filter", "fft", "zeromq", "audio", "channels", "pdu", "network",
                "qtgui", "soapy", "osmosdr", "uhd", "dtv", "trellis", "vocoder", "wavelet", "iio", "fg",
                "ieee802_15_4", "adsb", "satellites", "rds", "lora_sdr") or last in ("sdr_source", "capture_source")
            if not looks_block:
                continue
            params = []
            for i, a in enumerate(node.value.args[:6]):
                params.append([f"arg{i}", _unparse(a)])
            for kw in node.value.keywords[:6]:
                params.append([kw.arg or "**", _unparse(kw.value)])
            blocks[ep[0]] = {"type": fn, "params": params[:7]}
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and \
                node.func.attr in ("connect", "msg_connect"):
            args = node.args
            if node.func.attr == "msg_connect":
                if len(args) == 4:
                    a, b = _endpoint(args[0]), _endpoint(args[2])
                    if a and b:
                        conns.append({"src": a[0], "src_port": _unparse(args[1], 16).strip("'\""), "dst": b[0],
                                      "dst_port": _unparse(args[3], 16).strip("'\""), "dtype": "message",
                                      "domain": "message"})
                continue
            eps = [_endpoint(a) for a in args]
            if any(e is None for e in eps) or len(eps) < 2:
                continue
            for (a, pa), (b, pb) in zip(eps, eps[1:]):
                conns.append({"src": a, "src_port": pa, "dst": b, "dst_port": pb, "dtype": "", "domain": "stream"})
    return {"blocks": blocks, "connections": conns}


# ------------------------------------------------------------------ describe (dashboard format)
def _label(t: str) -> str:
    if "." not in t:  # a class defined in the script itself
        return "Python: " + t
    last = t.split(".")[-1]
    return " ".join(w.capitalize() if w.islower() else w for w in last.replace("_", " ").split()) or t


def describe(py_path: str, name: str = "") -> dict:
    folder = os.path.dirname(py_path)
    try:
        with open(py_path, encoding="utf-8") as f:
            code = f.read()
    except OSError as e:
        return {"error": f"can't read {py_path}: {e}"}
    st = static_graph(code)
    rt = None
    gp = os.path.join(folder, GRAPH_FILE)
    if os.path.exists(gp) and os.path.getmtime(gp) >= os.path.getmtime(py_path) - 1:
        try:
            with open(gp, encoding="utf-8") as f:
                rt = json.load(f)
        except (OSError, ValueError):
            rt = None
    conns = (rt or {}).get("connections") or st["connections"]
    names = []
    for c in conns:
        for n in (c["src"], c["dst"]):
            if n not in names and n != "self":
                names.append(n)
    for n in st["blocks"]:  # blocks created but not (visibly) connected
        if n not in names:
            names.append(n)
    if not names:
        return {"error": st.get("error") or "hand-written Python flowgraph - no connect() calls found to draw",
                "kind": "python", "name": name}
    rtb = (rt or {}).get("blocks") or {}
    blocks = {}
    for n in names:
        sb, rb = st["blocks"].get(n, {}), rtb.get(n, {})
        typ = sb.get("type") or rb.get("type") or n
        ins = {c["dst_port"]: c["dtype"] for c in conns if c["dst"] == n}
        outs = {c["src_port"]: c["dtype"] for c in conns if c["src"] == n}
        ins.update(rb.get("in") or {})
        outs.update(rb.get("out") or {})

        def ports(d):
            return [{"key": k, "dtype": v or "", "domain": "message" if v == "message" else "stream"}
                    for k, v in sorted(d.items(), key=lambda kv: (not kv[0].isdigit(), int(kv[0]) if kv[0].isdigit() else 0, kv[0]))]
        blocks[n] = {"name": n, "type": typ, "label": _label(typ), "params": sb.get("params", []),
                     "inputs": ports(ins), "outputs": ports(outs), "variable": False, "enabled": True, "errors": []}
    # fill connection dtypes from the block ports we now know
    for c in conns:
        if not c.get("dtype"):
            c["dtype"] = next((p["dtype"] for p in blocks.get(c["src"], {}).get("outputs", []) if p["key"] == c["src_port"]), "")
    _layout(blocks, conns)
    return {"name": name, "kind": "python", "title": name, "valid": not st.get("error"),
            "errors": [st["error"]] if st.get("error") else [], "blocks": list(blocks.values()),
            "connections": [c for c in conns if c["src"] in blocks and c["dst"] in blocks],
            "source": "from its last run" if rt else "from its code (exact once it has run)"}


def _size(b: dict) -> tuple[float, float]:
    lines = [b["label"]] + [f"{p[0]}: {p[1]}" for p in b["params"]]
    w = max(150, min(390, max(len(t) for t in lines) * 6.6 + 26))
    np_ = max(len(b["inputs"]), len(b["outputs"]))
    h = max(30 + len(b["params"]) * 15 + 10, 18 + np_ * 20 + 6, 50)
    return w, h


def _layout(blocks: dict, conns: list) -> None:
    """Columns by longest path from the sources, GRC-ish left-to-right."""
    preds: dict[str, set] = {n: set() for n in blocks}
    for c in conns:
        if c["src"] in blocks and c["dst"] in blocks and c["src"] != c["dst"]:
            preds[c["dst"]].add(c["src"])
    depth: dict[str, int] = {}

    def d(n, seen=()):
        if n in depth:
            return depth[n]
        if n in seen:
            return 0
        depth[n] = 0 if not preds[n] else 1 + max(d(p, seen + (n,)) for p in preds[n])
        return depth[n]

    for n in blocks:
        d(n)
    cols: dict[int, list] = {}
    for n in blocks:
        cols.setdefault(depth[n], []).append(n)
    x = 20.0
    for k in sorted(cols):
        y = 20.0
        wmax = 0.0
        for n in cols[k]:
            w, h = _size(blocks[n])
            blocks[n]["x"], blocks[n]["y"] = x, y
            y += h + 40
            wmax = max(wmax, w)
        x += wmax + 70
