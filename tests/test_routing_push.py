"""V3 board 4.5: PUSH routing — route_push materializes a provenance-headed
reference to an admissible artifact into a target session's carryover
context, so the target's next build_context treats it as prior context.

Load-bearing claims: the provenance header survives budget truncation (only
the body is cut), routing a non-admissible artifact writes nothing, re-push
is idempotent while a newer version replaces the entry, and unrouted
sessions render byte-identically.
"""
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
from unittest import mock

import artifacts as artlib
import orchestrator as orch
import sessions as seslib


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="routing-test-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.root = os.path.join(self.tmp, "workspace")
        os.makedirs(self.root)
        self.orch_dir = os.path.join(self.tmp, "orch")
        self.errors = []
        self.registry = artlib.load_registry(self.orch_dir,
                                              on_error=self.errors.append)

    def _project(self, name="gloam"):
        d = os.path.join(self.root, name)
        os.makedirs(d, exist_ok=True)
        # Mark the project so nested sessions can be minted under it (a real
        # project gets this from create_session; here we publish artifacts
        # directly into a bare project dir, so mark it explicitly).
        open(os.path.join(d, orch.SECTIONS_MARKER), "w").close()
        return d

    def _publish(self, project_dir, title="Dark mode", type="idea",
                 body="# Dark mode\n\nEveryone wants it.\n",
                 section="ideas", supersedes=None, **fields):
        meta = {"type": type, "title": title,
                "source": {"section": section, "session": "chat-1",
                           "phase": "brainstorm"}}
        meta.update(fields)
        return artlib.publish(project_dir, body, meta, self.registry,
                              on_error=self.errors.append,
                              supersedes=supersedes)

    def _target(self, sid="gloam/research/chat-2"):
        return orch.create_session(self.root, sid, "target prompt")

    def _read_state(self, session_dir):
        with open(artlib._state_path(session_dir), encoding="utf-8") as fh:
            return json.load(fh)

    def _carryover_in_context(self, target_dir, root="gloam"):
        # Fold the target's carryover into prior_outputs exactly as the
        # engine does, then render build_context and return the text.
        state = self._read_state(target_dir)
        phases = [("some_phase", "f", "f.md", "p")]
        prior = []
        cur = {pk for (pk, *_r) in phases}
        for ck, cv in (state.get("carryover_outputs") or {}).items():
            if cv and ck not in cur:
                prior.append((ck, cv))
        return orch.build_context(
            {"root": self.root}, "gloam",
            ("some_phase", "f", "f.md", "purpose"),
            "the original prompt", prior, "transcript")


