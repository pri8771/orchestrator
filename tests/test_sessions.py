"""V3 board 4.4: the delegation machinery — mint a nested chat for a
delegated request, spawn a detached engine run, and deliver the resulting
artifact back to the originating session's inbox exactly once.

The load-bearing claims: idempotent re-mint (never a duplicate dir), no
secret material on the spawn argv, and exactly-once reply delivery across
the crash-between-publish-and-append window.
"""
import json
import os
import shutil
import tempfile
import threading
import unittest
import unittest.mock

import orchestrator as orch
import sessions as seslib


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="sessions-test-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.root = os.path.join(self.tmp, "workspace")
        os.makedirs(self.root)
        self.errors = []

    def _meta(self, title="Deep dive on caching", **extra):
        m = {"title": title, "prompt": "Research caching strategies.",
             "origin_session": "gloam/ideas/chat-1"}
        m.update(extra)
        return m

    def _origin(self, sid="gloam/ideas/chat-1"):
        # A real originating session is itself minted via create_session,
        # which stamps the sections marker on the project dir — without it
        # the nested-conflict guard would (correctly) block a sibling
        # section from minting under an "unmarked occupied" project.
        return orch.create_session(self.root, sid, "origin prompt")

    def _mint(self, section="research", reply_to=None, meta=None,
              created_at="2026-07-18T00:00:00+00:00"):
        return seslib.mint_delegation_session(
            self.root, "gloam", section, meta or self._meta(),
            reply_to=reply_to, create_session=orch.create_session,
            created_at=created_at, on_error=self.errors.append)


# ---------------------------------------------------------------------------
# Mint + delegation.json.
# ---------------------------------------------------------------------------
class TestMint(_Base):
    def test_mints_a_nested_session_with_delegation_record(self):
        d = self._mint()
        self.assertEqual(self.errors, [])
        self.assertTrue(os.path.isdir(d))
        # Nested under gloam/research/<slug>, discoverable (marker minted).
        rel = os.path.relpath(d, self.root).split(os.sep)
        self.assertEqual(rel[:2], ["gloam", "research"])
        self.assertTrue(os.path.exists(
            os.path.join(self.root, "gloam", orch.SECTIONS_MARKER)))
        self.assertTrue(os.path.exists(os.path.join(
            d, "initial_prompt", "initial_prompt.md")))
        rec = seslib.read_delegation(d)
        self.assertEqual(rec["status"], "pending")
        self.assertEqual(rec["schema_version"], schemas_version())
        self.assertEqual(rec["created_at"], "2026-07-18T00:00:00+00:00")
        self.assertEqual(rec["delivered"], [])
        self.assertEqual(len(rec["delegation_id"]), 12)

    def test_re_mint_same_request_returns_same_dir_no_duplicate(self):
        d1 = self._mint()
        before = sorted(os.listdir(os.path.join(self.root, "gloam",
                                                "research")))
        d2 = self._mint()
        self.assertEqual(d1, d2)
        self.assertEqual(self.errors, [])
        self.assertEqual(
            sorted(os.listdir(os.path.join(self.root, "gloam", "research"))),
            before, "an idempotent re-mint must not create a second dir")

    def test_distinct_requests_get_distinct_dirs(self):
        d1 = self._mint(meta=self._meta(title="One"))
        d2 = self._mint(meta=self._meta(title="Two"))
        self.assertNotEqual(d1, d2)

    def test_crashed_mint_without_record_is_resumed(self):
        d = self._mint()
        os.remove(seslib.delegation_path(d))   # simulate crash before write
        again = self._mint()
        self.assertEqual(again, d)
        self.assertIsNotNone(seslib.read_delegation(d),
                             "a dir without delegation.json is resumed")

    def test_minted_dir_never_equals_reply_to(self):
        # The self-reply loop is structurally impossible — the slug hashes
        # reply_to, so the delegation always targets a DIFFERENT session.
        origin = self._origin()
        d = self._mint(reply_to=origin)
        self.assertNotEqual(os.path.abspath(d), os.path.abspath(origin))

    def test_non_dict_request_meta_refused(self):
        out = seslib.mint_delegation_session(
            self.root, "gloam", "research", "not a dict",
            create_session=orch.create_session, on_error=self.errors.append)
        self.assertIsNone(out)

    def test_non_serializable_request_meta_does_not_poison(self):
        # A request_meta carrying a non-JSON value must not raise out of
        # mint or leave a dir without delegation.json.
        meta = self._meta()
        meta["gadget"] = object()
        d = self._mint(meta=meta)
        self.assertIsNotNone(d, self.errors)
        self.assertIsNotNone(seslib.read_delegation(d),
                             "delegation.json must exist (default=str)")

    def test_delegation_json_atomic_write_and_corrupt_tolerance(self):
        d = self._mint()
        with open(seslib.delegation_path(d), "w") as fh:
            fh.write("{corrupt")
        errs = []
        self.assertIsNone(seslib.read_delegation(d, on_error=errs.append))
        self.assertEqual(len(errs), 1)


