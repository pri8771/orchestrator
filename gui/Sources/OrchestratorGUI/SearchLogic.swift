// V3 board 2.6 (GUI): transcript search over the engine's search.py index.
//
// Pure, store-free logic so tests never instantiate OrchestratorStore:
// parsing of the frozen `search.py --query … --json` contract, the
// degraded-status detection the palette must SURFACE (an unavailable
// index is a warning row, never silently-empty results), and the
// anchor→message matching that turns a hit into a transcript scroll
// target. The store owns only the Process spawn and the stale-query
// generation guard around these.

import Foundation

/// One hit from search.py --json. Field set is the frozen CLI contract.
struct SearchHit: Equatable, Hashable, Identifiable {
    let project: String
    let phase: String
    let round: Int
    let agent: String
    let kind: String
    let turnId: String
    let contentPath: String
    let snippet: String

    /// Stable across projects: two projects can share a turn_id.
    var id: String { project + "|" + turnId }
}

/// Where a selected hit should land the user.
struct TranscriptAnchor: Equatable {
    let project: String
    let phase: String
    let round: Int
    let agent: String
}

enum SearchResultParser {
    struct Result: Equatable {
        var status: String
        var hits: [SearchHit]
    }

    /// nil = malformed payload (treated as a failed query, not as empty).
    static func parse(_ data: Data) -> Result? {
        guard let obj = try? JSONSerialization.jsonObject(with: data),
              let dict = obj as? [String: Any],
              let status = dict["status"] as? String,
              let rawHits = dict["hits"] as? [[String: Any]] else { return nil }
        let hits = rawHits.compactMap { h -> SearchHit? in
            guard let project = h["project"] as? String,
                  let turnId = h["turn_id"] as? String else { return nil }
            return SearchHit(project: project,
                             phase: h["phase"] as? String ?? "",
                             round: h["round"] as? Int ?? 0,
                             agent: h["agent"] as? String ?? "",
                             kind: h["kind"] as? String ?? "",
                             turnId: turnId,
                             contentPath: h["content_path"] as? String ?? "",
                             snippet: h["snippet"] as? String ?? "")
        }
        return Result(status: status, hits: hits)
    }

    static func isDegraded(_ status: String) -> Bool {
        status.hasPrefix("degraded")
    }

    /// Palette row subtitle: "gloam · design · r2 · codex".
    static func detail(for hit: SearchHit) -> String {
        var parts = [hit.project, hit.phase]
        if hit.round > 0 { parts.append("r\(hit.round)") }
        if !hit.agent.isEmpty { parts.append(hit.agent) }
        return parts.filter { !$0.isEmpty }.joined(separator: " · ")
    }

    static func anchor(for hit: SearchHit) -> TranscriptAnchor {
        TranscriptAnchor(project: hit.project, phase: hit.phase,
                         round: hit.round, agent: hit.agent)
    }
}

enum SearchAnchorLogic {
    /// The message a transcript should scroll to for an anchor: the first
    /// bubble of the anchor's round section whose header names the agent,
    /// falling back to the section's first bubble, else nil (phase-level
    /// landing only — never a wrong scroll).
    static func messageID(for anchor: TranscriptAnchor,
                          in messages: [ChatMessage]) -> Int? {
        guard anchor.round > 0 else { return nil }
        let wanted = ["Round \(anchor.round)", "Iteration \(anchor.round)"]
        let inSection = messages.filter { m in
            wanted.contains(where: { m.section.hasSuffix($0) || m.section == $0 })
        }
        guard !inSection.isEmpty else { return nil }
        let agent = anchor.agent.lowercased()
        if !agent.isEmpty,
           let exact = inSection.first(where: {
               $0.header.lowercased().contains(agent)
           }) {
            return exact.id
        }
        return inSection.first?.id
    }
}
