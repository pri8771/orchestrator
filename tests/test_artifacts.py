"""V3 board 4.1: the artifact store — atomic publish, flock'd minting,
seed-then-disk-wins type registry, corruption-tolerant readers.

The claims under test are the bus's foundation: an artifact is either
fully present or absent (never torn), concurrent publishers never share
an id, and every failure is reported through on_error — never raised,
never silent.
"""
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

import artifacts as artlib
import orchestrator as orch
import schemas as schemalib
import sections as seclib
import workflows as wf

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="artifacts-test-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.orch_dir = os.path.join(self.tmp, "orch")
        self.project = os.path.join(self.tmp, "proj")
        os.makedirs(self.project)
        self.errors = []
        self.registry = artlib.load_registry(
            self.orch_dir, on_error=self.errors.append)
        self.assertEqual(self.errors, [])

    def _publish(self, meta, body="the body\n"):
        return artlib.publish(self.project, body, meta, self.registry,
                              on_error=self.errors.append)

    def _entries(self):
        root = artlib.artifacts_root(self.project)
        if not os.path.isdir(root):
            return []
        return sorted(n for n in os.listdir(root) if not n.startswith("."))


# ---------------------------------------------------------------------------
# Registry: seed / disk-wins / corrupt-file discipline.
# ---------------------------------------------------------------------------
class TestRegistry(_Base):
    def test_seeds_on_first_load_with_all_board_types(self):
        path = os.path.join(self.orch_dir, "artifact_types.json")
        self.assertTrue(os.path.exists(path), "first load must seed")
        for t in ("idea", "research_brief", "opportunity_signal", "gap",
                  "reconcile", "finding_report", "spec_bundle"):
            self.assertIn(t, self.registry["types"])
        self.assertEqual(self.registry["types"]["reconcile"]["required"],
                         ["title", "body", "parents"])

    def test_disk_wins_over_seeds(self):
        path = os.path.join(self.orch_dir, "artifact_types.json")
        doc = {"schema_version": 1,
               "types": {"idea": {"required": ["title"],
                                  "default_status": "draft"}}}
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(doc, fh)
        errs = []
        reg = artlib.load_registry(self.orch_dir, on_error=errs.append)
        self.assertEqual(errs, [])
        self.assertEqual(reg, doc, "an edited file must win verbatim")

    def test_seed_never_clobbers_an_existing_file(self):
        path = os.path.join(self.orch_dir, "artifact_types.json")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("{broken")
        artlib.ensure_seeded_artifact_types(self.orch_dir)
        with open(path, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "{broken",
                             "even an invalid file is never overwritten")

    def test_corrupt_file_reports_once_and_returns_full_seeds(self):
        path = os.path.join(self.orch_dir, "artifact_types.json")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("{broken")
        errs = []
        reg = artlib.load_registry(self.orch_dir, on_error=errs.append)
        self.assertEqual(len(errs), 1, errs)
        self.assertIn("artifact_types.json", errs[0])
        self.assertEqual(set(reg["types"]), set(artlib.SEED_TYPES),
                         "all-or-default: the COMPLETE seed set")

    def test_mis_shaped_file_is_all_or_default(self):
        cases = [
            {"types": []},
            {"types": {"idea": {"required": "title"}}},
            {"types": {"idea": {"required": ["title"],
                                "default_status": "converged"}}},
            ["not", "an", "object"],
        ]
        path = os.path.join(self.orch_dir, "artifact_types.json")
        for doc in cases:
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(doc, fh)
            errs = []
            reg = artlib.load_registry(self.orch_dir, on_error=errs.append)
            self.assertEqual(len(errs), 1, "one banner for %r" % (doc,))
            self.assertEqual(set(reg["types"]), set(artlib.SEED_TYPES))

    def test_every_shipped_section_emitted_type_is_registered(self):
        # A shipped section prompting for a type the shipped registry
        # rejects would be broken by design.
        for name, raw in seclib._BUILTINS.items():
            for t in raw.get("artifact_types_emitted", []):
                self.assertIn(t, artlib.SEED_TYPES,
                              "section %r emits unregistered %r" % (name, t))