def schemas_version():
    import schemas
    return schemas.SCHEMA_VERSION


# ---------------------------------------------------------------------------
# Spawn — argv shape, no secrets, run.pid, launch failure.
# ---------------------------------------------------------------------------
class TestSpawn(_Base):
    def test_argv_carries_root_app_and_no_secrets(self):
        argv = seslib.spawn_argv(self.root, "gloam/research/chat")
        self.assertIn("--root", argv)
        self.assertIn("--app", argv)
        self.assertEqual(argv[argv.index("--app") + 1], "gloam/research/chat")
        self.assertTrue(argv[1].endswith("orchestrator.py"))
        joined = " ".join(argv)
        for secret in seslib.SECRET_ENV:
            self.assertNotIn(secret, joined)

    def test_spawn_writes_run_pid_and_running_status(self):
        d = self._mint()
        calls = {}
        class _Proc:
            pid = 4242
        def fake_popen(argv, **kw):
            calls["argv"] = argv
            calls["env"] = kw.get("env", {})
            calls["cwd"] = kw.get("cwd")
            calls["detached"] = kw.get("start_new_session")
            return _Proc()
        pid = seslib.spawn_run(d, root=self.root, _popen=fake_popen,
                               on_error=self.errors.append)
        self.assertEqual(pid, 4242)
        self.assertEqual(seslib.read_pidfile(d), 4242)
        self.assertEqual(seslib.read_delegation(d)["status"], "running")
        self.assertTrue(calls["detached"], "the run must outlive its launcher")
        self.assertEqual(calls["cwd"], self.root)
        # The scrubbed env must not carry any key material.
        for secret in seslib.SECRET_ENV:
            self.assertNotIn(secret, calls["env"])

    def test_env_scrub_removes_secret_shaped_vars_beyond_the_floor(self):
        d = self._mint()
        captured = {}
        # Not in the readable SECRET_ENV floor, but secret-shaped — the
        # shared predicate must still drop them (finding: incomplete list).
        planted = {"ANTHROPIC_API_KEY": "sk-a", "AWS_SECRET_ACCESS_KEY": "x",
                   "GITHUB_TOKEN": "ghp_x", "MY_DB_PASSWORD": "p",
                   "SOME_SESSION": "s"}
        for k, v in planted.items():
            os.environ[k] = v
            self.addCleanup(os.environ.pop, k, None)
        class _Proc:
            pid = 1
        def fake_popen(argv, **kw):
            captured.update(kw.get("env", {}))
            return _Proc()
        seslib.spawn_run(d, root=self.root, _popen=fake_popen)
        for k in planted:
            self.assertNotIn(k, captured, k)

    def test_launch_failure_sets_failed_status_no_pidfile(self):
        d = self._mint()
        def boom(argv, **kw):
            raise OSError("no exec")
        pid = seslib.spawn_run(d, root=self.root, _popen=boom,
                               on_error=self.errors.append)
        self.assertIsNone(pid)
        rec = seslib.read_delegation(d)
        self.assertEqual(rec["status"], "failed")
        self.assertIn("failed_reason", rec)
        self.assertFalse(os.path.exists(seslib.run_pid_path(d)),
                         "a failed launch writes no run.pid")

    def test_reconcile_flips_dead_running_to_failed(self):
        d = self._mint()
        seslib.set_status(d, "running")
        # A pid that is certainly not alive.
        with open(seslib.run_pid_path(d), "w") as fh:
            fh.write("999999999\n")
        self.assertEqual(seslib.reconcile_delegation(d), "failed")
        self.assertEqual(seslib.read_delegation(d)["status"], "failed")

    def test_reconcile_leaves_a_live_run_running(self):
        d = self._mint()
        seslib.set_status(d, "running")
        with open(seslib.run_pid_path(d), "w") as fh:
            fh.write("%d\n" % os.getpid())   # this test process IS alive
        self.assertEqual(seslib.reconcile_delegation(d), "running")


