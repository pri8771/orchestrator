"""Tests for the per-project structured event log (events.py, spec §6 / M4):

- emit_event appends one JSON line per event, stamps ts/kind, drops None
  fields, redacts + caps string values, and NEVER raises (best-effort)
- read_events parses oldest-first, skips corrupt lines, filters by kind
- _call_agent_once emits exactly one turn_started + one turn_completed per
  call — success (model_used/dur/exit/output_len) and failure (reason) alike
- call_agent's fallback ladder emits agent_fallback events carrying
  from_model/to_model/reason, and aggregates per-run rescue counts into
  agent_state.json (state["fallback_counts"]) for UI badging
"""
import json
import os
import shutil
import subprocess
import tempfile
import unittest
import unittest.mock

import events as evlib
import orchestrator as orch


class TestEmitEvent(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="orch_events_")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_appends_one_json_line_per_event(self):
        self.assertTrue(evlib.emit_event(self.dir, "run_started", project="x"))
        self.assertTrue(evlib.emit_event(self.dir, "phase_started",
                                         project="x", phase="tech_specs"))
        with open(evlib.events_path(self.dir), encoding="utf-8") as fh:
            lines = [ln for ln in fh.read().splitlines() if ln]
        self.assertEqual(len(lines), 2)
        first = json.loads(lines[0])
        self.assertEqual(first["kind"], "run_started")
        self.assertEqual(first["project"], "x")
        self.assertIn("ts", first)

    def test_none_fields_dropped(self):
        evlib.emit_event(self.dir, "turn_completed", agent="claude", detail=None)
        evt = evlib.read_events(self.dir)[0]
        self.assertNotIn("detail", evt)
        self.assertEqual(evt["agent"], "claude")

    def test_never_raises_and_returns_false(self):
        self.assertFalse(evlib.emit_event(None, "run_started"))
        self.assertFalse(evlib.emit_event("", "run_started"))
        self.assertFalse(evlib.emit_event(self.dir, ""))
        # Nonexistent directory: open() fails, emit_event swallows it.
        self.assertFalse(evlib.emit_event(
            os.path.join(self.dir, "no", "such", "dir"), "run_started"))

    def test_unserializable_field_falls_back_to_str(self):
        self.assertTrue(evlib.emit_event(self.dir, "run_started",
                                         weird=object()))
        self.assertEqual(evlib.read_events(self.dir)[0]["kind"], "run_started")

    def test_string_fields_redacted_and_capped(self):
        evlib.emit_event(self.dir, "turn_completed",
                         detail="api_key=sk_livekey_1234567890abcd",
                         big="x" * 5000)
        evt = evlib.read_events(self.dir)[0]
        self.assertIn("[REDACTED", evt["detail"])
        self.assertNotIn("1234567890abcd", evt["detail"])
        self.assertLessEqual(len(evt["big"]), 500)

    def test_nested_dict_and_list_fields_are_also_redacted(self):
        evlib.emit_event(self.dir, "turn_completed",
                         payload={"note": "api_key=sk_livekey_1234567890abcd",
                                  "nested": {"inner": "token=sk_livekey_abcdefghijk"}},
                         items=["api_key=sk_livekey_1234567890abcd", "plain text"])
        evt = evlib.read_events(self.dir)[0]
        self.assertIn("[REDACTED", evt["payload"]["note"])
        self.assertIn("[REDACTED", evt["payload"]["nested"]["inner"])
        self.assertNotIn("1234567890abcd", json.dumps(evt))
        self.assertIn("[REDACTED", evt["items"][0])
        self.assertEqual(evt["items"][1], "plain text")

    def test_read_events_skips_corrupt_lines_and_filters(self):
        evlib.emit_event(self.dir, "run_started", project="x")
        with open(evlib.events_path(self.dir), "a", encoding="utf-8") as fh:
            fh.write("{corrupt json\n")
            fh.write("[1, 2, 3]\n")   # parseable but not an event dict
        evlib.emit_event(self.dir, "run_finished", project="x", status="done")
        all_evts = evlib.read_events(self.dir)
        self.assertEqual([e["kind"] for e in all_evts],
                         ["run_started", "run_finished"])
        only_fin = evlib.read_events(self.dir, kinds=("run_finished",))
        self.assertEqual(len(only_fin), 1)
        self.assertEqual(only_fin[0]["status"], "done")

    def test_read_events_missing_file_is_empty(self):
        self.assertEqual(evlib.read_events(self.dir), [])
        self.assertEqual(evlib.read_events(os.path.join(self.dir, "nope")), [])

    def test_unknown_kind_warns_but_still_writes(self):
        # A typo'd kind would silently vanish from any UI filtering on
        # events.KINDS — it must at least warn, and still write (best-effort).
        with self.assertWarns(Warning):
            self.assertTrue(evlib.emit_event(self.dir, "phase_finshed"))
        self.assertEqual(evlib.read_events(self.dir)[0]["kind"], "phase_finshed")

    def test_known_kind_does_not_warn(self):
        with unittest.mock.patch("warnings.warn") as mock_warn:
            evlib.emit_event(self.dir, "run_started")
        mock_warn.assert_not_called()


