#!/usr/bin/env python3
"""BOTS-117: deterministic bundle from checked-in, reviewable plain source."""
from pathlib import Path
import hashlib
import re
import zipfile

ROOT = Path(__file__).resolve().parent
WORKFLOW = ROOT.parents[1] / '.github/workflows/wait-how-big-operator.yml'
MEMBERS = ('config.json', 'queue.json', 'whb_operator.py')


def build():
    destination = ROOT / 'operator_bundle.zip'
    with zipfile.ZipFile(destination, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=9) as bundle:
        for name in MEMBERS:
            info = zipfile.ZipInfo(name, date_time=(2026, 9, 12, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            bundle.writestr(info, (ROOT / name).read_bytes(), compresslevel=9)
    digest = hashlib.sha256(destination.read_bytes()).hexdigest()
    text, count = re.subn(r"[a-f0-9]{64}  wait-how-big-social/operator/operator_bundle\.zip",
                        digest + '  wait-how-big-social/operator/operator_bundle.zip',
                        WORKFLOW.read_text(encoding='utf-8'))
    if count != 1: raise RuntimeError('Expected exactly one workflow bundle checksum')
    WORKFLOW.write_text(text, encoding='utf-8', newline='\n')
    print(digest)
    return digest


if __name__ == '__main__': build()
