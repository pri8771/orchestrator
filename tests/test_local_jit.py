import json
import fcntl
import multiprocessing
import os
import tempfile
import unittest
from unittest import mock

import localmodels as lm
import orchestrator as orch
import sections as seclib


GB = 1024 ** 3


def _mark_worker(root, model, timestamp):
    lm.mark_model_used(model, here=root, now=timestamp)


class _Response:
    def __init__(self, payload):
        self.data = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return self.data


class TestEvictOrder(unittest.TestCase):
    def test_lru_order_and_pin_exemption(self):
        with tempfile.TemporaryDirectory() as root:
            with open(os.path.join(root, lm.REGISTRY_FILENAME), "w",
                      encoding="utf-8") as fh:
                json.dump({"schema_version": 1, "models": [
                    {"id": "incoming", "size_gb": 1}
                ]}, fh)
            lm.mark_model_used("old", here=root, now=1, size_bytes=GB)
            lm.mark_model_used("pinned", here=root, now=2, size_bytes=GB,
                               pins=["pinned"])
            lm.mark_model_used("new", here=root, now=3, size_bytes=GB)
            live = [
                {"model": "old", "size_bytes": GB},
                {"model": "pinned", "size_bytes": GB},
                {"model": "new", "size_bytes": GB},
            ]
            unloaded = []
            warnings = []
            result = lm.ensure_capacity(
                "incoming", 3, pins=["pinned"], here=root,
                ps_probe=lambda: live,
                unload=lambda model: unloaded.append(model) or True,
                on_warn=warnings.append)
            self.assertEqual(result, ["old"])
            self.assertEqual(unloaded, ["old"])
            self.assertNotIn("pinned", unloaded)

    def test_all_pinned_over_budget_proceeds_with_warning(self):
        with tempfile.TemporaryDirectory() as root:
            warnings = []
            unloaded = []
            result = lm.ensure_capacity(
                "incoming", 1, pins=["pinned"], here=root,
                ps_probe=lambda: [{"model": "pinned", "size_bytes": GB}],
                unload=lambda model: unloaded.append(model) or True,
                on_warn=warnings.append)
            self.assertEqual(result, [])
            self.assertEqual(unloaded, [])
            self.assertTrue(any("over 1.0GB budget" in w for w in warnings))

    def test_unload_failure_is_visible_and_nonfatal(self):
        with tempfile.TemporaryDirectory() as root:
            warnings = []
            result = lm.ensure_capacity(
                "incoming", 1, here=root,
                ps_probe=lambda: [{"model": "victim", "size_bytes": GB}],
                unload=lambda _model: False, on_warn=warnings.append)
            self.assertEqual(result, [])
            self.assertTrue(any("over 1.0GB budget" in w for w in warnings))


