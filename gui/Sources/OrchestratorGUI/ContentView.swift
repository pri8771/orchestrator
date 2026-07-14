import SwiftUI

// Native Pro (AppShellView) is the app's only surface since M5 (§9): the
// classic three-pane browser and the factory dashboard — and the `ui.mode`
// flag that switched between them — are gone. Any lingering "ui.mode" value
// in preferences is simply never read again. This file keeps the shared
// organs the shell embeds: banners, approval sheets, the verification card,
// the phase list, and the per-project Iterate / Build-history sheets.
struct ContentView: View {
    var body: some View {
        AppShellView()
    }
}

// MARK: - Approval banner (semi-autonomous / manual checkpoint, spec §3.1)

struct ApprovalBanner: View {
    @EnvironmentObject var store: OrchestratorStore
    let project: Project
    let phase: String
    @State private var showEdit = false
    @State private var showChanges = false

    var body: some View {
        InlineBanner(kind: .warning, symbol: "pause.circle.fill",
                     title: "Paused for your approval",
                     message: "Finished \(project.titleFor(phase)). Approve to continue, edit the output first, or send it back with feedback.") {
            HStack(spacing: DS.space.xs) {
                Button("Request Changes…") { showChanges = true }
                    .accessibilityLabel("Request changes")
                    .accessibilityHint("Send feedback; the engine re-runs \(project.titleFor(phase))")
                Button("Edit & Approve…") { showEdit = true }
                    .accessibilityLabel("Edit and approve")
                    .accessibilityHint("Edit the phase output before continuing")
                Button("Approve") { store.approve(project, phase: phase) }
                    .keyboardShortcut(.defaultAction)
                    .accessibilityLabel("Approve")
                    .accessibilityHint("Continue to the next phase as-is")
            }
        }
        .sheet(isPresented: $showEdit) {
            ApprovalEditSheet(project: project, phase: phase).environmentObject(store)
        }
        .sheet(isPresented: $showChanges) {
            RequestChangesSheet(project: project, phase: phase).environmentObject(store)
        }
    }
}

// Edit & Approve: the edited body is written to approvals/<phase>.edit and
// REPLACES the phase's output; the run then continues.
struct ApprovalEditSheet: View {
    @EnvironmentObject var store: OrchestratorStore
    @Environment(\.dismiss) private var dismiss
    let project: Project
    let phase: String
    @State private var text = ""
    @State private var loaded = false

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("Edit & approve — \(project.titleFor(phase))").font(.title3).fontWeight(.medium)
            Text("Edit the phase's output below. On approve, your version replaces it and the run continues.")
                .font(.caption).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
            TextEditor(text: $text)
                .font(.system(.callout, design: .monospaced))
                .frame(minHeight: 260)
                .overlay(RoundedRectangle(cornerRadius: 6).stroke(Color.secondary.opacity(0.3)))
                .accessibilityLabel("Phase output editor")
            HStack {
                Spacer()
                Button("Cancel") { dismiss() }
                Button("Approve with edits") {
                    store.submitApproval(project, phase: phase,
                                         decision: .editAndApprove, body: text)
                    dismiss()
                }
                .keyboardShortcut(.defaultAction)
                .disabled(text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                .accessibilityHint("Writes the edited output and continues the run")
            }
        }
        .padding(20).frame(width: 560, height: 420)
        .onAppear {
            if !loaded { text = project.phaseOutputs[phase] ?? ""; loaded = true }
        }
    }
}

// Request Changes: the feedback body is written to approvals/<phase>.changes
// and the engine re-runs the phase with it.
struct RequestChangesSheet: View {
    @EnvironmentObject var store: OrchestratorStore
    @Environment(\.dismiss) private var dismiss
    let project: Project
    let phase: String
    @State private var feedback = ""

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("Request changes — \(project.titleFor(phase))").font(.title3).fontWeight(.medium)
            Text("Tell the agents what to change. The phase re-runs with your feedback folded in.")
                .font(.caption).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
            TextEditor(text: $feedback)
                .font(.system(.callout, design: .monospaced))
                .frame(minHeight: 140)
                .overlay(RoundedRectangle(cornerRadius: 6).stroke(Color.secondary.opacity(0.3)))
                .accessibilityLabel("Change-request feedback")
            HStack {
                Spacer()
                Button("Cancel") { dismiss() }
                Button("Send & re-run") {
                    store.submitApproval(project, phase: phase,
                                         decision: .requestChanges, body: feedback)
                    dismiss()
                }
                .keyboardShortcut(.defaultAction)
                .disabled(feedback.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                .accessibilityHint("Sends your feedback and re-runs the phase")
            }
        }
        .padding(20).frame(width: 480)
    }
}

