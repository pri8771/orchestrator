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


if __name__ == "__main__":
    unittest.main()
