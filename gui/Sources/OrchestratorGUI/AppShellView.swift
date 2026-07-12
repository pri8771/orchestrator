import SwiftUI
import AppKit

// MARK: - Native Pro shell (DESIGN-NATIVE-PRO.md §3)
//
// One window: NavigationSplitView (sidebar + content) + trailing .inspector.
// Since M5 this is the app's only surface (§9): the factory dashboard and the
// classic browser are gone, and everything they hosted lives here — queue
// reorder + autorun + retry/stop + lanes in the sidebar, intake in the ⌘N
// sheet, the workflow builder under Library › Workflows, the local roster in
// Models & Agents, transcripts per project, and the docked run log on ⌘L.

// What the sidebar can select.
enum ShellSelection: Hashable {
    case overview
    case activity
    case project(String)
    case workflows
    case models
}

struct AppShellView: View {
    @EnvironmentObject var store: OrchestratorStore

    @State private var selection: ShellSelection? = .overview
    @State private var searchText = ""
    @State private var showInspector = true
    @State private var showNewApp = false
    @State private var doneExpanded = false
    @State private var showLanesPopover = false
    @State private var showRunLog = false
    @FocusState private var searchFocused: Bool

    // MARK: Section classification — the same store arrays the 460pt queue
    // panel reads today (FactoryDashboard.swift), mapped 1:1 to the four
    // PROJECTS sections.

    private func isRunning(_ p: Project) -> Bool {
        store.appLocks[p.name] != nil || store.canStop(p.name)
    }

    private var runningApps: [Project] {
        filtered(store.projects.filter { isRunning($0) }
            .sorted { (store.appLocks[$0.name]?.since ?? .distantFuture)
                    < (store.appLocks[$1.name]?.since ?? .distantFuture) })
    }
    private var needsAttentionApps: [Project] {
        filtered(store.projects.filter {
            !isRunning($0) && ($0.status == .aborted || $0.awaitingApproval != nil
                               || $0.blockedConflict != nil)
        }.sorted { $0.name < $1.name })
    }
    private var doneApps: [Project] {
        filtered(store.projects.filter { !isRunning($0) && $0.status == .done }
            .sorted { $0.name < $1.name })
    }
    private var queuedApps: [Project] {
        let attention = Set(needsAttentionApps.map(\.name))
        let idx = Dictionary(store.queueOrder.enumerated().map { ($1, $0) },
                             uniquingKeysWith: { a, _ in a })
        return filtered(store.projects
            .filter { !isRunning($0) && $0.status != .done && $0.status != .aborted
                      && !attention.contains($0.name) }
            .sorted { (idx[$0.name] ?? Int.max, $0.name) < (idx[$1.name] ?? Int.max, $1.name) })
    }

    private func filtered(_ list: [Project]) -> [Project] {
        let q = searchText.trimmingCharacters(in: .whitespaces)
        guard !q.isEmpty else { return list }
        return list.filter {
            $0.name.localizedCaseInsensitiveContains(q)
                || $0.workflowTitle.localizedCaseInsensitiveContains(q)
        }
    }

    private var selectedProject: Project? {
        if case .project(let name) = selection {
            return store.projects.first { $0.name == name }
        }
        return nil
    }

    private var anyRunnable: Bool {
        store.agentOrder.contains { (store.enabledAgents[$0] ?? false) && (store.cliAvailable[$0] ?? false) }
    }

    // Explains why Run is enabled or disabled, so a greyed-out button isn't a
    // mystery (the common cause is no logged-in agent CLI).
    private var runButtonHelp: String {
        if selectedProject == nil { return "Select a project to run" }
        if selectedProject?.running ?? false { return "This project is already running" }
        if store.runQueue.contains(selectedProject?.name ?? "") { return "This project is already queued" }
        if !anyRunnable {
            return "No agent is runnable — enable one and make sure its CLI is "
                + "installed and logged in (codex / claude / gemini), or enable a local model"
        }
        return "Run the selected project (queues if the engine is busy)"
    }

    // MARK: Body

