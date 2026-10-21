"""SDR Pilot's block library: tested GNU Radio hierarchical blocks (blocks/*.json) installed into GRC's
hier-block folder (~/.grc_gnuradio), so AIs - and the user in GNU Radio Companion, category "SDR Pilot" - can
use whole receivers/demodulators as single blocks: sp_wbfm_receiver, sp_rds_decoder, sp_fsk_demod, ...

Each spec is rebuilt only when it changed (hash marker next to the generated block), and only if the blocks it
needs exist on this machine (e.g. sp_rds_decoder needs gr-rds)."""
from __future__ import annotations

import hashlib
import json
import logging
import os
import time

from . import grc

log = logging.getLogger("sdrpilot.library")
HERE = os.path.dirname(os.path.abspath(__file__))
BLOCKS_DIR = os.path.join(os.path.dirname(HERE), "blocks")
STATUS: dict = {"state": "not started", "built": [], "skipped": {}, "errors": {}}


def specs() -> dict:
    out = {}
    if os.path.isdir(BLOCKS_DIR):
        for fn in sorted(os.listdir(BLOCKS_DIR)):
            if fn.endswith(".json"):
                try:
                    with open(os.path.join(BLOCKS_DIR, fn), encoding="utf-8") as f:
                        s = json.load(f)
                    out[s.get("name") or fn[:-5]] = s
                except (OSError, ValueError) as e:
                    STATUS["errors"][fn] = f"unreadable: {e}"
    return out


def catalog() -> list[dict]:
    """What the AI should know about each library block."""
    p = grc.platform()
    out = []
    for name, s in specs().items():
        out.append({"type": name, "installed": name in p.blocks, "description": s.get("description", ""),
                    "parameters": s.get("parameters", {}),
                    "inputs": [{"label": i.get("label"), "type": i.get("type")} for i in s.get("inputs", [])],
                    "outputs": [{"label": o.get("label"), "type": o.get("type"), "optional": bool(o.get("optional"))}
                                for o in s.get("outputs", [])]})
    for name, d in PLAIN.items():
        out.append({"type": name, "installed": name in p.blocks, "description": d["description"],
                    "parameters": d["parameters"], "inputs": d["inputs"], "outputs": d["outputs"]})
    return out


