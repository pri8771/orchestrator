"""Tests for verify._verify_web — the install-then-build verification branch
for web/npm projects (NEXT_MILESTONES: 'Web build targets').

The old node path ran `npm run build --if-present || node -e exit(0)` with NO
install, so a deps-having app either couldn't build or the `||` masked the
failure as ok=True. _verify_web installs first, then builds, and reports
honestly — with environmental (network/auth/timeout) install failures kept as
ran=False (unverified), never a release-blocking ok=False.

Most cases mock verify._run so they're portable (no real npm needed); the one
functional test is gated on npm being present.
"""
import json
import os
import shutil
import tempfile
import unittest
import unittest.mock

import verify


def _write_pkg(d, scripts=None, lockfile=False):
    pkg = {"name": "webapp", "version": "1.0.0"}
    if scripts is not None:
        pkg["scripts"] = scripts
    with open(os.path.join(d, "package.json"), "w", encoding="utf-8") as fh:
        json.dump(pkg, fh)
    if lockfile:
        with open(os.path.join(d, "package-lock.json"), "w", encoding="utf-8") as fh:
            fh.write('{"lockfileVersion": 3}')


class TestVerifyWebSkips(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.d, ignore_errors=True)

    def test_skips_cleanly_when_npm_absent(self):
        _write_pkg(self.d, {"build": "vite build"})
        with unittest.mock.patch.object(shutil, "which", return_value=None):
            res = verify._verify_web(self.d, {}, 60)
        self.assertFalse(res["ran"])
        self.assertFalse(res["ok"])
        # ran=False must classify as "unverified", never "failed".
        self.assertEqual(verify.verification_status(res), "unverified")

    def test_skips_when_no_package_json(self):
        with unittest.mock.patch.object(shutil, "which", return_value="/usr/bin/npm"):
            res = verify._verify_web(self.d, {}, 60)
        self.assertFalse(res["ran"])
        self.assertEqual(verify.verification_status(res), "unverified")


class TestVerifyWebInstallBuild(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.d, ignore_errors=True)
        self._which = unittest.mock.patch.object(
            shutil, "which", return_value="/usr/bin/npm")
        self._which.start()
        self.addCleanup(self._which.stop)
        self.calls = []   # (cmd_str, cwd) per _run_sandboxed invocation

    def _mock_run(self, results):
        """Patch _run so each sandboxed step returns the next canned
        (code, out, err); record the command strings issued."""
        seq = iter(results)

        def fake_run(cmd, cwd, timeout, env=None):
            # cmd is the sandbox-wrapped argv; the shell command is the last elem.
            self.calls.append((cmd[-1], cwd, env))
            return next(seq)
        return unittest.mock.patch.object(verify, "_run", side_effect=fake_run)

    def test_install_then_build_both_succeed(self):
        _write_pkg(self.d, {"build": "vite build"})
        with self._mock_run([(0, "added 42 packages", ""), (0, "built", "")]):
            res = verify._verify_web(self.d, {}, 100)
        self.assertTrue(res["ran"])
        self.assertTrue(res["ok"])
        cmds = [c[0] for c in self.calls]
        self.assertEqual(cmds, ["npm install", "npm run build"])

    def test_install_only_pass_when_no_build_script(self):
        _write_pkg(self.d, {"test": "jest"})   # has scripts, but no "build"
        with self._mock_run([(0, "added 10 packages", "")]):
            res = verify._verify_web(self.d, {}, 100)
        self.assertTrue(res["ran"])
        self.assertTrue(res["ok"])
        # Only install ran — no build command issued.
        self.assertEqual([c[0] for c in self.calls], ["npm install"])
        self.assertIn("no build script", res["summary"])

    def test_build_failure_is_ran_true_ok_false(self):
        _write_pkg(self.d, {"build": "vite build"})
        with self._mock_run([(0, "installed", ""), (1, "", "TypeError: boom")]):
            res = verify._verify_web(self.d, {}, 100)
        self.assertTrue(res["ran"])
        self.assertFalse(res["ok"])
        self.assertEqual(verify.verification_status(res), "failed")
        self.assertIn("boom", res["errors"])

    def test_dependency_error_install_failure_is_ran_true_ok_false(self):
        # A genuine manifest/dependency error (not network) IS a real failure.
        _write_pkg(self.d, {"build": "vite build"})
        with self._mock_run([(1, "", "npm ERR! notarget No matching version found for left-pad@99.99.99")]):
            res = verify._verify_web(self.d, {}, 100)
        self.assertTrue(res["ran"])
        self.assertFalse(res["ok"])
        self.assertEqual(verify.verification_status(res), "failed")
        # Build never attempted after a failed install.
        self.assertEqual([c[0] for c in self.calls], ["npm install"])

    def test_network_install_failure_is_unverified_not_failed(self):
        _write_pkg(self.d, {"build": "vite build"})
        with self._mock_run([(1, "", "npm ERR! network request to https://registry.npmjs.org failed, reason: getaddrinfo ENOTFOUND")]):
            res = verify._verify_web(self.d, {}, 100)
        # Environmental → unverified, must NOT block the release gate.
        self.assertFalse(res["ran"])
        self.assertEqual(verify.verification_status(res), "unverified")

    def test_auth_install_failure_is_unverified(self):
        _write_pkg(self.d, {"build": "vite build"})
        with self._mock_run([(1, "", "npm ERR! code E403\nnpm ERR! 403 Forbidden - GET https://registry/@scope%2fpkg")]):
            res = verify._verify_web(self.d, {}, 100)
        self.assertFalse(res["ran"])
        self.assertEqual(verify.verification_status(res), "unverified")

    def test_install_timeout_is_unverified(self):
        _write_pkg(self.d, {"build": "vite build"})
        # verify._run returns code 124 on TimeoutExpired.
        with self._mock_run([(124, "", "verification timed out after 70s")]):
            res = verify._verify_web(self.d, {}, 100)
        self.assertFalse(res["ran"])
        self.assertEqual(verify.verification_status(res), "unverified")

    def test_env_passed_to_npm_scrubs_secrets_and_sets_cache(self):
        _write_pkg(self.d, {"build": "vite build"})
        with unittest.mock.patch.dict(
                os.environ,
                {"ANTHROPIC_API_KEY": "sk-secret", "PATH": "/usr/bin", "HOME": "/Users/x"},
                clear=True):
            with self._mock_run([(0, "installed", ""), (0, "built", "")]):
                verify._verify_web(self.d, {}, 100)
        install_env = self.calls[0][2]
        self.assertIsNotNone(install_env)
        self.assertNotIn("ANTHROPIC_API_KEY", install_env)   # secret scrubbed
        self.assertIn("PATH", install_env)                   # benign kept
        self.assertEqual(install_env["CI"], "1")
        self.assertTrue(install_env["npm_config_cache"])     # per-verify cache
        self.assertEqual(install_env["PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD"], "1")