    var body: some View {
        NavigationSplitView {
            sidebar
                .navigationSplitViewColumnWidth(min: 200, ideal: 220, max: 300)
        } detail: {
            content
                // Docked live run log (⌘L), scoped to the content pane so the
                // sidebar and inspector keep their full height — the same
                // placement the classic browser used (migrated in M5).
                .safeAreaInset(edge: .bottom, spacing: 0) {
                    if showRunLog {
                        RunLogPanel(isPresented: $showRunLog)
                            .environmentObject(store)
                            .transition(.move(edge: .bottom))
                    }
                }
                .inspector(isPresented: $showInspector) {
                    // M1 ships the region; ProjectInspectorView (M2) fills it.
                    ShellInspectorPane(project: selectedProject)
                        .inspectorColumnWidth(min: 300, ideal: 340, max: 420)
                }
        }
        .navigationTitle(selectedProject?.name ?? "Orchestrator")
        .navigationSubtitle(selectedProject.map(subtitle(for:)) ?? "")
        .searchable(text: $searchText, placement: .sidebar, prompt: "Filter projects")
        .shellSearchFocused($searchFocused)
        .toolbar { toolbarContent }
        .safeAreaInset(edge: .top, spacing: 0) {
            VStack(spacing: 0) {
                if !store.engineAvailable {
                    EngineMissingBanner(message: store.engineMissingMessage)
                }
                if let err = store.lastError {
                    ActionErrorBanner(message: err) { store.lastError = nil }
                }
            }
        }
        .sheet(isPresented: $showNewApp) {
            NewAppIntakeSheet { slug in selection = .project(slug) }
                .environmentObject(store)
        }
        .onChange(of: store.uiCommand) { _, cmd in
            guard let cmd else { return }
            switch cmd {
            case .newChat: showNewApp = true
            case .runSelected:
                if let p = selectedProject, !p.running, anyRunnable,
                   !store.runQueue.contains(p.name) {
                    store.runOrQueue(p.name)
                }
            case .toggleInspector: showInspector.toggle()
            case .focusSearch: searchFocused = true
            case .toggleLog: withAnimation { showRunLog.toggle() }
            case .openPlanTab: return     // handled by ProjectShellContent
            }
            store.uiCommand = nil
        }
        .frame(minWidth: 860, minHeight: 560)
    }

    private func subtitle(for p: Project) -> String {
        if let phase = p.currentPhase, p.status == .inProgress {
            return "\(p.titleFor(phase)) · round \(p.currentRound)"
        }
        return p.status.label
    }

    // MARK: Toolbar (Fleet Health capsule + Fallback bell arrive with M4's
    // event stream; M1 ships New App / Run / Inspector.)

    @ToolbarContentBuilder
    private var toolbarContent: some ToolbarContent {
        // §6 level 1 — never hidden: fleet health + the fallback bell.
        ToolbarItemGroup(placement: .principal) {
            FleetHealthCapsule { selection = .project($0) }
            FallbackBell { selection = .project($0) }
        }
        ToolbarItemGroup {
            Button {
                if let p = selectedProject { store.runOrQueue(p.name) }
            } label: {
                Label(store.orchestratorRunning ? "Queue" : "Run",
                      systemImage: store.orchestratorRunning ? "text.append" : "play.fill")
            }
            .disabled(selectedProject == nil || (selectedProject?.running ?? false)
                      || store.runQueue.contains(selectedProject?.name ?? "") || !anyRunnable)
            .help(runButtonHelp)

            if let p = selectedProject, p.running || store.canStop(p.name) {
                Button { store.stopRun(p.name) } label: {
                    Label("Stop", systemImage: "stop.fill")
                }
                .help("Stop this run")
            }

            Button { showNewApp = true } label: {
                Label("New App", systemImage: "plus")
            }
            .buttonStyle(.borderedProminent)
            .help("Queue a new idea (⌘N)")

            Button { showInspector.toggle() } label: {
                Label("Inspector", systemImage: "sidebar.trailing")
            }
            .help("Show or hide the inspector (⌥⌘I)")
        }
    }

    // MARK: Sidebar

    private var sidebar: some View {
        List(selection: $selection) {
            Section("Factory") {
                Label("Overview", systemImage: "square.grid.2x2")
                    .tag(ShellSelection.overview)
                Label("Activity", systemImage: "chart.bar.xaxis")
                    .tag(ShellSelection.activity)
            }

            Section {
                if runningApps.isEmpty {
                    Text("No lanes busy").font(DS.font.caption).foregroundStyle(.tertiary)
                }
                ForEach(runningApps) { p in
                    // Health dot: purple wins while the run is degraded (§6 level 2).
                    ShellProjectRow(project: p,
                                    health: store.fleetHealth.fallbacksActive.contains(p.name)
                                        ? .fallback
                                        : (store.fleetHealth.stalled.contains(p.name) ? .warning : .running),
                                    detail: p.currentPhase.map { p.titleFor($0) } ?? "starting")
                        .tag(ShellSelection.project(p.name))
                }
            } header: {
                sectionHeader("Running", count: runningApps.count)
            }

            Section {
                ForEach(queuedApps) { p in
                    ShellProjectRow(project: p, health: .idle, detail: p.workflowTitle,
                                    position: queuedApps.firstIndex { $0.name == p.name }.map { $0 + 1 })
                        .tag(ShellSelection.project(p.name))
                }
                .onMove(perform: moveQueued)
            } header: {
                sectionHeader("Queued", count: queuedApps.count)
            }

            // Materializes only when non-empty (§3 region 1).
            if !needsAttentionApps.isEmpty {
                Section {
                    ForEach(needsAttentionApps) { p in
                        ShellProjectRow(project: p,
                                        health: p.status == .aborted ? .error : .warning,
                                        detail: attentionDetail(p))
                            .tag(ShellSelection.project(p.name))
                    }
                } header: {
                    sectionHeader("Needs Attention", count: needsAttentionApps.count,
                                  tint: DS.status.warning.color)
                }
            }

            Section("Done", isExpanded: $doneExpanded) {
                ForEach(doneApps) { p in
                    ShellProjectRow(project: p, health: .success, detail: p.workflowTitle)
                        .tag(ShellSelection.project(p.name))
                }
            }

            Section("Library") {
                Label("Workflows", systemImage: "arrow.triangle.branch")
                    .tag(ShellSelection.workflows)
                Label("Models & Agents", systemImage: "cpu")
                    .tag(ShellSelection.models)
            }
        }
        .listStyle(.sidebar)
        .safeAreaInset(edge: .bottom, spacing: 0) { lanesFooter }
    }

