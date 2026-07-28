"""E5 writable-clone boundary for post-enrollment builds."""

import hashlib
import os
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

import artifacts
import enroll
import orchestrator as orch


HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


def _tree_hash(root):
    digest = hashlib.sha256()
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        for name in sorted(filenames):
            path = os.path.join(dirpath, name)
            digest.update(os.path.relpath(path, root).encode("utf-8"))
            if os.path.islink(path):
                digest.update(os.readlink(path).encode("utf-8"))
            else:
                with open(path, "rb") as fh:
                    digest.update(fh.read())
    return digest.hexdigest()


def _git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True,
                          capture_output=True, text=True).stdout.strip()


class CloneFixture(unittest.TestCase):
    def setUp(self):
        self.base = tempfile.mkdtemp(prefix="enroll-e5-")
        self.addCleanup(shutil.rmtree, self.base, True)
        self.root = os.path.join(self.base, "workspace")
        self.origin = os.path.join(self.base, "origin")
        os.makedirs(self.origin)
        _write(os.path.join(self.origin, "Package.swift"),
               "// swift-tools-version: 5.9\n")
        _write(os.path.join(self.origin, "Sources", "App.swift"),
               "struct Original {}\n")

    def init_git(self):
        _git(self.origin, "init", "-q")
        _git(self.origin, "config", "user.email", "test@example.com")
        _git(self.origin, "config", "user.name", "Test")
        _git(self.origin, "add", "-A")
        _git(self.origin, "commit", "-q", "-m", "fixture")

    def gated_project(self):
        result = enroll.scaffold(self.root, self.origin, name="adopted")
        state = orch.load_state(result["app_dir"])
        state.update({
            "workflow": "enroll",
            "prompt_hash": "old",
            "completed_phases": ["enroll_report"],
            "phase_outputs": {"enroll_report": "Observed report."},
            "enrollment_gate": {"phase": "enroll_report", "at": "now"},
        })
        orch.save_state(result["app_dir"], state)
        aid = artifacts.publish(
            result["app_dir"], "# Compliance\n", {
                "type": "compliance_report", "title": "Compliance",
                "findings": [{"rule": "knowledge/ios/example.md",
                              "verdict": "compliant",
                              "evidence_paths": ["Package.swift"],
                              "why": "The manifest is present."}],
            }, artifacts.load_registry(HERE), consensus=True)
        self.assertIsNotNone(aid)
        return result["app_dir"]


