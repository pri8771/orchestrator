"""Tests for models.local_active_limit (roster participation cap):

- a positive limit caps the local roster expansion at N participants
- installed models win the slots first, preserving roster order
- 0 / missing / garbage values mean "no limit" (legacy behavior)
- the cap composes with runtime.skip_uninstalled_local_models
- every default-roster entry has a curated local_models.json record
"""
import json
import os
import unittest

import orchestrator as orch

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

CLOUD = ["codex", "claude", "gemini"]


def _cfg(roster, limit=None, installed=None, **runtime):
    c = {
        "agents": {"ollama_enabled": True},
        "models": {"ollama": "", "ollama_roster": roster},
        "runtime": dict(runtime),
    }
    if limit is not None:
        c["models"]["local_active_limit"] = limit
    if installed is not None:
        c["_installed_ollama_models"] = list(installed)
    return c


def _locals(cfg):
    return [a for a in orch.enabled_agents(cfg) if a.startswith("local:")]


class TestActiveLimitCaps(unittest.TestCase):
    def test_limit_caps_roster_expansion(self):
        c = _cfg("a:1, b:1, c:1, d:1", limit=2, installed=["a:1", "b:1"])
        self.assertEqual(_locals(c), ["local:a:1", "local:b:1"])

    def test_installed_first_in_roster_order(self):
        # c:1 and d:1 are the installed ones — they jump the queue, but keep
        # their relative roster order; the remainder fills any leftover slots.
        c = _cfg("a:1, b:1, c:1, d:1", limit=3, installed=["d:1", "c:1"])
        self.assertEqual(_locals(c), ["local:c:1", "local:d:1", "local:a:1"])

    def test_limit_larger_than_roster_is_noop(self):
        c = _cfg("a:1, b:1", limit=5, installed=["a:1"])
        self.assertEqual(_locals(c), ["local:a:1", "local:b:1"])

    def test_note_emitted_once_and_memoized(self):
        c = _cfg("a:1, b:1, c:1", limit=1, installed=["b:1"])
        self.assertEqual(_locals(c), ["local:b:1"])
        self.assertTrue(c.get("_noted_local_active_limit"))
        # second call: same answer, no state reset
        self.assertEqual(_locals(c), ["local:b:1"])

    def test_cloud_agents_never_affected(self):
        c = _cfg("a:1, b:1, c:1", limit=1, installed=["a:1"])
        self.assertEqual([a for a in orch.enabled_agents(c)
                          if not a.startswith("local:")], CLOUD)


class TestZeroOrMissingIsUnlimited(unittest.TestCase):
    def test_zero_means_no_limit(self):
        c = _cfg("a:1, b:1, c:1", limit=0, installed=[])
        self.assertEqual(_locals(c), ["local:a:1", "local:b:1", "local:c:1"])

    def test_missing_means_no_limit(self):
        c = _cfg("a:1, b:1, c:1", installed=[])
        self.assertEqual(_locals(c), ["local:a:1", "local:b:1", "local:c:1"])

    def test_negative_and_garbage_mean_no_limit(self):
        for bad in (-3, "not-a-number", None):
            c = _cfg("a:1, b:1", limit=bad, installed=[])
            self.assertEqual(_locals(c), ["local:a:1", "local:b:1"],
                             "limit=%r must behave as unlimited" % (bad,))

    def test_string_limit_from_yaml_is_honored(self):
        # The tiny YAML reader may hand the value through as a string.
        c = _cfg("a:1, b:1, c:1", limit="2", installed=["a:1", "c:1"])
        self.assertEqual(_locals(c), ["local:a:1", "local:c:1"])


class TestComposesWithSkipUninstalled(unittest.TestCase):
    def test_skip_uninstalled_then_cap(self):
        c = _cfg("a:1, b:1, c:1, d:1", limit=2, installed=["b:1", "c:1", "d:1"],
                 skip_uninstalled_local_models=True)
        # a:1 dropped by the skip filter, then the cap keeps the first 2.
        self.assertEqual(_locals(c), ["local:b:1", "local:c:1"])

    def test_cap_alone_keeps_uninstalled_when_slots_remain(self):
        c = _cfg("a:1, b:1, c:1", limit=2, installed=["c:1"])
        self.assertEqual(_locals(c), ["local:c:1", "local:a:1"])


class TestDefaultRosterCurated(unittest.TestCase):
    def test_every_default_roster_entry_is_in_registry(self):
        # config.yaml's shipped roster must never reference a model the curated
        # registry (local_models.json) doesn't know about.
        with open(os.path.join(HERE, "local_models.json"), encoding="utf-8") as fh:
            ids = {m["id"] for m in json.load(fh)["models"]}
        with open(os.path.join(HERE, "config.yaml"), encoding="utf-8") as fh:
            text = fh.read()
        roster_line = next(line for line in text.splitlines()
                           if line.strip().startswith("ollama_roster:"))
        roster = orch._split_local_roster(roster_line.split(":", 1)[1].strip().strip('"'))
        self.assertTrue(roster)
        self.assertEqual([m for m in roster if m not in ids], [])


if __name__ == "__main__":
    unittest.main()
