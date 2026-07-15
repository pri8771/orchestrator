import SwiftUI
import AppKit

// MARK: - Run health surfaces (DESIGN-NATIVE-PRO.md §4.2/§4.4/§4.6/§6, M4)
//
// Everything here renders the store's published events.jsonl state
// (eventsByProject + fleetHealth) with the one §6 grammar: accent = running ·
// green = success · amber = warning/stall · purple = fallback, exclusively ·
// red = error · gray = idle. Always symbol + word, never color alone.

// MARK: - Zone A: Phase Timeline (64pt pinned capsule chain)

struct PhaseTimelineView: View {
    @EnvironmentObject var store: OrchestratorStore
    let project: Project
    /// Compact mode (DESIGN-REFRESH.md tranche 2): the Overview's Active Runs
    /// cards embed the same capsule chain at reduced height, without the
    /// round sublabel. Same data, lower altitude.
    var compact = false

    private var phases: [PhaseDef] { store.phases(for: project) }

    var body: some View {
        // Read the two routing files ONCE per body evaluation, not once per
        // phase. store.readProjectRouting/readModelRouting are TTL-cached,
        // but this ForEach previously called them up to 2× per phase (9
        // phases → up to 18 store hits) on every render — and a `body` here
        // can re-run many times a second while a `.repeatForever` breathing
        // dot animates elsewhere on the same screen. Loading both once and
        // passing them down keeps each render to exactly 2 cache lookups.
        let projectRouting = store.readProjectRouting(project)
        let fleetRouting = store.readModelRouting()
        ScrollViewReader { proxy in
            ScrollView(.horizontal, showsIndicators: false) {
                HStack(spacing: DS.space.xxs) {
                    ForEach(phases) { phase in
                        segment(phase, projectRouting: projectRouting, fleetRouting: fleetRouting)
                            .id(phase.key)
                    }
                }
                .padding(.horizontal, compact ? 0 : DS.space.s)
                .padding(.vertical, compact ? 2 : DS.space.xs)
            }
            .onAppear {
                if let cur = project.currentPhase { proxy.scrollTo(cur, anchor: .center) }
            }
            .onChange(of: project.currentPhase) { _, cur in
                if let cur { withAnimation { proxy.scrollTo(cur, anchor: .center) } }
            }
        }
        .frame(height: compact ? 28 : 64)
        // §2.3 documented opt-out: this is a fixed-height (64pt / 28pt
        // compact) horizontal capsule chain — geometry is load-bearing, so it
        // stays pinned to the system default text size. Compensated by the
        // per-segment accessibilityLabel below.
        .dynamicTypeSize(.large)
    }

    @ViewBuilder
    private func segment(_ phase: PhaseDef, projectRouting: ModelRouting, fleetRouting: ModelRouting) -> some View {
        let status = project.phaseStatus(phase.key)
        let isCurrent = project.currentPhase == phase.key && project.status == .inProgress
        VStack(spacing: 3) {
            HStack(spacing: DS.space.xxs) {
                Image(systemName: status.symbol)
                    .font(DS.font.caption)
                Text(phase.title)
                    .font(DS.font.caption)
                    .lineLimit(1)
                phaseAgents(phase, projectRouting: projectRouting, fleetRouting: fleetRouting)
            }
            .padding(.horizontal, DS.space.xs)
            .padding(.vertical, 4)
            .foregroundStyle(fill(status, isCurrent: isCurrent))
            .background(Capsule().fill(background(status, isCurrent: isCurrent)))
            .overlay(Capsule().stroke(
                status == .pending ? DS.hairline : Color.clear, lineWidth: 1))
            if isCurrent && !compact {
                Text("round \(project.currentRound)\(phase.rounds > 0 ? " of \(phase.rounds)" : "")")
                    .font(DS.font.caption).monospacedDigit()
                    .foregroundStyle(.secondary)
            }
        }
        .accessibilityElement(children: .ignore)
        .accessibilityLabel("\(phase.title): \(a11yStatus(status, isCurrent: isCurrent))")
    }

    // Per-phase agent avatars (16pt): routing readable at a glance.
    @ViewBuilder
    private func phaseAgents(_ phase: PhaseDef, projectRouting: ModelRouting, fleetRouting: ModelRouting) -> some View {
        let filter = routingFilter(phase.key, projectRouting: projectRouting, fleetRouting: fleetRouting)
        HStack(spacing: 2) {
            ForEach(RoutingMatrix.agents, id: \.self) { agent in
                if (store.enabledAgents[agent] ?? false),
                   RoutingMatrix.filterIncludes(filter, agent: agent) {
                    AgentAvatar(agent: agent, size: 16)
                }
            }
        }
    }

