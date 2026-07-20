"""V3 board 6.3: adversarial streaming lifecycle invariants.

The .stream channel is ephemeral preview state only. These tests pin creation,
delta order, every completion arm, kill/resume orphan recovery, deterministic
turn ids, and transcript byte parity. Provider SSE parsing/usage/error classes
remain covered in test_api_runners.py.
"""
import json
import os
import shutil
import subprocess
import tempfile
import unittest
import unittest.mock

import messages as msglib
import orchestrator as orch
import turncontext as tcxlib


class StreamLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.app = "p/s/chat"
        self.app_dir = os.path.join(self.root, self.app)
        os.makedirs(self.app_dir)
        self.quiet = orch._QUIET
        orch._QUIET = True

    def tearDown(self):
        orch._finish_api_stream()
        orch._QUIET = self.quiet
        shutil.rmtree(self.root, ignore_errors=True)

    def _cfg(self):
        cfg = {"root": self.root, "_app_dir": self.app_dir,
               "runtime": {"provider_min_gap_seconds": 0,
                           "global_worker_cap_enabled": False}}
        tcxlib.TurnContext(cfg).api_agents_enabled = True
        return cfg

    def _call(self, runner, cfg=None, agent="api:openai:gpt-5"):
        cfg = cfg or self._cfg()
        with unittest.mock.patch.object(orch, "resolve_runner",
                                         lambda _agent: runner), \
                unittest.mock.patch.object(orch, "write_call_log",
                                             lambda *a, **kw: None):
            return orch._call_agent_once(cfg, self.app, "design", 2,
                                         agent, "prompt")

    def test_live_file_uses_messages_turn_id_and_flushes_ordered_ndjson(self):
        observed = {}

        def runner(cfg, prompt, timeout):
            path = orch._API_STREAM.path
            observed["path"] = path
            observed["exists_before_delta"] = os.path.exists(path)
            orch._append_api_stream_delta("hel")
            orch._append_api_stream_delta("lo")
            with open(path, encoding="utf-8") as fh:
                observed["lines"] = [json.loads(line) for line in fh]
            return "hello", "", 0, "api:test"

        text = self._call(runner, agent="api:openai:org/model")
        expected = msglib.turn_id("design", "2", "api:openai:org/model",
                                  "turn")
        self.assertTrue(observed["exists_before_delta"])
        self.assertEqual(observed["lines"][0]["seq"], 1)
        self.assertEqual(observed["lines"][1]["seq"], 2)
        self.assertEqual("".join(x["delta"] for x in observed["lines"]),
                         "hello")
        self.assertTrue(all(set(x) == {"seq", "ts", "delta"}
                            for x in observed["lines"]))
        self.assertEqual(
            os.path.basename(observed["path"])[:-len(".ndjson")],
            orch.urllib.parse.quote(expected, safe=":._-"))
        self.assertFalse(os.path.exists(observed["path"]))
        self.assertEqual(text, orch.ensure_signature("hello",
                                                     "api:openai:org/model"))

    def test_file_is_deleted_on_success_agenterror_timeout_and_empty(self):
        paths = []

        def success(cfg, prompt, timeout):
            paths.append(orch._API_STREAM.path)
            orch._append_api_stream_delta("ok")
            return "ok", "", 0, "api:test"

        def agent_error(cfg, prompt, timeout):
            paths.append(orch._API_STREAM.path)
            orch._append_api_stream_delta("partial")
            raise orch.AgentError("runner refused")

        def timeout(cfg, prompt, turn_timeout):
            paths.append(orch._API_STREAM.path)
            orch._append_api_stream_delta("partial")
            raise subprocess.TimeoutExpired("api:test", 1)

        def empty(cfg, prompt, timeout):
            paths.append(orch._API_STREAM.path)
            return "", "empty", 1, "api:test"

        self._call(success)
        for runner in (agent_error, timeout, empty):
            with self.assertRaises(orch.AgentError):
                self._call(runner, cfg=self._cfg())
        self.assertEqual(len(paths), 4)
        self.assertTrue(all(not os.path.exists(path) for path in paths))

    def test_kill_mid_stream_resume_sweeps_preview_and_reruns_turn(self):
        md_path = os.path.join(self.app_dir, "phase.md")
        prefix = "# Phase\n\n## Transcript\n"
        crashed = prefix + "\n### Round 1\n"
        with open(md_path, "w", encoding="utf-8") as fh:
            fh.write(crashed)

        # Simulate SIGKILL: bytes flushed, but no finally and no transcript or
        # messages.jsonl append. Close the test process's descriptor without
        # invoking the engine cleanup helper, leaving the orphan on disk.
        orch._prepare_api_stream(self.app_dir, "design", 1,
                                 "api:anthropic:m")
        orch._append_api_stream_delta("NEVER AUTHORITATIVE")
        orphan = orch._API_STREAM.path
        orch._API_STREAM.fh.close()
        for name in ("fh", "path", "turn_id", "seq"):
            delattr(orch._API_STREAM, name)
        self.assertTrue(os.path.exists(orphan))
        self.assertEqual(msglib.read_messages(self.app_dir), [])

        notices = []
        with unittest.mock.patch.object(orch, "emit", notices.append):
            self.assertEqual(orch._sweep_stream_orphans(self.app_dir), 1)
        self.assertFalse(os.path.exists(orphan))
        self.assertTrue(any("turn will re-run" in line for line in notices))

        # The real resume parser ignores .stream and drops the incomplete round.
        with open(md_path, encoding="utf-8") as fh:
            still_md = fh.read()
        resume_round, kept, _header_end = orch._resume_round_state(still_md)
        self.assertEqual(resume_round, 1)
        self.assertEqual(kept, prefix + "\n")
        self.assertNotIn("NEVER AUTHORITATIVE", still_md)
        orch.write_md(md_path, kept)
        msglib.reconcile_messages(self.app_dir, "design", keep_below_round=1)

        calls = []
        def rerun(cfg, prompt, timeout):
            calls.append(1)
            orch._append_api_stream_delta("complete")
            return "complete", "", 0, "api:test"
        final = self._call(rerun, cfg=self._cfg(), agent="api:anthropic:m")
        self.assertEqual(len(calls), 1)
        self.assertIn("complete", final)
        self.assertEqual(msglib.read_messages(self.app_dir), [])

    def test_streamed_and_buffered_return_values_make_identical_blocks(self):
        def streamed(cfg, prompt, timeout):
            for delta in ("same ", "answer"):
                orch._append_api_stream_delta(delta)
            return "same answer", "", 0, "api:test"

        streamed_text = self._call(streamed)
        buffered_text = orch.ensure_signature("same answer", "api:openai:gpt-5")
        self.assertEqual(streamed_text, buffered_text)
        self.assertEqual("**Agent**\n\n%s\n" % streamed_text,
                         "**Agent**\n\n%s\n" % buffered_text)

    def test_process_app_sweeps_before_pipeline_or_resume_parsing(self):
        prompt_dir = os.path.join(self.app_dir, "initial_prompt")
        os.makedirs(prompt_dir)
        with open(os.path.join(prompt_dir, "initial_prompt.md"), "w",
                  encoding="utf-8") as fh:
            fh.write("build it")
        stream_dir = os.path.join(self.app_dir, ".stream")
        os.makedirs(stream_dir)
        orphan = os.path.join(stream_dir, "design:1:a:turn.ndjson")
        with open(orphan, "w", encoding="utf-8") as fh:
            fh.write('{"seq":1,"ts":1,"delta":"partial"}\n')
        heartbeat = unittest.mock.Mock()
        cfg = {"_checked_any_agent_runnable": True}
        reached = []

        def pipeline(_cfg, _app, _dir, _prompt):
            self.assertFalse(os.path.exists(orphan))
            reached.append(True)

        with unittest.mock.patch.object(orch, "acquire_app_lock",
                                         lambda *a: True), \
                unittest.mock.patch.object(orch, "release_app_lock",
                                             lambda *a: None), \
                unittest.mock.patch.object(orch, "_start_run_heartbeat",
                                             lambda *a: heartbeat), \
                unittest.mock.patch.object(orch, "_run_app_pipeline", pipeline), \
                unittest.mock.patch.object(orch, "emit", lambda *a: None):
            orch.process_app(cfg, self.root, self.app)
        self.assertEqual(reached, [True])
        heartbeat.set.assert_called_once_with()

    def test_sweep_never_follows_symlinked_directories(self):
        outside = tempfile.mkdtemp()
        try:
            sentinel = os.path.join(outside, "keep.txt")
            with open(sentinel, "w", encoding="utf-8") as fh:
                fh.write("keep")
            stream_dir = os.path.join(self.app_dir, ".stream")
            os.makedirs(stream_dir)
            os.symlink(outside, os.path.join(stream_dir, "hostile"))
            self.assertEqual(orch._sweep_stream_orphans(self.app_dir), 1)
            self.assertTrue(os.path.exists(sentinel))
            self.assertFalse(os.path.lexists(os.path.join(stream_dir,
                                                          "hostile")))
        finally:
            shutil.rmtree(outside, ignore_errors=True)

    def test_sweep_never_follows_symlinked_stream_root(self):
        outside = tempfile.mkdtemp()
        try:
            sentinel = os.path.join(outside, "keep.txt")
            with open(sentinel, "w", encoding="utf-8") as fh:
                fh.write("keep")
            os.symlink(outside, os.path.join(self.app_dir, ".stream"))
            self.assertEqual(orch._sweep_stream_orphans(self.app_dir), 1)
            self.assertTrue(os.path.exists(sentinel))
            self.assertFalse(os.path.lexists(os.path.join(self.app_dir,
                                                          ".stream")))
        finally:
            shutil.rmtree(outside, ignore_errors=True)

    def test_cleanup_failure_cannot_return_a_completed_turn(self):
        path_box = {}
        real_remove = os.remove

        def runner(cfg, prompt, timeout):
            path_box["path"] = orch._API_STREAM.path
            orch._append_api_stream_delta("complete")
            return "complete", "", 0, "api:test"

        def fail_target(path):
            if path == path_box.get("path"):
                raise PermissionError("planted unlink failure")
            return real_remove(path)

        with unittest.mock.patch.object(orch.os, "remove", fail_target):
            with self.assertRaisesRegex(orch.AgentError, "refusing to complete"):
                self._call(runner)
        self.assertTrue(os.path.exists(path_box["path"]))


if __name__ == "__main__":
    unittest.main()
