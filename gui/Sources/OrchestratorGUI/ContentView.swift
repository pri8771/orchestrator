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
        HStack(spacing: 10) {
            Image(systemName: "pause.circle.fill").foregroundStyle(.orange)
                .accessibilityHidden(true)
            VStack(alignment: .leading, spacing: 1) {
                Text("Paused for your approval").font(.body).fontWeight(.medium)
                Text("Finished \(project.titleFor(phase)). Approve to continue, edit the output first, or send it back with feedback.")
                    .font(.subheadline).foregroundStyle(.secondary)
            }
            .accessibilityElement(children: .combine)
            Spacer()
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
        .padding(.horizontal, 14).padding(.vertical, 10)
        .background(Color.orange.opacity(0.12))
        .overlay(Divider(), alignment: .bottom)
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
        HStack(alignment: .top, spacing: 10) {
            Image(systemName: "exclamationmark.octagon.fill").foregroundStyle(.red)
                .accessibilityHidden(true)
            VStack(alignment: .leading, spacing: 1) {
                Text("Run aborted").font(.body).fontWeight(.medium)
                Text(message)
                    .font(.subheadline).foregroundStyle(.secondary)
                    .textSelection(.enabled)
                    .lineLimit(4)
                    .fixedSize(horizontal: false, vertical: true)
            }
            .accessibilityElement(children: .combine)
            .accessibilityLabel("Run aborted")
            .accessibilityValue(message)
            Spacer()
            Button {
                NSPasteboard.general.clearContents()
                NSPasteboard.general.setString(message, forType: .string)
            } label: {
                Label("Copy", systemImage: "doc.on.doc")
            }
            .accessibilityLabel("Copy error")
            .accessibilityHint("Copies the full error message to the clipboard")
        }
        .padding(.horizontal, 14).padding(.vertical, 10)
        .background(Color.red.opacity(0.12))
        .overlay(Divider(), alignment: .bottom)
    }
}

// MARK: - blocked_conflict banner (§18.3: paused on a real merge conflict)

struct ConflictBanner: View {
    let conflict: BlockedConflict

    var body: some View {
        HStack(alignment: .top, spacing: 10) {
            Image(systemName: "arrow.triangle.branch").foregroundStyle(.purple)
                .accessibilityHidden(true)
            VStack(alignment: .leading, spacing: 1) {
                Text("Build blocked: merge conflict on \(conflict.filesDisplay) (lane \(conflict.lane)) — resolve, then Resume.")
                    .font(.body).fontWeight(.medium)
                    .fixedSize(horizontal: false, vertical: true)
                if !conflict.detail.isEmpty {
                    Text(conflict.detail)
                        .font(.subheadline).foregroundStyle(.secondary)
                        .textSelection(.enabled)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
            .accessibilityElement(children: .combine)
            .accessibilityLabel("Build blocked by a merge conflict")
            .accessibilityValue("Files \(conflict.filesDisplay), lane \(conflict.lane). \(conflict.detail)")
            Spacer()
        }
        .padding(.horizontal, 14).padding(.vertical, 10)
        .background(Color.purple.opacity(0.12))
        .overlay(Divider(), alignment: .bottom)
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
        HStack(alignment: .top, spacing: 10) {
            Image(systemName: "exclamationmark.triangle.fill").foregroundStyle(.red)
                .accessibilityHidden(true)
            Text(message)
                .font(.subheadline)
                .textSelection(.enabled)
                .fixedSize(horizontal: false, vertical: true)
            Spacer()
        }
        .padding(.horizontal, 14).padding(.vertical, 8)
        .background(Color.red.opacity(0.12))
        .overlay(Divider(), alignment: .bottom)
        .accessibilityElement(children: .combine)
        .accessibilityLabel("Engine missing")
        .accessibilityValue(message)
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