class TestConductorNotificationVocabulary(unittest.TestCase):
    KINDS = ("approval_needed", "stalled", "converged",
             "budget_exhausted")

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="orch_events_notify_")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_kinds_are_warning_free_filterable_redacted_and_capped(self):
        for kind in self.KINDS:
            with unittest.mock.patch("warnings.warn") as mock_warn:
                self.assertTrue(evlib.emit_event(
                    self.dir, kind, project="p", section="research",
                    reason="api_key=sk_livekey_1234567890abcd" + "x" * 800))
            mock_warn.assert_not_called()
        for kind in self.KINDS:
            records = evlib.read_events(self.dir, kinds=(kind,))
            self.assertEqual(1, len(records))
            self.assertIn("[REDACTED", records[0]["reason"])
            self.assertLessEqual(len(records[0]["reason"]), 500)

    def test_corrupt_partial_line_does_not_hide_valid_condition(self):
        evlib.emit_event(self.dir, "stalled", project="p", reason="votes")
        with open(evlib.events_path(self.dir), "a", encoding="utf-8") as fh:
            fh.write('{"kind":"converged"')
        self.assertEqual(["stalled"], [r["kind"] for r in
                                       evlib.read_events(self.dir)])

    def test_nested_evidence_bloat_still_stays_under_cap(self):
        # The shrink loop used to give up on non-string bloat ("break") —
        # a conductor termination/budget evidence dict (e.g. open_gaps)
        # then wrote one oversized line, defeating the PIPE_BUF atomicity
        # the cap exists for. Nested dict/list fields must be flattened
        # until the line fits, with top-level identifiers surviving.
        evidence = {"k%02d" % i: "v" * 500 for i in range(20)}
        self.assertTrue(evlib.emit_event(
            self.dir, "budget_exhausted", session="p/research/c1",
            reason="spend_exhausted", evidence=evidence,
            gaps=["g" * 400] * 10))
        with open(evlib.events_path(self.dir), encoding="utf-8") as fh:
            line = fh.readline().rstrip("\n")
        self.assertLessEqual(len(line.encode("utf-8")), 3500)
        evt = json.loads(line)
        self.assertEqual(evt["kind"], "budget_exhausted")
        self.assertEqual(evt["session"], "p/research/c1")
        self.assertEqual(evt["reason"], "spend_exhausted")
        self.assertNotIn("…", evt["ts"])   # ts never sacrificed first


