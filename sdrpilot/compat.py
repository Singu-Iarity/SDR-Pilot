"""Environment plumbing that has to happen before GNU Radio or MCP are imported.

* On Windows (radioconda), make sure the conda DLL folders are on the search
  path even when python.exe is launched directly by the Claude app without
  `conda activate`.
* Protect the MCP stdio channel: GNU Radio, gr-osmosdr, SoapySDR and libhackrf
  all print banners straight to file descriptor 1 from C++.  If that reached the
  MCP pipe it would corrupt the JSON-RPC stream, so fd 1 is pointed at stderr
  and the MCP transport gets a private duplicate of the real stdout.
"""
from __future__ import annotations

import io
import os
import sys


def setup_windows_dll_paths() -> None:
    if os.name != "nt":
        return
    prefix = sys.prefix
    candidates = [
        os.path.join(prefix, "Library", "bin"),
        os.path.join(prefix, "Library", "mingw-w64", "bin"),
        os.path.join(prefix, "Library", "usr", "bin"),
        os.path.join(prefix, "Scripts"),
        os.path.join(prefix, "DLLs"),
        prefix,
    ]
    existing = os.environ.get("PATH", "")
    add = [c for c in candidates if os.path.isdir(c) and c not in existing]
    if add:
        os.environ["PATH"] = os.pathsep.join(add + [existing])
    for c in candidates:
        if os.path.isdir(c):
            try:
                os.add_dll_directory(c)  # type: ignore[attr-defined]
            except (AttributeError, OSError):
                pass


_protected = False


def protect_stdout() -> None:
    """Route C-level writes on fd 1 to stderr; keep a private fd for MCP."""
    global _protected
    if _protected:
        return
    _protected = True
    try:
        sys.stdout.flush()
    except Exception:
        pass
    try:
        real_fd = os.dup(1)
        os.dup2(2, 1)
    except OSError:
        return
    raw = os.fdopen(real_fd, "wb", buffering=0)
    buffered = io.BufferedWriter(raw)
    sys.stdout = io.TextIOWrapper(buffered, encoding="utf-8", newline="\n", write_through=True)


def child_env(extra: dict | None = None) -> dict:
    """Environment for flowgraph subprocesses (same interpreter, same DLL path)."""
    env = dict(os.environ)
    pkg_parent = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env["PYTHONPATH"] = os.pathsep.join(p for p in [pkg_parent, env.get("PYTHONPATH", "")] if p)
    env["PYTHONUNBUFFERED"] = "1"
    env.setdefault("PYTHONIOENCODING", "utf-8")
    hier = hier_block_dir()
    if hier:  # SDR Pilot's own GRC blocks (sp_data_sink_impl, sp_* hier blocks) live in GRC's block folder
        env["GRC_HIER_PATH"] = hier
        env["PYTHONPATH"] = os.pathsep.join(p for p in [env["PYTHONPATH"], hier] if p)
    if extra:
        env.update({k: str(v) for k, v in extra.items()})
    return env


def hier_block_dir() -> str:
    """GNU Radio Companion's folder for generated hier blocks and SDR Pilot's plain blocks. GNU Radio 3.10.12 moved
    it from ~/.grc_gnuradio to the XDG state folder (Windows: %APPDATA%\\.local\\state\\gnuradio), and flowgraphs
    started outside GRC don't look there by themselves - '.. No module named sp_data_sink_impl'."""
    try:
        from . import grc
        if getattr(grc, "_PLATFORM", None) is not None:
            return grc._PLATFORM.config.hier_block_lib_dir
    except Exception:
        pass
    try:
        from gnuradio.grc.core.Config import Config  # cheap: reads the prefs, doesn't load the block library
        return Config(version="").hier_block_lib_dir
    except Exception:
        return ""


def import_tool_error():
    try:
        from mcp.server.mcpserver.exceptions import ToolError  # type: ignore
    except ImportError:
        try:
            from mcp.server.fastmcp.exceptions import ToolError  # type: ignore
        except ImportError:
            ToolError = RuntimeError  # type: ignore  # noqa: N806
    return ToolError


def import_mcp_server():
    """Return (ServerClass, ImageClass) for MCP SDK 2.x (MCPServer) or 1.x (FastMCP)."""
    try:
        from mcp.server.mcpserver import MCPServer as Server, Image  # type: ignore
        return Server, Image
    except ImportError:
        from mcp.server.fastmcp import FastMCP as Server, Image  # type: ignore
        return Server, Image
