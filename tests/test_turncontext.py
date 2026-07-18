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
    scan, SCANNED, ALLOWED_WRITTEN_KEYS, TRANCHE1_MIGRATED, HERE)

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


class TestViewSemantics(unittest.TestCase):
    def test_property_write_lands_in_dict(self):
        cfg = {}
        ctx = turncontext.TurnContext(cfg)
        for prop, key in BAND_B.items():
            setattr(ctx, prop, "v:" + prop)
            self.assertEqual(cfg[key], "v:" + prop,
                             "property %s must write %s" % (prop, key))

    def test_dict_write_visible_through_property(self):
        cfg = {}
        ctx = turncontext.TurnContext(cfg)
        for prop, key in BAND_B.items():
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
        for prop in BAND_B:
            self.assertIsNone(getattr(ctx, prop))
        self.assertEqual(cfg, {}, "reading the view must not seed keys")


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
        # migrated keys have zero raw writes left in the engine files
        # (_build_dir keeps exactly its one band-C lane write).
        self.assertTrue(os.path.exists(os.path.join(HERE, "turncontext.py")))
        self.assertNotIn("turncontext.py", SCANNED)
        inv = scan()
        for key in sorted(TRANCHE1_MIGRATED):
            self.assertFalse(
                inv.get(key, {}).get("writes"),
                "raw write of migrated key %s reintroduced at %s"
                % (key, inv.get(key, {}).get("writes")))
        self.assertEqual(len(inv["_build_dir"]["writes"]), 1)

    def test_migrated_keys_left_the_allowlist(self):
        # The monotonic gate is what catches reintroductions — that only
        # works if the migrated keys are actually gone from the allowlist.
        self.assertFalse(TRANCHE1_MIGRATED & ALLOWED_WRITTEN_KEYS)


if __name__ == "__main__":
    unittest.main()
