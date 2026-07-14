import SwiftUI
import AppKit
import UniformTypeIdentifiers
import QuickLook

// MARK: - New App intake (DESIGN-NATIVE-PRO.md §4.5, milestone M2)
//
// Sheet 560×620, 20pt padding. Ports the factory dashboard intake's
// docs-attach + backfill logic (commits d54d573/fc4b69f) into the Native Pro
// sheet: name auto-slugged from the idea, borderless idea well, dashed docs
// drop-well with 32pt rows and a "Backfill spec from docs" toggle, horizontal
// workflow cards with phase-dot strips, a routing-preset segmented control
// (the sheet never shows the matrix — intake stays fast), and a queue
// position control with a live caption. Footer: Cancel / Add to Queue (⌘↩).

struct NewAppIntakeSheet: View {
    @EnvironmentObject var store: OrchestratorStore
    @Environment(\.dismiss) private var dismiss
    let onCreated: (String) -> Void

    @State private var name = ""
    @State private var nameEdited = false
    @State private var idea = ""
    @State private var workflow = "app_build"
    @State private var attachedDocs: [URL] = []
    @State private var quickLookURL: URL?
    @State private var backfillFromDocs = true
    @State private var docDropTargeted = false
    @State private var routingPreset = "balanced"
    @State private var position: QueuePosition = .endOfQueue
    // Run-config options (ported from the classic New-chat sheet in M5).
    // Autonomy/completeness persist so they double as app-level defaults —
    // the same @AppStorage keys Settings › Defaults edits.
    @AppStorage("defaultAutonomy") private var autonomy = "fully_autonomous"
    @AppStorage("defaultCompleteness") private var completeness = ""
    @State private var stopAfter = ""
    @State private var targetPaths = ""

    enum QueuePosition: String, CaseIterable, Identifiable {
        case endOfQueue, next, runNow
        var id: String { rawValue }
        var label: String {
            switch self {
            case .endOfQueue: return "End of queue"
            case .next: return "Next"
            case .runNow: return "Run now"
            }
        }
    }

    private static let routingPresets: [(key: String, label: String)] = [
        ("balanced", "Balanced"), ("max_quality", "Max Quality"), ("economy", "Economy"),
    ]

    // Same slug rule as the factory intake (lowercase, spaces→hyphens,
    // [a-z0-9-] only).
    static func slugify(_ s: String) -> String {
        // Delegate to the store's slugify so folder names are consistent — the
        // old local version left consecutive hyphens (e.g. "Åpp Ünïq" -> "pp--nq").
        OrchestratorStore.slugify(s)
    }

    // The prompt written when docs are attached and the idea box stays empty.
    static let docsPlaceholderPrompt =
        "Build the app specified in docs/ — treat it as the source of truth."