# ---------------------------------------------------------------------------
# Reply edge — exactly once, card ref only, crash-retry safe.
# ---------------------------------------------------------------------------
class TestReplyEdge(_Base):
    def _card(self, aid="dark-mode", type="research_brief", version=1):
        return {"id": aid, "type": type, "version": version,
                "path": "gloam/artifacts/%s" % aid}

    def _delegated(self):
        origin = self._origin()
        d = self._mint(reply_to=origin)
        return origin, d

    def test_round_trip_appends_one_card_ref(self):
        origin, d = self._delegated()
        self.assertTrue(seslib.deliver_reply(d, self._card(),
                                             on_error=self.errors.append))
        with open(os.path.join(origin, "human_inbox.txt")) as fh:
            inbox = fh.read()
        self.assertIn("[artifact:dark-mode]", inbox)
        self.assertIn("research_brief", inbox)
        self.assertIn("gloam/artifacts/dark-mode", inbox)
        self.assertNotIn("initial_prompt", inbox, "no body, just a ref")
        rec = seslib.read_delegation(d)
        self.assertEqual(rec["status"], "replied")
        self.assertEqual(rec["delivered"], ["dark-mode"])
        # An artifact_routed event was emitted.
        with open(os.path.join(d, "events.jsonl")) as fh:
            kinds = [json.loads(l)["kind"] for l in fh if l.strip()]
        self.assertIn("artifact_routed", kinds)

    def test_redelivery_is_exactly_once_via_ledger(self):
        origin, d = self._delegated()
        seslib.deliver_reply(d, self._card())
        seslib.deliver_reply(d, self._card())   # resume/retry
        with open(os.path.join(origin, "human_inbox.txt")) as fh:
            self.assertEqual(fh.read().count("[artifact:dark-mode]"), 1)

    def test_ledger_dedupe_survives_an_inbox_drain(self):
        # The origin DRAINS its inbox after reading the reply, so the
        # inbox marker is gone. Only the durable `delivered` ledger can
        # stop a redelivery from re-appending — the inbox scan cannot.
        origin, d = self._delegated()
        seslib.deliver_reply(d, self._card())
        inbox = os.path.join(origin, "human_inbox.txt")
        open(inbox, "w").close()   # origin drained it
        seslib.deliver_reply(d, self._card())   # retry after drain
        with open(inbox) as fh:
            self.assertEqual(fh.read().count("[artifact:dark-mode]"), 0,
                             "the durable ledger blocks a post-drain "
                             "redelivery")

    def _crash_after_append(self, d, card=None):
        # The realistic crash: intent was fsync'd, the append landed, the
        # done-write was lost. Reproduce that exact on-disk state.
        card = card or self._card()
        rec = seslib.read_delegation(d)
        rec["delivering"] = [card["id"]]        # intent recorded
        seslib._write_delegation(d, rec)
        with open(os.path.join(rec["reply_to"], "human_inbox.txt"), "a") as fh:
            fh.write(seslib._reply_line(rec, card))   # append landed

    def test_crash_after_append_before_ledger_still_once(self):
        origin, d = self._delegated()
        self._crash_after_append(d)
        # Resume: delivering=[aid], delivered=[] → reconcile, no re-append.
        self.assertTrue(seslib.deliver_reply(d, self._card()))
        with open(os.path.join(origin, "human_inbox.txt")) as fh:
            self.assertEqual(fh.read().count("[artifact:dark-mode]"), 1,
                             "intent+origin-probe closes the pre-ledger gap")
        self.assertEqual(seslib.read_delegation(d)["delivered"],
                         ["dark-mode"])

    def test_crash_after_append_THEN_origin_drain_still_once(self):
        # The finding-0 conjunction: crash between append and ledger, THEN
        # the origin drains its inbox (folding the reply into its
        # transcript) before the delegated run resumes. Only the durable
        # transcript probe stops a second delivery.
        origin, d = self._delegated()
        self._crash_after_append(d)
        inbox = os.path.join(origin, "human_inbox.txt")
        # The origin drains: fold the reply into a phase transcript, then
        # truncate the inbox (exactly what drain_human_inbox does).
        with open(inbox) as fh:
            folded = fh.read()
        os.makedirs(os.path.join(origin, "discuss"), exist_ok=True)
        with open(os.path.join(origin, "discuss", "discussion.md"),
                  "w") as fh:
            fh.write("# transcript\n\n**You (human) — Research**\n" + folded)
        open(inbox, "w").close()                 # inbox truncated
        # Resume: inbox empty, ledger empty — the transcript probe must
        # recognize the already-delivered reply and NOT re-append.
        seslib.deliver_reply(d, self._card())
        with open(inbox) as fh:
            self.assertEqual(fh.read().count("[artifact:dark-mode]"), 0,
                             "the drained-then-resumed reply is not re-sent")
        self.assertEqual(seslib.read_delegation(d)["delivered"],
                         ["dark-mode"])

    def test_crash_before_append_does_deliver(self):
        # Intent recorded, but the append never landed (crash before it).
        # The origin never saw it → the retry MUST deliver.
        origin, d = self._delegated()
        rec = seslib.read_delegation(d)
        rec["delivering"] = ["dark-mode"]
        seslib._write_delegation(d, rec)
        self.assertTrue(seslib.deliver_reply(d, self._card()))
        with open(os.path.join(origin, "human_inbox.txt")) as fh:
            self.assertEqual(fh.read().count("[artifact:dark-mode]"), 1)

    def test_two_artifacts_each_get_their_own_ref(self):
        origin, d = self._delegated()
        seslib.deliver_reply(d, self._card(aid="one"))
        seslib.deliver_reply(d, self._card(aid="two"))
        with open(os.path.join(origin, "human_inbox.txt")) as fh:
            inbox = fh.read()
        self.assertIn("[artifact:one]", inbox)
        self.assertIn("[artifact:two]", inbox)
        self.assertEqual(sorted(seslib.read_delegation(d)["delivered"]),
                         ["one", "two"])

    def test_non_delegation_session_is_a_no_op(self):
        plain = self._origin("gloam/ideas/plain")
        self.assertFalse(seslib.deliver_reply(plain, self._card()))

    def test_routed_event_on_the_reconcile_branch(self):
        # A reconciled already-delivered reply must still emit exactly one
        # artifact_routed (the event fires on both success branches).
        origin, d = self._delegated()
        self._crash_after_append(d)
        seslib.deliver_reply(d, self._card())    # takes the reconcile path
        with open(os.path.join(d, "events.jsonl")) as fh:
            routed = [l for l in fh if '"artifact_routed"' in l]
        self.assertEqual(len(routed), 1)

    def test_non_string_reply_to_reports_never_raises(self):
        origin, d = self._delegated()
        rec = seslib.read_delegation(d)
        rec["reply_to"] = ["not", "a", "string"]
        seslib._write_delegation(d, rec)
        self.assertFalse(seslib.deliver_reply(d, self._card(),
                                              on_error=self.errors.append))
        self.assertTrue(any("not a string" in e for e in self.errors))

    def test_reply_to_gone_leaves_running_for_retry(self):
        origin, d = self._delegated()
        seslib.set_status(d, "running")
        shutil.rmtree(origin)   # origin vanished
        os.makedirs(origin)     # recreate as a dir but... make append fail:
        # Point reply_to at a path whose parent is a file → append raises.
        rec = seslib.read_delegation(d)
        bad = os.path.join(self.tmp, "afile")
        with open(bad, "w") as fh:
            fh.write("x")
        rec["reply_to"] = os.path.join(bad, "sub")
        seslib._write_delegation(d, rec)
        self.assertFalse(seslib.deliver_reply(d, self._card(),
                                              on_error=self.errors.append))
        self.assertEqual(seslib.read_delegation(d)["status"], "running",
                         "a failed delivery must not falsely claim replied")


