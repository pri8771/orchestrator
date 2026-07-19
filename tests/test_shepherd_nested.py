"""Fixture-parity + nesting tests for shepherd.sh (V3 board 3.0).

shepherd.sh was deliberately flat-only through the 3.0 seam review. It now
delegates lock naming and session discovery to the engine's single canonical
implementations (orchestrator.encode_lock_name / find_apps) rather than forking
a THIRD bash encoding: bash 3.2 — which shepherd targets — cannot reproduce NFC
normalization or Unicode-aware lowercasing, so a hand-rolled bash encoder could
never stay byte-identical to the shared fixture (see the café / Straße / Cyrillic
cases). These tests drive the REAL script hooks (`--lock-name`,
`--list-sessions`, `--check-lock`) exactly as the fleet loop would, and pin the
derivation against tests/fixtures/lock_encoding.json — the same fixture that
pins orchestrator.encode_lock_name and the Swift GUI. Neither side may edit the
fixture to pass.
"""
import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(HERE)
SHEPHERD = os.path.join(REPO_ROOT, "shepherd.sh")
FIXTURE = os.path.join(HERE, "fixtures", "lock_encoding.json")


def _has_bash_and_py():
    return shutil.which("bash") is not None and shutil.which("python3") is not None


def _fixture_cases():
    with open(FIXTURE, encoding="utf-8") as fh:
        return json.load(fh)["cases"]


def _functions_only_source():
    """Everything up to (not including) the `while true; do` main loop — the
    function definitions, sourced into a fresh bash to call one directly."""
    with open(SHEPHERD, encoding="utf-8") as fh:
        text = fh.read()
    return text[:text.index("\nwhile true; do")]


@unittest.skipUnless(_has_bash_and_py(), "requires bash + python3")
class TestShepherdLockNameParity(unittest.TestCase):
    """`shepherd.sh --lock-name <id>` must equal the shared fixture for EVERY
    case. Runs the real script hook so the whole delegation chain
    (lock_stem -> python3 -> orchestrator.encode_lock_name) is exercised."""

    def _lock_name(self, sid):
        proc = subprocess.run(["bash", SHEPHERD, "--lock-name", sid],
                              cwd=REPO_ROOT, capture_output=True, text=True,
                              timeout=30)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        # The hook prints the stem + one newline; a lock stem never contains a
        # newline (spaces/controls are percent-encoded), so rstrip is exact and
        # would not mask a stray leading/trailing byte in the stem itself.
        return proc.stdout.rstrip("\n")

    def test_every_fixture_case_matches(self):
        cases = _fixture_cases()
        self.assertTrue(cases, "fixture has no cases — test is stale")
        for case in cases:
            self.assertEqual(self._lock_name(case["id"]), case["lock"],
                             "shepherd --lock-name disagrees with the fixture "
                             "for id=%r" % case["id"])

    def test_flat_ids_stay_raw(self):
        # No '/', so lock_stem returns the id verbatim (no python call at all).
        self.assertEqual(self._lock_name("gloam"), "gloam")
        self.assertEqual(self._lock_name("proj--section--chat"),
                         "proj--section--chat")
        # A flat dir literally named like an encoded triple stays raw — it is
        # the digest suffix on the NESTED form that keeps the two from sharing
        # a lock, not any special-casing here.
        self.assertEqual(self._lock_name("proj%2Fsection%2Fchat"),
                         "proj%2Fsection%2Fchat")

    def test_nested_id_gets_percent_encoding_and_digest(self):
        stem = self._lock_name("proj/section/chat")
        self.assertNotIn("/", stem)   # never a path — always a flat filename
        self.assertTrue(stem.startswith("proj%2Fsection%2Fchat."))
        self.assertRegex(stem, r"\.[0-9a-f]{8}$")

    def test_unicode_case_that_bash_alone_could_not_encode(self):
        # The whole reason for delegating: NFC + Unicode lowercasing. A bash
        # tr/printf encoder would mangle these; the fixture pins them exactly.
        self.assertEqual(self._lock_name("café/section/chat"),
                         "caf%C3%A9%2Fsection%2Fchat.61d535d9")
        self.assertEqual(self._lock_name("Straße/S/C"),
                         "Stra%C3%9Fe%2FS%2FC.97b1e256")

    def test_lock_name_hook_does_not_enter_fleet_loop(self):
        start = time.time()
        subprocess.run(["bash", SHEPHERD, "--lock-name", "x/y/z"],
                       cwd=REPO_ROOT, capture_output=True, text=True, timeout=15)
        self.assertLess(time.time() - start, 10)