# plain (non-hierarchical) blocks: a GRC .block.yml + its Python implementation, copied into the hier-block folder
PLAIN = {"sp_data_sink": {"files": ["sp_data_sink.block.yml", "sp_data_sink_impl.py"],
                          "description": "Decoded-data output: connect any decoder's message output (gr-rds parser "
                                         "'out', gr-adsb decoder 'decoded', PDUs, dicts) to its 'in'. Every message "
                                         "becomes a SPDATA JSON line that SDR Pilot collects - the job summary's "
                                         "'decoded' section, decoded.jsonl and the dashboard's Decoded view. Use it "
                                         "instead of blocks_message_debug.",
                          "parameters": {"kind": "'rds' / 'adsb' / 'aprs' / 'packet' ... (string)",
                                         "key": "field naming one station/aircraft/device, e.g. 'icao' ('' = none)"},
                          "inputs": [{"label": "in", "type": "message"}], "outputs": [],
                          "keywords": ["decoded", "output", "display", "print", "log", "messages"]},
         "sp_convert": {"files": ["sp_convert.block.yml", "sp_convert_impl.py"],
                        "description": "Type converter: any stream type to any other (complex / float / int / short / "
                                       "byte). complex -> real types: mode mag (envelope), mag_squared (power), real, "
                                       "imag or arg (phase); real -> complex puts the value in I. scale multiplies on "
                                       "the way (32767 for float audio -> 16-bit short). SDR Pilot also inserts one "
                                       "automatically when you connect ports of different types.",
                        "parameters": {"in_type": "complex|float|int|short|byte", "out_type": "complex|float|int|short|byte",
                                       "mode": "mag|mag_squared|real|imag|arg (complex input only)",
                                       "scale": "multiplier (default 1.0)", "vlen": "vector length (default 1)"},
                        "inputs": [{"label": "in", "type": "in_type"}], "outputs": [{"label": "out", "type": "out_type"}],
                        "keywords": ["convert", "converter", "type", "complex to float", "float to complex",
                                     "short", "byte", "magnitude", "mismatch"]},
         "sp_radio_control": {"files": ["sp_radio_control.block.yml", "sp_radio_impl.py"],
                              "description": "Lets the flowgraph change the radio while it runs - e.g. follow a "
                                             "frequency hopper (BLE, FHSS) or switch antenna/filters. Message in "
                                             "'cmd': a number sets `param` to `value` (expression of x: 'x', "
                                             "'2402e6 + 2e6*x', 'center_freq + x'); a dict {'center_freq': f, "
                                             "'gain': g}, a pair or 'gain=30' sets those directly. Any radio "
                                             "parameter: center_freq, sample_rate, gain, agc, or an SDRangel device "
                                             "setting (lnaGain, vgaGain, biasT, bandwidth...). update_var keeps a "
                                             "flowgraph variable in step with what was applied.",
                              "parameters": {"param": "radio parameter for plain numbers (default center_freq)",
                                             "value": "expression of x (default 'x')",
                                             "min_interval": "seconds between requests (default 0.25)",
                                             "minimum": "clamp (optional)", "maximum": "clamp (optional)",
                                             "update_var": "flowgraph variable to set to the applied value"},
                              "inputs": [{"label": "cmd", "type": "message"}], "outputs": [],
                              "keywords": ["radio", "retune", "tune", "hop", "frequency hopping", "follow", "gain",
                                           "control", "set frequency", "antenna", "bias"]},
         "sp_radio_param_from_stream": {"files": ["sp_radio_param_from_stream.block.yml", "sp_radio_impl.py"],
                                        "description": "Drives one radio parameter from a float stream: every "
                                                       "`interval` s the stream's mean/max/min/last becomes x and "
                                                       "`param` is set to `value` (expression of x), only when x "
                                                       "moved more than `deadband`. Any radio parameter.",
                                        "parameters": {"param": "radio parameter (default gain)",
                                                       "value": "expression of x (default 'x')",
                                                       "interval": "seconds (default 1)",
                                                       "reduce": "mean|max|min|last", "deadband": "default 0",
                                                       "minimum": "clamp", "maximum": "clamp",
                                                       "update_var": "flowgraph variable to keep in step"},
                                        "inputs": [{"label": "in", "type": "float"}], "outputs": [],
                                        "keywords": ["radio", "gain", "control", "parameter", "from stream",
                                                     "feedback", "set"]},
         "sp_radio_agc": {"files": ["sp_radio_agc.block.yml", "sp_radio_impl.py"],
                          "description": "Closed-loop SDR gain control for a fading signal: input = the signal's "
                                         "level or SNR in dB (complex_to_mag_squared -> moving_average -> "
                                         "nlog10_ff(10)). Outside target_db +/- deadband_db it changes the gain by "
                                         "kp x error (<= max_step, within min_gain..max_gain), then waits `settle` s.",
                          "parameters": {"target_db": "wanted level (dB)", "param": "'gain' or a stage (lnaGain...)",
                                         "kp": "dB gain per dB error (0.5)", "deadband_db": "3",
                                         "max_step": "10", "min_gain": "0", "max_gain": "60", "interval": "1 s",
                                         "settle": "1.5 s"},
                          "inputs": [{"label": "in", "type": "float"}], "outputs": [],
                          "keywords": ["agc", "gain", "automatic gain", "snr", "fading", "level", "keep strong"]}}


def _install_plain(hier_dir: str) -> bool:
    changed = False
    for name, d in PLAIN.items():
        for fn in d["files"]:
            src = os.path.join(BLOCKS_DIR, fn)
            dst = os.path.join(hier_dir, fn)
            try:
                with open(src, "rb") as f:
                    data = f.read()
            except OSError:
                continue
            try:
                with open(dst, "rb") as f:
                    if f.read() == data:
                        continue
            except OSError:
                pass
            with open(dst, "wb") as f:
                f.write(data)
            changed = True
    return changed