# ---------------------------------------------------------------------------
# Minting.
# ---------------------------------------------------------------------------
class TestMint(_Base):
    def test_slug_is_lowercase_ascii(self):
        self.assertEqual(
            artlib.mint_id(self.project, "idea", "Dark Mode — Toggle!"),
            "dark-mode-toggle")
        # NFD input coalesces with NFC before the ASCII whitelist strips
        # the accent — no dependence on the caller's normalization form.
        nfc = artlib.mint_id(self.project, "idea", "Café Finder")
        nfd = artlib.mint_id(self.project, "idea", "Café Finder")
        self.assertEqual(nfc, nfd)
        self.assertEqual(nfc, "caf-finder")

    def test_hostile_titles_fall_back_to_the_type(self):
        self.assertEqual(artlib.mint_id(self.project, "idea", "!!!"), "idea")
        self.assertEqual(artlib.mint_id(self.project, "idea", "完全"),
                         "idea")
        self.assertEqual(artlib.mint_id(self.project, "idea", ""), "idea")

    def test_truncation_never_ends_in_hyphen(self):
        minted = artlib.mint_id(self.project, "idea",
                                "a" * 59 + " and more words here")
        self.assertLessEqual(len(minted), 60)
        self.assertFalse(minted.endswith("-"))

    def test_collision_bumps_deterministically(self):
        for name in ("plan", "plan-2"):
            os.makedirs(artlib.artifact_dir(self.project, name))
        self.assertEqual(artlib.mint_id(self.project, "idea", "Plan"),
                         "plan-3")

    def test_minted_ids_never_start_with_a_dot(self):
        self.assertFalse(
            artlib.mint_id(self.project, "idea", ".hidden").startswith("."))


# ---------------------------------------------------------------------------
# Publish.
# ---------------------------------------------------------------------------
class TestPublish(_Base):
    def test_round_trip_with_the_full_day_one_schema(self):
        body = "# Dark mode\n\nEveryone wants it.\n"
        aid = self._publish({"type": "idea", "title": "Dark Mode Toggle",
                             "source": {"section": "ideas",
                                        "session": "chat-01",
                                        "phase": "brainstorm",
                                        "turn": "brainstorm:final:x:contract"},
                             "keywords": ["ui", "theme"]},
                            body=body)
        self.assertEqual(self.errors, [])
        self.assertEqual(aid, "dark-mode-toggle")
        adir = artlib.artifact_dir(self.project, aid)
        self.assertEqual(sorted(os.listdir(adir)), ["body.md", "meta.json"],
                         "exactly body.md and meta.json")
        self.assertEqual(artlib.read_body(self.project, aid), body)
        meta = artlib.load_meta(self.project, aid)
        for key in ("schema_version", "id", "type", "title", "source",
                    "version", "supersedes", "lineage", "content_hash",
                    "keywords", "doc_slots", "status", "ts", "fields"):
            self.assertIn(key, meta, "day-one field %r missing" % key)
        self.assertEqual(meta["id"], aid)
        self.assertEqual(meta["version"], 1)
        self.assertIsNone(meta["supersedes"])
        self.assertEqual(meta["lineage"], [])
        self.assertEqual(meta["content_hash"],
                         hashlib.sha256(body.encode("utf-8")).hexdigest())
        self.assertEqual(meta["status"], "published")
        self.assertEqual(meta["source"]["session"], "chat-01")
        self.assertEqual(meta["keywords"], ["ui", "theme"])
        self.assertEqual(meta["doc_slots"], [])

    def test_missing_required_field_rejects_with_nothing_on_disk(self):
        aid = self._publish({"type": "research_brief", "title": "No sources"})
        self.assertIsNone(aid)
        self.assertEqual(len(self.errors), 1)
        self.assertIn("sources", self.errors[0])
        self.assertFalse(
            os.path.exists(artlib.artifacts_root(self.project)),
            "validation precedes ANY disk touch")

    def test_unknown_type_rejected(self):
        self.assertIsNone(self._publish({"type": "sonnet", "title": "x"}))
        self.assertIn("unknown artifact type", self.errors[0])

    def test_blank_body_fails_a_body_requiring_type(self):
        self.assertIsNone(self._publish({"type": "idea", "title": "x"},
                                        body="   \n"))
        self.assertIn("body", self.errors[0])

    def test_required_payload_key_may_live_under_fields(self):
        aid = self._publish({"type": "gap", "title": "Missing tests",
                             "fields": {"impact": "high"}})
        self.assertEqual(self.errors, [])
        self.assertIsNotNone(aid)
        self.assertEqual(artlib.load_meta(self.project, aid)
                         ["fields"]["impact"], "high")

    def test_engine_owned_fields_cannot_be_smuggled(self):
        aid = self._publish({"type": "idea", "title": "Sneaky",
                             "id": "evil", "version": 9,
                             "lineage": ["fake"], "content_hash": "0",
                             "extra_note": "kept"})
        meta = artlib.load_meta(self.project, aid)
        self.assertEqual(meta["id"], "sneaky")
        self.assertEqual(meta["version"], 1)
        self.assertEqual(meta["lineage"], [])
        self.assertNotEqual(meta["content_hash"], "0")
        # ... but nothing is silently discarded (§6.2):
        self.assertEqual(meta["fields"]["id"], "evil")
        self.assertEqual(meta["fields"]["extra_note"], "kept")

    def test_status_honors_draft_and_rejects_nonsense(self):
        aid = self._publish({"type": "idea", "title": "d", "status": "draft"})
        self.assertEqual(artlib.load_meta(self.project, aid)["status"],
                         "draft")
        self.assertEqual(self.errors, [])
        aid2 = self._publish({"type": "idea", "title": "c",
                              "status": "converged"})
        self.assertEqual(artlib.load_meta(self.project, aid2)["status"],
                         "published", "publishers may not mint 4.3 statuses")
        self.assertEqual(len(self.errors), 1)

    def test_same_title_republish_mints_a_new_id(self):
        a = self._publish({"type": "idea", "title": "Twin"}, body="one\n")
        b = self._publish({"type": "idea", "title": "Twin"}, body="two\n")
        self.assertEqual((a, b), ("twin", "twin-2"))
        self.assertEqual(artlib.read_body(self.project, "twin"), "one\n")
        self.assertEqual(artlib.read_body(self.project, "twin-2"), "two\n")

    def test_source_defaults_to_empty_strings_never_absent(self):
        aid = self._publish({"type": "idea", "title": "bare"})
        self.assertEqual(artlib.load_meta(self.project, aid)["source"],
                         {"section": "", "session": "", "phase": "",
                          "turn": ""})

    def test_unserializable_meta_fails_cleanly(self):
        aid = self._publish({"type": "idea", "title": "bad",
                             "gadget": object()})
        self.assertIsNone(aid)
        self.assertEqual(len(self.errors), 1)
        self.assertEqual(self._entries(), [], "no partial artifact")


