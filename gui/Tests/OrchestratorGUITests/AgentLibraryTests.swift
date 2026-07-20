import XCTest
@testable import OrchestratorGUI

final class AgentLibraryTests: XCTestCase {
    private func directory() throws -> URL {
        let url = FileManager.default.temporaryDirectory
            .appendingPathComponent("agent-library-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: url, withIntermediateDirectories: true)
        return url
    }

    func testSectionPersonasAndPresetsShadowFleetAndWarningsStayVisible() throws {
        let root = try directory()
        let fleet = root.appendingPathComponent("agent_library.json")
        let sectionDir = root.appendingPathComponent("sections/research")
        try FileManager.default.createDirectory(at: root.appendingPathComponent("presets"),
                                                withIntermediateDirectories: true)
        try FileManager.default.createDirectory(at: sectionDir.appendingPathComponent("presets"),
                                                withIntermediateDirectories: true)
        try #"{"personas":[{"id":"p","name":"Fleet","preamble":"f","backend":"api:openai","preset":"tone"}],"recommended_casts":{"ideation":{"count":3,"coordinator":true,"note":"diverse"}}}"#
            .write(to: fleet, atomically: true, encoding: .utf8)
        try #"{"personas":[{"id":"p","name":"Section","preamble":"s","backend":"api:openai","preset":"tone"},{"id":"bad","name":"Bad","preamble":"b","backend":"api:openai","preset":"missing"}]}"#
            .write(to: sectionDir.appendingPathComponent("agent_library.json"), atomically: true,
                   encoding: .utf8)
        try #"{"id":"tone","label":"Fleet","params":{"temperature":1.1}}"#
            .write(to: root.appendingPathComponent("presets/tone.json"), atomically: true,
                   encoding: .utf8)
        try #"{"id":"tone","label":"Section","params":{"temperature":0.2}}"#
            .write(to: sectionDir.appendingPathComponent("presets/tone.json"), atomically: true,
                   encoding: .utf8)
        let doc = AgentLibraryDocument.loadLayered(
            fleetURL: fleet, sectionURL: sectionDir.appendingPathComponent("agent_library.json"))
        XCTAssertEqual(doc.personas.first(where: { $0.id == "p" })?.name, "Section")
        XCTAssertEqual(doc.presets.first(where: { $0.id == "tone" })?.params["temperature"], 0.2)
        XCTAssertTrue(doc.warnings.contains { $0.contains("missing preset") })
        XCTAssertEqual(doc.recommendedHint(forPhase: "idea_discussion"),
                       "3 diverse + coordinator")
    }

    func testHintsAreInformationalAndRosterFieldsRoundTripAnySize() throws {
        var routing = ModelRouting()
        var route = PhaseRoute()
        route.castSize = 9
        route.composition = "local"
        routing.phases["verification"] = route
        let url = try directory().appendingPathComponent("model_routing.json")
        routing.save(to: url)
        let loaded = ModelRouting.load(from: url)
        XCTAssertEqual(loaded.phases["verification"]?.castSize, 9)
        XCTAssertEqual(loaded.phases["verification"]?.composition, "local")
    }

    func testBindingValidationRejectsFakeControlsAndPreservesUnknownKeys() throws {
        let url = try directory().appendingPathComponent("agent_library.json")
        try #"{"future_key":{"keep":true},"recommended_casts":{"verification":{"count":2,"note":"skeptics"}},"personas":[]}"#
            .write(to: url, atomically: true, encoding: .utf8)
        let invalid = AgentPersonaDef(id: "g", name: "Gem", preamble: "Check",
                                      backend: "gemini", model: "", defaultEffort: "high",
                                      preset: "")
        XCTAssertTrue(AgentLibraryDocument.save(personas: [invalid], to: url)?
            .contains("no effort control") ?? false)
        let valid = AgentPersonaDef(id: "a", name: "API", preamble: "Explore",
                                    backend: "api:openai", model: "gpt", defaultEffort: "",
                                    preset: "tone")
        XCTAssertNil(AgentLibraryDocument.save(personas: [valid], to: url))
        let raw = try JSONSerialization.jsonObject(with: Data(contentsOf: url)) as! [String: Any]
        XCTAssertNotNil(raw["future_key"])
        XCTAssertNotNil(raw["recommended_casts"])
        XCTAssertFalse(AgentLibraryDocument.supportsSampling("claude"))
        XCTAssertTrue(AgentLibraryDocument.supportsSampling("local:qwen3"))
    }
}
