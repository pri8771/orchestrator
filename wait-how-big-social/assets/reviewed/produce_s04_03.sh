#!/bin/bash
# BOTS-120 / S04-03: produce WHB-017..022 silent bundles (no TTS).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"
python3 "$ROOT/whb_s04_03_packets.py"
python3 <<'PY'
import json, shutil, subprocess, sys
from pathlib import Path

ROOT = Path.cwd()
sys.path.insert(0, str(ROOT))
from whb_s04_03_packets import PACKETS, write_packet_metadata, sha256_file

assert (ROOT / "encode_silent.swift").exists()

index = []
for cid, pkt in PACKETS.items():
    out = ROOT / cid
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    subprocess.check_call(["python3", str(ROOT / "render_whb_s04_03.py"), cid, str(out)])
    silent = out / f"{cid}-silent-video.mp4"
    if silent.exists():
        silent.unlink()
    subprocess.check_call(
        ["swift", "-suppress-warnings", str(ROOT / "encode_silent.swift"), str(out), cid]
    )
    digest = sha256_file(silent)
    hashed_name = f"{cid}-{pkt['slug']}-{digest}.mp4"
    dest = ROOT / hashed_name
    shutil.copy2(silent, dest)
    write_packet_metadata(out, cid, digest, silent.stat().st_size)
    index.append(
        {
            "content_id": cid,
            "topic": pkt["topic"],
            "slug": pkt["slug"],
            "sha256": digest,
            "bytes": silent.stat().st_size,
            "silent_path": str(silent),
            "hashed_path": str(dest),
            "captions": pkt["captions"],
            "duration_seconds": 12,
            "variant": "silent_tts_free",
            "jira_key": "BOTS-120",
            "status": "staged_not_queued_for_live_publish",
        }
    )
    print(f"produced {cid} sha256={digest}")

(ROOT / "S04_03_UPLOAD_READY_INDEX.local.json").write_text(
    json.dumps({"items": index}, indent=2) + "\n", encoding="utf-8"
)
print(json.dumps({"count": len(index), "all_hashed": True}))
PY