    private func routingFilter(_ phaseKey: String, projectRouting: ModelRouting, fleetRouting: ModelRouting) -> String {
        let proj = projectRouting.phases[phaseKey]?.agents ?? ""
        if !proj.isEmpty { return proj }
        return fleetRouting.phases[phaseKey]?.agents ?? ""
    }

    private func fill(_ s: PhaseStatus, isCurrent: Bool) -> Color {
        switch s {
        case .done, .active: return DS.accent.color
        case .aborted: return DS.status.error.color
        case .pending: return DS.textSecondary
        }
    }

    private func background(_ s: PhaseStatus, isCurrent: Bool) -> Color {
        switch s {
        case .done: return DS.accent.fill
        case .active: return DS.accent.fill
        case .aborted: return DS.status.error.fill
        case .pending: return .clear
        }
    }

    private func a11yStatus(_ s: PhaseStatus, isCurrent: Bool) -> String {
        if isCurrent { return "in progress, round \(project.currentRound)" }
        switch s {
        case .done: return "completed"
        case .active: return "current"
        case .aborted: return "stopped here"
        case .pending: return "not started"
        }
    }
}

// MARK: - Zone B: Agent Board (56pt rows)

struct AgentBoardView: View {
    @EnvironmentObject var store: OrchestratorStore
    let project: Project
    let events: [EngineEvent]

    private var states: [AgentRuntimeState] {
        RunHealthDeriver.agentStates(events: events, projectRunning: project.running)
    }

    var body: some View {
        let list = states
        if list.isEmpty {
            Text(project.running
                 ? "Waiting for the first turn event…"
                 : "No agent activity recorded yet — events appear once a run emits events.jsonl.")
                .font(DS.font.caption).foregroundStyle(.tertiary)
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(DS.space.s)
        } else {
            VStack(spacing: 0) {
                ForEach(list) { s in
                    AgentBoardRow(project: project, state: s)
                    if s.id != list.last?.id { Divider() }
                }
            }
            .background(RoundedRectangle(cornerRadius: DS.radius.card, style: .continuous)
                .fill(DS.cardBg))
            .overlay(RoundedRectangle(cornerRadius: DS.radius.card, style: .continuous)
                .stroke(DS.hairline, lineWidth: 1))
        }
    }
}

private struct AgentBoardRow: View {
    @EnvironmentObject var store: OrchestratorStore
    let project: Project
    let state: AgentRuntimeState

    private var identity: DS.AgentIdentity { DS.identity(state.agent) }

    var body: some View {
        HStack(spacing: DS.space.s) {
            AgentAvatar(identity: identity, size: 24)
            VStack(alignment: .leading, spacing: 2) {
                HStack(spacing: DS.space.xxs) {
                    Text(identity.displayName).font(DS.font.callout)
                    if !state.plannedModel.isEmpty {
                        Text("· \(state.plannedModel)")
                            .font(DS.font.caption).foregroundStyle(.secondary)
                            .lineLimit(1)
                    }
                    if identity.supportsEffort,
                       let e = EffortLevel(configValue: store.agentEfforts[state.agent] ?? "") {
                        EffortGauge(level: e, tint: identity.tint)
                    }
                }
                // Planned→actual route diff — the canonical rendering of
                // degradation (graft: Conductor). Rendered only when they differ.
                if state.degraded && !state.actualModel.isEmpty {
                    Text("\(state.plannedModel.isEmpty ? "planned" : state.plannedModel) → \(state.actualModel)")
                        .font(DS.font.monoInline)
                        .foregroundStyle(DS.status.fallback.color)
                        .lineLimit(1)
                        .accessibilityLabel("Running on fallback \(state.actualModel) instead of \(state.plannedModel)")
                }
            }
            Spacer()
            if state.recentOutputLens.count > 1 {
                VStack(alignment: .trailing, spacing: 1) {
                    Sparkline(values: state.recentOutputLens, tint: identity.tint.color)
                        .frame(width: 56, height: 14)
                    if let last = state.recentOutputLens.last,
                       Int(last) < RunHealthDeriver.shortOutputFloor(phase: project.currentPhase ?? "") {
                        StatusPill(kind: .warning, label: "Short output")
                    }
                }
            }
            statusPill
            menu
        }
        .padding(.horizontal, DS.space.s)
        .frame(height: 56)
        // Degradation is a state you see: a steady 3pt purple left border for
        // the remainder of degraded operation (§6 level 3).
        .overlay(alignment: .leading) {
            if state.degraded {
                Rectangle().fill(DS.status.fallback.color).frame(width: 3)
            }
        }
        .accessibilityElement(children: .combine)
    }