// MARK: - Aborted-run error banner

struct ErrorBanner: View {
    let project: Project
    let message: String

    var body: some View {
        InlineBanner(kind: .error, title: "Run aborted", message: message) {
            Button {
                NSPasteboard.general.clearContents()
                NSPasteboard.general.setString(message, forType: .string)
            } label: {
                Label("Copy", systemImage: "doc.on.doc")
            }
            .accessibilityLabel("Copy error")
            .accessibilityHint("Copies the full error message to the clipboard")
        }
    }
}

// MARK: - blocked_conflict banner (§18.3: paused on a real merge conflict)

struct ConflictBanner: View {
    let conflict: BlockedConflict

    var body: some View {
        // Amber, not purple: purple means fallback, exclusively (§2.2 / §6).
        InlineBanner(kind: .warning, symbol: "arrow.triangle.branch",
                     title: "Build blocked: merge conflict on \(conflict.filesDisplay) (lane \(conflict.lane)) — resolve, then Resume.",
                     message: conflict.detail.isEmpty ? nil : conflict.detail,
                     messageLineLimit: nil)
            .accessibilityLabel("Build blocked by a merge conflict")
            .accessibilityValue("Files \(conflict.filesDisplay), lane \(conflict.lane). \(conflict.detail)")
    }
}

// MARK: - Verification card (latest verify_results.json record, §15)

struct VerificationCard: View {
    let record: VerifyRecord
    let repairCount: Int
    @State private var showErrors = false

    private var tint: Color {
        switch record.status {
        case "verified": return DS.status.success.color
        case "failed": return DS.status.error.color
        default: return .secondary
        }
    }
    private var symbol: String {
        switch record.status {
        case "verified": return "checkmark.seal.fill"
        case "failed": return "xmark.seal.fill"
        default: return "questionmark.diamond"
        }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(spacing: 8) {
                Image(systemName: symbol).foregroundStyle(tint)
                    .accessibilityHidden(true)
                Text("Verification: \(record.statusLabel)")
                    .font(.body).fontWeight(.medium).foregroundStyle(tint)
                if !record.summary.isEmpty {
                    Text(record.summary).font(.subheadline).foregroundStyle(.secondary)
                        .lineLimit(2)
                }
                Spacer()
                if repairCount > 0 {
                    Text("\(repairCount) repair attempt\(repairCount == 1 ? "" : "s")")
                        .font(.footnote).foregroundStyle(.secondary)
                }
                if !record.errors.isEmpty {
                    Button {
                        withAnimation { showErrors.toggle() }
                    } label: {
                        Label(showErrors ? "Hide errors" : "Show errors",
                              systemImage: showErrors ? "chevron.up" : "chevron.down")
                            .font(.footnote)
                    }
                    .buttonStyle(.plain).foregroundStyle(.secondary)
                    .accessibilityLabel(showErrors ? "Hide verification errors" : "Show verification errors")
                }
            }
            .accessibilityElement(children: .contain)
            if showErrors && !record.errors.isEmpty {
                ScrollView {
                    Text(record.errors)
                        .font(.system(.caption, design: .monospaced))
                        .textSelection(.enabled)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .padding(8)
                }
                .frame(maxHeight: 180)
                .background(RoundedRectangle(cornerRadius: 6)
                    .fill(Color(nsColor: .textBackgroundColor).opacity(0.6)))
                .overlay(RoundedRectangle(cornerRadius: 6).stroke(Color.secondary.opacity(0.2)))
                .accessibilityLabel("Verification error output")
                .accessibilityValue(record.errors)
            }
        }
        .padding(.horizontal, 14).padding(.vertical, 8)
        .background(tint.opacity(0.08))
        .overlay(Divider(), alignment: .bottom)
        .accessibilityElement(children: .contain)
        .accessibilityLabel("Verification \(record.statusLabel)")
        .accessibilityValue(record.summary)
    }
}