# ---------------------------------------------------------------------------
# Atomicity: half-writes invisible, orphans reaped, readers blind to dots.
# ---------------------------------------------------------------------------
class TestAtomicity(_Base):
    def test_orphan_tmp_dir_is_invisible_and_swept(self):
        # The orphan carries a fully VALID meta.json — only the dot-skip
        # rule keeps it out of listings, not corruption tolerance.
        root = artlib.artifacts_root(self.project)
        orphan = os.path.join(root, ".tmp-ghost.999")
        os.makedirs(orphan)
        with open(os.path.join(orphan, "body.md"), "w") as fh:
            fh.write("half-written\n")
        with open(os.path.join(orphan, "meta.json"), "w") as fh:
            json.dump({"id": ".tmp-ghost.999", "type": "idea",
                       "title": "ghost"}, fh)
        errs = []
        self.assertEqual(artlib.list_artifacts(self.project,
                                               on_error=errs.append), [],
                         "a half-published artifact must never be listed")
        self.assertEqual(errs, [], "invisible means silent, not reported")
        self._publish({"type": "idea", "title": "fresh"})
        self.assertFalse(os.path.exists(orphan),
                         "the next publish sweeps crash orphans")

    def test_crash_before_rename_leaves_nothing_visible(self):
        real_rename = os.rename
        def boom(src, dst):
            if os.path.basename(dst) in ("crashy",):
                raise OSError("simulated crash at the rename window")
            return real_rename(src, dst)
        with mock.patch("os.rename", side_effect=boom):
            aid = self._publish({"type": "idea", "title": "Crashy"})
        self.assertIsNone(aid)
        self.assertEqual(len(self.errors), 1)
        self.assertEqual(self._entries(), [])
        self.assertEqual(artlib.list_artifacts(self.project), [])
        root = artlib.artifacts_root(self.project)
        self.assertEqual([n for n in os.listdir(root)
                          if n.startswith(".tmp-")], [],
                         "a failed publish cleans its own tmp dir")
        # §12.4 retry must be safe: the retried publish lands cleanly
        # under the ORIGINAL id (no half-duplicate, no -2 bump).
        self.errors.clear()
        self.assertEqual(self._publish({"type": "idea", "title": "Crashy"}),
                         "crashy")

    def test_lock_file_and_ds_store_are_invisible(self):
        self._publish({"type": "idea", "title": "real"})
        root = artlib.artifacts_root(self.project)
        with open(os.path.join(root, ".DS_Store"), "wb") as fh:
            fh.write(b"\x00")
        listed = artlib.list_artifacts(self.project,
                                       on_error=self.errors.append)
        self.assertEqual([m["id"] for m in listed], ["real"])
        self.assertEqual(self.errors, [])


