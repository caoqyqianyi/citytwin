#!/usr/bin/env bash
# Render the documentation figures from the built viewer.
# Requires a local Chrome/Chromium; the viewer itself needs neither.
set -euo pipefail

cd "$(dirname "$0")/.."
OUT=docs/assets
mkdir -p "$OUT"

CHROME="${CHROME:-}"
if [ -z "$CHROME" ]; then
  for candidate in \
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
    "$(command -v google-chrome || true)" \
    "$(command -v chromium || true)"; do
    if [ -n "$candidate" ] && { [ -x "$candidate" ] || command -v "$candidate" >/dev/null 2>&1; }; then
      CHROME="$candidate"
      break
    fi
  done
fi

if [ -z "$CHROME" ]; then
  echo "No Chrome/Chromium found; set CHROME=/path/to/chrome" >&2
  exit 1
fi

WIDTH=${WIDTH:-1500}
HEIGHT=${HEIGHT:-940}
SCALE=${SCALE:-2}

shoot() {
  local mode="$1" target="$2" url="file://$PWD/web/index.html"
  if [ -n "$mode" ]; then url="$url?mode=$mode"; fi
  "$CHROME" --headless=new --disable-gpu --hide-scrollbars \
    --force-device-scale-factor="$SCALE" --window-size="$WIDTH,$HEIGHT" \
    --virtual-time-budget=8000 \
    --screenshot="$target" "$url" >/dev/null 2>&1
  echo "rendered $target"
}

shoot "" "$OUT/plan_view.png"
shoot typology "$OUT/typology_view.png"

# keep README images light enough for GitHub
if command -v sips >/dev/null 2>&1; then
  for f in "$OUT/plan_view.png" "$OUT/typology_view.png"; do
    sips --resampleWidth 1600 "$f" >/dev/null
  done
fi

ls -lh "$OUT"
