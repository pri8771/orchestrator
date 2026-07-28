import SwiftUI
import AppKit
import UniformTypeIdentifiers

// MARK: - The Routing Grid (DESIGN-NATIVE-PRO.md §5, milestone M3)
//
// One component, three altitudes, one source of truth: glance (timeline
// avatars) → summary (Inspector › Plan Summary) → editor (this grid). The
// same component is embedded per-project on the Plan tab and fleet-wide in
// Settings › Defaults — users learn it once.
//
// Persistence (§5.7): the grid IS model_routing.json. Fleet scope edits the
// engine-dir file (engine-honored today); project scope edits
// <project>/model_routing.json, resolved over the fleet default in the GUI.
// Apply writes atomically; Revert re-reads; a "file changed on disk" banner
// appears when the file moves under a dirty grid.

// MARK: - Scope

enum RoutingScope: Equatable {
    case fleet
    case project(name: String)
    case section(name: String)   // V3 3.8: sections/<name>/routing.json

    var isFleet: Bool { self == .fleet }
    var projectName: String {
        if case .project(let name) = self { return name }
        return ""
    }
    var sectionName: String {
        if case .section(let name) = self { return name }
        return ""
    }
}

// MARK: - RoutingMatrix (phase × agent → model/effort, with provenance)

// The resolved value of one grid cell.
struct GridCell: Equatable {
    var model: String            // effective model id ("" = provider default)
    var effort: EffortLevel?     // effective effort (claude/codex only)
    var overriddenHere: Bool     // an override exists at THIS scope
    var inheritedFrom: String    // "" | the base value's origin, for hover text
    var off: Bool                // the phase's agents filter excludes this agent
    var invalid: Bool            // model fails the roster/installed check
}

// Pure resolution + editing over a draft ModelRouting, layered on a base.
// For .fleet the base is the config.yaml defaults; for .project the base is
// the fleet routing resolved over those same defaults.
struct RoutingMatrix {
    var scope: RoutingScope
    var draft: ModelRouting              // the scope's editable overrides
    var base: ModelRouting               // the fleet routing beneath a project scope
    var defaults: [String: String]       // config.yaml models per agent
    var defaultEfforts: [String: String] // config.yaml efforts per agent
    var enabledAgents: [String: Bool]
    var installedLocal: Set<String>

    static let agents = ["codex", "claude", "gemini", "ollama"]

    // The phase override that applies at this scope, and the base value below it.
    private func scopeRoute(_ phaseKey: String) -> PhaseRoute {
        draft.phases[phaseKey] ?? PhaseRoute()
    }
    private func baseRoute(_ phaseKey: String) -> PhaseRoute {
        scope.isFleet ? PhaseRoute() : (base.phases[phaseKey] ?? PhaseRoute())
    }

    func cell(_ phaseKey: String, _ agent: String) -> GridCell {
        let mine = scopeRoute(phaseKey)
        let below = baseRoute(phaseKey)
        let myModel = mine.model(for: agent)
        let baseModel = below.model(for: agent)
        let effectiveModel = !myModel.isEmpty ? myModel
            : (!baseModel.isEmpty ? baseModel : (defaults[agent] ?? ""))
        let myEffort = mine.effort(for: agent)
        let baseEffort = below.effort(for: agent)
        let effortStr = !myEffort.isEmpty ? myEffort
            : (!baseEffort.isEmpty ? baseEffort : (defaultEfforts[agent] ?? ""))
        var origin = ""
        if myModel.isEmpty && myEffort.isEmpty {
            if !baseModel.isEmpty || !baseEffort.isEmpty { origin = "fleet routing" }
            else { origin = "fleet default" }
        }
        // The scope's filter wins when set; otherwise the base filter applies.
        let filter = !mine.agents.isEmpty ? mine.agents : below.agents
        let off = !Self.filterIncludes(filter, agent: agent)
        let invalid = agent == "ollama" && !effectiveModel.isEmpty
            && !installedLocal.isEmpty && !installedLocal.contains(effectiveModel)
        return GridCell(model: effectiveModel,
                        effort: EffortLevel(configValue: effortStr),
                        overriddenHere: !myModel.isEmpty || !myEffort.isEmpty,
                        inheritedFrom: origin,
                        off: off,
                        invalid: invalid)
    }

    // The engine's participant-filter semantics (modelrouting.filter_agents):
    // empty = everyone; a comma/semicolon list of agent ids, local tags, and
    // the groups "cloud" / "local". Fail-open on an empty match is the
    // engine's job; the grid only renders intent.
    static func filterIncludes(_ filter: String, agent: String) -> Bool {
        let f = filter.trimmingCharacters(in: .whitespaces)
        guard !f.isEmpty else { return true }
        let terms = f.split(whereSeparator: { $0 == "," || $0 == ";" })
            .map { $0.trimmingCharacters(in: .whitespaces).lowercased() }
            .filter { !$0.isEmpty }
        guard !terms.isEmpty else { return true }
        let isLocal = agent == "ollama"
        for t in terms {
            if t == "cloud" && !isLocal { return true }
            if t == "local" && isLocal { return true }
            if t == agent { return true }
            if isLocal && (t == "ollama" || t.hasPrefix("local")) { return true }
        }
        return false
    }

    // MARK: Editing (all mutations funnel here so dirty-tracking is trivial)

    mutating func setModel(_ model: String, phase: String, agent: String) {
        var r = draft.phases[phase] ?? PhaseRoute()
        // Choosing the inherited value back clears the override.
        let below = baseRoute(phase).model(for: agent)
        let inherited = below.isEmpty ? (defaults[agent] ?? "") : below
        r.setModel(model == inherited ? "" : model, for: agent)
        draft.phases[phase] = r.isEmpty ? nil : r
    }

    mutating func setEffort(_ level: EffortLevel?, phase: String, agent: String) {
        guard DS.identity(agent).supportsEffort else { return }
        var r = draft.phases[phase] ?? PhaseRoute()
        r.setEffort(level.map(configValue) ?? "", for: agent)
        draft.phases[phase] = r.isEmpty ? nil : r
    }

    mutating func revertCell(phase: String, agent: String) {
        var r = draft.phases[phase] ?? PhaseRoute()
        r.setModel("", for: agent)
        r.setEffort("", for: agent)
        draft.phases[phase] = r.isEmpty ? nil : r
    }

    // Toggle an agent's participation in one phase by editing the phase's
    // agents filter (the engine's participant filter).
    mutating func setParticipates(_ on: Bool, phase: String, agent: String) {
        var r = draft.phases[phase] ?? PhaseRoute()
        let currentFilter = !r.agents.isEmpty ? r.agents : baseRoute(phase).agents
        var members = Self.agents.filter { Self.filterIncludes(currentFilter, agent: $0) }
        if on {
            if !members.contains(agent) { members.append(agent) }
        } else {
            members.removeAll { $0 == agent }
        }
        // Everyone in → clear the filter entirely (engine default).
        if Set(members) == Set(Self.agents) {
            r.agents = ""
        } else {
            // Keep the engine's preference order.
            r.agents = Self.agents.filter { members.contains($0) }.joined(separator: ",")
        }
        draft.phases[phase] = r.isEmpty ? nil : r
    }

