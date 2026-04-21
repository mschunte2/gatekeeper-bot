#!/bin/bash
# Package a webxdc app as a .xdc file (plain zip — no bundler).
#
# Usage: ./build-xdc.sh <path-to-app-source-dir>
#
# The app source directory must contain:
#   - index.html, main.js, main.css  (at root)
#   - public/*                        (copied to .xdc root alongside)
#
# The resulting .xdc is written to `<srcdir>/../<basename>.xdc`, so
#   ./build-xdc.sh apps/gatekeeper        -> apps/gatekeeper.xdc
#   ./build-xdc.sh apps-disabled/quick-lock -> apps-disabled/quick-lock.xdc
#
# Replaces the earlier vite-based build: the apps are vanilla JS with
# no imports, so bundling adds nothing. A .xdc is just a zip.

set -euo pipefail

if [[ $# -ne 1 ]]; then
    echo "Usage: $0 <path-to-app-source-dir>" >&2
    exit 1
fi

SRC="${1%/}"
if [[ ! -d "$SRC" ]]; then
    echo "Not a directory: $SRC" >&2
    exit 1
fi

NAME=$(basename "$SRC")
OUT="$(cd "$SRC/.." && pwd)/$NAME.xdc"

for f in index.html main.js main.css public/manifest.toml; do
    if [[ ! -f "$SRC/$f" ]]; then
        echo "Missing required file: $SRC/$f" >&2
        exit 1
    fi
done

STAGE=$(mktemp -d)
trap 'rm -rf "$STAGE"' EXIT

cp "$SRC/index.html" "$SRC/main.js" "$SRC/main.css" "$STAGE/"
cp -r "$SRC/public/." "$STAGE/"

rm -f "$OUT"
(cd "$STAGE" && zip -qrX "$OUT" .)

echo "built $OUT ($(stat -c%s "$OUT") bytes, $(unzip -l "$OUT" | tail -1 | awk '{print $2}') files)"
