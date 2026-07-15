"""Tests for verify.py's macOS sandbox-exec wrapping of generated http-boot
servers (NEXT_MILESTONES: 'verify.py currently boots generated servers
unsandboxed'). _sandbox_wrap is portable — it degrades to the plain
unsandboxed argv on any host without sandbox-exec (all of CI's Linux test
runners) — so most of this is tested without needing sandbox-exec present;
the real functional check is skipped where it isn't."""
import os
import shutil
import tempfile
import unittest
import unittest.mock

import verify


class TestSandboxWrapFallback(unittest.TestCase):
    def test_falls_back_to_plain_argv_when_sandbox_exec_missing(self):
        with unittest.mock.patch.object(shutil, "which", return_value=None):
            argv, profile_path = verify._sandbox_wrap("python3 app.py")
        self.assertEqual(argv, ["/bin/sh", "-lc", "python3 app.py"])
        self.assertIsNone(profile_path)

    def test_falls_back_when_profile_cannot_be_written(self):
        with unittest.mock.patch.object(shutil, "which", return_value="/usr/bin/sandbox-exec"), \
             unittest.mock.patch.object(tempfile, "mkstemp", side_effect=OSError("no space")):
            argv, profile_path = verify._sandbox_wrap("python3 app.py")
        self.assertEqual(argv, ["/bin/sh", "-lc", "python3 app.py"])
        self.assertIsNone(profile_path)


class TestSandboxWrapProfile(unittest.TestCase):
    """Profile-generation logic, checked without actually invoking
    sandbox-exec — portable to any host."""

    def setUp(self):
        self._patcher = unittest.mock.patch.object(
            shutil, "which", return_value="/usr/bin/sandbox-exec")
        self._patcher.start()
        self.addCleanup(self._patcher.stop)

    def test_argv_shape_wraps_command_with_generated_profile(self):
        argv, profile_path = verify._sandbox_wrap("npm start")
        self.addCleanup(lambda: os.path.exists(profile_path) and os.remove(profile_path))
        self.assertEqual(argv[0:2], ["sandbox-exec", "-f"])
        self.assertEqual(argv[2], profile_path)
        self.assertEqual(argv[3:], ["/bin/sh", "-lc", "npm start"])
        self.assertTrue(os.path.exists(profile_path))

    def test_profile_denies_sensitive_paths_and_allows_by_default(self):
        _argv, profile_path = verify._sandbox_wrap("npm start")
        self.addCleanup(lambda: os.path.exists(profile_path) and os.remove(profile_path))
        with open(profile_path, encoding="utf-8") as fh:
            profile = fh.read()
        self.assertIn("(allow default)", profile)
        self.assertIn(os.path.expanduser("~/.ssh"), profile)
        self.assertIn(os.path.expanduser("~/.orchestrator"), profile)
        self.assertIn(os.path.expanduser("~/Library/Keychains"), profile)
        # The engine's own source must never be writable by a generated server.
        self.assertIn(os.path.dirname(os.path.abspath(verify.__file__)), profile)

    def test_each_call_gets_its_own_profile_file(self):
        _argv1, p1 = verify._sandbox_wrap("cmd one")
        _argv2, p2 = verify._sandbox_wrap("cmd two")
        self.addCleanup(lambda: os.path.exists(p1) and os.remove(p1))
        self.addCleanup(lambda: os.path.exists(p2) and os.remove(p2))
        self.assertNotEqual(p1, p2)


@unittest.skipUnless(shutil.which("sandbox-exec"), "sandbox-exec is macOS-only")
class TestSandboxWrapFunctional(unittest.TestCase):
    """Real end-to-end check on an actual macOS host: a sandboxed command can
    still read/write inside its own directory and reach the network (both
    required for a real dev server to boot and respond), but can't write to
    a denied sensitive path."""

    def test_write_inside_build_dir_and_network_still_work(self):
        build_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, build_dir, ignore_errors=True)
        argv, profile_path = verify._sandbox_wrap(
            "echo hello > out.txt && cat out.txt")
        self.addCleanup(lambda: os.path.exists(profile_path) and os.remove(profile_path))
        out = verify.subprocess.run(argv, cwd=build_dir, capture_output=True,
                                    text=True, timeout=10)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("hello", out.stdout)

    def test_write_to_denied_sensitive_path_is_blocked(self):
        argv, profile_path = verify._sandbox_wrap(
            'echo pwned > "$HOME/.ssh/should_not_exist_%d"' % os.getpid())
        self.addCleanup(lambda: os.path.exists(profile_path) and os.remove(profile_path))
        target = os.path.expanduser("~/.ssh/should_not_exist_%d" % os.getpid())
        self.addCleanup(lambda: os.path.exists(target) and os.remove(target))
        out = verify.subprocess.run(argv, capture_output=True, text=True, timeout=10)
        self.assertNotEqual(out.returncode, 0)
        self.assertFalse(os.path.exists(target))


if __name__ == "__main__":
    unittest.main()
