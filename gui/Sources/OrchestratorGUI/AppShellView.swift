import SwiftUI
import AppKit
import UniformTypeIdentifiers

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
    case home
    case overview
    case activity
    case project(String)
    case section(String)   // V3 3.8: a section studio's chat list
    case conductor
    case workflows
    case models
}

struct AppShellView: View {
    @EnvironmentObject var store: OrchestratorStore

    @State private var selection: ShellSelection? = .home
    @State private var searchText = ""
    @State private var showInspector = true
    @State private var showNewApp = false
    @State private var doneExpanded = false
    @State private var archivedExpanded = false
    @State private var showLanesPopover = false
    @State private var showRunLog = false
    @State private var routeRequest: ArtifactRouteRequest?
    @FocusState private var searchFocused: Bool

    // MARK: Section classification — the same store arrays the 460pt queue
    // panel reads today (FactoryDashboard.swift), mapped 1:1 to the four
    // PROJECTS sections.

    private func isRunning(_ p: Project) -> Bool {
        AppShellLogic.showsAsRunning(lockPresent: store.appLocks[p.name] != nil,
                                     lockStale: store.staleLocks.contains(p.name),
                                     guiOwned: store.canStop(p.name))
    }

    // Archived projects leave the four active sections entirely; they live in
    // their own collapsed section below with a Restore action.
    private var activeProjects: [Project] { store.projects.filter { !$0.archived } }
    private var archivedApps: [Project] {
        filtered(store.projects.filter(\.archived).sorted { $0.name < $1.name })
    }

    private var runningApps: [Project] {
        filtered(activeProjects.filter { isRunning($0) }
            .sorted { (store.appLocks[$0.name]?.since ?? .distantFuture)
                    < (store.appLocks[$1.name]?.since ?? .distantFuture) })
    }
    private var needsAttentionApps: [Project] {
        filtered(activeProjects.filter { p in
            !isRunning(p) && (p.status == .aborted || p.awaitingApproval != nil
                              || p.blockedConflict != nil
                              || store.crashedRuns.contains { $0.name == p.name })
        }.sorted { $0.name < $1.name })
    }
    private var doneApps: [Project] {
        filtered(activeProjects.filter { !isRunning($0) && $0.status == .done }
            .sorted { $0.name < $1.name })
    }
    private var queuedApps: [Project] {
        let attention = Set(needsAttentionApps.map(\.name))
        let idx = Dictionary(store.queueOrder.enumerated().map { ($1, $0) },
                             uniquingKeysWith: { a, _ in a })
        return filtered(activeProjects
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
            let focused = store.paneCanvas.focusedSessionID ?? name
            return store.projects.first { $0.name == focused }
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
                if case .inProgress(let step) = store.onboardingProgress,
                   step > 1, !store.showOnboarding {
                    OnboardingCoachBar { store.showOnboarding = true }
                        .environmentObject(store)
                }
            }
        }
        .sheet(isPresented: $showNewApp) {
            NewAppIntakeSheet { slug in selection = .project(slug) }
                .environmentObject(store)
        }
        .sheet(item: $routeRequest) { request in
            ArtifactRoutePicker(request: request)
                .environmentObject(store)
        }
        // ⌘K palette: a floating panel over a dimmed scrim (DESIGN-REFRESH.md
        // §6), overlaid on the whole shell rather than presented as a sheet.
        .overlay {
            if store.showCommandPalette {
                CommandPaletteView(onJumpToProject: { selection = .project($0) })
                    .environmentObject(store)
            }
        }
        .onChange(of: store.uiCommand) { _, cmd in
            guard let cmd else { return }
            switch cmd {
            case .newChat: showNewApp = true
            case .newBrainstorm:
                let projectID = selectedProject?.name.components(separatedBy: "/").first
                    ?? "home"
                if let session = store.mintBrainstorm(project: projectID) {
                    store.startChatSession(session.id)
                    selection = .project(session.id)
                }
            case .sendToSection:
                if let artifact = store.commandRoutableArtifact,
                   let source = store.commandProjectName {
                    routeRequest = ArtifactRouteRequest(artifact: artifact,
                                                        sourceSession: source)
                }
            case .openConductor:
                if let target = AppShellLogic.conductorDestination(
                    available: store.conductorSurfaceAvailable) { selection = target }
            case .showOnboarding:
                store.beginOnboarding()
            case .focusPane1: store.focusPane(at: 0)
            case .focusPane2: store.focusPane(at: 1)
            case .focusPane3: store.focusPane(at: 2)
            case .closeFocusedPane: store.closeFocusedPane()
            case .runSelected:
                if let p = selectedProject, !p.running, anyRunnable,
                   !store.runQueue.contains(p.name) {
                    store.runOrQueue(p.name)
                }
            case .toggleInspector: showInspector.toggle()
            case .focusSearch: searchFocused = true
            case .toggleLog: withAnimation { showRunLog.toggle() }
            case .togglePause: store.toggleEnginePaused()
            case .openPlanTab: return     // handled by ProjectShellContent
            }
            store.uiCommand = nil
        }
        .onAppear { store.updateCommandContext(projectName: selectedProject?.name) }
        .onChange(of: selection) { _, _ in
            if case .project(let name) = selection {
                store.openPane(name, asSplit: false)
            }
            store.updateCommandContext(projectName: selectedProject?.name)
        }
        .sheet(isPresented: $store.showOnboarding) {
            OnboardingView(
                onStartBrainstorm: {
                    store.showOnboarding = false
                    store.uiCommand = .newBrainstorm
                },
                onOpenModels: {
                    store.showOnboarding = false
                    selection = .models
                },
                onDismiss: { store.showOnboarding = false })
                .environmentObject(store)
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

            Button { store.toggleEnginePaused() } label: {
                Label(store.enginePaused ? "Resume Engine" : "Pause Engine",
                      systemImage: store.enginePaused ? "play.circle" : "pause.circle")
            }
            .help(store.enginePaused
                  ? "Resume auto-launching queued projects"
                  : "Pause the engine: queued projects won't auto-launch (running work continues)")
            .accessibilityValue(store.enginePaused ? "Paused" : "Running")

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
                Label("Home", systemImage: "bubble.left.and.text.bubble.right")
                    .tag(ShellSelection.home)
                Label("Overview", systemImage: "square.grid.2x2")
                    .tag(ShellSelection.overview)
                Label("Activity", systemImage: "chart.bar.xaxis")
                    .tag(ShellSelection.activity)
                Label("Conductor", systemImage: "point.3.connected.trianglepath.dotted")
                    .tag(ShellSelection.conductor)
            }