    private func sectionHeader(_ title: String, count: Int, tint: Color? = nil) -> some View {
        HStack(spacing: DS.space.xxs) {
            Text(title)
            if count > 0 {
                Text("\(count)")
                    .font(DS.font.caption).monospacedDigit()
                    .foregroundStyle(tint ?? DS.textSecondary)
                    .padding(.horizontal, 5)
                    .background(Capsule().fill((tint ?? DS.textSecondary).opacity(0.12)))
            }
        }
    }

    private func attentionDetail(_ p: Project) -> String {
        if p.awaitingApproval != nil { return "awaiting approval" }
        if p.blockedConflict != nil { return "merge conflict" }
        if let phase = p.currentPhase { return "failed at \(p.titleFor(phase))" }
        return "failed"
    }

    // Native .onMove over the queued section → the same persisted file the
    // dashboard's drag-reorder writes (.orch-queue-order.json), via the
    // existing store calls.
    private func moveQueued(from source: IndexSet, to destination: Int) {
        let displayed = queuedApps.map(\.name)
        store.beginQueueDrag(displayedOrder: displayed)
        store.queueOrder.move(fromOffsets: source, toOffset: destination)
        store.endQueueDrag()
    }

    // Persistent Lanes control (§3 sidebar footer): concurrency is fleet
    // state; it never hides in Settings. Also carries the shepherd heartbeat
    // (migrated from the factory dashboard's top bar, §9).
    private var lanesFooter: some View {
        let running = store.projects.filter { isRunning($0) }.count
        let idle = max(0, store.buildLanes - running)
        return HStack(spacing: DS.space.xs) {
            Button { showLanesPopover.toggle() } label: {
                HStack(spacing: DS.space.xxs) {
                    Image(systemName: "road.lanes")
                    Text("\(store.buildLanes) lane\(store.buildLanes == 1 ? "" : "s")")
                        .font(DS.font.callout).monospacedDigit()
                }
            }
            .buttonStyle(.plain)
            .popover(isPresented: $showLanesPopover, arrowEdge: .top) {
                LanesPopover().environmentObject(store)
            }
            .accessibilityLabel("Build lanes: \(store.buildLanes)")
            HStack(spacing: DS.space.xxs) {
                Circle()
                    .fill(store.shepherdActive ? DS.status.success.color : DS.textTertiary)
                    .frame(width: 6, height: 6)
                Text("shepherd")
                    .font(DS.font.caption)
                    .foregroundStyle(DS.textSecondary)
            }
            .help(store.shepherdActive
                  ? "The shepherd is watching the queue and launching lanes"
                  : "Shepherd idle — queued apps wait until it runs")
            .accessibilityElement(children: .ignore)
            .accessibilityLabel(store.shepherdActive ? "Shepherd active" : "Shepherd idle")
            Spacer()
            Text("\(running) running · \(idle) idle")
                .font(DS.font.caption).monospacedDigit()
                .foregroundStyle(DS.textSecondary)
        }
        .padding(.horizontal, DS.space.s)
        .padding(.vertical, DS.space.xs)
        .background(.bar)
        .overlay(alignment: .top) { Divider() }
    }

    // MARK: Content (selection-driven, one thing deep)

    @ViewBuilder
    private var content: some View {
        switch selection {
        case .overview, nil:
            FactoryOverviewView(
                running: runningApps, queued: queuedApps,
                needsAttention: needsAttentionApps, done: doneApps,
                onOpen: { selection = .project($0) },
                onNewApp: { showNewApp = true })
        case .activity:
            ActivityView()
        case .workflows:
            WorkflowsLibraryView()
        case .models:
            ModelsLibraryPane()
        case .project:
            if let p = selectedProject {
                ProjectShellContent(project: p)
                    .id(p.name)
            } else {
                shellPlaceholder("Project not found", "questionmark.folder")
            }
        }
    }

