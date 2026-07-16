"""Regression tests for confirmed audit findings fixed in the engine:
 - reset_state_for_new_prompt clears per-run FAILURE bookkeeping, not just
   progress (audit #1: a stale release_gate_repairs budget skipped auto-repair;
   #12: stale phase_resolutions surfaced a false amber warning).
 - _verify_http sandboxes the boot command with write_root=build_dir (audit #2:
   a server was denied writes to its own dir and misreported as failed).
"""
import unittest
import unittest.mock

import orchestrator as orch
import verify


class TestResetStateForNewPrompt(unittest.TestCase):
    def test_clears_progress_and_failure_bookkeeping(self):
        state = {
            "prompt_hash": "old",
            "completed_phases": ["a", "b", "c"],
            "phase_outputs": {"a": "x"},
            "consensus_status": {"a": "yes"},
            "vote_results": {"a": {}},
            "done": True,
            "error": "release gate: design lint failed",
            "release_gate_repairs": 2,          # <- exhausted budget from old run
            "phase_resolutions": {"final_review": "vote_undecided"},  # <- stale
        }
        orch.reset_state_for_new_prompt(state, "newhash")
        self.assertEqual(state["prompt_hash"], "newhash")
        self.assertEqual(state["completed_phases"], [])
        self.assertEqual(state["phase_outputs"], {})
        self.assertFalse(state["done"])
        self.assertIsNone(state["error"])
        # The bug: these two used to survive the reset.
        self.assertEqual(state["release_gate_repairs"], 0)
        self.assertEqual(state["phase_resolutions"], {})

    def test_returns_the_same_state_object(self):
        state = {"prompt_hash": "old"}
        self.assertIs(orch.reset_state_for_new_prompt(state, "h"), state)

    def test_fresh_repair_budget_is_actually_usable_after_reset(self):
        # The point of the fix: after a reset, a new gate failure must be able
        # to attempt a repair (n < max_repairs), not immediately report the
        # budget exhausted.
        state = {"release_gate_repairs": 2}
        orch.reset_state_for_new_prompt(state, "h")
        n = int(state.get("release_gate_repairs") or 0)
        self.assertLess(n, 2)   # room to repair again


class TestVerifyHttpWriteRoot(unittest.TestCase):
    def test_boot_command_is_sandboxed_with_write_root(self):
        # _verify_http must pass write_root=build_dir so a server writing inside
        # its own dir isn't denied (and misreported as "did not respond") when
        # build_dir sits under the engine repo. Capture the _sandbox_wrap call;
        # return a command that exits immediately so the poll loop ends fast.
        captured = {}

        def fake_wrap(cmd_str, write_root=None):
            captured["cmd"] = cmd_str
            captured["write_root"] = write_root
            return ["/bin/sh", "-lc", "exit 0"], None

        import os
        import tempfile
        build_dir = tempfile.mkdtemp()
        # Give _detect_start something to find so `start` is non-empty: a node
        # package.json with a start script (it won't really boot — our fake
        # command exits immediately).
        with open(os.path.join(build_dir, "package.json"), "w", encoding="utf-8") as fh:
            fh.write('{"scripts": {"start": "node server.js"}}')
        with unittest.mock.patch.object(verify, "_sandbox_wrap", side_effect=fake_wrap):
            res = verify._verify_http(build_dir, {}, 5)
        self.assertEqual(captured.get("write_root"), build_dir)
        self.assertIn("ran", res)   # returns a normal result dict, never raises


if __name__ == "__main__":
    unittest.main()