    @ViewBuilder
    private var statusPill: some View {
        switch state.state {
        case .running(let since):
            StatusPill(kind: .running, label: "Running \(elapsed(since))", breathing: true)
        case .fallback(let to):
            StatusPill(kind: .fallback, label: "FALLBACK → \(shorten(to))")
        case .failed(let reason):
            StatusPill(kind: .error, label: reason.isEmpty ? "Failed" : shorten(reason))
        case .waiting:
            StatusPill(kind: .idle, label: "Waiting")
        case .idle:
            StatusPill(kind: .idle, label: "Idle")
        }
    }

    private var menu: some View {
        Menu {
            Button("Open transcript") {
                if let phase = project.currentPhase {
                    NSWorkspace.shared.open(project.dirURL.appendingPathComponent(phase))
                } else {
                    NSWorkspace.shared.open(project.dirURL)
                }
            }
            Button("Open logs folder") {
                NSWorkspace.shared.open(store.logsDirURL)
            }
            Button("Reveal project in Finder") {
                NSWorkspace.shared.activateFileViewerSelecting([project.dirURL])
            }
        } label: {
            Image(systemName: "ellipsis.circle")
        }
        .menuStyle(.borderlessButton)
        .fixedSize()
        .accessibilityLabel("Actions for \(identity.displayName)")
    }

    private func elapsed(_ since: Date) -> String {
        let s = max(0, Int(Date().timeIntervalSince(since)))
        return String(format: "%02d:%02d", s / 60, s % 60)
    }

    private func shorten(_ s: String) -> String {
        s.count > 24 ? String(s.prefix(22)) + "…" : s
    }
}

// MARK: - Zone C: Event Feed (filter chips + aggregation + colored bars)

struct EventFeedView: View {
    @EnvironmentObject var store: OrchestratorStore
    let project: Project
    let events: [EngineEvent]

    enum Filter: String, CaseIterable, Identifiable {
        case all = "All", fallbacks = "Fallbacks", errors = "Errors", phases = "Phases"
        var id: String { rawValue }
    }
    @State private var filter: Filter = .all
    @State private var expanded: Set<Int> = []