    func overriddenCount(phaseKeys: [String]) -> Int {
        phaseKeys.reduce(0) { n, key in
            n + RoutingMatrix.agents.filter { cell(key, $0).overriddenHere }.count
        }
    }

    private func configValue(_ level: EffortLevel) -> String {
        switch level {
        case .low: return "low"
        case .medium: return "medium"
        case .high: return "high"
        }
    }

    // MARK: Presets (§5.5 — starting points, not modes)

    // Preset semantics from the spec: Max Quality = opus-high everywhere +
    // codex-high build; Economy = sonnet-medium spec, codex-medium build,
    // local-7B reviews, claude-sonnet verify; Balanced = clear (fleet defaults).
    mutating func apply(preset: String, phases: [PhaseDef]) {
        for p in phases {
            var r = draft.phases[p.key] ?? PhaseRoute()
            r.claude = ""; r.codex = ""; r.gemini = ""; r.ollama = ""
            r.claudeReasoning = ""; r.codexReasoning = ""
            switch preset {
            case "max_quality":
                r.claude = "opus"
                r.claudeReasoning = "high"
                if p.writes { r.codexReasoning = "high" }
            case "economy":
                let key = p.key.lowercased()
                if p.writes {
                    r.codexReasoning = "medium"
                } else if key.contains("review") || key.contains("audit") {
                    r.claude = "haiku"
                    r.claudeReasoning = "low"
                    r.codexReasoning = "low"
                } else if key.contains("verif") {
                    r.claude = "sonnet"
                    r.claudeReasoning = "medium"
                } else {
                    r.claude = "sonnet"
                    r.claudeReasoning = "medium"
                    r.codexReasoning = "low"
                }
            default:
                break   // balanced = inherit everything
            }
            draft.phases[p.key] = r.isEmpty ? nil : r
        }
    }
}

// MARK: - Cell identity + clipboard payload

struct GridCellID: Hashable {
    let phaseKey: String
    let agent: String
}

// ⌘C/⌘V payload {model, effort} (§5.4).
struct CellClipboard: Equatable {
    var model: String
    var effort: EffortLevel?
}

// MARK: - RoutingGridView

struct RoutingGridView: View {
    @EnvironmentObject var store: OrchestratorStore
    let scope: RoutingScope
    /// The workflow whose phases are the rows.
    let workflow: WorkflowDef

    @State private var matrix: RoutingMatrix?
    @State private var savedDraft = ModelRouting()   // last applied/loaded state
    @State private var loadedMTime: Date?
    @State private var fileChangedOnDisk = false
    @State private var selection: Set<GridCellID> = []
    @State private var anchor: GridCellID?
    @State private var editingCell: GridCellID?
    @State private var editingRoles: String?    // phaseKey with the roles popover open
    @State private var clipboard: CellClipboard?
    @State private var selectedPreset = "balanced"
    @State private var agentLibrary = AgentLibraryDocument()
    @FocusState private var gridFocused: Bool

    private var phases: [PhaseDef] { workflow.phases }
    private var isDirty: Bool { (matrix?.draft ?? ModelRouting()) != savedDraft }

