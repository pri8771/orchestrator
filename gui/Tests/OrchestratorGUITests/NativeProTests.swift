import XCTest
@testable import OrchestratorGUI

// Pure-logic coverage for the Native Pro additions (M0–M5).
final class NativeProTests: XCTestCase {

    // MARK: M0 — tokens

    func testEffortLevelParsesConfigValues() {
        XCTAssertEqual(EffortLevel(configValue: "low"), .low)
        XCTAssertEqual(EffortLevel(configValue: "Medium"), .medium)
        XCTAssertEqual(EffortLevel(configValue: "HIGH"), .high)
        XCTAssertNil(EffortLevel(configValue: ""))
        XCTAssertNil(EffortLevel(configValue: "xhigh"))   // grid maps only L/M/H
    }

    func testAgentIdentityLookupTolerantOfSlugsAndAliases() {
        XCTAssertEqual(DS.identity("codex").key, "codex")
        XCTAssertEqual(DS.identity("codex-a").key, "codex")       // roster slug
        XCTAssertEqual(DS.identity("local:qwen").key, "ollama")   // local alias
        XCTAssertEqual(DS.identity("CLAUDE").key, "claude")
        XCTAssertFalse(DS.identity("gemini").supportsEffort)
        XCTAssertTrue(DS.identity("claude").supportsEffort)
        // Unknown agents still get a usable identity, never a crash.
        XCTAssertEqual(DS.identity("mystery").displayName, "Mystery")
    }

    func testProjectTintIsStable() {
        let a = DS.projectTint("backtimer")
        let b = DS.projectTint("backtimer")
        XCTAssertEqual(a.light, b.light)
        XCTAssertEqual(a.dark, b.dark)
    }

    // MARK: M2 — intake sheet (M5: the factory intake is gone; the slug rule
    // and docs-placeholder prompt it defined are now pinned here directly so
    // existing project folders keep resolving)

    func testIntakeSlugRule() {
        // Lowercase, spaces→hyphens, strip everything not [a-z0-9-], collapse
        // runs of separators, empty/all-punctuation falls back to "new-chat"
        // (see SlugifyTests.swift — NewAppIntakeSheet.slugify now delegates
        // to the shared OrchestratorStore.slugify).
        XCTAssertEqual(NewAppIntakeSheet.slugify("Back Timer 2!"), "back-timer-2")
        XCTAssertEqual(NewAppIntakeSheet.slugify("Åpp  Ünïq"), "pp-n-q")
        XCTAssertEqual(NewAppIntakeSheet.slugify(""), "new-chat")
        XCTAssertEqual(NewAppIntakeSheet.slugify("BackTimer"), "backtimer")
        XCTAssertEqual(NewAppIntakeSheet.slugify("notes app"), "notes-app")
        XCTAssertEqual(NewAppIntakeSheet.slugify("a-b-c"), "a-b-c")
    }

    func testIntakeDocsPlaceholderPromptIsStable() {
        // The engine treats this exact sentence as "docs/ is the spec".
        XCTAssertEqual(NewAppIntakeSheet.docsPlaceholderPrompt,
                       "Build the app specified in docs/ — treat it as the source of truth.")
    }

    // MARK: M3 — routing matrix / grid logic

    private func makeMatrix(scope: RoutingScope = .project(name: "demo"),
                            draft: ModelRouting = ModelRouting(),
                            base: ModelRouting = ModelRouting()) -> RoutingMatrix {
        RoutingMatrix(scope: scope, draft: draft, base: base,
                      defaults: ["claude": "sonnet", "codex": "gpt-5.4-mini",
                                 "gemini": "gemini-3.5-flash", "ollama": "qwen2.5-coder:7b"],
                      defaultEfforts: ["codex": "medium"],
                      enabledAgents: ["claude": true, "codex": true,
                                      "gemini": true, "ollama": true],
                      installedLocal: ["qwen2.5-coder:7b"])
    }