    private var filtered: [EngineEvent] {
        switch filter {
        case .all: return events
        case .fallbacks: return events.filter(\.isFallback)
        case .errors: return events.filter(\.isError)
        case .phases: return events.filter(\.isPhase)
        }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: DS.space.xs) {
            HStack(spacing: DS.space.xs) {
                ForEach(Filter.allCases) { f in
                    Chip(label: f.rawValue,
                         systemImage: f == .fallbacks ? "arrow.uturn.down" : nil,
                         selected: filter == f,
                         tint: f == .fallbacks ? DS.status.fallback : DS.accent,
                         action: { filter = f })
                }
                Spacer()
            }
            if filtered.isEmpty {
                Text("No events\(filter == .all ? " yet — the engine appends them to events.jsonl during runs" : "").")
                    .font(DS.font.caption).foregroundStyle(.tertiary)
            } else {
                ScrollViewReader { proxy in
                    ScrollView {
                        LazyVStack(alignment: .leading, spacing: 2) {
                            ForEach(EventAggregator.collapse(filtered).suffix(200)) { agg in
                                eventRow(agg)
                                    .id(agg.id)
                            }
                        }
                    }
                    .onAppear { proxy.scrollTo(filtered.last?.id, anchor: .bottom) }
                }
            }
            bottomBar
        }
    }

    @ViewBuilder
    private func eventRow(_ agg: AggregatedEvent) -> some View {
        let e = agg.event
        VStack(alignment: .leading, spacing: 2) {
            HStack(alignment: .firstTextBaseline, spacing: DS.space.xs) {
                Text(e.ts, format: .dateTime.hour().minute().second())
                    .font(DS.font.caption).monospacedDigit()
                    .foregroundStyle(.tertiary)
                Image(systemName: symbol(e))
                    .font(DS.font.caption)
                    .foregroundStyle(tint(e))
                    .frame(width: 14)
                    .accessibilityHidden(true)
                Text(e.headline)
                    .font(DS.font.caption)
                    .foregroundStyle(e.isFallback ? DS.status.fallback.color
                                     : (e.isError ? DS.status.error.color : DS.textPrimary))
                if agg.count > 1 {
                    Text("× \(agg.count)")
                        .font(DS.font.caption.weight(.semibold)).monospacedDigit()
                        .foregroundStyle(.secondary)
                }
                Spacer()
                if !e.detail.isEmpty {
                    Button {
                        if expanded.contains(e.id) { expanded.remove(e.id) }
                        else { expanded.insert(e.id) }
                    } label: {
                        Image(systemName: expanded.contains(e.id) ? "chevron.down" : "chevron.right")
                            .font(DS.font.caption)
                    }
                    .buttonStyle(.plain)
                    .accessibilityLabel("Raw detail")
                }
            }
            if expanded.contains(e.id), !e.detail.isEmpty {
                // SF Mono only inside per-event raw disclosures (§4.2).
                Text(e.detail)
                    .font(DS.font.monoWell)
                    .lineSpacing(DS.font.monoWellLineSpacing)
                    .textSelection(.enabled)
                    .padding(DS.space.xs)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .background(RoundedRectangle(cornerRadius: DS.radius.control, style: .continuous)
                        .fill(.quaternary.opacity(0.5)))
            }
        }
        .padding(.vertical, 2)
        .padding(.leading, DS.space.xs)
        .overlay(alignment: .leading) {
            if e.isFallback {
                Rectangle().fill(DS.status.fallback.color).frame(width: 3)
            } else if e.isError {
                Rectangle().fill(DS.status.error.color).frame(width: 3)
            }
        }
        .accessibilityElement(children: .combine)
        .accessibilityLabel("\(e.headline)\(agg.count > 1 ? ", repeated \(agg.count) times" : "")")
    }

    private var bottomBar: some View {
        HStack {
            Button {
                NSWorkspace.shared.open(store.logsDirURL)
            } label: {
                Label("Open logs folder", systemImage: "folder")
                    .font(DS.font.caption)
            }
            Spacer()
            if let start = events.last(where: { $0.kind == "run_started" })?.ts {
                Text(runElapsed(start))
                    .font(DS.font.monoInline)
                    .foregroundStyle(.secondary)
                    .accessibilityLabel("Run elapsed \(runElapsed(start))")
            }
        }
    }

    private func runElapsed(_ start: Date) -> String {
        let end = project.running ? Date()
            : (events.last(where: { $0.kind == "run_finished" })?.ts ?? Date())
        let s = max(0, Int(end.timeIntervalSince(start)))
        return String(format: "%d:%02d:%02d", s / 3600, (s / 60) % 60, s % 60)
    }

    private func symbol(_ e: EngineEvent) -> String {
        if e.isFallback { return "arrow.triangle.branch" }
        if e.isError { return "xmark.octagon.fill" }
        if e.isPhase { return "flag" }
        if e.kind == "run_started" || e.kind == "run_finished" { return "play.circle" }
        if e.kind == "verify_result" { return "checkmark.seal" }
        return "circle.fill"
    }

    private func tint(_ e: EngineEvent) -> Color {
        if e.isFallback { return DS.status.fallback.color }
        if e.isError { return DS.status.error.color }
        if e.kind == "phase_completed" { return DS.status.success.color }
        return DS.textTertiary
    }
}

// MARK: - Stall banner (§6 controls at the point of pain — banner, not alert)

struct StallBanner: View {
    @EnvironmentObject var store: OrchestratorStore
    let project: Project
    let lastEvent: Date
    @State private var dismissed = false

    var body: some View {
        let mins = Int(Date().timeIntervalSince(lastEvent) / 60)
        if !dismissed {
            InlineBanner(kind: .warning,
                         title: "\(project.titleFor(project.currentPhase ?? "")) hasn't reported for \(mins) min",
                         message: "The engine often recovers on its own.") {
                HStack(spacing: DS.space.xs) {
                    Button("Wait") { dismissed = true }
                    Button("Kill run", role: .destructive) { store.stopRun(project.name) }
                }
            }
        }
    }
}

// MARK: - Toolbar: Fleet Health capsule + Fallback bell (§6 level 1)

struct FleetHealthCapsule: View {
    @EnvironmentObject var store: OrchestratorStore
    var onJump: (String) -> Void
    @State private var showPopover = false

    var body: some View {
        let h = store.fleetHealth
        Button { showPopover.toggle() } label: {
            StatusPill(kind: h.worst == .success ? .success : h.worst,
                       label: h.headline)
        }
        .buttonStyle(.plain)
        .help("Fleet health — click for details")
        .popover(isPresented: $showPopover, arrowEdge: .bottom) {
            VStack(alignment: .leading, spacing: DS.space.xs) {
                Text("Fleet health").font(DS.font.headline)
                healthRows(h.failed, kind: .error, word: "failed")
                healthRows(h.fallbacksActive, kind: .fallback, word: "running on fallback")
                healthRows(h.stalled, kind: .warning, word: "stalled")
                if h.worst == .success {
                    Label("All healthy", systemImage: "checkmark")
                        .font(DS.font.caption)
                        .foregroundStyle(DS.status.success.color)
                }
            }
            .padding(DS.space.m)
            .frame(minWidth: 260)
        }
        .accessibilityLabel("Fleet health: \(h.headline)")
    }