// MARK: - Engine missing banner (no bundled engine, no repo checkout)

struct EngineMissingBanner: View {
    let message: String

    var body: some View {
        InlineBanner(kind: .error, symbol: "exclamationmark.triangle.fill",
                     title: "Engine not found", message: message,
                     messageLineLimit: nil)
            .accessibilityLabel("Engine missing")
            .accessibilityValue(message)
    }
}

// A dismissible top banner for a failed GUI action (config write, lock clear,
// message queue…). Surfaces errors that would otherwise hide in the ⌘L run log.
struct ActionErrorBanner: View {
    let message: String
    let onDismiss: () -> Void

    var body: some View {
        InlineBanner(kind: .warning, title: "Action failed", message: message,
                     messageLineLimit: nil) {
            Button(action: onDismiss) {
                Image(systemName: "xmark.circle.fill").foregroundStyle(.secondary)
            }
            .buttonStyle(.plain)
            .help("Dismiss")
            .accessibilityLabel("Dismiss error")
        }
        .accessibilityLabel("Action failed")
        .accessibilityValue(message)
    }
}

// ⌘K command palette (design §3/§8 + DESIGN-REFRESH.md §6): a translucent
// floating panel over a dimmed scrim — not a default sheet. Commands dispatch
// the same actions as the menu bar through store.uiCommand; project rows
// fuzzy-match the store's project list and jump the sidebar selection via
// onJumpToProject. Presented as an overlay by AppShellView.
struct CommandPaletteView: View {
    @EnvironmentObject var store: OrchestratorStore
    /// Jumps the shell's sidebar selection to the chosen project.
    var onJumpToProject: (String) -> Void = { _ in }
    @State private var query = ""
    @State private var selection: RowID?
    @FocusState private var searchFocused: Bool

    private struct Command: Identifiable {
        var id: UICommand { action }
        let title: String
        let symbol: String
        let shortcut: String
        let action: UICommand
    }

    // One selection identity across both sections, so arrow keys walk from
    // the last command straight into the first project.
    private enum RowID: Hashable {
        case command(UICommand)
        case project(String)
    }

    // Computed (not a stored constant) so the Pause/Resume entry's title
    // reflects the engine's current paused state. Titles/shortcuts for the
    // commands also on the menu bar come from MenuCommandSpec.all
    // (OrchestratorStore.swift), the shared source both surfaces read from.
    private var commands: [Command] {
        func spec(_ action: UICommand, symbol: String) -> Command {
            let s = MenuCommandSpec.spec(for: action)
            return Command(title: s.title, symbol: symbol,
                           shortcut: s.shortcutDisplay, action: s.action)
        }
        return [
            spec(.newChat, symbol: "plus"),
            spec(.runSelected, symbol: "play.fill"),
            Command(title: store.enginePaused ? "Resume Engine" : "Pause Engine",
                    symbol: store.enginePaused ? "play.circle" : "pause.circle",
                    shortcut: "", action: .togglePause),
            spec(.toggleLog, symbol: "terminal"),
            spec(.toggleInspector, symbol: "sidebar.trailing"),
            spec(.focusSearch, symbol: "magnifyingglass"),
        ]
    }

    private var filteredCommands: [Command] {
        let q = query.trimmingCharacters(in: .whitespaces)
        return q.isEmpty ? commands
                         : commands.filter { Self.fuzzyScore(q, $0.title) != nil }
    }

    // Projects, best fuzzy match first (score desc, then name for stability).
    private var filteredProjects: [Project] {
        let q = query.trimmingCharacters(in: .whitespaces)
        if q.isEmpty { return store.projects.sorted { $0.name < $1.name } }
        return store.projects
            .compactMap { p in Self.fuzzyScore(q, p.name).map { (p, $0) } }
            .sorted { $0.1 != $1.1 ? $0.1 > $1.1 : $0.0.name < $1.0.name }
            .map(\.0)
    }

