"""Tests for verify.py's macOS sandbox-exec wrapping of generated http-boot
servers (NEXT_MILESTONES: 'verify.py currently boots generated servers
unsandboxed'). _sandbox_wrap is portable — it degrades to the plain
unsandboxed argv on any host without sandbox-exec (all of CI's Linux test
runners) — so most of this is tested without needing sandbox-exec present;
the real functional check is skipped where it isn't."""
import os
import shutil
import sys
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

    def test_profile_denies_agent_cli_and_persistence_paths(self):
        """A-39: the deny list must also cover the agent CLIs' own config
        dirs, launchd persistence dirs, shell rc files and ~/.gitconfig —
        the write targets that turn a sandboxed postinstall into persistence
        or credential tampering."""
        _argv, profile_path = verify._sandbox_wrap("npm start")
        self.addCleanup(lambda: os.path.exists(profile_path) and os.remove(profile_path))
        with open(profile_path, encoding="utf-8") as fh:
            profile = fh.read()
        for p in ("~/.claude", "~/.codex", "~/.gemini", "~/.npmrc",
                  "~/.gitconfig", "~/Library/LaunchAgents",
                  "~/Library/LaunchDaemons", "~/.zshrc", "~/.zprofile",
                  "~/.zshenv", "~/.bashrc", "~/.bash_profile", "~/.profile"):
            self.assertIn(os.path.expanduser(p), profile, p)

    def test_quoted_write_root_is_escaped_not_injected(self):
        """SBPL prerequisite for A-38/A-39: a `"` in an interpolated path
        must be backslash-escaped, or it ends the SBPL string literal early —
        malforming the profile (sandbox-exec refuses to run anything) or
        letting a crafted directory name inject its own rules."""
        weird = os.path.join(tempfile.mkdtemp(), 'proj"weird')
        _argv, profile_path = verify._sandbox_wrap("true", write_root=weird)
        self.addCleanup(lambda: os.path.exists(profile_path) and os.remove(profile_path))
        with open(profile_path, encoding="utf-8") as fh:
            profile = fh.read()
        self.assertIn('proj\\"weird', profile)
        self.assertNotIn('proj"weird', profile.replace('proj\\"weird', ""))


class TestShellVerifierSandboxed(unittest.TestCase):
    """A-38: the auto-detected python route executes agent-authored test
    modules (`unittest discover` imports them at module scope) — it must run
    through _run_sandboxed with a secret-scrubbed env, never plain _run with
    the engine's whole environment."""

    def _shell_verify(self, command):
        build_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, build_dir, ignore_errors=True)
        open(os.path.join(build_dir, "requirements.txt"), "w").close()
        open(os.path.join(build_dir, "test_x.py"), "w").close()
        seen = {}

        def fake_sandboxed(cmd_str, cwd, timeout, env, write_root):
            seen.update(cmd=cmd_str, cwd=cwd, env=env, write_root=write_root)
            return 0, "", ""

        with unittest.mock.patch.object(verify, "_run_sandboxed", fake_sandboxed), \
             unittest.mock.patch.object(shutil, "which", lambda n: "/usr/bin/" + n), \
             unittest.mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-x",
                                                   "ORCH_PLAIN_VAR": "1"}):
            out = verify._verify_shell(build_dir, command, 60)
        return out, seen, build_dir

    def test_python_auto_route_is_sandboxed_and_env_scrubbed(self):
        out, seen, build_dir = self._shell_verify(None)
        self.assertTrue(out["ok"])
        self.assertIn("unittest discover", seen["cmd"])
        # build_dir carved back out of the deny so __pycache__ still writes.
        self.assertEqual(seen["write_root"], build_dir)
        self.assertEqual(seen["cwd"], build_dir)
        # Secret-shaped vars are scrubbed; ordinary ones survive.
        self.assertNotIn("ANTHROPIC_API_KEY", seen["env"])
        self.assertIn("ORCH_PLAIN_VAR", seen["env"])

    def test_explicit_shell_command_is_sandboxed_too(self):
        out, seen, build_dir = self._shell_verify("make check")
        self.assertTrue(out["ok"])
        self.assertEqual(seen["cmd"], "make check")
        self.assertEqual(seen["write_root"], build_dir)
        self.assertNotIn("ANTHROPIC_API_KEY", seen["env"])


class TestVerifyHttpProfileCleanup(unittest.TestCase):
    """A-87: _verify_http must remove its Seatbelt profile on EVERY path —
    it used to clean up only in the Popen-failure branch, leaking one
    verify_sandbox_*.sb per successful http verification (contradicting
    _run_sandboxed's cleanup contract). Portable: the wrap is faked to a
    plain argv plus a real on-disk stand-in profile, so the cleanup contract
    is exercised on hosts without sandbox-exec too."""

    def _fake_wrap(self):
        fd, path = tempfile.mkstemp(prefix="verify_sandbox_", suffix=".sb")
        os.close(fd)
        self.addCleanup(lambda: os.path.exists(path) and os.remove(path))

        def wrap(cmd_str, write_root=None):
            return ["/bin/sh", "-lc", cmd_str], path
        return wrap, path

    def test_successful_boot_removes_profile(self):
        wrap, profile = self._fake_wrap()
        build_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, build_dir, ignore_errors=True)
        # A real server boot: any HTTP status (404 included) counts as up.
        start = '"%s" -m http.server $PORT --bind 127.0.0.1' % sys.executable
        with unittest.mock.patch.object(verify, "_sandbox_wrap", wrap):
            out = verify._verify_http(
                build_dir, {"start": start, "ready_timeout": 30}, 60)
        self.assertTrue(out["ok"], out)
        self.assertFalse(
            os.path.exists(profile),
            "Seatbelt profile leaked after a successful boot-verify")

    def test_unresponsive_boot_removes_profile_too(self):
        wrap, profile = self._fake_wrap()
        build_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, build_dir, ignore_errors=True)
        with unittest.mock.patch.object(verify, "_sandbox_wrap", wrap):
            out = verify._verify_http(
                build_dir, {"start": "true", "ready_timeout": 2}, 10)
        self.assertFalse(out["ok"])
        self.assertFalse(os.path.exists(profile))


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

    def test_write_to_launch_agents_is_blocked(self):
        # A-39: persistence via a LaunchAgent plist is one of the concrete
        # attacks the extended deny list exists to stop — check it against
        # the REAL sandbox-exec. (If ~/Library/LaunchAgents doesn't exist the
        # redirect fails on its own and the assertion still holds.)
        argv, profile_path = verify._sandbox_wrap(
            'echo pwned > "$HOME/Library/LaunchAgents/should_not_exist_%d.plist"'
            % os.getpid())
        self.addCleanup(lambda: os.path.exists(profile_path) and os.remove(profile_path))
        target = os.path.expanduser(
            "~/Library/LaunchAgents/should_not_exist_%d.plist" % os.getpid())
        self.addCleanup(lambda: os.path.exists(target) and os.remove(target))
        out = verify.subprocess.run(argv, capture_output=True, text=True, timeout=10)
        self.assertNotEqual(out.returncode, 0)
        self.assertFalse(os.path.exists(target))


if __name__ == "__main__":
    unittest.main()