class TestLedger(unittest.TestCase):
    def test_concurrent_updates_do_not_lose_entries(self):
        with tempfile.TemporaryDirectory() as root:
            processes = [multiprocessing.Process(
                target=_mark_worker, args=(root, "model-%d" % i, i + 1))
                for i in range(8)]
            for process in processes:
                process.start()
            for process in processes:
                process.join(10)
                self.assertEqual(process.exitcode, 0)
            ledger = lm.read_loaded_ledger(root)
            self.assertEqual(set(ledger["models"]),
                             {"model-%d" % i for i in range(8)})

    def test_corrupt_ledger_rebuilds_from_ps_truth_with_warning(self):
        with tempfile.TemporaryDirectory() as root:
            runtime = os.path.join(root, "runtime")
            os.makedirs(runtime)
            with open(os.path.join(runtime, lm.LOADED_MODELS_FILENAME), "w",
                      encoding="utf-8") as fh:
                fh.write("{broken")
            warnings = []
            lm.ensure_capacity(
                "live", 10, here=root,
                ps_probe=lambda: [{"model": "live", "size_bytes": GB}],
                unload=lambda _model: True, on_warn=warnings.append)
            ledger = lm.read_loaded_ledger(root)
            self.assertEqual(set(ledger["models"]), {"live"})
            self.assertTrue(any("corrupt" in w for w in warnings))

    def test_ps_residency_drops_stale_ledger_entries(self):
        with tempfile.TemporaryDirectory() as root:
            lm.mark_model_used("stale", here=root, now=1, size_bytes=GB)
            lm.ensure_capacity("live", 10, here=root,
                               ps_probe=lambda: [{"model": "live",
                                                  "size_bytes": GB}],
                               unload=lambda _model: True)
            self.assertEqual(set(lm.read_loaded_ledger(root)["models"]), {"live"})

    def test_lock_timeout_warns_and_returns(self):
        with tempfile.TemporaryDirectory() as root:
            runtime = os.path.join(root, "runtime")
            os.makedirs(runtime)
            lock_path = os.path.join(runtime, lm.LOADED_MODELS_LOCK_FILENAME)
            warnings = []
            with open(lock_path, "a+", encoding="utf-8") as held:
                fcntl.flock(held.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                self.assertFalse(lm.mark_model_used(
                    "x", here=root, on_warn=warnings.append, lock_timeout=0))
            self.assertTrue(any("timed out" in w for w in warnings))


class TestRunLocalPolicy(unittest.TestCase):
    def _capture(self, cfg, pins):
        bodies = []

        def opener(req, timeout=None):
            bodies.append(json.loads(req.data.decode("utf-8")))
            return _Response({"response": "ok", "prompt_eval_count": 2,
                              "eval_count": 3})
        section = type("LoadedSection", (), {"local_pins": pins})()
        with mock.patch.object(orch, "_section_dir", return_value="/x/ideas"), \
                mock.patch.object(orch.seclib, "load_section", return_value=section), \
                mock.patch.object(orch.lmlib, "ensure_capacity") as capacity, \
                mock.patch.object(orch.lmlib, "mark_model_used") as marked, \
                mock.patch.object(orch.urllib.request, "urlopen", side_effect=opener):
            result = orch.run_local(cfg, "hello", 5, model="alpha")
        return result, bodies, capacity, marked

    def test_unpinned_request_has_configured_keep_alive(self):
        result, bodies, capacity, marked = self._capture(
            {"models": {"local_keep_alive": "9m",
                        "local_memory_budget_gb": 12}}, [])
        self.assertEqual(result[:3], ("ok", "", 0))
        self.assertEqual(bodies[0]["keep_alive"], "9m")
        capacity.assert_called_once()
        marked.assert_called_once()

    def test_pinned_request_uses_indefinite_keep_alive(self):
        _result, bodies, capacity, _marked = self._capture(
            {"models": {}}, ["alpha"])
        self.assertEqual(bodies[0]["keep_alive"], -1)
        self.assertEqual(capacity.call_args.kwargs["pins"], ["alpha"])

    def test_unconfigured_default_is_five_minutes(self):
        _result, bodies, _capacity, _marked = self._capture({}, [])
        self.assertEqual(bodies[0]["keep_alive"], "5m")


class TestSectionPins(unittest.TestCase):
    def test_invalid_and_uninstalled_pins_are_visible_and_ignored(self):
        warnings = []
        self.assertEqual(seclib.normalize_local_pins(
            ["installed", "missing", 7, "installed"], installed=["installed"],
            on_warn=warnings.append), ["installed"])
        self.assertTrue(any("not installed" in w for w in warnings))
        self.assertTrue(any("non-string" in w for w in warnings))


class TestOvernightSimulation(unittest.TestCase):
    def test_eleven_sections_three_models_two_slot_budget(self):
        with tempfile.TemporaryDirectory() as root:
            resident = {"pinned": GB}
            order = ["pinned", "alpha", "beta"] * 4
            completed = []

            def ps():
                return [{"model": model, "size_bytes": size}
                        for model, size in resident.items()]

            def unload(model):
                resident.pop(model, None)
                return True

            for turn, model in enumerate(order[:11]):
                lm.ensure_capacity(model, 16, pins=["pinned"], here=root,
                                   ps_probe=ps, unload=unload)
                resident[model] = GB * 8
                lm.mark_model_used(model, here=root, pins=["pinned"], now=turn)
                completed.append(model)
                self.assertLessEqual(len(resident), 2)
                self.assertIn("pinned", resident)
            self.assertEqual(len(completed), 11)
            self.assertGreaterEqual(len(set(completed)), 3)