class TestRoutePush(_Base):
    def test_routed_block_appears_in_build_context(self):
        proj = self._project()
        aid = self._publish(proj)
        target = self._target()
        res = artlib.route_push(aid, proj, target,
                                on_error=self.errors.append,
                                default_state=orch.load_state(target))
        self.assertEqual(res["status"], "routed", self.errors)
        ctx = self._carryover_in_context(target)
        self.assertIn("DECISIONS FROM EARLIER PHASES", ctx)
        # Carryover key is namespaced by source project (gloam) + root id.
        self.assertIn("--- artifact:gloam/%s (final decision) ---" % aid, ctx)
        self.assertIn("[routed artifact] idea %s v1" % aid, ctx)
        self.assertIn("source: ideas/chat-1 · phase brainstorm", ctx)
        self.assertIn("Everyone wants it.", ctx)

    def test_header_survives_budget_truncation(self):
        proj = self._project()
        big = "x" * 50000
        aid = self._publish(proj, body=big)
        target = self._target()
        res = artlib.route_push(aid, proj, target, budget=500,
                                on_error=self.errors.append,
                                default_state=orch.load_state(target))
        self.assertEqual(res["status"], "routed")
        state = self._read_state(target)
        value = state["carryover_outputs"]["artifact:gloam/%s" % aid]
        self.assertLessEqual(len(value), 500, "value must fit the budget")
        self.assertIn("[routed artifact]", value, "header kept")
        self.assertIn("truncated to fit context budget", value)
        # And after build_context's own _budget (tail-keep) at the SAME cap
        # — a NESTED cfg so cget actually reads it (a flat dotted key is
        # silently ignored and the 4000 default would hide the truncation).
        ctx = orch.build_context(
            {"root": self.root, "runtime": {"max_prior_output_chars": 500}},
            "gloam", ("p", "f", "f.md", "x"), "prompt",
            [("artifact:gloam/%s" % aid, value)], "t")
        self.assertIn("[routed artifact]", ctx)

    def test_header_survives_pathologically_small_budget(self):
        # Budget smaller than the header itself: the value must still be
        # <= budget so build_context's tail-keep never fires, and the
        # header prefix is the last thing cut (never the first).
        proj = self._project()
        aid = self._publish(proj, body="y" * 5000)
        for cap in (40, 120, 200):
            target = self._target("gloam/research/cap%d" % cap)
            res = artlib.route_push(aid, proj, target, budget=cap,
                                    on_error=self.errors.append,
                                    default_state=orch.load_state(target))
            self.assertEqual(res["status"], "routed", (cap, self.errors))
            value = self._read_state(target)[
                "carryover_outputs"]["artifact:gloam/%s" % aid]
            self.assertLessEqual(len(value), cap,
                                 "value must never exceed budget %d" % cap)
            self.assertTrue(value.startswith("[routed artifact]"),
                            "the header is the prefix, cut last")

    def test_non_admissible_refused_state_untouched(self):
        proj = self._project()
        target = self._target()
        # create_session mints the dir but no agent_state.json yet.
        self.assertFalse(os.path.exists(artlib._state_path(target)))
        # A converged artifact (derive an identical body).
        r = self._publish(proj, title="Stable", body="same\n")
        conv = self._publish(proj, title="Stable", body="same\n",
                             supersedes=r)
        self.assertEqual(artlib.load_meta(proj, conv)["status"], "converged")
        res = artlib.route_push(conv, proj, target,
                                on_error=self.errors.append,
                                default_state=orch.load_state(target))
        self.assertEqual(res["status"], "refused")
        self.assertFalse(os.path.exists(artlib._state_path(target)),
                         "a refused route writes nothing — not even a seed")

    def test_superseded_version_refused(self):
        proj = self._project()
        v1 = self._publish(proj, title="Evolving", body="v1\n")
        v2 = self._publish(proj, title="Evolving", body="v2\n",
                          supersedes=v1)
        target = self._target()
        # v1 is now stale — only the head (v2) is admissible.
        res1 = artlib.route_push(v1, proj, target,
                                 on_error=self.errors.append,
                                 default_state=orch.load_state(target))
        self.assertEqual(res1["status"], "refused")
        res2 = artlib.route_push(v2, proj, target,
                                 default_state=orch.load_state(target))
        self.assertEqual(res2["status"], "routed")

    def test_missing_and_corrupt_refuse(self):
        proj = self._project()
        aid = self._publish(proj)
        target = self._target()
        self.assertEqual(
            artlib.route_push("ghost", proj, target,
                              on_error=self.errors.append)["status"],
            "refused")
        # Corrupt target state → refuse, do not clobber.
        with open(artlib._state_path(target), "w") as fh:
            fh.write("{corrupt")
        res = artlib.route_push(aid, proj, target,
                                on_error=self.errors.append)
        self.assertEqual(res["status"], "refused")
        with open(artlib._state_path(target), encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "{corrupt",
                             "a corrupt target state is refused, never reset")

    def test_idempotent_repush_and_version_replacement(self):
        proj = self._project()
        v1 = self._publish(proj, title="Iter", body="v1\n")
        target = self._target()
        r1 = artlib.route_push(v1, proj, target,
                               default_state=orch.load_state(target))
        self.assertEqual(r1["status"], "routed")
        r2 = artlib.route_push(v1, proj, target)
        self.assertEqual(r2["status"], "noop", "re-push of same id is a no-op")
        # A newer version replaces the SAME key (keyed by lineage root).
        v2 = self._publish(proj, title="Iter", body="v2 body\n",
                          supersedes=v1)
        r3 = artlib.route_push(v2, proj, target)
        self.assertEqual(r3["status"], "routed")
        state = self._read_state(target)
        root = artlib._meta_root(artlib.load_meta(proj, v2))
        led_key = "gloam/%s" % root
        keys = [k for k in state["carryover_outputs"]
                if k.startswith("artifact:")]
        self.assertEqual(keys, ["artifact:%s" % led_key],
                         "v2 replaces v1's single entry, not a second")
        self.assertIn("v2 body",
                      state["carryover_outputs"]["artifact:%s" % led_key])
        self.assertEqual(state["routed_artifacts"][led_key]["version"], 2)
        # A downgrade back to v1 is refused.
        self.assertEqual(artlib.route_push(v1, proj, target)["status"],
                         "refused")

    def test_seed_survives_reset_for_new_prompt(self):
        proj = self._project()
        aid = self._publish(proj)
        target = self._target()
        artlib.route_push(aid, proj, target,
                          default_state=orch.load_state(target))
        state = self._read_state(target)
        # The engine resets pipeline progress on a new prompt but must
        # preserve routed carryover.
        orch.reset_state_for_new_prompt(state, "newhash")
        self.assertIn("artifact:gloam/%s" % artlib._meta_root(
            artlib.load_meta(proj, aid)), state["carryover_outputs"])

    def test_concurrent_pushes_do_not_tear_state(self):
        proj = self._project()
        ids = [self._publish(proj, title="A%d" % i, body="b%d\n" % i)
               for i in range(6)]
        target = self._target()
        orch.load_state(target)  # ensure dir exists
        artlib._write_state_atomic(target, orch.load_state(target))
        barrier = threading.Barrier(len(ids))
        def worker(a):
            barrier.wait()
            artlib.route_push(a, proj, target)
        threads = [threading.Thread(target=worker, args=(a,)) for a in ids]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        state = self._read_state(target)  # parses = intact
        routed = [k for k in state["carryover_outputs"]
                  if k.startswith("artifact:")]
        self.assertEqual(len(routed), len(ids),
                         "every concurrent push landed, none lost")