    private var routingURL: URL {
        switch scope {
        case .fleet: return store.modelRoutingURL
        case .project(let name):
            return store.rootURL.appendingPathComponent(name)
                .appendingPathComponent("model_routing.json")
        case .section(let name):
            // The 3.4 session layer: fleet -> project -> section overlay.
            // MODEL routing lives in model_routing.json (modelrouting.py's
            // ROUTING_FILENAME) — sections/<name>/routing.json is the
            // CONDUCTOR's artifact-routing config (conductor_routing.py,
            // {artifact_routes, rules}); writing a ModelRouting there both
            // hid every section model edit from the engine AND destroyed
            // the seeded conductor rules.
            return store.orchDirURL.appendingPathComponent(
                "sections/\(name)/model_routing.json")
        }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: DS.space.s) {
            topBar
            if fileChangedOnDisk {
                fileChangedBanner
            }
            gridCard
            if let m = matrix {
                ConsequenceStrip(matrix: m, phases: phases,
                                 enabledAgents: store.enabledAgents,
                                 onJump: { select($0) })
            }
        }
        .onAppear(perform: load)
        .onChange(of: scope) { _, _ in load() }
        .onChange(of: store.projects) { _, _ in checkDiskChange() }
    }

    // MARK: Load / apply / revert

    private func load() {
        agentLibrary = AgentLibraryDocument.loadLayered(
            fleetURL: store.orchDirURL.appendingPathComponent("agent_library.json"),
            sectionURL: scope.sectionName.isEmpty ? nil : store.orchDirURL
                .appendingPathComponent("sections/\(scope.sectionName)/agent_library.json"))
        let draft = ModelRouting.load(from: routingURL)
        var m = RoutingMatrix(
            scope: scope,
            draft: draft,
            base: scope.isFleet ? ModelRouting() : store.readModelRouting(),
            defaults: store.agentModels,
            defaultEfforts: store.agentEfforts,
            enabledAgents: store.enabledAgents,
            installedLocal: Set((store.localModels?.registry ?? [])
                .filter(\.installed).map(\.id)))
        // Seed a fresh project grid from the intake's preset marker (M2's
        // forward-compatible routing_preset.txt).
        if case .project(let name) = scope, draft.phases.isEmpty,
           let proj = store.projects.first(where: { $0.name == name }),
           let preset = store.readRoutingPresetMarker(proj) {
            m.apply(preset: preset, phases: phases)
            selectedPreset = preset
        }
        matrix = m
        savedDraft = draft
        loadedMTime = store.routingFileMTime(at: routingURL)
        fileChangedOnDisk = false
    }

    private func applyChanges() {
        // `let`: save(to:) is nonmutating; the compiler flags `var` here.
        guard let m = matrix else { return }
        m.draft.save(to: routingURL)
        // The store's TTL cache pairs every save with an invalidation
        // (writeModelRouting); a direct save must do the same or readers
        // get pre-Apply routing for up to 2s — and a concurrent
        // reload-before-mutate writer can revert this Apply.
        store.invalidateRoutingCache(at: routingURL)
        matrix = m
        savedDraft = m.draft
        loadedMTime = store.routingFileMTime(at: routingURL)
        fileChangedOnDisk = false
        if case .project(let name) = scope,
           let proj = store.projects.first(where: { $0.name == name }) {
            store.clearRoutingPresetMarker(proj)   // the file is authoritative now
        }
        store.objectWillChange.send()
    }

    private func checkDiskChange() {
        let now = store.routingFileMTime(at: routingURL)
        guard now != loadedMTime else { return }
        if isDirty {
            // The engine or another editor touched the JSON under a dirty
            // grid — surface it, never clobber silently (§5.7).
            fileChangedOnDisk = true
        } else {
            load()   // clean grid: follow the file (e.g. Inspector cell edits)
        }
    }

    // MARK: Top bar (52pt): workflow · preset chips · unsaved pill

    private var topBar: some View {
        HStack(spacing: DS.space.xs) {
            presetChips
            Spacer()
            if isDirty {
                HStack(spacing: DS.space.xxs) {
                    Text("Edited").font(DS.font.caption).foregroundStyle(.secondary)
                    Button("Revert") { load() }
                        .font(DS.font.caption)
                    Button("Apply") { applyChanges() }
                        .font(DS.font.caption)
                        .buttonStyle(.borderedProminent)
                        .controlSize(.small)
                }
                .padding(.horizontal, DS.space.xs)
                .padding(.vertical, 3)
                .background(Capsule().fill(DS.accent.fill))
                .overlay(Capsule().stroke(DS.accent.stroke, lineWidth: 1))
            } else {
                Text(scope.isFleet
                     ? "Fleet defaults — applies to every project without its own plan"
                     : "Changes apply from the next phase boundary")
                    .font(DS.font.caption).foregroundStyle(.tertiary)
            }
        }
        .frame(minHeight: 28)
    }

    private static let presets: [(key: String, label: String)] = [
        ("balanced", "Balanced"), ("max_quality", "Max Quality"), ("economy", "Economy"),
    ]

    private var presetChips: some View {
        HStack(spacing: DS.space.xs) {
            ForEach(Self.presets, id: \.key) { p in
                Chip(label: p.label,
                     selected: selectedPreset == p.key && !draftDiverged(from: p.key),
                     action: {
                        withAnimation(DS.spring) {
                            selectedPreset = p.key
                            matrix?.apply(preset: p.key, phases: phases)
                        }
                     })
            }
            if draftDiverged(from: selectedPreset) {
                Chip(label: "Custom", selected: true)
            }
        }
    }

    // Touching any cell flips to Custom and never silently discards edits.
    private func draftDiverged(from preset: String) -> Bool {
        guard var probe = matrix else { return false }
        let current = probe.draft
        probe.apply(preset: preset, phases: phases)
        return probe.draft != current
    }

    private var fileChangedBanner: some View {
        HStack(spacing: DS.space.xs) {
            Image(systemName: "exclamationmark.triangle.fill")
                .foregroundStyle(DS.status.warning.color)
            Text("model_routing.json changed on disk while you were editing.")
                .font(DS.font.caption)
            Spacer()
            Button("Reload file") { load() }.font(DS.font.caption)
            Button("Keep my edits") { fileChangedOnDisk = false }.font(DS.font.caption)
        }
        .padding(DS.space.xs)
        .background(RoundedRectangle(cornerRadius: DS.radius.chip, style: .continuous)
            .fill(DS.status.warning.fill))
        .overlay(RoundedRectangle(cornerRadius: DS.radius.chip, style: .continuous)
            .stroke(DS.status.warning.stroke, lineWidth: 1))
    }

    // MARK: Grid geometry (§5.1): frozen 140pt phase gutter + horizontal
    // scroll past the agent columns. Implemented as one horizontal ScrollView
    // whose leading gutter is pinned by rendering it OUTSIDE the scroll view,
    // with row heights fixed so the two columns stay aligned.

    private static let rowHeight: CGFloat = 76
    private static let gutterWidth: CGFloat = 236
    private static let columnWidth: CGFloat = 132

    private var gridCard: some View {
        HStack(alignment: .top, spacing: 0) {
            phaseGutter
            Divider()
            ScrollView(.horizontal, showsIndicators: true) {
                VStack(spacing: 0) {
                    agentHeaderRow
                    Divider()
                    ForEach(phases) { phase in
                        gridRow(phase)
                        Divider()
                    }
                }
            }
        }
        .background(RoundedRectangle(cornerRadius: DS.radius.card, style: .continuous)
            .fill(DS.cardBg))
        .overlay(RoundedRectangle(cornerRadius: DS.radius.card, style: .continuous)
            .stroke(DS.hairline, lineWidth: 1))
        .clipShape(RoundedRectangle(cornerRadius: DS.radius.card, style: .continuous))
        .focusable()
        .focused($gridFocused)
        .focusEffectDisabled()
        .onKeyPress(phases: .down) { handleKey($0) }
        .onCopyCommand {
            copySelection()
            guard let clip = clipboard else { return [] }
            let text = clip.effort.map { "\(clip.model) · \($0.label.lowercased())" } ?? clip.model
            return [NSItemProvider(object: text as NSString)]
        }
        .onPasteCommand(of: [UTType.plainText]) { _ in pasteClipboardToAll() }
        .accessibilityLabel("Routing grid: \(workflow.title), \(phases.count) phases by \(RoutingMatrix.agents.count) agents")
        // §2.3 documented opt-out: cell geometry (fixed column widths, dense
        // row heights) is load-bearing, so this subtree stays pinned to the
        // system default text size regardless of the user's Dynamic Type
        // setting. Compensated by the accessibilityLabel above plus the
        // per-cell labels inside (AssignmentChip, agentHeaderRow).
        .dynamicTypeSize(.large)
    }

    private var phaseGutter: some View {
        VStack(spacing: 0) {
            Text("Phase")
                .font(DS.font.caption).foregroundStyle(.secondary)
                .frame(width: Self.gutterWidth, height: 40, alignment: .leading)
                .padding(.leading, DS.space.s)
            Divider()
            ForEach(phases) { phase in
                phaseGutterRow(phase)
                Divider()
            }
        }
        .frame(width: Self.gutterWidth + DS.space.s)
    }

    private func phaseGutterRow(_ phase: PhaseDef) -> some View {
        VStack(alignment: .leading, spacing: 2) {
            Text(phase.title)
                .font(DS.font.callout.weight(.semibold))
                .lineLimit(1)
                .truncationMode(.tail)
            HStack(spacing: DS.space.xxs) {
                if phase.writes {
                    Image(systemName: "hammer.fill")
                        .font(DS.font.caption)
                        .foregroundStyle(DS.textTertiary)
                        .help("Build phase — writes files")
                        .accessibilityLabel("build phase")
                }
                RoundsStepper(workflowName: workflow.name, phase: phase)
            }
            phaseRosterControls(phase)
            if let hint = recommendedHint(for: phase) {
                Text("recommended: \(hint)")
                    .font(DS.font.caption2)
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
                    .accessibilityIdentifier("roster-hint-\(phase.key)")
            }
        }
        .padding(.leading, DS.space.s)
        .frame(width: Self.gutterWidth + DS.space.s, height: Self.rowHeight, alignment: .leading)
        .contextMenu {
            Button("Copy row") { copyRow(phase.key) }
            Button("Paste row") { pasteRow(phase.key) }
                .disabled(rowClipboard == nil)
            Button("Apply row to all phases below") { applyRowBelow(phase.key) }
            Divider()
            Button("Role overrides…") { editingRoles = phase.key }
        }
        .popover(isPresented: Binding(
            get: { editingRoles == phase.key },
            set: { if !$0 { editingRoles = nil } }
        ), arrowEdge: .trailing) {
            RolePopover(phaseTitle: phase.title,
                        roles: rolesBinding(phase.key),
                        modelOptions: modelOptions)
                .environmentObject(store)
        }
    }

    private func recommendedHint(for phase: PhaseDef) -> String? {
        agentLibrary.recommendedHint(forPhase: phase.key)
    }

    private func phaseRosterControls(_ phase: PhaseDef) -> some View {
        HStack(spacing: DS.space.xxs) {
            Stepper(value: Binding(
                get: { matrix?.draft.phases[phase.key]?.castSize ?? activeRosterCount(phase) },
                set: { value in
                    edit { m in
                        var route = m.draft.phases[phase.key] ?? PhaseRoute()
                        route.castSize = max(1, value)
                        m.draft.phases[phase.key] = route
                    }
                }), in: 1...12) {
                    Text("\(matrix?.draft.phases[phase.key]?.castSize ?? activeRosterCount(phase))")
                        .font(DS.font.caption)
                        .monospacedDigit()
                }
                .labelsHidden()
                .help("Roster size. Recommendations are informational only.")
                .accessibilityLabel("\(phase.title) roster size")
            Picker("Composition", selection: Binding(
                get: { matrix?.draft.phases[phase.key]?.composition ?? "" },
                set: { value in
                    edit { m in
                        var route = m.draft.phases[phase.key] ?? PhaseRoute()
                        route.composition = value
                        m.draft.phases[phase.key] = route.isEmpty ? nil : route
                    }
                })) {
                    Text("All").tag("")
                    Text("Cloud").tag("cloud")
                    Text("Local").tag("local")
                    Text("Claude + Codex").tag("claude,codex")
                }
                .labelsHidden()
                .pickerStyle(.menu)
                .font(DS.font.caption)
                .accessibilityIdentifier("composition-picker-\(phase.key)")
        }
    }

    private func activeRosterCount(_ phase: PhaseDef) -> Int {
        max(1, RoutingMatrix.agents.filter {
            store.enabledAgents[$0] ?? false
                && !(matrix?.cell(phase.key, $0).off ?? false)
        }.count)
    }

    // Round-trips PhaseRoute.roles through the draft matrix — nil collapses
    // back to "no roles key" the same way isEmpty does for the top-level cell.
    private func rolesBinding(_ phaseKey: String) -> Binding<RoleOverrides> {
        Binding(
            get: { matrix?.draft.phases[phaseKey]?.roles ?? RoleOverrides() },
            set: { newValue in
                edit { m in
                    var r = m.draft.phases[phaseKey] ?? PhaseRoute()
                    r.roles = newValue.isEmpty ? nil : newValue
                    m.draft.phases[phaseKey] = r.isEmpty ? nil : r
                }
            }
        )
    }

    private var agentHeaderRow: some View {
        HStack(spacing: 0) {
            ForEach(RoutingMatrix.agents, id: \.self) { agent in
                agentHeader(agent)
                    .frame(width: Self.columnWidth, height: 40)
            }
        }
    }

    private func agentHeader(_ agent: String) -> some View {
        let id = DS.identity(agent)
        let enabled = store.enabledAgents[agent] ?? false
        let chain = (scope.isFleet ? matrix?.draft : matrix?.base)?.chains[agent] ?? []
        return HStack(spacing: DS.space.xxs) {
            AgentAvatar(identity: id, size: 20)
            Text(id.displayName).font(DS.font.callout)
            if !chain.isEmpty {
                Label("\(chain.count)", systemImage: "link")
                    .font(DS.font.caption)
                    .foregroundStyle(DS.textTertiary)
                    .labelStyle(.titleAndIcon)
                    .help("Fallback ladder: \(chain.joined(separator: " → "))")
                    .accessibilityLabel("Fallback ladder, \(chain.count) rungs")
            }
            Toggle("", isOn: Binding(
                get: { enabled },
                set: { store.setAgentEnabled(agent, $0) }
            ))
            .toggleStyle(.switch)
            .controlSize(.mini)
            .labelsHidden()
            .accessibilityLabel("\(id.displayName) enabled")
        }
        .opacity(enabled ? 1 : 0.3)
        .contextMenu {
            Button("Copy column") { copyColumn(agent) }
            Button("Paste column") { pasteColumn(agent) }
                .disabled(columnClipboard == nil)
            Button("Reset column to defaults") { resetColumn(agent) }
        }
    }

    private func gridRow(_ phase: PhaseDef) -> some View {
        HStack(spacing: 0) {
            ForEach(RoutingMatrix.agents, id: \.self) { agent in
                let id = GridCellID(phaseKey: phase.key, agent: agent)
                cellView(id)
                    .frame(width: Self.columnWidth, height: Self.rowHeight)
            }
        }
    }

    @ViewBuilder
    private func cellView(_ id: GridCellID) -> some View {
        if let m = matrix {
            let cell = m.cell(id.phaseKey, id.agent)
            let columnEnabled = store.enabledAgents[id.agent] ?? false
            AssignmentChip(cell: cell,
                           identity: DS.identity(id.agent),
                           selected: selection.contains(id),
                           enabled: columnEnabled)
                .opacity(columnEnabled ? 1 : 0.3)
                .onTapGesture(count: 2) { select(id); editingCell = id }
                .onTapGesture { handleClick(id) }
                .popover(isPresented: Binding(
                    get: { editingCell == id },
                    set: { if !$0 { editingCell = nil } }
                ), arrowEdge: .bottom) {
                    CellEditorPopover(
                        cell: cell,
                        cellID: id,
                        modelOptions: modelOptions(id.agent),
                        localInstalled: m.installedLocal,
                        fallbackChain: (scope.isFleet ? m.draft : m.base).chains[id.agent] ?? [],
                        onSetModel: { model in
                            edit { $0.setModel(model, phase: id.phaseKey, agent: id.agent) }
                        },
                        onSetEffort: { level in
                            applyToSelection(anchorFirst: id) {
                                $0.setEffort(level, phase: $1.phaseKey, agent: $1.agent)
                            }
                        },
                        onRemove: { edit { $0.setParticipates(false, phase: id.phaseKey, agent: id.agent) } },
                        onRestore: { edit { $0.setParticipates(true, phase: id.phaseKey, agent: id.agent) } },
                        onRevert: { edit { $0.revertCell(phase: id.phaseKey, agent: id.agent) } })
                    .environmentObject(store)
                }
                .contextMenu {
                    Button("Edit…") { select(id); editingCell = id }
                    Button("Copy cell") { select(id); copySelection() }
                    Button("Paste") { pasteClipboard(to: id) }
                        .disabled(clipboard == nil)
                    Divider()
                    if cell.overriddenHere {
                        Button("Revert to inherited") {
                            edit { $0.revertCell(phase: id.phaseKey, agent: id.agent) }
                        }
                    }
                    if cell.off {
                        Button("Add to phase") {
                            edit { $0.setParticipates(true, phase: id.phaseKey, agent: id.agent) }
                        }
                    } else {
                        Button("Remove from phase") {
                            edit { $0.setParticipates(false, phase: id.phaseKey, agent: id.agent) }
                        }
                    }
                }
        }
    }

    // Model options for the picker, grouped: provider presets + installed local.
    private func modelOptions(_ agent: String) -> [String] {
        agent == "ollama"
            ? (store.localModels?.registry ?? []).filter(\.installed).map(\.id)
            : store.modelPresets(agent)
    }

    // MARK: Selection + keyboard (§5.4 spreadsheet ergonomics)

    private func select(_ id: GridCellID) {
        selection = [id]
        anchor = id
        gridFocused = true
        store.planCellSelection = .init(project: scope.projectName,
                                        phaseKey: id.phaseKey, agent: id.agent)
    }

    private func handleClick(_ id: GridCellID) {
        if NSEvent.modifierFlags.contains(.shift), let a = anchor {
            selection = rectangle(from: a, to: id)
            store.planCellSelection = .init(project: scope.projectName,
                                            phaseKey: id.phaseKey, agent: id.agent)
            gridFocused = true
        } else if NSEvent.modifierFlags.contains(.command) {
            if selection.contains(id) { selection.remove(id) } else { selection.insert(id) }
            anchor = id
            gridFocused = true
        } else {
            select(id)
        }
    }

    private func rectangle(from a: GridCellID, to b: GridCellID) -> Set<GridCellID> {
        let keys = phases.map(\.key)
        guard let r1 = keys.firstIndex(of: a.phaseKey),
              let r2 = keys.firstIndex(of: b.phaseKey),
              let c1 = RoutingMatrix.agents.firstIndex(of: a.agent),
              let c2 = RoutingMatrix.agents.firstIndex(of: b.agent) else { return [b] }
        var out = Set<GridCellID>()
        for r in min(r1, r2)...max(r1, r2) {
            for c in min(c1, c2)...max(c1, c2) {
                out.insert(GridCellID(phaseKey: keys[r], agent: RoutingMatrix.agents[c]))
            }
        }
        return out
    }

    private func handleKey(_ press: KeyPress) -> KeyPress.Result {
        let keys = phases.map(\.key)
        let agents = RoutingMatrix.agents

        func moved(_ dr: Int, _ dc: Int) -> GridCellID? {
            guard let a = anchor,
                  let r = keys.firstIndex(of: a.phaseKey),
                  let c = agents.firstIndex(of: a.agent) else {
                return GridCellID(phaseKey: keys.first ?? "", agent: agents[0])
            }
            let nr = min(max(r + dr, 0), keys.count - 1)
            let nc = min(max(c + dc, 0), agents.count - 1)
            return GridCellID(phaseKey: keys[nr], agent: agents[nc])
        }

        switch press.key {
        case .upArrow, .downArrow, .leftArrow, .rightArrow:
            let (dr, dc): (Int, Int) = {
                switch press.key {
                case .upArrow: return (-1, 0)
                case .downArrow: return (1, 0)
                case .leftArrow: return (0, -1)
                default: return (0, 1)
                }
            }()
            guard let next = moved(dr, dc) else { return .ignored }
            if press.modifiers.contains(.shift), let a = anchor {
                // ⇧-arrows extend the selection from the anchor (§5.4).
                selection = rectangle(from: a, to: next)
                store.planCellSelection = .init(project: scope.projectName,
                                                phaseKey: next.phaseKey, agent: next.agent)
            } else {
                select(next)
            }
            return .handled
        case .tab:
            guard let next = moved(0, press.modifiers.contains(.shift) ? -1 : 1)
            else { return .ignored }
            select(next)
            return .handled
        case .space, .return:
            if let a = anchor { editingCell = a }
            return .handled
        case .escape:
            selection = []
            anchor = nil
            editingCell = nil
            store.planCellSelection = nil
            return .handled
        default:
            break
        }
        // ⌘1/2/3 set effort directly (§5.4).
        if press.modifiers.contains(.command),
           let level = ["1": EffortLevel.low, "2": .medium, "3": .high][String(press.characters)] {
            applyToSelection { $0.setEffort(level, phase: $1.phaseKey, agent: $1.agent) }
            return .handled
        }
        return .ignored
    }

    // Any edit applies to ALL selected cells ("set 4 cells to medium").
    private func applyToSelection(anchorFirst: GridCellID? = nil,
                                  _ edit: (inout RoutingMatrix, GridCellID) -> Void) {
        guard var m = matrix else { return }
        var targets = selection
        if let a = anchorFirst { targets.insert(a) }
        if targets.isEmpty, let a = anchor { targets = [a] }
        for id in targets { edit(&m, id) }
        matrix = m
    }

    // One-shot draft mutation with the state round-trip handled.
    private func edit(_ body: (inout RoutingMatrix) -> Void) {
        guard var m = matrix else { return }
        body(&m)
        matrix = m
    }

    // MARK: Copy / paste (cells, rows, columns — payload {model, effort})

    @State private var rowClipboard: [String: CellClipboard]?
    @State private var columnClipboard: [String: CellClipboard]?

    private func copySelection() {
        guard let m = matrix, let a = anchor ?? selection.first else { return }
        let c = m.cell(a.phaseKey, a.agent)
        clipboard = CellClipboard(model: c.model, effort: c.effort)
    }

    private func pasteClipboard(to id: GridCellID) {
        guard let clip = clipboard, var m = matrix else { return }
        m.setModel(clip.model, phase: id.phaseKey, agent: id.agent)
        m.setEffort(clip.effort, phase: id.phaseKey, agent: id.agent)
        matrix = m
    }

    private func pasteClipboardToAll() {
        guard let clip = clipboard else { return }
        applyToSelection { m, id in
            m.setModel(clip.model, phase: id.phaseKey, agent: id.agent)
            m.setEffort(clip.effort, phase: id.phaseKey, agent: id.agent)
        }
    }

    private func copyRow(_ phaseKey: String) {
        guard let m = matrix else { return }
        var out: [String: CellClipboard] = [:]
        for agent in RoutingMatrix.agents {
            let c = m.cell(phaseKey, agent)
            out[agent] = CellClipboard(model: c.model, effort: c.effort)
        }
        rowClipboard = out
    }

    private func pasteRow(_ phaseKey: String) {
        guard let row = rowClipboard, var m = matrix else { return }
        for (agent, clip) in row {
            m.setModel(clip.model, phase: phaseKey, agent: agent)
            m.setEffort(clip.effort, phase: phaseKey, agent: agent)
        }
        matrix = m
    }

    private func applyRowBelow(_ phaseKey: String) {
        guard let m0 = matrix else { return }
        var source: [String: CellClipboard] = [:]
        for agent in RoutingMatrix.agents {
            let c = m0.cell(phaseKey, agent)
            source[agent] = CellClipboard(model: c.model, effort: c.effort)
        }
        guard let idx = phases.firstIndex(where: { $0.key == phaseKey }) else { return }
        var m = m0
        for p in phases[(idx + 1)...] {
            for (agent, clip) in source {
                m.setModel(clip.model, phase: p.key, agent: agent)
                m.setEffort(clip.effort, phase: p.key, agent: agent)
            }
        }
        matrix = m
    }

    private func copyColumn(_ agent: String) {
        guard let m = matrix else { return }
        var out: [String: CellClipboard] = [:]
        for p in phases {
            let c = m.cell(p.key, agent)
            out[p.key] = CellClipboard(model: c.model, effort: c.effort)
        }
        columnClipboard = out
    }

    private func pasteColumn(_ agent: String) {
        guard let col = columnClipboard, var m = matrix else { return }
        for (phaseKey, clip) in col where phases.contains(where: { $0.key == phaseKey }) {
            m.setModel(clip.model, phase: phaseKey, agent: agent)
            m.setEffort(clip.effort, phase: phaseKey, agent: agent)
        }
        matrix = m
    }

    private func resetColumn(_ agent: String) {
        guard var m = matrix else { return }
        for p in phases { m.revertCell(phase: p.key, agent: agent) }
        matrix = m
    }
}

