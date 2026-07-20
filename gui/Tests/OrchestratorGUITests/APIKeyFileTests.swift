import XCTest
@testable import OrchestratorGUI

/// Settings › API Keys: writes must land at the EXACT path
/// orchestrator.py's _api_key/_gemini_api_key read (~/.orchestrator/
/// <provider>_api_key), be atomic + 0600, and never re-expose a saved
/// secret's content — only whether one exists.
final class APIKeyFileTests: XCTestCase {
    private func tempBase() throws -> URL {
        let url = FileManager.default.temporaryDirectory
            .appendingPathComponent("api-key-test-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: url, withIntermediateDirectories: true)
        return url
    }

    func testSaveWritesUnderDotOrchestratorMatchingTheEnginePath() throws {
        let base = try tempBase()
        XCTAssertTrue(APIKeyFile.save("gemini_api_key", key: "AIza-fake-test-key", base: base))
        let expected = base.appendingPathComponent(".orchestrator/gemini_api_key")
        XCTAssertEqual(APIKeyFile.url(for: "gemini_api_key", base: base).path, expected.path)
        let saved = try String(contentsOf: expected, encoding: .utf8)
        XCTAssertEqual(saved, "AIza-fake-test-key")
    }

    func testSaveSetsOwnerOnlyPermissions() throws {
        let base = try tempBase()
        XCTAssertTrue(APIKeyFile.save("gemini_api_key", key: "k", base: base))
        let attrs = try FileManager.default.attributesOfItem(
            atPath: APIKeyFile.url(for: "gemini_api_key", base: base).path)
        let perms = (attrs[.posixPermissions] as? NSNumber)?.intValue
        XCTAssertEqual(perms, 0o600)
    }

    func testWhitespaceOnlyKeyIsRejected() throws {
        let base = try tempBase()
        XCTAssertFalse(APIKeyFile.save("gemini_api_key", key: "   \n  ", base: base))
        XCTAssertFalse(APIKeyFile.isSet("gemini_api_key", base: base))
    }

    func testKeyIsTrimmedBeforeWriting() throws {
        let base = try tempBase()
        XCTAssertTrue(APIKeyFile.save("gemini_api_key", key: "  secret-value  \n", base: base))
        let saved = try String(
            contentsOf: APIKeyFile.url(for: "gemini_api_key", base: base), encoding: .utf8)
        XCTAssertEqual(saved, "secret-value")
    }

    func testIsSetReflectsPresenceNotContent() throws {
        let base = try tempBase()
        XCTAssertFalse(APIKeyFile.isSet("gemini_api_key", base: base))
        APIKeyFile.save("gemini_api_key", key: "abc", base: base)
        XCTAssertTrue(APIKeyFile.isSet("gemini_api_key", base: base))
    }

    func testEmptyFileOnDiskCountsAsNotSet() throws {
        let base = try tempBase()
        let dir = base.appendingPathComponent(".orchestrator")
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        try "".write(to: dir.appendingPathComponent("gemini_api_key"),
                     atomically: true, encoding: .utf8)
        XCTAssertFalse(APIKeyFile.isSet("gemini_api_key", base: base))
    }

    func testClearRemovesTheFile() throws {
        let base = try tempBase()
        APIKeyFile.save("gemini_api_key", key: "abc", base: base)
        XCTAssertTrue(APIKeyFile.isSet("gemini_api_key", base: base))
        XCTAssertTrue(APIKeyFile.clear("gemini_api_key", base: base))
        XCTAssertFalse(APIKeyFile.isSet("gemini_api_key", base: base))
    }

    func testClearOnMissingFileIsAHarmlessFalse() throws {
        let base = try tempBase()
        XCTAssertFalse(APIKeyFile.clear("gemini_api_key", base: base))
    }

    func testOverwriteReplacesRatherThanAppends() throws {
        let base = try tempBase()
        APIKeyFile.save("gemini_api_key", key: "first-key", base: base)
        APIKeyFile.save("gemini_api_key", key: "second-key", base: base)
        let saved = try String(
            contentsOf: APIKeyFile.url(for: "gemini_api_key", base: base), encoding: .utf8)
        XCTAssertEqual(saved, "second-key")
    }

    func testProvidersMapToTheExactEngineFilenames() {
        // pins the filenames against orchestrator.py's _API_KEY_FILES /
        // _gemini_api_key contract — a rename here without updating the
        // engine (or vice versa) breaks the connection silently otherwise.
        let byID = Dictionary(uniqueKeysWithValues: apiKeyProviders.map { ($0.id, $0.filename) })
        XCTAssertEqual(byID["gemini"], "gemini_api_key")
        XCTAssertEqual(byID["google"], "google_api_key")
        XCTAssertEqual(byID["anthropic"], "anthropic_api_key")
        XCTAssertEqual(byID["openai"], "openai_api_key")
    }

    func testEachProviderKeyIsIndependent() throws {
        let base = try tempBase()
        APIKeyFile.save("gemini_api_key", key: "gem", base: base)
        XCTAssertFalse(APIKeyFile.isSet("openai_api_key", base: base))
        APIKeyFile.save("openai_api_key", key: "oai", base: base)
        XCTAssertTrue(APIKeyFile.isSet("gemini_api_key", base: base))
        XCTAssertTrue(APIKeyFile.isSet("openai_api_key", base: base))
    }
}
