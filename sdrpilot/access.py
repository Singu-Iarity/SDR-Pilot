"""Remote access: the access key, who counts as "this PC", Tailscale detection, and the links to hand out.

Model:
- A request from this PC's own browser (loopback, no proxy headers) is trusted.
- Everything else (a phone over Tailscale, `tailscale serve`, the LAN) needs the access key once; the dashboard
  then remembers it in a cookie.
- Every request must name a sane Host (localhost, an IP, a *.ts.net / single-word name or a configured name).
  That stops DNS-rebinding tricks from a malicious web page in the PC's browser.
- State-changing requests (POST) always need the key in a header, which a foreign web page cannot send.
"""
from __future__ import annotations

import ipaddress
import json
import os
import secrets
import shutil
import subprocess
import threading
import time
import urllib.parse

TS_V4 = ipaddress.ip_network("100.64.0.0/10")
TS_V6 = ipaddress.ip_network("fd7a:115c:a1e0::/48")
PROXY_HEADERS = ("x-forwarded-for", "x-forwarded-host", "x-forwarded-proto", "forwarded", "x-real-ip",
                 "tailscale-user-login", "tailscale-funnel-request")
NO_WINDOW = 0x08000000 if os.name == "nt" else 0  # don't flash a console window from the hidden MCP server


def _tailscale_exe() -> str | None:
    exe = shutil.which("tailscale")
    if exe:
        return exe
    for p in (r"C:\Program Files\Tailscale\tailscale.exe", r"C:\Program Files (x86)\Tailscale\tailscale.exe",
              "/Applications/Tailscale.app/Contents/MacOS/Tailscale", "/usr/bin/tailscale", "/usr/local/bin/tailscale"):
        if os.path.exists(p):
            return p
    return None


def run_tailscale(*args: str, timeout: float = 8.0) -> tuple[int, str]:
    exe = _tailscale_exe()
    if not exe:
        return 127, "tailscale command not found (is Tailscale installed?)"
    try:
        p = subprocess.run([exe, *args], capture_output=True, text=True, timeout=timeout, creationflags=NO_WINDOW)
        return p.returncode, (p.stdout + p.stderr).strip()
    except subprocess.TimeoutExpired:
        return 124, f"tailscale {' '.join(args)} timed out"
    except OSError as e:
        return 126, str(e)


