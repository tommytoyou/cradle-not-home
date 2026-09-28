#!/usr/bin/env bash
set -euo pipefail
SRC="${1:?source file}"
IN="${2:?start}"
OUTP="${3:?end}"
FOLDER="${4:?folder}"
STEM="${5:?stem}"
ROOT="$(cd "$(dirname "$0")" && pwd)"
DEST="$ROOT/library/$FOLDER/${STEM}.mp4"
mkdir -p "$ROOT/library/$FOLDER"
ffmpeg -y -ss "$IN" -to "$OUTP" -i "$SRC" -c:v libx264 -preset medium -crf 16 -pix_fmt yuv420p -an -movflags +faststart "$DEST"
echo "Wrote $DEST"