class TestArtifactBusVocabulary(unittest.TestCase):
    """V3 board 2.5: the six bus kinds are registered ahead of their
    emitters (2.4 / M4 / M7) — vocabulary, warning, and cap contracts."""

    NEW_KINDS = ("message_appended",
                 "artifact_published", "artifact_routed", "artifact_consumed",
                 "route_proposed", "route_approved")

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="orch_events_")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_each_new_kind_emits_warning_free(self):
        for kind in self.NEW_KINDS:
            with unittest.mock.patch("warnings.warn") as mock_warn:
                self.assertTrue(evlib.emit_event(self.dir, kind, probe="x"))
            mock_warn.assert_not_called()

    def test_unknown_kind_still_warns_regression(self):
        with self.assertWarns(Warning):
            self.assertTrue(evlib.emit_event(self.dir, "artifact_publised"))

    def test_read_events_filters_the_new_kinds(self):
        for kind in self.NEW_KINDS:
            evlib.emit_event(self.dir, kind, artifact_id="a1")
        evlib.emit_event(self.dir, "run_started")
        got = evlib.read_events(self.dir, kinds=["artifact_published",
                                                 "route_approved"])
        self.assertEqual([e["kind"] for e in got],
                         ["artifact_published", "route_approved"])

    def test_pathological_artifact_line_stays_under_cap(self):
        # Exercises the shrink loop: worst-case long id/path fields must
        # still produce one ≤3500B valid-JSON line (PIPE_BUF atomicity).
        evlib.emit_event(
            self.dir, "artifact_published",
            artifact_id="a" * 4000, type="research_report", version=3,
            path="/very/long/" + "p" * 4000)
        with open(evlib.events_path(self.dir), encoding="utf-8") as fh:
            line = fh.readline().rstrip("\n")
        self.assertLessEqual(len(line.encode("utf-8")), 3500)
        evt = json.loads(line)
        self.assertEqual(evt["kind"], "artifact_published")

    def test_docstring_documents_every_kind_ids_and_paths_only(self):
        # The module docstring is the human-readable contract: every new
        # kind must appear there, and no documented field may be a bare
        # content/body carrier (content_path is a path and is fine).
        doc = evlib.__doc__
        for kind in self.NEW_KINDS:
            self.assertIn(kind, doc, "kind %r missing from docstring" % kind)
            para = doc[doc.index(kind):]
            fields = para[para.index("(") + 1:para.index(")")]
            # Field NAMES precede the em-dash; prose after it may of course
            # mention where bodies live.
            names = fields.split("—", 1)[0]
            tokens = {t.strip(" .,") for t in names.replace(",", " ").split()}
            self.assertFalse({"content", "body"} & tokens,
                             "%s documents a content-carrying field" % kind)

    def test_every_kind_in_code_is_in_the_docstring(self):
        # No kind may exist in KINDS but not in the human-readable contract.
        doc = evlib.__doc__
        for kind in evlib.KINDS:
            self.assertIn(kind, doc,
                          "KINDS entry %r missing from docstring" % kind)


class _RunnerStub:
    def __init__(self, result=None, exc=None):
        self.result = result
        self.exc = exc
        self.calls = 0

    def __call__(self, cfg, prompt, timeout):
        self.calls += 1
        if self.exc is not None:
            raise self.exc
        return self.result