# ---------------------------------------------------------------------------
# Concurrency: distinct ids, intact artifacts — threads and processes.
# ---------------------------------------------------------------------------
class TestConcurrency(_Base):
    def test_racing_threads_mint_distinct_ids(self):
        n = 8
        barrier = threading.Barrier(n)
        results, errs = [], []
        def worker(i):
            barrier.wait()
            results.append(artlib.publish(
                self.project, "body %d\n" % i,
                {"type": "idea", "title": "Race"},
                self.registry, on_error=errs.append))
        threads = [threading.Thread(target=worker, args=(i,))
                   for i in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errs, [])
        self.assertEqual(len(set(results)), n, results)
        self.assertNotIn(None, results)
        listed = artlib.list_artifacts(self.project,
                                       on_error=errs.append)
        self.assertEqual(len(listed), n)
        self.assertEqual(errs, [], "every racer left a complete artifact")

    def test_racing_processes_mint_distinct_ids(self):
        # The claim flock actually exists for: two orchestrator PROCESSES
        # sharing one project.
        script = (
            "import sys; sys.path.insert(0, %r)\n"
            "import artifacts\n"
            "reg = artifacts.load_registry(%r)\n"
            "for i in range(5):\n"
            "    aid = artifacts.publish(%r, 'from %%s\\n' %% sys.argv[1],\n"
            "        {'type': 'idea', 'title': 'Shared Title'}, reg)\n"
            "    print(aid)\n" % (HERE, self.orch_dir, self.project))
        procs = [subprocess.Popen(
                    [sys.executable, "-c", script, name],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    text=True)
                 for name in ("alpha", "beta")]
        ids = []
        for p in procs:
            out, err = p.communicate(timeout=60)
            self.assertEqual(p.returncode, 0, err)
            ids.extend(out.split())
        self.assertEqual(len(ids), 10)
        self.assertEqual(len(set(ids)), 10, ids)
        errs = []
        listed = artlib.list_artifacts(self.project, on_error=errs.append)
        self.assertEqual(errs, [])
        self.assertEqual(sorted(m["id"] for m in listed), sorted(ids))


# ---------------------------------------------------------------------------
# Read tolerance.
# ---------------------------------------------------------------------------
class TestReadTolerance(_Base):
    def test_one_corrupt_meta_never_blinds_the_store(self):
        for title in ("alpha", "beta", "gamma"):
            self._publish({"type": "idea", "title": title})
        with open(os.path.join(artlib.artifact_dir(self.project, "beta"),
                               "meta.json"), "w") as fh:
            fh.write("{torn")
        errs = []
        listed = artlib.list_artifacts(self.project, on_error=errs.append)
        self.assertEqual([m["id"] for m in listed], ["alpha", "gamma"])
        self.assertEqual(len(errs), 1)
        self.assertIn("beta", errs[0])

    def test_hand_edited_meta_id_loses_to_the_directory(self):
        aid = self._publish({"type": "idea", "title": "honest"})
        path = os.path.join(artlib.artifact_dir(self.project, aid),
                            "meta.json")
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
        doc["id"] = "imposter"
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(doc, fh)
        errs = []
        listed = artlib.list_artifacts(self.project, on_error=errs.append)
        self.assertEqual([m["id"] for m in listed], ["honest"])
        self.assertEqual(len(errs), 1)

    def test_readers_return_none_for_absent_ids(self):
        errs = []
        self.assertIsNone(artlib.load_meta(self.project, "nope",
                                           on_error=errs.append))
        self.assertIsNone(artlib.read_body(self.project, "nope",
                                           on_error=errs.append))
        self.assertEqual(len(errs), 2)

    def test_type_and_status_filters(self):
        self._publish({"type": "idea", "title": "a"})
        self._publish({"type": "idea", "title": "b", "status": "draft"})
        self._publish({"type": "gap", "title": "g",
                       "fields": {"impact": "high"}})
        self.assertEqual(
            [m["id"] for m in artlib.list_artifacts(self.project,
                                                    type="idea")],
            ["a", "b"])
        self.assertEqual(
            [m["id"] for m in artlib.list_artifacts(self.project,
                                                    type="idea",
                                                    status="draft")],
            ["b"])


