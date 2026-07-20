"""V3 6.2: direct-API runners (api:<provider>:<model>) — file-only keys,
header-only transport, distinct §5.2 error classes, per-project opt-in, 4h
probe cache, and the planted-fake-key leak gate: the key must never reach
argv, any environ, a URL, a cmd display string, or any persisted sink, and
the api: paths must spawn no subprocess at all.
"""
import inspect
import json
import os
import shutil
import tempfile
import time
import unittest
import unittest.mock
import urllib.error

import orchestrator as orch
import traces as traceslib
import turncontext as tcxlib

FAKE_KEY = "sk-FAKE-LEAK-CANARY-42xyz"


class _FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def read(self):
        return json.dumps(self._payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


_OPEN_ERRORS = []


def _http_error(code, body=b"denied"):
    # Tracked so tests can close them: HTTPError is file-like, and the strict
    # gate escalates ResourceWarning to an error.
    import io
    exc = urllib.error.HTTPError("https://x", code, "reason", {},
                                 io.BytesIO(body))
    _OPEN_ERRORS.append(exc)
    return exc


def _close_errors():
    while _OPEN_ERRORS:
        try:
            _OPEN_ERRORS.pop().close()
        except Exception:  # noqa: BLE001 - already closed is fine
            pass


_SUCCESS = {
    "anthropic": {"content": [{"type": "text", "text": "hi from claude"}],
                  "usage": {"input_tokens": 11, "output_tokens": 7}},
    "openai": {"choices": [{"message": {"content": "hi from gpt"}}],
               "usage": {"prompt_tokens": 12, "completion_tokens": 8}},
    "google": {"candidates": [{"content": {"parts": [{"text": "hi from gemini"}]}}],
               "usageMetadata": {"promptTokenCount": 13,
                                 "candidatesTokenCount": 9}},
}
_EXPECT_TEXT = {"anthropic": "hi from claude", "openai": "hi from gpt",
                "google": "hi from gemini"}
_RUNNERS = {"anthropic": orch.run_anthropic_api, "openai": orch.run_openai_api,
            "google": orch.run_google_api}


class _ApiBase(unittest.TestCase):
    """Temp HOME with planted key files; probe stubbed OK; emit silenced."""

    def setUp(self):
        self.home = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.home, ".orchestrator"))
        for name in ("anthropic_api_key", "openai_api_key", "google_api_key"):
            with open(os.path.join(self.home, ".orchestrator", name), "w",
                      encoding="utf-8") as fh:
                fh.write(FAKE_KEY + "\n")
        self._env = unittest.mock.patch.dict(os.environ, {"HOME": self.home})
        self._env.start()
        self._probe = unittest.mock.patch.object(
            orch, "detect_api_available", lambda cfg, p: (True, ""))
        self._probe.start()
        self._quiet = orch._QUIET
        orch._QUIET = True
        traceslib.pop_last_usage()   # never inherit another test's stash
        self.cfg = {}
        tcxlib.TurnContext(self.cfg).api_agents_enabled = True

    def tearDown(self):
        self._probe.stop()
        self._env.stop()
        orch._QUIET = self._quiet
        traceslib.pop_last_usage()
        _close_errors()
        shutil.rmtree(self.home, ignore_errors=True)

    def _patched(self, fake_urlopen):
        return unittest.mock.patch.object(orch.urllib.request, "urlopen",
                                          fake_urlopen)


