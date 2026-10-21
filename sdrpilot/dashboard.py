"""Status dashboard (http://127.0.0.1:8765 on this PC, or your phone over Tailscale): what the AI is doing, task
progress, a chat box, the live gap-free waterfall, transmitters, flowgraphs and their outputs.

Every SDR Pilot copy (one per chat, plus the optional host) runs its own dashboard server and publishes its state
to workspace/state/sessions/<pid>.json. Any of them can show every chat: requests for another chat are proxied to
that copy's own dashboard port, so one address (e.g. the one `tailscale serve` points at) is enough."""
from __future__ import annotations

import base64
import http.client
import json
import mimetypes
import os
import re
import subprocess
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
from . import codecheck  # noqa: E402
HOP = "X-SDRPilot-Hop"

LOGIN_HTML = """<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>SDR Pilot</title><style>body{margin:0;min-height:100vh;display:grid;place-items:center;background:#0b0d12;color:#e6e8ee;
font:16px system-ui,-apple-system,sans-serif}form{width:min(92vw,380px);padding:26px;background:#12151d;border:1px solid #242a37;
border-radius:14px}b{font:700 17px monospace;letter-spacing:.18em;color:#4fe0a8}p{color:#8a91a3;font-size:14px;line-height:1.45}
input{width:100%;box-sizing:border-box;font:16px monospace;padding:11px;border-radius:9px;border:1px solid #242a37;background:#0b0d12;color:#e6e8ee}
button{margin-top:12px;width:100%;padding:11px;border:0;border-radius:9px;background:#4fe0a8;color:#04140d;font:600 16px system-ui}
.e{color:#ff6b81}</style></head><body><form method="get" action="./"><b>SDR PILOT</b>
<p>This dashboard is on another computer, so it needs its access key once. Open the dashboard on the PC, tap
<i>Settings</i> &rarr; <i>Remote access</i> and use the phone link there (or ask the AI for <i>open_dashboard</i>).</p>
__ERR__<input name="key" placeholder="access key" autocomplete="off" autocapitalize="off" spellcheck="false">
<button>Open dashboard</button></form></body></html>"""


