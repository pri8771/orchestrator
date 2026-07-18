"""V3 board 4.6 (engine side): @-mention delegation from a chat composer.

A leading "@<Section> [<tier>[ token=<hex>]] <question>" fires either a
Quick-take (one guest-persona turn in THIS session — an opinion, no
artifact) or a Deep-dive (a 4.4-minted sub-session running the target
section's real workflow; the brief returns via 4.4's inbox edge). The
parser is pure and tested directly; the two tiers are exercised through
the real _handle_chat_delegation dispatch against a nested session.
"""
import json
import os
import shutil
import tempfile
import unittest

import orchestrator as orch
import sessions as seslib
import workflows as wf

KEY = "chat"


# ---------------------------------------------------------------------------
# Pure parser + wire format.
# ---------------------------------------------------------------------------
class TestParseDelegation(unittest.TestCase):
    KNOWN = {"research": "research", "ideas": "ideas"}

    def test_tier_encoding_round_trip(self):
        p = orch.parse_delegation("@Research [deep-dive token=ab12] dig in",
                                  self.KNOWN)
        self.assertEqual((p["section"], p["tier"], p["token"], p["question"]),
                         ("research", "deep-dive", "ab12", "dig in"))
        q = orch.parse_delegation("@ideas [quick-take] a hunch", self.KNOWN)
        self.assertEqual((q["section"], q["tier"], q["token"]),
                         ("ideas", "quick-take", None))

    def test_absent_tier_defaults_to_safe_quick_take(self):
        p = orch.parse_delegation("@Research just answer this", self.KNOWN)
        self.assertEqual(p["tier"], "quick-take",
                         "the default tier must never auto-mint a sub-session")
        self.assertEqual(p["question"], "just answer this")

    def test_case_insensitive_section_by_id_or_title(self):
        self.assertEqual(
            orch.parse_delegation("@RESEARCH x", self.KNOWN)["section"],
            "research")

    def test_unknown_section_is_flagged_not_none(self):
        p = orch.parse_delegation("@Nope hello", self.KNOWN)
        self.assertFalse(p["known"])
        self.assertEqual(p["raw_section"], "Nope")

    def test_non_at_message_is_none(self):
        for msg in ("plain text", "email me @foo later", "", "  hi @x", "@"):
            self.assertIsNone(orch.parse_delegation(msg, self.KNOWN), msg)

    def test_tier_keyword_is_case_insensitive(self):
        p = orch.parse_delegation("@Research [DEEP-DIVE] q", self.KNOWN)
        self.assertEqual(p["tier"], "deep-dive")
        self.assertEqual(p["question"], "q",
                         "the bracket must not leak into the question")

    def test_multi_word_title_matched_by_longest_key(self):
        known = {"dr": "dr", "deep research": "dr", "research": "research"}
        p = orch.parse_delegation("@Deep Research [deep-dive] go", known)
        self.assertEqual((p["section"], p["tier"], p["question"]),
                         ("dr", "deep-dive", "go"))
        # A boundary check: "@researcher" must not match "research".
        q = orch.parse_delegation("@researcher hi", known)
        self.assertFalse(q["known"])

    def test_known_sections_map_covers_shipped_sections(self):
        m = orch._known_sections_map()
        self.assertEqual(m.get("research"), "research")
        self.assertEqual(m.get("ideas"), "ideas")

    def test_guest_ref_defaults_to_first_role(self):
        # research/roles.json declares product first.
        self.assertEqual(orch._section_guest_ref("research"),
                         "research:product")
        self.assertIsNone(orch._section_guest_ref("no-such-section"))


# ---------------------------------------------------------------------------
# Handler-level: the two tiers through the real dispatch.
# ---------------------------------------------------------------------------
class _HandlerBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="delegation-test-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.root = os.path.join(self.tmp, "workspace")
        os.makedirs(self.root)
        # A real nested origin chat session (mints the project marker).
        self.app_dir = orch.create_session(
            self.root, "proj/ideas/chat-1", "original prompt")
        self.md = os.path.join(self.app_dir, "chat.md")
        open(self.md, "w").close()
        self.cfg = {"root": self.root, "_app_dir": self.app_dir,
                    "_workflow_target": "app",
                    "agents": {"codex_enabled": True}}
        self._orig_avail = orch._agent_available
        orch._agent_available = lambda a, cfg=None: a == "codex"
        self.addCleanup(self._restore)

    def _restore(self):
        orch._agent_available = self._orig_avail

    def _phase(self):
        return wf.Phase(KEY, ".", "chat.md", "talk", rounds=1,
                        conversational=True)

    def _handle(self, raw, transcript="prior transcript"):
        return orch._handle_chat_delegation(
            self.cfg, "proj", self.app_dir, self._phase(), KEY, 3,
            "original prompt", [], {}, ["codex"], transcript, self.md, raw)

    def _md_text(self):
        with open(self.md, encoding="utf-8") as fh:
            return fh.read()

    def _events(self):
        p = os.path.join(self.app_dir, "events.jsonl")
        if not os.path.exists(p):
            return []
        with open(p, encoding="utf-8") as fh:
            return [json.loads(l) for l in fh if l.strip()]

    def _research_dirs(self):
        rp = os.path.join(self.root, "proj", "research")
        return sorted(os.listdir(rp)) if os.path.isdir(rp) else []