    private var allRowIDs: [RowID] {
        filteredCommands.map { .command($0.id) }
            + filteredProjects.map { .project($0.name) }
    }

    // Fuzzy subsequence match ("bri" → Brinekeeper): every query character
    // appears in order, case-insensitively. Prefix beats substring beats
    // scattered subsequence. nil = no match.
    // `nonisolated`: touches no actor state (pure String matching), and is
    // called synchronously from XCTest — without this it would inherit
    // CommandPaletteView's @MainActor isolation (View conformance) and the
    // test call wouldn't compile/run outside the main actor.
    nonisolated static func fuzzyScore(_ query: String, _ candidate: String) -> Int? {
        let q = query.lowercased(), c = candidate.lowercased()
        if q.isEmpty { return 0 }
        if c.hasPrefix(q) { return 3 }
        if c.contains(q) { return 2 }
        var pos = c.startIndex
        for ch in q {
            guard let hit = c[pos...].firstIndex(of: ch) else { return nil }
            pos = c.index(after: hit)
        }
        return 1
    }

    private func close() { store.showCommandPalette = false }

    private func run(_ row: RowID) {
        close()
        switch row {
        case .command(let action): store.uiCommand = action
        case .project(let name): onJumpToProject(name)
        }
    }

    // The search field keeps focus while the palette is open, so arrow keys
    // never reach the rows; handle them here instead. Clamped at both ends.
    private func moveSelection(by delta: Int) -> KeyPress.Result {
        let ids = allRowIDs
        guard !ids.isEmpty else { return .ignored }
        if let idx = ids.firstIndex(where: { $0 == selection }) {
            selection = ids[min(max(idx + delta, 0), ids.count - 1)]
        } else {
            selection = delta > 0 ? ids.first : ids.last
        }
        return .handled
    }

    private func runSelectedOrFirst() {
        if let sel = selection, allRowIDs.contains(sel) {
            run(sel)
        } else if let first = allRowIDs.first {
            run(first)
        }
    }

    var body: some View {
        ZStack(alignment: .top) {
            // Dimmed scrim: click anywhere outside the panel dismisses.
            Color.black.opacity(0.2)
                .ignoresSafeArea()
                .onTapGesture { close() }
                .accessibilityHidden(true)
            panel
                .padding(.top, 72)
        }
    }

    private var panel: some View {
        VStack(spacing: 0) {
            searchField
            Divider()
            results
            Divider()
            footer
        }
        .frame(width: 560)
        .background(.regularMaterial,
                    in: RoundedRectangle(cornerRadius: DS.radius.card, style: .continuous))
        .overlay(RoundedRectangle(cornerRadius: DS.radius.card, style: .continuous)
            .stroke(DS.hairline, lineWidth: 1))
        .shadow(color: .black.opacity(0.25), radius: 24, y: 8)
        .accessibilityElement(children: .contain)
        .accessibilityLabel("Command palette")
    }

    private var searchField: some View {
        HStack(spacing: DS.space.xs) {
            Image(systemName: "command")
                .foregroundStyle(.secondary)
                .accessibilityHidden(true)
            TextField("Search commands and projects…", text: $query)
                .textFieldStyle(.plain)
                .font(.title3)
                .focused($searchFocused)
                .accessibilityLabel("Command palette search")
                .onAppear {
                    // Focus on the next runloop turn: SwiftUI can drop a focus
                    // request issued in the same tick the overlay is presented.
                    DispatchQueue.main.async { searchFocused = true }
                    selection = allRowIDs.first
                }
                .onChange(of: query) {
                    // Keep a selection alive as filtering narrows the list, so
                    // Return always has something to activate.
                    if !allRowIDs.contains(where: { $0 == selection }) {
                        selection = allRowIDs.first
                    }
                }
                .onSubmit { runSelectedOrFirst() }
                .onKeyPress(.upArrow) { moveSelection(by: -1) }
                .onKeyPress(.downArrow) { moveSelection(by: 1) }
                .onKeyPress(.escape) { close(); return .handled }
        }
        .padding(DS.space.s)
    }

