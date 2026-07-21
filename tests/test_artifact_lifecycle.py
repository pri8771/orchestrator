"""V3 8.6 lifecycle: reclaim bodies without erasing provenance or dead ideas."""

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import unittest.mock

import artifacts
import orchestrator
import search


def _write_json(path, value):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(value, fh, indent=2, sort_keys=True)


def _tree_hash(root):
    digest = hashlib.sha256()
    for cur, dirs, files in os.walk(root):
        dirs.sort()
        for name in sorted(files):
            path = os.path.join(cur, name)
            digest.update(os.path.relpath(path, root).encode())
            with open(path, "rb") as fh:
                digest.update(fh.read())
    return digest.hexdigest()


class LifecycleTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="orch_lifecycle_")
        self.project = os.path.join(self.root, "demo")
        os.makedirs(self.project)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def seed_lineage(self, prefix="idea", count=6, head_status="final",
                     body_prefix="lifecycleword"):
        ids = []
        for version in range(1, count + 1):
            aid = "%s-%d" % (prefix, version)
            ids.append(aid)
            body = "%s-%d\n" % (body_prefix, version)
            adir = artifacts.artifact_dir(self.project, aid)
            os.makedirs(adir)
            with open(os.path.join(adir, "body.md"), "w", encoding="utf-8") as fh:
                fh.write(body)
            meta = {
                "id": aid, "type": "idea", "title": aid,
                "source": {"section": "ideas", "session": "s",
                           "phase": "brainstorm"},
                "version": version,
                "supersedes": ids[-2] if version > 1 else None,
                "lineage": ids[:-1], "branch": "", "depth": version - 1,
                "hop_count": 0, "content_hash": hashlib.sha256(
                    body.encode()).hexdigest(), "keywords": [body_prefix],
                "doc_slots": [],
                "status": head_status if version == count else "final",
                "status_history": [], "ts": "2026-01-01T00:00:00Z",
                "fields": {},
            }
            _write_json(os.path.join(adir, "meta.json"), meta)
        return ids

    def test_gc_dry_run_cli_is_zero_mutation_and_names_complete_diff(self):
        ids = self.seed_lineage()
        before = _tree_hash(self.root)
        proc = subprocess.run(
            [sys.executable, os.path.join(os.path.dirname(
                os.path.dirname(__file__)), "orchestrator.py"),
             "--root", self.root, "--gc"],
            capture_output=True, text=True, timeout=30)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(_tree_hash(self.root), before,
                         "dry-run must not create even workspace scratch")
        plan = artifacts.gc_plan(self.root, keep_versions=5)
        self.assertEqual([c["artifact_id"] for c in plan["candidates"]],
                         [ids[0]])
        self.assertIn(ids[0], proc.stdout)
        self.assertIn("superseded by", proc.stdout)
        self.assertRegex(proc.stdout, r"\d+ bytes")
        self.assertIn("DRY-RUN (no files changed)", proc.stdout)

    def test_apply_exact_plan_preserves_meta_lineage_and_latest_final(self):
        ids = self.seed_lineage()
        plan = artifacts.gc_plan(self.root, keep_versions=2)
        planned = [c["artifact_id"] for c in plan["candidates"]]
        self.assertEqual(planned, ids[:4])
        events = []
        done = artifacts.apply_gc_plan(plan, on_tombstone=events.append)
        self.assertEqual([d["artifact_id"] for d in done], planned)
        self.assertEqual([d["artifact_id"] for d in events], planned)
        idx = artifacts.lineage_index(self.project)
        self.assertEqual(artifacts.latest_final(
            self.project, ids[0], index=idx)["id"], ids[-1])
        self.assertEqual(artifacts.load_meta(
            self.project, ids[0])["lineage"], [])
        self.assertTrue(os.path.exists(os.path.join(
            artifacts.artifact_dir(self.project, ids[0]), "meta.json")))
        self.assertNotIn(ids[-1], planned,
                         "the current live final is unconditionally protected")
        self.assertEqual(artifacts.read_body(self.project, ids[-1]),
                         "lifecycleword-6\n")

    def test_tombstoned_resolution_is_typed_and_carries_surviving_meta(self):
        aid = self.seed_lineage(count=2)[0]
        result = artifacts.tombstone(self.project, aid)
        self.assertTrue(result["compacted"])
        with self.assertRaises(artifacts.TombstonedArtifactError) as caught:
            artifacts.resolve_artifact(self.project, aid)
        self.assertEqual(caught.exception.code, "tombstoned")
        self.assertEqual(caught.exception.meta["id"], aid)
        self.assertEqual(caught.exception.meta["lineage"], [])
        self.assertIn("provenance survives", str(caught.exception))

    def test_interrupted_compaction_is_logically_safe_and_rerun_repairs_once(self):
        aid = self.seed_lineage(count=2)[0]
        real = artifacts._rewrite_body_atomic
        with unittest.mock.patch.object(
                artifacts, "_rewrite_body_atomic",
                side_effect=OSError("simulated SIGKILL boundary")):
            first = artifacts.tombstone(self.project, aid)
        self.assertFalse(first["compacted"])
        self.assertIsNone(artifacts.read_body(self.project, aid),
                          "committed tombstone must never expose old body")
        repair = artifacts.gc_plan(self.root, keep_versions=1)
        self.assertEqual([c["artifact_id"] for c in repair["candidates"]],
                         [aid])
        with unittest.mock.patch.object(artifacts, "_rewrite_body_atomic",
                                        wraps=real):
            done = artifacts.apply_gc_plan(repair)
        self.assertEqual(len(done), 1)
        self.assertFalse(done[0]["transitioned"],
                         "a repair is not double-reported as a new tombstone")
        history = artifacts.load_meta(self.project, aid)["status_history"]
        self.assertEqual([h["status"] for h in history], ["tombstoned"])

    def test_unreconciled_branch_protects_shared_history(self):
        ids = self.seed_lineage(count=3)
        parent = ids[-1]
        for suffix in ("a", "b"):
            aid = "branch-" + suffix
            adir = artifacts.artifact_dir(self.project, aid)
            os.makedirs(adir)
            with open(os.path.join(adir, "body.md"), "w") as fh:
                fh.write(suffix)
            meta = dict(artifacts.load_meta(self.project, parent))
            meta.update({"id": aid, "version": 4, "supersedes": parent,
                         "lineage": ids, "branch": suffix,
                         "status": "final"})
            _write_json(os.path.join(adir, "meta.json"), meta)
        self.assertEqual(artifacts.gc_plan(
            self.root, keep_versions=1)["candidates"], [])

    def test_killed_lineage_is_absent_from_context_and_search(self):
        self.seed_lineage(prefix="dead", count=2, head_status="killed",
                          body_prefix="deadwombat")
        self.seed_lineage(prefix="live", count=2, body_prefix="livewombat")
        with open(os.path.join(self.project, "messages.jsonl"), "w") as fh:
            fh.write("")
        self.assertEqual(artifacts.retrieve(self.project, "deadwombat"), "")
        self.assertIn("livewombat", artifacts.retrieve(
            self.project, "livewombat"))
        search.reindex(self.root)
        self.assertEqual(search.query(self.root, "deadwombat")["hits"], [])
        live = search.query(self.root, "livewombat")["hits"]
        self.assertTrue(live)
        self.assertTrue(all(h["kind"] == "artifact" for h in live))

    def test_archive_refuses_live_nested_lock_then_round_trips(self):
        os.makedirs(os.path.join(self.project, "initial_prompt"))
        with open(os.path.join(self.project, "initial_prompt",
                               "initial_prompt.md"), "w") as fh:
            fh.write("prompt")
        lock_dir = os.path.join(self.root, ".orch-locks")
        os.makedirs(lock_dir)
        sid = "demo/ideas/chat"
        lock = os.path.join(lock_dir,
                            orchestrator.encode_lock_name(sid) + ".lock")
        with open(lock, "w") as fh:
            fh.write("pid=%d\n" % os.getpid())
        old_locks = orchestrator.LOCKS_DIR
        orchestrator.LOCKS_DIR = lock_dir
        try:
            with self.assertRaisesRegex(orchestrator.AppError,
                                        "live session lock.*demo/ideas/chat"):
                orchestrator.archive_project(self.root, "demo")
            os.remove(lock)
            dest = orchestrator.archive_project(self.root, "demo")
            self.assertEqual(dest, os.path.join(self.root, ".archive", "demo"))
            self.assertNotIn("demo", orchestrator.find_apps(self.root))
            self.assertFalse(os.path.exists(self.project))
            restored = orchestrator.archive_project(
                self.root, "demo", restore=True)
            self.assertEqual(restored, self.project)
            self.assertIn("demo", orchestrator.find_apps(self.root))
            self.assertTrue(os.path.exists(os.path.join(
                self.project, "initial_prompt", "initial_prompt.md")))
        finally:
            orchestrator.LOCKS_DIR = old_locks


if __name__ == "__main__":
    unittest.main()
