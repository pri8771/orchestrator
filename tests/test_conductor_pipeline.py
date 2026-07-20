"""V3 7.11: pipeline preset activation wired into the conductor — the request
marker (GUI path), --pipeline (CLI path), reconcile-replay crash-safety, the
routing/goal-manifest overlay actually taking effect, and the "a live run
holds its own copy" isolation guarantee.
"""
import json
import os
import shutil
import tempfile
import unittest
import unittest.mock

import conductor as cond
import pipeline_presets as pp


def _quiet(*_a, **_k):
    pass


PRESET = {
    "preset_name": "Brainstorm to Plan",
    "routing": {"artifact_routes": {}, "rules": [
        {"match": {"artifact_type": "idea"}, "strategy": "one",
         "targets": ["research"]}]},
    "goal_manifest": {"goal": {"doc_gap_empty": True}, "quiescence_cycles": 5},
    "seed": {"section": "ideas", "prompt_template": "seed: {{idea}}"},
    "ui": {"nodes": []},
}


class _Base(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.presets_dir = tempfile.mkdtemp()
        self.preset_path = os.path.join(self.presets_dir, "p.json")
        with open(self.preset_path, "w") as fh:
            json.dump(PRESET, fh)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)
        shutil.rmtree(self.presets_dir, ignore_errors=True)

    def _write_preset(self, obj):
        with open(self.preset_path, "w") as fh:
            json.dump(obj, fh)

    def _request(self, path=None):
        os.makedirs(cond.conductor_dir(self.root), exist_ok=True)
        with open(cond.pipeline_request_path(self.root), "w") as fh:
            json.dump({"preset_path": path or self.preset_path}, fh)


class TestConsumePipelineRequest(_Base):
    def test_no_marker_is_a_noop(self):
        state = cond.default_state()
        out = cond._consume_pipeline_request(self.root, state, emit=_quiet)
        self.assertIsNone(out["pipeline"])

    def test_valid_request_activates_and_ledgers(self):
        self._request()
        state = cond.default_state()
        out = cond._consume_pipeline_request(self.root, state, emit=_quiet)
        self.assertIsNotNone(out["pipeline"])
        self.assertEqual(out["pipeline"]["preset_name"], "Brainstorm to Plan")
        led = cond.read_ledger(self.root)
        self.assertTrue(any(r.get("decision") == "pipeline_loaded"
                            for r in led))
        # marker consumed
        self.assertFalse(os.path.exists(cond.pipeline_request_path(self.root)))

    def test_invalid_preset_refuses_and_ledgers_failure(self):
        self._write_preset({"preset_name": "bad"})   # missing everything else
        self._request()
        state = cond.default_state()
        out = cond._consume_pipeline_request(self.root, state, emit=_quiet)
        self.assertIsNone(out["pipeline"])
        led = cond.read_ledger(self.root)
        self.assertTrue(any(r.get("decision") == "pipeline_load_failed"
                            for r in led))
        self.assertFalse(os.path.exists(cond.pipeline_request_path(self.root)))

    def test_invalid_request_never_disturbs_a_prior_active_preset(self):
        self._request()
        state = cond._consume_pipeline_request(self.root, cond.default_state(),
                                               emit=_quiet)
        prior = state["pipeline"]
        self._write_preset({"preset_name": "broken"})
        self._request()
        state = cond._consume_pipeline_request(self.root, state, emit=_quiet)
        self.assertEqual(state["pipeline"], prior)   # unchanged, not cleared


class TestReconcileReplaysPipeline(_Base):
    def test_uncursored_pipeline_loaded_line_is_replayed(self):
        cond.ledger_append(self.root, {
            "v": 1, "ts": 1.0, "stage": "evaluating", "session": None,
            "decision": "pipeline_loaded",
            "detail": {"preset_path": self.preset_path,
                      "preset_name": "Brainstorm to Plan"}})
        state = cond.default_state()   # cursor 0, pipeline None
        state = cond.reconcile_on_start(self.root, state, emit=_quiet)
        self.assertIsNotNone(state["pipeline"])
        self.assertEqual(state["pipeline"]["preset_name"],
                         "Brainstorm to Plan")

    def test_replay_of_a_now_broken_preset_file_does_not_crash(self):
        cond.ledger_append(self.root, {
            "v": 1, "ts": 1.0, "stage": "evaluating", "session": None,
            "decision": "pipeline_loaded",
            "detail": {"preset_path": "/nonexistent/gone.json",
                      "preset_name": "Gone"}})
        state = cond.reconcile_on_start(self.root, cond.default_state(),
                                        emit=_quiet)
        self.assertIsNone(state["pipeline"])   # not reactivated, no crash


class TestRunHoldsItsOwnCopy(_Base):
    def test_editing_the_file_after_activation_does_not_change_state(self):
        self._request()
        state = cond._consume_pipeline_request(self.root, cond.default_state(),
                                               emit=_quiet)
        original_name = state["pipeline"]["preset_name"]
        # live edit to the SAME file, no new request marker dropped
        self._write_preset(dict(PRESET, preset_name="Mutated!"))
        # a normal poll must never re-read the preset file on its own
        state2 = cond.full_poll(self.root, state, emit=_quiet)
        self.assertEqual(state2["pipeline"]["preset_name"], original_name)


