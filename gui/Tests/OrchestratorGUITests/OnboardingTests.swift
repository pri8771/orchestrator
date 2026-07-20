import XCTest
@testable import OrchestratorGUI

final class OnboardingTests: XCTestCase {
    private var suiteName: String!
    private var defaults: UserDefaults!

    override func setUp() {
        super.setUp()
        suiteName = "OnboardingTests.\(UUID().uuidString)"
        defaults = UserDefaults(suiteName: suiteName)!
        defaults.removePersistentDomain(forName: suiteName)
    }

    override func tearDown() {
        defaults.removePersistentDomain(forName: suiteName)
        defaults = nil
        suiteName = nil
        super.tearDown()
    }

    func testFirstLaunchDetectionAndExistingUserBypass() {
        XCTAssertTrue(OnboardingPersistence.shouldPresent(
            progress: .notStarted, projectCount: 0))
        XCTAssertFalse(OnboardingPersistence.shouldPresent(
            progress: .notStarted, projectCount: 1))
        XCTAssertFalse(OnboardingPersistence.shouldPresent(
            progress: .complete, projectCount: 0))
    }

    func testProgressAndSkipPersistenceSurviveRelaunch() {
        OnboardingPersistence.save(.inProgress(step: 3), to: defaults)
        XCTAssertEqual(OnboardingProgress.load(from: defaults),
                       .inProgress(step: 3))
        OnboardingPersistence.save(.complete, to: defaults)
        XCTAssertEqual(OnboardingProgress.load(from: defaults), .complete)
        XCTAssertNil(defaults.object(forKey: OnboardingPersistence.stepKey))
    }

    func testDisclosureStartsWithIdeasResearchAndPersistsReveals() {
        XCTAssertEqual(OnboardingPersistence.visibleSections(from: defaults),
                       ["ideas", "research"])
        OnboardingPersistence.saveVisibleSections(
            ["ideas", "research", "qa"], to: defaults)
        XCTAssertEqual(OnboardingPersistence.visibleSections(from: defaults),
                       ["ideas", "research", "qa"])
        let metas = [SectionMeta(id: "ideas", title: "Ideas"),
                     SectionMeta(id: "qa", title: "QA")]
        XCTAssertEqual(SectionDisclosureLogic.visible(
            metas, revealed: ["ideas"]).map(\.id), ["ideas"])
    }

    func testArtifactRouteTargetsRevealTheirSection() {
        XCTAssertEqual(SectionDisclosureLogic.routedSection(
            targetSession: "gloam/qa/finding", target: "qa"), "qa")
        XCTAssertEqual(SectionDisclosureLogic.routedSection(
            targetSession: "", target: "gloam/research/brief"), "research")
        XCTAssertNil(SectionDisclosureLogic.routedSection(
            targetSession: "", target: ""))
    }

    func testChecklistNeverClaimsAvailableWithoutSuccessfulProbe() {
        let rows = OnboardingProbeLogic.rows(
            cliVersions: [:], cliAvailable: ["codex": true, "ollama": true],
            localModels: nil, cliProbeInFlight: false,
            doctorProbeInFlight: false, doctorProbeCompleted: true,
            capabilities: nil)
        XCTAssertEqual(rows.first(where: { $0.id == "codex" })?.state,
                       .broken("CLI found, but its version probe failed"))
        XCTAssertEqual(rows.first(where: { $0.id == "claude" })?.state, .missing)
        XCTAssertEqual(rows.first(where: { $0.id == "ollama" })?.state,
                       .broken("Ollama is installed, but the server is not running"))
        XCTAssertFalse(OnboardingProbeLogic.hasAvailableBackend(rows))
    }

    func testChecklistSuccessAndCapabilitiesComeFromProbePayloads() {
        let caps = AgentCapabilitiesInfo(
            agents: ["codex": AgentCapability(
                streams: false, tokenUsage: false, effortControl: true,
                sessionResume: "build_only")],
            dynamicPrefixes: ["local:": AgentCapability(
                streams: true, tokenUsage: true, effortControl: true,
                sessionResume: "never")])
        let local = LocalModelsInfo(
            serverRunning: true, selected: "tiny", selectedInstalled: true,
            registry: [LocalModelEntry(id: "tiny", installed: true)])
        let rows = OnboardingProbeLogic.rows(
            cliVersions: ["codex": "codex 1.2"],
            cliAvailable: ["codex": true, "ollama": true],
            localModels: local, cliProbeInFlight: false,
            doctorProbeInFlight: false, doctorProbeCompleted: true,
            capabilities: caps)
        XCTAssertEqual(rows.first(where: { $0.id == "codex" })?.state,
                       .available("codex 1.2"))
        XCTAssertEqual(rows.first(where: { $0.id == "codex" })?.resumes, true)
        XCTAssertEqual(rows.first(where: { $0.id == "ollama" })?.streams, true)
        XCTAssertTrue(OnboardingProbeLogic.hasAvailableBackend(rows))
    }

    func testProbingIsExplicitAndResolvedFailureHasFixIt() {
        let probing = OnboardingProbeLogic.rows(
            cliVersions: [:], cliAvailable: [:], localModels: nil,
            cliProbeInFlight: true, doctorProbeInFlight: true,
            doctorProbeCompleted: false, capabilities: nil)
        XCTAssertTrue(probing.allSatisfy { $0.state == .probing })
        let resolved = OnboardingProbeLogic.rows(
            cliVersions: [:], cliAvailable: [:], localModels: nil,
            cliProbeInFlight: false, doctorProbeInFlight: false,
            doctorProbeCompleted: true, capabilities: nil)
        XCTAssertTrue(resolved.allSatisfy { $0.state == .missing })
        XCTAssertTrue(resolved.allSatisfy { !$0.fix.isEmpty })
    }

    func testGuideOnlyAdvancesFromRealEngineEvents() {
        XCTAssertNil(OnboardingGuideLogic.progressed(
            step: 2, eventKinds: [], routedSection: nil))
        XCTAssertEqual(OnboardingGuideLogic.progressed(
            step: 2, eventKinds: ["turn_completed"], routedSection: nil),
                       .inProgress(step: 3))
        XCTAssertEqual(OnboardingGuideLogic.progressed(
            step: 3, eventKinds: ["step_in_joined"], routedSection: nil),
                       .inProgress(step: 4))
        XCTAssertEqual(OnboardingGuideLogic.progressed(
            step: 4, eventKinds: ["artifact_routed"], routedSection: "research"),
                       .complete)
    }

    func testArtifactRoutedEventParsesDisclosureFields() {
        let event = EngineEvent.parse(
            line: #"{"kind":"artifact_routed","target":"research","target_session":"gloam/research/brief"}"#,
            id: 1)
        XCTAssertEqual(event?.target, "research")
        XCTAssertEqual(event?.targetSession, "gloam/research/brief")
    }
}
