// V3 board 3.8 (sub-PR 2): the Section Settings sheet — three tabs that
// RE-SKIN the proven editors (WorkflowBuilderSheet, RoutingGridView with
// the new .section scope, a PromptSnippet-style rules editor) plus lint
// surfacing from `--lint-section --lint-json`. Every save writes through
// the store and RE-READS to confirm (R2: "Saved" only after the disk
// says so); lint re-runs after each save.

import Foundation
import SwiftUI

// MARK: - Lint model (pure, testable)

struct SectionLintEntry: Equatable, Identifiable {
    let severity: String
    let file: String
    let field: String
    let message: String
    var id: String { "\(severity)|\(file)|\(field)|\(message)" }
}

struct SectionLintSummary: Equatable {
    var entries: [SectionLintEntry] = []
    var errors: Int { entries.filter { $0.severity == "error" }.count }
    var warnings: Int { entries.filter { $0.severity == "warning" }.count }
}

enum SectionLintParser {
    /// Parse `--lint-section --lint-json` stdout. nil = the run failed —
    /// callers surface that as unavailable, never as "clean" (R2).
    static func parse(_ data: Data) -> SectionLintSummary? {
        guard let obj = try? JSONSerialization.jsonObject(with: data),
              let dict = obj as? [String: Any],
              let report = dict["report"] as? [[String: Any]] else {
            return nil
        }
        var summary = SectionLintSummary()
        for e in report {
            summary.entries.append(SectionLintEntry(
                severity: e["severity"] as? String ?? "error",
                file: e["file"] as? String ?? "-",
                field: e["field"] as? String ?? "-",
                message: e["message"] as? String ?? ""))
        }
        return summary
    }
}

// MARK: - Rules editing model (pure, testable)

enum SectionRulesLogic {
    /// Load sections/<name>/rules.json phase entries as editable text:
    /// {phase: bullet-lines}. Missing file => empty (normal).
    static func load(rulesURL: URL) -> [String: String]? {
        guard let data = try? Data(contentsOf: rulesURL) else { return [:] }
        guard let obj = try? JSONSerialization.jsonObject(with: data),
              let dict = obj as? [String: Any],
              let phases = dict["phases"] as? [String: Any] else {
            return nil   // present but corrupt — the editor must say so
        }
        var out: [String: String] = [:]
        for (key, raw) in phases {
            let entry = raw as? [String: Any] ?? [:]
            let rules = entry["rules"] as? [String] ?? []
            out[key] = rules.joined(separator: "\n")
        }
        return out
    }

    /// Serialize edited bullet text back, preserving non-"rules" fields
    /// of each phase entry (required_output / acceptance_checks survive)
    /// AND unknown top-level keys — the shipped template carries "_comment",
    /// and the file is engine-seeded, not GUI-owned, so rebuilding the root
    /// destroyed hand-added notes (same merge discipline as
    /// ModelRouting.save). schema_version fills only when absent.
    static func save(edited: [String: String], rulesURL: URL) -> Bool {
        var root: [String: Any] = [:]
        if let data = try? Data(contentsOf: rulesURL),
           let obj = try? JSONSerialization.jsonObject(with: data),
           let dict = obj as? [String: Any] {
            root = dict
        }
        var phases = root["phases"] as? [String: Any] ?? [:]
        for (key, text) in edited {
            var entry = phases[key] as? [String: Any] ?? [:]
            let bullets = text.split(separator: "\n")
                .map { $0.trimmingCharacters(in: .whitespaces) }
                .filter { !$0.isEmpty }
            entry["rules"] = bullets
            phases[key] = entry
        }
        root["phases"] = phases
        if root["schema_version"] == nil { root["schema_version"] = 1 }
        guard let out = try? JSONSerialization.data(
            withJSONObject: root, options: [.prettyPrinted, .sortedKeys])
        else { return false }
        do {
            try FileManager.default.createDirectory(
                at: rulesURL.deletingLastPathComponent(),
                withIntermediateDirectories: true)
            // .atomic: a crash mid-write must not leave a torn rules.json —
            // phase_rules._load_layer treats that as corrupt and disables
            // the section's rules overlay until hand-repaired.
            try out.write(to: rulesURL, options: .atomic)
        } catch { return false }
        // R2: confirm by re-reading — a write that does not read back
        // identical is a failure, never a silent "Saved".
        return (try? Data(contentsOf: rulesURL)) == out
    }
}

// MARK: - The sheet

struct SectionSettingsSheet: View {
    @EnvironmentObject var store: OrchestratorStore
    @Environment(\.dismiss) private var dismiss
    let section: SectionMeta

    private enum Tab: String, CaseIterable {
        case phases = "Phases", models = "Models", rules = "Rules",
             lint = "Lint"
    }
    @State private var tab: Tab = .rules
    @State private var editedRules: [String: String] = [:]
    @State private var rulesCorrupt = false
    @State private var saveState: SaveState = .idle

    enum SaveState: Equatable { case idle, saved, failed(String) }

    private var rulesURL: URL {
        store.orchDirURL.appendingPathComponent(
            "sections/\(section.id)/rules.json")
    }