    @ViewBuilder
    private func healthRows(_ names: [String], kind: StatusKind, word: String) -> some View {
        ForEach(names, id: \.self) { name in
            Button {
                showPopover = false
                onJump(name)
            } label: {
                HStack(spacing: DS.space.xxs) {
                    Image(systemName: kind.symbol)
                        .font(DS.font.caption)
                        .foregroundStyle(kind.tint.color)
                    Text(name).font(DS.font.callout)
                    Text(word).font(DS.font.caption).foregroundStyle(.secondary)
                    Spacer()
                    Image(systemName: "arrow.right.circle").font(DS.font.caption)
                        .foregroundStyle(.tertiary)
                }
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .accessibilityLabel("\(name), \(word) — open")
        }
    }
}

struct FallbackBell: View {
    @EnvironmentObject var store: OrchestratorStore
    var onJump: (String) -> Void
    @State private var showLedger = false

    var body: some View {
        let count = store.fleetHealth.fallbacks24h.count
        Button { showLedger.toggle() } label: {
            HStack(spacing: 3) {
                Image(systemName: count > 0 ? "bell.badge" : "bell")
                    .foregroundStyle(count > 0 ? DS.status.fallback.color : DS.textSecondary)
                if count > 0 {
                    Text("\(count)")
                        .font(DS.font.callout.weight(.semibold)).monospacedDigit()
                        .foregroundStyle(DS.status.fallback.color)
                }
            }
        }
        .help("Fallbacks in the last 24h — click for the ledger")
        .popover(isPresented: $showLedger, arrowEdge: .bottom) {
            VStack(alignment: .leading, spacing: DS.space.xs) {
                Text("Fallback ledger — last 24h").font(DS.font.headline)
                if store.fleetHealth.fallbacks24h.isEmpty {
                    Text("No fallbacks. Silence here is earned, not assumed.")
                        .font(DS.font.caption).foregroundStyle(.secondary)
                } else {
                    ScrollView {
                        VStack(alignment: .leading, spacing: DS.space.xxs) {
                            ForEach(store.fleetHealth.fallbacks24h.prefix(50)) { e in
                                ledgerRow(e)
                            }
                        }
                    }
                    .frame(maxHeight: 320)
                }
            }
            .padding(DS.space.m)
            .frame(width: 380)
        }
        .accessibilityLabel("Fallback bell: \(count) in the last 24 hours")
    }

    private func ledgerRow(_ e: EngineEvent) -> some View {
        Button {
            showLedger = false
            onJump(e.project)
        } label: {
            VStack(alignment: .leading, spacing: 1) {
                HStack(spacing: DS.space.xxs) {
                    Text(e.project).font(DS.font.callout)
                    Text(e.ts, format: .dateTime.hour().minute())
                        .font(DS.font.caption).monospacedDigit()
                        .foregroundStyle(.tertiary)
                    Spacer()
                }
                Text(e.headline)
                    .font(DS.font.caption)
                    .foregroundStyle(DS.status.fallback.color)
                    .lineLimit(2)
                    .multilineTextAlignment(.leading)
            }
            .padding(.leading, DS.space.xs)
            .overlay(alignment: .leading) {
                Rectangle().fill(DS.status.fallback.color).frame(width: 3)
            }
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .accessibilityLabel("\(e.project): \(e.headline) — open")
    }
}

// MARK: - IntegrityStrip (§4.4): one thin bar per phase, segments colored by
// who actually did the work; rescued spans hatched purple.

struct HatchedRect: View {
    var body: some View {
        // 45° purple hatching over the fallback fill (graft: Mission Control).
        GeometryReader { geo in
            let step: CGFloat = 5
            Path { p in
                var x: CGFloat = -geo.size.height
                while x < geo.size.width {
                    p.move(to: CGPoint(x: x, y: geo.size.height))
                    p.addLine(to: CGPoint(x: x + geo.size.height, y: 0))
                    x += step
                }
            }
            .stroke(DS.status.fallback.color, lineWidth: 1.5)
        }
        .background(DS.status.fallback.fill)
        .clipped()
    }
}

struct IntegrityStrip: View {
    let segments: [IntegritySegment]