class Access:
    def __init__(self, cfg):
        self.cfg = cfg
        self.state_dir = os.path.join(cfg.workspace, "state")
        os.makedirs(self.state_dir, exist_ok=True)
        self.key = cfg.access_token or self._load_key()
        self._ts: dict = {"checked": 0.0}
        self._ts_lock = threading.Lock()
        threading.Thread(target=self.tailscale, kwargs={"refresh": True}, daemon=True, name="sdrpilot-ts").start()

    # ------------------------------------------------------------------ key
    def key_path(self) -> str:
        return os.path.join(self.state_dir, "access_key.txt")

    def _load_key(self) -> str:
        p = self.key_path()
        for _ in range(20):
            try:
                with open(p, encoding="utf-8") as f:
                    k = f.read().strip()
                if len(k) >= 16:
                    return k
            except OSError:
                pass
            try:  # exclusive create: several SDR Pilot copies may start at once
                fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                k = secrets.token_urlsafe(18)
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(k)
                return k
            except FileExistsError:
                time.sleep(0.05)
        raise RuntimeError(f"could not read or create {p}")

    def check(self, value: str | None) -> bool:
        return bool(value) and secrets.compare_digest(str(value).strip(), self.key)

    # ------------------------------------------------------------------ requests
    @staticmethod
    def client_kind(ip: str, headers: dict) -> str:
        """local (this PC's browser), proxy (this PC, but forwarded e.g. by tailscale serve), tailnet, lan, internet."""
        try:
            a = ipaddress.ip_address(ip.split("%")[0])
            if getattr(a, "ipv4_mapped", None):
                a = a.ipv4_mapped
        except ValueError:
            return "internet"
        if a.is_loopback:
            return "proxy" if any(h in headers for h in PROXY_HEADERS) else "local"
        if a in TS_V4 or a in TS_V6:
            return "tailnet"
        if a.is_private or a.is_link_local:
            return "lan"
        return "internet"

    def bind_allows(self, kind: str, bind: str) -> bool:
        if kind in ("local", "proxy"):
            return True
        if bind == "tailscale":
            return kind == "tailnet"
        if bind == "all":
            return kind in ("tailnet", "lan")
        return False

    def host_ok(self, host_header: str | None) -> bool:
        if not host_header:
            return True
        h = host_header.strip().lower()
        if h.startswith("["):
            name = h[1:h.find("]")] if "]" in h else h
        elif h.count(":") == 1:
            name = h.split(":")[0]
        else:
            name = h
        name = name.rstrip(".")
        if name in ("localhost",) or name.endswith((".localhost", ".ts.net", ".local", ".lan", ".home.arpa")):
            return True
        try:
            ipaddress.ip_address(name)
            return True
        except ValueError:
            pass
        if "." not in name:  # MagicDNS short names, Windows computer names
            return True
        return name in [str(x).lower().rstrip(".") for x in (self.cfg.allowed_hosts or [])]

    @staticmethod
    def bind_address(bind: str) -> str:
        return "127.0.0.1" if bind not in ("tailscale", "all") else "0.0.0.0"

    # ------------------------------------------------------------------ tailscale
    def tailscale(self, refresh: bool = False) -> dict:
        with self._ts_lock:
            if not refresh and time.time() - self._ts.get("checked", 0) < 60:
                return dict(self._ts)
        info: dict = {"checked": time.time(), "installed": bool(_tailscale_exe()), "running": False, "ip4": None,
                      "dns_name": None, "short_name": None, "serve": {}, "funnel": [], "error": None}
        if info["installed"]:
            rc, out = run_tailscale("status", "--json")
            if rc == 0:
                try:
                    st = json.loads(out)
                    me = st.get("Self") or {}
                    info["running"] = st.get("BackendState") == "Running"
                    ips = me.get("TailscaleIPs") or []
                    info["ip4"] = next((i for i in ips if "." in i), None)
                    dns = (me.get("DNSName") or "").rstrip(".")
                    info["dns_name"] = dns or None
                    info["short_name"] = dns.split(".")[0] if dns else (me.get("HostName") or None)
                except ValueError as e:
                    info["error"] = f"unreadable tailscale status: {e}"
            else:
                info["error"] = out[-300:]
            rc, out = run_tailscale("serve", "status", "--json")
            if rc == 0 and out.strip().startswith("{"):
                try:
                    info["serve"], info["funnel"] = self._parse_serve(json.loads(out))
                except Exception as e:  # pragma: no cover
                    info["error"] = f"unreadable serve status: {e}"
        if not info["ip4"]:  # no CLI: look for a 100.64/10 address on this machine
            try:
                import socket
                for fam, *_r, addr in socket.getaddrinfo(socket.gethostname(), None):
                    a = ipaddress.ip_address(addr[0].split("%")[0])
                    if a.version == 4 and a in TS_V4:
                        info["ip4"] = str(a)
                        info["running"] = True
                        break
            except Exception:
                pass
        with self._ts_lock:
            self._ts = info
        return dict(info)

    @staticmethod
    def _parse_serve(cfg: dict) -> tuple[dict, list]:
        """tailscale serve status --json -> {local_port: https_url}, [funnel host:ports]."""
        out: dict = {}
        for hostport, web in (cfg.get("Web") or {}).items():
            host, _, port = hostport.rpartition(":")
            base = f"https://{host}" + ("" if port == "443" else f":{port}")
            for path, h in (web.get("Handlers") or {}).items():
                proxy = (h or {}).get("Proxy") or ""
                if not proxy:
                    continue
                pu = urllib.parse.urlparse(proxy if "://" in proxy else "http://" + proxy)
                if pu.hostname in ("127.0.0.1", "localhost", "::1") and pu.port:
                    out[int(pu.port)] = base + (path if path.endswith("/") else path + "/")
        funnel = [hp for hp, on in (cfg.get("AllowFunnel") or {}).items() if on]
        return out, funnel

    def dashboard_urls(self, port: int, bind: str) -> dict:
        """Ways to open the dashboard, best first. Each URL is given without the key; see with_key()."""
        ts = self.tailscale()
        urls = {"this_pc": f"http://127.0.0.1:{port}/"}
        notes = []
        serve = {int(k): v for k, v in (ts.get("serve") or {}).items()}
        front = min(serve) if serve else None
        if port in serve:
            urls["tailscale_https"] = serve[port]
        elif front is not None and 8765 <= front <= 8780 and front != port:
            urls["tailscale_https"] = serve[front]  # another SDR Pilot copy's dashboard shows every chat
        if bind in ("tailscale", "all") and ts.get("ip4"):
            urls["tailscale_direct"] = f"http://{ts['ip4']}:{port}/"
            if ts.get("short_name"):
                urls["tailscale_name"] = f"http://{ts['short_name']}:{port}/"
        if not ts.get("installed"):
            notes.append("Tailscale isn't installed on this PC.")
        elif not ts.get("running"):
            notes.append("Tailscale is installed but not connected.")
        elif "tailscale_https" not in urls and "tailscale_direct" not in urls:
            notes.append("Not reachable from your other devices yet: run remote_access.bat (or "
                         "`python remote_setup.py serve`) once.")
        return {"urls": urls, "tailscale": {k: ts.get(k) for k in ("installed", "running", "ip4", "dns_name")},
                "notes": notes}

    def with_key(self, url: str) -> str:
        return url + ("&" if "?" in url else "?") + "key=" + self.key


