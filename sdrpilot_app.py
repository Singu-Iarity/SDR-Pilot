"""SDR Pilot as a standalone app - no Claude app needed.

    pythonw sdrpilot_app.py            start SDR Pilot (if it isn't running) and open the dashboard in an app window
    python  sdrpilot_app.py url        start SDR Pilot if needed and print the dashboard URL (used by SDR Pilot.exe)
    python  sdrpilot_app.py service    keep SDR Pilot running: start it, restart it if it stops (used at logon)
    python  sdrpilot_app.py install    Start-menu + desktop shortcuts, and start SDR Pilot when you log in
    python  sdrpilot_app.py uninstall  remove the shortcuts and the logon start
    python  sdrpilot_app.py stop       stop SDR Pilot
    python  sdrpilot_app.py start      start SDR Pilot in the background if it isn't running (no window)
    python  sdrpilot_app.py status

SDR Pilot "host" = the dashboard, the dashboard agent (chat with Claude via an API key, OpenAI/Gemini/OpenRouter, or
a local LM Studio / Ollama model) and the MCP server over HTTP for other AI apps. SDRangel can be started
automatically too (sdrangel_autostart in sdrpilot_config.json)."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
TASK = "SDR Pilot"


def _cfg():
    from sdrpilot import config
    return config.load()


def _pythonw() -> str:
    exe = sys.executable
    if os.name == "nt":
        w = os.path.join(os.path.dirname(exe), "pythonw.exe")
        if os.path.exists(w):
            return w
    return exe


def dashboard_url() -> str:
    c = _cfg()
    return f"http://127.0.0.1:{c.dashboard_port}/"


def host_alive(timeout: float = 2.0) -> dict | None:
    """The running host's /api/sessions answer (local requests need no key), or None."""
    c = _cfg()
    for port in range(c.dashboard_port, c.dashboard_port + 10):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/sessions", timeout=timeout) as r:
                d = json.loads(r.read())
            if d.get("kind") == "host":
                d["port"] = port
                return d
            for s in d.get("sessions", []):
                if s.get("kind") == "host" and s.get("alive", True) and s.get("dashboard_url"):
                    return {"self": s.get("pid"), "port": int(s["dashboard_url"].rsplit(":", 1)[1].strip("/")),
                            "kind": "host"}
        except Exception:
            continue
    return None


def start_host() -> int:
    log_dir = os.path.join(_cfg().workspace, "logs")
    os.makedirs(log_dir, exist_ok=True)
    logf = open(os.path.join(log_dir, "host_console.log"), "ab")
    kw: dict = {"stdin": subprocess.DEVNULL, "stdout": logf, "stderr": logf, "cwd": HERE, "close_fds": True}
    if os.name == "nt":
        kw["creationflags"] = 0x08000000 | 0x00000200 | 0x00000008  # no window, own group, detached
    else:
        kw["start_new_session"] = True
    p = subprocess.Popen([_pythonw(), os.path.join(HERE, "run_server.py"), "--host"], **kw)
    return p.pid


def wait_for_host(seconds: float = 40.0) -> dict | None:
    end = time.time() + seconds
    while time.time() < end:
        h = host_alive(1.0)
        if h:
            return h
        time.sleep(0.5)
    return None


APP_EXE = os.path.join(HERE, "SDR Pilot.exe")   # the native desktop window (WebView2); see launcher/


def open_window(url: str) -> None:
    """The dashboard in the SDR Pilot desktop window, else an Edge / Chrome --app window, else the default browser."""
    if os.name == "nt" and os.path.exists(APP_EXE):
        try:
            subprocess.Popen([APP_EXE, url], close_fds=True, creationflags=0x00000008)
            return
        except OSError:
            pass
    if os.name == "nt":
        for exe in (r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe",
                    r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe",
                    r"%ProgramFiles%\Google\Chrome\Application\chrome.exe",
                    r"%LocalAppData%\Google\Chrome\Application\chrome.exe"):
            p = os.path.expandvars(exe)
            if os.path.exists(p):
                subprocess.Popen([p, f"--app={url}", "--window-size=1500,950"], close_fds=True,
                                 creationflags=0x00000008)
                return
    import webbrowser
    webbrowser.open(url)


def cmd_open() -> None:
    h = host_alive()
    if not h:
        start_host()
        h = wait_for_host()
    if not h:
        _msg("SDR Pilot didn't start - see workspace/logs/host_console.log")
        return
    open_window(f"http://127.0.0.1:{h['port']}/")


def cmd_url() -> None:
    """For SDR Pilot.exe: make sure the host runs, then print its dashboard URL (exit 1 if it won't start)."""
    h = host_alive()
    if not h:
        start_host()
        h = wait_for_host()
    if not h:
        print("SDR Pilot didn't start - see workspace/logs/host_console.log", file=sys.stderr)
        sys.exit(1)
    print(f"http://127.0.0.1:{h['port']}/")


def cmd_service() -> None:
    """Supervisor: keep the host running (restart within ~15 s if it stops or crashes). Leaves a host that was
    stopped on purpose from the dashboard stopped for 10 minutes."""
    stop_flag = os.path.join(_cfg().workspace, "state", "host_stopped_by_user")
    while True:
        if not host_alive(3.0):
            try:
                recent = time.time() - os.path.getmtime(stop_flag) < 600
            except OSError:
                recent = False
            try:
                restarting = time.time() - os.path.getmtime(_restart_flag()) < 90
            except OSError:
                restarting = False
            if not recent and not restarting:
                start_host()
                wait_for_host(60)
        time.sleep(15)


