"""Runs one flowgraph in a child process with a graceful, cross-platform stop.

usage: python -m sdrpilot.runner SCRIPT.py [--duration S] [--stop-file PATH]

Imports the script, instantiates its gr.top_block subclass, runs it until it
finishes on its own, the duration elapses, or the stop file appears; then calls
stop()/wait() so file and WAV sinks are flushed and closed properly.
Scripts without a top_block subclass are executed as __main__ instead.
"""
from __future__ import annotations

import argparse
import importlib.util
import os
import runpy
import sys
import threading
import time
import traceback


def _jsonable(v):
    import json
    try:
        json.dumps(v)
        return v
    except (TypeError, ValueError):
        try:
            return float(v)
        except Exception:
            return repr(v)


def _variables(tb) -> dict:
    out = {}
    for name in dir(tb):
        if name.startswith("get_") and callable(getattr(tb, name, None)) and hasattr(tb, "set_" + name[4:]):
            try:
                out[name[4:]] = _jsonable(getattr(tb, name)())
            except Exception as e:
                out[name[4:]] = f"<error {e}>"
    return out


PAUSE = {"paused": False, "since": 0.0, "total": 0.0}


def _pause(tb) -> str:
    """Hold the flowgraph: lock() stops the scheduler at the next work() boundary; unlock() carries on."""
    if PAUSE["paused"]:
        return "already paused"
    t = threading.Thread(target=tb.lock, daemon=True)
    t.start()
    t.join(4.0)
    PAUSE.update(paused=True, since=time.time())
    print("[sdrpilot] paused", flush=True)
    return "paused" if not t.is_alive() else "pausing (a block is busy - it pauses when its current call returns)"


def _resume(tb) -> str:
    if not PAUSE["paused"]:
        return "not paused"
    tb.unlock()
    PAUSE["total"] += time.time() - PAUSE["since"]
    PAUSE.update(paused=False, since=0.0)
    print("[sdrpilot] resumed", flush=True)
    return "running"