class TestWritableEnrollmentClone(CloneFixture):
    def test_git_promote_iterate_cycle_never_mutates_origin(self):
        self.init_git()
        app_dir = self.gated_project()
        source_file = os.path.join(self.origin, "Sources", "App.swift")
        before_hash = _tree_hash(self.origin)
        before_mtime = os.stat(source_file).st_mtime_ns

        rc, target = orch.promote_chat(self.root, "adopted")
        self.assertEqual((rc, target), (0, "adopted"))
        build = os.path.join(app_dir, "app_build")
        self.assertEqual(_git(build, "branch", "--show-current"),
                         "enroll/adopted")
        self.assertNotEqual(os.stat(source_file).st_ino,
                            os.stat(os.path.join(build, "Sources", "App.swift")).st_ino)

        def phase_stub(cfg, app, project_dir, phase, prompt, prior, state,
                       phase_index=0):
            if phase.get("writes"):
                _write(os.path.join(build, "Generated.swift"),
                       "struct Generated {}\n")
            state.setdefault("completed_phases", []).append(phase.key)
            state.setdefault("phase_outputs", {})[phase.key] = "built in clone"
            orch.save_state(project_dir, state)
            return "built in clone"

        cfg = {"root": self.root,
               "runtime": {"fetch_prompt_urls": False,
                           "fleet_ledger_enabled": False,
                           "docs_git_sync_enabled": False}}
        with mock.patch.object(orch, "process_phase", side_effect=phase_stub), \
                mock.patch.object(orch, "_release_gate_failure", return_value=None), \
                mock.patch.object(orch, "_run_timed_gate", return_value=None), \
                mock.patch.object(orch.docslib, "write_project_docs", return_value=[]), \
                mock.patch.object(orch.docslib, "write_project_archive", return_value=[]):
            orch.process_app(cfg, self.root, "adopted")
        self.assertTrue(orch.load_state(app_dir)["done"])
        self.assertTrue(os.path.isfile(os.path.join(build, "Generated.swift")))
        self.assertEqual(_tree_hash(self.origin), before_hash)
        self.assertEqual(os.stat(source_file).st_mtime_ns, before_mtime)
        self.assertFalse(os.path.exists(os.path.join(self.origin, "Generated.swift")))

    def test_non_git_origin_is_snapshot_committed_then_branched(self):
        app_dir = enroll.scaffold(self.root, self.origin, name="plain")["app_dir"]
        result = enroll.prepare_writable_clone(app_dir, self.origin, "plain")
        build = result["path"]
        self.assertEqual(_git(build, "branch", "--show-current"), "enroll/plain")
        subject = _git(build, "log", "-1", "--format=%s")
        self.assertIn("enrolled snapshot of %s at mtime-" %
                      os.path.realpath(self.origin), subject)
        self.assertEqual(_tree_hash(self.origin), _tree_hash_without_git(build))

    def test_dirty_git_origin_surfaces_head_divergence_warning(self):
        # Regression (A-52): the git clone materializes committed HEAD while
        # intake and the compliance audit read the working tree — uncommitted
        # origin state used to be dropped with no signal at either seam.
        self.init_git()
        _write(os.path.join(self.origin, "Sources", "App.swift"),
               "struct Edited {}\n")  # uncommitted WIP the audit would see
        scaffolded = enroll.scaffold(self.root, self.origin, name="dirty")
        self.assertTrue(any("uncommitted" in w for w in scaffolded["warnings"]),
                        scaffolded["warnings"])
        result = enroll.prepare_writable_clone(
            scaffolded["app_dir"], self.origin, "dirty")
        self.assertTrue(any("uncommitted" in w for w in result["warnings"]),
                        result["warnings"])
        # Warn, never refuse: enrolling WIP is legitimate, and the clone
        # still succeeds — from HEAD, exactly as the warning says.
        with open(os.path.join(result["path"], "Sources", "App.swift"),
                  encoding="utf-8") as fh:
            self.assertIn("Original", fh.read())

    def test_clean_git_origin_has_no_divergence_warning(self):
        self.init_git()
        scaffolded = enroll.scaffold(self.root, self.origin, name="clean")
        self.assertEqual(scaffolded["warnings"], [])
        result = enroll.prepare_writable_clone(
            scaffolded["app_dir"], self.origin, "clean")
        self.assertEqual(result["warnings"], [])

    def test_prepared_clone_is_idempotent_for_crash_safe_promotion_retry(self):
        app_dir = enroll.scaffold(self.root, self.origin, name="retry")["app_dir"]
        first = enroll.prepare_writable_clone(app_dir, self.origin, "retry")
        second = enroll.prepare_writable_clone(app_dir, self.origin, "retry")
        self.assertEqual(second["path"], first["path"])
        self.assertEqual(_git(second["path"], "branch", "--show-current"),
                         "enroll/retry")

    def test_existing_build_tree_is_never_overwritten(self):
        app_dir = enroll.scaffold(self.root, self.origin, name="existing")["app_dir"]
        sentinel = os.path.join(app_dir, "app_build", "KEEP")
        _write(sentinel, "keep")
        with self.assertRaisesRegex(enroll.EnrollError, "refusing to overwrite"):
            enroll.prepare_writable_clone(app_dir, self.origin, "existing")
        with open(sentinel, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "keep")

    def test_clone_refuses_symlink_that_would_write_back_into_origin(self):
        os.symlink(os.path.join(self.origin, "Sources"),
                   os.path.join(self.origin, "origin-link"))
        app_dir = enroll.scaffold(self.root, self.origin, name="linked")["app_dir"]
        with self.assertRaisesRegex(enroll.EnrollError, "symlink back"):
            enroll.prepare_writable_clone(app_dir, self.origin, "linked")
        self.assertFalse(os.path.lexists(os.path.join(app_dir, "app_build")))
        self.assertFalse(os.path.lexists(os.path.join(
            app_dir, ".app_build.enroll.tmp")))


def _tree_hash_without_git(root):
    digest = hashlib.sha256()
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(name for name in dirnames if name != ".git")
        for name in sorted(filenames):
            path = os.path.join(dirpath, name)
            digest.update(os.path.relpath(path, root).encode("utf-8"))
            if os.path.islink(path):
                digest.update(os.readlink(path).encode("utf-8"))
            else:
                with open(path, "rb") as fh:
                    digest.update(fh.read())
    return digest.hexdigest()


if __name__ == "__main__":
    unittest.main()
