import XCTest
@testable import OrchestratorGUI

/// model_routing.json is SHARED with the engine — a GUI save must be a merge,
/// never a reconstruction. The old rebuild-from-typed-fields save destroyed
/// `_examples`, the long `_docs`, `fallback.chains: {}`, phase- and role-level
/// `gemini_reasoning`/`ollama_reasoning` (engine-honored, GUI-unmodeled),
/// downgraded `schema_version`, and dropped the trailing newline.
final class ModelRoutingRoundTripTests: XCTestCase {
    private func tempFile(_ contents: String) throws -> URL {
        let url = FileManager.default.temporaryDirectory
            .appendingPathComponent("routing-\(UUID().uuidString).json")
        try contents.write(to: url, atomically: true, encoding: .utf8)
        addTeardownBlock { try? FileManager.default.removeItem(at: url) }
        return url
    }

    private let engineShapedFile = """
    {
      "schema_version": 2,
      "_docs": "Long engine-authored field reference that the GUI must not replace.",
      "_examples": {"phases": {"build_coordination": {"timeout": 1800}}},
      "enabled": true,
      "fallback": {"cloud_to_local": true, "local_model": "", "chains": {}},
      "phases": {
        "tech_specs": {
          "codex_reasoning": "high",
          "gemini_reasoning": "high",
          "roles": {
            "worker": {"ollama": "qwen3:14b", "ollama_reasoning": "low"},
            "custom_role_key": {"note": "unmodeled sibling survives"}
          }
        },
        "ghost_phase": {"future_key": "engine-only phase survives too"}
      }
    }
    """

    private func rootObject(at url: URL) throws -> [String: Any] {
        let data = try Data(contentsOf: url)
        return try XCTUnwrap(
            JSONSerialization.jsonObject(with: data) as? [String: Any])
    }

    func testUnmodeledKeysSurviveLoadSaveRoundTrip() throws {
        let url = try tempFile(engineShapedFile)
        let routing = ModelRouting.load(from: url)
        routing.save(to: url)

        let root = try rootObject(at: url)
        XCTAssertEqual(root["schema_version"] as? Int, 2,
                       "schema_version must never be downgraded")
        XCTAssertEqual(root["_docs"] as? String,
                       "Long engine-authored field reference that the GUI must not replace.")
        XCTAssertNotNil(root["_examples"], "_examples must survive")
        let fb = try XCTUnwrap(root["fallback"] as? [String: Any])
        XCTAssertNotNil(fb["chains"], "explicit empty chains key must survive")

        let phases = try XCTUnwrap(root["phases"] as? [String: Any])
        let tech = try XCTUnwrap(phases["tech_specs"] as? [String: Any])
        XCTAssertEqual(tech["gemini_reasoning"] as? String, "high",
                       "phase-level unmodeled reasoning must survive")
        XCTAssertEqual(tech["codex_reasoning"] as? String, "high")
        let roles = try XCTUnwrap(tech["roles"] as? [String: Any])
        let worker = try XCTUnwrap(roles["worker"] as? [String: Any])
        XCTAssertEqual(worker["ollama_reasoning"] as? String, "low",
                       "role-level unmodeled reasoning must survive")
        XCTAssertEqual(worker["ollama"] as? String, "qwen3:14b")
        XCTAssertNotNil(roles["custom_role_key"],
                        "unmodeled roles sibling must survive")
        XCTAssertNotNil(phases["ghost_phase"],
                        "a phase with only unmodeled keys must survive")

        let text = try String(contentsOf: url, encoding: .utf8)
        XCTAssertTrue(text.hasSuffix("\n"), "engine files end in a newline")
    }

    func testTypedEditsStillLandAndClearedFieldsStillClear() throws {
        let url = try tempFile(engineShapedFile)
        var routing = ModelRouting.load(from: url)
        routing.phases["tech_specs"]?.codexReasoning = ""    // cleared
        routing.phases["tech_specs"]?.claude = "claude-opus-5"  // edited
        routing.save(to: url)

        let root = try rootObject(at: url)
        let phases = try XCTUnwrap(root["phases"] as? [String: Any])
        let tech = try XCTUnwrap(phases["tech_specs"] as? [String: Any])
        XCTAssertNil(tech["codex_reasoning"], "cleared typed field is removed")
        XCTAssertEqual(tech["claude"] as? String, "claude-opus-5")
        XCTAssertEqual(tech["gemini_reasoning"] as? String, "high",
                       "residual survives a typed edit in the same phase")
    }

    func testFreshFileStillGetsMetadata() throws {
        let url = FileManager.default.temporaryDirectory
            .appendingPathComponent("routing-fresh-\(UUID().uuidString).json")
        addTeardownBlock { try? FileManager.default.removeItem(at: url) }
        var routing = ModelRouting()
        routing.phases["tech_specs"] = {
            var p = PhaseRoute(); p.codexReasoning = "high"; return p
        }()
        routing.save(to: url)
        let root = try rootObject(at: url)
        XCTAssertEqual(root["schema_version"] as? Int, 1)
        XCTAssertNotNil(root["_docs"])
        XCTAssertNotNil(root["fallback"])
        let phases = try XCTUnwrap(root["phases"] as? [String: Any])
        XCTAssertNotNil(phases["tech_specs"])
    }

    func testUnknownKeysNeverMakeTheGridDirty() throws {
        let url = try tempFile(engineShapedFile)
        let a = ModelRouting.load(from: url)
        var b = a
        b.rawRoot = [:]   // raw carry differs, modeled fields identical
        XCTAssertEqual(a, b, "equality must cover modeled fields only")
    }
}