class TestTurnEvents(unittest.TestCase):
    def setUp(self):
        self.app_dir = tempfile.mkdtemp(prefix="orch_events_app_")
        self.cfg = {
            "models": {"claude": "sonnet"},
            "_resolved": {"claude_model": "sonnet"},
            "_app_dir": self.app_dir,
        }
        self._orig_runner = orch.RUNNERS["claude"]

    def tearDown(self):
        orch.RUNNERS["claude"] = self._orig_runner
        shutil.rmtree(self.app_dir, ignore_errors=True)

    def test_success_emits_started_and_completed(self):
        orch.RUNNERS["claude"] = _RunnerStub(("hello world", "", 0, "claude -p"))
        text = orch._call_agent_once(self.cfg, "appx", "tech_specs", 2,
                                     "claude", "hi")
        self.assertEqual(text, "hello world")
        evts = evlib.read_events(self.app_dir)
        self.assertEqual([e["kind"] for e in evts],
                         ["turn_started", "turn_completed"])
        started, done = evts
        self.assertEqual(started["agent"], "claude")
        self.assertEqual(started["phase"], "tech_specs")
        self.assertEqual(started["round"], 2)
        self.assertEqual(started["model_requested"], "sonnet")
        self.assertTrue(done["ok"])
        self.assertEqual(done["exit"], 0)
        self.assertEqual(done["model_used"], "sonnet")
        self.assertEqual(done["output_len"], len("hello world"))
        self.assertIn("dur", done)

    def test_timeout_emits_failed_completion(self):
        orch.RUNNERS["claude"] = _RunnerStub(
            exc=subprocess.TimeoutExpired(cmd="claude", timeout=1))
        with self.assertRaises(orch.AgentError):
            orch._call_agent_once(self.cfg, "appx", "tech_specs", 1,
                                  "claude", "hi")
        done = evlib.read_events(self.app_dir, kinds=("turn_completed",))
        self.assertEqual(len(done), 1)
        self.assertFalse(done[0]["ok"])
        self.assertEqual(done[0]["reason"], "timeout")
        self.assertEqual(done[0]["exit"], 124)

    def test_empty_output_emits_failed_completion(self):
        orch.RUNNERS["claude"] = _RunnerStub(("", "boom", 1, "claude -p"))
        with self.assertRaises(orch.AgentError):
            orch._call_agent_once(self.cfg, "appx", "tech_specs", 1,
                                  "claude", "hi")
        done = evlib.read_events(self.app_dir, kinds=("turn_completed",))
        self.assertEqual(done[0]["reason"], "empty_output")

    def test_provider_banner_emits_failed_completion(self):
        orch.RUNNERS["claude"] = _RunnerStub(
            ("You've hit your usage limit.", "", 0, "claude -p"))
        with self.assertRaises(orch.AgentError):
            orch._call_agent_once(self.cfg, "appx", "tech_specs", 1,
                                  "claude", "hi")
        done = evlib.read_events(self.app_dir, kinds=("turn_completed",))
        self.assertFalse(done[0]["ok"])
        self.assertEqual(done[0]["reason"], "provider_banner")

    def test_no_app_dir_is_silent_noop(self):
        cfg = {"models": {"claude": "sonnet"},
               "_resolved": {"claude_model": "sonnet"}}
        orch.RUNNERS["claude"] = _RunnerStub(("hello", "", 0, "claude -p"))
        text = orch._call_agent_once(cfg, "appx", "tech_specs", 1, "claude", "hi")
        self.assertEqual(text, "hello")   # events never affect the turn


class TestGlobalSlotReleasedOnSetupFailure(unittest.TestCase):
    """A claimed global-worker slot must be released even when setup BETWEEN
    the claim and the runner call raises — Thread.start() can genuinely raise
    RuntimeError under thread exhaustion (realistic exactly when many parallel
    workers run), and the slot used to leak until the 6-hour age reap."""

    def test_slot_released_when_heartbeat_thread_start_raises(self):
        cfg = {"models": {"claude": "sonnet"},
               "_resolved": {"claude_model": "sonnet"},
               "runtime": {"global_worker_cap_enabled": True}}
        token = 4242
        released = []

        class _BoomThread:
            def __init__(self, *args, **kwargs):
                pass

            def start(self):
                raise RuntimeError("can't start new thread")

        with unittest.mock.patch.object(orch.grlib, "claim_slot",
                                        return_value=token), \
                unittest.mock.patch.object(
                    orch.grlib, "release",
                    side_effect=lambda rc, tok, **kw: released.append((rc, tok))), \
                unittest.mock.patch.object(orch.threading, "Thread", _BoomThread):
            with self.assertRaises(RuntimeError):
                orch._call_agent_once(cfg, "appx", "tech_specs", 1, "claude", "hi")
        self.assertEqual(released, [("cli_remote", token)])