class TestDeepDive(_HandlerBase):
    def setUp(self):
        super().setUp()
        self.spawns = []
        self._orig_spawn = seslib.spawn_run
        def fake_spawn(sd, workflow=None, **kw):
            self.spawns.append(sd)
            seslib.set_status(sd, "running")   # real spawn_run does this
            return 4242
        seslib.spawn_run = fake_spawn
        self.addCleanup(self._restore_spawn)

    def _restore_spawn(self):
        seslib.spawn_run = self._orig_spawn

    def test_deep_dive_mints_spawns_and_folds_inflight(self):
        out = self._handle("@Research [deep-dive token=tok1] cache strategy?")
        # Exactly one sub-session minted under proj/research, seeded with
        # the question as its initial prompt.
        dirs = self._research_dirs()
        self.assertEqual(len(dirs), 1, dirs)
        sess = os.path.join(self.root, "proj", "research", dirs[0])
        with open(os.path.join(sess, "initial_prompt",
                               "initial_prompt.md")) as fh:
            self.assertIn("cache strategy?", fh.read())
        rec = seslib.read_delegation(sess)
        self.assertEqual(rec["reply_to"], self.app_dir,
                         "the brief must return to THIS chat")
        self.assertEqual(self.spawns, [sess], "spawned exactly once")
        # Honest in-flight block folded; delegation_spawned emitted.
        self.assertIn("Deep dive delegated to Research", out)
        self.assertIn("Deep dive delegated to Research", self._md_text())
        self.assertIn("delegation_spawned",
                      [e["kind"] for e in self._events()])

    def test_double_send_same_token_does_not_double_mint_or_reannounce(self):
        self._handle("@Research [deep-dive token=same] q")
        out2 = self._handle("@Research [deep-dive token=same] q",
                            transcript="T2")
        self.assertEqual(len(self._research_dirs()), 1,
                         "an idempotent resend must not mint a second dir")
        self.assertEqual(len(self.spawns), 1,
                         "and must not spawn a second engine")
        # The resend must NOT re-fold the block, re-emit the event, or add a
        # second index row.
        self.assertEqual(out2, "T2", "resend folds nothing")
        self.assertEqual(
            [e["kind"] for e in self._events()].count("delegation_spawned"),
            1, "exactly one delegation_spawned across a resend")
        self.assertEqual(self._md_text().count("Deep dive delegated"), 1)

    def test_launch_failure_tells_the_truth(self):
        # spawn_run returns None on a launch failure — the user must NOT be
        # told the brief is on its way.
        seslib.spawn_run = lambda sd, workflow=None, **kw: (
            seslib.set_status(sd, "failed") and None) or None
        out = self._handle("@Research [deep-dive token=x] q")
        self.assertIn("Could not launch", out)
        self.assertNotIn("returns here when ready", out)
        self.assertNotIn("delegation_spawned",
                         [e["kind"] for e in self._events()],
                         "no delegation_spawned for a failed launch")

    def test_distinct_sends_make_distinct_delegations(self):
        self._handle("@Research [deep-dive token=one] q")
        self._handle("@Research [deep-dive token=two] q")
        self.assertEqual(len(self._research_dirs()), 2)

    def test_deep_dive_reply_returns_to_origin_inbox(self):
        self._handle("@Research [deep-dive token=t] q")
        sess = os.path.join(self.root, "proj", "research",
                            self._research_dirs()[0])
        seslib.set_status(sess, "running")
        # The sub-session publishes a brief and delivers its card back.
        card = {"id": "cache-brief", "type": "research_brief", "version": 1,
                "path": "proj/artifacts/cache-brief"}
        self.assertTrue(seslib.deliver_reply(sess, card))
        with open(os.path.join(self.app_dir, "human_inbox.txt")) as fh:
            self.assertIn("[artifact:cache-brief]", fh.read())


class TestQuickTake(_HandlerBase):
    def setUp(self):
        super().setUp()
        self.agent_calls = []
        self._orig_call = orch.call_agent
        def fake_call(acfg, app, key, rnd, ident, prompt):
            self.agent_calls.append(prompt)
            return "My hunch: cache at the edge."
        orch.call_agent = fake_call
        self.addCleanup(self._restore_call)

    def _restore_call(self):
        orch.call_agent = self._orig_call

    def test_quick_take_runs_one_turn_mints_nothing(self):
        out = self._handle("@Research [quick-take] will caching help?")
        self.assertEqual(len(self.agent_calls), 1, "exactly one guest turn")
        # No sub-session, no artifact store touched.
        self.assertEqual(self._research_dirs(), [],
                         "quick-take must NOT mint a sub-session")
        self.assertFalse(os.path.isdir(
            os.path.join(self.root, "proj", "artifacts")),
            "quick-take must NOT publish an artifact")
        # Distinctly labeled so it can't pass as the section's real process.
        self.assertIn("Quick take (guest: Research", out)
        self.assertIn("My hunch: cache at the edge.", out)

    def test_quick_take_is_persona_chipped_in_the_index(self):
        self._handle("@Research [quick-take] q")
        with open(os.path.join(self.app_dir, "messages.jsonl")) as fh:
            recs = [json.loads(l) for l in fh if l.strip()]
        qt = [r for r in recs if r.get("kind") == "quicktake"]
        self.assertEqual(len(qt), 1)
        self.assertEqual(qt[0].get("persona"), "Product Strategist",
                         "the guest persona is chipped into the index")

    def test_unknown_section_degrades_to_warning_no_mint(self):
        out = self._handle("@Nope what about this")
        self.assertIn("No section named", out)
        self.assertEqual(self.agent_calls, [], "no guest turn for unknown")
        self.assertEqual(self._research_dirs(), [])

    def test_plain_message_is_a_pure_no_op(self):
        out = self._handle("just a normal chat message", transcript="T")
        self.assertEqual(out, "T", "non-@ message must not touch anything")
        self.assertEqual(self.agent_calls, [])
        self.assertEqual(self._md_text(), "", "no block folded")


if __name__ == "__main__":
    unittest.main()