    @ViewBuilder
    private var results: some View {
        let cmds = filteredCommands
        let projs = filteredProjects
        if cmds.isEmpty && projs.isEmpty {
            VStack(spacing: DS.space.xxs) {
                Text("No matches")
                Text("Clear the search to see all commands and projects")
                    .font(DS.font.caption)
            }
            .foregroundStyle(.secondary)
            .frame(maxWidth: .infinity, minHeight: 80)
        } else {
            ScrollViewReader { proxy in
                ScrollView {
                    LazyVStack(alignment: .leading, spacing: 1) {
                        if !cmds.isEmpty {
                            sectionHeader("Commands")
                            ForEach(cmds) { c in
                                row(id: .command(c.id), symbol: c.symbol, title: c.title,
                                    trailing: c.shortcut)
                            }
                        }
                        if !projs.isEmpty {
                            sectionHeader("Projects")
                            ForEach(projs) { p in
                                row(id: .project(p.name), symbol: "folder", title: p.name,
                                    trailing: "", detail: p.workflowTitle)
                            }
                        }
                    }
                    .padding(DS.space.xs)
                }
                .frame(maxHeight: 320)
                .onChange(of: selection) {
                    if let selection { proxy.scrollTo(selection) }
                }
            }
        }
    }

    private func sectionHeader(_ title: String) -> some View {
        Text(title)
            .font(DS.font.caption)
            .foregroundStyle(.tertiary)
            .padding(.horizontal, DS.space.xs)
            .padding(.top, DS.space.xxs)
    }

    // 38pt row: symbol + title (+ caption detail) + right-aligned shortcut in
    // monoInline; selected row = the accent 8/12% fill.
    private func row(id: RowID, symbol: String, title: String,
                     trailing: String, detail: String? = nil) -> some View {
        let selected = selection == id
        return Button { run(id) } label: {
            HStack(spacing: DS.space.xs) {
                Image(systemName: symbol)
                    .font(DS.font.body)
                    .foregroundStyle(selected ? DS.accent.color : DS.textSecondary)
                    .frame(width: 18)
                    .accessibilityHidden(true)
                Text(title)
                    .font(DS.font.body)
                    .lineLimit(1)
                if let detail {
                    Text(detail)
                        .font(DS.font.caption)
                        .foregroundStyle(.tertiary)
                        .lineLimit(1)
                }
                Spacer()
                if !trailing.isEmpty {
                    Text(trailing)
                        .font(DS.font.monoInline)
                        .foregroundStyle(.secondary)
                }
            }
            .padding(.horizontal, DS.space.xs)
            .frame(height: 38)
            .background(RoundedRectangle(cornerRadius: DS.radius.control, style: .continuous)
                .fill(selected ? AnyShapeStyle(DS.accent.fill) : AnyShapeStyle(Color.clear)))
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .id(id)
        .accessibilityLabel(title)
        .accessibilityAddTraits(selected ? .isSelected : [])
    }

    private var footer: some View {
        HStack(spacing: DS.space.m) {
            hint("↑↓", "Navigate")
            hint("↩", "Run")
            hint("esc", "Dismiss")
            Spacer()
        }
        .padding(.horizontal, DS.space.s)
        .padding(.vertical, DS.space.xs)
        .accessibilityHidden(true)   // keyboard hints, redundant for VoiceOver
    }

    private func hint(_ key: String, _ label: String) -> some View {
        HStack(spacing: DS.space.xxs) {
            Text(key)
                .font(DS.font.caption2)
                .foregroundStyle(.secondary)
                .padding(.horizontal, 4).padding(.vertical, 1)
                .background(RoundedRectangle(cornerRadius: DS.radius.control - 2,
                                             style: .continuous)
                    .fill(.quaternary.opacity(0.5)))
            Text(label)
                .font(DS.font.caption)
                .foregroundStyle(.tertiary)
        }
    }
}

struct IterateSheet: View {
    @EnvironmentObject var store: OrchestratorStore
    @Environment(\.dismiss) private var dismiss
    let project: Project
    @State private var feature = ""

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("Iterate on \(project.name)").font(.title3).fontWeight(.medium)
            Text("Describe a feature to add or a bug to fix. The agents extend the existing app in app_build (short workflow) instead of rebuilding from scratch.")
                .font(.caption).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
            TextEditor(text: $feature)
                .font(.system(.callout, design: .monospaced))
                .frame(minHeight: 120)
                .overlay(RoundedRectangle(cornerRadius: 6).stroke(Color.secondary.opacity(0.3)))
                .accessibilityLabel("Feature description")
            HStack {
                Spacer()
                Button("Cancel") { dismiss() }
                Button("Build change") {
                    store.iterateProject(project, feature: feature); dismiss()
                }
                .keyboardShortcut(.defaultAction)
                .disabled(feature.trimmingCharacters(in: .whitespaces).isEmpty)
            }
        }
        .padding(20).frame(width: 460)
    }
}

