"""Registers SDR Pilot as a local MCP server in the Claude desktop app (Windows / macOS / Linux).

Run with the Python that has GNU Radio (radioconda):  radioconda\\python.exe install_claude_config.py
Quit Claude first: the app can rewrite its config file when it exits, which would drop the new entry.
Backs up each existing config, adds/updates the "sdr-pilot" entry, then reads it back to verify.
"""
import glob
import json
import os
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
NAME = "sdr-pilot"


def candidates() -> list[str]:
    out = []
    if os.name == "nt":
        roaming = os.environ.get("APPDATA") or os.path.join(os.path.expanduser("~"), "AppData", "Roaming")
        local = os.environ.get("LOCALAPPDATA") or os.path.join(os.path.expanduser("~"), "AppData", "Local")
        out.append(os.path.join(roaming, "Claude"))
        out.append(os.path.join(os.path.expanduser("~"), "AppData", "Roaming", "Claude"))
        # Microsoft Store (MSIX) installs may read a virtualised copy of AppData instead
        out += glob.glob(os.path.join(local, "Packages", "*Claude*", "LocalCache", "Roaming", "Claude"))
    elif sys.platform == "darwin":
        out.append(os.path.expanduser("~/Library/Application Support/Claude"))
    else:
        out.append(os.path.expanduser("~/.config/Claude"))
    seen, uniq = set(), []
    for d in out:
        k = os.path.normcase(os.path.abspath(d))
        if k not in seen:
            seen.add(k)
            uniq.append(d)
    return uniq


def claude_running() -> bool:
    if os.name != "nt":
        return False
    try:
        r = subprocess.run(["tasklist", "/FI", "IMAGENAME eq claude.exe", "/NH"], capture_output=True, text=True)
        return "claude.exe" in r.stdout.lower()
    except Exception:
        return False


def main() -> int:
    if claude_running() and "--force" not in sys.argv:
        print("Claude is still running. Quit it completely first (right-click its tray icon > Quit),")
        print("then run this again. (Claude can overwrite its config file when it exits.)")
        return 2
    entry = {"command": sys.executable, "args": [os.path.join(HERE, "run_server.py")], "env": {}}
    cands = candidates()
    print("Config locations checked:")
    for d in cands:
        print(f"  {'exists ' if os.path.isdir(d) else 'missing'}  {d}")
    targets = [d for d in cands if os.path.isdir(d)]
    if os.name == "nt" and cands[0] not in targets:
        targets.insert(0, cands[0])  # always register in the standard %APPDATA%\Claude location too
    if not targets:
        targets = cands[:1]
    ok = 0
    for d in targets:
        try:
            os.makedirs(d, exist_ok=True)
            path = os.path.join(d, "claude_desktop_config.json")
            cfg = {}
            if os.path.exists(path):
                with open(path, encoding="utf-8") as f:
                    txt = f.read().strip()
                cfg = json.loads(txt) if txt else {}
                bak = path + f".bak-{time.strftime('%Y%m%d-%H%M%S')}"
                shutil.copy2(path, bak)
            if not isinstance(cfg.get("mcpServers"), dict):
                cfg["mcpServers"] = {}
            cfg["mcpServers"][NAME] = entry
            with open(path, "w", encoding="utf-8") as f:
                json.dump(cfg, f, indent=2)
            with open(path, encoding="utf-8") as f:
                check = json.load(f)
            assert check["mcpServers"][NAME]["args"][0] == entry["args"][0]
            others = [k for k in check["mcpServers"] if k != NAME]
            print(f"registered '{NAME}' in {path}" + (f"  (other servers kept: {', '.join(others)})" if others else ""))
            ok += 1
        except Exception as e:
            print(f"could not update {d}: {e}")
    print("\nServer command:", entry["command"], entry["args"][0])
    if not ok:
        print("Nothing was registered. In Claude: Settings > Developer > Edit Config, and add this under \"mcpServers\":")
        print(json.dumps({NAME: entry}, indent=2))
        return 1
    print("Now start Claude again. SDR Pilot should appear in its tools/connectors list.")
    print("\nFor Claude Code instead, run:")
    print(f'  claude mcp add {NAME} -- "{entry["command"]}" "{entry["args"][0]}"')
    return 0


if __name__ == "__main__":
    sys.exit(main())
