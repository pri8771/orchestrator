"""V3 8.4: platform policy lives exclusively in the Build section."""

import hashlib
import json
import os
import shutil
import tempfile
import unittest

import buildpolicy
import completeness
import designlint
import orchestrator
import phase_rules


HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MARKERS = ("swiftui", "xcodebuild", "uikit", "xcode", ".xcodeproj",
           "accessibilityidentifier", "alamofire")


def _sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class TestBuildTargetPolicy(unittest.TestCase):
    def setUp(self):
        buildpolicy.clear_cache()

    def test_ios_prompt_surfaces_are_byte_identical_to_pre_move(self):
        # Fixed hashes were captured from dev immediately before the 8.4 move.
        # They pin actual rendered bytes, not merely the presence of keywords.
        self.assertEqual(
            _sha(phase_rules.render_phase_playbook(
                HERE, "app", "build_coordination")),
            "e768ef263466a3d8b3e8fd649b05111c758a2d53155897c58cff82eac5e41ea9")
        self.assertEqual(
            _sha(phase_rules.render_phase_quality_rubric(
                HERE, "app", "build_coordination")),
            "16862d54aee2f0968975a969b55c7cf5b18c099fae6febd50b3af4e5d452d222")
        self.assertEqual(
            _sha(designlint.render_tech_stack(
                designlint.load_tech_stack(HERE))),
            "310b0900485738175ca8eb299fe216591dc13388e2b82db3d929eb7f26f06fdb")
        self.assertEqual(
            _sha(completeness.render_dod(HERE, "v1", "app")),
            "a1439a5741a280d320fd4a3245dfc2fe23b0f6bbfdc99e92fe135aa5cbda014b")
        self.assertEqual(
            _sha(orchestrator.phase_extra(
                {"_workflow_target": "app", "_app_dir": None},
                "ios_architecture_review")),
            "c9685d8350092db05a35b46745377bf5896dcc785e2d1575a09c31c6c7bcc20c")

    def test_neutral_quality_bar_remains_and_is_enforced_for_apps(self):
        with open(os.path.join(HERE, "phase_rules.json"), encoding="utf-8") as fh:
            neutral = json.load(fh)["global_app_rules"]
        expected = (
            "No two text or control elements may visually overlap",
            "The interface must never claim an outcome that has not happened",
            "Empty states are designed content",
        )
        for fragment in expected:
            self.assertTrue(any(fragment in item for item in neutral), fragment)
        rendered = phase_rules.render_phase_playbook(
            HERE, "app", "build_coordination")
        for fragment in expected:
            self.assertIn(fragment, rendered)

    def test_non_app_playbooks_receive_no_platform_policy(self):
        for target, phase in (("research", "product_research"),
                              ("audit", "recon"),
                              ("brainstorm", "initial_discussion")):
            rendered = phase_rules.render_phase_playbook(HERE, target, phase)
            lowered = rendered.lower()
            self.assertFalse([m for m in MARKERS if m in lowered],
                             "%s/%s leaked Build policy" % (target, phase))
        self.assertIsNone(buildpolicy.load_target_policy(HERE, "research"))
        neutral_dod = completeness.render_dod(HERE, "v1", "research").lower()
        self.assertFalse([m for m in MARKERS if m in neutral_dod])

    def test_corrupt_editable_policy_banners_and_uses_nonempty_seed(self):
        root = tempfile.mkdtemp()
        shutil.copytree(os.path.join(HERE, "sections", "build"),
                        os.path.join(root, "sections", "build"))
        active = os.path.join(root, "sections", "build", "target_policy.json")
        with open(active, "w", encoding="utf-8") as fh:
            fh.write("{broken")
        seen = []
        # A headless first read must not suppress the later visible banner.
        headless = buildpolicy.load_target_policy(root, "app")
        self.assertTrue(headless["app_rules"])
        policy = buildpolicy.load_target_policy(
            root, "app", on_fallback=lambda path, error: seen.append(
                (path, str(error))))
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0][0], active)
        self.assertTrue(policy["app_rules"])
        self.assertTrue(policy["phase_rules"]["build_coordination"]["rules"])
        self.assertTrue(policy["tech_stack"]["banned"])
        self.assertTrue(policy["dod"]["mvp"])
        # The cached corrupt version does not flood one banner per consumer.
        buildpolicy.load_target_policy(
            root, "app", on_fallback=lambda path, error: seen.append(
                (path, str(error))))
        self.assertEqual(len(seen), 1)

    def test_malformed_tech_stack_entries_engage_seed_fallback(self):
        # A-29: render_tech_stack does e["name"] / " — " + e["for"]
        # unconditionally, so entry shapes the renderer cannot handle must be
        # rejected by validation (engaging the seed) instead of being cached
        # VALID and crashing every app-workflow run at startup.
        for bad_entry in ("GRDB",                      # bare-string shorthand
                          {},                          # no name
                          {"name": "GRDB", "for": 123}):  # non-str for
            buildpolicy.clear_cache()
            root = tempfile.mkdtemp()
            self.addCleanup(shutil.rmtree, root, True)
            shutil.copytree(os.path.join(HERE, "sections", "build"),
                            os.path.join(root, "sections", "build"))
            active = os.path.join(root, "sections", "build",
                                  "target_policy.json")
            with open(active, encoding="utf-8") as fh:
                data = json.load(fh)
            data["targets"]["ios"]["tech_stack"]["allowed"].append(bad_entry)
            with open(active, "w", encoding="utf-8") as fh:
                json.dump(data, fh)
            seen = []
            policy = buildpolicy.load_target_policy(
                root, "app", on_fallback=lambda path, error: seen.append(
                    (path, str(error))))
            self.assertEqual([p for p, _ in seen], [active],
                             "seed fallback must engage for %r" % (bad_entry,))
            self.assertIn("tech_stack", seen[0][1])
            # The resolved (seed) stack must render without crashing.
            designlint.render_tech_stack(policy["tech_stack"])

    def test_global_and_non_build_manifests_are_platform_neutral(self):
        paths = [os.path.join(HERE, name) for name in (
            "phase_rules.json", "tech_stack.json", "definition_of_done.json")]
        sections_dir = os.path.join(HERE, "sections")
        for dirname, _dirs, files in os.walk(sections_dir):
            if os.path.abspath(dirname).startswith(
                    os.path.abspath(os.path.join(sections_dir, "build"))):
                continue
            paths.extend(os.path.join(dirname, name) for name in files
                         if name.endswith(".json"))
        violations = []
        for path in paths:
            with open(path, encoding="utf-8") as fh:
                lowered = fh.read().lower()
            for marker in MARKERS:
                if marker in lowered:
                    violations.append("%s: %s" %
                                      (os.path.relpath(path, HERE), marker))
        self.assertEqual(violations, [], "platform marker grep gate failed")

    def test_build_section_is_the_only_source_of_ios_dependency_policy(self):
        with open(os.path.join(HERE, "tech_stack.json"), encoding="utf-8") as fh:
            fleet = json.load(fh)
        self.assertEqual(fleet["banned"], [])
        stack = designlint.load_tech_stack(HERE)
        self.assertIn("Alamofire", [entry["name"] for entry in stack["banned"]])
        with open(os.path.join(HERE, "sections", "build",
                               "target_policy.json"), encoding="utf-8") as fh:
            active = json.load(fh)
        with open(os.path.join(HERE, "sections", "build",
                               "target_policy.seed.json"), encoding="utf-8") as fh:
            seed = json.load(fh)
        self.assertEqual(active, seed,
                         "editable policy and shipped fallback must start equal")


if __name__ == "__main__":
    unittest.main()