struct BuildHistorySheet: View {
    @EnvironmentObject var store: OrchestratorStore
    @Environment(\.dismiss) private var dismiss
    let project: Project
    @State private var lines: [String] = []

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("Build history — \(project.name)").font(.title3).fontWeight(.medium)
            Text("Each commit is one build iteration in app_build (git).")
                .font(.caption).foregroundStyle(.secondary)
            if lines.isEmpty {
                Text("No build history yet (the app hasn't been built, or git isn't available).")
                    .font(.caption).foregroundStyle(.tertiary).padding(.vertical, 20)
            } else {
                ScrollView {
                    VStack(alignment: .leading, spacing: 4) {
                        ForEach(lines, id: \.self) { line in
                            Text(line).font(.system(.subheadline, design: .monospaced))
                                .textSelection(.enabled)
                        }
                    }
                }.frame(maxHeight: 300)
            }
            HStack { Spacer(); Button("Done") { dismiss() }.keyboardShortcut(.defaultAction) }
        }
        .padding(20).frame(width: 460)
        .task {
            // git log runs off the main actor — a big app_build repo (or a git
            // that stats a cold disk) must not beachball the whole GUI.
            let dir = project.dirURL.appendingPathComponent("app_build")
            lines = await Task.detached { OrchestratorStore.buildHistory(buildDir: dir) }.value
        }
    }
}

// MARK: - Phase list (middle column)

struct PhaseListView: View {
    @EnvironmentObject var store: OrchestratorStore
    let project: Project
    @Binding var selection: String?

    var body: some View {
        List(selection: $selection) {
            Section {
                ForEach(store.phases(for: project)) { def in
                    let st = project.phaseStatus(def.key)
                    HStack(spacing: 9) {
                        Image(systemName: def.writes ? "hammer" : st.symbol)
                            .foregroundStyle(st.tint)
                            .font(.body).frame(width: 14)
                        Text(def.title)
                            .font(.body)
                            .fontWeight(st == .active ? .medium : .regular)
                            .foregroundStyle(st == .pending ? .secondary : .primary)
                        Spacer()
                        if st == .active && project.status == .inProgress {
                            Text("r\(project.currentRound)")
                                .font(.footnote).foregroundStyle(.secondary)
                                .padding(.horizontal, 6).padding(.vertical, 1)
                                .background(Capsule().fill(Color.secondary.opacity(0.15)))
                        } else {
                            Text("\(def.rounds)")
                                .font(.footnote).foregroundStyle(.tertiary)
                        }
                    }
                    .tag(def.key)
                    .accessibilityElement(children: .ignore)
                    .accessibilityLabel(def.title)
                    .accessibilityValue(phaseStatusDescription(st))
                }
            } header: {
                HStack(spacing: 6) {
                    Image(systemName: workflowSymbol).font(.footnote)
                    Text(project.workflowTitle)
                }
            }
        }
        .listStyle(.sidebar)
    }

    private var workflowSymbol: String {
        store.workflow(named: project.workflow)?.symbol ?? "hammer"
    }

    private func phaseStatusDescription(_ st: PhaseStatus) -> String {
        switch st {
        case .done: return "done"
        case .active: return project.status == .inProgress
            ? "active, round \(project.currentRound)" : "active"
        case .aborted: return "aborted"
        case .pending: return "pending"
        }
    }
}
