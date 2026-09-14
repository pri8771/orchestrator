#!/usr/bin/env python3
"""BOTS-120 / S04-05 prep: finite local preparation batch with receipts, dedupe, stop.

Does not publish, does not call LLMs, does not touch WHB-001 or Guru.
Windows note: hashing/receipts are portable; silent encode remains macOS Swift or ffmpeg per PACKET.
"""
from __future__ import annotations

import hashlib
import json
import os
import platform
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PACKET_PATH = ROOT / "S04_05_PACKET.json"
STATE_PATH = ROOT / "content_state.jsonl"
STOP_FILE = ROOT / "STOP"


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def load_prior_ids() -> set[tuple[str, str]]:
    done: set[tuple[str, str]] = set()
    if not STATE_PATH.exists():
        return done
    for line in STATE_PATH.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if row.get("result") == "ok" and row.get("sha256_match") is True:
            done.add((row["content_id"], row.get("sha256", "")))
    return done


def append_receipt(row: dict) -> None:
    with STATE_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def main() -> int:
    if STOP_FILE.exists() or os.environ.get("WHB_STOP") == "1":
        print("STOP engaged; no items processed")
        return 0
    packet = json.loads(PACKET_PATH.read_text(encoding="utf-8"))
    max_items = int(packet.get("max_items", 3))
    items = packet["items"][:max_items]
    prior = load_prior_ids()
    started_batch = time.perf_counter()
    results = []
    completed_items = 0
    for item in items:
        if STOP_FILE.exists() or os.environ.get("WHB_STOP") == "1":
            print("STOP engaged mid-batch")
            break
        cid = item["content_id"]
        expected = item["sha256"]
        path = ROOT / item["relative_path"]
        t0 = time.perf_counter()
        if (cid, expected) in prior and path.is_file() and sha256_file(path) == expected:
            row = {
                "content_id": cid,
                "result": "skipped_duplicate",
                "sha256_expected": expected,
                "started_at": utc_now(),
                "finished_at": utc_now(),
                "runtime_seconds": round(time.perf_counter() - t0, 6),
                "tokens": None,
                "attempt": 1,
            }
            append_receipt(row)
            results.append(row)
            completed_items += 1
            continue
        attempt = 0
        last_err = None
        while attempt < 2:
            attempt += 1
            try:
                if not path.exists():
                    raise FileNotFoundError(path)
                digest = sha256_file(path)
                match = digest == expected
                row = {
                    "content_id": cid,
                    "path": str(path),
                    "sha256": digest,
                    "sha256_expected": expected,
                    "sha256_match": match,
                    "bytes": path.stat().st_size,
                    "result": "ok" if match else "hash_mismatch",
                    "started_at": utc_now(),
                    "finished_at": utc_now(),
                    "runtime_seconds": round(time.perf_counter() - t0, 6),
                    "tokens": None,
                    "attempt": attempt,
                    "machine": platform.node(),
                }
                append_receipt(row)
                results.append(row)
                if match:
                    completed_items += 1
                    break
                last_err = "hash_mismatch"
            except Exception as exc:  # noqa: BLE001 — receipt must record failure
                last_err = str(exc)
                row = {
                    "content_id": cid,
                    "result": "error",
                    "error": last_err,
                    "started_at": utc_now(),
                    "finished_at": utc_now(),
                    "runtime_seconds": round(time.perf_counter() - t0, 6),
                    "tokens": None,
                    "attempt": attempt,
                }
                append_receipt(row)
                results.append(row)
        if last_err and not any(r.get("content_id") == cid and r.get("result") == "ok" for r in results):
            print(f"{cid} failed: {last_err}")
    ok_count = sum(1 for r in results if r.get("result") == "ok")
    skipped = sum(1 for r in results if r.get("result") == "skipped_duplicate")
    summary = {
        "batch_runtime_seconds": round(time.perf_counter() - started_batch, 6),
        "items": results,
        "ok_count": ok_count,
        "skipped_duplicate_count": skipped,
        "tokens": None,
        "jira_key": "BOTS-120",
        "recorded_at_utc": utc_now(),
    }
    (ROOT / "S04_05_BATCH_RECEIPT.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary))
    # Success if every requested item is ok or an intentional dedupe skip; failures otherwise.
    return 0 if completed_items == len(items) else 1


if __name__ == "__main__":
    raise SystemExit(main())
