#!/usr/bin/env python3
"""Deterministically rebuild operator_bundle.zip from src/.

Fixed mtimes and a fixed file order make the archive byte-for-byte
reproducible, so its SHA-256 (pinned in the GitHub Actions workflow's
verify step) only changes when the packaged source actually changes.

Usage: python build_bundle.py
"""
from __future__ import annotations

import hashlib
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC = HERE / "src"
OUTPUT = HERE / "operator_bundle.zip"
FIXED_DATE_TIME = (1980, 1, 1, 0, 0, 0)
FILES = ("whb_operator.py", "queue.json")


def build() -> str:
    if OUTPUT.exists():
        OUTPUT.unlink()
    with zipfile.ZipFile(OUTPUT, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for name in FILES:
            path = SRC / name
            data = path.read_bytes()
            info = zipfile.ZipInfo(filename=name, date_time=FIXED_DATE_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            zf.writestr(info, data)
    digest = hashlib.sha256(OUTPUT.read_bytes()).hexdigest()
    print(f"{digest}  {OUTPUT.name}")
    return digest


if __name__ == "__main__":
    build()
