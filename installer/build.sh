#!/bin/sh
# Build the Windows installer. Stamps sdrpilot/BUILD with the current time first (unless --keep-build), so the
# installer, the app and the dashboard can all tell this build from earlier ones.
set -e
cd "$(dirname "$0")"
VER=$(sed -n 's/^__version__ = "\(.*\)"/\1/p' ../sdrpilot/__init__.py)
if [ "$1" != "--keep-build" ]; then
  printf "%s" "$VER.$(TZ=America/Denver date +%Y%m%d.%H%M)" > ../sdrpilot/BUILD
fi
makensis -V2 -DVERSION="$VER" sdrpilot_setup.nsi
echo "built SDR-Pilot-Setup-$VER.exe, build $(cat ../sdrpilot/BUILD)"
