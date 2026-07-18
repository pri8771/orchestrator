"""V3 board 3.3: externalized phase contracts — byte parity is the product.

The frozen fixture (tests/fixtures/contracts_frozen.json) was captured
from the pre-3.3 constant-based _phase_contract; the data-driven assembly
must reproduce every case exactly, plus section overlays, the
library_mining gate, extraction round-trips, and the corrupt-file banner.
"""
import json
import os
import shutil
import tempfile
import unittest

import events as evlib
import orchestrator as orch
import schemas as schemalib
import sections as seclib
import workflows as wf

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FROZEN = os.path.join(HERE, "tests", "fixtures", "contracts_frozen.json")


def _phase(key, writes=False):
    return wf.Phase(key, ".", "x.md", "p", rounds=2, writes=writes)


class TestByteParity(unittest.TestCase):
    def test_every_frozen_case_reproduces_exactly(self):
        with open(FROZEN, encoding="utf-8") as fh:
            doc = json.load(fh)
        self.assertGreaterEqual(len(doc["cases"]), 9)
        for case in doc["cases"]:
            cfg = {"_workflow_target": case["workflow_target"]}
            out = orch._phase_contract(
                cfg, _phase(case["phase_key"], writes=case["writes"]))
            self.assertEqual(out, case["text"],
                             "byte drift for %s/%s" % (case["phase_key"],
                                                       case["workflow_target"]))

    def test_library_mining_gate_rides_the_data(self):
        lm = orch._phase_contract({"_workflow_target": "library_mining"},
                                  _phase("extraction_candidates"))
        other = orch._phase_contract({"_workflow_target": "app"},
                                     _phase("extraction_candidates"))
        self.assertIn("extraction-json", lm)
        self.assertNotIn("extraction-json", other,
                         "an unrelated workflow must not inherit the contract")

    def test_summary_is_always_appended(self):
        for key, writes in (("anything", False), ("build_coordination", True)):
            out = orch._phase_contract({"_workflow_target": "research"},
                                       _phase(key, writes=writes))
            self.assertIn("phase-summary-json", out)


class TestSingleSource(unittest.TestCase):
    def test_extraction_round_trip_from_the_same_data(self):
        # Emit a block using the DATA's fence + required fields; the
        # extraction call must parse it — publication and extraction can
        # never disagree because both read one entry.
        fence, req_key = seclib.contract_fence("phase_summary")
        self.assertEqual(fence, "phase-summary-json")
        fields = schemalib.REQUIRED_FIELDS[req_key]
        payload = {f: "x" for f in fields}
        text = "prose\n```%s\n%s\n```\n" % (fence, json.dumps(payload))
        blocks = schemalib.extract_structured_blocks(
            text, fence, required_fields=fields)
        self.assertEqual(len(blocks), 1)

    def test_every_default_fence_tag_is_the_historical_one(self):
        tags = {e["contract"]: e["fence_tag"]
                for e in seclib.DEFAULT_CONTRACTS}
        self.assertEqual(tags, {
            "tasks": "tasks-json", "flows": "flows-json",
            "requirements": "requirements-json",
            "interfaces": "interfaces-json",
            "extraction": "extraction-json",
            "decisions": "decisions-json",
            "artifact": "artifact-json",
            "phase_summary": "phase-summary-json"})


class TestSectionOverlay(unittest.TestCase):
    def setUp(self):
        self.orch_dir = tempfile.mkdtemp(prefix="orch_contracts_")
        self.app_dir = tempfile.mkdtemp(prefix="orch_contracts_app_")

    def tearDown(self):
        shutil.rmtree(self.orch_dir, ignore_errors=True)
        shutil.rmtree(self.app_dir, ignore_errors=True)

    def _write(self, section, obj_or_text):
        d = os.path.join(self.orch_dir, "sections", section)
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, seclib.CONTRACTS_FILENAME)
        with open(path, "w", encoding="utf-8") as fh:
            if isinstance(obj_or_text, str):
                fh.write(obj_or_text)
            else:
                json.dump(obj_or_text, fh)

    def test_overlay_replaces_and_adds(self):
        self._write("research", {"contracts": [
            {"phase_key": "@summary", "contract": "phase_summary",
             "fence_tag": "phase-summary-json",
             "prompt_snippet": "SECTION SUMMARY SNIPPET\n"},
            {"phase_key": "findings", "contract": "findings",
             "fence_tag": "finding-json",
             "prompt_snippet": "SECTION FINDINGS SNIPPET\n"},
        ]})
        contracts = seclib.load_contracts(self.orch_dir, section="research",
                                          app_dir=self.app_dir)
        out = seclib.assemble_contract(contracts, "findings", "research",
                                       include_decisions=False)
        self.assertIn("SECTION FINDINGS SNIPPET", out)
        self.assertIn("SECTION SUMMARY SNIPPET", out)
        self.assertNotIn("one_paragraph_summary", out,
                         "the overlay REPLACES the default summary entry")
        self.assertEqual(evlib.read_events(self.app_dir,
                                           kinds=["config_fallback"]), [])

    def test_absent_is_silent_corrupt_banners_and_defaults(self):
        contracts = seclib.load_contracts(self.orch_dir, section="research",
                                          app_dir=self.app_dir)
        self.assertEqual(len(contracts), len(seclib.DEFAULT_CONTRACTS))
        self.assertEqual(evlib.read_events(self.app_dir,
                                           kinds=["config_fallback"]), [])
        self._write("research", "{broken")
        contracts = seclib.load_contracts(self.orch_dir, section="research",
                                          app_dir=self.app_dir)
        self.assertEqual(len(contracts), len(seclib.DEFAULT_CONTRACTS),
                         "corrupt overlay must fall back to FULL defaults")
        evts = evlib.read_events(self.app_dir, kinds=["config_fallback"])
        self.assertEqual(len(evts), 1)
        self.assertIn("contracts.json", evts[0]["file"])


if __name__ == "__main__":
    unittest.main()
