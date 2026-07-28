"""Subprocess-driven tests for install_launch_agent.sh (mirroring
tests/test_run_sh.py's style — the REAL script runs as a subprocess, not a
re-implementation).

The non-Darwin platform guard is covered deterministically on Linux. The
install/uninstall paths are covered everywhere by shimming `uname` (reports
Darwin) and `launchctl` (no-op) onto PATH with HOME pointed at a scratch dir,
so no real LaunchAgent is ever written or loaded. Anything that would need a
real launchctl/macOS session is skip-decorated.
"""
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(HERE)
SCRIPT = os.path.join(REPO_ROOT, "install_launch_agent.sh")

LABEL = "com.orchestrator.autonomous"


def _has_bash():
    return shutil.which("bash") is not None


@unittest.skipUnless(_has_bash(), "requires bash")
@unittest.skipIf(sys.platform == "darwin", "guard only triggers off-macOS")
class TestNonDarwinGuard(unittest.TestCase):
    def test_exits_1_with_clear_message_on_linux(self):
        proc = subprocess.run(["bash", SCRIPT], cwd=REPO_ROOT,
                              capture_output=True, text=True, timeout=30)
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("macOS LaunchAgent", proc.stderr)
        # Points the user at the Linux alternative instead of a bare refusal.
        self.assertIn("cron", proc.stderr)

    def test_uninstall_also_guarded_off_macos(self):
        # The guard runs before argument handling, so even `uninstall` refuses
        # (there is nothing launchctl-shaped to uninstall here anyway).
        proc = subprocess.run(["bash", SCRIPT, "uninstall"], cwd=REPO_ROOT,
                              capture_output=True, text=True, timeout=30)
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("macOS LaunchAgent", proc.stderr)


@unittest.skipUnless(_has_bash(), "requires bash")
class TestInstallUninstallWithShims(unittest.TestCase):
    """Exercise the Darwin code path hermetically: `uname` shimmed to report
    Darwin, `launchctl` shimmed to a no-op, HOME pointed at a scratch dir."""

    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="orch_launchagent_home_")
        self.addCleanup(shutil.rmtree, self.home, True)
        self.bin = os.path.join(self.home, "shim-bin")
        os.makedirs(self.bin)
        self._shim("uname", 'echo "Darwin"')
        self._shim("launchctl", "exit 0")
        # plutil is macOS-only too; shim it so the script's -lint validation
        # step runs hermetically on Linux (a real-plutil test below is
        # darwin-gated).
        self._shim("plutil", "exit 0")

    def _shim(self, name, body):
        path = os.path.join(self.bin, name)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("#!/usr/bin/env bash\n%s\n" % body)
        os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR | stat.S_IXGRP)

    def _run(self, *args, extra_env=None):
        env = dict(os.environ)
        env["PATH"] = self.bin + os.pathsep + env.get("PATH", "")
        env["HOME"] = self.home
        env["ORCH_ROOT"] = os.path.join(self.home, "factory-root")
        if extra_env:
            env.update(extra_env)
        return subprocess.run(["bash", SCRIPT, *args], cwd=REPO_ROOT,
                              capture_output=True, text=True, timeout=30, env=env)

    def _plist_path(self):
        return os.path.join(self.home, "Library", "LaunchAgents",
                            "%s.plist" % LABEL)

    def test_install_writes_plist_with_interval_and_run_sh(self):
        proc = self._run(extra_env={"INTERVAL": "900"})
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("Installed and loaded %s" % LABEL, proc.stdout)
        self.assertIn("every 900s", proc.stdout)
        with open(self._plist_path(), encoding="utf-8") as fh:
            plist = fh.read()
        self.assertIn("<integer>900</integer>", plist)
        self.assertIn(os.path.join(REPO_ROOT, "run.sh"), plist)
        self.assertIn("<string>--once</string>", plist)
        self.assertIn("<string>%s</string>" % LABEL, plist)
        # The workspace root it created and pointed WorkingDirectory at.
        root = os.path.join(self.home, "factory-root")
        self.assertIn("<string>%s</string>" % root, plist)
        self.assertTrue(os.path.isdir(root))

    def test_default_interval_is_1800(self):
        proc = self._run()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        with open(self._plist_path(), encoding="utf-8") as fh:
            self.assertIn("<integer>1800</integer>", fh.read())

    def test_uninstall_removes_plist(self):
        self.assertEqual(self._run().returncode, 0)
        self.assertTrue(os.path.exists(self._plist_path()))
        proc = self._run("uninstall")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("Uninstalled %s" % LABEL, proc.stdout)
        self.assertFalse(os.path.exists(self._plist_path()))

    def test_xml_metacharacters_in_paths_and_label_are_escaped(self):
        # A-62: '&' / '<' / '>' in an interpolated path or label used to land
        # verbatim inside the plist's <string> elements — malformed XML that
        # launchctl rejects. They must come out entity-escaped.
        root = os.path.join(self.home, "Apps & Tools", "factory<root>")
        label = "com.orch.a&b"
        proc = self._run(extra_env={"ORCH_ROOT": root,
                                    "ORCH_LAUNCH_LABEL": label})
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        plist_path = os.path.join(self.home, "Library", "LaunchAgents",
                                  "%s.plist" % label)
        with open(plist_path, encoding="utf-8") as fh:
            plist = fh.read()
        self.assertIn("Apps &amp; Tools", plist)
        self.assertIn("factory&lt;root&gt;", plist)
        self.assertIn("<string>com.orch.a&amp;b</string>", plist)
        self.assertNotIn("Apps & Tools", plist)

    def test_launchctl_load_failure_is_fatal_and_reported(self):
        # A-62: launchctl's exit status used to be swallowed — the script
        # printed 'Installed and loaded' even when the load failed.
        self._shim("launchctl", "exit 1")
        proc = self._run()
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("launchctl load failed", proc.stderr)
        self.assertNotIn("Installed and loaded", proc.stdout)

    def test_plutil_lint_failure_is_fatal_and_skips_launchctl_load(self):
        # An invalid plist must stop the install BEFORE launchd sees it.
        self._shim("plutil", "exit 1")
        marker = os.path.join(self.home, "launchctl.calls")
        self._shim("launchctl", 'echo "$@" >> "%s"; exit 0' % marker)
        proc = self._run()
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("plutil -lint", proc.stderr)
        self.assertNotIn("Installed and loaded", proc.stdout)
        self.assertFalse(os.path.exists(marker))

    @unittest.skipUnless(sys.platform == "darwin", "requires real plutil")
    def test_escaped_plist_passes_real_plutil_lint(self):
        # End-to-end on macOS: delegate the shim to the REAL plutil so the
        # escaped plist is genuinely parsed. Before the A-62 fix, a root
        # containing '&' failed this lint.
        self._shim("plutil", 'exec /usr/bin/plutil "$@"')
        root = os.path.join(self.home, "Apps & Tools", "factory<root>")
        proc = self._run(extra_env={"ORCH_ROOT": root})
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)


if __name__ == "__main__":
    unittest.main()
