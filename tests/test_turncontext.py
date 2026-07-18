"""V3 board 2.3b: the TurnContext wrapper view — tranche 1 (band B).

Proves the three load-bearing properties of the design: the view is a VIEW
(same dict, both access styles interchangeable), getters have .get
semantics and never seed keys (containment latches like
`"_base_models" not in cfg` must not re-trigger), and end_phase() clears
exactly the historical nine-key block. Plus the gate retarget: the migrated
band-B keys have no raw writes left in the scanned engine files.
"""
import os
import sys
import unittest

import turncontext

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_cfg_key_inventory import (  # noqa: E402
    scan, SCANNED, ALLOWED_WRITTEN_KEYS, TRANCHE1_MIGRATED,
    TRANCHE_C1_MIGRATED, TRANCHE_C2_MIGRATED, TRANCHE_D_MIGRATED, HERE)

BAND_A_SWEEP = {
    "app_dir": "_app_dir", "state": "_state",
    "original_prompt": "_original_prompt", "workflow_name": "_workflow_name",
    "workflow_target": "_workflow_target",
    "workflow_verify_spec": "_workflow_verify_spec", "roles": "_roles",
    "personalities": "_personalities",
    "agent_role_overrides": "_agent_role_overrides",
    "role_by_id": "_role_by_id", "autonomy": "_autonomy",
    "round_multiplier": "_round_multiplier", "completeness": "_completeness",
    "target_path": "_target_path", "target_paths": "_target_paths",
    "tech_stack_block": "_tech_stack_block", "url_context": "_url_context",
    "budget": "_budget", "deadline": "_deadline",
    "base_models": "_base_models", "base_resolved": "_base_resolved",
    "explicit_app": "_explicit_app",
    "gemini_disabled_reason": "_gemini_disabled_reason",
    "phase_deadline": "_phase_deadline",
    "prior_discussions": "_prior_discussions",
    "checked_any_agent_runnable": "_checked_any_agent_runnable",
    "gemini_unavailable": "_gemini_unavailable",
    "installed_ollama_models": "_installed_ollama_models",
    "iter_verify_toolchain_absent": "_iter_verify_toolchain_absent",
    "noted_local_active_limit": "_noted_local_active_limit",
    "noted_local_lane_skip": "_noted_local_lane_skip",
    "noted_local_ram_gate": "_noted_local_ram_gate",
    "noted_ollama_sprint_skip": "_noted_ollama_sprint_skip",
    "noted_ollama_uninstalled_skip": "_noted_ollama_uninstalled_skip",
    "warned_no_git_repo": "_warned_no_git_repo",
    "sim_ctx": "_sim_ctx",
}

BAND_C_PROPS = {
    "session": "_session",
    "claude_model_override": "_claude_model_override",
    "resolved": "_resolved",
}

BAND_BPRIME = {
    "phase_key": "_phase_key",
    "routed_turn_timeout": "_routed_turn_timeout",
    "routed_rounds": "_routed_rounds",
    "phase_instructions": "_phase_instructions",
    "role_routing": "_role_routing",
    "noted_indep_grader": "_noted_indep_grader",
    "routing": "_routing",
}

BAND_B = {
    "turn_timeout": "_turn_timeout",
    "verify_context": "_verify_context",
    "phase_exemplar": "_phase_exemplar",
    "phase_playbook": "_phase_playbook",
    "knowledge": "_knowledge",
    "allow_writes": "_allow_writes",
    "build_dir": "_build_dir",
    "session_cwd": "_session_cwd",
    "prior_disc_cap": "_prior_disc_cap",
    "target_digest": "_target_digest",
    "read_dir": "_read_dir",
}

END_PHASE_CLEARED = {
    "_allow_writes": False, "_build_dir": None, "_session_cwd": None,
    "_prior_disc_cap": None, "_phase_playbook": "", "_knowledge": "",
    "_read_dir": None, "_target_digest": "", "_verify_context": "",
}