# ---------------------------------------------------------------------------
# The reply edge wired through the real 4.2 phase-close hook: a delegated
# run that publishes an artifact-json block delivers a card back to its
# origin, and a non-delegated run is a byte no-op (golden-safe).
# ---------------------------------------------------------------------------
class TestHookReplyEdge(_Base):
    def setUp(self):
        super().setUp()
        # Seed the artifact registry so the delegated publish succeeds.
        self.errors = []
        import artifacts as artlib
        artlib.load_registry(self.root, on_error=self.errors.append)

    def _run_hook(self, app_dir, final_output, key="brainstorm"):
        import workflows as wf
        return orch._hook_artifact_publish(
            {"root": self.root, "_app_dir": app_dir}, "gloam", app_dir,
            wf.Phase(key, ".", "x.md", "p"), {},
            key=key, md_path=os.path.join(app_dir, "x.md"),
            transcript="t", final_output=final_output, coord=None,
            active=[], is_build=False, is_verify_repair=False,
            allow_writes=False, _needs_vlabel=False)

    def _block(self, obj):
        return "```artifact-json\n%s\n```" % json.dumps(obj)

    def test_delegated_publish_replies_to_origin(self):
        origin = self._origin()
        d = self._mint(section="research", reply_to=origin)
        seslib.set_status(d, "running")
        out = "Wrap-up.\n" + self._block(
            {"type": "research_brief", "title": "Caching brief",
             "body": "# Findings\n", "sources": ["a"]})
        t, f = self._run_hook(d, out)
        self.assertEqual((t, f), ("t", out), "the hook is a pure reader")
        # The artifact landed in the project store...
        project_dir = os.path.join(self.root, "gloam")
        import artifacts as artlib
        listed = artlib.list_artifacts(project_dir)
        self.assertEqual(len(listed), 1)
        # ...and a card ref reached the origin inbox exactly once.
        with open(os.path.join(origin, "human_inbox.txt")) as fh:
            inbox = fh.read()
        self.assertIn("[artifact:%s]" % listed[0]["id"], inbox)
        self.assertNotIn("# Findings", inbox, "card ref, never the body")
        self.assertEqual(seslib.read_delegation(d)["status"], "replied")
        # Re-close (resume) must not double-deliver.
        self._run_hook(d, out)
        with open(os.path.join(origin, "human_inbox.txt")) as fh:
            self.assertEqual(fh.read().count("[artifact:"), 1)

    def test_crash_between_publish_and_reply_delivers_on_resume(self):
        # First close publishes the artifact but "crashes" before the
        # reply (simulate by deleting the inbox marker path is n/a — instead
        # publish via a delegation whose status is left pending, then the
        # SECOND close, where publish_from_output returns [] due to dedupe,
        # must still deliver from the durable ledger).
        origin = self._origin()
        d = self._mint(section="research", reply_to=origin)
        seslib.set_status(d, "running")
        out = "Wrap-up.\n" + self._block(
            {"type": "research_brief", "title": "Brief",
             "body": "# B\n", "sources": ["s"]})
        # Publish only (no reply) by running the publish half directly.
        import artifacts as artlib
        project_dir = os.path.join(self.root, "gloam")
        reg = artlib.load_registry(self.root)
        artlib.publish_from_output(
            project_dir, out,
            {"section": "research", "session": os.path.basename(d),
             "phase": "brainstorm", "turn": ""}, reg)
        self.assertEqual(len(artlib.list_artifacts(project_dir)), 1)
        self.assertEqual(os.path.exists(
            os.path.join(origin, "human_inbox.txt")), False,
            "no reply yet — the crash happened before delivery")
        # Resume: the hook re-runs; publish_from_output dedupes to [] but
        # the reply edge, driven by the ledger, still delivers.
        self._run_hook(d, out)
        with open(os.path.join(origin, "human_inbox.txt")) as fh:
            self.assertEqual(fh.read().count("[artifact:"), 1,
                             "exactly-once even when publish re-does nothing")

    def test_later_phase_close_still_delivers(self):
        # A delegated run that publishes in a SECOND phase close must still
        # deliver that artifact — the 'replied' status from the first close
        # must not terminally gate the reply edge.
        origin = self._origin()
        d = self._mint(section="research", reply_to=origin)
        seslib.set_status(d, "running")
        self._run_hook(d, "P1\n" + self._block(
            {"type": "research_brief", "title": "Draft",
             "body": "# D\n", "sources": ["a"]}), key="brainstorm")
        self.assertEqual(seslib.read_delegation(d)["status"], "replied")
        self._run_hook(d, "P2\n" + self._block(
            {"type": "research_brief", "title": "Final",
             "body": "# F\n", "sources": ["b"]}), key="synthesize")
        with open(os.path.join(origin, "human_inbox.txt")) as fh:
            inbox = fh.read()
        import artifacts as artlib
        ids = [m["id"] for m in artlib.list_artifacts(
            os.path.join(self.root, "gloam"))]
        self.assertEqual(len(ids), 2)
        for aid in ids:
            self.assertIn("[artifact:%s]" % aid, inbox,
                          "every phase's artifact reaches the origin")

    def test_non_delegated_session_writes_no_inbox(self):
        # A normal (non-delegation) session with the same publish must not
        # touch any inbox — golden-safe.
        d = orch.create_session(self.root, "gloam/research/plain-chat", "p")
        out = "Wrap-up.\n" + self._block(
            {"type": "research_brief", "title": "Plain",
             "body": "# P\n", "sources": ["x"]})
        self._run_hook(d, out)
        import artifacts as artlib
        self.assertEqual(
            len(artlib.list_artifacts(os.path.join(self.root, "gloam"))), 1)
        self.assertFalse(os.path.exists(
            os.path.join(d, "delegation.json")))


