"""V3 board 3.5: cross-section cast references — "section:id" guests with
loud, dropped danglers (never a fabricated stand-in).
"""
import json
import os
import shutil
import tempfile
import unittest

import roles as roleslib


class RefBase(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="orch_xsec_")
        self.addCleanup(shutil.rmtree, self.root, True)
        d = os.path.join(self.root, "research")
        os.makedirs(d)
        with open(os.path.join(d, "roles.json"), "w", encoding="utf-8") as fh:
            json.dump({
                "roles": [{"id": "investigator", "name": "Investigator",
                           "focus": "digs for evidence"}],
                "personalities": [{"id": "visionary", "name": "The Visionary",
                                   "style": "asks what if"}],
            }, fh)

    def resolve(self, ids):
        events = []
        kept, guests = roleslib.resolve_phase_role_refs(
            ids, self.root, on_missing=lambda r, why: events.append((r, why)))
        return kept, guests, events


class TestResolution(RefBase):
    def test_roles_pool_ref_joins_as_guest(self):
        kept, guests, events = self.resolve(["product",
                                             "research:investigator"])
        self.assertEqual(kept, ["product", "research:investigator"])
        self.assertEqual(events, [])
        (g,) = guests
        self.assertEqual(g["id"], "research:investigator",
                         "the FULL ref is the guest id — no collision")
        self.assertEqual(g["name"], "Investigator")
        self.assertEqual(g["focus"], "digs for evidence")

    def test_personality_pool_ref_wraps_as_role(self):
        _k, guests, events = self.resolve(["research:visionary"])
        self.assertEqual(events, [])
        (g,) = guests
        self.assertEqual(g["focus"], "asks what if",
                         "style becomes focus for a personality guest")

    def test_roles_pool_wins_over_personalities(self):
        d = os.path.join(self.root, "both")
        os.makedirs(d)
        with open(os.path.join(d, "roles.json"), "w", encoding="utf-8") as fh:
            json.dump({
                "roles": [{"id": "x", "name": "Role X", "focus": "role"}],
                "personalities": [{"id": "x", "name": "Pers X",
                                   "style": "pers"}]}, fh)
        _k, guests, _e = self.resolve(["both:x"])
        self.assertEqual(guests[0]["name"], "Role X",
                         "roles pool first — deterministic order")

    def test_plain_ids_pass_through_byte_identically(self):
        ids = ["product", "qa"]
        kept, guests, events = self.resolve(ids)
        self.assertEqual(kept, ids)
        self.assertEqual(guests, [])
        self.assertEqual(events, [])
        self.assertEqual(roleslib.resolve_phase_role_refs(None, self.root),
                         (None, []))


class TestDanglers(RefBase):
    def test_unknown_section_banners_and_drops(self):
        kept, guests, events = self.resolve(["nope:x", "product"])
        self.assertEqual(kept, ["product"])
        self.assertEqual(guests, [])
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0][0], "nope:x")
        self.assertIn("unknown section", events[0][1])

    def test_unknown_id_banners_and_drops(self):
        kept, _g, events = self.resolve(["research:ghost"])
        self.assertEqual(kept, [])
        self.assertIn("no role or personality", events[0][1])

    def test_missing_roles_json_banners(self):
        os.makedirs(os.path.join(self.root, "bare"))
        _k, _g, events = self.resolve(["bare:x"])
        self.assertIn("no roles.json", events[0][1])

    def test_corrupt_roles_json_banners(self):
        d = os.path.join(self.root, "broken")
        os.makedirs(d)
        with open(os.path.join(d, "roles.json"), "w") as fh:
            fh.write("{nope")
        _k, _g, events = self.resolve(["broken:x"])
        self.assertIn("unreadable", events[0][1])

    def test_no_placeholder_is_ever_invented(self):
        kept, guests, _e = self.resolve(["nope:x"])
        self.assertEqual((kept, guests), ([], []),
                         "a dangling ref must vanish, not become a stand-in")


class TestIsolationAndDeterminism(RefBase):
    def test_source_section_pools_never_mutate(self):
        _k, guests, _e = self.resolve(["research:investigator"])
        guests[0]["name"] = "CORRUPTED"
        guests[0]["id"] = "CORRUPTED"
        _k2, guests2, _e2 = self.resolve(["research:investigator"])
        self.assertEqual(guests2[0]["name"], "Investigator")
        with open(os.path.join(self.root, "research", "roles.json"),
                  encoding="utf-8") as fh:
            on_disk = json.load(fh)
        self.assertEqual(on_disk["roles"][0]["name"], "Investigator")

    def test_guest_joins_assign_personas_deterministically(self):
        _k, guests, _e = self.resolve(["research:investigator"])
        roles = list(roleslib.DEFAULT_ROLES) + guests
        ids = ["research:investigator"]
        a = roleslib.assign_personas(2, ["codex"],
                                     roleslib.DEFAULT_PERSONALITIES,
                                     roles, ids)
        b = roleslib.assign_personas(2, ["codex"],
                                     roleslib.DEFAULT_PERSONALITIES,
                                     roles, ids)
        self.assertEqual(a["codex"]["role"]["name"], "Investigator")
        self.assertEqual(a, b, "same phase index -> identical cast (resume)")

    def test_empty_pool_after_drops_falls_back(self):
        kept, guests, _e = self.resolve(["nope:x"])
        out = roleslib.assign_personas(0, ["codex"],
                                       roleslib.DEFAULT_PERSONALITIES,
                                       list(roleslib.DEFAULT_ROLES) + guests,
                                       kept)
        self.assertIn("role", out["codex"],
                      "the existing empty-pool fallback still applies")


if __name__ == "__main__":
    unittest.main()
