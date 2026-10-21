#!/bin/sh
# Publish the current build as a GitHub Release, which installed copies of SDR Pilot pick up as an update.
#   ./publish.sh owner/repo "what changed"          (needs the GitHub CLI logged in: gh auth login)
# Uploads SDR-Pilot-Setup-<build>.exe plus its .sha256; the dashboard refuses installers without a checksum.
set -e
cd "$(dirname "$0")"
REPO="$1"; NOTES="${2:-SDR Pilot update}"
[ -n "$REPO" ] || { echo "usage: ./publish.sh owner/repo \"release notes\""; exit 1; }
BUILD=$(cat ../sdrpilot/BUILD)
VER=$(sed -n 's/^__version__ = "\(.*\)"/\1/p' ../sdrpilot/__init__.py)
SRC="SDR-Pilot-Setup-$VER.exe"
[ -f "$SRC" ] || { echo "build the installer first (./build.sh)"; exit 1; }
OUT="dist/SDR-Pilot-Setup-$BUILD.exe"
mkdir -p dist && cp "$SRC" "$OUT"
( cd dist && sha256sum "SDR-Pilot-Setup-$BUILD.exe" > "SDR-Pilot-Setup-$BUILD.exe.sha256" )
gh release create "v$BUILD" "$OUT" "$OUT.sha256" --repo "$REPO" --title "SDR Pilot $BUILD" --notes "$NOTES"
echo "published v$BUILD to $REPO"