class TestKeyFiles(_ApiBase):
    def test_keys_come_from_files_only_env_ignored(self):
        # Planted env vars must NOT be read (file-only design; run.sh unsets
        # them anyway, and env transport is the forbidden leak vector).
        os.remove(os.path.join(self.home, ".orchestrator", "anthropic_api_key"))
        with unittest.mock.patch.dict(os.environ,
                                      {"ANTHROPIC_API_KEY": "env-key"}):
            self.assertIsNone(orch._api_key("anthropic"))

    def test_missing_or_empty_file_is_none(self):
        os.remove(os.path.join(self.home, ".orchestrator", "openai_api_key"))
        self.assertIsNone(orch._api_key("openai"))
        with open(os.path.join(self.home, ".orchestrator", "openai_api_key"),
                  "w", encoding="utf-8") as fh:
            fh.write("   \n")
        self.assertIsNone(orch._api_key("openai"))

    def test_google_falls_back_to_gemini_key_file(self):
        os.remove(os.path.join(self.home, ".orchestrator", "google_api_key"))
        with open(os.path.join(self.home, ".orchestrator", "gemini_api_key"),
                  "w", encoding="utf-8") as fh:
            fh.write("shared-gemini-cred")
        self.assertEqual(orch._api_key("google"), "shared-gemini-cred")

    def test_key_path_helper_names_the_canonical_file(self):
        self.assertTrue(orch._api_key_path("anthropic").endswith(
            ".orchestrator/anthropic_api_key"))


class TestSuccessPaths(_ApiBase):
    def test_each_provider_parses_text_and_stashes_usage(self):
        for provider, runner in _RUNNERS.items():
            captured = {}

            def fake_urlopen(req, timeout=None, _c=captured, _p=provider):
                _c["url"] = req.full_url
                _c["headers"] = dict(req.headers)
                _c["body"] = json.loads(req.data.decode("utf-8"))
                return _FakeResp(_SUCCESS[_p])

            with self._patched(fake_urlopen):
                out, err, code, cmd = runner(self.cfg, "hello", 30, "m1")
            self.assertEqual((out, err, code), (_EXPECT_TEXT[provider], "", 0),
                             provider)
            self.assertEqual(cmd, "api:%s model=m1" % provider)
            usage = traceslib.pop_last_usage()
            self.assertEqual(usage["provider"], provider)
            self.assertEqual(usage["model"], "m1")
            self.assertIsInstance(usage["prompt_tokens"], int)
            self.assertIsInstance(usage["completion_tokens"], int)

    def test_auth_travels_in_headers_never_url(self):
        seen = {}
        for provider, runner in _RUNNERS.items():
            def fake_urlopen(req, timeout=None, _p=provider):
                seen[_p] = (req.full_url, dict(req.headers))
                return _FakeResp(_SUCCESS[_p])

            with self._patched(fake_urlopen):
                runner(self.cfg, "hello", 30, "m1")
        for provider, (url, headers) in seen.items():
            self.assertNotIn(FAKE_KEY, url, provider)   # never ?key=
            self.assertIn(FAKE_KEY, json.dumps(headers), provider)
        # provider-specific header names
        self.assertEqual(seen["anthropic"][1].get("X-api-key"), FAKE_KEY)
        self.assertEqual(seen["openai"][1].get("Authorization"),
                         "Bearer " + FAKE_KEY)
        self.assertEqual(seen["google"][1].get("X-goog-api-key"), FAKE_KEY)