class TestRouteVerificationFindings(_Base):
    """Each pins an execution-confirmed defect from the 4.5 pre-commit
    verification (spend-limited mid-run; findings triaged by hand)."""

    def test_same_title_different_projects_do_not_collide(self):
        # Two projects each publish "Dark mode" → both mint id "dark-mode".
        # Keying carryover by the bare root would treat the second as a
        # no-op and silently drop its content.
        a = self._project("alpha")
        b = self._project("beta")
        ida = self._publish(a, title="Dark mode", body="ALPHA body\n")
        idb = self._publish(b, title="Dark mode", body="BETA body\n")
        self.assertEqual(ida, idb, "same slug in each project")
        target = orch.create_session(self.root, "alpha/research/dst", "p")
        r1 = artlib.route_push(ida, a, target,
                               default_state=orch.load_state(target))
        r2 = artlib.route_push(idb, b, target,
                               on_error=self.errors.append,
                               default_state=orch.load_state(target))
        self.assertEqual((r1["status"], r2["status"]), ("routed", "routed"),
                         "the second project's artifact is NOT a no-op")
        carry = self._read_state(target)["carryover_outputs"]
        self.assertIn("alpha/dark-mode",
                      " ".join(k[len("artifact:"):] for k in carry))
        self.assertIn("beta/dark-mode",
                      " ".join(k[len("artifact:"):] for k in carry))
        joined = " ".join(carry.values())
        self.assertIn("ALPHA body", joined)
        self.assertIn("BETA body", joined, "neither body is dropped")

    def test_flock_failure_returns_refused_not_raises(self):
        proj = self._project()
        aid = self._publish(proj)
        target = self._target()
        with mock.patch("fcntl.flock",
                        side_effect=OSError(37, "No locks available")):
            res = artlib.route_push(aid, proj, target,
                                    on_error=self.errors.append,
                                    default_state=orch.load_state(target))
        self.assertEqual(res["status"], "refused",
                         "a lock failure must return the dict, never raise")
        self.assertEqual(res["reason"], "cannot lock target")

    def test_cli_refused_route_leaves_no_orphan_session(self):
        proj = self._project()
        # A converged (non-admissible) artifact.
        r = self._publish(proj, title="Stable", body="same\n")
        conv = self._publish(proj, title="Stable", body="same\n",
                             supersedes=r)
        self.assertEqual(artlib.load_meta(proj, conv)["status"], "converged")
        argv = ["orchestrator.py", "--root", self.root,
                "--route-artifact", conv,
                "--route-from", "gloam/ideas/chat-1",
                "--route-to", "gloam/research/phantom"]
        with mock.patch.object(sys, "argv", argv):
            rc = orch.main()
        self.assertEqual(rc, 2, "refused")
        self.assertFalse(
            os.path.isdir(os.path.join(self.root, "gloam/research/phantom")),
            "a refused route must not mint a phantom target session")


