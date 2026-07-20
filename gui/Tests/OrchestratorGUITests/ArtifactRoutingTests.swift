import XCTest
@testable import OrchestratorGUI

final class ArtifactRoutingTests: XCTestCase {
    private func tempProject() throws -> URL {
        let url = FileManager.default.temporaryDirectory
            .appendingPathComponent("artifact-route-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: url.appendingPathComponent("artifacts"),
                                                withIntermediateDirectories: true)
        addTeardownBlock { try? FileManager.default.removeItem(at: url) }
        return url
    }

    private func meta(_ project: URL, id: String, status: String = "final",
                      version: Int = 1, supersedes: String? = nil,
                      lineage: [String]? = nil, ts: String = "2026-01-01") throws {
        let dir = project.appendingPathComponent("artifacts/\(id)")
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        var obj: [String: Any] = ["id": id, "type": "idea", "status": status,
                                  "version": version, "ts": ts]
        if let supersedes { obj["supersedes"] = supersedes }
        if let lineage { obj["lineage"] = lineage }
        let data = try JSONSerialization.data(withJSONObject: obj)
        try data.write(to: dir.appendingPathComponent("meta.json"))
    }

    func testLatestRoutableRequiresFinalAndSkipsStaleAncestor() throws {
        let project = try tempProject()
        try meta(project, id: "idea", version: 1, lineage: ["idea"])
        try meta(project, id: "idea-2", version: 2, supersedes: "idea",
                 lineage: ["idea", "idea-2"], ts: "2026-02-01")
        XCTAssertEqual(ArtifactRouteIndex.latestRoutable(projectDir: project)?.id, "idea-2")
    }

    func testPendingConvergedBranchedAndCorruptAreNotRoutable() throws {
        let project = try tempProject()
        try meta(project, id: "pending", status: "pending_review")
        try meta(project, id: "done", status: "converged")
        let corrupt = project.appendingPathComponent("artifacts/corrupt")
        try FileManager.default.createDirectory(at: corrupt, withIntermediateDirectories: true)
        try Data("{".utf8).write(to: corrupt.appendingPathComponent("meta.json"))
        XCTAssertNil(ArtifactRouteIndex.latestRoutable(projectDir: project))

        try meta(project, id: "root", lineage: ["root"])
        try meta(project, id: "branch-a", supersedes: "root", lineage: ["root", "branch-a"])
        try meta(project, id: "branch-b", supersedes: "root", lineage: ["root", "branch-b"])
        XCTAssertNil(ArtifactRouteIndex.latestRoutable(projectDir: project))
    }

    func testRouteSummaryPreservesEngineRefusalReason() {
        XCTAssertEqual(ArtifactRouteCommand.summary(
            "notice\n--route refused: artifact is converged\n", fallback: "failed"),
                       "--route refused: artifact is converged")
    }

    func testRouteCommandUsesTheEngineRoutePushCLIContract() {
        XCTAssertEqual(ArtifactRouteCommand.arguments(
            engine: "/engine/orchestrator.py", root: "/workspace",
            artifactID: "idea-2", sourceSession: "demo/ideas/chat",
            targetSession: "demo/research/idea-2"),
            ["/engine/orchestrator.py", "--root", "/workspace",
             "--route-artifact", "idea-2", "--route-from", "demo/ideas/chat",
             "--route-to", "demo/research/idea-2"])
    }
}