class TestErrorClasses(_ApiBase):
    """Auth, quota, network, and parse failures are DISTINCT and actionable."""

    def _run_with(self, exc_or_resp):
        def fake_urlopen(req, timeout=None):
            if isinstance(exc_or_resp, Exception):
                raise exc_or_resp
            return exc_or_resp
        with self._patched(fake_urlopen):
            return orch.run_anthropic_api(self.cfg, "hi", 30, "m1")

    def test_auth_error_names_the_key_file(self):
        for code in (401, 403):
            out, err, rc, _ = self._run_with(_http_error(code))
            self.assertEqual((out, rc), ("", 1))
            self.assertIn("auth failed", err)
            self.assertIn(orch._api_key_path("anthropic"), err)

    def test_quota_error_is_distinct(self):
        _, err, rc, _ = self._run_with(_http_error(429))
        self.assertEqual(rc, 1)
        self.assertIn("rate/quota", err)
        self.assertNotIn("auth failed", err)

    def test_network_error_is_distinct(self):
        _, err, rc, _ = self._run_with(urllib.error.URLError("conn refused"))
        self.assertEqual(rc, 1)
        self.assertIn("unreachable (network)", err)

    def test_receive_phase_timeout_is_a_network_error_not_a_crash(self):
        # urllib wraps only SEND-phase failures into URLError; a timeout or
        # reset while reading the response raises raw socket errors. Those
        # must map to the network class — an escaped exception would skip
        # trace finalization and crash the turn.
        for exc in (TimeoutError("timed out"),
                    ConnectionResetError("peer reset")):
            out, err, rc, _ = self._run_with(exc)
            self.assertEqual((out, rc), ("", 1), exc)
            self.assertIn("unreachable (network)", err)

    def test_malformed_json_is_specific(self):
        class _Junk(_FakeResp):
            def read(self):
                return b"<html>not json"
        _, err, rc, _ = self._run_with(_Junk(None))
        self.assertEqual(rc, 1)
        self.assertIn("malformed JSON", err)

    def test_unexpected_shape_is_specific(self):
        _, err, rc, _ = self._run_with(_FakeResp({"content": "not-a-list"}))
        self.assertEqual(rc, 1)
        self.assertIn("unexpected payload shape", err)

    def test_failed_turn_never_leaves_stale_usage(self):
        traceslib.set_last_usage({"prompt_tokens": 1, "completion_tokens": 1})
        traceslib.pop_last_usage()
        self._run_with(_http_error(429))
        self.assertIsNone(traceslib.pop_last_usage())


class TestOptIn(_ApiBase):
    def test_refusal_without_opt_in_and_no_http(self):
        cfg = {}   # api_agents_enabled unset
        def exploding_urlopen(req, timeout=None):
            raise AssertionError("refused turn must not reach the network")
        with self._patched(exploding_urlopen):
            out, err, code, cmd = orch.run_openai_api(cfg, "hi", 30, "m1")
        self.assertEqual((out, code), ("", 1))
        self.assertIn("opt-in", err)
        self.assertIn("api_agents", err)
        self.assertIn("run_config.json", err)

    def test_apply_api_optin_enables_and_allows(self):
        cfg = {}
        tctx = tcxlib.TurnContext(cfg)
        orch._apply_api_optin(tctx, {"api_agents": True})
        self.assertTrue(tctx.api_agents_enabled)
        self.assertIsNone(orch._api_refusal(cfg, "openai", "m1"))

    def test_apply_api_optin_resets_stale_flag(self):
        # --watch reuses one cfg across iterations: removing api_agents from
        # run_config.json must revoke the opt-in on the next pass, or a
        # de-opted project keeps billing the provider account.
        cfg = {}
        tctx = tcxlib.TurnContext(cfg)
        orch._apply_api_optin(tctx, {"api_agents": True})
        orch._apply_api_optin(tctx, {})   # opt-in removed on disk
        self.assertFalse(bool(tctx.api_agents_enabled))
        self.assertIsNotNone(orch._api_refusal(cfg, "openai", "m1"))

    def test_pipeline_calls_the_optin_wiring(self):
        # Mutation guard: deleting the pipeline's _apply_api_optin call (or
        # typoing the run-config key) must fail a test, not ship silently.
        src = inspect.getsource(orch)
        self.assertIn("_apply_api_optin(tctx, _rc)", src)
        self.assertIn('run_cfg.get("api_agents")',
                      inspect.getsource(orch._apply_api_optin))


