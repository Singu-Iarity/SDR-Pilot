"""Configuration: sdrpilot_config.json next to the package (all keys optional)."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, asdict, field

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@dataclass
class Config:
    # "sdrangel" = a running SDRangel instance (REST API + Remote TCP Sink channel)
    # "sim"      = built-in simulated SDRangel with test signals (no hardware needed)
    source: str = "sdrangel"
    sdrangel_host: str = "127.0.0.1"
    sdrangel_api_port: int = 8091
    sdrangel_device_set: int = 0
    # Port the Remote TCP Sink channel listens on (SDRangel's default is 1234)
    sdrangel_iq_port: int = 1234
    # IQ stream format from SDRangel's Remote TCP Sink (the AI can change it with radio_stream_format):
    # "rtl0" = rtl_tcp protocol, always 8-bit (48 dB range; plenty for an 8-bit SDR like the HackRF at full rate)
    # "sdra" = SDRangel protocol: 8/16/24/32-bit samples (16+ keeps weak signals next to strong ones, and the extra
    #          resolution SDRangel's decimation and 12-16-bit SDRs give), sent raw ("IQ only") or zlib-compressed
    #          (only worth it when SDRangel runs on another machine). "SDRangel wss" (WebSocket+TLS) is for the
    #          browser version of SDRangel - slower here, so it isn't offered.
    sdrangel_protocol: str = "rtl0"
    sdrangel_bits: int = 0               # 0 = 8 for rtl0, 16 for sdra
    sdrangel_compression: str = "none"   # "none" (IQ only) or "zlib" (sdra only)
    # web_search: optional SearXNG instance (e.g. "http://127.0.0.1:8888") or Brave Search API key; otherwise
    # DuckDuckGo / Bing pages are used
    web_search_url: str = ""
    brave_api_key: str = ""
    # Start SDRangel when it isn't running and an AI needs the radio (standalone use). sdrangel_exe: path to
    # sdrangel.exe (GUI) or sdrangelsrv.exe (headless server); empty = look in the usual install folders
    # where the antenna is (for bearing / range / elevation of decoded positions, e.g. ADS-B). None = use SDRangel's
    # own "My Position" (Preferences) if it's set
    station_lat: float | None = None
    station_lon: float | None = None
    station_alt_m: float = 0.0
    sdrangel_autostart: bool = True
    sdrangel_exe: str = ""
    # Let SDR Pilot add/configure the Remote TCP Sink channel automatically
    auto_configure_channel: bool = True
    # Simulator settings
    sim_api_port: int = 18091
    sim_iq_port: int = 11234
    sim_center_freq: float = 2402e6
    sim_sample_rate: float = 4e6
    # Local ports
    zmq_port: int = 5600
    dashboard_port: int = 8765
    open_dashboard_on_first_use: bool = True
    # Who can reach the dashboard: "local" = this PC only (use `tailscale serve` to reach it from your other
    # devices), "tailscale" = also directly over Tailscale (100.x addresses), "all" = also your home LAN.
    dashboard_bind: str = "local"
    # Remote viewers (anything but this PC's own browser) need the access key once; it is then remembered.
    remote_require_key: bool = True
    # Access key for the dashboard and the MCP-over-HTTP endpoint; empty = generated in workspace/state/access_key.txt
    access_token: str = ""
    # Extra host names the dashboard may be reached by (localhost, IPs, *.ts.net and single-word names always work)
    allowed_hosts: list = field(default_factory=list)
    # SDR Pilot host (run_server.py --host): MCP over HTTP for other devices / Claude Code, plus the dashboard agent
    mcp_http_port: int = 8770
    mcp_http_bind: str = "local"
    # Dashboard agent (the dashboard's chat box). Model-agnostic: "anthropic" (Claude) or "openai" = any
    # OpenAI-compatible API (OpenAI, Gemini, OpenRouter, LM Studio, Ollama, vLLM...). Usually set from the dashboard.
    agent_provider: str = ""
    agent_base_url: str = ""
    agent_api_key: str = ""
    agent_model: str = ""
    agent_max_steps: int = 40
    # Tool results longer than this many characters are shortened for the AI and stored in full
    # (get_full_result). 0 = never shorten. Can be switched on the dashboard (Settings -> AI context).
    result_max_chars: int = 12000
    # Where SDR Pilot keeps chats (agent conversation, sessions), settings, logs and state
    workspace: str = os.path.join(ROOT, "workspace")
    # Where captures, flowgraphs, job outputs, images and saved recipes go ("" = inside the workspace)
    data_dir: str = ""
    # Updates: GitHub repository whose Releases carry SDR-Pilot-Setup-<build>.exe ("owner/repo"; empty = no checks)
    update_repo: str = ""
    update_check_hours: float = 6.0
    ring_seconds: float = 2.0
    ring_max_samples: int = 24_000_000

    @property
    def data_root(self) -> str:
        return self.data_dir or self.workspace

    @property
    def zmq_address(self) -> str:
        return f"tcp://127.0.0.1:{self.zmq_port}"


def load() -> Config:
    cfg = Config()
    path = os.environ.get("SDRPILOT_CONFIG", os.path.join(ROOT, "sdrpilot_config.json"))
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        for k, v in data.items():
            if not k.startswith("_") and hasattr(cfg, k):
                setattr(cfg, k, v)
    # any setting can be overridden with an environment variable, e.g. SDRPILOT_SOURCE=sim
    for k, v in list(vars(cfg).items()):
        env = os.environ.get("SDRPILOT_" + k.upper())
        if env is None or env == "":
            continue
        if isinstance(v, list):
            setattr(cfg, k, [s.strip() for s in env.split(",") if s.strip()])
        elif isinstance(v, bool):
            setattr(cfg, k, env.lower() in ("1", "true", "yes", "on"))
        elif isinstance(v, int):
            setattr(cfg, k, int(float(env)))
        elif isinstance(v, float):
            setattr(cfg, k, float(env))
        else:
            setattr(cfg, k, env)
    if not os.path.isabs(cfg.workspace):
        cfg.workspace = os.path.join(ROOT, cfg.workspace)
    cfg.workspace = os.path.expanduser(cfg.workspace)
    if cfg.data_dir:
        cfg.data_dir = os.path.expanduser(cfg.data_dir)
        if not os.path.isabs(cfg.data_dir):
            cfg.data_dir = os.path.join(ROOT, cfg.data_dir)
    for sub in ("captures", "flowgraphs", "jobs", "images"):
        os.makedirs(os.path.join(cfg.data_root, sub), exist_ok=True)
    return cfg


def save_settings(**kw) -> str:
    """Write settings into sdrpilot_config.json (keeps everything else in it). Takes effect on restart."""
    path = os.environ.get("SDRPILOT_CONFIG", os.path.join(ROOT, "sdrpilot_config.json"))
    data = {}
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    data.update(kw)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)
    return path


def as_dict(cfg: Config) -> dict:
    return asdict(cfg)