    var body: some View {
        VStack(spacing: 0) {
            HStack {
                Text("\(section.title) — Section Settings")
                    .font(DS.font.title)
                Spacer()
                Picker("", selection: $tab) {
                    ForEach(Tab.allCases, id: \.self) {
                        Text($0.rawValue).tag($0)
                    }
                }
                .pickerStyle(.segmented)
                .frame(width: 320)
                Button("Done") { dismiss() }
            }
            .padding(DS.space.s)
            Divider()
            content
        }
        .frame(minWidth: 760, minHeight: 520)
        .onAppear {
            reloadRules()
            store.lintSection(section.id)
        }
    }

    @ViewBuilder
    private var content: some View {
        switch tab {
        case .phases:
            WorkflowBuilderSheet(initialSelection: workflowName)
        case .models:
            if let wf = store.workflows.first(where: {
                $0.name == workflowName
            }) {
                RoutingGridView(scope: .section(name: section.id),
                                workflow: wf)
                    .padding(DS.space.s)
            } else {
                EmptyStateView(symbol: "cpu",
                               title: "Workflow not loaded",
                               message: "The section's workflow "
                                        + "'\(workflowName)' isn't available.")
            }
        case .rules:
            rulesTab
        case .lint:
            lintTab
        }
    }

    private var workflowName: String {
        // The manifest's workflow field (name form); inline workflows edit
        // via the manifest file itself.
        let url = store.orchDirURL.appendingPathComponent(
            "sections/\(section.id)/section.json")
        guard let data = try? Data(contentsOf: url),
              let obj = try? JSONSerialization.jsonObject(with: data),
              let dict = obj as? [String: Any],
              let name = dict["workflow"] as? String else { return "" }
        return name
    }

    // MARK: Rules tab

    private func reloadRules() {
        if let loaded = SectionRulesLogic.load(rulesURL: rulesURL) {
            editedRules = loaded
            rulesCorrupt = false
        } else {
            rulesCorrupt = true
        }
    }

    private var rulesTab: some View {
        VStack(alignment: .leading, spacing: DS.space.s) {
            if rulesCorrupt {
                Label("sections/\(section.id)/rules.json does not parse — "
                      + "fix it on disk or run --lint-section \(section.id)",
                      systemImage: "exclamationmark.triangle")
                    .foregroundStyle(DS.status.warning.color)
                    .padding(DS.space.s)
            } else if editedRules.isEmpty {
                EmptyStateView(symbol: "list.bullet.rectangle",
                               title: "No phase rules yet",
                               message: "This section inherits the global "
                                        + "playbooks. Rules added here win "
                                        + "for its sessions.")
            } else {
                ScrollView {
                    VStack(alignment: .leading, spacing: DS.space.m) {
                        ForEach(editedRules.keys.sorted(), id: \.self) { key in
                            VStack(alignment: .leading,
                                   spacing: DS.space.xxs) {
                                Text(key).font(DS.font.headline)
                                TextEditor(text: Binding(
                                    get: { editedRules[key] ?? "" },
                                    set: { editedRules[key] = $0 }))
                                    .font(DS.font.monoInline)
                                    .frame(minHeight: 72)
                                    .overlay(RoundedRectangle(
                                        cornerRadius: DS.radius.control)
                                        .stroke(DS.hairline, lineWidth: 1))
                            }
                        }
                    }
                    .padding(DS.space.s)
                }
                HStack {
                    switch saveState {
                    case .idle: EmptyView()
                    case .saved:
                        Label("Saved", systemImage: "checkmark.circle")
                            .font(DS.font.caption)
                            .foregroundStyle(DS.status.success.color)
                    case .failed(let why):
                        Label(why, systemImage: "exclamationmark.triangle")
                            .font(DS.font.caption)
                            .foregroundStyle(DS.status.error.color)
                    }
                    Spacer()
                    Button("Save Rules") {
                        if SectionRulesLogic.save(edited: editedRules,
                                                  rulesURL: rulesURL) {
                            saveState = .saved
                            reloadRules()
                            store.lintSection(section.id)
                        } else {
                            saveState = .failed(
                                "Could not write rules.json — nothing "
                                + "was saved")
                        }
                    }
                    .keyboardShortcut("s", modifiers: .command)
                }
                .padding(DS.space.s)
            }
        }
    }

    // MARK: Lint tab

    private var lintTab: some View {
        Group {
            switch store.sectionLint[section.id] {
            case .none:
                Text("Linting…").foregroundStyle(.secondary)
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
            case .some(nil):
                Label("Lint unavailable — the engine could not run "
                      + "--lint-section", systemImage:
                      "exclamationmark.triangle")
                    .foregroundStyle(DS.status.warning.color)
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
            case .some(.some(let summary)):
                if summary.entries.isEmpty {
                    EmptyStateView(symbol: "checkmark.seal",
                                   title: "Lints clean",
                                   message: "No warnings or errors in this "
                                            + "section's manifests.")
                } else {
                    List(summary.entries) { e in
                        HStack(alignment: .top, spacing: DS.space.s) {
                            Image(systemName: e.severity == "error"
                                  ? "xmark.octagon"
                                  : "exclamationmark.triangle")
                                .foregroundStyle(e.severity == "error"
                                                 ? DS.status.error.color
                                                 : DS.status.warning.color)
                            VStack(alignment: .leading, spacing: 2) {
                                Text("\(e.file) · \(e.field)")
                                    .font(DS.font.caption)
                                    .foregroundStyle(.secondary)
                                Text(e.message).font(DS.font.body)
                            }
                        }
                    }
                    .listStyle(.inset)
                }
            }
        }
    }
}