    func testMatrixResolvesInheritedAndOverridden() {
        var fleet = ModelRouting()
        var fleetPhase = PhaseRoute()
        fleetPhase.claude = "opus"
        fleet.phases["spec"] = fleetPhase
        var m = makeMatrix(base: fleet)

        // Inherited from fleet routing, not overridden at project scope.
        let inherited = m.cell("spec", "claude")
        XCTAssertEqual(inherited.model, "opus")
        XCTAssertFalse(inherited.overriddenHere)
        XCTAssertEqual(inherited.inheritedFrom, "fleet routing")

        // No routing anywhere → config default.
        XCTAssertEqual(m.cell("build", "codex").model, "gpt-5.4-mini")
        XCTAssertEqual(m.cell("build", "codex").effort, .medium)

        // Project override wins and reads as overridden-here.
        m.setModel("haiku", phase: "spec", agent: "claude")
        let over = m.cell("spec", "claude")
        XCTAssertEqual(over.model, "haiku")
        XCTAssertTrue(over.overriddenHere)

        // Choosing the inherited value back clears the override.
        m.setModel("opus", phase: "spec", agent: "claude")
        XCTAssertFalse(m.cell("spec", "claude").overriddenHere)
    }

    func testMatrixEffortAndRevert() {
        var m = makeMatrix()
        m.setEffort(.high, phase: "build", agent: "claude")
        XCTAssertEqual(m.cell("build", "claude").effort, .high)
        XCTAssertTrue(m.cell("build", "claude").overriddenHere)
        // Gemini has no effort control — a set is refused.
        m.setEffort(.high, phase: "build", agent: "gemini")
        XCTAssertNil(m.cell("build", "gemini").effort)
        m.revertCell(phase: "build", agent: "claude")
        XCTAssertFalse(m.cell("build", "claude").overriddenHere)
        XCTAssertTrue(m.draft.phases.isEmpty, "an all-clear revert leaves no residue")
    }

    func testAgentsFilterSemanticsMatchEngine() {
        XCTAssertTrue(RoutingMatrix.filterIncludes("", agent: "claude"))
        XCTAssertTrue(RoutingMatrix.filterIncludes("cloud", agent: "codex"))
        XCTAssertFalse(RoutingMatrix.filterIncludes("cloud", agent: "ollama"))
        XCTAssertTrue(RoutingMatrix.filterIncludes("local", agent: "ollama"))
        XCTAssertFalse(RoutingMatrix.filterIncludes("local", agent: "gemini"))
        XCTAssertTrue(RoutingMatrix.filterIncludes("claude, codex", agent: "claude"))
        XCTAssertFalse(RoutingMatrix.filterIncludes("claude, codex", agent: "gemini"))
        XCTAssertTrue(RoutingMatrix.filterIncludes("local:qwen2.5", agent: "ollama"))
    }

    func testParticipationToggleEditsAgentsFilter() {
        var m = makeMatrix()
        m.setParticipates(false, phase: "spec", agent: "gemini")
        XCTAssertTrue(m.cell("spec", "gemini").off)
        XCTAssertFalse(m.cell("spec", "claude").off)
        XCTAssertEqual(m.draft.phases["spec"]?.agents, "codex,claude,ollama")
        // Turning everyone back on clears the filter entirely.
        m.setParticipates(true, phase: "spec", agent: "gemini")
        XCTAssertNil(m.draft.phases["spec"], "all-on filter must serialize to nothing")
    }

    func testPresetsAreStartingPointsNotModes() {
        let phases = [PhaseDef(key: "spec", folder: "spec", file: "spec.md", title: "Spec"),
                      PhaseDef(key: "review_gate", folder: "r", file: "r.md", title: "Review"),
                      PhaseDef(key: "build_coordination", folder: "b", file: "b.md",
                               title: "Build", rounds: 3, writes: true)]
        var m = makeMatrix()
        m.apply(preset: "max_quality", phases: phases)
        XCTAssertEqual(m.cell("spec", "claude").model, "opus")
        XCTAssertEqual(m.cell("spec", "claude").effort, .high)
        XCTAssertEqual(m.cell("build_coordination", "codex").effort, .high)

        m.apply(preset: "economy", phases: phases)
        XCTAssertEqual(m.cell("review_gate", "claude").model, "haiku")
        XCTAssertEqual(m.cell("review_gate", "claude").effort, .low)

        m.apply(preset: "balanced", phases: phases)
        XCTAssertTrue(m.draft.phases.isEmpty, "balanced = inherit everything")
    }