    private func shellPlaceholder(_ title: String, _ symbol: String) -> some View {
        VStack(spacing: DS.space.s) {
            Image(systemName: symbol).font(DS.font.emptyStateIcon).foregroundStyle(.tertiary)
            Text(title).font(DS.font.body).foregroundStyle(.secondary)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }
}

// ⌘F focuses the sidebar search field where the OS supports programmatic
// search focus (macOS 15+); earlier systems still get the field, just not
// keyboard-driven focus.
private extension View {
    @ViewBuilder
    func shellSearchFocused(_ binding: FocusState<Bool>.Binding) -> some View {
        if #available(macOS 15.0, *) {
            self.searchFocused(binding)
        } else {
            self
        }
    }
}

// MARK: - Inspector pane host (M2: the three-tab ProjectInspectorView).
// Nothing selected → fleet-defaults caption (§3).

struct ShellInspectorPane: View {
    let project: Project?

    var body: some View {
        if let project {
            ProjectInspectorView(project: project)
        } else {
            VStack(spacing: DS.space.s) {
                Image(systemName: "slider.horizontal.3")
                    .font(DS.font.emptyStateIcon).foregroundStyle(.tertiary)
                Text("These are global defaults — select a project to override.")
                    .font(DS.font.caption)
                    .foregroundStyle(.secondary)
                    .multilineTextAlignment(.center)
                    .padding(.horizontal, DS.space.m)
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity)
        }
    }
}

// MARK: - Sidebar project row: monogram avatar (project hue), name, inline
// phase capsule, 6pt health dot. Carries the full project action set as a
// context menu (migrated from the classic browser's ProjectRow, §9).

private struct ShellProjectRow: View {
    @EnvironmentObject var store: OrchestratorStore
    let project: Project
    let health: StatusKind
    let detail: String
    var position: Int? = nil

    @State private var confirmReset = false
    @State private var confirmDelete = false
    @State private var showHistory = false
    @State private var showIterate = false

    private var anyRunnable: Bool {
        store.agentOrder.contains { (store.enabledAgents[$0] ?? false) && (store.cliAvailable[$0] ?? false) }
    }

    var body: some View {
        rowBody
            .contextMenu { menuItems }
            .sheet(isPresented: $showIterate) {
                IterateSheet(project: project).environmentObject(store)
            }
            .sheet(isPresented: $showHistory) {
                BuildHistorySheet(project: project).environmentObject(store)
            }
            .confirmationDialog("Reset \(project.name)?", isPresented: $confirmReset) {
                Button("Reset — move generated work to the Trash", role: .destructive) {
                    store.resetProject(project)
                }
                Button("Duplicate as fork first, then reset", role: .destructive) {
                    store.forkProject(project)
                    store.resetProject(project)
                }
                Button("Cancel", role: .cancel) {}
            } message: {
                Text("Moves transcripts, docs, verification, and the built app to the Trash. Keeps your prompt and settings.")
            }
            .confirmationDialog("Delete \(project.name)?", isPresented: $confirmDelete) {
                Button("Move project to Trash", role: .destructive) {
                    store.deleteProject(project)
                }
                Button("Cancel", role: .cancel) {}
            } message: {
                Text("Moves the whole project folder — prompt included — to the Trash.")
            }
    }

    @ViewBuilder
    private var menuItems: some View {
        let running = project.running || store.canStop(project.name)
            || store.appLocks[project.name] != nil
        if !running && anyRunnable {
            Button("Run / add to queue") { store.runOrQueue(project.name) }
        }
        if store.runQueue.contains(project.name) {
            Button("Remove from run queue") { store.removeFromQueue(project.name) }
        }
        if running {
            Button("Stop run", role: .destructive) { store.stopRun(project.name) }
        }
        if project.status == .aborted && !running {
            Button("Retry — clear error") { store.retryFailedApp(project.name) }
        }
        if !running {
            // Shepherd autorun marker (migrated from the dashboard's MiniToggle).
            let paused = store.autorunDisabled.contains(project.name)
            Button(paused ? "Enable autorun (shepherd)" : "Pause autorun (shepherd)") {
                store.setAutorunEnabled(project.name, paused)
            }
        }
        if project.status == .done && !running {
            Button("Add a feature / iterate…") { showIterate = true }
                .disabled(!anyRunnable)
        }
        Divider()
        if let xcode = store.xcodeProjectURL(for: project) {
            Button("Open in Xcode") { NSWorkspace.shared.open(xcode) }
        }
        Button("Reveal in Finder") {
            NSWorkspace.shared.activateFileViewerSelecting([project.dirURL])
        }
        Button("Duplicate as fork") { store.forkProject(project) }
        Button("Build history…") { showHistory = true }
        Divider()
        Button("Reset to prompt…", role: .destructive) { confirmReset = true }
            .disabled(running)
        Button("Delete project…", role: .destructive) { confirmDelete = true }
            .disabled(running)
    }