            sectionsRailSection

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
                        .onDrag { PaneSessionDrag.provider(for: p.name) }
                        .tag(ShellSelection.project(p.name))
                }
            } header: {
                sectionHeader("Running", count: runningApps.count)
            }

            Section {
                ForEach(queuedApps) { p in
                    ShellProjectRow(project: p, health: .idle, detail: p.workflowTitle,
                                    position: queuedApps.firstIndex { $0.name == p.name }.map { $0 + 1 })
                        .onDrag { PaneSessionDrag.provider(for: p.name) }
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
                            .onDrag { PaneSessionDrag.provider(for: p.name) }
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
                        .onDrag { PaneSessionDrag.provider(for: p.name) }
                        .tag(ShellSelection.project(p.name))
                }
            }

            // Removed-from-list projects (folder kept). Materializes only when
            // non-empty; Restore lives in the row's context menu.
            if !archivedApps.isEmpty {
                Section("Archived", isExpanded: $archivedExpanded) {
                    ForEach(archivedApps) { p in
                        ShellProjectRow(project: p, health: .idle, detail: "archived")
                            .onDrag { PaneSessionDrag.provider(for: p.name) }
                            .tag(ShellSelection.project(p.name))
                    }
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
        if store.crashedRuns.contains(where: { $0.name == p.name }) {
            return "crashed — resume available"
        }
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
        case .home, nil:
            ChatHomeView(onOpenProject: { selection = .project($0) },
                         onNewApp: { showNewApp = true })
        case .overview:
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
            if !store.paneCanvas.panes.isEmpty {
                PaneCanvasView()
            } else {
                shellPlaceholder("Project not found", "questionmark.folder")
            }
        case .section(let name):
            SectionChatsView(section: name,
                             onOpenChat: { selection = .project($0) })
        case .conductor:
            MissionControlView()
        }
    }

    // V3 3.8 sub-PR 1: the section rail. Every state is explicit (R4);
    // status lines derive from live Project state only (R2).
    @ViewBuilder
    private var sectionsRailSection: some View {
        Section("Sections") {
            switch store.sectionRail {
            case .loading:
                Text("Loading sections…")
                    .font(DS.font.caption).foregroundStyle(.tertiary)
            case .empty:
                Text("No sections yet")
                    .font(DS.font.caption).foregroundStyle(.tertiary)
                Button("Seed default sections") { store.seedDefaultSections() }
                    .font(DS.font.caption)
            case .error(let message):
                Label(message, systemImage: "exclamationmark.triangle")
                    .font(DS.font.caption)
                    .foregroundStyle(DS.status.warning.color)
            case .populated(let metas):
                ForEach(SectionDisclosureLogic.visible(
                    metas, revealed: store.visibleSectionIDs)) { meta in
                    SectionRailDropRow(
                        section: meta,
                        status: SectionRailLogic.statusLine(
                            section: meta.id, projects: store.projects),
                        lint: store.sectionLint[meta.id] ?? nil)
                        .tag(ShellSelection.section(meta.id))
                }
                if metas.contains(where: {
                    !store.visibleSectionIDs.contains($0.id)
                }) {
                    Button("Show all sections") { store.revealAllSections() }
                        .font(DS.font.caption)
                }
                if let revealed = store.newlyRevealedSection {
                    Label("\(revealed.capitalized) just appeared",
                          systemImage: "sparkles")
                        .font(DS.font.caption)
                        .foregroundStyle(DS.accent.color)
                        .onTapGesture { store.newlyRevealedSection = nil }
                }
            }
        }
    }

    private func shellPlaceholder(_ title: String, _ symbol: String) -> some View {
        EmptyStateView(symbol: symbol, title: title)
    }
}

enum PaneSessionDrag {
    private static let prefix = "orchestrator-session:"

    static func provider(for sessionID: String) -> NSItemProvider {
        NSItemProvider(object: (prefix + sessionID) as NSString)
    }

    static func decode(_ text: String) -> String? {
        guard text.hasPrefix(prefix) else { return nil }
        let id = String(text.dropFirst(prefix.count))
        return id.isEmpty ? nil : id
    }