    private var effectiveName: String {
        let trimmed = name.trimmingCharacters(in: .whitespaces)
        if !trimmed.isEmpty { return trimmed }
        return firstLine(idea)
    }
    private var slug: String { Self.slugify(effectiveName) }
    private var folderExists: Bool {
        !slug.isEmpty && FileManager.default.fileExists(
            atPath: store.rootURL.appendingPathComponent(slug).path)
    }
    // Audit / library-mining workflows analyze an existing repo, so they need
    // at least one target path (same rule as the classic New-chat sheet).
    private var needsTargets: Bool {
        let t = store.workflow(named: workflow)?.target ?? ""
        return t == "audit" || t == "library_mining"
    }
    private var canCreate: Bool {
        !slug.isEmpty && !folderExists
            && (!idea.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
                || !attachedDocs.isEmpty)
            && (!needsTargets
                || !targetPaths.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
    }

    private var runningCount: Int {
        store.projects.filter { store.appLocks[$0.name] != nil || store.canStop($0.name) }.count
    }
    private var queuedCount: Int {
        store.projects.filter { p in
            store.appLocks[p.name] == nil && !store.canStop(p.name)
                && p.status != .done && p.status != .aborted
        }.count
    }
    // "Will start in lane 2, position 4."
    private var positionCaption: String {
        let lanes = max(1, store.buildLanes)
        switch position {
        case .runNow:
            let lane = min(lanes, runningCount + 1)
            return "Launches immediately in lane \(lane)."
        case .next:
            let lane = (runningCount % lanes) + 1
            return "Will start in lane \(lane), position 1."
        case .endOfQueue:
            let pos = queuedCount + 1
            let lane = ((runningCount + pos - 1) % lanes) + 1
            return "Will start in lane \(lane), position \(pos)."
        }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: DS.space.s) {
            VStack(alignment: .leading, spacing: 2) {
                Text("New App").font(DS.font.title)
                Text("It will be checked for uniqueness against past apps.")
                    .font(DS.font.caption).foregroundStyle(.secondary)
            }

            ScrollView {
                VStack(alignment: .leading, spacing: DS.space.s) {
                    nameField
                    ideaField
                    docsSection
                    workflowSection
                    if needsTargets { targetsSection }
                    runOptionsSection
                    routingSection
                    positionSection
                }
                .padding(.horizontal, 2)
            }

            HStack {
                if folderExists {
                    Label("Folder “\(slug)” already exists", systemImage: "exclamationmark.triangle.fill")
                        .font(DS.font.caption)
                        .foregroundStyle(DS.status.warning.color)
                }
                Spacer()
                Button("Cancel") { dismiss() }
                    .keyboardShortcut(.cancelAction)
                Button(position == .runNow ? "Add & Run" : "Add to Queue") { create() }
                    .buttonStyle(.borderedProminent)
                    .keyboardShortcut(.return, modifiers: .command)
                    .disabled(!canCreate)
            }
        }
        .padding(20)
        .frame(width: 560, height: 620)
        .quickLookPreview($quickLookURL)
    }

    // MARK: Name (auto-slugged from the idea)

    private var nameField: some View {
        VStack(alignment: .leading, spacing: DS.space.xxs) {
            Text("Name").font(DS.font.callout).foregroundStyle(.secondary)
            TextField("Auto-named from the idea", text: $name)
                .textFieldStyle(.roundedBorder)
                .onChange(of: name) { _, _ in nameEdited = !name.isEmpty }
                .accessibilityLabel("App name")
            HStack {
                Spacer()
                Text(slug.isEmpty ? " " : slug)
                    .font(DS.font.monoInline)
                    .foregroundStyle(folderExists ? DS.status.warning.color : DS.textTertiary)
                    .accessibilityLabel("Folder slug \(slug)")
            }
        }
    }

    // MARK: Idea prompt (borderless editor in a 12pt-radius quaternary well)

    private var ideaField: some View {
        VStack(alignment: .leading, spacing: DS.space.xxs) {
            Text("Idea").font(DS.font.callout).foregroundStyle(.secondary)
            TextEditor(text: $idea)
                .font(DS.font.body)
                .scrollContentBackground(.hidden)
                .padding(DS.space.xs)
                .frame(height: 140)
                .background(RoundedRectangle(cornerRadius: DS.radius.card, style: .continuous)
                    .fill(.quaternary.opacity(0.5)))
                .accessibilityLabel("Idea prompt")
            if !attachedDocs.isEmpty {
                Text("Optional — with docs attached, an empty prompt points the agents at docs/.")
                    .font(DS.font.caption).foregroundStyle(.tertiary)
            }
        }
    }

    // MARK: Docs (dashed drop-well + 32pt rows + backfill toggle)

    private static let docExtensions: Set<String> = ["md", "markdown", "txt", "pdf"]

