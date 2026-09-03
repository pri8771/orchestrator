#!/usr/bin/env python3
"""Regression test for BOTS-9: the packaged WHB operator must not shadow the
Python stdlib `operator` module, and the no-key path must exit cleanly.

Run: python wait-how-big-social/operator/test_bundle.py
Exits 0 and prints "PASS" on success; raises AssertionError / prints "FAIL"
and exits non-zero otherwise. Deliberately dependency-free (stdlib only) so
it runs the same locally and under actions/setup-python in CI.
"""
from __future__ import annotations

import hashlib
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
BUNDLE = HERE / "operator_bundle.zip"
WORKFLOW = HERE.parent.parent / ".github" / "workflows" / "wait-how-big-operator.yml"


def test_bundle_does_not_shadow_stdlib_operator() -> None:
    names = zipfile.ZipFile(BUNDLE).namelist()
    assert "operator.py" not in names, (
        "operator_bundle.zip still packages operator.py, which shadows the "
        "stdlib operator module and reproduces the circular-import failure "
        "from Actions run 33342049065."
    )
    assert "whb_operator.py" in names, "operator_bundle.zip is missing whb_operator.py"


def test_workflow_sha256_matches_bundle() -> None:
    workflow_text = WORKFLOW.read_text(encoding="utf-8")
    match = re.search(r"echo '([0-9a-f]{64})\s+wait-how-big-social/operator/operator_bundle\.zip'", workflow_text)
    assert match, "Could not find the pinned SHA-256 in wait-how-big-operator.yml"
    pinned = match.group(1)
    actual = hashlib.sha256(BUNDLE.read_bytes()).hexdigest()
    assert pinned == actual, (
        f"Workflow pins SHA-256 {pinned} but operator_bundle.zip actually hashes to {actual}. "
        "Run build_bundle.py and update the workflow's verify step."
    )
    assert "python /tmp/whb-operator/whb_operator.py" in workflow_text, (
        "Workflow no longer invokes whb_operator.py by its renamed entrypoint."
    )


def test_packaged_executable_starts_without_shadowing_and_exits_clean_on_missing_key() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="whb-bundle-test-"))
    try:
        with zipfile.ZipFile(BUNDLE) as zf:
            zf.extractall(tmp)
        # No state.json present on purpose: load_json() must fall back to defaults.
        env = {"PATH": __import__("os").environ.get("PATH", "")}
        result = subprocess.run(
            [sys.executable, str(tmp / "whb_operator.py")],
            cwd=tmp,
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, (
            f"Packaged operator exited {result.returncode} with no BUFFER_API_KEY set.\n"
            f"stdout: {result.stdout}\nstderr: {result.stderr}"
        )
        assert "WAIT_HOW_BIG_NOT_CONFIGURED" in result.stdout, (
            f"Expected the fail-closed missing-key message; got stdout: {result.stdout!r}"
        )
        lowered = (result.stdout + result.stderr).lower()
        assert "circular import" not in lowered and "cannot import name" not in lowered, (
            f"Circular-import shadowing regression detected.\nstdout: {result.stdout}\nstderr: {result.stderr}"
        )
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


TESTS = [
    test_bundle_does_not_shadow_stdlib_operator,
    test_workflow_sha256_matches_bundle,
    test_packaged_executable_starts_without_shadowing_and_exits_clean_on_missing_key,
]


def main() -> int:
    failures = 0
    for test in TESTS:
        try:
            test()
            print(f"PASS: {test.__name__}")
        except AssertionError as exc:
            failures += 1
            print(f"FAIL: {test.__name__}: {exc}")
    if failures:
        print(f"{failures}/{len(TESTS)} tests failed")
        return 1
    print(f"All {len(TESTS)} tests passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
