"""SDR Pilot - let an AI drive SDRangel and GNU Radio through MCP."""
import os as _os

__version__ = "0.9.0"


def _read_build() -> str:
    """The build stamp shipped with these files (sdrpilot/BUILD, e.g. 0.9.0.20261021.1315): the installer, the
    running app and the dashboard all compare this, so an update by the installer, by copying files in, or through
    the dashboard's restart button is recognised the same way everywhere."""
    try:
        with open(_os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "BUILD"), encoding="utf-8") as f:
            b = f.read().strip()
        return b or __version__
    except OSError:
        return __version__


__build__ = _read_build()


def build_label(build: str | None = None) -> str:
    """0.9.0.20261021.1315 -> '0.9.0 (build 2026-10-21 13:15)'."""
    b = build or __build__
    parts = b.split(".")
    if len(parts) >= 5 and len(parts[3]) == 8 and parts[3].isdigit():
        d, t = parts[3], parts[4].zfill(4)
        return f"{'.'.join(parts[:3])} (build {d[:4]}-{d[4:6]}-{d[6:]} {t[:2]}:{t[2:]})"
    return b
