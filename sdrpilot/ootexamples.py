"""Make an installed out-of-tree module easy to use: its example flowgraphs become SDR Pilot recipes and its example
hier blocks get compiled into GRC's block library.

Most GNU Radio modules ship examples/*.grc (gr-lte, gr-satellites, gr-ieee802-11, ...). Those are the authors' own
working receivers - the best starting point an AI can get. After an install SDR Pilot:
  * compiles the hierarchical-block examples (options -> generate_options 'hb') into the hier-block folder, so the
    top-level examples that use them can be built (old 3.7 XML .grc files are converted by GRC itself);
  * turns every top-level example into a recipe named <module>_<example> (hardware sources such as USRP / osmosdr /
    file sources replaced by SDR Pilot's live radio source), marked "imported example, not tested yet".
"""
from __future__ import annotations

import glob
import json
import os
import re
import time

HW_SOURCE_PREFIXES = ("uhd_usrp_source", "osmosdr_source", "soapy_", "rtlsdr_source", "iio_pluto_source",
                      "iio_fmcomms2_source", "limesdr_source", "hackrf_source", "airspy_source", "sdrplay")
FILE_SOURCES = ("blocks_file_source", "blocks_file_meta_source")
GUI_SINK_PREFIX = "qtgui_"
SKIP_DIRS = ("build", ".git", "_archive", "__pycache__")


def find_grc_files(roots: list[str], limit: int = 80) -> list[str]:
    out = []
    for root in roots:
        if not root or not os.path.isdir(root):
            continue
        for dp, dn, fn in os.walk(root):
            dn[:] = [d for d in dn if d not in SKIP_DIRS]
            for f in fn:
                if f.endswith(".grc"):
                    out.append(os.path.join(dp, f))
            if len(out) >= limit:
                break
    # examples folders first, then anything else (apps/, grc test flowgraphs...)
    out.sort(key=lambda p: (0 if os.sep + "examples" in p else 1 if os.sep + "apps" in p else 2, len(p), p))
    return out[:limit]


def example_roots(module: str, src_dirs: list[str], prefix: str) -> list[str]:
    roots = [os.path.join(d, sub) for d in src_dirs if d for sub in ("examples", "apps", "flowgraphs") if
             os.path.isdir(os.path.join(d, sub))]
    for share in (os.path.join(prefix, "share"), os.path.join(prefix, "Library", "share")):
        for pat in (f"gr-{module}*", f"gnuradio-{module}*", f"{module}*", os.path.join("gnuradio", "examples", f"*{module}*"),
                    os.path.join("doc", f"gr-{module}*")):
            roots += [p for p in glob.glob(os.path.join(share, pat)) if os.path.isdir(p)]
    seen, out = set(), []
    for r in roots:
        k = os.path.normcase(os.path.realpath(r))
        if k not in seen:
            seen.add(k)
            out.append(r)
    return out


def _parse(platform, path: str) -> dict | None:
    try:
        return platform.parse_flow_graph(path)
    except Exception:
        return None


# bump when fixed_copy learns a new fix: modules whose examples were imported with an older fixer are redone
FIXER_VERSION = 2
_QUOTED_NAME = re.compile(r"""^\s*(['"])([A-Za-z_]\w*(?:\.\w+)*)\1\s*$""")

PARAM_TYPES = {"string": "str", "int": "intx", "real": "eng_float", "float": "eng_float", "raw": "",
               "complex": "complex", "long": "long"}