class TestFallbackEventsAndCounts(unittest.TestCase):
    """call_agent's ladder: agent_fallback events + fallback_counts badge."""

    def setUp(self):
        self.app_dir = tempfile.mkdtemp(prefix="orch_events_fb_")
        self.cfg = {
            "models": {"claude": "sonnet"},
            "_resolved": {"claude_model": "sonnet"},
            "_app_dir": self.app_dir,
            "_state": orch.load_state(self.app_dir),
        }
        self._orig_steps = orch._fallback_steps
        self._orig_installed = orch.lmlib.installed_models_cached
        self._orig_once = orch._call_agent_once

    def tearDown(self):
        orch._fallback_steps = self._orig_steps
        orch.lmlib.installed_models_cached = self._orig_installed
        orch._call_agent_once = self._orig_once
        shutil.rmtree(self.app_dir, ignore_errors=True)

    def _wire(self, steps, once):
        orch._fallback_steps = lambda cfg, agent: list(steps)
        orch.lmlib.installed_models_cached = lambda: ["tiny:1b"]
        orch._call_agent_once = once

    def test_local_rescue_emits_events_and_bumps_count(self):
        def once(cfg, app, phase, rnd, agent, prompt, parent_call=None):
            if str(agent).startswith("local:"):
                return "rescued answer"
            raise orch.AgentError("Claude unavailable — provider banner")

        self._wire(["local:tiny:1b"], once)
        text = orch.call_agent(self.cfg, "appx", "tech_specs", 1, "claude", "hi")
        self.assertIn("rescued answer", text)
        evts = evlib.read_events(self.app_dir, kinds=("agent_fallback",))
        self.assertEqual([e["status"] for e in evts], ["attempt", "rescued"])
        for e in evts:
            self.assertEqual(e["agent"], "claude")
            self.assertEqual(e["from_model"], "sonnet")
            self.assertEqual(e["to_model"], "local:tiny:1b")
            self.assertIn("unavailable", e["reason"])
        # The aggregate badge landed in agent_state.json on disk.
        st = orch.load_state(self.app_dir)
        self.assertEqual(st["fallback_counts"], {"claude": 1})

    def test_sibling_rescue_emits_events_and_bumps_count(self):
        def once(cfg, app, phase, rnd, agent, prompt, parent_call=None):
            if cfg.get("_claude_model_override") == "haiku":
                return "sibling answer"
            raise orch.AgentError("timed out")

        # "haiku" is NOT an installed ollama tag, so the ladder treats it as a
        # same-provider sibling model retry (via _patch_agent_model).
        self._wire(["haiku"], once)
        text = orch.call_agent(self.cfg, "appx", "tech_specs", 1, "claude", "hi")
        self.assertIn("sibling answer", text)
        evts = evlib.read_events(self.app_dir, kinds=("agent_fallback",))
        self.assertEqual([e["status"] for e in evts], ["attempt", "rescued"])
        self.assertEqual(evts[0]["to_model"], "haiku")
        st = orch.load_state(self.app_dir)
        self.assertEqual(st["fallback_counts"], {"claude": 1})

    def test_exhausted_ladder_emits_events_and_no_count(self):
        def once(cfg, app, phase, rnd, agent, prompt, parent_call=None):
            raise orch.AgentError("everything is down")

        self._wire(["local:tiny:1b"], once)
        with self.assertRaises(orch.AgentError):
            orch.call_agent(self.cfg, "appx", "tech_specs", 1, "claude", "hi")
        statuses = [e["status"] for e in
                    evlib.read_events(self.app_dir, kinds=("agent_fallback",))]
        self.assertEqual(statuses, ["attempt", "failed", "exhausted"])
        st = orch.load_state(self.app_dir)
        self.assertEqual(st.get("fallback_counts", {}), {})

    def test_not_pulled_step_emits_skipped(self):
        def once(cfg, app, phase, rnd, agent, prompt, parent_call=None):
            raise orch.AgentError("down")

        self._wire(["local:not-pulled:7b"], once)
        with self.assertRaises(orch.AgentError):
            orch.call_agent(self.cfg, "appx", "tech_specs", 1, "claude", "hi")
        statuses = [e["status"] for e in
                    evlib.read_events(self.app_dir, kinds=("agent_fallback",))]
        self.assertEqual(statuses, ["skipped", "exhausted"])

    def test_counts_accumulate_across_rescues(self):
        def once(cfg, app, phase, rnd, agent, prompt, parent_call=None):
            if str(agent).startswith("local:"):
                return "ok"
            raise orch.AgentError("down")

        self._wire(["local:tiny:1b"], once)
        orch.call_agent(self.cfg, "appx", "tech_specs", 1, "claude", "hi")
        orch.call_agent(self.cfg, "appx", "tech_specs", 2, "claude", "hi")
        st = orch.load_state(self.app_dir)
        self.assertEqual(st["fallback_counts"], {"claude": 2})

    def test_bump_without_state_or_dir_is_noop(self):
        orch._bump_fallback_count({}, "claude")   # must never raise
        orch._bump_fallback_count({"_state": {}}, "claude")


if __name__ == "__main__":
    unittest.main()