    private var rowBody: some View {
        HStack(spacing: DS.space.xs) {
            monogram
            VStack(alignment: .leading, spacing: 1) {
                Text(project.name)
                    .font(DS.font.body)
                    .lineLimit(1)
                HStack(spacing: DS.space.xxs) {
                    if let position {
                        Text("#\(position)")
                            .font(DS.font.caption).monospacedDigit()
                            .foregroundStyle(.secondary)
                    }
                    Text(detail)
                        .font(DS.font.caption)
                        .foregroundStyle(.secondary)
                        .lineLimit(1)
                }
            }
            Spacer()
            Circle()
                .fill(health.tint.color)
                .frame(width: 6, height: 6)
                .accessibilityHidden(true)
        }
        .padding(.vertical, 1)
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(project.name)
        .accessibilityValue(detail)
    }

    private var monogram: some View {
        let tint = DS.projectTint(project.name)
        let initials = String(project.name.replacingOccurrences(of: "-", with: " ")
            .split(separator: " ").prefix(2).compactMap(\.first)).uppercased()
        return Text(initials.isEmpty ? "?" : initials)
            .font(DS.font.caption.weight(.medium))
            .foregroundStyle(tint.color)
            .frame(width: 22, height: 22)
            .background(RoundedRectangle(cornerRadius: DS.radius.control, style: .continuous)
                .fill(tint.fill))
            .overlay(RoundedRectangle(cornerRadius: DS.radius.control, style: .continuous)
                .stroke(tint.stroke, lineWidth: 1))
            .accessibilityHidden(true)
    }
}

// MARK: - Lanes popover (slider 1–9 + live annotation)

private struct LanesPopover: View {
    @EnvironmentObject var store: OrchestratorStore

    var body: some View {
        let running = store.projects.filter {
            store.appLocks[$0.name] != nil || store.canStop($0.name)
        }.count
        VStack(alignment: .leading, spacing: DS.space.xs) {
            Text("Build lanes").font(DS.font.headline)
            Text("How many apps build at once. The shepherd honours this from .orch-queue-order.json.")
                .font(DS.font.caption).foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
            HStack(spacing: DS.space.s) {
                Slider(value: Binding(
                    get: { Double(store.buildLanes) },
                    set: { store.setBuildLanes(Int($0.rounded())) }
                ), in: 1...9, step: 1)
                Text("\(store.buildLanes)")
                    .font(DS.font.callout).monospacedDigit()
                    .frame(width: 18)
            }
            Text("\(running) running · \(max(0, store.buildLanes - running)) idle")
                .font(DS.font.caption).monospacedDigit()
                .foregroundStyle(DS.textSecondary)
        }
        .padding(DS.space.m)
        .frame(width: 260)
    }
}

// MARK: - Factory Overview (§4.1, hollow M1 version: stat row + active runs +
// empty state. The Recent Events feed arrives with M4's events.jsonl reader.)

private struct FactoryOverviewView: View {
    @EnvironmentObject var store: OrchestratorStore
    let running: [Project]
    let queued: [Project]
    let needsAttention: [Project]
    let done: [Project]
    let onOpen: (String) -> Void
    let onNewApp: () -> Void

    var body: some View {
        if running.isEmpty && queued.isEmpty && needsAttention.isEmpty && done.isEmpty {
            emptyState
        } else {
            ScrollView {
                VStack(alignment: .leading, spacing: DS.space.zone) {
                    statRow
                    Divider()
                    activeRuns
                    Divider()
                    recentEvents
                }
                .padding(DS.space.margin)
            }
        }
    }