class TestRoutingOverlay(_Base):
    def _session(self, sid):
        app_dir = os.path.join(self.root, sid)
        os.makedirs(app_dir, exist_ok=True)
        return app_dir

    def test_active_preset_supplies_routing_for_every_section(self):
        import artifacts as artlib
        self._session("proj/ideas/chat-1")
        state = cond.default_state()
        state["pipeline"], _ = pp.validate_preset(
            PRESET, pp.known_sections_in_workspace(cond._sections_dir()))
        meta = {"id": "a1", "artifact_type": "idea", "content_hash": "h1",
               "lineage": [], "hop_count": 0, "status": "final"}
        minted = []
        with unittest.mock.patch.object(artlib, "list_artifacts",
                                        lambda *a, **k: [meta]), \
                unittest.mock.patch.object(artlib, "is_admissible",
                                           lambda *a, **k: True), \
                unittest.mock.patch.object(artlib, "lineage_index",
                                           lambda *a, **k: {}), \
                unittest.mock.patch("sessions.mint_delegation_session",
                                    lambda *a, **k: minted.append(a) or "x"):
            cond.route_engine(self.root, state, ["proj/ideas/chat-1"],
                              emit=_quiet)
        # the preset's rule (idea -> research) fired with NO routing.json
        # ever written for the 'ideas' section — proof the preset, not the
        # normal per-section config, drove this route.
        self.assertEqual(len(minted), 1)
        self.assertEqual(minted[0][2], "research")

    def test_ui_block_present_or_absent_routes_identically(self):
        with_ui, _ = pp.validate_preset(PRESET,
            pp.known_sections_in_workspace(cond._sections_dir()))
        no_ui_src = dict(PRESET)
        del no_ui_src["ui"]
        without_ui, _ = pp.validate_preset(no_ui_src,
            pp.known_sections_in_workspace(cond._sections_dir()))
        cfg_a = pp.as_route_config(with_ui)
        cfg_b = pp.as_route_config(without_ui)
        self.assertEqual(cfg_a.routes, cfg_b.routes)
        self.assertEqual(cfg_a.rules, cfg_b.rules)


class TestNoDoubleLedger(_Base):
    def test_pipeline_flag_on_restart_with_unchanged_preset_is_a_noop(self):
        rc = cond.main(["--root", self.root, "--once",
                       "--pipeline", self.preset_path])
        self.assertEqual(rc, 0)
        led_after_first = cond.read_ledger(self.root)
        loaded_count_1 = sum(1 for r in led_after_first
                            if r and r.get("decision") == "pipeline_loaded")
        self.assertEqual(loaded_count_1, 1)
        # a second launch with the SAME preset file must not re-ledger
        rc2 = cond.main(["--root", self.root, "--once",
                        "--pipeline", self.preset_path])
        self.assertEqual(rc2, 0)
        led_after_second = cond.read_ledger(self.root)
        loaded_count_2 = sum(1 for r in led_after_second
                            if r and r.get("decision") == "pipeline_loaded")
        self.assertEqual(loaded_count_2, 1,
                         "an unchanged preset must not double-ledger "
                         "'pipeline_loaded' on restart")

    def test_a_genuinely_changed_preset_at_the_same_path_does_reledger(self):
        cond.main(["--root", self.root, "--once",
                  "--pipeline", self.preset_path])
        self._write_preset(dict(PRESET, preset_name="Different Pipeline"))
        cond.main(["--root", self.root, "--once",
                  "--pipeline", self.preset_path])
        led = cond.read_ledger(self.root)
        loaded = [r for r in led if r and r.get("decision") == "pipeline_loaded"]
        self.assertEqual(len(loaded), 2)
        self.assertEqual(loaded[1]["detail"]["preset_name"],
                         "Different Pipeline")


class TestGoalManifestNotAliased(_Base):
    def test_mutating_the_source_dict_after_validation_does_not_corrupt_it(self):
        data = dict(PRESET, goal_manifest={
            "goal": {"doc_gap_empty": True},
            "budgets": {"turns": 5}, "stall": {"vote_undecided_limit": 2}})
        norm, err = pp.validate_preset(
            data, pp.known_sections_in_workspace(cond._sections_dir()))
        self.assertIsNone(err)
        # mutate the CALLER's own dict after validation — a GUI holding a
        # live-editing preset dict does exactly this on every keystroke.
        data["goal_manifest"]["budgets"]["turns"] = 999
        data["goal_manifest"]["stall"]["vote_undecided_limit"] = 999
        self.assertEqual(norm["goal_manifest"]["budgets"]["turns"], 5)
        self.assertEqual(
            norm["goal_manifest"]["stall"]["vote_undecided_limit"], 2)


class TestCLIFlag(_Base):
    def test_pipeline_flag_activates_before_first_poll(self):
        rc = cond.main(["--root", self.root, "--once",
                       "--pipeline", self.preset_path])
        self.assertEqual(rc, 0)
        state = cond.load_conductor_state(self.root)
        self.assertIsNotNone(state["pipeline"])
        self.assertEqual(state["pipeline"]["preset_name"],
                         "Brainstorm to Plan")

    def test_invalid_pipeline_flag_refuses_to_start(self):
        self._write_preset({"preset_name": "bad"})
        rc = cond.main(["--root", self.root, "--once",
                       "--pipeline", self.preset_path])
        self.assertEqual(rc, 2)
        state = cond.load_conductor_state(self.root)
        self.assertIsNone(state["pipeline"])


if __name__ == "__main__":
    unittest.main()
