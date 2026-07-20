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
                      lineage: [String]? = nil, ts: String = "2026-01-01",
                      type: String = "idea", phase: String = "report",
                      parents: [String]? = nil) throws {
        let dir = project.appendingPathComponent("artifacts/\(id)")
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        var obj: [String: Any] = ["id": id, "type": type, "status": status,
                                  "version": version, "ts": ts,
                                  "source": ["phase": phase, "session": "chat"]]
        if let supersedes { obj["supersedes"] = supersedes }
        if let lineage { obj["lineage"] = lineage }
        if let parents { obj["fields"] = ["parents": parents] }
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

    func testArtifactIndexParsesMetaAndSurfacesCorruption() throws {
        let project = try tempProject()
        try meta(project, id: "brief", status: "pending_review", version: 3,
                 lineage: ["brief", "brief-2", "brief"], type: "research_brief")
        // A directory at body.md would make a body read fail; the refresh index
        // is deliberately meta-only and must still surface the card.
        try FileManager.default.createDirectory(
            at: project.appendingPathComponent("artifacts/brief/body.md"),
            withIntermediateDirectories: false)
        let bad = project.appendingPathComponent("artifacts/bad")
        try FileManager.default.createDirectory(at: bad, withIntermediateDirectories: true)
        try Data("{".utf8).write(to: bad.appendingPathComponent("meta.json"))

        let summaries = ArtifactIndex.scanProject(
            project, policies: ["research_brief": "requires_review_gate"])
        let brief = try XCTUnwrap(summaries.first { $0.id == "brief" })
        XCTAssertEqual(brief.type, "research_brief")
        XCTAssertEqual(brief.version, 3)
        XCTAssertEqual(brief.sourcePhase, "report")
        XCTAssertTrue(brief.canHumanFinalize)
        XCTAssertNotNil(summaries.first { $0.id == "bad" }?.unreadableReason)
        XCTAssertEqual(ArtifactIndex.scanProject(
            project.appendingPathComponent("missing"), policies: [:]), [])
    }

    func testArtifactIndexStaleMatchesAuthoritativeEngineSuccessors() throws {
        let project = try tempProject()
        try meta(project, id: "linear")
        try meta(project, id: "linear-2", version: 2, supersedes: "linear")
        try meta(project, id: "branched")
        try meta(project, id: "branch-a", version: 2, supersedes: "branched")
        try meta(project, id: "branch-b", version: 2, supersedes: "branched")
        try meta(project, id: "live-parent")
        try meta(project, id: "same-content", status: "converged", version: 2,
                 supersedes: "live-parent")
        try meta(project, id: "reconciled-parent")
        try meta(project, id: "merge", version: 2,
                 lineage: ["reconciled-parent", "merge"], type: "reconcile")
        let mergeURL = project.appendingPathComponent("artifacts/merge/meta.json")
        var merge = try XCTUnwrap(try JSONSerialization.jsonObject(
            with: Data(contentsOf: mergeURL)) as? [String: Any])
        merge["fields"] = ["parents": ["reconciled-parent"]]
        try JSONSerialization.data(withJSONObject: merge).write(to: mergeURL)

        let byID = Dictionary(uniqueKeysWithValues: ArtifactIndex.scanProject(
            project, policies: [:]).map { ($0.id, $0) })
        XCTAssertTrue(try XCTUnwrap(byID["linear"]).stale)
        XCTAssertFalse(try XCTUnwrap(byID["branched"]).stale)
        XCTAssertFalse(try XCTUnwrap(byID["live-parent"]).stale)
        XCTAssertTrue(try XCTUnwrap(byID["reconciled-parent"]).stale)
    }

    func testCardStateMappingIsExplicitAcrossStatusStaleAndActions() {
        func summary(status: String, stale: Bool = false,
                     unreadable: String? = nil) -> ArtifactSummary {
            ArtifactSummary(id: "a", type: "idea", version: 1, status: status,
                            stale: stale, lineage: ["a"], sourcePhase: "p",
                            sourceSession: "s", finalizationPolicy: "requires_human",
                            unreadableReason: unreadable)
        }
        XCTAssertEqual(ArtifactCardState.resolve(
            summary: summary(status: "pending_review"), route: nil), .pendingReview)
        XCTAssertEqual(ArtifactCardState.resolve(
            summary: summary(status: "final"), route: nil), .final)
        XCTAssertEqual(ArtifactCardState.resolve(
            summary: summary(status: "converged"), route: nil), .converged)
        XCTAssertEqual(ArtifactCardState.resolve(
            summary: summary(status: "final", stale: true), route: nil), .stale)
        XCTAssertEqual(ArtifactCardState.resolve(
            summary: summary(status: "final", unreadable: "bad"), route: nil), .unreadable)
        XCTAssertEqual(ArtifactCardState.resolve(
            summary: summary(status: "final"), route: .routing(target: "research")),
                       .routing(target: "research"))
        XCTAssertEqual(ArtifactCardState.resolve(
            summary: summary(status: "final"), route: .routed(target: "research")),
                       .routed(target: "research"))
        XCTAssertEqual(ArtifactCardState.resolve(
            summary: summary(status: "final"), route: .refused(reason: "branched")),
                       .refused(reason: "branched"))
    }

    func testFinalizeEligibilityRequiresPendingStateReadableMetaAndRealPolicy() {
        func summary(status: String = "pending_review", policy: String = "requires_human",
                     unreadable: String? = nil) -> ArtifactSummary {
            ArtifactSummary(id: "a", type: "spec_bundle", version: 1,
                            status: status, stale: false, lineage: ["a"],
                            sourcePhase: "report", sourceSession: "demo/planning/chat",
                            finalizationPolicy: policy, unreadableReason: unreadable)
        }
        XCTAssertTrue(summary().canHumanFinalize)
        XCTAssertTrue(summary(status: "draft", policy: "requires_review_gate")
            .canHumanFinalize)
        XCTAssertFalse(summary(status: "final").canHumanFinalize)
        XCTAssertFalse(summary(policy: "unknown").canHumanFinalize)
        XCTAssertFalse(summary(unreadable: "bad meta").canHumanFinalize)
    }

    func testCardGlyphsAreTypedAndUnknownTypesUseHonestFallback() {
        XCTAssertEqual(ArtifactCard.glyph(for: "idea"), "lightbulb")
        XCTAssertEqual(ArtifactCard.glyph(for: "research_brief"),
                       "doc.text.magnifyingglass")
        XCTAssertEqual(ArtifactCard.glyph(for: "not-yet-known"), "doc.richtext")
        XCTAssertEqual(ArtifactCard.lineageLabel(["idea", "idea-2"]),
                       "idea → idea-2")
    }

    func testDropPayloadRoundTripsAndRejectsUnrelatedText() throws {
        let payload = ArtifactDragPayload(artifactID: "idea-2", type: "idea",
                                          version: 2, sourceSession: "demo/ideas/chat")
        XCTAssertEqual(ArtifactDragPayload.decode(try XCTUnwrap(payload.encode())), payload)
        XCTAssertNil(ArtifactDragPayload.decode("idea-2"))
    }

    func testFinalizeCommandUsesHumanEngineContract() {
        XCTAssertEqual(ArtifactFinalizeCommand.arguments(
            engine: "/engine/orchestrator.py", root: "/workspace",
            artifactID: "spec", sourceSession: "demo/planning/chat"),
            ["/engine/orchestrator.py", "--root", "/workspace",
             "--finalize-artifact", "spec", "--finalize-in", "demo/planning/chat",
             "--by", "human:gui", "--by-human"])
    }

    func testFinalizeCommandFlipsRealMetaAndRereadSeesFinal() throws {
        let project = try tempProject()
        let id = "manual-spec"
        try meta(project, id: id, status: "pending_review", type: "spec_bundle")

        var repo = URL(fileURLWithPath: #filePath)
        while repo.path != "/" && !FileManager.default.fileExists(
                atPath: repo.appendingPathComponent("orchestrator.py").path) {
            repo.deleteLastPathComponent()
        }
        let engine = repo.appendingPathComponent("orchestrator.py")
        let python = "/usr/bin/python3"
        guard FileManager.default.fileExists(atPath: engine.path),
              FileManager.default.isExecutableFile(atPath: python) else {
            throw XCTSkip("repository engine or system Python is unavailable")
        }

        let result = ArtifactFinalizeCommand.run(
            python: python, engine: engine.path,
            root: project.deletingLastPathComponent().path,
            artifactID: id,
            sourceSession: "\(project.lastPathComponent)/planning/chat")
        XCTAssertEqual(result.code, 0, result.output)

        let data = try Data(contentsOf: project.appendingPathComponent(
            "artifacts/\(id)/meta.json"))
        let reread = try XCTUnwrap(
            try JSONSerialization.jsonObject(with: data) as? [String: Any])
        XCTAssertEqual(reread["status"] as? String, "final")
        let history = try XCTUnwrap(reread["status_history"] as? [[String: Any]])
        XCTAssertEqual(history.last?["by"] as? String, "human:gui")
    }

    func testArtifactPublishedEventCarriesCardIdentity() throws {
        let event = try XCTUnwrap(EngineEvent.parse(line:
            #"{"kind":"artifact_published","artifact_id":"idea","type":"idea","version":2,"path":"artifacts/idea","phase":"report"}"#,
            id: 0))
        XCTAssertEqual(event.artifactID, "idea")
        XCTAssertEqual(event.artifactType, "idea")
        XCTAssertEqual(event.artifactVersion, 2)
        XCTAssertEqual(event.artifactPath, "artifacts/idea")
    }

    func testReconcileRetiresBranchHeadsAndRestoresRoutability() throws {
        // Engine reality: a reconcile lists merged heads in fields.parents
        // (supersedes stays null). Before the fix those heads survived as
        // rivals and the routable artifact vanished forever post-reconcile.
        let project = try tempProject()
        try meta(project, id: "root", status: "superseded",
                 lineage: ["root"], ts: "2026-01-01")
        try meta(project, id: "branchA", supersedes: "root",
                 lineage: ["root", "branchA"], ts: "2026-01-02")
        try meta(project, id: "branchB", supersedes: "root",
                 lineage: ["root", "branchB"], ts: "2026-01-02")
        // Two rival heads: nothing routable yet (ambiguous lineage).
        XCTAssertNil(ArtifactRouteIndex.latestRoutable(projectDir: project))
        try meta(project, id: "merged",
                 lineage: ["root", "merged"], ts: "2026-01-03",
                 type: "reconcile", parents: ["branchA", "branchB"])
        let routable = ArtifactRouteIndex.latestRoutable(projectDir: project)
        XCTAssertEqual(routable?.id, "merged",
                       "post-reconcile lineage must be routable again")
    }
}