    // §4.1 bottom: last 20 engine events, identical repeats aggregated "× N",
    // fallback rows with a 3pt purple accent bar.
    @ViewBuilder
    private var recentEvents: some View {
        Text("Recent Events").font(DS.font.headline)
        let latest = store.eventsByProject.values.flatMap { $0 }
            .sorted { $0.ts < $1.ts }
            .suffix(60)
        let rows = EventAggregator.collapse(Array(latest)).suffix(20)
        if rows.isEmpty {
            Text("No engine events yet — they stream into events.jsonl as runs execute.")
                .font(DS.font.caption).foregroundStyle(.tertiary)
        }
        VStack(alignment: .leading, spacing: 2) {
            ForEach(Array(rows)) { agg in
                let e = agg.event
                Button { onOpen(e.project) } label: {
                    HStack(spacing: DS.space.xs) {
                        Text(e.ts, format: .dateTime.hour().minute())
                            .font(DS.font.caption).monospacedDigit()
                            .foregroundStyle(.tertiary)
                        Text(e.project)
                            .font(DS.font.caption)
                            .foregroundStyle(.secondary)
                            .frame(width: 130, alignment: .leading)
                            .lineLimit(1)
                        Text(e.headline)
                            .font(DS.font.caption)
                            .foregroundStyle(e.isFallback ? DS.status.fallback.color
                                             : (e.isError ? DS.status.error.color : DS.textPrimary))
                            .lineLimit(1)
                        if agg.count > 1 {
                            Text("× \(agg.count)")
                                .font(DS.font.caption.weight(.semibold)).monospacedDigit()
                                .foregroundStyle(.secondary)
                        }
                        Spacer()
                    }
                    .padding(.leading, DS.space.xs)
                    .overlay(alignment: .leading) {
                        if e.isFallback {
                            Rectangle().fill(DS.status.fallback.color).frame(width: 3)
                        } else if e.isError {
                            Rectangle().fill(DS.status.error.color).frame(width: 3)
                        }
                    }
                    .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                .accessibilityLabel("\(e.project): \(e.headline)\(agg.count > 1 ? ", repeated \(agg.count) times" : "") — open")
            }
        }
        .padding(DS.space.xs)
        .background(RoundedRectangle(cornerRadius: DS.radius.chip, style: .continuous)
            .fill(.quaternary.opacity(0.5)))
    }

    private var statRow: some View {
        HStack(alignment: .top, spacing: DS.space.l) {
            StatTile(value: "\(running.count)", label: "Running",
                     tint: running.isEmpty ? DS.textPrimary : DS.accent.color)
            hairline
            StatTile(value: "\(queued.count)", label: "Queued")
            hairline
            StatTile(value: "\(needsAttention.count)", label: "Needs Attention",
                     tint: needsAttention.isEmpty ? DS.textPrimary : DS.status.warning.color)
            hairline
            StatTile(value: "\(done.count)", label: "Done",
                     tint: done.isEmpty ? DS.textPrimary : DS.status.success.color)
            Spacer()
        }
    }

    private var hairline: some View {
        Rectangle().fill(DS.hairline).frame(width: 1, height: 44)
    }

    @ViewBuilder
    private var activeRuns: some View {
        Text("Active Runs").font(DS.font.headline)
        if running.isEmpty {
            Text("Nothing building right now.")
                .font(DS.font.body).foregroundStyle(.secondary)
        }
        ForEach(running) { p in
            Button { onOpen(p.name) } label: {
                HStack(spacing: DS.space.s) {
                    VStack(alignment: .leading, spacing: DS.space.xxs) {
                        Text(p.name).font(DS.font.headline)
                        Text(p.progressText)
                            .font(DS.font.caption).foregroundStyle(.secondary)
                    }
                    Spacer()
                    StatusPill(kind: .running,
                               label: p.currentPhase.map { p.titleFor($0) } ?? "Starting",
                               breathing: true)
                }
                .padding(DS.space.s)
                .frame(maxWidth: .infinity, alignment: .leading)
                .background(RoundedRectangle(cornerRadius: DS.radius.card, style: .continuous)
                    .fill(.quaternary.opacity(0.5)))
                .overlay(RoundedRectangle(cornerRadius: DS.radius.card, style: .continuous)
                    .stroke(DS.hairline, lineWidth: 1))
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .accessibilityLabel("\(p.name), running — open")
        }
    }

    private var emptyState: some View {
        VStack(spacing: DS.space.s) {
            Image(systemName: "sparkles")
                .font(DS.font.emptyStateIcon)
                .foregroundStyle(.tertiary)
            Text("No apps building")
                .font(DS.font.largeTitle)
            Text("Press ⌘N to queue your first idea.")
                .font(DS.font.body).foregroundStyle(.secondary)
            Button("New App") { onNewApp() }
                .buttonStyle(.borderedProminent)
                .padding(.top, DS.space.xs)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }
}

// MARK: - Library panes (hollow M1: reuse existing organs)

private struct WorkflowsLibraryView: View {
    @EnvironmentObject var store: OrchestratorStore
    @State private var showBuilder = false

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: DS.space.zone) {
                HStack {
                    Text("Workflows").font(DS.font.title)
                    Spacer()
                    Button("Open Builder…") { showBuilder = true }
                }
                ForEach(store.workflows) { wf in
                    HStack(spacing: DS.space.s) {
                        Image(systemName: wf.symbol).foregroundStyle(DS.accent.color)
                            .frame(width: 20)
                        VStack(alignment: .leading, spacing: 1) {
                            Text(wf.title).font(DS.font.headline)
                            Text(wf.description.isEmpty
                                 ? "\(wf.phases.count) phases · \(wf.kindLabel)"
                                 : wf.description)
                                .font(DS.font.caption).foregroundStyle(.secondary)
                                .lineLimit(2)
                        }
                        Spacer()
                        if store.isBuiltInWorkflow(wf.name) {
                            Text("built-in").font(DS.font.caption).foregroundStyle(.tertiary)
                        }
                    }
                    .padding(DS.space.s)
                    .background(RoundedRectangle(cornerRadius: DS.radius.card, style: .continuous)
                        .fill(.quaternary.opacity(0.5)))
                }
            }
            .padding(DS.space.margin)
        }
        .sheet(isPresented: $showBuilder) {
            WorkflowBuilderSheet().environmentObject(store)
        }
    }
}