# ---------------------------------------------------------------------------
# V3 board 4.2: publication — fenced artifact-json extraction, the last
# phase-close hook, snippet gating on declared emitted types.
# ---------------------------------------------------------------------------
def _block(obj):
    return "```artifact-json\n%s\n```" % json.dumps(obj)


class TestPublishFromOutput(_Base):
    def test_fence_is_single_sourced_with_the_contract_data(self):
        # artifacts.py may import only schemas, so the pin is a TEST.
        self.assertEqual(seclib.contract_fence("artifact"),
                         (artlib.FENCE_TAG, "artifact_block"))
        self.assertEqual(schemalib.REQUIRED_FIELDS["artifact_block"],
                         list(artlib.BLOCK_REQUIRED))

    def test_valid_block_publishes_with_engine_provenance(self):
        out = "Great debate.\n%s\nWrap-up prose.\n" % _block(
            {"type": "idea", "title": "Night Mode", "body": "# Night\n",
             "keywords": ["ui"]})
        src = {"section": "ideas", "session": "chat-1",
               "phase": "brainstorm", "turn": "brainstorm:final:x:contract"}
        ids = artlib.publish_from_output(self.project, out, src,
                                         self.registry,
                                         on_error=self.errors.append)
        self.assertEqual(self.errors, [])
        self.assertEqual(ids, ["night-mode"])
        meta = artlib.load_meta(self.project, "night-mode")
        self.assertEqual(meta["source"], src)
        self.assertEqual(artlib.read_body(self.project, "night-mode"),
                         "# Night\n")

    def test_multiple_blocks_publish_in_document_order(self):
        out = "%s\nmiddle\n%s" % (
            _block({"type": "idea", "title": "One", "body": "1"}),
            _block({"type": "gap", "title": "Two", "body": "2",
                    "impact": "high"}))
        ids = artlib.publish_from_output(self.project, out, {},
                                         self.registry,
                                         on_error=self.errors.append)
        self.assertEqual(ids, ["one", "two"])
        self.assertEqual(self.errors, [])

    def test_malformed_siblings_are_skipped_not_fatal(self):
        out = "\n".join([
            "```artifact-json\n{broken\n```",
            _block({"type": "idea", "title": "Survivor", "body": "ok"}),
            _block({"type": "sonnet", "title": "Unknown", "body": "x"}),
            _block({"type": "idea", "title": "No body at all"}),
            _block({"type": "idea", "title": "Bad body", "body": ["l"]}),
        ])
        ids = artlib.publish_from_output(self.project, out, {},
                                         self.registry,
                                         on_error=self.errors.append)
        self.assertEqual(ids, ["survivor"],
                         "one good block among four bad ones publishes")
        self.assertEqual(len(self.errors), 4, self.errors)

    def test_no_blocks_publishes_nothing(self):
        self.assertEqual(
            artlib.publish_from_output(self.project, "plain prose", {},
                                       self.registry,
                                       on_error=self.errors.append),
            [])
        self.assertEqual(self.errors, [])
        self.assertFalse(os.path.exists(artlib.artifacts_root(self.project)))


class TestSnippetGating(unittest.TestCase):
    def _contract(self, cfg):
        return orch._phase_contract(
            cfg, wf.Phase("brainstorm", ".", "x.md", "p", rounds=2))

    def test_flat_runs_are_byte_untouched(self):
        for cfg in ({"_workflow_target": "app"}, {},
                    {"_workflow_target": "research"}):
            self.assertNotIn("artifact-json", self._contract(cfg))

    def test_a_phase_literally_keyed_at_artifact_cannot_leak_the_snippet(self):
        # "@" keys are engine-gated pseudo-entries: a user workflow phase
        # named "@artifact" must not direct-match one (it would bypass
        # the gate and leak an unsubstituted __TYPES__ placeholder).
        text = orch._phase_contract(
            {"_workflow_target": "app"},
            wf.Phase("@artifact", ".", "x.md", "p", rounds=2))
        self.assertNotIn("artifact-json", text)
        self.assertNotIn("__TYPES__", text)

    def test_declaring_section_gets_the_snippet_with_its_types(self):
        root = tempfile.mkdtemp(prefix="gate-root-")
        self.addCleanup(shutil.rmtree, root, True)
        cfg = {"root": root,
               "_app_dir": os.path.join(root, "proj", "research", "chat-1"),
               "_workflow_target": "research"}
        text = self._contract(cfg)
        self.assertIn("artifact-json", text)
        self.assertIn("one of: research_brief, opportunity_signal", text,
                      "__TYPES__ must be substituted from the manifest")
        self.assertNotIn("__TYPES__", text)
        # The snippet sits before the always-appended phase summary.
        self.assertLess(text.index("artifact-json"),
                        text.index("phase-summary-json"))


