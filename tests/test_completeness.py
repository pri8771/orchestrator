import json, os, tempfile, unittest
import completeness as c
import workflows as w

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# The GUI's stop-target picker labels (Components.swift) — every one must
# resolve to a real app_build phase key or the option is a silent no-op.
GUI_STOP_LABELS = ["idea validated", "docs complete", "design complete",
                   "tech spec complete", "mvp built", "production ready"]
# The GUI's completeness picker tags (Components.swift / Configuration.swift).
GUI_PROFILES = ["prototype", "mvp", "v1", "production_draft"]


class TestCompleteness(unittest.TestCase):
    def _phases(self, keys):
        return [(k, k.title()) for k in keys]

    def _app_build(self):
        return w.load_workflow("app_build", HERE).phases

    def test_profile_keys_exist_in_app_build(self):
        """Every profile allow-list key must be a real app_build phase key —
        the original bug shipped spec-era keys that matched nothing."""
        wf_keys = {p.key for p in self._app_build()}
        for name, prof in c.PROFILES.items():
            for k in (prof["phase_keys"] or []):
                self.assertIn(k, wf_keys,
                              "profile %r references unknown phase %r" % (name, k))

    def test_profiles_keep_meaningful_run_for_every_workflow(self):
        """No profile may strip ANY shipped workflow below 3 phases (or the
        workflow's own size), and the final verification phase always survives."""
        for wf_name in w.list_workflows(HERE):
            phases = w.load_workflow(wf_name, HERE).phases
            for prof in GUI_PROFILES:
                kept = c.filter_phases(phases, prof)
                self.assertGreaterEqual(
                    len(kept), min(3, len(phases)),
                    "%s/%s kept only %d phases" % (wf_name, prof, len(kept)))
                self.assertEqual(kept[-1].key, phases[-1].key,
                                 "%s/%s dropped the final phase" % (wf_name, prof))

    def test_prototype_and_mvp_subset_app_build(self):
        phases = self._app_build()
        proto = [p.key for p in c.filter_phases(phases, "prototype")]
        mvp = [p.key for p in c.filter_phases(phases, "mvp")]
        for keys in (proto, mvp):
            self.assertIn("initial_discussion", keys)
            self.assertIn("build_coordination", keys)
            self.assertIn("final_review", keys)
            self.assertGreater(len(keys), 2)
        self.assertLess(len(proto), len(phases))   # still a real subset
        self.assertLess(len(proto), len(mvp) + 1)  # prototype <= mvp

    def test_mismatched_profile_falls_back_to_all(self):
        warns = []
        allp = self._phases(["x", "y", "z", "build"])
        out = c.filter_phases(allp, "prototype", on_warn=warns.append)
        self.assertEqual(len(out), 4)
        self.assertTrue(warns, "fallback must warn")

    def test_v1_and_unknown_keep_all(self):
        allp = self._phases(["a", "b", "legal_and_compliance"])
        self.assertEqual(len(c.filter_phases(allp, "v1")), 3)
        self.assertEqual(len(c.filter_phases(allp, "nonexistent")), 3)
        self.assertEqual(len(c.filter_phases(allp, "")), 3)

    def test_round_multiplier(self):
        self.assertEqual(c.round_multiplier("prototype"), 0.75)
        self.assertEqual(c.round_multiplier("production_draft"), 1.5)
        self.assertEqual(c.round_multiplier("unknown"), 1.0)

    def test_every_gui_stop_label_resolves_in_app_build(self):
        """Every GUI stop label must truncate an app_build run — the original
        bug left 4 of 5 as silent full-pipeline no-ops."""
        phases = self._app_build()
        for label in GUI_STOP_LABELS:
            out = c.apply_stop_target(phases, label)
            key = c.STOP_TARGETS[label]
            self.assertEqual(out[-1].key, key, "label %r must stop at %r" % (label, key))
            if key != phases[-1].key:
                self.assertLess(len(out), len(phases),
                                "label %r did not truncate" % label)

    def test_stop_target_key_and_label(self):
        allp = self._phases(["initial_discussion", "next_steps_small",
                             "design_discussion", "build_coordination"])
        out = c.apply_stop_target(allp, "next_steps_small")
        self.assertEqual([p[0] for p in out], ["initial_discussion", "next_steps_small"])
        out2 = c.apply_stop_target(allp, "Design Complete")  # friendly label
        self.assertEqual([p[0] for p in out2][-1], "design_discussion")
        self.assertEqual(len(c.apply_stop_target(allp, "")), 4)  # blank = unchanged

    def test_unresolvable_stop_target_warns_and_keeps_all(self):
        warns = []
        allp = self._phases(["a", "b", "c"])
        out = c.apply_stop_target(allp, "no_such_phase", on_warn=warns.append)
        self.assertEqual(len(out), 3)
        self.assertTrue(warns, "unresolvable stop target must warn")

    def test_load_run_config(self):
        d = tempfile.mkdtemp()
        with open(os.path.join(d, "run_config.json"), "w") as fh:
            json.dump({"completeness": "mvp", "stop_after_phase": "design_discussion"}, fh)
        rc = c.load_run_config(d)
        self.assertEqual(rc["completeness"], "mvp")
        self.assertEqual(c.load_run_config(tempfile.mkdtemp()), {})  # no file


if __name__ == "__main__":
    unittest.main()