    func testInvalidLocalModelFlagged() {
        var m = makeMatrix()
        m.setModel("llama3.2:3b", phase: "spec", agent: "ollama")   // not installed
        XCTAssertTrue(m.cell("spec", "ollama").invalid)
        XCTAssertFalse(m.cell("spec", "claude").invalid)
    }

    func testPhaseRouteRoundTripsNewFields() throws {
        var r = ModelRouting()
        var p = PhaseRoute()
        p.claudeReasoning = "high"
        p.gemini = "gemini-2.5-pro"
        p.ollama = "qwen3-coder:30b"
        r.phases["spec"] = p
        let dir = FileManager.default.temporaryDirectory
            .appendingPathComponent("nativepro-tests-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: dir) }
        let url = dir.appendingPathComponent("model_routing.json")
        r.save(to: url)
        let loaded = ModelRouting.load(from: url)
        XCTAssertEqual(loaded.phases["spec"]?.claudeReasoning, "high")
        XCTAssertEqual(loaded.phases["spec"]?.gemini, "gemini-2.5-pro")
        XCTAssertEqual(loaded.phases["spec"]?.ollama, "qwen3-coder:30b")
    }

    func testParameterBillionsParser() {
        XCTAssertEqual(RoutingConsequences.parameterBillions("llama3.2:3b"), 3)
        XCTAssertEqual(RoutingConsequences.parameterBillions("qwen2.5-coder:7b"), 7)
        XCTAssertEqual(RoutingConsequences.parameterBillions("devstral:24B"), 24)
        XCTAssertNil(RoutingConsequences.parameterBillions("opus"))
        // The version number ("3.2") must not read as a size.
        XCTAssertEqual(RoutingConsequences.parameterBillions("llama3.2:70b"), 70)
    }

    func testConsequenceStripMathAndFloorWarning() {
        let phases = [PhaseDef(key: "spec", folder: "s", file: "s.md", title: "Spec", rounds: 2),
                      PhaseDef(key: "code_review", folder: "r", file: "r.md",
                               title: "Code Review", rounds: 1)]
        var m = makeMatrix()
        m.setModel("llama3.2:3b", phase: "code_review", agent: "ollama")
        m.setEffort(.high, phase: "spec", agent: "claude")
        let s = RoutingConsequences.summarize(
            matrix: m, phases: phases,
            enabledAgents: ["claude": true, "codex": true, "gemini": false, "ollama": true])
        // 3 participants (gemini disabled): spec 3×2 + review 3×1 = 9 max, 6 min.
        XCTAssertEqual(s.minCalls, 6)
        XCTAssertEqual(s.maxCalls, 9)
        XCTAssertEqual(s.highEffortPhases, 1)
        XCTAssertEqual(s.warnings.count, 1)
        XCTAssertEqual(s.warnings.first?.phaseKey, "code_review")
        XCTAssertTrue(s.warnings.first?.text.contains("3B") ?? false)
    }

    // MARK: M4 — engine events / run health

    private func event(_ json: String, id: Int = 0) -> EngineEvent? {
        EngineEvent.parse(line: json, id: id)
    }

    func testEngineEventParsesEngineShape() throws {
        let e = try XCTUnwrap(event("""
        {"ts": "2026-07-10T09:15:30", "kind": "turn_completed", "project": "backtimer", \
         "phase": "build_coordination", "round": 2, "agent": "codex", "ok": true, \
         "exit": 0, "model_requested": "gpt-5.4", "model_used": "gpt-5.4", \
         "output_len": 8123, "dur": 41.5}
        """))
        XCTAssertEqual(e.kind, "turn_completed")
        XCTAssertEqual(e.project, "backtimer")
        XCTAssertEqual(e.round, 2)
        XCTAssertEqual(e.ok, true)
        XCTAssertEqual(e.outputLen, 8123)
        XCTAssertEqual(e.dur, 41.5)
        XCTAssertNotEqual(e.ts, .distantPast, "naive isoformat timestamps must parse")
        // Corrupt/partial lines are skipped, never fatal.
        XCTAssertNil(event("{\"kind\":"))
        XCTAssertNil(event("{\"project\": \"x\"}"), "kind is required")
        let parsed = EngineEvent.parse(text: "garbage\n{\"ts\":\"2026-07-10T09:15:30\",\"kind\":\"run_started\"}\n")
        XCTAssertEqual(parsed.count, 1)
    }

    func testFallbackHeadlineAndAggregation() throws {
        let rescue = try XCTUnwrap(event("""
        {"ts": "2026-07-10T09:00:00", "kind": "agent_fallback", "agent": "claude", \
         "from_model": "opus", "to_model": "local:llama3.2:3b", "status": "rescued", \
         "reason": "usage cap"}
        """))
        XCTAssertTrue(rescue.isFallback)
        XCTAssertTrue(rescue.headline.contains("rescued by local:llama3.2:3b"))

        // Identical consecutive events collapse to one "× N" row.
        var stream: [EngineEvent] = []
        for i in 0..<133 {
            var e = rescue
            e.id = i
            stream.append(e)
        }
        var other = rescue
        other.id = 999
        other.toModel = "sonnet"
        stream.append(other)
        let collapsed = EventAggregator.collapse(stream)
        XCTAssertEqual(collapsed.count, 2)
        XCTAssertEqual(collapsed.first?.count, 133)
        XCTAssertEqual(collapsed.last?.count, 1)
    }

    func testAgentStatesPlannedActualDiff() {
        let lines = [
            #"{"ts":"2026-07-10T09:00:00","kind":"run_started","project":"demo"}"#,
            #"{"ts":"2026-07-10T09:00:01","kind":"turn_started","agent":"claude","model_requested":"opus"}"#,
            #"{"ts":"2026-07-10T09:00:05","kind":"agent_fallback","agent":"claude","from_model":"opus","to_model":"local:qwen2.5-coder:7b","status":"rescued"}"#,
            #"{"ts":"2026-07-10T09:00:09","kind":"turn_started","agent":"codex","model_requested":"gpt-5.4"}"#,
            #"{"ts":"2026-07-10T09:00:20","kind":"turn_completed","agent":"codex","ok":true,"model_used":"gpt-5.4","output_len":900,"dur":11}"#,
        ]
        let events = EngineEvent.parse(text: lines.joined(separator: "\n"))
        let states = RunHealthDeriver.agentStates(events: events, projectRunning: true)
        XCTAssertEqual(states.count, 2)
        let claude = states.first { $0.agent == "claude" }!
        XCTAssertEqual(claude.plannedModel, "opus")
        XCTAssertEqual(claude.actualModel, "local:qwen2.5-coder:7b")
        XCTAssertTrue(claude.degraded)
        XCTAssertEqual(claude.consecutiveFallbacks, 1)
        let codex = states.first { $0.agent == "codex" }!
        XCTAssertFalse(codex.degraded)
        XCTAssertEqual(codex.recentOutputLens, [900])
        // A dead run never shows a stale "Running".
        let idle = RunHealthDeriver.agentStates(events: events, projectRunning: false)
        for s in idle {
            if case .running = s.state { XCTFail("dead run shows running") }
        }
    }

    func testRunHistoryAndIntegritySegments() {
        let lines = [
            #"{"ts":"2026-07-10T08:00:00","kind":"run_started"}"#,
            #"{"ts":"2026-07-10T08:00:10","kind":"turn_completed","agent":"codex","phase":"spec","ok":true,"output_len":500}"#,
            #"{"ts":"2026-07-10T08:00:20","kind":"agent_fallback","agent":"claude","phase":"spec","from_model":"opus","to_model":"llama3.2:3b","status":"rescued"}"#,
            #"{"ts":"2026-07-10T08:00:30","kind":"turn_completed","agent":"claude","phase":"spec","ok":true,"output_len":18}"#,
            #"{"ts":"2026-07-10T08:01:00","kind":"run_finished","status":"done"}"#,
            #"{"ts":"2026-07-10T09:00:00","kind":"run_started"}"#,
        ]
        let events = EngineEvent.parse(text: lines.joined(separator: "\n"))
        let runs = RunHistoryDeriver.runs(events: events)
        XCTAssertEqual(runs.count, 2, "the open run counts too")
        let finished = runs.last!   // newest first → the finished one is last
        XCTAssertEqual(finished.status, "done")
        XCTAssertEqual(finished.fallbackCount, 1)
        XCTAssertNotNil(finished.duration)
        let segs = RunHistoryDeriver.integritySegments(run: finished, phase: "spec")
        XCTAssertEqual(segs.count, 2)
        XCTAssertFalse(segs[0].isRescue)
        XCTAssertEqual(segs[1].rescuedBy, "llama3.2:3b",
                       "the rescued turn carries the rescue model's name")
    }

    func testFleetSummaryRollsUpWorstState() {
        let degraded = EngineEvent.parse(text: [
            #"{"ts":"2026-07-10T09:00:00","kind":"run_started","project":"appa"}"#,
            #"{"ts":"2026-07-10T09:00:01","kind":"turn_started","agent":"claude","model_requested":"opus"}"#,
            #"{"ts":"2026-07-10T09:00:05","kind":"agent_fallback","agent":"claude","from_model":"opus","to_model":"sonnet","status":"rescued"}"#,
        ].joined(separator: "\n"))
        let now = EngineEvent.tsFormatter.date(from: "2026-07-10T09:01:00Z")
            .map { $0.addingTimeInterval(-Double(TimeZone.current.secondsFromGMT())) } ?? Date()
        let s = EventsScanner.summarize(eventsByProject: ["appa": degraded],
                                        runningProjects: ["appa"],
                                        failedProjects: [],
                                        now: now)
        XCTAssertEqual(s.fallbacksActive, ["appa"])
        XCTAssertEqual(s.worst, .fallback)
        XCTAssertEqual(s.fallbacks24h.count, 1)
        XCTAssertEqual(s.fallbacks24h.first?.project, "appa",
                       "scan/summarize must backfill the project for ledger jumps")
        // Red always outranks purple.
        let s2 = EventsScanner.summarize(eventsByProject: ["appa": degraded],
                                         runningProjects: ["appa"],
                                         failedProjects: ["appb"], now: now)
        XCTAssertEqual(s2.worst, .error)
        XCTAssertTrue(s2.headline.contains("failed"))
    }

    func testShortOutputFloors() {
        XCTAssertEqual(RunHealthDeriver.shortOutputFloor(phase: "build_verification"), 200)
        XCTAssertEqual(RunHealthDeriver.shortOutputFloor(phase: "final_review"), 200)
        XCTAssertEqual(RunHealthDeriver.shortOutputFloor(phase: "tech_specs"), 500)
        XCTAssertEqual(RunHealthDeriver.shortOutputFloor(phase: "build_coordination"), 40)
    }

    func testEffortConfigKeyParity() {
        // Claude gained --effort parity (engine e89e403): both cloud coders
        // expose the knob; gemini/local do not.
        XCTAssertEqual(effortConfigKey("codex"), "codex_reasoning")
        XCTAssertEqual(effortConfigKey("claude"), "claude_reasoning")
        XCTAssertNil(effortConfigKey("gemini"))
        XCTAssertNil(effortConfigKey("ollama"))
    }
}