    var body: some View {
        HStack(spacing: 1) {
            if segments.isEmpty {
                Rectangle().fill(.quaternary.opacity(0.5))
            }
            ForEach(segments) { seg in
                Group {
                    if seg.isRescue {
                        HatchedRect()
                            .help("Rescued by \(seg.rescuedBy)")
                    } else {
                        Rectangle().fill(DS.identity(seg.agent).tint.color)
                            .help("\(DS.identity(seg.agent).displayName) — primary")
                    }
                }
            }
        }
        .frame(height: 8)
        .clipShape(RoundedRectangle(cornerRadius: 2, style: .continuous))
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(a11y)
    }

    private var a11y: String {
        guard !segments.isEmpty else { return "no recorded turns" }
        let rescued = segments.filter(\.isRescue).count
        return rescued == 0
            ? "\(segments.count) turns, all primary"
            : "\(segments.count) turns, \(rescued) rescued by fallback"
    }
}

// MARK: - Project · History (§4.4 master-detail)

struct ProjectHistoryView: View {
    @EnvironmentObject var store: OrchestratorStore
    let project: Project

    @State private var selectedRunID: Int?

    private var runs: [RunRecord] {
        RunHistoryDeriver.runs(events: store.eventsByProject[project.name] ?? [])
    }

    var body: some View {
        let list = runs
        if list.isEmpty {
            EmptyStateView(symbol: "clock.arrow.circlepath",
                           title: "No recorded runs yet",
                           message: "Run history builds from events.jsonl — runs started before the event stream landed aren't shown.")
        } else {
            HStack(spacing: 0) {
                runList(list)
                    .frame(width: 280)
                Divider()
                if let run = list.first(where: { $0.id == selectedRunID }) ?? list.first {
                    runDetail(run)
                }
            }
            .onAppear { if selectedRunID == nil { selectedRunID = list.first?.id } }
        }
    }

    private func runList(_ list: [RunRecord]) -> some View {
        List(selection: $selectedRunID) {
            ForEach(list) { run in
                VStack(alignment: .leading, spacing: 2) {
                    HStack(spacing: DS.space.xxs) {
                        Text(run.started, format: .dateTime.month().day().hour().minute())
                            .font(DS.font.callout)
                        Spacer()
                        outcomeBadge(run)
                    }
                    HStack(spacing: DS.space.xs) {
                        if let d = run.duration {
                            Text(durationLabel(d))
                                .font(DS.font.caption).monospacedDigit()
                                .foregroundStyle(.secondary)
                        }
                        if run.fallbackCount > 0 {
                            StatusPill(kind: .fallback, label: "\(run.fallbackCount)")
                        }
                    }
                }
                .tag(run.id)
                .accessibilityLabel("Run \(run.started.formatted()), \(run.status.isEmpty ? "in progress" : run.status), \(run.fallbackCount) fallbacks")
            }
        }
        .listStyle(.inset)
    }

    @ViewBuilder
    private func outcomeBadge(_ run: RunRecord) -> some View {
        if run.finished == nil {
            StatusPill(kind: .running, label: "Live", breathing: true)
        } else if run.status == "done" {
            StatusPill(kind: .success, label: "Done")
        } else {
            StatusPill(kind: .error, label: run.status.isEmpty ? "Ended" : run.status)
        }
    }

    private func runDetail(_ run: RunRecord) -> some View {
        ScrollView {
            VStack(alignment: .leading, spacing: DS.space.s) {
                Text("Who did the work").font(DS.font.headline)
                Text("Primary agent tint for primary calls; hatched purple = rescued by a fallback model.")
                    .font(DS.font.caption).foregroundStyle(.secondary)
                ForEach(RunHistoryDeriver.phasesInRun(run), id: \.self) { phase in
                    let segs = RunHistoryDeriver.integritySegments(run: run, phase: phase)
                    VStack(alignment: .leading, spacing: 3) {
                        HStack {
                            Text(project.titleFor(phase)).font(DS.font.callout)
                            Spacer()
                            if segs.contains(where: \.isRescue) {
                                StatusPill(kind: .fallback,
                                           label: "\(segs.filter(\.isRescue).count) rescued")
                            }
                        }
                        IntegrityStrip(segments: segs)
                    }
                }
                Divider()
                DisclosureGroup {
                    EventFeedView(project: project, events: run.events)
                        .frame(minHeight: 200)
                } label: {
                    Label("Run events", systemImage: "list.bullet.rectangle")
                        .font(DS.font.callout)
                }
            }
            .padding(DS.space.margin)
        }
    }

