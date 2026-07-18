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
    TRANCHE_C1_MIGRATED, HERE)

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
        for key in sorted(TRANCHE1_MIGRATED | TRANCHE_C1_MIGRATED):
            self.assertFalse(
                inv.get(key, {}).get("writes"),
                "raw write of migrated key %s reintroduced at %s"
                % (key, inv.get(key, {}).get("writes")))
        self.assertEqual(len(inv["_resolved"]["writes"]), 4)

    def test_migrated_keys_left_the_allowlist(self):
        # The monotonic gate is what catches reintroductions — that only
        # works if the migrated keys are actually gone from the allowlist.
        self.assertFalse(
            (TRANCHE1_MIGRATED | TRANCHE_C1_MIGRATED) & ALLOWED_WRITTEN_KEYS)


if __name__ == "__main__":
    unittest.main()