    static func load(_ providers: [NSItemProvider], perform: @escaping (String) -> Void)
        -> Bool {
        guard let provider = providers.first(where: {
            $0.canLoadObject(ofClass: NSString.self)
        }) else { return false }
        provider.loadObject(ofClass: NSString.self) { object, _ in
            guard let text = object as? String, let id = decode(text) else { return }
            DispatchQueue.main.async { perform(id) }
        }
        return true
    }
}

private struct PaneCanvasView: View {
    @EnvironmentObject var store: OrchestratorStore
    @State private var canvasDropTarget = false

    var body: some View {
        GeometryReader { geometry in
            let count = store.paneCanvas.visibleCount(
                availableWidth: geometry.size.width)
            let visible = Array(store.paneCanvas.panes.prefix(count))
            let compact = Array(store.paneCanvas.panes.dropFirst(count))
                + store.paneCanvas.overflow
            VStack(spacing: 0) {
                HSplitView {
                    ForEach(visible, id: \.self) { sessionID in
                        PaneCanvasPane(sessionID: sessionID)
                            .frame(minWidth: 0, maxWidth: .infinity,
                                   maxHeight: .infinity)
                    }
                }
                .background(canvasDropTarget ? DS.accent.fill : Color.clear)
                .onDrop(of: [UTType.text], isTargeted: $canvasDropTarget) { providers in
                    PaneSessionDrag.load(providers) {
                        store.openPane($0, asSplit: true)
                    }
                }
                if !compact.isEmpty {
                    Divider()
                    ScrollView(.horizontal, showsIndicators: false) {
                        HStack(spacing: DS.space.xs) {
                            Text("More")
                                .font(DS.font.caption)
                                .foregroundStyle(DS.textSecondary)
                            ForEach(compact, id: \.self) { sessionID in
                                Button {
                                    if store.paneCanvas.overflow.contains(sessionID) {
                                        store.activateOverflowPane(sessionID)
                                    } else {
                                        store.bringPaneIntoVisiblePrefix(
                                            sessionID, count: count)
                                    }
                                } label: {
                                    Label(sessionID.components(separatedBy: "/").last
                                          ?? sessionID,
                                          systemImage: "rectangle.split.3x1")
                                        .font(DS.font.caption)
                                }
                                .buttonStyle(.bordered)
                                .help("Swap (sessionID) into the focused pane")
                            }
                        }
                        .padding(.horizontal, DS.space.s)
                        .padding(.vertical, DS.space.xs)
                    }
                    .background(.bar)
                }
            }
        }
    }
}

private struct PaneCanvasPane: View {
    @EnvironmentObject var store: OrchestratorStore
    let sessionID: String
    @State private var paneDropTarget = false

    private var project: Project? {
        store.projects.first { $0.name == sessionID }
    }

    private var focused: Bool {
        store.paneCanvas.focusedSessionID == sessionID
    }

    var body: some View {
        VStack(spacing: 0) {
            HStack(spacing: DS.space.xs) {
                Circle()
                    .fill(focused ? DS.accent.color : DS.textTertiary)
                    .frame(width: 7, height: 7)
                Text(sessionID)
                    .font(DS.font.caption)
                    .lineLimit(1)
                Spacer()
                Button { store.closePane(sessionID) } label: {
                    Image(systemName: "xmark")
                }
                .buttonStyle(.plain)
                .help("Close pane")
                .accessibilityLabel("Close (sessionID) pane")
            }
            .padding(.horizontal, DS.space.s)
            .padding(.vertical, DS.space.xs)
            .background(focused ? DS.accent.fill : Color.clear)
            Divider()
            if let project {
                ProjectShellContent(project: project)
                    .id(project.name)
            } else {
                EmptyStateView(symbol: "questionmark.folder",
                               title: "Session not found",
                               message: sessionID)
            }
        }
        .contentShape(Rectangle())
        .onTapGesture { store.focusPane(sessionID) }
        .overlay {
            RoundedRectangle(cornerRadius: DS.radius.control, style: .continuous)
                .stroke(focused || paneDropTarget
                        ? DS.accent.color : Color.clear,
                        lineWidth: focused ? 2 : 1)
                .allowsHitTesting(false)
        }
        .onDrop(of: [UTType.text], isTargeted: $paneDropTarget) { providers in
            PaneSessionDrag.load(providers) {
                store.replacePane(sessionID, with: $0)
            }
        }
        .accessibilityElement(children: .contain)
        .accessibilityLabel("Pane (sessionID)")
        .accessibilityValue(focused ? "Focused" : "Not focused")
    }
}

private struct SectionRailDropRow: View {
    @EnvironmentObject var store: OrchestratorStore
    let section: SectionMeta
    let status: String
    let lint: SectionLintSummary?
    @State private var isDropTarget = false

