"""SDR Pilot setup (called by setup_windows.bat). Every step is shown on screen AND written to
logs/setup_<time>.log, so if anything fails you can send that one file."""
import os
import platform
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
os.makedirs(os.path.join(HERE, "logs"), exist_ok=True)
LOG_PATH = os.path.join(HERE, "logs", f"setup_{time.strftime('%Y%m%d_%H%M%S')}.log")
LOG = open(LOG_PATH, "w", encoding="utf-8", buffering=1)


def out(msg=""):
    print(msg, flush=True)
    LOG.write(msg + "\n")


def run(cmd, show=True) -> int:
    """Run a command, streaming its output to the screen (optional) and always to the log."""
    LOG.write(f"$ {' '.join(cmd)}\n")
    p = subprocess.Popen(cmd, cwd=HERE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                         stdin=subprocess.DEVNULL, text=True, encoding="utf-8", errors="replace")
    for line in p.stdout:
        LOG.write(line)
        if show:
            print(line, end="", flush=True)
    rc = p.wait()
    LOG.write(f"(exit code {rc})\n")
    return rc


def finish(code: int) -> int:
    out("")
    out(f"Setup log: {LOG_PATH}")
    if code:
        out("Setup did not finish. Send that log file (and the selftest log it mentions) to Claude.")
    LOG.close()
    return code


def main() -> int:
    out(f"SDR Pilot setup  -  {time.ctime()}")
    out(f"Python {sys.version.split()[0]} at {sys.executable}  ({platform.platform()})")
    out("")
    out("[1/4] Checking GNU Radio...")
    rc = run([sys.executable, "-c",
              "from gnuradio import gr, zeromq, blocks, analog, digital, filter; import numpy, yaml; "
              "print('   GNU Radio', gr.version(), '| numpy', numpy.__version__, '| prefix', gr.prefix())"])
    if rc:
        out("   GNU Radio did not import - is this radioconda's python?")
        return finish(1)
    out("[2/4] Installing / updating the MCP Python SDK...")
    pip = [sys.executable, "-m", "pip", "install", "--upgrade", "--disable-pip-version-check", "mcp>=1.2"]
    rc = run(pip, show=False)
    if rc and os.name != "nt":  # system Pythons (Debian/Ubuntu) refuse global installs without this
        rc = run(pip + ["--user", "--break-system-packages"], show=False)
    if rc:
        out("   pip install failed (details in the log)")
        return finish(1)
    run([sys.executable, "-c", "import importlib.metadata as m; print('   mcp', m.version('mcp'))"])
    out("[3/4] Running the self-test (simulator, no hardware needed)...")
    rc = run([sys.executable, os.path.join(HERE, "selftest.py")])
    if rc:
        out("   Self-test failed - see the messages above.")
        return finish(1)
    out("[4/4] Registering SDR Pilot with the Claude desktop app...")
    rc = run([sys.executable, os.path.join(HERE, "install_claude_config.py")])
    if rc == 2:
        out("   Quit Claude completely, then double-click register_with_claude.bat to finish this step.")
        return finish(0)
    if rc:
        return finish(1)
    out("")
    out("Done. Quit Claude completely (tray icon > Quit) and reopen it, then ask it to use SDR Pilot.")
    return finish(0)


if __name__ == "__main__":
    sys.exit(main())