def _serve_commands(tb, control_dir: str) -> None:
    """Live control: SDR Pilot drops cmd_<id>.json files here; we answer with resp_<id>.json.
    ops: get (all variables), set {var, value} (calls the generated set_<var>(), which updates every block
    that uses it), call {target: 'block.method', args: [...]}."""
    import glob
    import json
    for path in sorted(glob.glob(os.path.join(control_dir, "cmd_*.json"))):
        rid = os.path.basename(path)[4:-5]
        try:
            with open(path, encoding="utf-8") as f:
                cmd = json.load(f)
            os.remove(path)
        except (OSError, ValueError):
            continue
        try:
            op = cmd.get("op")
            if op == "get":
                resp = {"ok": True, "variables": _variables(tb)}
            elif op == "set":
                var = cmd["var"]
                setter = getattr(tb, "set_" + var, None)
                if setter is None:
                    raise KeyError(f"no variable '{var}'. Variables: {sorted(_variables(tb))}")
                setter(cmd["value"])
                print(f"[sdrpilot] set {var} = {cmd['value']!r}", flush=True)
                resp = {"ok": True, "var": var, "value": _jsonable(getattr(tb, "get_" + var)())}
            elif op == "pause":
                resp = {"ok": True, "state": _pause(tb)}
            elif op == "resume":
                resp = {"ok": True, "state": _resume(tb)}
            elif op == "call":
                obj = tb
                parts = cmd["target"].split(".")
                for p in parts[:-1]:
                    obj = getattr(obj, p)
                result = getattr(obj, parts[-1])(*cmd.get("args", []))
                print(f"[sdrpilot] called {cmd['target']}{tuple(cmd.get('args', []))}", flush=True)
                resp = {"ok": True, "result": _jsonable(result)}
            else:
                raise ValueError(f"unknown op {op!r}")
        except Exception as e:
            resp = {"ok": False, "error": f"{type(e).__name__}: {e}"}
        tmp = os.path.join(control_dir, f"resp_{rid}.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(resp, f)
        os.replace(tmp, os.path.join(control_dir, f"resp_{rid}.json"))


def _shims() -> None:
    """gr-adsb imports asciimatics (a console UI library) even when it never draws; on Windows that import can
    fail (pywin32 DLL mismatch) and take the whole flowgraph down. Give it a stand-in that only complains if
    something really tries to open a console screen."""
    try:
        import asciimatics.screen  # noqa: F401
    except Exception:
        import types
        for k in [k for k in sys.modules if k == "asciimatics" or k.startswith("asciimatics.")]:
            sys.modules.pop(k, None)
        pkg, scr = types.ModuleType("asciimatics"), types.ModuleType("asciimatics.screen")

        class Screen:
            A_NORMAL = A_BOLD = A_REVERSE = A_UNDERLINE = 0
            COLOUR_BLACK = COLOUR_RED = COLOUR_GREEN = COLOUR_YELLOW = COLOUR_BLUE = COLOUR_WHITE = 0

            @staticmethod
            def open(*a, **k):
                raise RuntimeError("no console screen in an SDR Pilot job - set the block's print level to 'None' "
                                   "and send its messages to an sp_data_sink")

        scr.Screen = Screen
        pkg.screen = scr
        sys.modules["asciimatics"] = pkg
        sys.modules["asciimatics.screen"] = scr


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("script")
    ap.add_argument("--duration", type=float, default=0.0)
    ap.add_argument("--stop-file", default="")
    a = ap.parse_args()

    here = os.path.dirname(os.path.abspath(a.script))
    sys.path.insert(0, here)
    hier = os.environ.get("GRC_HIER_PATH")
    if hier and hier not in sys.path:
        sys.path.append(hier)  # sp_data_sink_impl and the sp_* blocks
    try:
        from sdrpilot.compat import setup_windows_dll_paths
        setup_windows_dll_paths()
    except Exception:
        pass

    print(f"[sdrpilot] loading {os.path.basename(a.script)}", flush=True)
    t_load = time.time()
    try:
        from gnuradio import gr
        spec = importlib.util.spec_from_file_location("sdrpilot_fg", a.script)
        mod = importlib.util.module_from_spec(spec)
        sys.modules["sdrpilot_fg"] = mod
        _shims()
        spec.loader.exec_module(mod)
    except SystemExit as e:
        return int(e.code or 0)
    except Exception:
        traceback.print_exc()
        print("[sdrpilot] ERROR: script failed while loading", flush=True)
        return 2

    tb_cls = None
    for v in vars(mod).values():
        if isinstance(v, type) and issubclass(v, gr.top_block) and v is not gr.top_block and v.__module__ == mod.__name__:
            tb_cls = v
    if tb_cls is None:
        print("[sdrpilot] no gr.top_block subclass found - running script as __main__", flush=True)
        try:
            runpy.run_path(a.script, run_name="__main__")
        except SystemExit as e:
            return int(e.code or 0)
        except Exception:
            traceback.print_exc()
            return 2
        return 0

    rec = None
    try:  # record the real connections so the dashboard can draw hand-written flowgraphs
        from sdrpilot.pygraph import ConnectRecorder
        rec = ConnectRecorder(gr)
        rec.install()
    except Exception:
        rec = None
    try:
        tb = tb_cls()
    except Exception:
        traceback.print_exc()
        print(f"[sdrpilot] ERROR: could not construct {tb_cls.__name__}", flush=True)
        return 3
    if rec is not None:
        rec.uninstall()
        if not os.path.exists(os.path.join(here, os.path.splitext(os.path.basename(a.script))[0] + ".grc")):
            rec.save(tb, here)
    print(f"[sdrpilot] running {tb_cls.__name__} (loaded in {time.time() - t_load:.1f}s)"
          + (f", will stop after {a.duration:g}s" if a.duration else ""), flush=True)
    tb.start()
    finished = threading.Event()

    def waiter():
        try:
            tb.wait()
        finally:
            finished.set()

    threading.Thread(target=waiter, daemon=True).start()
    control_dir = os.path.dirname(os.path.abspath(a.stop_file)) if a.stop_file else os.getcwd()
    t0 = time.time()
    reason = "flowgraph completed"
    try:
        while not finished.wait(0.2):
            paused_s = PAUSE["total"] + (time.time() - PAUSE["since"] if PAUSE["paused"] else 0.0)
            if a.duration and time.time() - t0 - paused_s >= a.duration:
                reason = "duration reached"
                break
            if a.stop_file and os.path.exists(a.stop_file):
                reason = "stop requested"
                break
            _serve_commands(tb, control_dir)
    except KeyboardInterrupt:
        reason = "interrupted"
    if PAUSE["paused"]:
        try:
            tb.unlock()
        except Exception:
            pass
    if not finished.is_set():
        tb.stop()
        tb.wait()
    print(f"[sdrpilot] finished: {reason} after {time.time() - t0:.2f}s", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