// MARK: - Rounds stepper (row gutter): "× 2" with ± on hover — writes the
// workflow file, the same field RoundsEditor edits.

private struct RoundsStepper: View {
    @EnvironmentObject var store: OrchestratorStore
    let workflowName: String
    let phase: PhaseDef
    @State private var hovering = false

    var body: some View {
        HStack(spacing: 2) {
            Text(phase.rounds == 0 ? "× ∞" : "× \(phase.rounds)")
                .font(DS.font.caption).monospacedDigit()
                .foregroundStyle(.secondary)
            if hovering {
                Stepper("", value: Binding(
                    get: { max(0, phase.rounds) },
                    set: { store.setPhaseRounds(workflow: workflowName,
                                                phaseKey: phase.key, rounds: $0) }
                ), in: 0...120)
                .labelsHidden()
                .controlSize(.mini)
            }
        }
        .frame(height: 16)
        .onHover { hovering = $0 }
        .help("Rounds for \(phase.title) — edits the workflow (shared by every project on it)")
        .accessibilityLabel("\(phase.title): \(phase.rounds == 0 ? "unlimited" : String(phase.rounds)) rounds")
    }
}

// MARK: - AssignmentChip (§5.2): 124×44, 8pt radius, agent tint 8/12% fill +
// 25% stroke. Line 1 model short-name; line 2 EffortGauge. Provenance states.