def _restart_flag() -> str:
    return os.path.join(_cfg().workspace, "state", "host_restarting")


def cmd_relaunch(old_pid: int, argv: list[str]) -> None:
    """Used by the dashboard's Restart button: wait for the old host to exit, then start it again the same way."""
    from sdrpilot import codecheck
    flag = _restart_flag()
    try:
        with open(flag, "w") as f:
            f.write(str(time.time()))
    except OSError:
        pass
    try:
        codecheck.wait_pid_exit(old_pid, 30)
        time.sleep(1.0)
        if not host_alive(2.0):
            log_dir = os.path.join(_cfg().workspace, "logs")
            logf = open(os.path.join(log_dir, "host_console.log"), "ab")
            kw: dict = {"stdin": subprocess.DEVNULL, "stdout": logf, "stderr": logf, "cwd": HERE, "close_fds": True}
            if os.name == "nt":
                kw["creationflags"] = 0x08000000 | 0x00000200 | 0x00000008
            else:
                kw["start_new_session"] = True
            subprocess.Popen([sys.executable, *argv], **kw)
            wait_for_host(60)
    finally:
        try:
            os.remove(flag)
        except OSError:
            pass


def cmd_start() -> None:
    """Start the host in the background (no window) if it isn't running - used by the installer after an update.
    Clears the 'stopped on purpose' mark the update's stop left, so the logon service keeps it running again."""
    try:
        os.remove(os.path.join(_cfg().workspace, "state", "host_stopped_by_user"))
    except OSError:
        pass
    if host_alive(1.5):
        print("already running")
        return
    start_host()
    h = wait_for_host(60)
    print(f"running: http://127.0.0.1:{h['port']}/" if h else "didn't start - see workspace/logs/host_console.log")


def cmd_stop() -> None:
    h = host_alive()
    if not h:
        print("SDR Pilot isn't running")
        return
    key = open(os.path.join(_cfg().workspace, "state", "access_key.txt")).read().strip()
    req = urllib.request.Request(f"http://127.0.0.1:{h['port']}/api/host/stop", data=b'{"user": true}', method="POST",
                                 headers={"Content-Type": "application/json", "X-SDRPilot-Key": key})
    urllib.request.urlopen(req, timeout=5).read()
    print("stopped")


def _ps(script: str) -> int:
    return subprocess.call(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script])


def cmd_install(logon: bool = True) -> None:
    if os.name != "nt":
        print("Shortcuts and logon start are Windows-only; on Linux/macOS run 'python sdrpilot_app.py service' from "
              "your session's autostart instead.")
        return
    target = _pythonw()
    script = os.path.join(HERE, "sdrpilot_app.py")
    ico = os.path.join(HERE, "sdrpilot", "sdrpilot.ico")
    if os.path.exists(APP_EXE):   # the desktop app: its own taskbar button, pins as SDR Pilot
        lnk_target, lnk_args, ico = APP_EXE, "", APP_EXE
        try:
            with open(os.path.join(HERE, "launcher.json"), "w", encoding="utf-8") as f:
                json.dump({"python": sys.executable.replace("pythonw.exe", "python.exe")}, f)
        except OSError:
            pass
    else:
        lnk_target, lnk_args = target, f'"{script}"'
    for folder in ("[Environment]::GetFolderPath('Desktop')", "[Environment]::GetFolderPath('Programs')"):
        _ps(f"$s=(New-Object -ComObject WScript.Shell).CreateShortcut((Join-Path {folder} 'SDR Pilot.lnk'));"
            f"$s.TargetPath='{lnk_target}';$s.Arguments='{lnk_args}';$s.WorkingDirectory='{HERE}';"
            + (f"$s.IconLocation='{ico}';" if os.path.exists(ico) else "")
            + "$s.Description='SDR Pilot - AI-operated SDR dashboard';$s.Save()")
    if logon:
        tr = f'"{target}" "{script}" service'
        subprocess.call(["schtasks", "/Create", "/F", "/SC", "ONLOGON", "/TN", TASK, "/TR", tr, "/RL", "LIMITED"])
    print("Installed: 'SDR Pilot' on the desktop and in the Start menu" + (", and it starts when you log in" if logon
                                                                         else ""))


def cmd_uninstall() -> None:
    if os.name != "nt":
        return
    for folder in ("[Environment]::GetFolderPath('Desktop')", "[Environment]::GetFolderPath('Programs')"):
        _ps(f"Remove-Item -ErrorAction SilentlyContinue (Join-Path {folder} 'SDR Pilot.lnk')")
    subprocess.call(["schtasks", "/Delete", "/F", "/TN", TASK])
    print("Removed the shortcuts and the logon start (SDR Pilot itself is untouched)")


def _msg(text: str) -> None:
    if os.name == "nt" and os.path.basename(sys.executable).lower() == "pythonw.exe":
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(0, text, "SDR Pilot", 0x40)
            return
        except Exception:
            pass
    print(text)


def main() -> None:
    a = sys.argv[1] if len(sys.argv) > 1 else "open"
    if a == "open":
        cmd_open()
    elif a == "url":
        cmd_url()
    elif a == "service":
        cmd_service()
    elif a == "install":
        cmd_install("--no-logon" not in sys.argv)
    elif a == "uninstall":
        cmd_uninstall()
    elif a == "stop":
        cmd_stop()
    elif a == "start":
        cmd_start()
    elif a == "relaunch":
        i = sys.argv.index("--")
        cmd_relaunch(int(sys.argv[2]), sys.argv[i + 1:])
    elif a == "status":
        h = host_alive()
        print(f"running: http://127.0.0.1:{h['port']}/" if h else "not running")
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