    private var docsSection: some View {
        VStack(alignment: .leading, spacing: DS.space.xxs) {
            Text("Docs").font(DS.font.callout).foregroundStyle(.secondary)
            Button { pickDocs() } label: {
                HStack(spacing: DS.space.xs) {
                    Image(systemName: "arrow.down.doc")
                        .foregroundStyle(docDropTargeted ? DS.accent.color : DS.textSecondary)
                    Text("Drop .md .txt .pdf or Browse…")
                        .font(DS.font.body)
                        .foregroundStyle(.secondary)
                    Spacer()
                }
                .padding(DS.space.s)
                .frame(maxWidth: .infinity)
                .overlay(
                    RoundedRectangle(cornerRadius: DS.radius.chip, style: .continuous)
                        .strokeBorder(docDropTargeted ? DS.accent.color : DS.hairline,
                                      style: StrokeStyle(lineWidth: 1, dash: [5, 3])))
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .onDrop(of: [UTType.fileURL], isTargeted: $docDropTargeted) { handleDrop($0) }
            .accessibilityLabel("Attach documents")

            ForEach(attachedDocs, id: \.self) { url in
                HStack(spacing: DS.space.xs) {
                    Image(systemName: "doc.text").foregroundStyle(DS.accent.color)
                    Text(url.lastPathComponent)
                        .font(DS.font.body)
                        .lineLimit(1).truncationMode(.middle)
                    Text(fileSizeLabel(url))
                        .font(DS.font.caption).foregroundStyle(.tertiary)
                    Spacer()
                    Button {
                        quickLookURL = url
                    } label: {
                        Image(systemName: "eye")
                            .foregroundStyle(.secondary)
                    }
                    .buttonStyle(.plain)
                    .accessibilityLabel("Preview \(url.lastPathComponent)")
                    Button {
                        attachedDocs.removeAll { $0 == url }
                    } label: {
                        Image(systemName: "xmark.circle.fill")
                            .foregroundStyle(.secondary)
                    }
                    .buttonStyle(.plain)
                    .accessibilityLabel("Remove \(url.lastPathComponent)")
                }
                .frame(height: 32)
            }

            if !attachedDocs.isEmpty {
                Toggle("Backfill spec from docs", isOn: $backfillFromDocs)
                    .toggleStyle(.switch)
                    .controlSize(.small)
                    .font(DS.font.body)
                Text("Distills the docs into the early phases; the build always runs live.")
                    .font(DS.font.caption).foregroundStyle(.tertiary)
            }
        }
        .animation(DS.spring, value: attachedDocs)
    }

    private func pickDocs() {
        let panel = NSOpenPanel()
        panel.canChooseFiles = true
        panel.canChooseDirectories = false
        panel.allowsMultipleSelection = true
        var types: [UTType] = [.plainText, .pdf]
        if let md = UTType(filenameExtension: "md") { types.append(md) }
        panel.allowedContentTypes = types
        if panel.runModal() == .OK { addDocs(panel.urls) }
    }

    private func addDocs(_ urls: [URL]) {
        for url in urls
        where Self.docExtensions.contains(url.pathExtension.lowercased())
            && !attachedDocs.contains(url) {
            attachedDocs.append(url)
        }
    }

    private func handleDrop(_ providers: [NSItemProvider]) -> Bool {
        var any = false
        for p in providers where p.hasItemConformingToTypeIdentifier(UTType.fileURL.identifier) {
            any = true
            p.loadItem(forTypeIdentifier: UTType.fileURL.identifier, options: nil) { item, _ in
                var url: URL?
                if let data = item as? Data {
                    url = URL(dataRepresentation: data, relativeTo: nil)
                } else if let u = item as? URL {
                    url = u
                }
                if let url { Task { @MainActor in addDocs([url]) } }
            }
        }
        return any
    }

    private func fileSizeLabel(_ url: URL) -> String {
        let bytes = (try? FileManager.default.attributesOfItem(atPath: url.path))?[.size]
            as? Int64 ?? 0
        return ByteCountFormatter.string(fromByteCount: bytes, countStyle: .file)
    }

    // MARK: Workflow (horizontal 120×80 cards with phase-dot strips)

    private var workflowOptions: [WorkflowDef] {
        let pinned = ["app_build", "app_spec", "research"]
        let byName = Dictionary(store.workflows.map { ($0.name, $0) },
                                uniquingKeysWith: { a, _ in a })
        let lead = pinned.compactMap { byName[$0] }
        let rest = store.workflows.filter { !pinned.contains($0.name) }
            .sorted { $0.name < $1.name }
        return lead + rest
    }

    private var workflowSection: some View {
        VStack(alignment: .leading, spacing: DS.space.xxs) {
            Text("Workflow").font(DS.font.callout).foregroundStyle(.secondary)
            ScrollView(.horizontal, showsIndicators: false) {
                HStack(spacing: DS.space.xs) {
                    ForEach(workflowOptions) { wf in
                        workflowCard(wf)
                    }
                }
                .padding(.vertical, 2)
            }
        }
    }

    private func workflowCard(_ wf: WorkflowDef) -> some View {
        let selected = workflow == wf.name
        return Button { workflow = wf.name } label: {
            VStack(alignment: .leading, spacing: DS.space.xxs) {
                Image(systemName: wf.symbol)
                    .font(DS.font.callout)
                    .foregroundStyle(selected ? DS.accent.color : DS.textSecondary)
                Text(wf.title)
                    .font(DS.font.callout)
                    .foregroundStyle(selected ? DS.textPrimary : DS.textSecondary)
                    .lineLimit(2)
                    .multilineTextAlignment(.leading)
                Spacer(minLength: 0)
                // Phase-dot strip: one 4pt dot per phase (capped).
                HStack(spacing: 3) {
                    ForEach(0..<min(wf.phases.count, 14), id: \.self) { _ in
                        Circle()
                            .fill(selected ? DS.accent.color : DS.hairline)
                            .frame(width: 4, height: 4)
                    }
                    if wf.phases.count > 14 {
                        Text("+\(wf.phases.count - 14)")
                            .font(DS.font.caption).foregroundStyle(.tertiary)
                    }
                }
                .accessibilityHidden(true)
            }
            .padding(DS.space.xs)
            .frame(width: 120, height: 80, alignment: .topLeading)
            .background(RoundedRectangle(cornerRadius: DS.radius.chip, style: .continuous)
                .fill(selected ? AnyShapeStyle(DS.accent.fill)
                               : AnyShapeStyle(.quaternary.opacity(0.5))))
            .overlay(RoundedRectangle(cornerRadius: DS.radius.chip, style: .continuous)
                .stroke(selected ? DS.accent.stroke : DS.hairline, lineWidth: 1))
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .accessibilityLabel("\(wf.title), \(wf.phases.count) phases")
        .accessibilityAddTraits(selected ? .isSelected : [])
    }

    // MARK: Target paths (audit / library-mining workflows only)

    private var targetsSection: some View {
        VStack(alignment: .leading, spacing: DS.space.xxs) {
            Text(store.workflow(named: workflow)?.target == "library_mining"
                 ? "Repositories to analyze (one path per line)"
                 : "Repository to analyze")
                .font(DS.font.callout).foregroundStyle(.secondary)
            TextEditor(text: $targetPaths)
                .font(DS.font.monoInline)
                .scrollContentBackground(.hidden)
                .padding(DS.space.xs)
                .frame(height: 60)
                .background(RoundedRectangle(cornerRadius: DS.radius.card, style: .continuous)
                    .fill(.quaternary.opacity(0.5)))
                .accessibilityLabel("Repository paths to analyze")
            Text("Read-only. e.g. /Users/you/Projects/my-app")
                .font(DS.font.caption).foregroundStyle(.tertiary)
        }
    }

    // MARK: Run options (autonomy · completeness · stop-at → run_config.json)

    private var runOptionsSection: some View {
        VStack(alignment: .leading, spacing: DS.space.xxs) {
            Text("Run options").font(DS.font.callout).foregroundStyle(.secondary)
            HStack(alignment: .top, spacing: DS.space.s) {
                VStack(alignment: .leading, spacing: DS.space.xxs) {
                    Text("Autonomy").font(DS.font.caption).foregroundStyle(.tertiary)
                    Picker("", selection: $autonomy) {
                        Text("Fully autonomous").tag("fully_autonomous")
                        Text("Semi-autonomous").tag("semi_autonomous")
                        Text("Manual").tag("manual")
                    }.labelsHidden().pickerStyle(.menu)
                        .accessibilityLabel("Autonomy")
                }
                VStack(alignment: .leading, spacing: DS.space.xxs) {
                    Text("Completeness").font(DS.font.caption).foregroundStyle(.tertiary)
                    Picker("", selection: $completeness) {
                        Text("Workflow default").tag("")
                        Text("Prototype").tag("prototype")
                        Text("MVP").tag("mvp")
                        Text("V1").tag("v1")
                        Text("Production draft").tag("production_draft")
                    }.labelsHidden().pickerStyle(.menu)
                        .accessibilityLabel("Completeness")
                }
                VStack(alignment: .leading, spacing: DS.space.xxs) {
                    Text("Stop at").font(DS.font.caption).foregroundStyle(.tertiary)
                    Picker("", selection: $stopAfter) {
                        Text("Run to end").tag("")
                        Text("Idea validated").tag("idea validated")
                        Text("Docs complete").tag("docs complete")
                        Text("Design complete").tag("design complete")
                        Text("Tech spec complete").tag("tech spec complete")
                        Text("MVP built").tag("mvp built")
                        Text("Production ready").tag("production ready")
                    }.labelsHidden().pickerStyle(.menu)
                        .accessibilityLabel("Stop at")
                }
            }
        }
    }

    // MARK: Routing preset (the sheet never shows the matrix — §4.5)

    private var routingSection: some View {
        VStack(alignment: .leading, spacing: DS.space.xxs) {
            Text("Routing").font(DS.font.callout).foregroundStyle(.secondary)
            Picker("Routing preset", selection: $routingPreset) {
                ForEach(Self.routingPresets, id: \.key) { p in
                    Text(p.label).tag(p.key)
                }
            }
            .pickerStyle(.segmented)
            .labelsHidden()
            Text("Customize after creation opens the Plan tab.")
                .font(DS.font.caption).foregroundStyle(.tertiary)
        }
    }

    // MARK: Queue position

    private var positionSection: some View {
        VStack(alignment: .leading, spacing: DS.space.xxs) {
            Text("Position").font(DS.font.callout).foregroundStyle(.secondary)
            Picker("Queue position", selection: $position) {
                ForEach(QueuePosition.allCases) { p in
                    Text(p.label).tag(p)
                }
            }
            .pickerStyle(.segmented)
            .labelsHidden()
            Text(positionCaption)
                .font(DS.font.caption).foregroundStyle(.tertiary)
                .monospacedDigit()
        }
    }

    // MARK: Create

    private func create() {
        var effectiveIdea = idea.trimmingCharacters(in: .whitespacesAndNewlines)
        if effectiveIdea.isEmpty && !attachedDocs.isEmpty {
            effectiveIdea = Self.docsPlaceholderPrompt
        }
        guard store.createFactoryApp(slug: slug, idea: effectiveIdea, workflow: workflow,
                                     docs: attachedDocs,
                                     backfillFromDocs: backfillFromDocs && !attachedDocs.isEmpty)
        else { return }
        // run_config.json (autonomy / completeness / stop-after) + target paths —
        // the same store calls the classic New-chat sheet made.
        store.writeRunConfig(project: slug, autonomy: autonomy,
                             completeness: completeness, stopAfter: stopAfter)
        if needsTargets,
           !targetPaths.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
            store.writeTargetPaths(project: slug, paths: targetPaths)
        }
        // Forward-compatible marker for M3's per-project RoutingMatrix: record
        // the chosen preset so the Plan tab can seed itself from it.
        if routingPreset != "balanced" {
            let url = store.rootURL.appendingPathComponent(slug)
                .appendingPathComponent("routing_preset.txt")
            try? (routingPreset + "\n").write(to: url, atomically: true, encoding: .utf8)
        }
        switch position {
        case .endOfQueue:
            break   // createFactoryApp already appended to .orch-queue-order.json
        case .next:
            store.prioritizeInQueueOrder(slug)
        case .runNow:
            store.runOrQueue(slug)
        }
        onCreated(slug)
        dismiss()
    }

    private func firstLine(_ s: String) -> String {
        for raw in s.components(separatedBy: "\n") {
            let line = raw.trimmingCharacters(in: CharacterSet(charactersIn: "# ").union(.whitespaces))
            if !line.isEmpty { return String(line.prefix(48)) }
        }
        return ""
    }
}
