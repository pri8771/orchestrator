// V3 board 3.8 (sub-PR 1): the section rail — sections discovered from
// the engine's sections/ dir, with a truthful one-line live status per
// section derived from the SAME run state the store already parses
// (R2: a section with no running chat shows idle, never a stale
// "running"). All state is an explicit enum (R4) — loading resolves to
// populated, empty, or error; no spinner that never ends (§12.1).

import Foundation

/// One discovered section (sections/<id>/section.json identity fields).
struct SectionMeta: Equatable, Identifiable {
    let id: String
    let title: String
}

/// Explicit rail state (R4): never boolean soup.
enum SectionRailState: Equatable {
    case loading
    case empty                       // no sections/ dir or nothing in it
    case populated([SectionMeta])
    case error(String)               // unreadable dir / malformed identity
}

enum SectionRailLogic {
    /// Discover sections from the ENGINE dir. Skips _template and dot
    /// dirs; a section.json that fails to parse contributes an .error
    /// state naming the file rather than silently vanishing (the loader
    /// would banner-fallback at runtime — the rail must not pretend the
    /// section is healthy OR absent).
    static func discover(sectionsDirURL: URL) -> SectionRailState {
        let fm = FileManager.default
        guard let names = try? fm.contentsOfDirectory(
            atPath: sectionsDirURL.path) else { return .empty }
        var metas: [SectionMeta] = []
        for name in names.sorted() {
            if name.hasPrefix(".") || name.hasPrefix("_") { continue }
            let manifest = sectionsDirURL.appendingPathComponent(
                "\(name)/section.json")
            guard fm.fileExists(atPath: manifest.path) else { continue }
            guard let data = try? Data(contentsOf: manifest),
                  let obj = try? JSONSerialization.jsonObject(with: data),
                  let dict = obj as? [String: Any] else {
                return .error("sections/\(name)/section.json does not parse "
                              + "— run --lint-section \(name)")
            }
            metas.append(SectionMeta(
                id: (dict["id"] as? String) ?? name,
                title: (dict["title"] as? String) ?? name.capitalized))
        }
        return metas.isEmpty ? .empty : .populated(metas)
    }

    /// The chats belonging to a section: nested ids "proj/<section>/chat"
    /// plus the M1 legacy flat "proj--<section>--chat" convention.
    static func belongs(_ projectName: String, toSection section: String)
        -> Bool {
        let parts = projectName.contains("/")
            ? projectName.components(separatedBy: "/")
            : projectName.components(separatedBy: "--")
        return parts.count == 3 && parts[1] == section
    }

    /// Truthful one-line status ("debating, round 3" per the sections
    /// plan §10) derived from live Project state only.
    static func statusLine(section: String, projects: [Project]) -> String {
        let mine = projects.filter { belongs($0.name, toSection: section) }
        if mine.isEmpty { return "no chats yet" }
        if let waiting = mine.first(where: { $0.awaitingHuman != nil }) {
            return "\(chatLabel(waiting.name)) — waiting for you"
        }
        if let live = mine.first(where: { $0.running }) {
            let phase = live.currentPhase.map { live.titleFor($0) }
                ?? "starting"
            let round = live.currentRound > 0
                ? ", round \(live.currentRound)" : ""
            return "\(chatLabel(live.name)) — \(phase.lowercased())\(round)"
        }
        return mine.count == 1 ? "1 chat, idle" : "\(mine.count) chats, idle"
    }

    static func chatLabel(_ projectName: String) -> String {
        let parts = projectName.contains("/")
            ? projectName.components(separatedBy: "/")
            : projectName.components(separatedBy: "--")
        return parts.count == 3 ? parts[2] : projectName
    }
}

import SwiftUI

// The center pane for a selected section: its chats across every project,
// with truthful per-chat status; opening routes to the project transcript.
struct SectionChatsView: View {
    @EnvironmentObject var store: OrchestratorStore
    let section: String
    let onOpenChat: (String) -> Void
    @State private var showSettings = false

    private var chats: [Project] {
        store.projects.filter {
            SectionRailLogic.belongs($0.name, toSection: section)
        }
    }

    var body: some View {
        Group {
            if chats.isEmpty {
                EmptyStateView(
                    symbol: "bubble.left.and.bubble.right",
                    title: "No chats in this section yet",
                    message: "Start a chat from Home and pick this section — "
                             + "it will appear here.")
            } else {
                List(chats) { chat in
                    Button {
                        onOpenChat(chat.name)
                    } label: {
                        HStack(spacing: DS.space.s) {
                            VStack(alignment: .leading, spacing: 2) {
                                Text(SectionRailLogic.chatLabel(chat.name))
                                    .font(DS.font.body)
                                Text(chat.name)
                                    .font(DS.font.caption)
                                    .foregroundStyle(.tertiary)
                            }
                            Spacer()
                            Text(chat.running
                                 ? (chat.currentPhase.map { chat.titleFor($0) }
                                    ?? "starting")
                                 : (chat.awaitingHuman != nil
                                    ? "waiting for you" : "idle"))
                                .font(DS.font.caption)
                                .foregroundStyle(chat.running
                                                 ? DS.accent.color
                                                 : .secondary)
                        }
                        .contentShape(Rectangle())
                    }
                    .buttonStyle(.plain)
                }
                .listStyle(.inset)
            }
        }
        .navigationTitle(section.capitalized)
        .toolbar {
            ToolbarItem(placement: .primaryAction) {
                Button {
                    showSettings = true
                } label: {
                    Label("Section Settings", systemImage: "slider.horizontal.3")
                }
                .help("Edit this section's phases, models, and rules")
            }
        }
        .sheet(isPresented: $showSettings) {
            SectionSettingsSheet(section: SectionMeta(
                id: section, title: section.capitalized))
                .environmentObject(store)
        }
    }
}
