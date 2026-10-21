#!/usr/bin/env bash
# SDR Pilot setup for Linux / macOS. Needs a Python that has GNU Radio (radioconda, conda-forge gnuradio,
# or your distro's gnuradio package). Override with: SDRPILOT_PYTHON=/path/to/python ./setup_linux_mac.sh
cd "$(dirname "$0")"
PY="${SDRPILOT_PYTHON:-}"
for c in "$PY" "$HOME/radioconda/bin/python" "$HOME/miniforge3/bin/python" "$(command -v python3)"; do
  if [ -n "$c" ] && [ -x "$c" ] && "$c" -c "from gnuradio import gr" 2>/dev/null; then PY="$c"; break; fi
done
if [ -z "$PY" ] || ! "$PY" -c "from gnuradio import gr" 2>/dev/null; then
  echo "No Python with GNU Radio found. Install radioconda (https://github.com/ryanvolz/radioconda) or your"
  echo "distro's gnuradio package, or set SDRPILOT_PYTHON=/path/to/python."
  exit 1
fi
exec "$PY" setup_sdrpilot.py
