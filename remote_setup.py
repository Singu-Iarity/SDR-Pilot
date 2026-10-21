"""Remote access for SDR Pilot: open the dashboard (and the MCP endpoint) from your other devices over Tailscale.

    python remote_setup.py              show what's set up, and the links for your phone/tablet
    python remote_setup.py serve        put the dashboard on your tailnet over HTTPS (tailscale serve)   [recommended]
    python remote_setup.py serve-mcp    also put the MCP-over-HTTP endpoint on your tailnet (port 8443)
    python remote_setup.py funnel-mcp   make the MCP endpoint reachable from the INTERNET (Tailscale Funnel), e.g.
                                        for a claude.ai custom connector. Anyone with the link can use your radio.
    python remote_setup.py direct       instead of `serve`: let the dashboard listen on the Tailscale IP itself
    python remote_setup.py local        undo `direct`
    python remote_setup.py off          stop all `tailscale serve` / funnel entries
    python remote_setup.py autostart on|off   start the SDR Pilot host (dashboard + agent + MCP over HTTP) at login
    python remote_setup.py new-key      make a new access key (old phone links stop working)
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from sdrpilot import config as config_mod  # noqa: E402
from sdrpilot.access import Access, run_tailscale  # noqa: E402

CFG_PATH = os.path.join(HERE, "sdrpilot_config.json")
STARTUP_NAME = "SDR Pilot host.vbs"


def set_config(**kw) -> None:
    try:
        with open(CFG_PATH, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        data = {}
    data.update(kw)
    with open(CFG_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    print(f"  saved {', '.join(f'{k}={v!r}' for k, v in kw.items())} in sdrpilot_config.json")


def ts(*args) -> bool:
    print(f"  > tailscale {' '.join(args)}")
    rc, out = run_tailscale(*args, timeout=60)
    for line in (out or "").splitlines():
        print("    " + line)
    if rc != 0:
        print(f"  (exit code {rc})")
        if "HTTPS" in out or "https" in out.lower() and "enable" in out.lower():
            print("  -> Turn on MagicDNS and HTTPS certificates for your tailnet at "
                  "https://login.tailscale.com/admin/dns, then run this again.")
    return rc == 0


def status(cfg, acc: Access) -> None:
    t = acc.tailscale(refresh=True)
    print("\nTailscale:", "not installed" if not t["installed"] else
          ("connected as " + (t["dns_name"] or t["ip4"] or "?")) if t["running"] else "installed, not connected")
    if t.get("error"):
        print("  " + t["error"])
    info = acc.dashboard_urls(cfg.dashboard_port, cfg.dashboard_bind)
    print(f"\nDashboard (dashboard_bind = {cfg.dashboard_bind}):")
    for k, u in info["urls"].items():
        print(f"  {k:18s} {u if k == 'this_pc' else acc.with_key(u)}")
    for n in info["notes"]:
        print("  " + n)
    serve = {int(k): v for k, v in (t.get("serve") or {}).items()}
    if cfg.mcp_http_port in serve:
        print(f"\nMCP over HTTP on your tailnet:\n  {serve[cfg.mcp_http_port].rstrip('/')}/k/{acc.key}/mcp")
    if t.get("funnel"):
        print(f"\n!! Funnel (public internet) is ON for: {', '.join(t['funnel'])}")
    print("\nThe links above contain your access key: open them on your own devices, don't share them.")


def autostart(on: bool) -> None:
    if os.name != "nt":
        print("On Linux/macOS add this to your login items / a systemd user service:\n"
              f"  {sys.executable} {os.path.join(HERE, 'run_server.py')} --host")
        return
    folder = os.path.join(os.environ.get("APPDATA", ""), r"Microsoft\Windows\Start Menu\Programs\Startup")
    path = os.path.join(folder, STARTUP_NAME)
    if not on:
        if os.path.exists(path):
            os.remove(path)
        print("  autostart removed")
        return
    py = sys.executable
    pyw = os.path.join(os.path.dirname(py), "pythonw.exe")
    exe = pyw if os.path.exists(pyw) else py
    script = os.path.join(HERE, "run_server.py")
    with open(path, "w", encoding="utf-8") as f:
        f.write('Set sh = CreateObject("WScript.Shell")\n'
                f'sh.CurrentDirectory = "{HERE}"\n'
                f'sh.Run """{exe}"" ""{script}"" --host", 0, False\n')
    print(f"  the SDR Pilot host now starts hidden when you log in ({path})")
    print("  start it now with start_host.bat (or log out and in)")


def main() -> int:
    cmd = (sys.argv[1] if len(sys.argv) > 1 else "status").lower()
    cfg = config_mod.load()
    if cmd == "new-key":
        p = os.path.join(cfg.workspace, "state", "access_key.txt")
        if os.path.exists(p):
            os.remove(p)
        acc = Access(cfg)
        print(f"  new access key: {acc.key}\n  Restart Claude / the SDR Pilot host so every copy uses it.")
        return 0
    acc = Access(cfg)
    if cmd == "serve":
        if ts("serve", "--bg", str(cfg.dashboard_port)):
            print("  The dashboard is now on your tailnet over HTTPS (only your own devices can reach it).")
    elif cmd == "serve-mcp":
        ts("serve", "--bg", "--https=8443", str(cfg.mcp_http_port))
        print("  Needs the SDR Pilot host running (start_host.bat), which serves MCP on port "
              f"{cfg.mcp_http_port}.")
    elif cmd == "funnel-mcp":
        print("WARNING: this makes SDR Pilot's MCP endpoint reachable from the whole internet. Anyone who gets the\n"
              "link (it contains your access key) can control your radio and run Python code on this PC.")
        if input("Type YES to continue: ").strip() != "YES":
            print("  cancelled")
            return 1
        ts("funnel", "--bg", "--https=8443", str(cfg.mcp_http_port))
    elif cmd == "direct":
        set_config(dashboard_bind="tailscale")
        print("  Restart Claude / the host. Windows Firewall may ask about python.exe; if your phone still can't\n"
              "  connect, allow the ports from Tailscale addresses (run in an admin terminal):\n"
              f'  netsh advfirewall firewall add rule name="SDR Pilot (Tailscale)" dir=in action=allow protocol=TCP '
              f"localport={cfg.dashboard_port}-{cfg.dashboard_port + 9},{cfg.mcp_http_port} remoteip=100.64.0.0/10")
    elif cmd == "local":
        set_config(dashboard_bind="local", mcp_http_bind="local")
    elif cmd == "off":
        ts("serve", "--https=443", "off")
        ts("serve", "--https=8443", "off")
        ts("funnel", "--https=8443", "off")
    elif cmd == "autostart":
        autostart((sys.argv[2] if len(sys.argv) > 2 else "on").lower() != "off")
    elif cmd != "status":
        print(__doc__)
        return 1
    status(cfg, acc)
    return 0


if __name__ == "__main__":
    sys.exit(main())