ALL_PROPS = {}
ALL_PROPS.update(BAND_B)
ALL_PROPS.update(BAND_C_PROPS)
ALL_PROPS.update(BAND_BPRIME)
ALL_PROPS.update(BAND_A_SWEEP)


class TestViewSemantics(unittest.TestCase):
    def test_property_write_lands_in_dict(self):
        cfg = {}
        ctx = turncontext.TurnContext(cfg)
        for prop, key in ALL_PROPS.items():
            setattr(ctx, prop, "v:" + prop)
            self.assertEqual(cfg[key], "v:" + prop,
                             "property %s must write %s" % (prop, key))

    def test_dict_write_visible_through_property(self):
        cfg = {}
        ctx = turncontext.TurnContext(cfg)
        for prop, key in ALL_PROPS.items():
            cfg[key] = "d:" + key
            self.assertEqual(getattr(ctx, prop), "d:" + key)

    def test_two_views_share_one_dict(self):
        cfg = {}
        a, b = turncontext.TurnContext(cfg), turncontext.TurnContext(cfg)
        a.knowledge = "shared"
        self.assertEqual(b.knowledge, "shared")

    def test_missing_key_reads_none_and_seeds_nothing(self):
        # T1 (.get semantics: the `ctx.target_digest or build_…` memo idiom)
        # and T4 (no seeding: `"_x" not in cfg` latches must not re-trigger).
        cfg = {}
        ctx = turncontext.TurnContext(cfg)
        for prop in ALL_PROPS:
            self.assertIsNone(getattr(ctx, prop))
        self.assertEqual(cfg, {}, "reading the view must not seed keys")

    def test_resolved_getter_returns_live_object(self):
        # Band-C patches copy-before-mutate THEMSELVES; the getter must
        # hand back the live dict so sibling aliasing stays observable.
        cfg = {"_resolved": {"claude_model": "x"}}
        self.assertIs(turncontext.TurnContext(cfg).resolved,
                      cfg["_resolved"])


class TestThreadCopy(unittest.TestCase):
    def test_thread_copy_contract(self):
        shared = {"_agent_health": {}, "_claude_sessions": {},
                  "_codex_sessions": {}}
        cfg = dict(shared, _build_dir="/phase", plain="x")
        c = turncontext.TurnContext(cfg).thread_copy(
            health_key="lane1", stateless=True)
        self.assertIsNot(c, cfg)                    # it is a copy
        self.assertEqual(c["_health_key"], "lane1")
        self.assertNotIn("_health_key", cfg)        # write stayed on the copy
        self.assertIsNone(c["_session"])
        self.assertNotIn("_session", cfg)
        for k in shared:                            # band D: IDENTITY, not equality
            self.assertIs(c[k], cfg[k])
        self.assertEqual(c["_build_dir"], "/phase")  # band B inherited by value
        turncontext.TurnContext(c).claude_model_override = "opus"
        self.assertNotIn("_claude_model_override", cfg)
        c2 = turncontext.TurnContext(cfg).thread_copy()   # None means ABSENT
        self.assertNotIn("_health_key", c2)
        self.assertNotIn("_session", c2)
        self.assertNotIn("_drop_prior_discussions", c2)

    def test_drop_prior_discussions_flag(self):
        cfg = {}
        c = turncontext.TurnContext(cfg).thread_copy(
            drop_prior_discussions=True)
        self.assertIs(c["_drop_prior_discussions"], True)
        self.assertNotIn("_drop_prior_discussions", cfg)


class TestSessionIdChannel(unittest.TestCase):
    def test_stash_take_roundtrip_and_exactly_once(self):
        cfg = {}
        ctx = turncontext.TurnContext(cfg)
        ctx.stash_new_session_id("sid-1")
        self.assertEqual(cfg["_new_session_id"], "sid-1")
        self.assertEqual(ctx.take_new_session_id(), "sid-1")
        self.assertNotIn("_new_session_id", cfg, "take must POP, not read")
        self.assertIsNone(ctx.take_new_session_id(),
                          "second take finds the channel empty")