class TestDispatchAndDisplay(_ApiBase):
    def test_resolve_runner_dispatches_api_ids(self):
        def fake_urlopen(req, timeout=None):
            return _FakeResp(_SUCCESS["anthropic"])
        runner = orch.resolve_runner("api:anthropic:claude-sonnet-5")
        with self._patched(fake_urlopen):
            out, err, code, cmd = runner(self.cfg, "hi", 30)
        self.assertEqual((out, code), ("hi from claude", 0))
        self.assertEqual(cmd, "api:anthropic model=claude-sonnet-5")

    def test_malformed_api_ids_fail_specifically_not_keyerror(self):
        for bad in ("api:bogus:m", "api:anthropic", "api:"):
            runner = orch.resolve_runner(bad)
            out, err, code, _ = runner({}, "hi", 30)
            self.assertEqual((out, code), ("", 1), bad)
            self.assertIn("expected api:<provider>:<model>", err, bad)

    def test_static_and_local_dispatch_unchanged(self):
        self.assertIs(orch.resolve_runner("codex"), orch.run_codex)
        self.assertTrue(callable(orch.resolve_runner("local:qwen3:8b")))
        with self.assertRaises(KeyError):
            orch.resolve_runner("mystery")

    def test_display_labels_api_ids_readably(self):
        self.assertEqual(orch._derive_display("api:anthropic:claude-sonnet-5"),
                         "Claude Sonnet 5 (api)")
        self.assertEqual(orch.DISPLAY["api:openai:gpt-5"], "Gpt 5 (api)")
        self.assertNotEqual(orch._derive_display("api:openai:gpt-5"), "Api")

    def test_pace_provider_recognizes_api_ids(self):
        # gap=0 -> instant return; the point is no crash and no misparse.
        cfg = {"runtime": {"provider_min_gap_seconds": 0}}
        orch._pace_provider(cfg, "api:anthropic:claude-sonnet-5")
        orch._pace_provider(cfg, "api:notaprovider:x")
        orch._pace_provider(cfg, "api:")


class TestPreflightGate(_ApiBase):
    """The not-ok probe branch through the REAL runner path — _ApiBase stubs
    the probe OK, so this class re-patches it per test."""

    def test_probe_not_ok_blocks_the_turn_with_its_reason(self):
        def exploding_urlopen(req, timeout=None):
            raise AssertionError("blocked turn must not reach the network")
        with unittest.mock.patch.object(
                orch, "detect_api_available",
                lambda cfg, p: (False, "anthropic: no API key — create X")), \
                self._patched(exploding_urlopen):
            out, err, code, cmd = orch.run_anthropic_api(self.cfg, "hi", 30, "m1")
        self.assertEqual((out, code), ("", 1))
        self.assertIn("no API key", err)

    def test_opt_in_but_missing_key_fails_via_real_probe(self):
        # End-to-end through the unstubbed detect_api_available: opt-in on,
        # key file absent -> the turn refuses naming the exact file, and no
        # POST ever fires.
        os.remove(os.path.join(self.home, ".orchestrator", "openai_api_key"))
        cachedir = tempfile.mkdtemp()
        def exploding_urlopen(req, timeout=None):
            raise AssertionError("no key -> no HTTP at all")
        try:
            self._probe.stop()   # use the real probe
            with unittest.mock.patch.object(
                    orch, "_probe_cache_path",
                    lambda name: os.path.join(cachedir, name)), \
                    self._patched(exploding_urlopen):
                out, err, code, cmd = orch.run_openai_api(self.cfg, "hi", 30, "m1")
        finally:
            self._probe.start()
            shutil.rmtree(cachedir, ignore_errors=True)
        self.assertEqual((out, code), ("", 1))
        self.assertIn(orch._api_key_path("openai"), err)


