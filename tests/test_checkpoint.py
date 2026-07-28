import os, tempfile, threading, time, unittest
import orchestrator as orch


class FakePhase:
    def __init__(self, checkpoint=False):
        self._c = checkpoint
    def get(self, k, default=None):
        return self._c if k == "checkpoint" else default


class TestCheckpointDecision(unittest.TestCase):
    def test_fully_autonomous_never_pauses(self):
        cfg = {"_autonomy": "fully_autonomous"}
        self.assertFalse(orch._should_pause_after(cfg, FakePhase(checkpoint=True)))
        self.assertFalse(orch._should_pause_after({}, FakePhase(checkpoint=True)))

    def test_manual_always_pauses(self):
        cfg = {"_autonomy": "manual"}
        self.assertTrue(orch._should_pause_after(cfg, FakePhase(checkpoint=False)))

    def test_semi_only_on_checkpoint(self):
        cfg = {"_autonomy": "semi_autonomous"}
        self.assertTrue(orch._should_pause_after(cfg, FakePhase(checkpoint=True)))
        self.assertFalse(orch._should_pause_after(cfg, FakePhase(checkpoint=False)))


class TestAwaitApproval(unittest.TestCase):
    def _run_await(self, d, phase, drop_name=None, drop_body=""):
        """Run _await_approval in a thread; optionally drop a decision file."""
        state = {}
        result = {}
        def run():
            result["decision"], result["payload"] = orch._await_approval(
                d, phase, state, timeout=10, poll=0.1)
        t = threading.Thread(target=run); t.start()
        # Bounded poll instead of a fixed 0.3s nap: a loaded shared runner
        # can take longer than 0.3s just to schedule the thread. The 5s
        # deadline still fails loudly (same assertion) if the pause never
        # happens.
        deadline = time.time() + 5
        while time.time() < deadline \
                and state.get("awaiting_approval") != phase:
            time.sleep(0.02)
        self.assertEqual(state.get("awaiting_approval"), phase)  # paused
        if drop_name:
            # Atomic write (temp file + rename), matching the real writer
            # (the GUI's ApprovalFiles.write uses String.write(atomically:
            # true)): _await_approval's poll loop does os.path.exists() then
            # immediately reads. A plain open(path, "w") lets the poll
            # thread — running every 0.1s here, vs. the engine's real 2s
            # default — observe the file the instant it's CREATED, before
            # fh.write() lands any content, and read back "" instead of
            # drop_body. Not a production bug (the real writer already
            # doesn't do this); just this test's own drop-helper needed to.
            appr_dir = os.path.join(d, "approvals")
            os.makedirs(appr_dir, exist_ok=True)
            dest = os.path.join(appr_dir, drop_name)
            tmp = dest + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                fh.write(drop_body)
            os.replace(tmp, dest)
        t.join(timeout=5)
        self.assertIsNone(state.get("awaiting_approval"))  # cleared
        return result

    def test_approval_file_unblocks(self):
        d = tempfile.mkdtemp()
        r = self._run_await(d, "scope_and_prd", "scope_and_prd.ok")
        self.assertEqual(r["decision"], "approved")
        self.assertIsNone(r["payload"])

    def test_edit_and_approve_returns_body(self):
        d = tempfile.mkdtemp()
        r = self._run_await(d, "tech_specs", "tech_specs.edit", "# my edited spec\n")
        self.assertEqual(r["decision"], "edited")
        self.assertEqual(r["payload"], "# my edited spec\n")

    def test_request_changes_returns_feedback(self):
        d = tempfile.mkdtemp()
        r = self._run_await(d, "app_features", "app_features.changes",
                            "please add offline mode")
        self.assertEqual(r["decision"], "changes_requested")
        self.assertEqual(r["payload"], "please add offline mode")

    def test_timeout_proceeds(self):
        d = tempfile.mkdtemp()
        state = {}
        decision, payload = orch._await_approval(d, "p", state, timeout=0.3, poll=0.1)
        self.assertEqual(decision, "timeout")
        self.assertIsNone(payload)
        self.assertIsNone(state.get("awaiting_approval"))


class TestAwaitApprovalShutdown(unittest.TestCase):
    """A-78: a stop signal during a checkpoint pause must exit the wait
    promptly (the process otherwise hangs up to 2h joining the worker thread)
    — WITHOUT clearing awaiting_approval, so the resume path re-arms the
    interrupted checkpoint instead of sailing past the human decision."""

    def tearDown(self):
        orch._SHUTDOWN.clear()

    def test_shutdown_exits_promptly_and_keeps_the_marker(self):
        d = tempfile.mkdtemp()
        state = {}
        result = {}

        def run():
            result["decision"], result["payload"] = orch._await_approval(
                d, "scope_and_prd", state, timeout=7200, poll=0.1)
        t = threading.Thread(target=run)
        t.start()
        time.sleep(0.3)
        self.assertEqual(state.get("awaiting_approval"), "scope_and_prd")
        orch._SHUTDOWN.set()
        t.join(timeout=5)   # well under the 7200s timeout
        self.assertFalse(t.is_alive(), "the wait must exit on shutdown")
        self.assertEqual(result["decision"], "shutdown")
        self.assertIsNone(result["payload"])
        # The marker survives — exactly what the re-arm-on-resume path needs.
        self.assertEqual(state.get("awaiting_approval"), "scope_and_prd")


if __name__ == "__main__":
    unittest.main()
