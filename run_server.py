"""Entry point.

    python run_server.py           MCP server over stdio (what the Claude app / any MCP client starts)
    python run_server.py --host    SDR Pilot host: dashboard + dashboard agent + MCP over HTTP (port 8770)
    python run_server.py --http    MCP over HTTP only
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

if sys.stdout is None or sys.stderr is None:  # pythonw.exe (host started hidden): give output somewhere to go
    _logs = os.path.join(HERE, "workspace", "logs")
    os.makedirs(_logs, exist_ok=True)
    _f = open(os.path.join(_logs, "host_console.log"), "a", buffering=1, encoding="utf-8")
    for _fd in (1, 2):
        try:
            os.dup2(_f.fileno(), _fd)
        except OSError:
            pass
    sys.stdout = sys.stdout or _f
    sys.stderr = sys.stderr or _f

from sdrpilot.server import main  # noqa: E402

if __name__ == "__main__":
    main()