class KeyGate:
    """ASGI middleware in front of the MCP-over-HTTP endpoint: same host/network checks as the dashboard, and the
    access key as `Authorization: Bearer <key>`, `?key=<key>`, or a path prefix /k/<key>/mcp (for clients that
    only take a URL, e.g. a claude.ai custom connector). Requests from this PC itself need no key."""

    def __init__(self, app, access: Access, bind: str):
        self.app, self.access, self.bind = app, access, bind
        self.last_request = 0.0

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            return await self.app(scope, receive, send)
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers") or []}
        client = (scope.get("client") or ("127.0.0.1", 0))[0]
        kind = self.access.client_kind(client, headers)
        path = scope.get("path") or "/"
        key = None
        if path.startswith("/k/"):
            parts = path.split("/", 3)
            key = parts[2] if len(parts) > 2 else ""
            path = "/" + (parts[3] if len(parts) > 3 else "")
            scope = dict(scope, path=path, raw_path=path.encode())
        auth = headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            key = key or auth[7:].strip()
        qs = urllib.parse.parse_qs((scope.get("query_string") or b"").decode("latin-1"))
        key = key or (qs.get("key") or [None])[0]
        if not self.access.host_ok(headers.get("host")):
            return await self._deny(send, 403, "unexpected Host header")
        if not self.access.bind_allows(kind, self.bind):
            return await self._deny(send, 403, f"{kind} access to the MCP endpoint is off (mcp_http_bind)")
        if kind != "local" and not self.access.check(key):
            return await self._deny(send, 401, "access key required")
        self.last_request = time.time()
        return await self.app(scope, receive, send)

    @staticmethod
    async def _deny(send, code: int, msg: str):
        body = json.dumps({"error": msg}).encode()
        await send({"type": "http.response.start", "status": code,
                    "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]})
        await send({"type": "http.response.body", "body": body})
