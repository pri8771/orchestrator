"""V3 board 1.9: fork_session — copy minus every continuity carrier."""
import os
import tempfile
import unittest

import orchestrator as orch


def _write(path, text="x"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


class TestForkSession(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.slug = "home--ideas--demo"
        d = os.path.join(self.root, self.slug)
        _write(os.path.join(d, "initial_prompt", "initial_prompt.md"), "seed")
        _write(os.path.join(d, "workflow.txt"), "chat_ideas\n")
        _write(os.path.join(d, "chat", "chat.md"), "# transcript\nreal content\n")
        # Continuity/coordination carriers that must NOT survive the fork:
        _write(os.path.join(d, ".agent_cwd", "claude-session.json"), "{}")
        _write(os.path.join(d, "stray.lock"), "pid=1")
        _write(os.path.join(d, "chat", "inner.lock"), "pid=2")
        _write(os.path.join(d, "approvals", "chat.ok"), "")
        _write(os.path.join(d, "human_inbox.txt"), "undrained message")
        _write(os.path.join(d, ".step_in"), "")
        orch.save_state(d, {
            "current_phase": "chat", "current_round": 2, "next_agent": "codex",
            "workflow": "chat_ideas", "completed_phases": [], "prompt_hash": "h",
            "phase_outputs": {"x": "kept"}, "consensus_status": {},
            "vote_results": {}, "runner_pid": 4242,
            "awaiting_human": "chat", "awaiting_approval": "x",
            "error": "old error", "done": False})

    def test_fork_strips_carriers_and_sanitizes_state(self):
        rc, name = orch.fork_session(self.root, self.slug)
        self.assertEqual(rc, 0)
        self.assertEqual(name, self.slug + "-fork")
        f = os.path.join(self.root, name)
        # Content copied byte-identical.
        with open(os.path.join(f, "chat", "chat.md"), encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "# transcript\nreal content\n")
        with open(os.path.join(f, "workflow.txt"), encoding="utf-8") as fh:
            self.assertEqual(fh.read().strip(), "chat_ideas")
        # No agent-thread carrier, no locks anywhere, no approvals, no inbox,
        # no step-in marker.
        self.assertFalse(os.path.exists(os.path.join(f, ".agent_cwd")))
        for dirpath, _dirs, files in os.walk(f):
            for fn in files:
                self.assertFalse(fn.endswith(".lock"),
                                 "lock survived: %s/%s" % (dirpath, fn))
        self.assertFalse(os.path.exists(os.path.join(f, "approvals")))
        self.assertFalse(os.path.exists(os.path.join(f, "human_inbox.txt")))
        self.assertFalse(os.path.exists(os.path.join(f, ".step_in")))
        # State sanitized; content fields kept.
        st = orch.load_state(f)
        self.assertIsNone(st["runner_pid"])
        self.assertIsNone(st["next_agent"])
        self.assertIsNone(st["awaiting_approval"])
        self.assertIsNone(st["awaiting_human"])
        self.assertIsNone(st["error"])
        self.assertEqual(st["phase_outputs"], {"x": "kept"})
        # The source is untouched.
        self.assertTrue(os.path.exists(os.path.join(
            self.root, self.slug, ".agent_cwd", "claude-session.json")))

    def test_fork_names_are_suffixed_never_overwritten(self):
        self.assertEqual(orch.fork_session(self.root, self.slug)[1],
                         self.slug + "-fork")
        self.assertEqual(orch.fork_session(self.root, self.slug)[1],
                         self.slug + "-fork-2")
        self.assertEqual(orch.fork_session(self.root, self.slug)[1],
                         self.slug + "-fork-3")

    def test_live_locked_source_is_refused(self):
        lockp = orch._app_lock_path(self.slug)
        _write(lockp, "pid=%d host=test started=now\n" % os.getpid())
        try:
            rc, name = orch.fork_session(self.root, self.slug)
            self.assertEqual((rc, name), (3, None))
            self.assertFalse(os.path.exists(
                os.path.join(self.root, self.slug + "-fork")))
        finally:
            os.remove(lockp)

    def test_missing_source_is_a_clean_error(self):
        rc, name = orch.fork_session(self.root, "nope")
        self.assertEqual((rc, name), (2, None))


if __name__ == "__main__":
    unittest.main()