private struct ModelsLibraryPane: View {
    var body: some View {
        ModelsAgentsView()
    }
}

// MARK: - Project content: segmented scope bar (§3 region 2) — Run (the §4.2
// health zones) · Plan (the M3 routing grid) · Transcript (the old organs:
// phase list + banners + TranscriptView) · History (§4.4).

enum ProjectScopeTab: String, CaseIterable, Identifiable {
    case run = "Run"
    case plan = "Plan"
    case transcript = "Transcript"
    case history = "History"
    var id: String { rawValue }
}

private struct ProjectShellContent: View {
    @EnvironmentObject var store: OrchestratorStore
    let project: Project
    @State private var scopeTab: ProjectScopeTab = .run

    private var liveProject: Project {
        store.projects.first { $0.name == project.name } ?? project
    }

    var body: some View {
        let proj = liveProject
        VStack(spacing: 0) {
            HStack {
                Picker("View", selection: $scopeTab) {
                    ForEach(ProjectScopeTab.allCases) { t in
                        Text(t.rawValue).tag(t)
                    }
                }
                .pickerStyle(.segmented)
                .labelsHidden()
                .frame(width: 340)
                Spacer()
            }
            .padding(.horizontal, DS.space.s)
            .padding(.vertical, DS.space.xs)
            Divider()
            switch scopeTab {
            case .run: ProjectRunHealth(project: proj)
            case .plan: ProjectPlanTab(project: proj)
            case .transcript: ProjectRunContent(project: proj)
            case .history: ProjectHistoryView(project: proj)
            }
        }
        .onChange(of: store.uiCommand) { _, cmd in
            guard cmd == .openPlanTab else { return }
            scopeTab = .plan
            store.uiCommand = nil
        }
    }
}

// The §4.2 screen users stare at: Zone A phase timeline (pinned) · Zone B
// agent board · Zone C event feed, plus the run banners + stall banner.
private struct ProjectRunHealth: View {
    @EnvironmentObject var store: OrchestratorStore
    let project: Project

    private var events: [EngineEvent] { store.eventsByProject[project.name] ?? [] }

    var body: some View {
        VStack(spacing: 0) {
            PhaseTimelineView(project: project)
            Divider()
            if project.running, let last = events.last?.ts,
               Date().timeIntervalSince(last) > 240 {
                StallBanner(project: project, lastEvent: last)
            }
            if project.status == .aborted, let err = project.error, !err.isEmpty {
                ErrorBanner(project: project, message: err)
            }
            if let bc = project.blockedConflict {
                ConflictBanner(conflict: bc)
            }
            if let ap = project.awaitingApproval {
                ApprovalBanner(project: project, phase: ap)
                    .environmentObject(store)
            }
            if let v = project.latestVerify {
                VerificationCard(record: v, repairCount: project.verifyRepairCount)
            }
            ScrollView {
                VStack(alignment: .leading, spacing: DS.space.zone) {
                    Text("Agent Board").font(DS.font.headline)
                    AgentBoardView(project: project, events: events)
                    Text("Event Feed").font(DS.font.headline)
                    EventFeedView(project: project, events: events)
                        .frame(minHeight: 260)
                }
                .padding(DS.space.margin)
            }
        }
    }
}

// The §4.3 Plan tab: workflow pop-up, the routing grid card, and disclosures.
private struct ProjectPlanTab: View {
    @EnvironmentObject var store: OrchestratorStore
    let project: Project