struct AssignmentChip: View {
    let cell: GridCell
    let identity: DS.AgentIdentity
    var selected: Bool = false
    var enabled: Bool = true

    var body: some View {
        Group {
            if cell.off {
                offBody
            } else {
                normalBody
            }
        }
        .frame(width: 124, height: 44)
        .overlay(
            RoundedRectangle(cornerRadius: DS.radius.chip, style: .continuous)
                .stroke(DS.accent.color, lineWidth: selected ? 2 : 0)
                .padding(selected ? 1 : 0))
        .contentShape(Rectangle())
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(a11y)
    }

    private var a11y: String {
        if cell.off { return "\(identity.displayName): skips this phase" }
        var parts = ["\(identity.displayName): \(cell.model.isEmpty ? "provider default" : cell.model)"]
        if let e = cell.effort { parts.append("\(e.label) effort") }
        parts.append(cell.overriddenHere ? "overridden" : "inherited")
        if cell.invalid { parts.append("model not found — will use fallback chain") }
        return parts.joined(separator: ", ")
    }

    private var normalBody: some View {
        VStack(alignment: .leading, spacing: 3) {
            HStack(spacing: DS.space.xxs) {
                Text(shortName(cell.model))
                    .font(DS.font.callout)
                    .foregroundStyle(cell.model.isEmpty ? DS.textSecondary : DS.textPrimary)
                    .lineLimit(1)
                    .truncationMode(.middle)
                Spacer(minLength: 0)
                if cell.invalid {
                    Image(systemName: "exclamationmark.triangle.fill")
                        .font(DS.font.caption)
                        .foregroundStyle(DS.status.warning.color)
                        .help("Model not found — the run would start on the fallback chain")
                }
            }
            if identity.supportsEffort {
                EffortGauge(level: cell.effort, tint: identity.tint)
            } else {
                Text("no effort control")
                    .font(DS.font.caption)
                    .foregroundStyle(DS.textTertiary)
            }
        }
        .padding(.horizontal, DS.space.xs)
        .padding(.vertical, DS.space.xxs)
        .frame(width: 124, height: 44, alignment: .leading)
        .background(
            RoundedRectangle(cornerRadius: DS.radius.chip, style: .continuous)
                .fill(identity.tint.fill))
        .overlay(
            RoundedRectangle(cornerRadius: DS.radius.chip, style: .continuous)
                .stroke(cell.invalid
                        ? AnyShapeStyle(DS.status.warning.color)
                        : AnyShapeStyle(identity.tint.stroke),
                        style: StrokeStyle(lineWidth: cell.overriddenHere ? 1.5 : 1,
                                           dash: cell.invalid ? [4, 3] : [])))
        // Overridden-here: 5pt accent dot top-right (§5.2 provenance).
        .overlay(alignment: .topTrailing) {
            if cell.overriddenHere {
                Circle().fill(DS.accent.color).frame(width: 5, height: 5)
                    .padding(4)
                    .help("Overrides \(cell.inheritedFrom.isEmpty ? "default" : cell.inheritedFrom)")
            }
        }
        .opacity(enabled ? 1 : 0.3)
    }