class TestPublicationHook(_Base):
    def _run_hook(self, cfg, app_dir, final_output, key="brainstorm"):
        return orch._hook_artifact_publish(
            cfg, "proj", app_dir, wf.Phase(key, ".", "x.md", "p"),
            {}, key=key, md_path=os.path.join(app_dir, "x.md"),
            transcript="t", final_output=final_output, coord=None,
            active=[], is_build=False, is_verify_repair=False,
            allow_writes=False, _needs_vlabel=False)

    def _nested(self):
        root = os.path.join(self.tmp, "root")
        app_dir = os.path.join(root, "proj", "ideas", "chat-1")
        os.makedirs(app_dir)
        return root, app_dir

    def test_end_to_end_publish_with_event(self):
        root, app_dir = self._nested()
        with open(os.path.join(app_dir, "messages.jsonl"), "w") as fh:
            fh.write(json.dumps({"turn_id": "brainstorm:final:a:contract"})
                     + "\n")
        out = "Wrap-up.\n%s" % _block(
            {"type": "idea", "title": "Hooked", "body": "# H\n"})
        cfg = {"root": root, "_app_dir": app_dir}
        t, f = self._run_hook(cfg, app_dir, out)
        self.assertEqual((t, f), ("t", out), "the hook is a pure reader")
        project_dir = os.path.join(root, "proj")
        meta = artlib.load_meta(project_dir, "hooked")
        self.assertIsNotNone(meta, "artifact must land in the PROJECT store")
        self.assertEqual(meta["source"],
                         {"section": "ideas", "session": "chat-1",
                          "phase": "brainstorm",
                          "turn": "brainstorm:final:a:contract"})
        with open(os.path.join(app_dir, "events.jsonl"),
                  encoding="utf-8") as fh:
            lines = [json.loads(l) for l in fh if l.strip()]
        pub = [e for e in lines if e.get("kind") == "artifact_published"]
        self.assertEqual(len(pub), 1)
        self.assertEqual(pub[0]["artifact_id"], "hooked")
        self.assertEqual(pub[0]["path"], "artifacts/hooked")
        self.assertEqual(pub[0]["type"], "idea")
        self.assertNotIn("# H", json.dumps(pub[0]),
                         "events carry ids+paths, never body content")
        self.assertLess(len(json.dumps(pub[0])), 3500)

    def test_flat_runs_and_blockless_outputs_are_no_ops(self):
        root, app_dir = self._nested()
        flat_dir = os.path.join(root, "flatapp")
        os.makedirs(flat_dir)
        self._run_hook({"root": root, "_app_dir": flat_dir}, flat_dir,
                       "%s" % _block({"type": "idea", "title": "F",
                                      "body": "x"}))
        self._run_hook({"root": root, "_app_dir": app_dir}, app_dir,
                       "no artifacts here")
        self.assertFalse(
            os.path.exists(os.path.join(root, "proj", "artifacts")))
        self.assertFalse(os.path.exists(os.path.join(root, "artifacts")))

    def test_malformed_block_warns_but_never_fails_the_phase(self):
        root, app_dir = self._nested()
        out = "```artifact-json\n{torn\n```"
        t, f = self._run_hook({"root": root, "_app_dir": app_dir},
                              app_dir, out)
        self.assertEqual((t, f), ("t", out),
                         "a bad block must not raise out of the hook")

    def test_re_close_does_not_duplicate_artifacts(self):
        # Crash-resume re-runs the phase close AFTER a durable publish;
        # identical blocks must dedupe, a changed body must republish.
        root, app_dir = self._nested()
        cfg = {"root": root, "_app_dir": app_dir}
        out = _block({"type": "idea", "title": "Once", "body": "same\n"})
        self._run_hook(cfg, app_dir, out)
        self._run_hook(cfg, app_dir, out)
        project_dir = os.path.join(root, "proj")
        self.assertEqual(
            [m["id"] for m in artlib.list_artifacts(project_dir)],
            ["once"], "a re-close must not mint a -2 duplicate")
        with open(os.path.join(app_dir, "events.jsonl"),
                  encoding="utf-8") as fh:
            pubs = [l for l in fh if '"artifact_published"' in l]
        self.assertEqual(len(pubs), 1, "one event per DISTINCT artifact")
        # A genuinely new body is not a resume — it publishes.
        self._run_hook(cfg, app_dir, _block(
            {"type": "idea", "title": "Once", "body": "changed\n"}))
        self.assertEqual(
            [m["id"] for m in artlib.list_artifacts(project_dir)],
            ["once", "once-2"])

    def test_corrupt_custom_section_fails_closed(self):
        # A parse error must never flip a declared-closed publication
        # gate to open: the unknown-name fallback clone carries NO
        # artifact types (the shipped builtins keep their own).
        sec = seclib.load_section("no-such-custom-section", HERE)
        self.assertEqual(sec.artifact_types_emitted, [])
        self.assertEqual(sec.artifact_types_accepted, [])
        root = os.path.join(self.tmp, "gate")
        cfg = {"root": root,
               "_app_dir": os.path.join(root, "p", "no-such-custom-section",
                                        "c")}
        self.assertEqual(orch._artifact_types_emitted(
            cfg, "no-such-custom-section"), [])

    def test_transcript_drafts_are_never_published(self):
        # Artifacts have no merge key: scanning the transcript would
        # republish every superseded draft from earlier rounds.
        root, app_dir = self._nested()
        cfg = {"root": root, "_app_dir": app_dir}
        draft = _block({"type": "idea", "title": "Draft", "body": "old"})
        final = "final prose %s" % _block(
            {"type": "idea", "title": "Kept", "body": "new"})
        result = orch._hook_artifact_publish(
            cfg, "proj", app_dir, wf.Phase("brainstorm", ".", "x.md", "p"),
            {}, key="brainstorm", md_path=os.path.join(app_dir, "x.md"),
            transcript=draft, final_output=final, coord=None,
            active=[], is_build=False, is_verify_repair=False,
            allow_writes=False, _needs_vlabel=False)
        self.assertEqual(result, (draft, final))
        project_dir = os.path.join(root, "proj")
        self.assertEqual(
            [m["id"] for m in artlib.list_artifacts(project_dir)],
            ["kept"], "the transcript draft must NOT be materialized")


