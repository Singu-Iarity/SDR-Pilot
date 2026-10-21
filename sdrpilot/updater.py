"""Updates from a published release: check, download, verify, install.

SDR Pilot releases are published on GitHub Releases (free, always-on HTTPS hosting; nothing to run yourself). Each
release carries the Windows installer named SDR-Pilot-Setup-<build>.exe (build = the stamp in sdrpilot/BUILD, e.g.
0.9.0.20261021.1315). The dashboard checks for a newer build now and then, shows a banner, and on the user's click
downloads the installer, checks its SHA-256 (GitHub's own asset digest, or a published .sha256 file - an installer
without one is refused), and starts it in silent update mode. The installer stops SDR Pilot, replaces the program
files (settings and data are kept), and starts it again; open dashboards reload by themselves.

Nothing is ever installed without a click. sdrpilot_config.json: "update_repo": "owner/repo" (empty = no update
checks), "update_check_hours": 6.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.request

from . import __build__, build_label

API = "https://api.github.com/repos/{repo}/releases/latest"
ASSET_RE = re.compile(r"^SDR-Pilot-Setup-(\d+(?:\.\d+)+)\.exe$", re.I)
UA = {"User-Agent": "SDR-Pilot-updater", "Accept": "application/vnd.github+json"}


def vtuple(v: str) -> tuple:
    out = []
    for p in str(v or "").split("."):
        try:
            out.append(int(p))
        except ValueError:
            out.append(0)
    return tuple(out)


class Updater:
    def __init__(self, cfg):
        self.cfg = cfg
        self.lock = threading.Lock()
        self.state: dict = {"current": __build__, "current_label": build_label(__build__), "checked": 0.0,
                            "available": None, "error": None, "phase": "idle", "progress": 0.0}
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------ settings
    @property
    def repo(self) -> str:
        r = str(getattr(self.cfg, "update_repo", "") or "").strip().strip("/")
        m = re.search(r"github\.com/([^/\s]+/[^/\s#?]+)", r)
        if m:
            r = m.group(1)
        return r[:-4] if r.endswith(".git") else r

    def status(self) -> dict:
        with self.lock:
            out = dict(self.state)
        out["repo"] = self.repo
        out["enabled"] = bool(self.repo)
        out["can_install"] = os.name == "nt"
        return out

    # ------------------------------------------------------------ check
    def check(self, force: bool = False) -> dict:
        if not self.repo:
            return self.status()
        every = float(getattr(self.cfg, "update_check_hours", 6) or 6) * 3600
        with self.lock:
            if not force and time.time() - self.state["checked"] < every:
                return dict(self.state, repo=self.repo, enabled=True, can_install=os.name == "nt")
        try:
            req = urllib.request.Request(API.format(repo=self.repo), headers=UA)
            with urllib.request.urlopen(req, timeout=15) as r:
                rel = json.loads(r.read())
            avail = None
            for a in rel.get("assets") or []:
                m = ASSET_RE.match(a.get("name", ""))
                if not m:
                    continue
                build = m.group(1)
                if vtuple(build) > vtuple(__build__):
                    digest = str(a.get("digest") or "")
                    sha = digest.split(":", 1)[1] if digest.lower().startswith("sha256:") else ""
                    side = next((b for b in rel.get("assets") or [] if b.get("name") == a["name"] + ".sha256"), None)
                    avail = {"build": build, "label": build_label(build), "name": a["name"], "size": a.get("size"),
                             "url": a.get("browser_download_url"), "sha256": sha.lower(),
                             "sha256_url": side.get("browser_download_url") if side else "",
                             "release": rel.get("name") or rel.get("tag_name"), "notes": (rel.get("body") or "")[:4000],
                             "published": rel.get("published_at"), "page": rel.get("html_url")}
                break
            with self.lock:
                self.state.update(checked=time.time(), available=avail, error=None)
        except Exception as e:  # offline, rate-limited, repo missing...
            code = getattr(e, "code", None)
            msg = (f"no published release found at github.com/{self.repo} yet" if code == 404 else
                   "GitHub's rate limit was reached - it checks again later" if code in (403, 429) else
                   f"{type(e).__name__}: {e}")
            with self.lock:
                self.state.update(checked=time.time(), error=msg[:300])
        return self.status()

    # ------------------------------------------------------------ download + install
    def install(self) -> dict:
        """Download the newer installer, verify it, and start it (silent update). Runs in the background."""
        if os.name != "nt":
            raise RuntimeError("automatic install is Windows-only - on Linux/macOS update from the release page")
        st = self.status()
        if not st.get("available"):
            st = self.check(force=True)
        if not st.get("available"):
            raise RuntimeError("no newer release to install" + (f" ({st['error']})" if st.get("error") else ""))
        with self.lock:
            if self.state["phase"] in ("downloading", "verifying", "installing"):
                return dict(self.state)
            self.state.update(phase="downloading", progress=0.0, error=None)
        self._thread = threading.Thread(target=self._run, args=(st["available"],), daemon=True, name="sdrpilot-update")
        self._thread.start()
        return self.status()

    def _set(self, **kw) -> None:
        with self.lock:
            self.state.update(**kw)

    def _run(self, a: dict) -> None:
        try:
            d = os.path.join(self.cfg.workspace, "updates")
            os.makedirs(d, exist_ok=True)
            path = os.path.join(d, a["name"])
            sha = a.get("sha256") or ""
            if not sha and a.get("sha256_url"):
                with urllib.request.urlopen(urllib.request.Request(a["sha256_url"], headers=UA), timeout=20) as r:
                    sha = r.read().decode("ascii", "replace").split()[0].strip().lower()
            if not re.fullmatch(r"[0-9a-f]{64}", sha or ""):
                raise RuntimeError("the release has no SHA-256 checksum for the installer - refusing to run it")
            h = hashlib.sha256()
            got, total = 0, int(a.get("size") or 0)
            with urllib.request.urlopen(urllib.request.Request(a["url"], headers=UA), timeout=60) as r, \
                    open(path + ".part", "wb") as f:
                while True:
                    chunk = r.read(1 << 16)
                    if not chunk:
                        break
                    f.write(chunk)
                    h.update(chunk)
                    got += len(chunk)
                    if total:
                        self._set(progress=round(got / total, 3))
            self._set(phase="verifying")
            if total and got != total:
                raise RuntimeError(f"download incomplete ({got} of {total} bytes)")
            if h.hexdigest() != sha:
                os.remove(path + ".part")
                raise RuntimeError("the downloaded installer doesn't match its published SHA-256 - not run")
            os.replace(path + ".part", path)
            self._set(phase="installing", progress=1.0)
            # detached: the installer stops this very process, updates the files and starts SDR Pilot again
            flags = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
            subprocess.Popen([path, "/S", "/UPDATE"], close_fds=True, creationflags=flags,
                             cwd=d, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception as e:
            self._set(phase="failed", error=f"{e}"[:400])