    private func durationLabel(_ d: TimeInterval) -> String {
        let s = Int(d)
        return s >= 3600 ? String(format: "%dh %02dm", s / 3600, (s / 60) % 60)
                         : String(format: "%dm %02ds", s / 60, s % 60)
    }
}

// MARK: - Activity (§4.6 — honest by design)

struct ActivityView: View {
    @EnvironmentObject var store: OrchestratorStore

    enum Range: String, CaseIterable, Identifiable {
        case today = "Today", week = "7d", month = "30d"
        var id: String { rawValue }
        var days: Int { self == .today ? 1 : (self == .week ? 7 : 30) }
    }
    @State private var range: Range = .week
    @AppStorage("activity.planCostPerMonth") private var planCost = 0.0

    private var allEvents: [EngineEvent] {
        store.eventsByProject.values.flatMap { $0 }
    }

    private func turns(inLastDays days: Int, offsetDays: Int = 0) -> [EngineEvent] {
        let end = Calendar.current.startOfDay(for: Date())
            .addingTimeInterval(TimeInterval(86_400 - offsetDays * 86_400))
        let start = end.addingTimeInterval(TimeInterval(-days * 86_400))
        return allEvents.filter {
            $0.kind == "turn_completed" && $0.ts >= start && $0.ts < end
        }
    }

    var body: some View {
        let current = turns(inLastDays: range.days)
        let previous = turns(inLastDays: range.days, offsetDays: range.days)
        ScrollView {
            VStack(alignment: .leading, spacing: DS.space.zone) {
                HStack {
                    Text("Activity").font(DS.font.title)
                    Spacer()
                    Picker("Range", selection: $range) {
                        ForEach(Range.allCases) { r in Text(r.rawValue).tag(r) }
                    }
                    .pickerStyle(.segmented)
                    .labelsHidden()
                    .frame(width: 200)
                }

                providerTiles(current: current, previous: previous)

                // Always visible, directly beneath the tiles (§4.6).
                Text("Subscription CLIs report invocations, not tokens. Counts are CLI calls; costs are not estimable.")
                    .font(DS.font.caption).foregroundStyle(.secondary)

                callsPerDay(current)
                Divider()
                fallbackBurden(current)
                Divider()
                localUtilization
                Divider()
                planCostSection(current)
            }
            .padding(DS.space.margin)
        }
    }

    private func providerTiles(current: [EngineEvent], previous: [EngineEvent]) -> some View {
        HStack(alignment: .top, spacing: DS.space.l) {
            ForEach(RoutingMatrix.agents, id: \.self) { agent in
                let id = DS.identity(agent)
                let n = current.filter { DS.identity($0.agent).key == agent }.count
                let p = previous.filter { DS.identity($0.agent).key == agent }.count
                VStack(alignment: .leading, spacing: DS.space.xxs) {
                    StatTile(value: "\(n)", label: "\(id.displayName) calls",
                             tint: id.tint.color)
                    if p > 0 || n > 0 {
                        let delta = n - p
                        Text(delta == 0 ? "· level" : (delta > 0 ? "▲ \(delta)" : "▼ \(-delta)"))
                            .font(DS.font.caption).monospacedDigit()
                            .foregroundStyle(.secondary)
                            .accessibilityLabel("change versus previous period: \(delta)")
                    }
                }
            }
            Spacer()
        }
    }

    // Stacked calls/day bars in agent identity hues (capsule stacks).
    private func callsPerDay(_ turns: [EngineEvent]) -> some View {
        let cal = Calendar.current
        let days = (0..<range.days).map {
            cal.startOfDay(for: Date()).addingTimeInterval(TimeInterval(-$0 * 86_400))
        }.reversed()
        let maxTotal = max(1, days.map { day in
            turns.filter { cal.isDate($0.ts, inSameDayAs: day) }.count
        }.max() ?? 1)
        return VStack(alignment: .leading, spacing: DS.space.xs) {
            Text("Calls per day").font(DS.font.headline)
            HStack(alignment: .bottom, spacing: 3) {
                ForEach(Array(days), id: \.self) { day in
                    let dayTurns = turns.filter { cal.isDate($0.ts, inSameDayAs: day) }
                    VStack(spacing: 1) {
                        ForEach(RoutingMatrix.agents.reversed(), id: \.self) { agent in
                            let n = dayTurns.filter { DS.identity($0.agent).key == agent }.count
                            if n > 0 {
                                Capsule()
                                    .fill(DS.identity(agent).tint.color)
                                    .frame(width: range.days > 7 ? 10 : 22,
                                           height: max(2, CGFloat(n) / CGFloat(maxTotal) * 80))
                            }
                        }
                    }
                    .frame(height: 84, alignment: .bottom)
                    .help("\(day.formatted(date: .abbreviated, time: .omitted)): \(dayTurns.count) calls")
                    .accessibilityLabel("\(day.formatted(date: .abbreviated, time: .omitted)): \(dayTurns.count) calls")
                }
            }
        }
    }