# ---------------------------------------------------------------------------
# Regressions from the pre-commit adversarial verification pass — each test
# pins an execution-confirmed defect in the first draft of this module.
# ---------------------------------------------------------------------------
class TestVerificationFindings(_Base):
    def test_post_rename_fsync_failure_is_success_with_warning(self):
        # A failure AFTER the rename must not claim "nothing was stored"
        # about an artifact readers can already see (R2), or a retry
        # would mint a -2 duplicate (§12.4).
        root = artlib.artifacts_root(self.project)
        real_open = os.open
        def open_spy(path, flags, *args, **kwargs):
            if path == root and flags == os.O_RDONLY:
                raise OSError(24, "Too many open files (simulated)")
            return real_open(path, flags, *args, **kwargs)
        with mock.patch("os.open", side_effect=open_spy):
            aid = self._publish({"type": "idea", "title": "Windowed"})
        self.assertEqual(aid, "windowed",
                         "the artifact IS live — publish must say so")
        self.assertEqual(len(self.errors), 1)
        self.assertIn("live", self.errors[0])
        self.assertNotIn("nothing was stored", self.errors[0])
        self.assertEqual(
            [m["id"] for m in artlib.list_artifacts(self.project)],
            ["windowed"])

    def test_tmp_dir_entries_fsynced_before_rename(self):
        # The dirents naming body.md/meta.json live in the tmp dir's own
        # data; skipping its fsync lets a power cut surface a torn
        # artifacts/<id>/ — the exact state the module rules out.
        events = []
        real_open, real_rename = os.open, os.rename
        def open_spy(path, flags, *args, **kwargs):
            if (isinstance(path, str) and "/.tmp-" in path
                    and flags == os.O_RDONLY):
                events.append("fsync-tmp-dir")
            return real_open(path, flags, *args, **kwargs)
        def rename_spy(src, dst):
            events.append("rename")
            return real_rename(src, dst)
        with mock.patch("os.open", side_effect=open_spy), \
             mock.patch("os.rename", side_effect=rename_spy):
            self.assertEqual(self._publish({"type": "idea",
                                            "title": "Durable"}),
                             "durable")
        self.assertIn("fsync-tmp-dir", events)
        self.assertLess(events.index("fsync-tmp-dir"),
                        events.index("rename"),
                        "tmp dir must be durable BEFORE it becomes visible")

    def test_flock_failure_reports_never_raises(self):
        with mock.patch("fcntl.flock",
                        side_effect=OSError(77, "No locks available")):
            aid = self._publish({"type": "idea", "title": "Unlockable"})
        self.assertIsNone(aid)
        self.assertEqual(len(self.errors), 1)
        self.assertIn("cannot lock", self.errors[0])
        self.assertEqual(self._entries(), [])

    def test_concurrent_first_seed_leaves_a_valid_registry(self):
        fresh = os.path.join(self.tmp, "fresh-orch")
        barrier = threading.Barrier(6)
        def worker():
            barrier.wait()
            artlib.ensure_seeded_artifact_types(fresh)
        threads = [threading.Thread(target=worker) for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        with open(os.path.join(fresh, "artifact_types.json"),
                  encoding="utf-8") as fh:
            self.assertEqual(set(json.load(fh)["types"]),
                             set(artlib.SEED_TYPES))
        self.assertEqual([n for n in os.listdir(fresh) if ".tmp" in n], [],
                         "no torn or leftover seed tmp files")

    def test_non_string_type_reported_not_raised(self):
        for hostile in (["idea"], {}, 7, None):
            self.errors.clear()
            self.assertIsNone(self._publish({"type": hostile, "title": "x"}))
            self.assertEqual(len(self.errors), 1, repr(hostile))
            self.assertIn("unknown artifact type", self.errors[0])

    def test_traversal_ids_cannot_escape_the_store(self):
        outside = os.path.join(self.tmp, "outside")
        os.makedirs(outside)
        with open(os.path.join(outside, "meta.json"), "w") as fh:
            json.dump({"id": "outside", "type": "idea"}, fh)
        with open(os.path.join(outside, "body.md"), "w") as fh:
            fh.write("secret")
        for hostile in ("../../outside", "/etc", "", "UPPER", ".hidden",
                        "a/b", "a..b"):
            self.errors.clear()
            self.assertIsNone(
                artlib.load_meta(self.project, hostile,
                                 on_error=self.errors.append), hostile)
            self.assertIsNone(
                artlib.read_body(self.project, hostile,
                                 on_error=self.errors.append), hostile)
            self.assertEqual(len(self.errors), 2, hostile)
            self.assertIn("invalid id", self.errors[0])

    def test_malformed_optional_shapes_reported_and_preserved(self):
        aid = self._publish({"type": "idea", "title": "Shapes",
                             "keywords": "ui, theme",
                             "source": "ideas",
                             "fields": [1, 2]})
        self.assertIsNotNone(aid)
        self.assertEqual(len(self.errors), 3,
                         "each wrong-shaped key reports once: %s"
                         % self.errors)
        meta = artlib.load_meta(self.project, aid)
        self.assertEqual(meta["keywords"], [])
        self.assertEqual(meta["source"],
                         {"section": "", "session": "", "phase": "",
                          "turn": ""})
        # ... and nothing vanished (§6.2):
        self.assertEqual(meta["fields"]["keywords"], "ui, theme")
        self.assertEqual(meta["fields"]["source"], "ideas")
        self.assertEqual(meta["fields"]["fields"], [1, 2])

    def test_surrogate_body_rejected_before_any_disk_touch(self):
        aid = self._publish({"type": "idea", "title": "Bad"},
                            body="lone surrogate \ud800 here")
        self.assertIsNone(aid)
        self.assertEqual(len(self.errors), 1)
        self.assertFalse(os.path.exists(artlib.artifacts_root(self.project)))

    def test_crlf_body_round_trips_and_rehashes_exactly(self):
        body = "line one\r\nline two\rline three\n"
        aid = self._publish({"type": "idea", "title": "CRLF"}, body=body)
        got = artlib.read_body(self.project, aid)
        self.assertEqual(got, body, "no universal-newline mangling")
        self.assertEqual(
            hashlib.sha256(got.encode("utf-8")).hexdigest(),
            artlib.load_meta(self.project, aid)["content_hash"],
            "re-hash of the read body must match content_hash (4.3's "
            "convergence check depends on it)")


if __name__ == "__main__":
    unittest.main()