    var body: some View {
        HStack(spacing: DS.space.xs) {
            VStack(alignment: .leading, spacing: 1) {
                Text(section.title).font(DS.font.body)
                Text(status).font(DS.font.caption)
                    .foregroundStyle(.tertiary).lineLimit(1)
            }
            Spacer()
            if isDropTarget {
                Image(systemName: "arrow.down.circle.fill")
                    .foregroundStyle(DS.accent.color)
                    .accessibilityLabel("Drop to route here")
            } else if let lint, lint.errors + lint.warnings > 0 {
                Image(systemName: lint.errors > 0
                      ? "xmark.octagon.fill" : "exclamationmark.triangle.fill")
                    .font(DS.font.caption)
                    .foregroundStyle(lint.errors > 0
                                     ? DS.status.error.color : DS.status.warning.color)
                    .help("\(lint.errors) error(s), \(lint.warnings) warning(s) — "
                          + "open Section Settings › Lint")
            }
        }
        .padding(.vertical, DS.space.xxs)
        .background(RoundedRectangle(cornerRadius: DS.radius.control, style: .continuous)
            .fill(isDropTarget ? AnyShapeStyle(DS.accent.fill) : AnyShapeStyle(Color.clear)))
        .onDrop(of: [UTType.text], isTargeted: $isDropTarget) { providers in
            guard let provider = providers.first(where: {
                $0.canLoadObject(ofClass: NSString.self)
            }) else { return false }
            provider.loadObject(ofClass: NSString.self) { object, _ in
                guard let text = object as? String,
                      let payload = ArtifactDragPayload.decode(text) else { return }
                let sourceParts = payload.sourceSession.components(separatedBy: "/")
                guard sourceParts.count != 3 || sourceParts[1] != section.id else { return }
                DispatchQueue.main.async {
                    store.routeArtifact(
                        ArtifactRouteRef(id: payload.artifactID, type: payload.type,
                                         version: payload.version),
                        from: payload.sourceSession, to: section.id)
                }
            }
            return true
        }
    }
}

struct ArtifactRouteRequest: Identifiable {
    let artifact: ArtifactRouteRef
    let sourceSession: String
    var id: String { artifact.id }
}

private struct ArtifactRoutePicker: View {
    @EnvironmentObject var store: OrchestratorStore
    @Environment(\.dismiss) private var dismiss
    let request: ArtifactRouteRequest

    private var sourceSection: String? {
        let parts = request.sourceSession.components(separatedBy: "/")
        return parts.count == 3 ? parts[1] : nil
    }

    var body: some View {
        VStack(alignment: .leading, spacing: DS.space.m) {
            Text("Send \(request.artifact.id) to …")
                .font(DS.font.headline)
            Text("The engine will validate final status, lineage, and route admissibility.")
                .font(DS.font.caption).foregroundStyle(.secondary)
            switch store.sectionRail {
            case .populated(let sections):
                ForEach(sections.filter { $0.id != sourceSection }) { section in
                    Button {
                        store.routeArtifact(request.artifact,
                                            from: request.sourceSession,
                                            to: section.id)
                    } label: {
                        Label(section.title, systemImage: "arrow.turn.up.right")
                            .frame(maxWidth: .infinity, alignment: .leading)
                    }
                    .disabled(isRouting)
                }
            case .loading:
                ProgressView("Loading sections…")
            case .empty:
                Text("No target sections are available.")
                    .foregroundStyle(.secondary)
            case .error(let message):
                Text(message).foregroundStyle(DS.status.error.color)
            }
            if let state = store.artifactRouteState(
                    request.artifact.id, sourceSession: request.sourceSession) {
                routeStatus(state)
            }
            HStack {
                Spacer()
                Button("Done") { dismiss() }
            }
        }
        .padding(DS.space.l)
        .frame(width: 420)
    }

    private var isRouting: Bool {
        if case .routing = store.artifactRouteState(
                request.artifact.id, sourceSession: request.sourceSession) { return true }
        return false
    }

