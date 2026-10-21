"""Is this SDR Pilot process still running the code that is on disk?

A running copy keeps the code it started with. After an update (new files copied in), every copy that was already
running - the background host especially, which restarting Claude doesn't restart - keeps the old behaviour
until it restarts. This notes the code files' modification times at startup and reports which changed since."""
from __future__ import annotations

import os
import threading
import time

PKG = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(PKG)
# dashboard.html is re-read on every page load, so a change to it needs no restart
_EXTS = (".py", ".json")
_SKIP = {"dashboard.html"}


def _scan() -> dict[str, float]:
    out = {}
    for folder, names in ((PKG, None), (ROOT, ("run_server.py", "sdrpilot_app.py"))):
        try:
            entries = os.listdir(folder) if names is None else names
        except OSError:
            continue
        for n in entries:
            if n in _SKIP or not (n.endswith(_EXTS) or n == "BUILD"):
                continue
            p = os.path.join(folder, n)
            try:
                out[os.path.relpath(p, ROOT).replace("\\", "/")] = os.path.getmtime(p)
            except OSError:
                pass
    return out


STARTED = time.time()
_BASE = _scan()
_lock = threading.Lock()
_cache: tuple[float, dict | None] = (0.0, None)


def stale(max_age: float = 5.0) -> dict | None:
    """None if the files on disk are the ones this process loaded, else {"files": [...], "updated": unix time}."""
    global _cache
    with _lock:
        t, res = _cache
        if time.time() - t < max_age:
            return res
        now = _scan()
        changed = sorted(f for f, m in now.items() if _BASE.get(f) != m)
        res = None
        if changed:
            res = {"files": changed, "updated": max(now.get(f, 0) for f in changed), "running_since": STARTED}
            try:
                from . import __build__, _read_build, build_label
                disk = _read_build()
                res["running_build"], res["disk_build"] = __build__, disk
                res["running_label"], res["disk_label"] = build_label(__build__), build_label(disk)
            except Exception:  # pragma: no cover
                pass
        _cache = (time.time(), res)
        return res


def wait_pid_exit(pid: int, timeout: float) -> bool:
    """Wait for another process to end (True if it did)."""
    end = time.time() + timeout
    if os.name == "nt":
        import ctypes
        k32 = ctypes.windll.kernel32
        h = k32.OpenProcess(0x00100000, False, int(pid))  # SYNCHRONIZE
        if not h:
            return True  # already gone
        try:
            return k32.WaitForSingleObject(h, int(max(0, timeout) * 1000)) == 0
        finally:
            k32.CloseHandle(h)
    while time.time() < end:
        try:
            os.kill(int(pid), 0)
        except ProcessLookupError:
            return True
        except PermissionError:
            pass
        time.sleep(0.2)
    return False


def sync_install_registry() -> str | None:
    """Windows: when this copy is the installed one, write the build it is running into the 'Apps & features' entry
    the installer made, so the installer (and Windows) see an update done by copying files + the dashboard's restart
    button the same as one the installer did. Returns the version written, or None."""
    if os.name != "nt":
        return None
    try:
        import winreg
        from . import __build__
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\SDR Pilot") as k:
            inst = winreg.QueryValueEx(k, "InstallDir")[0]
        if os.path.normcase(os.path.normpath(inst)) != os.path.normcase(os.path.normpath(ROOT)):
            return None  # a different copy (e.g. the project folder) - leave the installed one's entry alone
        key = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\SDR Pilot"
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key, 0, winreg.KEY_READ | winreg.KEY_SET_VALUE) as k:
            try:
                cur = winreg.QueryValueEx(k, "DisplayVersion")[0]
            except OSError:
                cur = ""
            if cur != __build__:
                winreg.SetValueEx(k, "DisplayVersion", 0, winreg.REG_SZ, __build__)
        return __build__
    except OSError:
        return None
    except Exception:  # pragma: no cover
        return None
