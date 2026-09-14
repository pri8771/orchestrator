#!/bin/bash
# BOTS-122: produce WHB-013..016 bundles (scenes, TTS, MP4, hashes)
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
REVIEWED="$ROOT"
VOICE="${WHB_TTS_VOICE:-Samantha}"
RATE="${WHB_TTS_RATE:-180}"

hash_file() {
  shasum -a 256 "$1" | awk '{print $1}'
}

produce_one() {
  local ID="$1"
  local SCRIPT="$2"
  local OUT="$REVIEWED/$ID"
  mkdir -p "$OUT"
  python3 "$REVIEWED/render_whb_s04.py" "$ID" "$OUT"
  printf '%s' "$SCRIPT" > "$OUT/narration.txt"
  say -v "$VOICE" -r "$RATE" -o "$OUT/narration.aiff" --file-format=AIFF "$SCRIPT"
  swift -suppress-warnings "$REVIEWED/encode_narrated.swift" "$OUT" "$ID" "$OUT/narration.aiff"
  local HASH
  HASH=$(hash_file "$OUT/${ID}-narrated-video.mp4")
  cp "$OUT/${ID}-narrated-video.mp4" "$REVIEWED/${ID}-narrated-${HASH}.mp4"
  echo "$HASH" > "$OUT/media.sha256"
  echo "produced $ID sha256=$HASH"
}

produce_one "WHB-013" "The average gap between Earth and the Moon is about three hundred eighty-four thousand kilometers. Earth's equatorial diameter is about twelve thousand seven hundred fifty-six kilometers. Divide them, and roughly thirty Earths fit in that gap. Textbook diagrams compress space. This is the math, not a scale picture."

produce_one "WHB-014" "The Sun is about one point four million kilometers wide. Earth is about twelve thousand seven hundred forty-two. Width alone understates the difference, because volume grows with the cube of diameter. About one point three million Earths would fill the Sun's volume. That is a volume comparison, not physical packing."

produce_one "WHB-015" "Challenger Deep reaches about ten thousand nine hundred thirty-five meters. Mount Everest's summit is about eight thousand eight hundred forty-nine meters above sea level. Drop Everest into the deepest ocean trench and its peak would still sit about two kilometers underwater."

produce_one "WHB-016" "One million hours is about one hundred fourteen years. One billion hours is about one hundred fourteen thousand years. Three extra zeros move you from a human lifetime into deep prehistory. A billion is one thousand times a million."

python3 "$REVIEWED/math_checks_s04.py" "$REVIEWED/MATH_CHECKS_S04.json"