def _plain(o):
    if isinstance(o, dict):
        return {str(k): _plain(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_plain(x) for x in o]
    return o


def fixed_copy(platform, path: str, module: str, out_dir: str) -> str:
    """Write a GNU Radio 3.10-ready copy of an example flowgraph (YAML) and return its path: 3.7 XML converted by
    GRC, `import lte` -> `from gnuradio import lte` (3.7 modules were top-level packages), 3.7 parameter types
    (string/int/real) mapped to 3.10's. The original stays untouched."""
    import yaml
    data = _plain(platform.parse_flow_graph(path))
    mod = re.escape(module)
    names = {str(b.get("name")) for b in data.get("blocks", []) if b.get("name")}
    for b in data.get("blocks", []):
        ps = b.get("parameters") or {}
        if b.get("id") == "import" and ps.get("imports"):
            imp = str(ps["imports"])
            imp = re.sub(rf"^\s*import\s+{mod}\s*$", f"from gnuradio import {module}", imp, flags=re.M)
            imp = re.sub(rf"^\s*import\s+{mod}\s+as\s+(\w+)\s*$", rf"from gnuradio import {module} as \1", imp,
                         flags=re.M)
            imp = re.sub(rf"^\s*from\s+{mod}\s+import\s+", f"from gnuradio.{module} import ", imp, flags=re.M)
            ps["imports"] = imp
        # 3.7 evaluated "4, 1" as a tuple; 3.10 pastes it into make() as two arguments - vectors get brackets
        B = platform.blocks.get(b.get("id"))
        if B is not None:
            for pd in getattr(B, "parameters_data", []) or []:
                k, dt = pd.get("id"), str(pd.get("dtype", ""))
                v = ps.get(k)
                if dt.endswith("_vector") and isinstance(v, str) and "," in v and not re.match(r"^\s*[\[(]", v):
                    ps[k] = "[" + v.strip() + "]"
                # a reference to another block or variable that old GRC stored quoted ('"sync_ffofrac_sig"'): 3.10
                # pastes raw values as-is, so make(self.${sig}) became self."sync_ffofrac_sig" - a SyntaxError
                m = _QUOTED_NAME.match(v) if isinstance(v, str) and dt in ("raw", "") else None
                if m:
                    make = ""
                    try:
                        make = str((getattr(B, "templates", None) or {}).get("make", "") or "")
                    except Exception:
                        pass
                    if m.group(2).split(".")[0] in names or re.search(r"self\.\$\{?\s*" + re.escape(k) + r"\b", make):
                        ps[k] = m.group(2)
        if b.get("id") == "parameter" and str(ps.get("type", "")) in PARAM_TYPES:
            ps["type"] = PARAM_TYPES[str(ps["type"])]
            if ps["type"] == "str" and ps.get("value") and not re.match(r"^['\"]", str(ps["value"])):
                ps["value"] = str(ps["value"])
    data.setdefault("metadata", {})["file_format"] = 1
    os.makedirs(out_dir, exist_ok=True)
    dst = os.path.join(out_dir, os.path.splitext(os.path.basename(path))[0] + ".grc")
    with open(dst, "w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, sort_keys=False, allow_unicode=True)
    return dst


def _options(data: dict) -> dict:
    return ((data or {}).get("options") or {}).get("parameters") or {}


def compile_hier_blocks(grc, paths: list[str], hier_dir: str, log=None) -> tuple[list[str], list[str]]:
    """Compile the 'hb' flowgraphs among paths into hier_dir (several passes: hier blocks may use each other)."""
    p = grc.platform()
    hbs = []
    for path in paths:
        data = _parse(p, path)
        if data and str(_options(data).get("generate_options", "")).startswith("hb"):
            hbs.append(path)
    done, failed = [], []
    pending = list(hbs)
    for _pass in range(4):
        progress = False
        for path in list(pending):
            try:
                with grc.GRC_LOCK:
                    old = p.config.hier_block_lib_dir
                    p.config.hier_block_lib_dir = hier_dir
                    try:
                        fg, out = p.load_and_generate_flow_graph(os.path.abspath(path), os.path.abspath(hier_dir))
                    finally:
                        p.config.hier_block_lib_dir = old
                if out:
                    done.append(os.path.basename(path))
                    pending.remove(path)
                    progress = True
            except Exception as e:  # a hier block that needs another one not compiled yet - next pass
                if log:
                    log(f"hier block {os.path.basename(path)}: {e}")
        if progress:
            with grc.GRC_LOCK:
                p.build_library()
                grc._DESCRIBE_CACHE.clear()
        if not pending or not progress:
            break
    failed = [os.path.basename(x) for x in pending]
    return done, failed


def _recipe_name(module: str, path: str) -> str:
    base = re.sub(r"[^A-Za-z0-9_]+", "_", os.path.splitext(os.path.basename(path))[0]).strip("_").lower()
    base = re.sub(rf"^(gr_)?{re.escape(module.lower())}_?", "", base) or "example"
    return f"{module.lower()}_example_{base}"[:60]


def import_examples(grc, module: str, paths: list[str], recipe_dir: str, log=None) -> list[dict]:
    """Turn the top-level example flowgraphs into recipes. Returns [{recipe, file, about, hardware_replaced...}]."""
    p = grc.platform()
    os.makedirs(recipe_dir, exist_ok=True)
    out = []
    for path in paths:
        data = _parse(p, path)
        if not data:
            continue
        opts = _options(data)
        if str(opts.get("generate_options", "")).startswith("hb"):
            continue
        try:
            spec = grc.load_flowgraph(path)["spec"]
        except Exception as e:
            if log:
                log(f"example {os.path.basename(path)}: {e}")
            continue
        name = _recipe_name(module, path)
        spec["name"] = name
        # examples often keep alternative inputs around disabled (a file source, a throttle, a noise adder):
        # only what was enabled is the receiver
        off = {b["name"] for b in spec.get("blocks", []) if b.get("disabled")}
        if off:
            spec["blocks"] = [b for b in spec["blocks"] if b["name"] not in off]
            spec["connections"] = [c for c in spec.get("connections", [])
                                   if not any(re.match(rf"\s*{re.escape(n)}\s*:", part)
                                              for n in off for part in c.split("->"))]
        # headless: Qt GUI windows don't belong in an SDR Pilot job (the dashboard shows the data); message
        # debug printers become sp_data_sink, so decoded messages reach the job summary and the Decoded view
        qt = {b["name"] for b in spec.get("blocks", []) if str(b.get("type", "")).startswith(GUI_SINK_PREFIX)}
        dbg = {b["name"] for b in spec.get("blocks", []) if b.get("type") == "blocks_message_debug"}
        if qt:
            spec["blocks"] = [b for b in spec["blocks"] if b["name"] not in qt]
            spec["connections"] = [c for c in spec.get("connections", [])
                                   if not any(re.match(rf"\s*{re.escape(n)}\s*:", part)
                                              for n in qt for part in c.split("->"))]
        spec["gui"] = False
        for b in spec.get("blocks", []):
            if b["name"] in dbg:
                b["type"], b["params"] = "sp_data_sink", {"kind": f"'{module}'"}
        if dbg:
            fixed = []
            for c in spec.get("connections", []):
                src, _, dst = c.partition("->")
                for n in dbg:
                    dst = re.sub(rf"^(\s*{re.escape(n)}\s*:)\s*(print|store|print_pdu|log)\s*$", r"\1in", dst)
                fixed.append(src + "->" + dst)
            spec["connections"] = fixed
        replaced, missing = [], []
        has_hw = any(b.get("type", "").startswith(HW_SOURCE_PREFIXES) for b in spec.get("blocks", []))
        for b in spec.get("blocks", []):
            t = b.get("type", "")
            is_iq_file = t in FILE_SOURCES and "complex" in json.dumps(b.get("params", {})).lower()
            if t.startswith(HW_SOURCE_PREFIXES) or (is_iq_file and not has_hw):
                # the radio is SDRangel's - an example's own SDR (or its recorded-IQ file) becomes the live radio
                replaced.append(f"{b['name']} ({t})")
                b["type"], b["params"] = "sdr_source", {}
                has_hw = True
            elif t not in p.blocks and t not in grc.PSEUDO_BLOCKS:
                missing.append(t)
        # a GUI example still builds; SDR Pilot's dashboard shows the data instead of the Qt windows
        vars_ = spec.get("variables") or {}
        rate = vars_.get("samp_rate") or vars_.get("sample_rate")
        title = str(opts.get("title") or "").strip() or os.path.basename(path)
        desc = str(opts.get("description") or "").strip()
        about = (f"Example '{title}' shipped with the {module} module (imported automatically, NOT tested yet)"
                 + (f": {desc}" if desc else "") + ".")
        notes = []
        if replaced:
            notes.append(f"its source(s) {replaced} were replaced by the live radio (sdr_source)")
        if rate:
            notes.append(f"it was written for samp_rate = {rate} - radio_tune(sample_rate=...) to match first")
        if missing:
            notes.append(f"uses block types that aren't installed: {sorted(set(missing))[:8]}")
        if qt:
            notes.append(f"its {len(qt)} Qt GUI display(s) were removed (runs headless; use plot / spectrum tools)")
        if dbg:
            notes.append("message debug outputs go to sp_data_sink (decoded data in the job summary)")
        if notes:
            about += " Notes: " + "; ".join(notes) + "."
        words = sorted({w for w in re.split(r"[^a-z0-9]+", (title + " " + desc + " " + os.path.basename(path)).lower())
                        if len(w) > 2} | {module.lower()})
        rec = {"_about": about, "use_when": f"{module} signals - the module authors' own example ({title})",
               "keywords": words[:20], "variables_help": {k: "from the example" for k in list(vars_)[:12]},
               "origin": {"kind": "oot_example", "module": module, "file": path, "imported": time.ctime()},
               "requires": sorted({b["type"] for b in spec.get("blocks", []) if b["type"].lower().startswith(module.lower())}),
               "spec": spec}
        with open(os.path.join(recipe_dir, name + ".json"), "w", encoding="utf-8") as f:
            json.dump(rec, f, indent=1, default=str)
        n_mod = sum(1 for b in spec.get("blocks", []) if b["type"].lower().startswith(module.lower() + "_"))
        out.append({"recipe": name, "file": os.path.basename(path), "title": title, "module_blocks_used": n_mod,
                    **({"missing_blocks": sorted(set(missing))[:8]} if missing else {}),
                    **({"sources_replaced": replaced} if replaced else {})})
    return out


def fix_installed_block_imports(platform, module: str, log=None) -> list[str]:
    """Block files of a module installed the 3.7/3.8 way say `import lte`, but on 3.10 the package is
    gnuradio.lte: every flowgraph using them would fail with "No module named 'lte'". Fix those files in place."""
    import importlib.util
    try:
        top = importlib.util.find_spec(module) is not None
    except (ImportError, ValueError):
        top = False
    try:
        nested = importlib.util.find_spec(f"gnuradio.{module}") is not None
    except (ImportError, ValueError):
        nested = False
    if top or not nested:
        return []
    from .ootbuild import fix_block_imports
    fixed = []
    for d in list(getattr(platform.config, "block_paths", []) or []):
        for f in glob.glob(os.path.join(d, f"{module}_*.yml")) + glob.glob(os.path.join(d, f"{module}.*.yml")):
            try:
                with open(f, encoding="utf-8") as fh:
                    old = fh.read()
                new = fix_block_imports(old, module)
                if new != old:
                    with open(f, "w", encoding="utf-8") as fh:
                        fh.write(new)
                    fixed.append(os.path.basename(f))
            except OSError as e:
                if log:
                    log(f"couldn't fix {f}: {e}")
    return fixed


def make_usable(grc, module: str, src_dirs: list[str], prefix: str, hier_dir: str, recipe_dir: str,
                log=None) -> dict:
    """Everything after an install: reload GRC's block list, compile example hier blocks, import examples."""
    p = grc.platform()
    before = set(p.blocks)
    fixed_imports = fix_installed_block_imports(p, module, log)
    with grc.GRC_LOCK:
        p.build_library()
        grc._DESCRIBE_CACHE.clear()
    roots = example_roots(module, src_dirs, prefix)
    files = []
    fixed_dir = os.path.join(recipe_dir, "_examples", module)
    for f in find_grc_files(roots):
        try:
            files.append(fixed_copy(p, f, module, fixed_dir))
        except Exception as e:  # unreadable / not a flowgraph: skip it
            if log:
                log(f"{os.path.basename(f)}: {e}")
    hb_done, hb_failed = compile_hier_blocks(grc, files, hier_dir, log) if files else ([], [])
    recipes = import_examples(grc, module, files, recipe_dir, log) if files else []
    # the module's real receivers first: complete ones (nothing missing) that use the most of the module's blocks
    recipes.sort(key=lambda r: (bool(r.get("missing_blocks")), -r.get("module_blocks_used", 0), r["recipe"]))
    mod_blocks = sorted(k for k in p.blocks if k.lower().startswith(module.lower() + "_") or k.lower() == module.lower())
    return {"module": module, "blocks": mod_blocks[:120], "new_blocks": sorted(set(p.blocks) - before)[:120],
            **({"block_files_fixed": fixed_imports} if fixed_imports else {}),
            "example_files": len(files), "hier_blocks_compiled": hb_done, "hier_blocks_failed": hb_failed,
            "recipes_from_examples": recipes}