    private var offBody: some View {
        HStack {
            Spacer()
            Image(systemName: "plus")
                .font(DS.font.caption)
                .foregroundStyle(DS.textTertiary)
            Spacer()
        }
        .frame(width: 124, height: 44)
        .overlay(
            RoundedRectangle(cornerRadius: DS.radius.chip, style: .continuous)
                .stroke(DS.hairline, style: StrokeStyle(lineWidth: 1, dash: [4, 3])))
        .help("\(identity.displayName) skips this phase — click to add")
    }

    private func shortName(_ id: String) -> String {
        if id.isEmpty { return "default" }
        // Full IDs belong to the picker (11pt mono); the chip shows a short name.
        let tail = id.split(separator: "/").last.map(String.init) ?? id
        return tail.count > 18 ? String(tail.prefix(16)) + "…" : tail
    }
}

// MARK: - Cell editor popover (§5.3, 280pt): searchable model picker with
// relative-cost glyphs, the shared EffortPicker, remove-from-phase, and a
// read-only fallback preview.

private struct CellEditorPopover: View {
    @EnvironmentObject var store: OrchestratorStore
    let cell: GridCell
    let cellID: GridCellID
    let modelOptions: [String]
    let localInstalled: Set<String>
    let fallbackChain: [String]
    let onSetModel: (String) -> Void
    let onSetEffort: (EffortLevel?) -> Void
    let onRemove: () -> Void
    let onRestore: () -> Void
    let onRevert: () -> Void

