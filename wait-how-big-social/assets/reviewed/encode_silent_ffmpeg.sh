#!/usr/bin/env bash
# BOTS-120 / S04-05: Windows/cross-platform silent H.264 encode (ffmpeg).
# Deterministic args for 1080x1920 @ 30fps, 12s silent WHB packets.
# macOS path remains encode_silent.swift; this is the documented Windows replacement.
set -euo pipefail

if [[ $# -lt 2 ]]; then
  echo "Usage: $0 OUTPUT_DIR CONTENT_ID" >&2
  exit 1
fi

DIR="$1"
CONTENT_ID="$2"
MANIFEST="$DIR/SCENE_MANIFEST.json"
OUT="$DIR/${CONTENT_ID}-silent-video.mp4"
LIST="$DIR/.ffmpeg_concat_$$.txt"

if [[ ! -f "$MANIFEST" ]]; then
  echo "missing SCENE_MANIFEST.json in $DIR" >&2
  exit 1
fi
if [[ -e "$OUT" ]]; then
  echo "refusing to overwrite $OUT" >&2
  exit 2
fi
if ! command -v ffmpeg >/dev/null 2>&1; then
  echo "ffmpeg not found on PATH (install for Windows/Linux encode path)" >&2
  exit 3
fi
if ! command -v python3 >/dev/null 2>&1; then
  echo "python3 required to expand SCENE_MANIFEST frame_counts" >&2
  exit 3
fi

# Expand each scene PNG by frame_counts into a concat demuxer list (copy frames).
python3 - "$MANIFEST" "$DIR" "$LIST" <<'PY'
import json, sys
from pathlib import Path
manifest_path, directory, list_path = sys.argv[1:4]
manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
counts = manifest["frame_counts"]
fps = int(manifest.get("fps", 30))
if fps != 30:
    raise SystemExit(f"expected fps=30, got {fps}")
root = Path(directory)
lines = []
for index, count in enumerate(counts):
    png = root / "scenes" / f"scene-{index}.png"
    if not png.exists():
        raise SystemExit(f"missing {png}")
    # duration per frame at 30fps
    dur = 1.0 / 30.0
    for _ in range(int(count)):
        lines.append(f"file '{png.resolve()}'")
        lines.append(f"duration {dur:.10f}")
# concat demuxer needs a final file line without duration
if counts:
    last = root / "scenes" / f"scene-{len(counts)-1}.png"
    lines.append(f"file '{last.resolve()}'")
Path(list_path).write_text("\n".join(lines) + "\n", encoding="utf-8")
PY

# Deterministic silent encode: H.264 High, ~2.5Mbps, yuv420p, no audio.
ffmpeg -y -hide_banner -loglevel error \
  -f concat -safe 0 -i "$LIST" \
  -vf "scale=1080:1920:flags=lanczos,fps=30,format=yuv420p" \
  -c:v libx264 -profile:v high -level 4.1 \
  -b:v 2500000 -maxrate 2500000 -bufsize 5000000 \
  -an -movflags +faststart \
  "$OUT"

rm -f "$LIST"
echo "wrote $OUT"