class TestNpmEnvAndSecretScrub(unittest.TestCase):
    def test_secret_env_detection(self):
        for name in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GITHUB_TOKEN",
                     "AWS_SECRET_ACCESS_KEY", "MY_PASSWORD", "npm_token",
                     "SESSION_SECRET", "GEMINI_API_KEY"):
            self.assertTrue(verify._is_secret_env(name), name)
        for name in ("PATH", "HOME", "LANG", "TERM", "NODE_ENV", "PORT"):
            self.assertFalse(verify._is_secret_env(name), name)

    def test_npm_env_isolates_cache_and_quiets_npm(self):
        cache = "/tmp/whatever_cache"
        env = verify._npm_env(cache)
        self.assertEqual(env["npm_config_cache"], cache)
        self.assertEqual(env["npm_config_fund"], "false")
        self.assertEqual(env["npm_config_audit"], "false")
        self.assertEqual(env["NO_UPDATE_NOTIFIER"], "1")


class TestRunVerificationRoutesWeb(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.d, ignore_errors=True)

    def test_explicit_web_type_routes_to_verify_web(self):
        _write_pkg(self.d, {"build": "x"})
        with unittest.mock.patch.object(verify, "_verify_web",
                                        return_value={"ran": True, "ok": True,
                                                      "tool": "npm", "summary": "",
                                                      "errors": ""}) as m:
            verify.run_verification(self.d, {"type": "web"}, 60)
        m.assert_called_once()

    def test_auto_detect_node_routes_to_verify_web(self):
        _write_pkg(self.d, {"build": "x"})   # detect_project -> "node"
        self.assertEqual(verify.detect_project(self.d), "node")
        with unittest.mock.patch.object(verify, "_verify_web",
                                        return_value={"ran": True, "ok": True,
                                                      "tool": "npm", "summary": "",
                                                      "errors": ""}) as m:
            verify.run_verification(self.d, {}, 60)   # type omitted -> auto
        m.assert_called_once()


class TestSandboxWrapWriteRoot(unittest.TestCase):
    """D6 fix: when build_dir is under the engine dir, the engine-dir write-deny
    must not block npm writing into build_dir/node_modules."""

    def setUp(self):
        self._which = unittest.mock.patch.object(
            shutil, "which", return_value="/usr/bin/sandbox-exec")
        self._which.start()
        self.addCleanup(self._which.stop)

    def test_write_root_appends_allow_after_deny(self):
        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        argv, profile_path = verify._sandbox_wrap("npm install", write_root=root)
        self.addCleanup(lambda: profile_path and os.path.exists(profile_path)
                        and os.remove(profile_path))
        with open(profile_path, encoding="utf-8") as fh:
            profile = fh.read()
        deny_idx = profile.index("(deny file-write*")
        allow_idx = profile.index("(allow file-write* (subpath")
        # The build_dir allow must come AFTER the deny block (last rule wins).
        self.assertGreater(allow_idx, deny_idx)
        self.assertIn(os.path.abspath(root), profile)

    def test_no_write_root_has_no_extra_allow(self):
        argv, profile_path = verify._sandbox_wrap("npm start")
        self.addCleanup(lambda: profile_path and os.path.exists(profile_path)
                        and os.remove(profile_path))
        with open(profile_path, encoding="utf-8") as fh:
            profile = fh.read()
        self.assertNotIn("(allow file-write* (subpath", profile)


@unittest.skipUnless(shutil.which("npm"), "npm not installed")
class TestVerifyWebFunctional(unittest.TestCase):
    """A real end-to-end install+build on a tiny zero-dependency package —
    proves the whole path (install, no build script -> install-only pass) works
    against the actual npm binary without touching the network for deps."""

    def test_zero_dependency_install_only_pass(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        # No dependencies and no build script: `npm install` succeeds offline,
        # and _verify_web reports an install-only clean pass.
        _write_pkg(d, {})
        res = verify._verify_web(d, {}, 120)
        self.assertTrue(res["ran"], res)
        self.assertTrue(res["ok"], res)


if __name__ == "__main__":
    unittest.main()