class TestPatchAgentModel(unittest.TestCase):
    def test_patch_is_isolated_from_parent_resolved(self):
        # T4 pin: patching a per-call copy must never mutate the parent's
        # _resolved object — copies alias it until the patch replaces it.
        parent = {"_resolved": {"claude_model": "a", "codex_model": "b"}}
        original = parent["_resolved"]
        child = turncontext.TurnContext(parent).thread_copy()
        turncontext.TurnContext(child).patch_agent_model("codex", "gpt-x")
        self.assertIs(parent["_resolved"], original)
        self.assertEqual(parent["_resolved"]["codex_model"], "b")
        self.assertIsNot(child["_resolved"], original)
        self.assertEqual(child["_resolved"]["codex_model"], "gpt-x")

    def test_claude_patch_uses_override_key(self):
        child = {"_resolved": {"claude_model": "a"}}
        turncontext.TurnContext(child).patch_agent_model("claude", "opus")
        self.assertEqual(child["_claude_model_override"], "opus")
        self.assertEqual(child["_resolved"]["claude_model"], "a",
                         "claude patches ride the override key, not resolved")


class TestEndPhase(unittest.TestCase):
    def test_clears_exactly_the_nine_keys(self):
        cfg = {k: "live" for k in BAND_B.values()}
        cfg["_agent_health"] = {"codex": 1}      # band D — must not be touched
        cfg["_turn_timeout"] = 480
        cfg["_phase_exemplar"] = "exemplar"
        before = dict(cfg)
        turncontext.TurnContext(cfg).end_phase()
        changed = {k for k in before if cfg[k] != before[k]}
        self.assertEqual(changed, set(END_PHASE_CLEARED),
                         "end_phase must change exactly the nine-key block")
        for k, v in END_PHASE_CLEARED.items():
            self.assertEqual(cfg[k], v)
        self.assertEqual(set(cfg), set(before), "no keys added or removed")
        self.assertEqual(cfg["_turn_timeout"], 480)
        self.assertEqual(cfg["_phase_exemplar"], "exemplar")
        self.assertEqual(cfg["_agent_health"], {"codex": 1})


class TestBandDIdentity(unittest.TestCase):
    def test_session_map_returns_the_live_seeded_dict(self):
        cfg = {}
        ctx = turncontext.TurnContext(cfg)
        m = ctx.session_map("claude")
        self.assertIs(m, cfg["_claude_sessions"], "must be the live object")
        self.assertIs(ctx.session_map("claude"), m, "stable across calls")
        self.assertIs(turncontext.TurnContext(cfg).session_map("codex"),
                      cfg["_codex_sessions"])

    def test_aliasing_survives_thread_copies_end_to_end(self):
        # The whole point of band D: a session stored through one thread
        # copy is visible through every other, because all copies alias
        # ONE map that existed before the copies were taken.
        cfg = {}
        turncontext.TurnContext(cfg).session_map("claude")   # ensure-before-copy
        a = turncontext.TurnContext(cfg).thread_copy(health_key="lane-a")
        b = turncontext.TurnContext(cfg).thread_copy(health_key="lane-b")
        turncontext.TurnContext(a).session_map("claude")["k"] = "sid-1"
        self.assertEqual(
            turncontext.TurnContext(b).session_map("claude")["k"], "sid-1")
        self.assertIs(a["_claude_sessions"], b["_claude_sessions"])

    def test_reset_agent_health_replaces_never_clears(self):
        cfg = {"_agent_health": {"codex": "cooling"}}
        old_map = cfg["_agent_health"]
        copy_before = turncontext.TurnContext(cfg).thread_copy()
        turncontext.TurnContext(cfg).reset_agent_health()
        self.assertIsNot(cfg["_agent_health"], old_map)
        self.assertEqual(cfg["_agent_health"], {})
        self.assertIs(copy_before["_agent_health"], old_map,
                      "pre-reset copies must keep aliasing the OLD map")
        self.assertEqual(copy_before["_agent_health"]["codex"], "cooling")

    def test_no_plain_banded_getters_exist(self):
        # Shape pin: the band-D MAPS must stay method-only. A future plain
        # property invites a copying getter, which silently breaks
        # cooldown/session sharing. (`routing` is deliberately exempt: a
        # rebindable reference, never mutated in place.)
        for name in ("agent_health", "claude_sessions", "codex_sessions"):
            self.assertFalse(hasattr(turncontext.TurnContext, name),
                             "band-D key %r must not be a property" % name)

    def test_ensure_before_copy_ordering_in_phase_routing(self):
        # After _apply_phase_routing, the returned phase copy's band-D maps
        # must BE the original's (the :5150 comment made structural).
        import orchestrator as orch
        cfg = {"agents": {"codex_enabled": True}, "runtime": {}}
        c = orch._apply_phase_routing(cfg, "design_discussion")
        self.assertIsNot(c, cfg)
        for k in ("_agent_health", "_claude_sessions", "_codex_sessions"):
            self.assertIn(k, cfg, "%s must be seeded on the ORIGINAL" % k)
            self.assertIs(c[k], cfg[k], "%s must alias across the copy" % k)