if __name__ == "__main__":
    unittest.main()


class TestRunPidLifecycle(unittest.TestCase):
    """V3 7.0: run.pid write/remove discipline — the belt-and-braces stop
    target must exist exactly while a run is live."""

    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.app_dir = os.path.join(self.root, "p", "s", "chat")
        os.makedirs(self.app_dir)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_write_read_remove_roundtrip(self):
        seslib.write_pidfile(self.app_dir, 4242)
        self.assertEqual(seslib.read_pidfile(self.app_dir), 4242)
        seslib.remove_pidfile(self.app_dir)
        self.assertIsNone(seslib.read_pidfile(self.app_dir))

    def test_remove_missing_is_silent(self):
        seslib.remove_pidfile(self.app_dir)   # must not raise

    def test_process_app_self_writes_then_removes(self):
        import orchestrator as orch
        seen = {}
        os.makedirs(os.path.join(self.app_dir, "initial_prompt"))
        with open(os.path.join(self.app_dir, "initial_prompt",
                               "initial_prompt.md"), "w",
                  encoding="utf-8") as fh:
            fh.write("build a thing")

        def fake_pipeline(cfg, app, app_dir, prompt):
            seen["live_pid"] = seslib.read_pidfile(app_dir)

        saved_locks, saved_quiet = orch.LOCKS_DIR, orch._QUIET
        orch.LOCKS_DIR, orch._QUIET = os.path.join(self.root, ".orch-locks"), True
        try:
            with unittest.mock.patch.object(orch, "_run_app_pipeline",
                                            fake_pipeline), \
                    unittest.mock.patch.object(orch, "_start_run_heartbeat",
                                               lambda *a: threading.Event()), \
                    unittest.mock.patch.object(orch, "_sweep_stream_orphans",
                                               lambda *a: None):
                orch.process_app({"root": self.root, "runtime": {}},
                                 self.root, "p/s/chat")
        finally:
            orch.LOCKS_DIR, orch._QUIET = saved_locks, saved_quiet
        # Alive DURING the pipeline with our own pid; gone after the finally.
        self.assertEqual(seen["live_pid"], os.getpid())
        self.assertIsNone(seslib.read_pidfile(self.app_dir))

    def test_pipeline_crash_still_removes(self):
        import orchestrator as orch
        os.makedirs(os.path.join(self.app_dir, "initial_prompt"))
        with open(os.path.join(self.app_dir, "initial_prompt",
                               "initial_prompt.md"), "w",
                  encoding="utf-8") as fh:
            fh.write("x")

        def exploding(cfg, app, app_dir, prompt):
            raise RuntimeError("boom")

        saved_locks, saved_quiet = orch.LOCKS_DIR, orch._QUIET
        orch.LOCKS_DIR, orch._QUIET = os.path.join(self.root, ".orch-locks"), True
        try:
            with unittest.mock.patch.object(orch, "_run_app_pipeline",
                                            exploding), \
                    unittest.mock.patch.object(orch, "_start_run_heartbeat",
                                               lambda *a: threading.Event()), \
                    unittest.mock.patch.object(orch, "_sweep_stream_orphans",
                                               lambda *a: None):
                with self.assertRaises(RuntimeError):
                    orch.process_app({"root": self.root, "runtime": {}},
                                     self.root, "p/s/chat")
        finally:
            orch.LOCKS_DIR, orch._QUIET = saved_locks, saved_quiet
        self.assertIsNone(seslib.read_pidfile(self.app_dir))

    def test_lock_skipped_duplicate_never_touches_pidfile(self):
        # The ordering claim in process_app's comment, pinned: a second
        # process losing the lock race returns before the write and must
        # leave the live run's pidfile byte-identical.
        import orchestrator as orch
        os.makedirs(os.path.join(self.app_dir, "initial_prompt"))
        with open(os.path.join(self.app_dir, "initial_prompt",
                               "initial_prompt.md"), "w",
                  encoding="utf-8") as fh:
            fh.write("x")
        seslib.write_pidfile(self.app_dir, 999999)   # the "live run's" file
        saved_locks, saved_quiet = orch.LOCKS_DIR, orch._QUIET
        orch.LOCKS_DIR, orch._QUIET = os.path.join(self.root, ".orch-locks"), True
        try:
            with unittest.mock.patch.object(orch, "acquire_app_lock",
                                            lambda *a, **k: False), \
                    unittest.mock.patch.object(
                        orch, "_run_app_pipeline",
                        lambda *a: (_ for _ in ()).throw(
                            AssertionError("must not run"))):
                orch.process_app({"root": self.root, "runtime": {}},
                                 self.root, "p/s/chat")
        finally:
            orch.LOCKS_DIR, orch._QUIET = saved_locks, saved_quiet
        self.assertEqual(seslib.read_pidfile(self.app_dir), 999999)

    def test_failed_pidfile_write_still_releases_the_lock(self):
        # Regression for the review's converged finding: an OSError from the
        # write must NOT leak the held lock (permanent app starvation under
        # parallel workers) — it unwinds through the finally instead.
        import orchestrator as orch
        os.makedirs(os.path.join(self.app_dir, "initial_prompt"))
        with open(os.path.join(self.app_dir, "initial_prompt",
                               "initial_prompt.md"), "w",
                  encoding="utf-8") as fh:
            fh.write("x")
        released = {}
        saved_locks, saved_quiet = orch.LOCKS_DIR, orch._QUIET
        orch.LOCKS_DIR, orch._QUIET = os.path.join(self.root, ".orch-locks"), True
        try:
            real_release = orch.release_app_lock
            with unittest.mock.patch.object(
                    seslib, "write_pidfile",
                    lambda *a: (_ for _ in ()).throw(OSError("disk full"))), \
                    unittest.mock.patch.object(
                        orch, "release_app_lock",
                        lambda app: released.update(app=app) or real_release(app)), \
                    unittest.mock.patch.object(orch, "_start_run_heartbeat",
                                               lambda *a: threading.Event()), \
                    unittest.mock.patch.object(orch, "_sweep_stream_orphans",
                                               lambda *a: None):
                with self.assertRaises(OSError):
                    orch.process_app({"root": self.root, "runtime": {}},
                                     self.root, "p/s/chat")
        finally:
            orch.LOCKS_DIR, orch._QUIET = saved_locks, saved_quiet
        self.assertEqual(released.get("app"), "p/s/chat")

    def test_fork_never_copies_run_pid(self):
        import orchestrator as orch
        src = os.path.join(self.root, "srcproj")
        os.makedirs(os.path.join(src, "initial_prompt"))
        with open(os.path.join(src, "initial_prompt", "initial_prompt.md"),
                  "w", encoding="utf-8") as fh:
            fh.write("x")
        seslib.write_pidfile(src, 12345)
        saved_quiet = orch._QUIET
        orch._QUIET = True
        try:
            orch.fork_session(self.root, "srcproj", "forkproj")
        finally:
            orch._QUIET = saved_quiet
        dst = os.path.join(self.root, "forkproj")
        self.assertTrue(os.path.isdir(dst), "fork must have copied the dir")
        self.assertIsNone(seslib.read_pidfile(dst),
                          "a fork has no runner — run.pid must not be copied")