class TestProbeCache(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()
        self.cachedir = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.home, ".orchestrator"))
        with open(os.path.join(self.home, ".orchestrator", "openai_api_key"),
                  "w", encoding="utf-8") as fh:
            fh.write(FAKE_KEY)
        self._env = unittest.mock.patch.dict(os.environ, {"HOME": self.home})
        self._env.start()
        self._cache = unittest.mock.patch.object(
            orch, "_probe_cache_path",
            lambda name: os.path.join(self.cachedir, name))
        self._cache.start()

    def tearDown(self):
        self._cache.stop()
        self._env.stop()
        shutil.rmtree(self.home, ignore_errors=True)
        shutil.rmtree(self.cachedir, ignore_errors=True)

    def _cache_file(self):
        return os.path.join(self.cachedir, ".api_probe_openai.json")

    def test_probe_ok_cached_then_no_http_within_ttl(self):
        calls = []
        def fake_urlopen(req, timeout=None):
            calls.append(req.full_url)
            return _FakeResp({"data": []})
        with unittest.mock.patch.object(orch.urllib.request, "urlopen",
                                        fake_urlopen):
            self.assertEqual(orch.detect_api_available({}, "openai"),
                             (True, ""))
            self.assertEqual(orch.detect_api_available({}, "openai"),
                             (True, ""))
        self.assertEqual(len(calls), 1)   # second verdict came from disk
        self.assertNotIn(FAKE_KEY, calls[0])   # key in header, not URL
        with open(self._cache_file(), encoding="utf-8") as fh:
            c = json.load(fh)
        self.assertTrue(c["ok"])
        self.assertNotIn(FAKE_KEY, json.dumps(c))

    def test_expired_ttl_reprobes(self):
        calls = []
        def fake_urlopen(req, timeout=None):
            calls.append(1)
            return _FakeResp({"data": []})
        with unittest.mock.patch.object(orch.urllib.request, "urlopen",
                                        fake_urlopen):
            orch.detect_api_available({}, "openai")
            with open(self._cache_file(), encoding="utf-8") as fh:
                c = json.load(fh)
            c["ts"] = time.time() - 5 * 3600
            with open(self._cache_file(), "w", encoding="utf-8") as fh:
                json.dump(c, fh)
            orch.detect_api_available({}, "openai")
        self.assertEqual(len(calls), 2)

    def test_changed_key_file_invalidates_cache(self):
        # A fixed key must be picked up without waiting out the TTL.
        calls = []
        def fake_urlopen(req, timeout=None):
            calls.append(1)
            return _FakeResp({"data": []})
        key_p = os.path.join(self.home, ".orchestrator", "openai_api_key")
        with unittest.mock.patch.object(orch.urllib.request, "urlopen",
                                        fake_urlopen):
            orch.detect_api_available({}, "openai")
            os.utime(key_p, (time.time() + 5, time.time() + 5))
            orch.detect_api_available({}, "openai")
        self.assertEqual(len(calls), 2)

    def test_missing_key_verdict_names_path_and_is_cached(self):
        os.remove(os.path.join(self.home, ".orchestrator", "openai_api_key"))
        def exploding_urlopen(req, timeout=None):
            raise AssertionError("no key -> no probe HTTP")
        with unittest.mock.patch.object(orch.urllib.request, "urlopen",
                                        exploding_urlopen):
            ok, reason = orch.detect_api_available({}, "openai")
        self.assertFalse(ok)
        self.assertIn(orch._api_key_path("openai"), reason)
        self.assertIn("file-only", reason)

    def test_unknown_provider_refused(self):
        ok, reason = orch.detect_api_available({}, "bogus")
        self.assertFalse(ok)
        self.assertIn("unknown api provider", reason)

    def test_google_fallback_key_mtime_invalidates_cache(self):
        # Only gemini_api_key exists (the documented google fallback): the
        # probe must stat THAT file, so fixing the key reprobes immediately
        # instead of pinning mtime to the nonexistent canonical path.
        gem_p = os.path.join(self.home, ".orchestrator", "gemini_api_key")
        with open(gem_p, "w", encoding="utf-8") as fh:
            fh.write(FAKE_KEY)
        calls = []
        def fake_urlopen(req, timeout=None):
            calls.append(1)
            return _FakeResp({"models": []})
        with unittest.mock.patch.object(orch.urllib.request, "urlopen",
                                        fake_urlopen):
            orch.detect_api_available({}, "google")
            os.utime(gem_p, (time.time() + 5, time.time() + 5))
            orch.detect_api_available({}, "google")
        self.assertEqual(len(calls), 2)

    def test_google_auth_error_names_the_fallback_file_actually_read(self):
        gem_p = os.path.join(self.home, ".orchestrator", "gemini_api_key")
        with open(gem_p, "w", encoding="utf-8") as fh:
            fh.write("revoked-key")
        def fake_urlopen(req, timeout=None):
            raise _http_error(401)
        with unittest.mock.patch.object(orch.urllib.request, "urlopen",
                                        fake_urlopen):
            ok, reason = orch.detect_api_available({}, "google")
        _close_errors()
        self.assertFalse(ok)
        self.assertIn("gemini_api_key", reason)
        self.assertNotIn("google_api_key", reason)

    def test_probe_receive_phase_timeout_is_cached_not_raised(self):
        def fake_urlopen(req, timeout=None):
            raise TimeoutError("timed out")
        with unittest.mock.patch.object(orch.urllib.request, "urlopen",
                                        fake_urlopen):
            ok, reason = orch.detect_api_available({}, "openai")
        self.assertFalse(ok)
        self.assertIn("unreachable (network)", reason)