    // Per agent: primary vs rescued calls — the weekly-review surface where
    // silent fallbacks become a trend.
    private func fallbackBurden(_ turns: [EngineEvent]) -> some View {
        let rescues = allEvents.filter {
            $0.isFallback && $0.status == "rescued"
                && $0.ts > Date().addingTimeInterval(TimeInterval(-range.days * 86_400))
        }
        return VStack(alignment: .leading, spacing: DS.space.xs) {
            Text("Fallback burden").font(DS.font.headline)
            Grid(alignment: .leading, horizontalSpacing: DS.space.m, verticalSpacing: DS.space.xxs) {
                GridRow {
                    Text("Agent").font(DS.font.caption).foregroundStyle(.secondary)
                    Text("Primary").font(DS.font.caption).foregroundStyle(.secondary)
                    Text("Rescued").font(DS.font.caption).foregroundStyle(.secondary)
                    Text("%").font(DS.font.caption).foregroundStyle(.secondary)
                }
                Divider()
                ForEach(["codex", "claude", "gemini"], id: \.self) { agent in
                    let total = turns.filter { DS.identity($0.agent).key == agent }.count
                    let rescued = rescues.filter { DS.identity($0.agent).key == agent }.count
                    let pct = total + rescued == 0 ? 0
                        : Int((Double(rescued) / Double(total + rescued) * 100).rounded())
                    GridRow {
                        HStack(spacing: DS.space.xxs) {
                            AgentAvatar(agent: agent, size: 14)
                            Text(DS.identity(agent).displayName).font(DS.font.callout)
                        }
                        Text("\(total)").font(DS.font.callout).monospacedDigit()
                        Text("\(rescued)")
                            .font(DS.font.callout).monospacedDigit()
                            .foregroundStyle(rescued > 0 ? DS.status.fallback.color : DS.textPrimary)
                        Text("\(pct)%")
                            .font(DS.font.callout).monospacedDigit()
                            .foregroundStyle(pct > 10 ? DS.status.fallback.color : DS.textSecondary)
                            .padding(.horizontal, pct > 10 ? 4 : 0)
                            .background(pct > 10 ? DS.status.fallback.fill : Color.clear,
                                        in: Capsule())
                    }
                    .accessibilityElement(children: .combine)
                    .accessibilityLabel("\(DS.identity(agent).displayName): \(total) primary, \(rescued) rescued, \(pct) percent")
                }
            }
        }
    }

    private var localUtilization: some View {
        let limit = store.readRuntimeInt("local_active_limit", defaultValue: 4)
        let roster = store.readLocalRoster()
        return VStack(alignment: .leading, spacing: DS.space.xxs) {
            Text("Local models").font(DS.font.headline)
            Text("\(min(roster.count, limit)) of \(limit) active slots (roster: \(roster.count))")
                .font(DS.font.callout).monospacedDigit()
            if roster.isEmpty {
                Text("No local models on the roster — Library › Models & Agents.")
                    .font(DS.font.caption).foregroundStyle(.tertiary)
            }
        }
    }

    // Optional user-entered plan cost → one derived stat, explicitly labeled
    // as user-supplied. No invented dollar signs anywhere else.
    private func planCostSection(_ turns: [EngineEvent]) -> some View {
        VStack(alignment: .leading, spacing: DS.space.xxs) {
            HStack(spacing: DS.space.xs) {
                Text("Plan cost / month").font(DS.font.callout)
                TextField("0", value: $planCost, format: .number)
                    .textFieldStyle(.roundedBorder)
                    .frame(width: 80)
                    .accessibilityLabel("Your plan cost per month, user supplied")
            }
            if planCost > 0 {
                let monthly = Double(turns.count) * (30.0 / Double(range.days))
                let perDollar = monthly / planCost
                Text(String(format: "≈ %.0f calls per plan-dollar (user-supplied cost; call counts, not billing data)", perDollar))
                    .font(DS.font.caption).foregroundStyle(.secondary)
            } else {
                Text("Enter your subscription's monthly cost to see calls per plan-dollar. Explicitly user-supplied — the CLIs report no billing data.")
                    .font(DS.font.caption).foregroundStyle(.tertiary)
            }
        }
    }
}