def make_icon(size: int) -> bytes:
    """App icon for 'Add to Home Screen': a small waterfall with a spectrum trace on top."""
    from .plot import colormap, png_bytes
    y, x = np.mgrid[0:size, 0:size] / size
    sig = np.exp(-((x - 0.62) / 0.05) ** 2) * (0.55 + 0.45 * np.sin(y * 18) ** 2)
    sig += 0.6 * np.exp(-((x - 0.3) / 0.025) ** 2) * (np.sin(y * 7 + 1) > 0.2)
    img = colormap(np.clip(sig + np.random.default_rng(7).random((size, size)) * 0.18, 0, 1)).copy()
    top = int(size * 0.32)
    xs = np.arange(size)
    line = (0.29 - 0.17 * np.exp(-((xs / size - 0.62) / 0.05) ** 2)
            - 0.11 * np.exp(-((xs / size - 0.3) / 0.025) ** 2)) * size
    img[:top] = (11, 13, 18)
    for dy in range(max(2, size // 48)):
        img[np.clip(line.astype(int) + dy, 0, top - 1), xs] = (79, 224, 168)
    return png_bytes(img)


class SessionStore:
    """Every SDR Pilot copy publishes its dashboard state to workspace/state/sessions/<pid>.json."""

    ALIVE_S = 20.0      # heartbeat age after which a session counts as ended
    KEEP_S = 6 * 3600   # ended sessions stay listed this long

    def __init__(self, workspace: str, snapshot_fn):
        self.dir = os.path.join(workspace, "state", "sessions")
        os.makedirs(self.dir, exist_ok=True)
        self.snapshot_fn = snapshot_fn
        self.pid = os.getpid()
        self.path = os.path.join(self.dir, f"{self.pid}.json")
        threading.Thread(target=self._loop, daemon=True, name="sdrpilot-session").start()

    def _loop(self) -> None:
        while True:
            try:
                data = self.snapshot_fn()
                data["heartbeat"] = time.time()
                tmp = self.path + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(data, f, default=str)
                os.replace(tmp, self.path)
            except Exception:
                pass
            time.sleep(1.0)

    def read(self, pid) -> dict | None:
        try:
            with open(os.path.join(self.dir, f"{int(pid)}.json"), encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError, TypeError):
            return None

    def alive(self, d: dict | None) -> bool:
        return bool(d) and time.time() - d.get("heartbeat", 0) < self.ALIVE_S

    def all(self) -> list[dict]:
        out = []
        now = time.time()
        for fn in os.listdir(self.dir):
            if not fn.endswith(".json"):
                continue
            p = os.path.join(self.dir, fn)
            try:
                if now - os.path.getmtime(p) > self.KEEP_S:
                    os.remove(p)
                    continue
                with open(p, encoding="utf-8") as f:
                    out.append(json.load(f))
            except (OSError, ValueError):
                continue
        return out

    def list(self) -> list[dict]:
        out = []
        now = time.time()
        for d in self.all():
            task = d.get("task") or {}
            cs = d.get("chat_state") or {}
            out.append({"pid": d.get("pid"), "kind": d.get("kind", "chat"), "version": d.get("version", "older"),
                        "goal": task.get("goal") or "",
                        "finished": bool(task.get("finished")), "progress": task.get("progress", 0),
                        "last_activity": d.get("last_activity", 0), "alive": self.alive(d),
                        "is_self": d.get("pid") == self.pid, "listening": bool(cs.get("listening")),
                        "agent_busy": bool((cs.get("agent") or {}).get("busy")),
                        "radio": (d.get("radio") or {}).get("state") == "connected",
                        "running_jobs": sum(1 for j in d.get("jobs", []) if j.get("status") in ("running", "paused"))})
        out.sort(key=lambda s: (s["kind"] != "host", -s["last_activity"]))
        return out


class Dashboard:
    def __init__(self, cfg, board, radio, jobs, access=None, relay=None, kind: str = "chat"):
        self.cfg, self.board, self.radio, self.jobs = cfg, board, radio, jobs
        self.access, self.relay, self.kind = access, relay, kind
        self.agent = None          # DashboardAgent, host process only
        self.prefs = None          # ContextPrefs: shortening switches shared by every SDR Pilot copy
        from .updater import Updater
        self.updater = Updater(cfg)   # newer releases (GitHub Releases), checked by the host
        self.extra_info: dict = {}  # e.g. the MCP-over-HTTP endpoint of a host
        self.httpd: ThreadingHTTPServer | None = None
        self.url = ""
        self.port = 0
        self.sessions: SessionStore | None = None
        self._cache: dict = {}
        self._lock = threading.Lock()
        self._radio_prev: dict | None = None
        self._radio_changes: list = []
        self._icons: dict = {}
        self.last_view = 0.0

    # ================================================================== HTTP server
    def start(self) -> str:
        dash = self
        if self.access is None:
            from .access import Access
            self.access = Access(self.cfg)

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            # ---------------------------------------------------------- plumbing
            def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None):
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                for k, v in (extra or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                try:
                    if self.command != "HEAD":
                        self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    pass

            def _json(self, obj, code: int = 200):
                self._send(code, json.dumps(obj, default=str).encode(), "application/json")

            def _err(self, code: int, msg: str):
                self._json({"error": msg}, code)

            def _hdrs(self) -> dict:
                return {k.lower(): v for k, v in self.headers.items()}

            def _cookie_key(self) -> str:
                for part in (self.headers.get("Cookie") or "").split(";"):
                    k, _, v = part.strip().partition("=")
                    if k == "sdrpilot_key":
                        return urllib.parse.unquote(v)
                return ""

            def _gate(self, u, q) -> bool:
                """Host check, network check, key check. Returns False if a response was already sent."""
                h = self._hdrs()
                acc = dash.access
                if not acc.host_ok(h.get("host")):
                    self._err(403, "unexpected Host header (add it to allowed_hosts in sdrpilot_config.json)")
                    return False
                kind = acc.client_kind(self.client_address[0], h)
                self.client_kind = kind
                if not acc.bind_allows(kind, dash.cfg.dashboard_bind):
                    self._err(403, f"{kind} access is off (dashboard_bind = {dash.cfg.dashboard_bind})")
                    return False
                qkey = q.get("key", [""])[0]
                self.authed = (kind == "local" or not dash.cfg.remote_require_key
                               or acc.check(h.get("x-sdrpilot-key")) or acc.check(self._cookie_key()) or acc.check(qkey))
                if self.command == "POST" and not acc.check(h.get("x-sdrpilot-key")):
                    self._err(403, "missing or wrong access key")
                    return False
                if qkey and acc.check(qkey) and u.path in ("/", "/index.html") and self.command == "GET":
                    # remember the key and drop it from the address bar
                    secure = "; Secure" if h.get("x-forwarded-proto") == "https" else ""
                    self.send_response(303)
                    self.send_header("Set-Cookie", f"sdrpilot_key={urllib.parse.quote(acc.key)}; Max-Age=31536000; "
                                                   f"Path=/; HttpOnly; SameSite=Lax{secure}")
                    self.send_header("Location", "./")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return False
                if not self.authed:
                    if u.path in ("/", "/index.html"):
                        err = '<p class="e">That key is not right.</p>' if qkey else ""
                        self._send(200, LOGIN_HTML.replace("__ERR__", err).encode(), "text/html; charset=utf-8")
                    elif u.path in ("/manifest.webmanifest",) or u.path.startswith("/icon"):
                        return True
                    else:
                        self._err(401, "access key needed")
                    return False
                return True

            def _proxy(self, port: int, body: bytes | None = None):
                """Forward this request to another SDR Pilot copy's dashboard on this PC."""
                conn = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
                hdrs = {HOP: "1", "X-SDRPilot-Key": dash.access.key, "Host": f"127.0.0.1:{port}"}
                for k in ("Range", "Content-Type"):
                    if self.headers.get(k):
                        hdrs[k] = self.headers[k]
                try:
                    conn.request(self.command, self.path, body=body, headers=hdrs)
                    r = conn.getresponse()
                    self.send_response(r.status)
                    for k in ("Content-Type", "Content-Length", "Content-Range", "Accept-Ranges", "Cache-Control"):
                        v = r.getheader(k)
                        if v:
                            self.send_header(k, v)
                    if not r.getheader("Content-Length"):
                        data = r.read()
                        self.send_header("Content-Length", str(len(data)))
                        self.end_headers()
                        self.wfile.write(data)
                        return
                    self.end_headers()
                    while True:
                        chunk = r.read(256 * 1024)
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                except (ConnectionRefusedError, TimeoutError, OSError) as e:
                    try:
                        self._err(502, f"that chat's SDR Pilot didn't answer: {e}")
                    except Exception:
                        pass
                finally:
                    conn.close()

            def _send_file(self, path: str):
                size = os.path.getsize(path)
                ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
                rng = self.headers.get("Range")
                start, end, code = 0, size - 1, 200
                m = re.match(r"bytes=(\d*)-(\d*)$", rng or "")
                if m and size:
                    if m.group(1):
                        start = int(m.group(1))
                        end = int(m.group(2)) if m.group(2) else size - 1
                    else:  # suffix range
                        start = max(0, size - int(m.group(2) or 0))
                    end = min(end, size - 1)
                    if start > end:
                        self.send_response(416)
                        self.send_header("Content-Range", f"bytes */{size}")
                        self.send_header("Content-Length", "0")
                        self.end_headers()
                        return
                    code = 206
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Content-Length", str(end - start + 1))
                self.send_header("Cache-Control", "no-store")
                if code == 206:
                    self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
                self.end_headers()
                if self.command == "HEAD":
                    return
                with open(path, "rb") as f:
                    f.seek(start)
                    left = end - start + 1
                    try:
                        while left > 0:
                            chunk = f.read(min(left, 256 * 1024))
                            if not chunk:
                                break
                            self.wfile.write(chunk)
                            left -= len(chunk)
                    except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                        pass

            # ---------------------------------------------------------- routes
            def do_HEAD(self):
                self.do_GET()

            def do_GET(self):
                u = urllib.parse.urlparse(self.path)
                q = urllib.parse.parse_qs(u.query)
                try:
                    if not self._gate(u, q):
                        return
                    dash.last_view = time.time()
                    if u.path in ("/", "/index.html"):
                        with open(os.path.join(HERE, "dashboard.html"), "rb") as f:
                            from . import __build__ as _ver
                            html = f.read().replace(b"__SDRPILOT_KEY__", dash.access.key.encode()) \
                                .replace(b"__SDRPILOT_VERSION__", _ver.encode())
                        return self._send(200, html, "text/html; charset=utf-8")
                    if u.path == "/manifest.webmanifest":
                        return self._send(200, json.dumps({
                            "name": "SDR Pilot", "short_name": "SDR Pilot", "start_url": "./", "scope": "./",
                            "display": "standalone", "background_color": "#0b0d12", "theme_color": "#0b0d12",
                            "icons": [{"src": "icon-192.png", "sizes": "192x192", "type": "image/png"},
                                      {"src": "icon-512.png", "sizes": "512x512", "type": "image/png"}]}).encode(),
                            "application/manifest+json")
                    m = re.match(r"^/icon-(\d+)\.png$", u.path)
                    if m:
                        s = min(512, max(32, int(m.group(1))))
                        if s not in dash._icons:
                            dash._icons[s] = make_icon(s)
                        return self._send(200, dash._icons[s], "image/png", {"Cache-Control": "max-age=86400"})
                    sess = q.get("session", [""])[0]
                    if sess and not sess.isdigit():
                        return self._err(400, "bad session")
                    # requests about another chat go to that chat's own SDR Pilot - except the live spectrum: that's the
                    # radio's, the same whichever chat is selected, so it comes from the copy with the freshest view
                    radio_view = u.path in ("/api/wf", "/api/live", "/api/signals", "/api/radio")
                    if sess and int(sess) != os.getpid() and not self.headers.get(HOP) and not radio_view:
                        port = dash.session_port(int(sess))
                        if port:
                            return self._proxy(port)
                    if u.path == "/api/state":
                        return self._json(dash.session_state(sess, int(q.get("since", ["0"])[0]),
                                                             int(q.get("chat_since", ["0"])[0])))
                    if u.path == "/api/chat/detail":
                        try:
                            mid = int(q.get("id", ["0"])[0])
                        except ValueError:
                            return self._err(400, "bad id")
                        det = dash.board.chat_details.get(mid)
                        return self._json(det if det else {"error": "the full text of this call isn't kept any more"})
                    if u.path == "/api/gallery":
                        return self._json(dash.gallery({k: v[0] for k, v in q.items()}))
                    if u.path == "/api/gallery/img":
                        fn = os.path.basename(q.get("f", [""])[0])
                        img_root = os.path.realpath(os.path.join(dash.cfg.data_root, "images"))
                        p = os.path.realpath(os.path.join(img_root, fn))
                        if fn.endswith(".png") and p.startswith(img_root) and os.path.exists(p):
                            with open(p, "rb") as f:
                                return self._send(200, f.read(), "image/png", {"Cache-Control": "max-age=86400"})
                        return self._send(404, b"no image", "text/plain")
                    if u.path == "/api/task_history":
                        from .status import load_task_history
                        hp = dash.board.history_path or os.path.join(dash.cfg.workspace, "state", "task_history.jsonl")
                        try:
                            lim = min(200, max(1, int(q.get("limit", ["60"])[0])))
                        except ValueError:
                            lim = 60
                        return self._json({"tasks": load_task_history(hp, lim, q.get("q", [""])[0])})
                    if u.path == "/api/instances":
                        return self._json({"self": os.getpid(), "instances": dash.instances()})
                    if u.path == "/instances":
                        with open(os.path.join(HERE, "instances.html"), "rb") as f:
                            html = f.read().replace(b"__SDRPILOT_KEY__", dash.access.key.encode())
                        return self._send(200, html, "text/html; charset=utf-8")
                    if u.path == "/api/sessions":
                        return self._json({"self": os.getpid(), "kind": dash.kind, "sessions": dash.sessions.list()})
                    if u.path == "/api/flowgraphs":
                        return self._json(dash.flowgraphs(sess))
                    if u.path == "/api/flowgraph":
                        return self._json(dash.flowgraph(q.get("name", [""])[0]))
                    if u.path == "/api/decoded":
                        return self._json(dash.jobs.decoded_view(q.get("job", [""])[0]))
                    if u.path == "/api/flowgraphs/prune":
                        return self._json(dash.prune_preview(float(q.get("older_than_h", ["1"])[0])))
                    if u.path in ("/api/wf", "/api/live", "/api/signals", "/api/radio"):
                        if not dash.radio.connected and not self.headers.get(HOP):
                            port = dash.radio_owner_port()
                            if port:
                                return self._proxy(port)
                        if u.path == "/api/radio":
                            return self._json(dash.radio_panel())
                        if u.path == "/api/signals":
                            return self._json(dash.signals(float(q.get("seconds", ["30"])[0])))
                        if u.path == "/api/live":
                            return self._json(dash.live(int(q.get("bins", ["1024"])[0])))
                        cur = q.get("cursor", [""])[0]
                        return self._json(dash.wf(int(cur) if cur.lstrip("-").isdigit() else None,
                                                  int(q.get("bins", ["512"])[0])))
                    if u.path == "/api/update":
                        return self._json(dash.update_status())
                    if u.path == "/api/context_prefs":
                        return self._json(dash.prefs.get() if dash.prefs else {})
                    if u.path == "/api/storage":
                        from . import config as _config
                        return self._json({"workspace": dash.cfg.workspace, "data_dir": dash.cfg.data_dir,
                                           "data_root": dash.cfg.data_root, "default_workspace":
                                           os.path.join(_config.ROOT, "workspace")})
                    if u.path == "/api/remote":
                        return self._json(dash.remote_info(self.client_kind))
                    if u.path.startswith("/api/image/"):
                        idx = int(u.path.rsplit("/", 1)[1])
                        if sess and int(sess) != os.getpid():
                            paths = (dash.sessions.read(int(sess)) or {}).get("image_paths", [])
                        else:
                            paths = dash.board.image_paths()
                        img_root = os.path.realpath(os.path.join(dash.cfg.data_root, "images"))
                        if 0 <= idx < len(paths):
                            p = os.path.realpath(paths[idx])
                            if p.startswith(img_root) and os.path.exists(p):
                                with open(p, "rb") as f:
                                    return self._send(200, f.read(), "image/png", {"Cache-Control": "max-age=3600"})
                        return self._send(404, b"no image", "text/plain")
                    if u.path.startswith("/files/"):
                        _, _, jid, rel = u.path.split("/", 3)
                        folder = None
                        if sess and int(sess) != os.getpid():
                            for j in (dash.sessions.read(int(sess)) or {}).get("jobs", []):
                                if j.get("job_id") == jid:
                                    folder = j.get("output_folder")
                        else:
                            job = dash.jobs.jobs.get(jid)
                            folder = job.folder if job else None
                        jobs_root = os.path.realpath(os.path.join(dash.cfg.data_root, "jobs"))
                        if not folder or not os.path.realpath(folder).startswith(jobs_root):
                            return self._send(404, b"no job", "text/plain")
                        p = os.path.realpath(os.path.join(folder, urllib.parse.unquote(rel)))
                        if not p.startswith(os.path.realpath(folder)) or not os.path.isfile(p):
                            return self._send(404, b"not found", "text/plain")
                        return self._send_file(p)
                    return self._send(404, b"not found", "text/plain")
                except Exception as e:  # pragma: no cover
                    try:
                        return self._send(500, f"{type(e).__name__}: {e}".encode(), "text/plain")
                    except Exception:
                        pass

            def do_POST(self):
                u = urllib.parse.urlparse(self.path)
                q = urllib.parse.parse_qs(u.query)
                n = int(self.headers.get("Content-Length") or 0)
                if n > 1_000_000:
                    return self._err(413, "too large")
                raw = self.rfile.read(n) if n else b""
                try:
                    if not self._gate(u, q):
                        return
                    sess = q.get("session", [""])[0]
                    if sess and sess.isdigit() and int(sess) != os.getpid() and not self.headers.get(HOP):
                        port = dash.session_port(int(sess))
                        if not port:
                            return self._err(410, "that chat has ended")
                        return self._proxy(port, raw)
                    body = json.loads(raw or b"{}")
                    return self._json(dash.post(u.path, body))
                except (ValueError, KeyError, RuntimeError) as e:
                    return self._err(400, f"{type(e).__name__}: {e}")
                except Exception as e:  # pragma: no cover
                    return self._err(500, f"{type(e).__name__}: {e}")

        # On Windows SO_REUSEADDR lets a second process silently share the port; insist on exclusive use
        ThreadingHTTPServer.allow_reuse_address = os.name != "nt"
        addr = self.access.bind_address(self.cfg.dashboard_bind)
        last_err = None
        for port in range(self.cfg.dashboard_port, self.cfg.dashboard_port + 10):
            try:
                self.httpd = ThreadingHTTPServer((addr, port), H)
                break
            except OSError as e:
                last_err = e
        if self.httpd is None:
            raise RuntimeError(f"could not start dashboard: {last_err}")
        self.httpd.daemon_threads = True
        threading.Thread(target=self.httpd.serve_forever, daemon=True, name="sdrpilot-dashboard").start()
        self.port = self.httpd.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}/"
        self.sessions = SessionStore(self.cfg.workspace, self._publish_snapshot)
        return self.url

    # ================================================================== sessions
    def session_port(self, pid: int) -> int | None:
        d = self.sessions.read(pid)
        if not self.sessions.alive(d) or not d.get("dashboard_url"):
            return None
        try:
            return urllib.parse.urlparse(d["dashboard_url"]).port
        except ValueError:
            return None

    def radio_owner_port(self) -> int | None:
        """The dashboard port of whichever SDR Pilot copy currently has the radio connected."""
        best = None
        rank = lambda d: (not d.get("code_update"), d.get("kind") == "host", d.get("last_activity", 0))  # noqa: E731
        for d in self.sessions.all():  # prefer copies running the current code (they follow retunes made elsewhere)
            if d.get("pid") == os.getpid() or not self.sessions.alive(d):
                continue
            if (d.get("radio") or {}).get("state") == "connected":
                if best is None or rank(d) > rank(best):
                    best = d
        if best:
            try:
                return urllib.parse.urlparse(best["dashboard_url"]).port
            except (KeyError, ValueError):
                return None
        return None

    def instance_info(self) -> dict:
        """What the Instances page shows about this copy of SDR Pilot."""
        r = self.radio
        st = r.stream
        jobs = self.jobs.overview()
        ag = self.board.chat_state.get("agent") or {}
        try:
            upd = codecheck.stale()
        except Exception:  # pragma: no cover
            upd = None
        return {"pid": os.getpid(), "kind": self.kind, "port": self.port, "url": self.url,
                "started": codecheck.STARTED, "python": sys.executable,
                "code_update": upd, "paused": bool(getattr(r, "paused", False)),
                "radio": {"state": "paused" if getattr(r, "paused", False) else
                          ("connected" if r.connected else "idle"),
                          "center_freq": r.center_freq, "sample_rate": r.sample_rate, "hw": r.hw_type,
                          "protocol": st.meta.get("protocol") if st else None, "bits": st.meta.get("bits") if st else None,
                          "measured_rate": round(st.rate_measured) if st and st.connected else 0},
                "jobs": [{"id": j["job_id"], "name": j["flowgraph"], "status": j["status"]} for j in jobs
                         if j["status"] in ("running", "paused")][:12],
                "jobs_total": len(jobs),
                "task": {"goal": (self.board.task or {}).get("goal", ""), "progress": self.board.progress(),
                         "finished": bool((self.board.task or {}).get("finished"))},
                "agent": {"model": ag.get("model", ""), "label": ag.get("label", ""), "busy": bool(ag.get("busy"))}
                if self.kind == "host" else None,
                "last_activity": self.board.last_activity_time(),
                "mcp_http": self.extra_info.get("mcp_http")}

    def gallery(self, q: dict) -> dict:
        """Every picture the AIs made (images/index.jsonl, all copies of SDR Pilot), grouped by tool call, filtered
        and sorted for the dashboard's "What the AI looked at" panel."""
        path = os.path.join(self.cfg.data_root, "images", "index.jsonl")
        recs = []
        try:
            with open(path, encoding="utf-8") as f:
                lines = f.readlines()[-6000:]
        except OSError:
            lines = []
        for ln in lines:
            try:
                recs.append(json.loads(ln))
            except ValueError:
                continue
        img_dir = os.path.join(self.cfg.data_root, "images")
        groups: dict = {}
        for r in recs:
            g = r.get("group") or r.get("file")
            e = groups.setdefault(g, {"group": g, "t": r.get("t", 0), "tool": r.get("tool"), "pid": r.get("pid"),
                                      "kinds": [], "images": [], "center_hz": r.get("center_hz"),
                                      "f_lo_hz": r.get("f_lo_hz"), "f_hi_hz": r.get("f_hi_hz"),
                                      "caption": r.get("caption", "")})
            e["images"].append({"file": r.get("file"), "kind": r.get("kind"), "caption": r.get("caption", "")})
            if r.get("kind") not in e["kinds"]:
                e["kinds"].append(r.get("kind"))
            e["t"] = max(e["t"], r.get("t", 0))
            if e.get("center_hz") is None and r.get("center_hz"):
                e.update(center_hz=r.get("center_hz"), f_lo_hz=r.get("f_lo_hz"), f_hi_hz=r.get("f_hi_hz"))
        items = [g for g in groups.values() if any(os.path.exists(os.path.join(img_dir, i["file"] or ""))
                                                   for i in g["images"][:1])]
        facets: dict = {}
        for g in items:
            for k in g["kinds"]:
                facets[k] = facets.get(k, 0) + 1
        kind = (q.get("kind") or "").lower()
        if kind and kind != "all":
            items = [g for g in items if kind in g["kinds"]]
        if q.get("pid"):
            items = [g for g in items if str(g.get("pid")) == str(q["pid"])]
        text = (q.get("q") or "").lower().strip()
        if text:
            items = [g for g in items if text in (" ".join([g.get("caption") or "", g.get("tool") or ""] +
                                                         [i["caption"] for i in g["images"]])).lower()]
        try:
            fmin = float(q["fmin"]) if q.get("fmin") else None
            fmax = float(q["fmax"]) if q.get("fmax") else None
        except ValueError:
            fmin = fmax = None
        if fmin is not None or fmax is not None:  # MHz range overlap
            def overlaps(g):
                lo = (g.get("f_lo_hz") or g.get("center_hz") or 0) / 1e6
                hi = (g.get("f_hi_hz") or g.get("center_hz") or 0) / 1e6
                return (fmax is None or lo <= fmax) and (fmin is None or hi >= fmin) and (g.get("center_hz") or 0) > 0
            items = [g for g in items if overlaps(g)]
        sort = q.get("sort") or "newest"
        if sort == "oldest":
            items.sort(key=lambda g: g["t"])
        elif sort in ("freq_asc", "freq_desc"):
            items.sort(key=lambda g: (g.get("center_hz") is None, g.get("center_hz") or 0), reverse=False)
            if sort == "freq_desc":
                withf = [g for g in items if g.get("center_hz")]
                items = withf[::-1] + [g for g in items if not g.get("center_hz")]
        elif sort == "kind":
            items.sort(key=lambda g: (g["kinds"][0] if g["kinds"] else "", -g["t"]))
        else:
            items.sort(key=lambda g: -g["t"])
        total = len(items)
        off, lim = int(q.get("offset") or 0), int(min(int(q.get("limit") or 60), 300))
        return {"total": total, "facets": facets, "items": items[off:off + lim]}

    def instances(self) -> list[dict]:
        out = []
        for d in self.sessions.all():
            info = dict(d.get("instance") or {"pid": d.get("pid"), "kind": d.get("kind"), "version": d.get("version")})
            info.setdefault("pid", d.get("pid"))
            info["version"] = d.get("version", "older")
            info["alive"] = self.sessions.alive(d)
            info["heartbeat"] = d.get("heartbeat")
            if not d.get("instance"):  # a copy older than the Instances page: what its snapshot has
                info["radio"] = {"state": (d.get("radio") or {}).get("state"),
                                 "center_freq": (d.get("radio") or {}).get("center_freq"),
                                 "sample_rate": (d.get("radio") or {}).get("sample_rate"),
                                 "measured_rate": (d.get("radio") or {}).get("measured_rate")}
                info["url"] = d.get("dashboard_url")
                info["old_code"] = True
                info["task"] = {"goal": (d.get("task") or {}).get("goal", "")}
                info["jobs"] = [{"id": j.get("job_id"), "name": j.get("flowgraph"), "status": j.get("status")}
                                for j in d.get("jobs", []) if j.get("status") in ("running", "paused")]
            info["is_self"] = info.get("pid") == os.getpid()
            out.append(info)
        out.sort(key=lambda s: (not s.get("alive"), s.get("kind") != "host", -(s.get("last_activity") or 0)))
        return out

    def _publish_snapshot(self) -> dict:
        d = self.state(0)
        try:
            d["instance"] = self.instance_info()
        except Exception:  # pragma: no cover
            pass
        d["pid"] = os.getpid()
        d["kind"] = self.kind
        from . import __build__ as _ver
        d["version"] = _ver
        d["dashboard_url"] = self.url
        d["last_activity"] = self.board.last_activity_time()
        d["image_paths"] = self.board.image_paths()
        return d

    def session_state(self, pid, since: int, chat_since: int = 0) -> dict:
        if not pid or int(pid) == os.getpid():
            d = self.state(since, chat_since)
            d["pid"] = os.getpid()
            d["kind"] = self.kind
            return d
        d = self.sessions.read(int(pid))
        if d is None:
            return {"error": f"session {pid} not found"}
        d = dict(d)
        d.pop("image_paths", None)
        d["activity"] = [e for e in d.get("activity", []) if e.get("id", 0) > since]
        d["chat"] = [m for m in d.get("chat", []) if m.get("rev", 0) > chat_since]
        d["server_time"] = time.time()
        d["ended"] = True
        d.setdefault("radio", {})["state"] = "session ended"
        return d

    def state(self, since: int, chat_since: int = 0) -> dict:
        snap = self.board.snapshot(since, chat_since)
        snap["jobs"] = self.jobs.overview()
        st = self.radio.stream
        snap["radio"]["measured_rate"] = round(st.rate_measured) if st is not None and st.connected else None
        snap["radio"]["state"] = "connected" if self.radio.connected else snap["radio"].get("state", "idle")
        snap["radio"]["center_freq"] = self.radio.center_freq or snap["radio"].get("center_freq")
        snap["radio"]["sample_rate"] = self.radio.sample_rate or snap["radio"].get("sample_rate")
        mon = self.radio.monitor
        snap["monitor"] = mon.summary(last_n=15) if (mon.running or mon.detections) else None
        snap["server_time"] = time.time()
        try:
            snap["code_update"] = codecheck.stale()
        except Exception:  # pragma: no cover
            snap["code_update"] = None
        snap["relay_pending"] = bool(self.relay and self.relay.has_pending())
        try:
            snap["system"] = self.system()
        except Exception as e:  # pragma: no cover
            snap["system"] = {"error": str(e)}
        return snap

    # ================================================================== actions (POST)
    def post(self, path: str, body: dict) -> dict:
        if path == "/api/report":
            # the user wants to know what's going on: an instant summary from the activity log, and a request for
            # the AI's own short report (it answers after its current step)
            self.board.chat_add("system", self.activity_summary())
            from .agent import REPORT_TEXT as text
            if self.agent is not None:
                return {"ok": True, "mode": "agent", **self.agent.request_report(text)}
            if self.relay is None:
                raise RuntimeError("this SDR Pilot copy has no chat relay")
            # an MCP chat (e.g. Claude Desktop): the request rides on its next tool result, marked as a priority
            self.relay.report_requested = time.time()
            return {"ok": True, "mode": "relay", **self.relay.post(text)}
        if path == "/api/chat":
            text = str(body.get("text") or "").strip()
            if not text:
                raise ValueError("empty message")
            if len(text) > 8000:
                raise ValueError("message too long")
            if self.agent is not None:
                return {"ok": True, "mode": "agent", **self.agent.submit(text)}
            if self.relay is None:
                raise RuntimeError("this SDR Pilot copy has no chat relay")
            return {"ok": True, "mode": "relay", **self.relay.post(text)}
        if path == "/api/agent":
            if self.agent is None:
                raise RuntimeError("the dashboard agent runs in the SDR Pilot host; start it first")
            act = body.get("action")
            if act == "stop":
                self.agent.stop()
            elif act == "reset":
                self.agent.reset()
            elif act == "config":
                self.agent.configure(preset=body.get("preset"), base_url=body.get("base_url"),
                                     model=body.get("model"), api_key=body.get("api_key"), vision=body.get("vision"),
                                     vision_mode=body.get("vision_mode"),
                                     context_tokens=body.get("context_tokens"), tool_detail=body.get("tool_detail"),
                                     compact_history=body.get("compact_history"), nudge=body.get("nudge"),
                                     stream=body.get("stream"), auto_recover=body.get("auto_recover"),
                                     max_reply_tokens=body.get("max_reply_tokens"),
                                     custom_instructions=body.get("custom_instructions"),
                                     show_reasoning=body.get("show_reasoning"))
            elif act == "models":
                info = self.agent.list_models_info()
                return {"ok": True, "models": [m["id"] for m in info], "details": info}
            elif act == "budget":
                n, src = self.agent.budget()
                return {"ok": True, "budget": n, "source": src, "short_tools": self.agent._short_tools()}
            else:
                raise ValueError(f"unknown action {act!r}")
            return {"ok": True, "agent": self.agent.state()}
        if path == "/api/storage":
            from . import config as _config
            out = {}
            for key in ("workspace", "data_dir"):
                if key not in body:
                    continue
                v = str(body.get(key) or "").strip().strip('"')
                if key == "workspace" and not v:
                    v = os.path.join(_config.ROOT, "workspace")
                if v:
                    v = os.path.abspath(os.path.expanduser(v))
                    try:
                        os.makedirs(v, exist_ok=True)
                        probe = os.path.join(v, ".sdrpilot_write_test")
                        with open(probe, "w") as f:
                            f.write("ok")
                        os.remove(probe)
                    except OSError as e:
                        raise ValueError(f"can't use {v}: {e}")
                out[key] = v
            cfg_path = _config.save_settings(**out)
            self.board.note(f"Storage folders changed ({', '.join(f'{k}={v or chr(39)*2}' for k, v in out.items())}) - "
                            "applies after SDR Pilot restarts", "event")
            return {"ok": True, "saved": out, "config": cfg_path,
                    "note": "Saved. Quit and reopen Claude (and run start_host.bat) to switch; existing files stay where "
                            "they are - move them yourself if you want them in the new place."}
        if path == "/api/context_prefs":
            if self.prefs is None:
                raise RuntimeError("not available")
            return {"ok": True, "prefs": self.prefs.set(shorten_results=body.get("shorten_results"),
                                                       result_max_chars=body.get("result_max_chars"),
                                                       task_reminders=body.get("task_reminders"),
                                                       task_after_reply=body.get("task_after_reply"),
                                                       task_reminder_every=body.get("task_reminder_every"),
                                                       vision_hints=body.get("vision_hints"),
                                                       cleanup=body.get("cleanup"),
                                                       image_days=body.get("image_days"))}
        if path == "/api/flowgraphs/prune":
            from . import fglib
            names = [str(n) for n in body.get("names") or []]
            running = {j["flowgraph"] for j in self.jobs.overview() if j["status"] in ("running", "paused")}
            names = [n for n in names if n not in running]
            res = fglib.archive(self.jobs.fg_dir, names)
            if res["archived"]:
                self.board.note(f"Archived {len(res['archived'])} flowgraph(s) from the dashboard", "event")
            return {"ok": True, **res}
        if path == "/api/flowgraphs/restore":
            from . import fglib
            dest = fglib.restore(self.jobs.fg_dir, str(body.get("name") or ""))
            return {"ok": True, "restored": dest}
        if path == "/api/job/stop":
            res = self.jobs.stop(str(body.get("job_id")), by="user")
            self.board.note(f"The user stopped job {body.get('job_id')} ({res.get('flowgraph')}) from the dashboard",
                            "event")
            return {"ok": True, "status": res.get("status")}
        if path == "/api/job/stop_all":
            n = 0
            for j in self.jobs.overview():
                if j["status"] in ("running", "paused"):
                    self.jobs.stop(j["job_id"], timeout=3, by="user")
                    n += 1
            self.board.note(f"The user stopped all {n} running job(s) from the dashboard", "event")
            return {"ok": True, "stopped": n}
        if path == "/api/job/pause":
            res = self.jobs.pause(str(body.get("job_id")), by="user")
            return {"ok": True, "status": res.get("status"), "state": res.get("state")}
        if path == "/api/job/resume":
            res = self.jobs.resume(str(body.get("job_id")))
            return {"ok": True, "status": res.get("status")}
        if path == "/api/job/run":
            if body.get("job_id"):
                res = self.jobs.rerun(str(body["job_id"]))
            else:
                name = str(body.get("name") or "")
                if not name:
                    raise ValueError("give a flowgraph name or a job_id")
                res = self.jobs.run(name, wait_s=3.0, by="user")
            self.board.note(f"The user started flowgraph '{res.get('flowgraph')}' as job {res.get('job_id')} from "
                            "the dashboard", "event")
            return {"ok": True, "job_id": res.get("job_id"), "status": res.get("status")}
        if path == "/api/monitor/stop":
            self.radio.monitor.stop()
            self.board.set_radio(monitor={"running": False})
            self.board.note("Monitor stopped from the dashboard", "event")
            return {"ok": True}
        if path == "/api/sessions/forget":
            # remove ended chats from the dashboard (their session files); running chats can't be removed
            pids = [int(body["pid"])] if body.get("pid") is not None else None
            removed = []
            for sd in self.sessions.all():
                pid = sd.get("pid")
                if self.sessions.alive(sd) or (pids is not None and pid not in pids):
                    continue
                try:
                    os.remove(os.path.join(self.sessions.dir, f"{int(pid)}.json"))
                    removed.append(pid)
                except (OSError, TypeError, ValueError):
                    pass
                # and the tool results it saved (its captures, flowgraphs and job outputs stay)
                import shutil
                shutil.rmtree(os.path.join(self.cfg.workspace, "state", "results", str(pid)), ignore_errors=True)
            return {"ok": True, "removed": removed}
        if path == "/api/host/start":
            return spawn_host(self.cfg, self.sessions)
        if path == "/api/instance/pause":
            self.radio.pause()
            self.board.note("Paused from the Instances page: this copy no longer reads the radio", "warn")
            return {"ok": True, "paused": True}
        if path == "/api/instance/resume":
            self.radio.resume()
            self.board.note("Resumed from the Instances page", "event")
            return {"ok": True, "paused": False}
        if path == "/api/instance/stop_jobs":
            n = sum(1 for j in self.jobs.overview() if j["status"] in ("running", "paused"))
            self.jobs.stop_all()
            return {"ok": True, "stopped": n}
        if path == "/api/instance/stop":
            if self.kind == "host":
                try:
                    with open(os.path.join(self.cfg.workspace, "state", "host_stopped_by_user"), "w") as f:
                        f.write(str(time.time()))
                except OSError:
                    pass

            def bye():
                for f in (self.jobs.stop_all, self.radio.disconnect):
                    try:
                        f()
                    except Exception:
                        pass
                try:
                    os.remove(os.path.join(self.sessions.dir, f"{os.getpid()}.json"))
                except Exception:
                    pass
                os._exit(0)
            threading.Timer(0.6, bye).start()
            return {"ok": True, "stopping": os.getpid()}
        if path == "/api/user_action":
            a = self.board.answer_user_action(int(body.get("id")), str(body.get("answer") or "done"),
                                              str(body.get("note") or ""))
            return {"ok": True, "action": a}
        if path == "/api/flowgraph/move":
            return self.flowgraph_move(str(body.get("name") or ""), body.get("moves") or {})
        if path == "/api/update/check":
            return self.updater.check(force=True)
        if path == "/api/update/repo":
            from . import config as _config
            repo = str(body.get("repo") or "").strip()
            _config.save_settings(update_repo=repo)
            self.cfg.update_repo = repo
            return self.updater.check(force=True) if repo else self.updater.status()
        if path == "/api/update/install":
            if self.kind != "host":
                raise RuntimeError("updates are installed by the background host - open its dashboard")
            return self.updater.install()
        if path == "/api/host/restart":
            if self.kind != "host":
                raise RuntimeError("this copy of SDR Pilot belongs to an AI chat - start a new chat (or restart Claude) "
                                   "to load an update; only the background host can restart itself")
            return self.restart_host()
        if path == "/api/host/stop":
            if self.kind != "host":
                raise RuntimeError("this isn't the host")
            if body.get("user"):  # stopped on purpose: the background supervisor leaves it stopped for a while
                try:
                    with open(os.path.join(self.cfg.workspace, "state", "host_stopped_by_user"), "w") as f:
                        f.write(str(time.time()))
                except OSError:
                    pass
            threading.Timer(0.5, lambda: os._exit(0)).start()
            return {"ok": True}
        raise KeyError(f"no such action {path}")

    def restart_host(self) -> dict:
        """Stop this host (its jobs and the radio stream) and start it again with the same command line, so it
        loads the code that's on disk now. A small helper process waits for this one to end, then relaunches."""
        argv = list(sys.argv)
        if argv and not os.path.isabs(argv[0]):
            argv[0] = os.path.join(ROOT, argv[0])
        cmd = [sys.executable, os.path.join(ROOT, "sdrpilot_app.py"), "relaunch", str(os.getpid()), "--", *argv]
        from .compat import child_env
        logf = open(os.path.join(self.cfg.workspace, "logs", "host_console.log"), "ab")
        kw: dict = {"stdin": subprocess.DEVNULL, "stdout": logf, "stderr": logf, "cwd": ROOT, "env": child_env(),
                    "close_fds": True}
        if os.name == "nt":
            base = 0x08000000 | 0x00000200  # CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP
            try:
                subprocess.Popen(cmd, creationflags=base | 0x01000000, **kw)  # CREATE_BREAKAWAY_FROM_JOB
            except OSError:
                subprocess.Popen(cmd, creationflags=base, **kw)
        else:
            subprocess.Popen(cmd, start_new_session=True, **kw)
        self.board.note("Restarting SDR Pilot to load the update", "event")

        def bye():
            for f in (self.jobs.stop_all, self.radio.disconnect):
                try:
                    f()
                except Exception:
                    pass
            os._exit(0)
        threading.Timer(0.8, bye).start()
        return {"ok": True, "restarting": True, "port": self.port}

    def activity_summary(self, minutes: float = 10.0) -> str:
        """What happened recently, from the activity log - no AI needed."""
        import collections
        now = time.time()
        acts = [e for e in self.board.activity if e["kind"] == "tool" and now - e["t"] < minutes * 60]
        task = self.board.task or {}
        parts = []
        if task.get("goal"):
            cur = next((s["title"] for s in task.get("steps") or [] if s["status"] == "active"), None)
            done = sum(1 for s in task.get("steps") or [] if s["status"] in ("done", "skipped"))
            parts.append(f"Task: {task['goal']} ({done}/{len(task.get('steps') or [])} steps done"
                         + (f", now: {cur}" if cur else "") + (", finished" if task.get("finished") else "") + ").")
        if acts:
            counts = collections.Counter(e["title"] for e in acts)
            errs = [e for e in acts if e["status"] == "error"]
            running = [e for e in acts if e["status"] == "running"]
            span = (now - acts[0]["t"]) / 60
            parts.append(f"Last {span:.0f} min: {len(acts)} tool calls - "
                         + ", ".join(f"{k} ×{v}" if v > 1 else k for k, v in counts.most_common(8)) + ".")
            if errs:
                parts.append(f"{len(errs)} failed (latest: {errs[-1]['title']}: {str(errs[-1].get('result', ''))[:120]}).")
            if running:
                parts.append(f"Running now: {running[-1]['title']}.")
            last_ok = next((e for e in reversed(acts) if e["status"] == "ok" and e.get("result")), None)
            if last_ok:
                parts.append(f"Latest result - {last_ok['title']}: {str(last_ok['result'])[:160]}")
        else:
            parts.append(f"No tool calls in the last {minutes:.0f} minutes.")
        focus = self.board.radio.get("focus_flowgraph")
        if focus:
            parts.append(f"Flowgraph: {focus} ({self.board.radio.get('focus_what') or 'in use'}).")
        return "📋 Activity summary: " + " ".join(parts)

    # ================================================================== flowgraphs / system
    def flowgraphs(self, sess: str = "") -> dict:
        out = []
        if sess and int(sess) != os.getpid():
            other = self.sessions.read(int(sess)) or {}
            jobs, focus = other.get("jobs", []), (other.get("radio") or {}).get("focus_flowgraph")
        else:
            jobs, focus = self.jobs.overview(), self.board.radio.get("focus_flowgraph")
        running = {j["flowgraph"]: j["job_id"] for j in jobs if j["status"] in ("running", "paused")}
        for name in self.jobs.list_flowgraphs():
            try:
                m = self.jobs.meta(name)
            except Exception:
                continue
            g = m.get("grc") if m.get("grc") and os.path.exists(m.get("grc")) else m.get("py")
            if g and m.get("kind") == "python":
                gp = os.path.join(os.path.dirname(g), "sdrpilot_graph.json")
                mt = max(os.path.getmtime(g) if os.path.exists(g) else 0, os.path.getmtime(gp) if os.path.exists(gp) else 0)
            else:
                mt = os.path.getmtime(g) if g and os.path.exists(g) else 0
            health = m.get("health") or ("invalid" if m.get("valid") is False else
                                         "draft" if m.get("draft") else "untested")
            out.append({"name": name, "kind": m.get("kind", "grc"), "valid": m.get("valid", True),
                        "draft": bool(m.get("draft")), "health": health,
                        "live": bool(m.get("uses_live_radio")), "mtime": mt,
                        "running_job": running.get(name)})
        out.sort(key=lambda f: -f["mtime"])
        rad = (other.get("radio") or {}) if sess and int(sess) != os.getpid() else self.board.radio
        return {"flowgraphs": out, "focus": focus, "focus_what": rad.get("focus_what") or "",
                "focus_t": rad.get("focus_t") or 0}

    def prune_preview(self, older_than_h: float = 1.0) -> dict:
        from . import fglib
        running = {j["flowgraph"] for j in self.jobs.overview() if j["status"] in ("running", "paused")}
        return {"candidates": fglib.candidates(self.jobs.fg_dir, running, older_than_h),
                "archived": fglib.archived(self.jobs.fg_dir)[:200]}

    def flowgraph(self, name: str) -> dict:
        from . import grc
        try:
            m = self.jobs.meta(name)
        except Exception as e:
            return {"error": str(e)}
        if not m.get("grc") or not os.path.exists(m["grc"]):
            if m.get("py") and os.path.exists(m["py"]):
                from . import pygraph
                try:
                    out = pygraph.describe(m["py"], name)
                except Exception as e:  # pragma: no cover
                    out = {"error": f"could not read the Python flowgraph: {e}", "name": name}
                out["running_job"] = next((j["job_id"] for j in self.jobs.overview()
                                           if j["status"] in ("running", "paused") and j["flowgraph"] == name), None)
                out["ai_notes"] = self._fg_notes(name)
                return out
            return {"error": "no diagram for this flowgraph", "name": name, "kind": m.get("kind")}
        try:
            d = dict(grc.describe_flowgraph(m["grc"]))
        except Exception as e:
            return {"error": f"could not read flowgraph: {e}", "name": name}
        d["name"] = name
        # descriptions: the AI's own (set_docs / doc) win; the radio/capture source is SDR Pilot's
        spec_blocks = {b.get("name"): b for b in (m.get("spec") or {}).get("blocks") or []}
        blocks = []
        for b in d.get("blocks", []):
            sb = spec_blocks.get(b["name"]) or {}
            b = dict(b)
            if sb.get("doc") or sb.get("description"):
                b["doc"], b["doc_by"] = grc._tidy_doc(sb.get("doc") or sb.get("description"), 400), "ai"
            elif sb.get("type") in grc.PSEUDO_BLOCKS:
                b["doc"], b["doc_by"] = grc.PSEUDO_BLOCKS[sb["type"]], "sdr pilot"
                b["label"] = {"sdr_source": "Radio (live IQ)", "capture_source": "Capture file"}.get(sb["type"], b["label"])
            b["ai_made"] = b.get("doc_by") == "ai" or sb.get("type") == "epy_block"
            blocks.append(b)
        d["blocks"] = blocks
        d["running_job"] = next((j["job_id"] for j in self.jobs.overview()
                                 if j["status"] in ("running", "paused") and j["flowgraph"] == name), None)
        d["draft"] = bool(m.get("draft"))
        d["history"] = (m.get("history") or [])[-40:]
        d["ai_notes"] = self._fg_notes(name)
        return d

    def _fg_notes(self, name: str):
        from . import fglib
        try:
            return fglib.notes_for(os.path.join(self.cfg.workspace, "state", "flowgraph_notes.json"),
                                   fglib.note_key(name), brief=False)
        except Exception:
            return None

    def system(self) -> dict:
        r = self.radio
        st = r.stream
        acts = [e for e in self.board.activity if e["kind"] == "tool"]
        last = acts[-1] if acts else None
        jobs = self.jobs.overview()
        ag = self.board.chat_state.get("agent") or {}
        return {
            "pid": os.getpid(), "kind": self.kind,
            "ai": (ag.get("label", "AI") + " · " + ag.get("model", "")) if self.kind == "host" and ag.get("model") else
                  ("Dashboard agent" if self.kind == "host" else "AI chat (MCP client)"),
            "mode": r.mode, "hw": r.hw_type, "center": r.center_freq, "rate": r.sample_rate,
            "sdrangel": f"{self.cfg.sdrangel_host}:{self.cfg.sdrangel_api_port}" if r.mode != "sim" else "built-in simulator",
            "stream": {"connected": bool(st and st.connected), "protocol": (st.meta.get("protocol") if st else None),
                       "bits": (st.meta.get("bits") if st else None),
                       "rate": round(st.rate_measured) if st and st.connected else 0,
                       "port": st.port if st else None},
            "zmq": {"address": r.publisher.address, "active": r.publisher.active, "subscribers": r.publisher.refs},
            "monitor": r.monitor.running, "capturing": r._capture is not None,
            "lease_other": r.lease.other_holder(),
            "last_tool": {"title": last["title"], "status": last["status"], "t": last["t"]} if last else None,
            "tool_running": any(e["status"] == "running" for e in acts[-5:]),
            "jobs": [{"id": j["job_id"], "name": j["flowgraph"], "status": j["status"], "live": j["uses_live_radio"],
                      "outputs": [o["file"] for o in j["outputs"]][:4]} for j in jobs][-6:],
            "grc_open": self.board.radio.get("grc_open"),
            "dashboard": self.url,
            "mcp_http": self.extra_info.get("mcp_http"),
        }

    def remote_info(self, client_kind: str = "local") -> dict:
        info = self.access.dashboard_urls(self.port, self.cfg.dashboard_bind)
        info["phone_links"] = {k: self.access.with_key(v) for k, v in info["urls"].items() if k != "this_pc"}
        info["viewer"] = client_kind
        info["bind"] = self.cfg.dashboard_bind
        info["hosts"] = [s for s in self.sessions.list() if s["kind"] == "host" and s["alive"]]
        if self.extra_info.get("mcp_http"):
            info["mcp_http"] = self.extra_info["mcp_http"]
        else:
            for d in self.sessions.all():
                if d.get("kind") == "host" and self.sessions.alive(d) and (d.get("system") or {}).get("mcp_http"):
                    info["mcp_http"] = d["system"]["mcp_http"]
        if info.get("mcp_http"):
            ts = self.access.tailscale()
            serve = {int(k): v for k, v in (ts.get("serve") or {}).items()}
            port = info["mcp_http"].get("port")
            if port in serve:
                info["mcp_http"]["tailscale_url"] = serve[port].rstrip("/") + f"/k/{self.access.key}/mcp"
            info["mcp_http"]["local_url"] = f"http://127.0.0.1:{port}/mcp"
        return info

    # ================================================================== live data
    def live(self, bins: int = 1024) -> dict:
        with self._lock:
            t, data = self._cache.get("live", (0.0, None))
            if time.time() - t >= 0.2:
                try:
                    data = self.radio.live_psd()
                except Exception:
                    data = None
                self._cache["live"] = (time.time(), data)
        if not data:
            return {}
        try:
            h = self.radio.stream_health()
            if h:
                data = {**data, "health": h}
        except Exception:  # pragma: no cover
            pass
        if bins and bins < len(data["avg"]):
            k = len(data["avg"]) // bins
            if k > 1:
                a = np.asarray(data["avg"])[: bins * k].reshape(bins, k)
                m = np.asarray(data["max"])[: bins * k].reshape(bins, k)
                data = {**data, "avg": np.round(10 * np.log10(np.mean(10 ** (a / 10), axis=1)), 1).tolist(),
                        "max": np.round(m.max(axis=1), 1).tolist()}
        return data

    def flowgraph_move(self, name: str, moves: dict) -> dict:
        """Blocks the user dragged in the flowgraph view: save their new positions into the .grc (what GNU Radio
        Companion shows) and into SDR Pilot's own copy of the layout, so the AI's next edit keeps them."""
        import yaml
        from . import grc
        m = self.jobs.meta(name)
        path = m.get("grc")
        if not path or not os.path.exists(path):
            raise RuntimeError("only GNU Radio Companion flowgraphs can be rearranged here (this one is Python code)")
        clean = {}
        for k, v in (moves or {}).items():
            if isinstance(v, (list, tuple)) and len(v) >= 2:
                clean[str(k)] = [int(round(max(0.0, float(v[0])))), int(round(max(0.0, float(v[1]))))]
        if not clean:
            raise ValueError("nothing to move")
        with grc.GRC_LOCK:
            with open(path, encoding="utf-8") as f:
                text = f.read()
            if "<flow_graph>" in text[:400]:
                raise RuntimeError("this is an old GNU Radio 3.7 (XML) flowgraph - open it in GRC to rearrange it")
            data = yaml.safe_load(text) or {}
            hit = []
            for b in data.get("blocks") or []:
                if b.get("name") in clean:
                    st = b.get("states")
                    if not isinstance(st, dict):
                        st = b["states"] = {}
                    st["coordinate"] = clean[b["name"]]
                    hit.append(b["name"])
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                yaml.safe_dump(data, f, sort_keys=False, allow_unicode=True)
            os.replace(tmp, path)
            side = os.path.join(os.path.dirname(path), "sdrpilot.json")
            try:
                with open(side, encoding="utf-8") as f:
                    meta = json.load(f)
                for sb in (meta.get("spec") or {}).get("blocks") or []:
                    if sb.get("name") in clean:
                        sb["pos"] = clean[sb["name"]]
                with open(side + ".tmp", "w", encoding="utf-8") as f:
                    json.dump(meta, f, indent=2)
                os.replace(side + ".tmp", side)
            except (OSError, ValueError):
                pass
            try:
                grc._DESCRIBE_CACHE.clear()
            except Exception:
                pass
        return {"ok": True, "moved": hit, "missing": sorted(set(clean) - set(hit))}

    def update_status(self) -> dict:
        """Release check for the banner: answered at once; a due check runs in the background."""
        u = self.updater
        st = u.status()
        if st["enabled"] and self.kind == "host" and time.time() - st["checked"] > \
                float(getattr(self.cfg, "update_check_hours", 6) or 6) * 3600 and not getattr(u, "_checking", False):
            u._checking = True

            def run():
                try:
                    u.check()
                finally:
                    u._checking = False
            threading.Thread(target=run, daemon=True, name="sdrpilot-update-check").start()
        st["kind"] = self.kind
        return st

    # settings the panel leads with, per device type (everything else follows in its own order)
    RADIO_KEY_FIELDS = {
        "hackRFInputSettings": ["lnaExt", "lnaGain", "vgaGain", "bandwidth", "devSampleRate", "log2Decim", "fcPos",
                                "dcBlock", "iqCorrection", "biasT", "LOppmTenths"],
        "rtlSdrSettings": ["gain", "agc", "rfBandwidth", "devSampleRate", "log2Decim", "loPpmCorrection", "biasTee"],
        "airspySettings": ["lnaGain", "mixerGain", "vgaGain", "lnaAGC", "mixerAGC", "biasT"],
    }
    RADIO_SKIP = ("reverseAPI", "useReverseAPI", "fileRecordName", "title", "rgbColor", "streamIndex",
                  "workspaceIndex", "geometry")

    def radio_panel(self) -> dict:
        """Everything about the receiver the AI can change - for the dashboard's Radio panel. Polled every ~2 s; the
        SDRangel query and the level measurement are cached so several open dashboards cost no more."""
        from . import devices
        with self._lock:
            t, data = self._cache.get("radio", (0.0, None))
            if data is not None and time.time() - t < 1.5:
                return data
        r = self.radio
        out: dict = {"connected": bool(r.connected), "mode": r.mode, "t": time.time()}
        st = r.stream
        out["stream"] = {"connected": bool(st and st.connected),
                         "rate": round(st.rate_measured) if st and st.connected else 0,
                         "protocol": st.meta.get("protocol") if st else None, "bits": st.meta.get("bits") if st else None}
        if r.connected and r.api is not None:
            try:
                s = r.api.summary()
            except Exception as e:
                out["error"] = str(e)[:200]
                s = None
            if s:
                key = s.get("settings_key") or ""
                dev = s.get("device_settings") or {}
                out.update({"hw_type": s.get("hw_type"), "state": s.get("state"), "settings_key": key,
                            "center_freq_hz": s.get("center_freq_hz"), "sample_rate": s.get("baseband_rate")})
                try:
                    out["total_gain_db"] = devices.current_gain_db(key, dev)
                except Exception:
                    out["total_gain_db"] = None
                order = [k for k in self.RADIO_KEY_FIELDS.get(key, []) if k in dev]
                order += [k for k in dev if k not in order]
                fields = []
                for k in order:
                    if k.startswith(self.RADIO_SKIP) or k == "centerFrequency" or isinstance(dev[k], (dict, list)):
                        continue
                    label, desc = devices.friendly(key, k)
                    fields.append({"key": k, "label": label, "desc": desc, "value": dev[k],
                                   "main": k in self.RADIO_KEY_FIELDS.get(key, [])})
                out["fields"] = fields
                out["chain"] = [k for k in devices.GAIN_CHAIN.get(key, []) if k in dev]
                out["channels"] = s.get("channels") or []
                self._radio_track(out, dev)
        try:
            lvl = r.signal_level() if r.connected else None
            if lvl:
                out["level"] = {k: lvl.get(k) for k in ("rms_dbfs", "peak_dbfs", "clipped_pct", "verdict")}
        except Exception:
            pass
        out["changes"] = list(self._radio_changes)[-12:]
        with self._lock:
            self._cache["radio"] = (time.time(), out)
        return out

    def _radio_track(self, out: dict, dev: dict) -> None:
        """Note what changed since the last look, and who most likely changed it."""
        now = time.time()
        cur = {"center_freq_hz": out.get("center_freq_hz"), "sample_rate": out.get("sample_rate"),
               **{k: v for k, v in dev.items() if not isinstance(v, (dict, list)) and not k.startswith(self.RADIO_SKIP)}}
        prev = self._radio_prev
        self._radio_prev = cur
        if prev is None:
            return
        diff = {k: (prev.get(k), v) for k, v in cur.items() if prev.get(k) != v and k != "centerFrequency"}
        if not diff:
            return
        recent = [e for e in self.board.activity if e.get("kind") == "tool" and now - e["t"] < 8]
        flow = None
        try:
            for j in self.jobs.overview():
                if j.get("status") == "running" and (j.get("radio_control") or {}).get("applied"):
                    flow = "flowgraph " + str(j.get("flowgraph"))
                    break
        except Exception:
            pass
        freq_keys = {"center_freq_hz", "sample_rate", "devSampleRate", "log2Decim", "fcPos", "bandwidth"}
        for k, (a, b) in diff.items():
            # the tool that changes this kind of setting; any radio tool if none matches
            pat = (r"^(Tune|Band sweep|Use recipe|Run flowgraph|Connect radio|Select SDR|Device settings)" if k in freq_keys
                   else r"^(Set gain|Device settings|Connect radio|Select SDR|Use recipe)")
            hit = [e for e in recent if re.search(pat, e.get("title", ""))] or \
                  [e for e in recent if re.search(r"radio|tune|gain|recipe|run|sweep|setting|SDR", e.get("title", ""), re.I)]
            by = ("AI: " + hit[-1]["title"]) if hit else (flow or "outside SDR Pilot (SDRangel / PortaPack)")
            self._radio_changes.append({"t": now, "key": k, "from": a, "to": b, "by": by})
        del self._radio_changes[:-40]

    def wf(self, cursor: int | None, bins: int = 512) -> dict:
        """Gap-free waterfall rows from the spectrum recorder (the same data the AI's spectrum_watch reads):
        every 50 ms of signal as one row, peak-held, 8-bit (0.5 dB steps above `lo`), base64."""
        rec = getattr(self.radio, "recorder", None)
        if rec is None or not self.radio.connected or rec.n == 0 or not rec.running:
            d = self.live(bins)
            return {"mode": "psd", **d} if d else {"mode": "none"}
        if cursor is None or cursor > rec.n or rec.n - cursor > 120:
            cursor = max(0, rec.n - 60)
        try:
            health = self.radio.stream_health()
        except Exception:  # pragma: no cover
            health = {}
        d = rec.rows(since_cursor=cursor)
        if d is None:
            return {"mode": "rows", "cursor": rec.n, "n": 0, "health": health}
        peak, mean = d["peak"][-120:], d["mean"][-120:]
        nb = peak.shape[1]
        bins = int(min(max(bins, 64), nb))
        k = max(1, nb // bins)
        if k > 1:
            peak = peak[:, : (nb // k) * k].reshape(len(peak), -1, k).max(axis=2)
            mean = mean[:, : (nb // k) * k].reshape(len(mean), -1, k).mean(axis=2)
        lo = float(np.floor(np.percentile(mean, 10)) - 8)
        q = np.clip(np.round((peak - lo) * 2), 0, 255).astype(np.uint8)
        avg = 10 * np.log10(np.mean(10 ** (mean / 10), axis=0))
        return {"mode": "rows", "cursor": int(d["ids"][-1]) + 1, "center": d["fc"], "rate": d["fs"],
                "bins": int(q.shape[1]), "n": int(q.shape[0]), "lo": lo, "step": 0.5, "slice_s": rec.slice_s,
                "t_last": float(d["t"][-1]), "data": base64.b64encode(q.tobytes()).decode(),
                "avg": np.round(avg, 1).tolist(), "max": np.round(peak.max(axis=0), 1).tolist(), "health": health}

    def signals(self, seconds: float = 30.0) -> dict:
        """Transmitter table (same as spectrum_watch's) for the dashboard, cached for 2 s."""
        with self._lock:
            t, data = self._cache.get("signals", (0.0, None))
            if time.time() - t < 2.0 and data is not None:
                return data
        from . import watch
        rec = getattr(self.radio, "recorder", None)
        if rec is None or rec.n == 0 or not self.radio.connected:
            data = {"transmitters": [], "note": "radio not connected"}
        else:
            try:
                r = watch.render(rec, float(min(max(seconds, 2), 300)), None, None, None, 20, 4, 8.0, None)
                data = {k: r.get(k) for k in ("transmitters", "noise_floor_dbfs", "window_s", "center_freq_mhz",
                                               "sample_rate") if k in r}
                data["transmitters"] = r.get("transmitters") or []
            except Exception as e:  # pragma: no cover
                data = {"transmitters": [], "note": str(e)}
        with self._lock:
            self._cache["signals"] = (time.time(), data)
        return data


def spawn_host(cfg, sessions: SessionStore | None) -> dict:
    """Start the SDR Pilot host (dashboard agent + MCP over HTTP) in the background, if none is running."""
    if sessions:
        for d in sessions.all():
            if d.get("kind") == "host" and sessions.alive(d):
                return {"ok": True, "already_running": True, "pid": d.get("pid")}
    from .compat import child_env
    log_dir = os.path.join(cfg.workspace, "logs")
    os.makedirs(log_dir, exist_ok=True)
    cmd = [sys.executable, os.path.join(ROOT, "run_server.py"), "--host", "--idle-exit", "180"]
    logf = open(os.path.join(log_dir, "host_console.log"), "ab")
    kw: dict = {"stdin": subprocess.DEVNULL, "stdout": logf, "stderr": logf, "cwd": ROOT, "env": child_env(),
                "close_fds": True}
    if os.name == "nt":
        base = 0x08000000 | 0x00000200  # CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP
        try:  # outlive the Claude app's process job if it allows that
            p = subprocess.Popen(cmd, creationflags=base | 0x01000000, **kw)  # CREATE_BREAKAWAY_FROM_JOB
        except OSError:
            p = subprocess.Popen(cmd, creationflags=base, **kw)
    else:
        p = subprocess.Popen(cmd, start_new_session=True, **kw)
    return {"ok": True, "started": True, "pid": p.pid}