class TestSecretLeakGate(_ApiBase):
    """Planted-fake-key gate: the key reaches ONLY the HTTP auth header."""

    def test_key_never_in_returned_tuple_env_or_subprocess(self):
        env_before = dict(os.environ)
        def no_subprocess(*a, **k):
            raise AssertionError("api: runners must spawn no subprocess")
        outcomes = [_FakeResp(_SUCCESS["anthropic"]), _http_error(401),
                    urllib.error.URLError("down")]
        for provider, runner in _RUNNERS.items():
            for outcome in outcomes:
                def fake_urlopen(req, timeout=None, _o=outcome, _p=provider):
                    if isinstance(_o, Exception):
                        raise _o
                    return _FakeResp(_SUCCESS[_p])
                with self._patched(fake_urlopen), \
                        unittest.mock.patch("subprocess.Popen", no_subprocess), \
                        unittest.mock.patch("subprocess.run", no_subprocess):
                    out, err, code, cmd = runner(self.cfg, "hi", 30, "m1")
                for field in (out, err, cmd):
                    self.assertNotIn(FAKE_KEY, str(field),
                                     (provider, type(outcome).__name__))
        self.assertEqual(env_before, dict(os.environ),
                         "api: runners must not mutate os.environ")

    def test_call_log_on_disk_never_contains_key(self):
        # The REAL sink: run a real api turn, persist it through the real
        # write_call_log into a scratch LOG_DIR, and scan the actual bytes.
        def fake_urlopen(req, timeout=None):
            return _FakeResp(_SUCCESS["anthropic"])
        with self._patched(fake_urlopen):
            out, err, code, cmd = orch.run_anthropic_api(self.cfg, "hi", 30, "m1")
        logdir = tempfile.mkdtemp()
        try:
            with unittest.mock.patch.object(orch, "LOG_DIR", logdir):
                orch.write_call_log("leakapp", "phase1", 1,
                                    "api:anthropic:m1", cmd, out, err, code)
            written = os.listdir(logdir)
            self.assertEqual(len(written), 1)
            with open(os.path.join(logdir, written[0]),
                      encoding="utf-8") as fh:
                self.assertNotIn(FAKE_KEY, fh.read())
        finally:
            shutil.rmtree(logdir, ignore_errors=True)

    def test_static_source_no_subprocess_or_env_in_api_paths(self):
        for fn in (orch._api_key, orch._api_http, orch.run_anthropic_api,
                   orch.run_openai_api, orch.run_google_api,
                   orch.detect_api_available):
            src = inspect.getsource(fn)
            for forbidden in ("subprocess", "Popen", "os.environ",
                              "putenv", "?key="):
                self.assertNotIn(forbidden, src, (fn.__name__, forbidden))


if __name__ == "__main__":
    unittest.main()