class TestNoteOnce(unittest.TestCase):
    def test_true_exactly_once_per_key(self):
        cfg = {}
        ctx = turncontext.TurnContext(cfg)
        self.assertTrue(ctx.note_once("routing_filter", "chat"))
        self.assertFalse(ctx.note_once("routing_filter", "chat"))
        self.assertTrue(ctx.note_once("routing_filter", "app_design"),
                        "a different suffix is a different latch")

    def test_key_bytes_match_the_four_historical_families(self):
        # note_once must build the exact keys the raw %-format code built,
        # or resumed runs would re-emit already-noted warnings.
        cfg = {}
        ctx = turncontext.TurnContext(cfg)
        ctx.note_once("ollama_reasoning_noop_cli", "chat")
        ctx.note_once("routing_filter", "chat")
        ctx.note_once("%s_noop" % "gemini_reasoning", "chat")
        ctx.note_once("ollama_override_shadowed", "chat")
        self.assertEqual(set(cfg), {
            "_noted_ollama_reasoning_noop_cli_%s" % "chat",
            "_noted_routing_filter_%s" % "chat",
            "_noted_%s_noop_%s" % ("gemini_reasoning", "chat"),
            "_noted_ollama_override_shadowed_%s" % "chat",
        })


class TestGateRetarget(unittest.TestCase):
    def test_turncontext_is_the_only_home_for_migrated_writes(self):
        # turncontext.py exists, is intentionally NOT scanned, and the
        # migrated keys have zero raw writes left in the engine files.
        # _resolved is NOT migrated: four band-A sites stay raw — two
        # base-resolution rebinds plus two workflow-override interior
        # chains (cfg["models"][...] = cfg["_resolved"][...] = ov[...])
        # the chained-assignment scanner fix surfaced. Only the per-call
        # patches moved into patch_agent_model.
        self.assertTrue(os.path.exists(os.path.join(HERE, "turncontext.py")))
        self.assertNotIn("turncontext.py", SCANNED)
        inv = scan()
        for key in sorted(TRANCHE1_MIGRATED | TRANCHE_C1_MIGRATED
                          | TRANCHE_C2_MIGRATED | TRANCHE_D_MIGRATED):
            self.assertFalse(
                inv.get(key, {}).get("writes"),
                "raw write of migrated key %s reintroduced at %s"
                % (key, inv.get(key, {}).get("writes")))

    def test_allowlist_is_zero(self):
        # The 2.3 end state: no raw cfg["_…"] write anywhere in the
        # scanned engine files is permitted, full stop. Growing this set
        # again is a design decision, not a convenience.
        self.assertEqual(ALLOWED_WRITTEN_KEYS, set())
        self.assertEqual(
            {k: v["writes"] for k, v in scan().items() if v["writes"]}, {})


if __name__ == "__main__":
    unittest.main()
