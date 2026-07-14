import XCTest
@testable import OrchestratorGUI

// PhaseRoute.roles (model_routing.json phases.<key>.roles) round-trip:
// operators hand-edit worker/integrator overrides directly in the file, and
// the GUI's Routing Grid must preserve them on save even when no UI edit
// touches them, and reflect an edit when one is made (Item 1, round 5).
final class RoutingRolesTests: XCTestCase {
    private func tempURL() -> URL {
        let dir = FileManager.default.temporaryDirectory
            .appendingPathComponent("routing-roles-tests-\(UUID().uuidString)")
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        return dir.appendingPathComponent("model_routing.json")
    }

    // A hand-edited "roles" sub-dict survives load() -> save() unchanged when
    // no UI edit touches the phase — the concrete bug this item fixes.
    func testHandEditedRolesSurviveUnrelatedSave() throws {
        let url = tempURL()
        let json = """
        {
          "schema_version": 1,
          "enabled": true,
          "fallback": {"cloud_to_local": true, "local_model": ""},
          "phases": {
            "build_coordination": {
              "codex_reasoning": "high",
              "roles": {
                "worker": {"codex_reasoning": "low"},
                "integrator": {"claude_reasoning": "high", "claude": "opus"}
              }
            }
          }
        }
        """
        try json.write(to: url, atomically: true, encoding: .utf8)

        var r = ModelRouting.load(from: url)
        XCTAssertEqual(r.phases["build_coordination"]?.roles?.worker.codexReasoning, "low")
        XCTAssertEqual(r.phases["build_coordination"]?.roles?.integrator.claudeReasoning, "high")
        XCTAssertEqual(r.phases["build_coordination"]?.roles?.integrator.claude, "opus")

        // Simulate an unrelated Routing Grid edit — a top-level field only.
        r.phases["build_coordination"]?.codexReasoning = "medium"
        r.save(to: url)

        let reloaded = ModelRouting.load(from: url)
        XCTAssertEqual(reloaded.phases["build_coordination"]?.codexReasoning, "medium")
        // The hand-added roles sub-dict must NOT have been silently dropped.
        XCTAssertEqual(reloaded.phases["build_coordination"]?.roles?.worker.codexReasoning, "low")
        XCTAssertEqual(reloaded.phases["build_coordination"]?.roles?.integrator.claudeReasoning, "high")
        XCTAssertEqual(reloaded.phases["build_coordination"]?.roles?.integrator.claude, "opus")
    }

    // A programmatic role edit (what the new Role overrides popover writes)
    // round-trips correctly.
    func testProgrammaticRoleEditRoundTrips() throws {
        var r = ModelRouting()
        var p = PhaseRoute()
        var roles = RoleOverrides()
        roles.worker.codexReasoning = "low"
        roles.integrator.claudeReasoning = "high"
        roles.integrator.gemini = "gemini-2.5-pro"
        p.roles = roles
        r.phases["spec"] = p

        let url = tempURL()
        r.save(to: url)
        let loaded = ModelRouting.load(from: url)
        XCTAssertEqual(loaded.phases["spec"]?.roles?.worker.codexReasoning, "low")
        XCTAssertEqual(loaded.phases["spec"]?.roles?.integrator.claudeReasoning, "high")
        XCTAssertEqual(loaded.phases["spec"]?.roles?.integrator.gemini, "gemini-2.5-pro")
        // Unset fields stay unset.
        XCTAssertEqual(loaded.phases["spec"]?.roles?.worker.claude, "")
        XCTAssertEqual(loaded.phases["spec"]?.roles?.integrator.ollama, "")
    }

    // A phase with only a "roles" sub-dict and no top-level fields is not
    // itself empty, and does not round-trip an empty "roles": {}.
    func testRolesOnlyPhaseIsNotEmptyAndOmitsEmptyRolesKey() {
        var p = PhaseRoute()
        XCTAssertTrue(p.isEmpty)
        var roles = RoleOverrides()
        roles.worker.claude = "haiku"
        p.roles = roles
        XCTAssertFalse(p.isEmpty)

        var r = ModelRouting()
        r.phases["review_gate"] = p
        let url = tempURL()
        r.save(to: url)
        let raw = try! JSONSerialization.jsonObject(with: Data(contentsOf: url)) as! [String: Any]
        let phases = raw["phases"] as! [String: Any]
        let phase = phases["review_gate"] as! [String: Any]
        XCTAssertNotNil(phase["roles"])
        let rolesObj = phase["roles"] as! [String: Any]
        XCTAssertNotNil(rolesObj["worker"])
        XCTAssertNil(rolesObj["integrator"], "empty integrator overrides must not be emitted")

        // A phase that never had roles must never gain an empty "roles" key.
        var q = PhaseRoute()
        q.claude = "sonnet"
        r.phases["spec"] = q
        r.save(to: url)
        let raw2 = try! JSONSerialization.jsonObject(with: Data(contentsOf: url)) as! [String: Any]
        let phases2 = raw2["phases"] as! [String: Any]
        let spec = phases2["spec"] as! [String: Any]
        XCTAssertNil(spec["roles"])
    }

    // Unknown role names in a hand-edited file are dropped by the engine
    // (modelrouting.py ROLE_NAMES); the GUI mirrors that by only ever
    // parsing "worker" and "integrator" keys, ignoring anything else.
    func testUnknownRoleNameIgnored() throws {
        let url = tempURL()
        let json = """
        {"phases": {"spec": {"roles": {"reviewer": {"claude": "opus"}, "worker": {"claude": "haiku"}}}}}
        """
        try json.write(to: url, atomically: true, encoding: .utf8)
        let r = ModelRouting.load(from: url)
        XCTAssertEqual(r.phases["spec"]?.roles?.worker.claude, "haiku")
        XCTAssertTrue(r.phases["spec"]?.roles?.integrator.isEmpty ?? true)
    }
}
