"""Regression tests for the agent/build subprocess hang.

The bug: subprocess.run(timeout=..., capture_output=True) kills only the direct
child on timeout, then blocks forever re-reaping a stdout pipe a spawned
grandchild still holds open (agy/gemini and xcodebuild do exactly this). These
tests pin the fix — procutil.run_capture kills the whole process group — and the
no-output heartbeat that catches silent hangs long before the overall timeout.
"""
import os
import subprocess
import sys
import time
import unittest

import procutil
import orchestrator as orch


# A child shell that backgrounds a long sleep (which inherits our stdout/stderr
# pipes) and then exits immediately. With plain subprocess.run the reaping
# communicate() would block forever on the never-closing pipe; run_capture must
# kill the group and return promptly.
_GRANDCHILD = ["/bin/sh", "-c", "sleep 30 & exit 0"]
_SILENT = [sys.executable, "-c", "import time; time.sleep(30)"]
_STREAMING = [sys.executable, "-c",
              "import sys,time\n"
              "for _ in range(5):\n"
              "    sys.stdout.write('x'); sys.stdout.flush(); time.sleep(0.3)"]


@unittest.skipUnless(os.name == "posix", "process-group kill is POSIX-only")
class TestRunCapture(unittest.TestCase):
    def test_normal_command_returns_output_and_code(self):
        out, err, code = procutil.run_capture(
            [sys.executable, "-c", "import sys; sys.stdout.write('hello'); sys.exit(3)"],
            timeout=10)
        self.assertEqual(out, "hello")
        self.assertEqual(code, 3)

    def test_pipe_inheriting_grandchild_does_not_hang(self):
        # The core regression: without the group-kill this call never returns.
        t0 = time.monotonic()
        with self.assertRaises(subprocess.TimeoutExpired):
            procutil.run_capture(_GRANDCHILD, timeout=2)
        self.assertLess(time.monotonic() - t0, 20,
                        "run_capture hung on a pipe-inheriting grandchild")

    def test_heartbeat_kills_silent_process_early(self):
        # 30s sleeper, 60s overall timeout, 1s heartbeat -> killed in ~1s, not 60.
        self.assertTrue(issubclass(procutil.NoOutputTimeout, subprocess.TimeoutExpired))
        t0 = time.monotonic()
        with self.assertRaises(procutil.NoOutputTimeout):
            procutil.run_capture(_SILENT, timeout=60, heartbeat=1)
        self.assertLess(time.monotonic() - t0, 15,
                        "heartbeat did not fire well before the overall timeout")

    def test_heartbeat_allows_streaming_process(self):
        # Emits a byte every 0.3s: each write resets the 1s idle timer, so a
        # slow-but-productive process is NOT falsely killed.
        out, err, code = procutil.run_capture(_STREAMING, timeout=30, heartbeat=1)
        self.assertEqual(out, "xxxxx")
        self.assertEqual(code, 0)


@unittest.skipUnless(os.name == "posix", "process-group kill is POSIX-only")
class TestAgentChokepoint(unittest.TestCase):
    def test_run_subprocess_raises_within_timeout(self):
        # Item 3: the agent chokepoint must RAISE (not hang) on the grandchild
        # case so call_agent's graceful timeout path can run.
        t0 = time.monotonic()
        with self.assertRaises(subprocess.TimeoutExpired):
            orch._run_subprocess(_GRANDCHILD, cwd=None, timeout=2)
        self.assertLess(time.monotonic() - t0, 20)

    def test_git_timeout_returns_not_raises(self):
        # _git must never raise; a hung git turns into a non-zero code instead.
        code, out, err = orch._git("/", "wait-for-a-command-that-hangs-would-here")
        self.assertNotEqual(code, 0)   # bad subcommand -> non-zero, no exception


if __name__ == "__main__":
    unittest.main()