class TestRouteRoundTrip(_Base):
    def test_ideas_research_ideas_round_trip(self):
        # Ideas publishes an idea; route it to a Research session (minted
        # via 4.4); Research publishes a brief; route it back to Ideas.
        proj = self._project("gloam")
        idea = self._publish(proj, title="Offline mode", type="idea",
                             section="ideas", body="# Offline\n")
        research = seslib.mint_delegation_session(
            self.root, "gloam", "research",
            {"title": "Look into offline"}, reply_to=None,
            create_session=orch.create_session, created_at="t")
        r1 = artlib.route_push(idea, proj, research,
                               on_error=self.errors.append,
                               default_state=orch.load_state(research))
        self.assertEqual(r1["status"], "routed", self.errors)
        self.assertIn("# Offline", self._carryover_in_context(research))
        # Research produces a brief in the SAME project store.
        brief = self._publish(proj, title="Offline brief",
                             type="research_brief", section="research",
                             body="# Brief\n\nUse a local cache.\n",
                             sources=["x"])
        ideas2 = orch.create_session(self.root, "gloam/ideas/chat-9", "p")
        r2 = artlib.route_push(brief, proj, ideas2,
                               on_error=self.errors.append,
                               default_state=orch.load_state(ideas2))
        self.assertEqual(r2["status"], "routed")
        ctx = self._carryover_in_context(ideas2)
        self.assertIn("[routed artifact] research_brief", ctx)
        self.assertIn("Use a local cache.", ctx)


class TestRouteCLI(_Base):
    def test_cli_route_emits_event_and_lands_carryover(self):
        proj = self._project()
        aid = self._publish(proj)
        orch.create_session(self.root, "gloam/research/dest", "p")
        argv = ["orchestrator.py", "--root", self.root,
                "--route-artifact", aid,
                "--route-from", "gloam/ideas/chat-1",
                "--route-to", "gloam/research/dest"]
        with mock.patch.object(sys, "argv", argv):
            rc = orch.main()
        self.assertEqual(rc, 0)
        target = os.path.join(self.root, "gloam/research/dest")
        self.assertIn("artifact:", " ".join(
            self._read_state(target)["carryover_outputs"].keys()))
        with open(os.path.join(target, "events.jsonl")) as fh:
            kinds = [json.loads(l)["kind"] for l in fh if l.strip()]
        self.assertIn("artifact_routed", kinds)


if __name__ == "__main__":
    unittest.main()
