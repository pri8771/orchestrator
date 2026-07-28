"""A-31: one project's unexpected crash must not abort the rest of a
sequential pass (run_once's default single-worker branch) nor kill the
--watch daemon (main's loop) — the CLI contract is '--watch SECONDS: loop
forever'. Crashes are already recorded by process_app; containment here is
about the OTHER apps and the NEXT pass."""
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

import orchestrator as orch


def _mk_app(root, name):
    d = os.path.join(root, name, "initial_prompt")
    os.makedirs(d)
    with open(os.path.join(d, "initial_prompt.md"), "w",
              encoding="utf-8") as fh:
        fh.write("p\n")


class TestSequentialPassContainment(unittest.TestCase):
    def test_crash_in_first_app_still_processes_second(self):
        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root, True)
        _mk_app(root, "app-a")
        _mk_app(root, "app-b")
        processed = []

        def fake_process_app(_cfg, _root, app):
            processed.append(app)
            if app == "app-a":
                raise RuntimeError("boom in app-a")

        # Default runtime -> 1 project worker -> the sequential branch.
        cfg = {"root": root, "runtime": {}}
        with mock.patch.object(orch, "process_app",
                               side_effect=fake_process_app):
            orch.run_once(cfg)  # must not raise
        self.assertEqual(processed, ["app-a", "app-b"],
                         "app-a's crash must not abort app-b's turn")


class TestWatchLoopContainment(unittest.TestCase):
    def test_watch_survives_a_crashed_pass(self):
        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root, True)
        _mk_app(root, "app-a")
        calls = []

        def fake_run_once(_cfg):
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("pass crashed")
            # Second pass: stop the daemon the way _cleanup does. SystemExit
            # must NOT be swallowed by the containment (except Exception).
            raise SystemExit(0)

        argv = ["orchestrator.py", "--root", root, "--watch", "1"]
        with mock.patch.object(sys, "argv", argv), \
                mock.patch.object(orch, "run_once",
                                  side_effect=fake_run_once), \
                mock.patch.object(orch, "doctor", lambda _cfg: None), \
                mock.patch.object(orch.time, "sleep", lambda _s: None):
            with self.assertRaises(SystemExit):
                orch.main()
        self.assertEqual(len(calls), 2,
                         "the crash in pass 1 must not kill the watch loop")


if __name__ == "__main__":
    unittest.main()