def _digest(spec: dict) -> str:
    return hashlib.sha1(json.dumps(spec, sort_keys=True).encode()).hexdigest()[:16]


def ensure(workspace: str) -> dict:
    """Build any missing/outdated library block. Cheap when everything is current."""
    STATUS["state"] = "checking"
    p = grc.platform()
    hier_dir = p.config.hier_block_lib_dir
    os.makedirs(hier_dir, exist_ok=True)
    if _install_plain(hier_dir) or any(n not in p.blocks for n in PLAIN):
        with grc.GRC_LOCK:
            p.build_library()
            grc._DESCRIBE_CACHE.clear()
    todo = []
    for name, s in specs().items():
        missing = [r for r in s.get("requires", []) if r not in p.blocks]
        if missing:
            STATUS["skipped"][name] = f"needs {missing} (not installed)"
            continue
        marker = os.path.join(hier_dir, f"{name}.sdrpilot")
        try:
            with open(marker, encoding="utf-8") as f:
                current = f.read().strip()
        except OSError:
            current = ""
        if current == _digest(s) and name in p.blocks:
            continue
        todo.append((name, s, marker))
    if not todo:
        STATUS["state"] = "ready"
        return STATUS
    lock = os.path.join(workspace, "state", "library_build.lock")
    os.makedirs(os.path.dirname(lock), exist_ok=True)
    try:  # another SDR Pilot copy may be building right now
        if os.path.exists(lock) and time.time() - os.path.getmtime(lock) > 300:
            os.remove(lock)
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.close(fd)
    except FileExistsError:
        for _ in range(120):
            time.sleep(1)
            if not os.path.exists(lock):
                break
        with grc.GRC_LOCK:
            p.build_library()
        STATUS["state"] = "ready (built by another SDR Pilot copy)"
        return STATUS
    try:
        out_dir = os.path.join(workspace, "blocks")
        for name, s, marker in todo:
            spec = {"name": name, "blocks": s["blocks"], "connections": s.get("connections", []),
                    "variables": s.get("variables", {}), "imports": s.get("imports", []), "title": name,
                    "description": s.get("description", ""),
                    "hier": {"parameters": s.get("parameters", {}), "inputs": s.get("inputs", []),
                             "outputs": s.get("outputs", []), "category": "SDR Pilot", "reload": False}}
            try:
                res = grc.build_flowgraph(spec, out_dir, None)
            except Exception as e:  # pragma: no cover
                res = {"valid": False, "errors": [f"{type(e).__name__}: {e}"]}
            if res.get("valid"):
                with open(marker, "w", encoding="utf-8") as f:
                    f.write(_digest(s))
                STATUS["built"].append(name)
            else:
                STATUS["errors"][name] = res.get("errors")
                log.warning("library block %s failed: %s", name, res.get("errors"))
        with grc.GRC_LOCK:
            p.build_library()
            grc._DESCRIBE_CACHE.clear()
    finally:
        try:
            os.remove(lock)
        except OSError:
            pass
    STATUS["state"] = "ready"
    log.info("block library: built %s, skipped %s, errors %s", STATUS["built"], list(STATUS["skipped"]),
             list(STATUS["errors"]))
    return STATUS


def matching(text: str, kw) -> list[dict]:
    """Library blocks whose keywords appear in `text` (kw = the server's word-boundary matcher)."""
    out = []
    try:
        installed = grc.platform().blocks
    except Exception:  # pragma: no cover
        installed = {}
    for name, s in specs().items():
        if kw(text, s.get("keywords") or []):
            out.append({"type": name, "installed": name in installed, "description": s.get("description", "")[:300],
                        "parameters": s.get("parameters", {})})
    for name, d in PLAIN.items():
        if kw(text, d.get("keywords") or []):
            out.append({"type": name, "installed": name in installed, "description": d["description"][:300],
                        "parameters": d["parameters"]})
    return out