    @State private var query = ""

    private var identity: DS.AgentIdentity { DS.identity(cellID.agent) }
    private var filteredModels: [String] {
        let q = query.trimmingCharacters(in: .whitespaces).lowercased()
        guard !q.isEmpty else { return modelOptions }
        return modelOptions.filter { $0.lowercased().contains(q) }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: DS.space.s) {
            HStack(spacing: DS.space.xs) {
                AgentAvatar(identity: identity, size: 20)
                Text("\(identity.displayName) · \(prettyPhase)")
                    .font(DS.font.callout)
                Spacer()
                if cell.overriddenHere {
                    Button("Revert") { onRevert() }
                        .font(DS.font.caption)
                        .help("Back to \(cell.inheritedFrom.isEmpty ? "the default" : cell.inheritedFrom)")
                }
            }

            if cell.off {
                Text("\(identity.displayName) skips this phase.")
                    .font(DS.font.caption).foregroundStyle(.secondary)
                Button("Add to phase") { onRestore() }
                    .buttonStyle(.borderedProminent)
                    .controlSize(.small)
            } else {
                TextField("Filter models", text: $query)
                    .textFieldStyle(.roundedBorder)
                    .font(DS.font.caption)
                    .accessibilityLabel("Filter models")

                ScrollView {
                    VStack(alignment: .leading, spacing: 2) {
                        modelRow("", label: "Inherit (\(cell.inheritedFrom.isEmpty ? "default" : cell.inheritedFrom))")
                        ForEach(filteredModels, id: \.self) { m in
                            modelRow(m, label: m)
                        }
                    }
                }
                .frame(maxHeight: 160)

                if identity.supportsEffort {
                    EffortPicker(level: Binding(
                        get: { cell.effort },
                        set: { onSetEffort($0) }
                    ), agent: cellID.agent)
                } else {
                    EffortPicker(level: .constant(nil), agent: cellID.agent)
                }

                if !fallbackChain.isEmpty {
                    Text("Rescue: \(fallbackChain.joined(separator: " → "))")
                        .font(DS.font.caption)
                        .foregroundStyle(DS.status.fallback.color)
                        .lineLimit(2)
                        .help("Edit ladders in Library › Models & Agents")
                }

                Button("Remove from phase", role: .destructive) { onRemove() }
                    .buttonStyle(.plain)
                    .font(DS.font.caption)
                    .foregroundStyle(DS.status.error.color)
            }
        }
        .padding(DS.space.s)
        .frame(width: 280)
    }

    private var prettyPhase: String {
        cellID.phaseKey.replacingOccurrences(of: "_", with: " ").capitalized
    }

    private func modelRow(_ value: String, label: String) -> some View {
        let selected = value == cell.model || (value.isEmpty && !cell.overriddenHere)
        return Button {
            onSetModel(value)
        } label: {
            HStack(spacing: DS.space.xs) {
                Image(systemName: selected ? "checkmark" : "circle")
                    .font(DS.font.caption)
                    .foregroundStyle(selected ? DS.accent.color : DS.textTertiary)
                    .frame(width: 12)
                Text(label)
                    .font(value.isEmpty ? DS.font.caption : DS.font.monoInline)
                    .lineLimit(1)
                    .truncationMode(.middle)
                Spacer()
                if !value.isEmpty {
                    if isSmallLocal(value) {
                        Image(systemName: "exclamationmark.circle")
                            .font(DS.font.caption)
                            .foregroundStyle(DS.status.warning.color)
                            .help("Models under 7B produce low-quality reviews")
                    }
                    Text(costGlyph(value))
                        .font(DS.font.caption)
                        .foregroundStyle(DS.textTertiary)
                        .help("Relative cost by tier — call counts, not dollars")
                }
            }
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .padding(.vertical, 2)
        .accessibilityLabel("\(label)\(selected ? ", selected" : "")")
    }

    // Honest relative-cost intuition, never fake economics (§5.3): local = $,
    // small/fast tiers = $, frontier = $$$, everything else $$.
    private func costGlyph(_ id: String) -> String {
        let m = id.lowercased()
        if cellID.agent == "ollama" || m.hasPrefix("local") { return "$" }
        if ["mini", "flash", "haiku", "spark", "lite", "nano"].contains(where: m.contains) {
            return "$"
        }
        if ["opus", "pro", "max", "-high"].contains(where: m.contains) { return "$$$" }
        return "$$"
    }

    private func isSmallLocal(_ id: String) -> Bool {
        guard cellID.agent == "ollama" else { return false }
        return RoutingConsequences.parameterBillions(id).map { $0 < 7 } ?? false
    }
}

// MARK: - Role overrides popover (worker/integrator split within one phase)
//
// model_routing.json phases.<key>.roles.{worker,integrator} — patches every
// parallel build lane vs. only the integrator's turn (_apply_role_routing in
// orchestrator.py). Two disclosure sections, same model/effort picker
// vocabulary as the top-level phase editor (PhaseRouteRow, ModelLibrary.swift).

private struct RolePopover: View {
    @EnvironmentObject var store: OrchestratorStore
    let phaseTitle: String
    @Binding var roles: RoleOverrides
    let modelOptions: (String) -> [String]

    var body: some View {
        VStack(alignment: .leading, spacing: DS.space.s) {
            Text("Role overrides · \(phaseTitle)").font(DS.font.callout)
            Text("Patch worker lanes and the integrator's turn separately — the cheap-workers/expensive-integrator split.")
                .font(DS.font.caption).foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
            DisclosureGroup("Worker overrides") {
                roleFieldsEditor($roles.worker)
            }
            .font(DS.font.callout)
            DisclosureGroup("Integrator overrides") {
                roleFieldsEditor($roles.integrator)
            }
            .font(DS.font.callout)
        }
        .padding(DS.space.s)
        .frame(width: 300)
    }