    @ViewBuilder
    private func routeStatus(_ state: ArtifactRouteState) -> some View {
        switch state {
        case .routing(let target):
            ProgressView("Routing to \(target)…")
        case .routed(let target):
            Label("Routed to \(target)", systemImage: "checkmark.circle.fill")
                .foregroundStyle(DS.status.success.color)
        case .refused(let reason):
            Label(reason, systemImage: "xmark.octagon.fill")
                .foregroundStyle(DS.status.error.color)
        }
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
            EmptyStateView(symbol: "slider.horizontal.3",
                           title: "Fleet defaults",
                           message: "These are global defaults — select a project to override them.")
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
    @State private var confirmRemove = false
    @State private var showHistory = false
    @State private var showIterate = false

    private var anyRunnable: Bool {
        store.agentOrder.contains { (store.enabledAgents[$0] ?? false) && (store.cliAvailable[$0] ?? false) }
    }

    private var isLive: Bool {
        project.running || store.canStop(project.name)
            || (store.appLocks[project.name] != nil
                && !store.staleLocks.contains(project.name))
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
            .confirmationDialog(isLive ? "Stop and remove \(project.name)?"
                                       : "Remove \(project.name)?",
                                isPresented: $confirmRemove) {
                Button("Archive whole project — move to .archive") {
                    store.removeProject(project, deleteFolder: false)
                }
                Button("Remove and move folder to Trash", role: .destructive) {
                    store.removeProject(project, deleteFolder: true)
                }
                Button("Cancel", role: .cancel) {}
            } message: {
                Text(ProjectArchivePresentation.confirmation(
                    project: ProjectArchivePresentation.projectSlug(
                        for: project.name), stopping: isLive)
                    + " The Trash option removes it entirely.")
            }
    }

    @ViewBuilder
    private var menuItems: some View {
        if project.archived {
            archivedMenuItems
        } else {
            activeMenuItems
        }
    }

    @ViewBuilder
    private var archivedMenuItems: some View {
        Button("Restore from archive") { store.unarchiveProject(project) }
        Button("Reveal in Finder") {
            NSWorkspace.shared.activateFileViewerSelecting([project.dirURL])
        }
        Divider()
        Button("Remove…", role: .destructive) { confirmRemove = true }
    }

    @ViewBuilder
    private var activeMenuItems: some View {
        let running = isLive
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
            // Staged continuation: a finished research/spec/etc. project can be
            // re-opened under a different workflow — prior outputs carry over
            // as context (engine --continue-with).
            Menu("Continue with workflow…") {
                ForEach(store.workflows.filter { $0.name != project.workflow },
                        id: \.name) { wf in
                    Button(wf.title) {
                        store.continueProject(project.name, workflow: wf.name)
                    }
                }
            }
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
        Menu("Rate…") {
            Button("👍 Good — teach from this project") {
                store.rateProject(project, verdict: "good")
            }
            Button("👎 Bad — feed the anti-pattern ledger") {
                store.rateProject(project, verdict: "bad")
            }
            Button("Clear rating") { store.rateProject(project, verdict: nil) }
        }
        Divider()
        Button("Reset to prompt…", role: .destructive) { confirmReset = true }
            .disabled(running)
        Button("Remove…", role: .destructive) { confirmRemove = true }
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
                VStack(alignment: .leading, spacing: DS.space.xs) {
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
                    // §4.1: the per-phase timeline capsule with agent avatars,
                    // embedded at glance altitude — the card stays one click.
                    PhaseTimelineView(project: p, compact: true)
                        .allowsHitTesting(false)
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
        EmptyStateView(symbol: "sparkles",
                       title: "No apps building",
                       message: "Press ⌘N to queue your first idea.",
                       actionLabel: "New App",
                       action: onNewApp,
                       prominent: true)
    }
}

// MARK: - Library panes (hollow M1: reuse existing organs)

private struct WorkflowsLibraryView: View {
    @EnvironmentObject var store: OrchestratorStore
    @State private var showBuilder = false
    @State private var showPipelineBuilder = false
    @State private var showDocumentBuilder = false

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: DS.space.zone) {
                HStack {
                    Text("Workflows").font(DS.font.title)
                    Spacer()
                    Button("Document Flow…") { showDocumentBuilder = true }
                        .accessibilityIdentifier("open-document-builder")
                    Button("Pipeline Canvas…") { showPipelineBuilder = true }
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
                ProfilesLibrarySection()
                SnippetsLibrarySection()
            }
            .padding(DS.space.margin)
        }
        .sheet(isPresented: $showBuilder) {
            WorkflowBuilderSheet().environmentObject(store)
        }
        .sheet(isPresented: $showPipelineBuilder) {
            PipelineBuilderSheet().environmentObject(store)
        }
        .sheet(isPresented: $showDocumentBuilder) {
            DocumentBuilderSheet().environmentObject(store)
        }
    }
}

// Saved run profiles: workflow + per-phase models/effort/rounds/instructions
// bundles, saved from a project's Plan tab and applied from the New App sheet.
private struct ProfilesLibrarySection: View {
    @EnvironmentObject var store: OrchestratorStore
    @State private var profiles: [RunProfile] = []

    var body: some View {
        VStack(alignment: .leading, spacing: DS.space.xs) {
            Text("Profiles").font(DS.font.title)
            Text("A profile bundles a workflow with per-phase models, effort, rounds, and instructions. Save one from any project's Plan tab; apply it in the New App sheet.")
                .font(DS.font.caption).foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
            if profiles.isEmpty {
                Text("No profiles yet.").font(DS.font.caption).foregroundStyle(.tertiary)
            }
            ForEach(profiles) { p in
                HStack(spacing: DS.space.s) {
                    Image(systemName: "square.stack.3d.up")
                        .foregroundStyle(DS.accent.color).frame(width: 20)
                    VStack(alignment: .leading, spacing: 1) {
                        Text(p.name).font(DS.font.headline)
                        Text(p.workflow.isEmpty ? "any workflow" : p.workflow)
                            .font(DS.font.caption).foregroundStyle(.secondary)
                    }
                    Spacer()
                    Button("Delete") {
                        store.deleteProfile(p)
                        profiles = store.listProfiles()
                    }
                    .buttonStyle(.plain)
                    .font(DS.font.caption)
                    .foregroundStyle(DS.status.error.color)
                }
                .padding(DS.space.s)
                .background(RoundedRectangle(cornerRadius: DS.radius.card, style: .continuous)
                    .fill(.quaternary.opacity(0.5)))
            }
        }
        .onAppear { profiles = store.listProfiles() }
    }
}

// Reusable per-phase prompt snippets, insertable wherever phase instructions
// are edited (Plan tab › Phase Rounds & Instructions).
private struct SnippetsLibrarySection: View {
    @EnvironmentObject var store: OrchestratorStore
    @State private var snippets: [PromptSnippet] = []
    @State private var newName = ""
    @State private var newPhase = ""
    @State private var newText = ""
    @State private var loaded = false

