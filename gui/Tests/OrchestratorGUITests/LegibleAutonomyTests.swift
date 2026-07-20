import XCTest
@testable import OrchestratorGUI

final class LegibleAutonomyTests: XCTestCase {
    func testPurposeMapParsesActualWorkflowShapeAndDropsMissingPurpose() {
        let data = Data(#"{"name":"brainstorm","phases":[{"key":"frame","purpose":"Why now"},{"key":"empty","purpose":"  "},{"key":"missing"}]}"#.utf8)
        XCTAssertEqual(WorkflowPurposeParser.parse(data), ["frame": "Why now"])
        XCTAssertEqual(WorkflowPurposeParser.parse(Data("not json".utf8)), [:])
    }

    func testRoutePreviewHitMissAndProjectOverlayPrecedence() {
        let fleet = Data(#"{"artifact_routes":{"idea":"research","gap":"documentation"}}"#.utf8)
        let project = Data(#"{"artifact_routes":{"idea":"planning"}}"#.utf8)
        XCTAssertEqual(RoutePreviewResolver.resolve(.init(
            section: "ideas", artifactType: "idea",
            fleetRouting: fleet, projectRouting: project)), "planning")
        XCTAssertEqual(RoutePreviewResolver.resolve(.init(
            section: "ideas", artifactType: "gap",
            fleetRouting: fleet, projectRouting: project)), "documentation")
        XCTAssertNil(RoutePreviewResolver.resolve(.init(
            section: "ideas", artifactType: "unknown",
            fleetRouting: fleet, projectRouting: project)))
    }

    func testAbsentOrMalformedRoutingHidesPreview() {
        XCTAssertNil(RoutePreviewResolver.resolve(.init(
            section: "ideas", artifactType: "idea",
            fleetRouting: nil, projectRouting: nil)))
        XCTAssertNil(RoutePreviewResolver.resolve(.init(
            section: "ideas", artifactType: "idea",
            fleetRouting: Data("{".utf8), projectRouting: nil)))
    }

    func testEngineRealModelRoutingOverlayHidesPreview() {
        // The ONLY routing.json the engine writes today (modelrouting.py):
        // phases/model overlays, zero artifact routes. The chip must stay
        // hidden on a real installation — and alias keys the 7.2 contract
        // does not define must not light it either (R2).
        let real = Data(#"{"schema_version":1,"enabled":true,"fallback":"claude","phases":{"tech_specs":{"claude":"claude-sonnet-5"}}}"#.utf8)
        XCTAssertNil(RoutePreviewResolver.resolve(.init(
            section: "ideas", artifactType: "idea",
            fleetRouting: real, projectRouting: nil)))
        for alias in ["default_routes", "defaults", "rules", "routes"] {
            let data = Data("{\"\(alias)\":{\"idea\":\"research\"}}".utf8)
            XCTAssertNil(RoutePreviewResolver.resolve(.init(
                section: "ideas", artifactType: "idea",
                fleetRouting: data, projectRouting: nil)),
                "alias key \(alias) must not light the preview")
        }
    }

    func testPreviewSourceIsInjectableForConductorUpgrade() {
        let source = RoutePreviewSource { context in
            context.section == "ideas" ? "actual-pending-target" : nil
        }
        XCTAssertEqual(source.target(.init(
            section: "ideas", artifactType: "idea",
            fleetRouting: nil, projectRouting: nil)), "actual-pending-target")
    }

    func testArtifactTypeHintRequiresOneUnambiguousArtifact() {
        XCTAssertEqual(ArtifactTypeHintParser.parse(finalOutput:
            "```artifact-json\n{\"type\":\"idea\"}\n```"), "idea")
        XCTAssertNil(ArtifactTypeHintParser.parse(finalOutput:
            "```artifact-json\n{\"artifacts\":[{\"type\":\"idea\"},{\"type\":\"gap\"}]}\n```"))
    }
}