    @ViewBuilder
    private func roleFieldsEditor(_ fields: Binding<RoleFields>) -> some View {
        VStack(alignment: .leading, spacing: DS.space.xs) {
            HStack(spacing: DS.space.xs) {
                labeledPicker("Claude", selection: fields.claude, options: modelOptions("claude"))
                labeledPicker("Codex", selection: fields.codex, options: modelOptions("codex"))
            }
            HStack(spacing: DS.space.xs) {
                labeledPicker("Gemini", selection: fields.gemini, options: modelOptions("gemini"))
                labeledPicker("Ollama", selection: fields.ollama, options: modelOptions("ollama"))
            }
            EffortPicker(level: Binding(
                get: { EffortLevel(configValue: fields.wrappedValue.claudeReasoning) },
                set: { fields.wrappedValue.claudeReasoning = $0.map(configValue) ?? "" }
            ), agent: "claude", showFootnote: false)
            EffortPicker(level: Binding(
                get: { EffortLevel(configValue: fields.wrappedValue.codexReasoning) },
                set: { fields.wrappedValue.codexReasoning = $0.map(configValue) ?? "" }
            ), agent: "codex", showFootnote: false)
        }
        .padding(.top, DS.space.xxs)
    }

    private func configValue(_ level: EffortLevel) -> String {
        switch level {
        case .low: return "low"
        case .medium: return "medium"
        case .high: return "high"
        }
    }

    @ViewBuilder
    private func labeledPicker(_ label: String, selection: Binding<String>,
                               options: [String]) -> some View {
        VStack(alignment: .leading, spacing: 2) {
            Text(label).font(DS.font.caption2).foregroundStyle(.secondary)
            Picker("", selection: selection) {
                Text("default").tag("")
                ForEach(options, id: \.self) { Text($0).font(DS.font.monoInline).tag($0) }
            }
            .labelsHidden().pickerStyle(.menu).font(DS.font.caption)
            .accessibilityLabel("\(label) override")
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }
}

// MARK: - Consequence Strip (§5.6 — the grid's conscience)

// Pure math, unit-tested: call arithmetic from rounds × participants ×
// phases, plus static size-floor quality warnings.
enum RoutingConsequences {
    struct Summary: Equatable {
        var minCalls: Int
        var maxCalls: Int
        var highEffortPhases: Int
        var heaviest: String        // "spec (opus, high)" or ""
        var warnings: [(phaseKey: String, text: String)]

        static func == (a: Summary, b: Summary) -> Bool {
            a.minCalls == b.minCalls && a.maxCalls == b.maxCalls
                && a.highEffortPhases == b.highEffortPhases && a.heaviest == b.heaviest
                && a.warnings.map(\.text) == b.warnings.map(\.text)
        }
    }

    // "3b"/"7B"/":13b" → parameter count in billions, nil when unparseable.
    static func parameterBillions(_ modelID: String) -> Double? {
        let m = modelID.lowercased()
        var best: Double?
        var digits = ""
        for ch in m {
            if ch.isNumber || ch == "." {
                digits.append(ch)
            } else {
                if ch == "b", !digits.isEmpty, let v = Double(digits) {
                    best = max(best ?? 0, v)
                }
                digits = ""
            }
        }
        return best
    }

    static func summarize(matrix: RoutingMatrix, phases: [PhaseDef],
                          enabledAgents: [String: Bool]) -> Summary {
        var minCalls = 0
        var maxCalls = 0
        var highEffort = 0
        var heaviestScore = -1
        var heaviest = ""
        var warnings: [(phaseKey: String, text: String)] = []
        for p in phases {
            var participants = 0
            var phaseHasHigh = false
            var phaseWeight = 0
            var heavyModel = ""
            for agent in RoutingMatrix.agents where enabledAgents[agent] ?? false {
                let c = matrix.cell(p.key, agent)
                if c.off { continue }
                participants += 1
                let effortScore = (c.effort?.rawValue ?? 2)
                phaseWeight += effortScore
                if c.effort == .high {
                    phaseHasHigh = true
                    heavyModel = c.model.isEmpty ? agent : c.model
                }
                // Quality floor: a <7B local model on a review/verify phase.
                if agent == "ollama",
                   let b = parameterBillions(c.model), b < 7,
                   p.key.lowercased().contains("review")
                    || p.key.lowercased().contains("verif") {
                    warnings.append((phaseKey: p.key,
                        text: "⚠ \(p.title) → \(c.model) (\(Int(b))B) — models under 7B produce low-quality reviews"))
                }
            }
            let rounds = p.rounds == 0 ? 3 : p.rounds   // unlimited: assume ~3 for the estimate
            minCalls += participants                      // at least one round happens
            maxCalls += participants * max(1, rounds)
            if phaseHasHigh { highEffort += 1 }
            if phaseWeight * max(1, rounds) > heaviestScore {
                heaviestScore = phaseWeight * max(1, rounds)
                heaviest = participants == 0 ? "" :
                    "\(p.title.lowercased())\(heavyModel.isEmpty ? "" : " (\(shorten(heavyModel))\(phaseHasHigh ? ", high" : ""))")"
            }
        }
        return Summary(minCalls: minCalls, maxCalls: maxCalls,
                       highEffortPhases: highEffort, heaviest: heaviest,
                       warnings: warnings)
    }

    private static func shorten(_ id: String) -> String {
        let tail = id.split(separator: "/").last.map(String.init) ?? id
        return tail.count > 20 ? String(tail.prefix(18)) + "…" : tail
    }
}

struct ConsequenceStrip: View {
    let matrix: RoutingMatrix
    let phases: [PhaseDef]
    let enabledAgents: [String: Bool]
    var onJump: ((GridCellID) -> Void)? = nil

    var body: some View {
        let s = RoutingConsequences.summarize(matrix: matrix, phases: phases,
                                              enabledAgents: enabledAgents)
        VStack(alignment: .leading, spacing: DS.space.xxs) {
            HStack(spacing: DS.space.xxs) {
                Image(systemName: "sum")
                    .font(DS.font.caption)
                    .foregroundStyle(DS.textTertiary)
                    .accessibilityHidden(true)
                Text(headline(s))
                    .font(DS.font.caption)
                    .foregroundStyle(.secondary)
                    .monospacedDigit()
            }
            ForEach(Array(s.warnings.enumerated()), id: \.offset) { _, w in
                Button {
                    onJump?(GridCellID(phaseKey: w.phaseKey, agent: "ollama"))
                } label: {
                    Text(w.text)
                        .font(DS.font.caption)
                        .foregroundStyle(DS.status.warning.color)
                        .multilineTextAlignment(.leading)
                }
                .buttonStyle(.plain)
                .help("Jump to the cell")
            }
        }
        .padding(DS.space.xs)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(RoundedRectangle(cornerRadius: DS.radius.chip, style: .continuous)
            .fill(.quaternary.opacity(0.5)))
        .accessibilityElement(children: .combine)
    }

    private func headline(_ s: RoutingConsequences.Summary) -> String {
        var parts = ["≈ \(s.minCalls)–\(s.maxCalls) CLI calls per run"]
        if s.highEffortPhases > 0 {
            parts.append("\(s.highEffortPhases) phase\(s.highEffortPhases == 1 ? "" : "s") at high effort")
        }
        if !s.heaviest.isEmpty { parts.append("heaviest: \(s.heaviest)") }
        return parts.joined(separator: " · ")
    }
}