    var body: some View {
        VStack(alignment: .leading, spacing: DS.space.xs) {
            Text("Prompt Snippets").font(DS.font.title)
            Text("Reusable instruction blocks. Scope one to a phase key (e.g. design_handoff) or leave the scope empty to offer it everywhere.")
                .font(DS.font.caption).foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
            ForEach(snippets) { s in
                HStack(alignment: .top, spacing: DS.space.s) {
                    Image(systemName: "text.badge.plus")
                        .foregroundStyle(DS.accent.color).frame(width: 20)
                    VStack(alignment: .leading, spacing: 1) {
                        HStack(spacing: DS.space.xxs) {
                            Text(s.name).font(DS.font.headline)
                            if !s.phase.isEmpty {
                                Text(s.phase).font(DS.font.caption)
                                    .foregroundStyle(.secondary)
                                    .padding(.horizontal, 5)
                                    .background(Capsule().fill(.quaternary.opacity(0.6)))
                            }
                        }
                        Text(s.text).font(DS.font.caption)
                            .foregroundStyle(.secondary).lineLimit(2)
                    }
                    Spacer()
                    Button("Delete") {
                        snippets.removeAll { $0.id == s.id }
                        store.saveSnippets(snippets)
                    }
                    .buttonStyle(.plain)
                    .font(DS.font.caption)
                    .foregroundStyle(DS.status.error.color)
                }
                .padding(DS.space.s)
                .background(RoundedRectangle(cornerRadius: DS.radius.card, style: .continuous)
                    .fill(.quaternary.opacity(0.5)))
            }
            VStack(alignment: .leading, spacing: DS.space.xxs) {
                HStack(spacing: DS.space.xs) {
                    TextField("Snippet name", text: $newName)
                        .textFieldStyle(.roundedBorder).frame(width: 180)
                    TextField("Phase key (optional)", text: $newPhase)
                        .textFieldStyle(.roundedBorder).frame(width: 180)
                    Spacer()
                }
                TextEditor(text: $newText)
                    .font(DS.font.body)
                    .scrollContentBackground(.hidden)
                    .padding(DS.space.xxs)
                    .frame(height: 60)
                    .background(RoundedRectangle(cornerRadius: DS.radius.chip,
                                                 style: .continuous)
                        .fill(.quaternary.opacity(0.5)))
                Button("Add snippet") {
                    let s = PromptSnippet(
                        name: newName.trimmingCharacters(in: .whitespaces),
                        phase: newPhase.trimmingCharacters(in: .whitespaces),
                        text: newText.trimmingCharacters(in: .whitespacesAndNewlines))
                    guard !s.name.isEmpty, !s.text.isEmpty,
                          !snippets.contains(where: { $0.name == s.name }) else { return }
                    snippets.append(s)
                    store.saveSnippets(snippets)
                    newName = ""; newPhase = ""; newText = ""
                }
                .disabled(newName.trimmingCharacters(in: .whitespaces).isEmpty
                          || newText.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
            }
        }
        .onAppear {
            guard !loaded else { return }
            snippets = store.loadSnippets()
            loaded = true
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

// Pure classification: "shows as running" = a NON-STALE lock or a live
// GUI-owned process. A stale (dead/absent-pid) lock is a crashed corpse and
// must never render as Running — that was the standing lie this fixes.
enum AppShellLogic {
    static func showsAsRunning(lockPresent: Bool, lockStale: Bool,
                               guiOwned: Bool) -> Bool {
        (lockPresent && !lockStale) || guiOwned
    }

    static func conductorDestination(available: Bool) -> ShellSelection? {
        available ? .conductor : nil
    }
}

// Banner for a crashed run (dead-pid lock): states what the GUI actually
// knows and offers a one-click resume. Rendered only while a settled
// ResumeOffer exists — parked (autorun-disabled) apps never get an offer.
private struct CrashedRunBanner: View {
    @EnvironmentObject var store: OrchestratorStore
    let offer: ResumeOffer

    var body: some View {
        InlineBanner(kind: .warning,
                     title: "\(offer.name) crashed",
                     message: ResumeAdvisor.bannerText(deadPid: offer.deadPid,
                                                       since: offer.since,
                                                       shepherdActive: store.shepherdActive)) {
            HStack(spacing: DS.space.xs) {
                Button(store.shepherdActive ? "Resume now" : "Resume") {
                    store.resumeCrashedRun(offer.name)
                }
                Button("Clear lock") { store.stopRun(offer.name) }
            }
        }
    }
}

private struct ProjectShellContent: View {
    @EnvironmentObject var store: OrchestratorStore
    let project: Project
    // Transcript-first: clicking a project lands on its phases + discussions
    // (the Run health zones stay one segment away).
    @State private var scopeTab: ProjectScopeTab = .transcript

    private var liveProject: Project {
        store.projects.first { $0.name == project.name } ?? project
    }

    var body: some View {
        let proj = liveProject
        VStack(spacing: 0) {
            if let offer = store.crashedRuns.first(where: { $0.name == proj.name }) {
                CrashedRunBanner(offer: offer)
            }
            HStack {
                Picker("View", selection: $scopeTab) {
                    ForEach(ProjectScopeTab.allCases) { t in
                        Text(t.rawValue).tag(t)
                    }
                }
                .pickerStyle(.segmented)
                .labelsHidden()
                .frame(width: 340)
                if proj.isPrivate {
                    Label("Private", systemImage: "lock.fill")
                        .font(DS.font.caption)
                        .foregroundStyle(DS.accent.color)
                        .help("Engine-enforced local models only")
                        .accessibilityLabel("Private, local models only")
                }
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
            GateCardsRow(project: project)
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
    @State private var tuningExpanded = true

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
        DisclosureGroup(isExpanded: $tuningExpanded) {
            PhaseTuningEditor(project: project, workflow: workflow)
        } label: {
            Label("Phase Rounds & Instructions", systemImage: "slider.horizontal.2.square.on.square")
                .font(DS.font.callout)
        }
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

// Per-phase rounds (∞ = until natural consensus) + operator instructions,
// written to <project>/model_routing.json — the same file the routing grid
// edits; the engine honors "rounds"/"instructions" per phase. A profile
// snapshots this whole setup for reuse on future runs.
private struct PhaseTuningEditor: View {
    @EnvironmentObject var store: OrchestratorStore
    let project: Project
    let workflow: WorkflowDef

    @State private var routing = ModelRouting()
    @State private var snippets: [PromptSnippet] = []
    @State private var loaded = false
    @State private var profileName = ""

    private func binding(_ key: String) -> Binding<PhaseRoute> {
        Binding(get: { routing.phases[key] ?? PhaseRoute() },
                set: { newVal in
                    routing.phases[key] = newVal.isEmpty ? nil : newVal
                    store.writeProjectRouting(routing, for: project)
                })
    }

    var body: some View {
        VStack(alignment: .leading, spacing: DS.space.s) {
            Text("Rounds: how long a phase may debate — Default is the workflow's cap, ∞ runs until every model naturally agrees (no forced vote). Instructions are injected into every turn of that phase for THIS project.")
                .font(DS.font.caption).foregroundStyle(.tertiary)
                .fixedSize(horizontal: false, vertical: true)
            ForEach(workflow.phases, id: \.key) { phase in
                PhaseTuningRow(phase: phase, route: binding(phase.key),
                               snippets: snippets)
            }
            Divider()
            HStack(spacing: DS.space.xs) {
                TextField("Profile name", text: $profileName)
                    .textFieldStyle(.roundedBorder)
                    .frame(width: 200)
                Button("Save as profile") {
                    store.saveProfile(named: profileName, from: project)
                    profileName = ""
                }
                .disabled(profileName.trimmingCharacters(in: .whitespaces).isEmpty)
                Spacer()
            }
            Text("A profile snapshots this workflow + every phase's model, effort, rounds, and instructions — apply it from the New App sheet.")
                .font(DS.font.caption).foregroundStyle(.tertiary)
        }
        .padding(.vertical, DS.space.xxs)
        .onAppear {
            guard !loaded else { return }
            routing = store.readProjectRouting(project)
            snippets = store.loadSnippets()
            loaded = true
        }
    }
}

private struct PhaseTuningRow: View {
    let phase: PhaseDef
    @Binding var route: PhaseRoute
    let snippets: [PromptSnippet]

    @State private var expanded = false

    private var roundsLabel: String {
        guard let r = route.rounds else { return "Default (\(phase.rounds))" }
        return r == 0 ? "∞ until consensus" : "\(r)"
    }

    var body: some View {
        VStack(alignment: .leading, spacing: DS.space.xxs) {
            HStack(spacing: DS.space.xs) {
                Text(phase.title)
                    .font(DS.font.callout)
                    .frame(width: 190, alignment: .leading)
                    .lineLimit(1)
                Menu(roundsLabel) {
                    Button("Workflow default (\(phase.rounds))") { route.rounds = nil }
                    Button("∞ — until natural consensus") { route.rounds = 0 }
                    Divider()
                    ForEach([1, 2, 3, 4, 5, 6, 8, 10, 12, 15, 20], id: \.self) { n in
                        Button("\(n) round\(n == 1 ? "" : "s")") { route.rounds = n }
                    }
                }
                .menuStyle(.borderlessButton)
                .fixedSize()
                .accessibilityLabel("Rounds for \(phase.title): \(roundsLabel)")
                if !route.instructions.isEmpty {
                    Image(systemName: "text.alignleft")
                        .font(DS.font.caption)
                        .foregroundStyle(DS.accent.color)
                        .help("Has custom instructions")
                }
                Spacer()
                Button(expanded ? "Hide instructions" : "Instructions…") {
                    withAnimation(DS.spring) { expanded.toggle() }
                }
                .buttonStyle(.plain)
                .font(DS.font.caption)
                .foregroundStyle(DS.accent.color)
            }
            if expanded {
                VStack(alignment: .leading, spacing: DS.space.xxs) {
                    TextEditor(text: $route.instructions)
                        .font(DS.font.body)
                        .scrollContentBackground(.hidden)
                        .padding(DS.space.xxs)
                        .frame(height: 72)
                        .background(RoundedRectangle(cornerRadius: DS.radius.chip,
                                                     style: .continuous)
                            .fill(.quaternary.opacity(0.5)))
                        .accessibilityLabel("Instructions for \(phase.title)")
                    let usable = snippets.filter { $0.phase.isEmpty || $0.phase == phase.key }
                    if !usable.isEmpty {
                        Menu("Insert snippet") {
                            ForEach(usable) { s in
                                Button(s.name) {
                                    route.instructions += (route.instructions.isEmpty ? "" : "\n") + s.text
                                }
                            }
                        }
                        .menuStyle(.borderlessButton)
                        .font(DS.font.caption)
                        .fixedSize()
                    }
                }
            }
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
                    EmptyStateView(symbol: "bubble.left.and.bubble.right",
                                   title: "Select a phase",
                                   message: "Pick a phase on the left to read its conversation.")
                }
            }
        }
        .onAppear {
            // A palette search hit lands here with a pending anchor: it
            // wins phase selection once, then TranscriptView consumes the
            // anchor for the turn scroll.
            if let anchor = store.pendingTranscriptAnchor,
               anchor.project == proj.name, !anchor.phase.isEmpty {
                selectedPhaseKey = anchor.phase
            } else {
                syncPhaseSelection(proj)
            }
        }
        .onChange(of: store.pendingTranscriptAnchor) { _, anchor in
            if let anchor, anchor.project == proj.name, !anchor.phase.isEmpty {
                selectedPhaseKey = anchor.phase
            }
        }
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


// MARK: - Quality-gate verdict chips (adherence · visual QA · UI crawl ·
// design lint), read from the docs/*.json artifacts each gate persists.

private struct GateVerdict: Identifiable {
    let name: String
    let symbol: String
    let verdict: String     // PASS / FAIL / — 
    let detail: String
    var id: String { name }
    var kind: StatusKind {
        switch verdict {
        case "PASS": return .success
        case "FAIL": return .error
        default: return .idle
        }
    }
}

private struct GateCardsRow: View {
    let project: Project

    private func loadJSON(_ rel: String) -> [String: Any]? {
        let url = project.dirURL.appendingPathComponent(rel)
        guard let data = try? Data(contentsOf: url) else { return nil }
        return (try? JSONSerialization.jsonObject(with: data)) as? [String: Any]
    }

    private var verdicts: [GateVerdict] {
        var out: [GateVerdict] = []
        if let adh = loadJSON("docs/adherence.json") {
            let v = (adh["verdict"] as? String) ?? "—"
            let score = adh["score"].map { "\($0)" } ?? ""
            out.append(GateVerdict(name: "Adherence", symbol: "checklist",
                                   verdict: v,
                                   detail: score.isEmpty ? v : "score \(score)"))
        }
        if let vqa = loadJSON("docs/visual_qa.json") {
            let v = (vqa["verdict"] as? String) ?? "—"
            let score = vqa["score"].map { "\($0)" } ?? ""
            out.append(GateVerdict(name: "Visual QA", symbol: "eye",
                                   verdict: v,
                                   detail: score.isEmpty ? v : "score \(score)"))
        }
        if let crawl = loadJSON("docs/ui_crawl.json") {
            let crashes = (crawl["crashes"] as? [Any])?.count ?? 0
            let dead = (crawl["dead_taps"] as? [Any])?.count ?? 0
            let flows = (crawl["flows"] as? [[String: Any]]) ?? []
            let passed = flows.filter { ($0["passed"] as? Bool) ?? false }.count
            let bad = crashes > 0 || passed < flows.count
            let bits = [
                "\((crawl["screens"] as? Int) ?? 0) screens",
                flows.isEmpty ? nil : "\(passed)/\(flows.count) flows",
                dead > 0 ? "\(dead) dead" : nil,
                crashes > 0 ? "\(crashes) crash" : nil,
            ].compactMap { $0 }
            out.append(GateVerdict(name: "UI Crawl",
                                   symbol: "cursorarrow.click.2",
                                   verdict: bad ? "FAIL" : "PASS",
                                   detail: bits.joined(separator: " · ")))
        }
        if let lint = loadJSON("docs/design_lint.json") {
            let errs = (lint["errors"] as? [Any])?.count ?? 0
            let warns = (lint["warnings"] as? [Any])?.count ?? 0
            out.append(GateVerdict(name: "Design Lint",
                                   symbol: "paintpalette",
                                   verdict: errs > 0 ? "FAIL" : "PASS",
                                   detail: errs > 0 ? "\(errs) error(s)"
                                       : "\(warns) warning(s)"))
        }
        return out
    }

    var body: some View {
        let items = verdicts
        if !items.isEmpty {
            ScrollView(.horizontal, showsIndicators: false) {
                HStack(spacing: DS.space.xs) {
                    ForEach(items, id: \.name) { g in
                        chip(g)
                    }
                }
                .padding(.horizontal, DS.space.s)
                .padding(.vertical, DS.space.xxs)
            }
        }
    }

    private func chip(_ g: GateVerdict) -> some View {
        let tint = g.kind.tint.color
        return HStack(spacing: DS.space.xxs) {
            Image(systemName: g.symbol)
                .foregroundStyle(tint)
            VStack(alignment: .leading, spacing: 0) {
                Text(g.name).font(DS.font.caption)
                    .foregroundStyle(.secondary)
                Text(g.detail).font(DS.font.callout)
                    .foregroundStyle(tint)
            }
        }
        .padding(.horizontal, DS.space.s)
        .padding(.vertical, DS.space.xxs)
        .background(RoundedRectangle(cornerRadius: DS.radius.chip,
                                     style: .continuous)
            .fill(.quaternary.opacity(0.5)))
        .accessibilityLabel("\(g.name): \(g.detail)")
    }
}