@unittest.skipUnless(_has_bash_and_py(), "requires bash + python3")
class TestShepherdNestedDiscoveryAndLock(unittest.TestCase):
    """Discovery is delegated to find_apps and lock checks flow through the
    encoded stem, so shepherd can manage nested sessions when the M7 gate is on
    without ever re-deriving the .orch-sections rules or the encoding in bash."""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="shep_nested_")
        os.makedirs(os.path.join(self.root, ".orch-locks"))
        self._procs = []

    def tearDown(self):
        for p in self._procs:
            try:
                p.terminate()
                p.wait(timeout=5)
            except Exception:
                pass
        shutil.rmtree(self.root, ignore_errors=True)

    def _flat_app(self, name):
        d = os.path.join(self.root, name, "initial_prompt")
        os.makedirs(d)
        with open(os.path.join(d, "initial_prompt.md"), "w") as fh:
            fh.write("# build\n")

    def _nested_session(self, project, section, chat):
        d = os.path.join(self.root, project, section, chat, "initial_prompt")
        os.makedirs(d)
        with open(os.path.join(d, "initial_prompt.md"), "w") as fh:
            fh.write("# build\n")
        # The marker is what makes the engine (and thus list_sessions) recurse.
        open(os.path.join(self.root, project, ".orch-sections"), "w").close()

    def _live_pid(self):
        p = subprocess.Popen(["sleep", "60"])
        self._procs.append(p)
        return p.pid

    def _shep(self, *args):
        env = dict(os.environ)
        env["ORCH_ROOT"] = self.root
        return subprocess.run(["bash", SHEPHERD, *args], cwd=REPO_ROOT,
                              capture_output=True, text=True, timeout=30, env=env)

    def test_list_sessions_finds_flat_and_nested(self):
        self._flat_app("flatapp")
        self._nested_session("proj", "sec", "chat")
        proc = self._shep("--list-sessions")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        out = set(proc.stdout.split())
        self.assertIn("flatapp", out)
        self.assertIn("proj/sec/chat", out)

    def test_list_sessions_ignores_unmarked_wrapper(self):
        # A two-level-deep dir with no .orch-sections marker must NOT surface as
        # a session — same recursion guard the engine enforces (one impl).
        d = os.path.join(self.root, "wrapper", "sec", "chat", "initial_prompt")
        os.makedirs(d)
        with open(os.path.join(d, "initial_prompt.md"), "w") as fh:
            fh.write("# build\n")
        out = self._shep("--list-sessions").stdout.split()
        self.assertNotIn("wrapper/sec/chat", out)

    def test_list_sessions_empty_on_missing_root(self):
        shutil.rmtree(self.root)
        proc = self._shep("--list-sessions")
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout.strip(), "")

    def test_nested_locked_consults_the_encoded_lock(self):
        # The core M7 correctness property: a live nested run holds its lock at
        # the ENCODED stem. A bare "café/sec/chat.lock" check would miss it and
        # shepherd would double-launch. locked() -> lock_stem must resolve it.
        self._nested_session("café", "sec", "chat")
        stem = self._shep("--lock-name", "café/sec/chat").stdout.rstrip("\n")
        self.assertIn("%", stem)  # encoded, not raw
        with open(os.path.join(self.root, ".orch-locks", stem + ".lock"), "w") as fh:
            fh.write("pid=%d host=test started=2026-07-18 00:00:00\n" % self._live_pid())
        self.assertEqual(self._shep("--check-lock", "café/sec/chat").returncode, 0)

    def test_nested_without_lock_reads_free(self):
        self._nested_session("proj", "sec", "chat")
        self.assertEqual(self._shep("--check-lock", "proj/sec/chat").returncode, 1)

    def test_nested_stale_lock_reads_free(self):
        # A crashed nested run's leftover lock (dead pid) at the encoded stem
        # must read FREE, exactly like the flat staleness fix.
        self._nested_session("proj", "sec", "chat")
        stem = self._shep("--lock-name", "proj/sec/chat").stdout.rstrip("\n")
        dead = subprocess.Popen(["sleep", "30"])
        dead.terminate()
        dead.wait(timeout=5)
        with open(os.path.join(self.root, ".orch-locks", stem + ".lock"), "w") as fh:
            fh.write("pid=%d host=test started=2026-07-18 00:00:00\n" % dead.pid)
        self.assertEqual(self._shep("--check-lock", "proj/sec/chat").returncode, 1)


@unittest.skipUnless(_has_bash_and_py(), "requires bash")
class TestShepherdNestedGate(unittest.TestCase):
    """Nested auto-relaunch is OFF unless ORCH_SHEPHERD_NESTED is set, so the
    current human-paced nested chats are never resurrected and the fleet can
    still declare completion. Exercises the pure-bash gate function directly."""

    def _gate_rc(self, env_set):
        funcs = _functions_only_source()
        prefix = "ORCH_SHEPHERD_NESTED=1 " if env_set else ""
        proc = subprocess.run(
            ["bash", "-c", "%s\n%snested_autorun_enabled" % (funcs, prefix)],
            capture_output=True, text=True, timeout=15)
        return proc.returncode

    def test_gate_off_by_default(self):
        self.assertEqual(self._gate_rc(env_set=False), 1)   # 1 = disabled

    def test_gate_on_when_env_set(self):
        self.assertEqual(self._gate_rc(env_set=True), 0)    # 0 = enabled


if __name__ == "__main__":
    unittest.main()