    private var workflow: WorkflowDef {
        store.workflow(named: project.workflow)
            ?? store.workflow(named: "app_build")
            ?? WorkflowDef(name: "app_build", title: "Build an App", description: "",
                           target: "app", phases: ALL_PHASES)
    }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: DS.space.zone) {
                HStack(spacing: DS.space.xs) {
                    Text("Workflow").font(DS.font.callout).foregroundStyle(.secondary)
                    Picker("", selection: Binding(
                        get: { project.workflow },
                        set: { store.setWorkflow(project: project, workflow: $0) }
                    )) {
                        ForEach(store.workflows) { wf in
                            Text(wf.title).tag(wf.name)
                        }
                    }
                    .labelsHidden()
                    .pickerStyle(.menu)
                    .disabled(project.running)
                    .accessibilityLabel("Workflow for \(project.name)")
                    if project.running {
                        Text("running — workflow locked")
                            .font(DS.font.caption).foregroundStyle(.tertiary)
                    }
                    Spacer()
                }
                RoutingGridView(scope: .project(name: project.name), workflow: workflow)
                planDisclosures
            }
            .padding(DS.space.margin)
        }
    }

    @ViewBuilder
    private var planDisclosures: some View {
        DisclosureGroup {
            Form { QualityGatesSection() }
                .formStyle(.grouped)
                .frame(maxHeight: 220)
        } label: {
            Label("Quality Gates (fleet)", systemImage: "checkmark.shield")
                .font(DS.font.callout)
        }
        DisclosureGroup {
            ProjectFallbackOverrides(project: project)
        } label: {
            Label("Fallback Overrides", systemImage: "arrow.uturn.down")
                .font(DS.font.callout)
        }
    }
}

// Project-level ladder overrides, rendered dimmed-inherited until edited
// (§4.3). Writes the per-project model_routing.json's fallback.chains.
private struct ProjectFallbackOverrides: View {
    @EnvironmentObject var store: OrchestratorStore
    let project: Project
    @State private var routing = ModelRouting()
    @State private var fleet = ModelRouting()
    @State private var loaded = false

    var body: some View {
        VStack(alignment: .leading, spacing: DS.space.xs) {
            ForEach(["codex", "claude", "gemini"], id: \.self) { agent in
                let overridden = !(routing.chains[agent] ?? []).isEmpty
                VStack(alignment: .leading, spacing: DS.space.xxs) {
                    HStack(spacing: DS.space.xxs) {
                        AgentAvatar(agent: agent, size: 14)
                        Text(DS.identity(agent).displayName).font(DS.font.callout)
                        if !overridden {
                            Text("inherited: \((fleet.chains[agent] ?? []).isEmpty ? "safety net only" : (fleet.chains[agent] ?? []).joined(separator: " → "))")
                                .font(DS.font.caption).foregroundStyle(.tertiary)
                                .lineLimit(1).truncationMode(.middle)
                        }
                        Spacer()
                    }
                    .opacity(overridden ? 1 : 0.6)
                    FallbackChainEditor(agent: agent, chain: Binding(
                        get: { routing.chains[agent] ?? [] },
                        set: {
                            routing.chains[agent] = $0.isEmpty ? nil : $0
                            store.writeProjectRouting(routing, for: project)
                        }))
                }
            }
        }
        .padding(.vertical, DS.space.xxs)
        .onAppear {
            guard !loaded else { return }
            routing = store.readProjectRouting(project)
            fleet = store.readModelRouting()
            loaded = true
        }
    }
}

// The pre-M3 project surface, unchanged: phase list + banners + transcript.
private struct ProjectRunContent: View {
    @EnvironmentObject var store: OrchestratorStore
    let project: Project
    @State private var selectedPhaseKey: String?

    var body: some View {
        let proj = project
        HStack(spacing: 0) {
            PhaseListView(project: proj, selection: $selectedPhaseKey)
                .frame(width: 250)
            Divider()
            VStack(spacing: 0) {
                if proj.status == .aborted, let err = proj.error, !err.isEmpty {
                    ErrorBanner(project: proj, message: err)
                }
                if let bc = proj.blockedConflict {
                    ConflictBanner(conflict: bc)
                }
                if let ap = proj.awaitingApproval {
                    ApprovalBanner(project: proj, phase: ap)
                        .environmentObject(store)
                }
                if let v = proj.latestVerify {
                    VerificationCard(record: v, repairCount: proj.verifyRepairCount)
                }
                if let key = selectedPhaseKey {
                    TranscriptView(project: proj, phaseKey: key)
                } else {
                    VStack(spacing: DS.space.s) {
                        Image(systemName: "bubble.left.and.bubble.right")
                            .font(DS.font.emptyStateIcon).foregroundStyle(.tertiary)
                        Text("Select a phase").foregroundStyle(.secondary)
                    }
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
                }
            }
        }
        .onAppear { syncPhaseSelection(proj) }
        .onChange(of: proj.currentPhase) { _, _ in followLiveEdge(proj) }
    }

    private func syncPhaseSelection(_ proj: Project) {
        guard selectedPhaseKey == nil else { return }
        selectedPhaseKey = proj.currentPhase
            ?? proj.completedPhases.last
            ?? store.phases(for: proj).first?.key
    }

    private func followLiveEdge(_ proj: Project) {
        guard proj.status == .inProgress, let cur = proj.currentPhase else { return }
        // Only auto-follow while the selection sits on the live edge.
        if selectedPhaseKey == nil || selectedPhaseKey == proj.completedPhases.last {
            selectedPhaseKey = cur
        }
    }
}
