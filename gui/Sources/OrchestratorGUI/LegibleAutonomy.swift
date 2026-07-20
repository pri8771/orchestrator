import Foundation

// V3 4.10: pure readers behind the transcript's WHY / WHERE affordances.
// They deliberately accept Data so malformed disk input is fixture-testable
// and fails closed without coupling SwiftUI to a second workflow parser.

enum WorkflowPurposeParser {
    static func parse(_ data: Data) -> [String: String] {
        guard let root = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any],
              let phases = root["phases"] as? [[String: Any]] else { return [:] }
        var result: [String: String] = [:]
        for phase in phases {
            guard let key = phase["key"] as? String, !key.isEmpty,
                  let purpose = phase["purpose"] as? String else { continue }
            let trimmed = purpose.trimmingCharacters(in: .whitespacesAndNewlines)
            if !trimmed.isEmpty { result[key] = trimmed }
        }
        return result
    }
}

struct RoutePreviewContext {
    let section: String
    let artifactType: String
    let fleetRouting: Data?
    let projectRouting: Data?
}

/// Injectable now so 7.10 can substitute pending-route truth without changing
/// TranscriptView or FinalOutputCard.
struct RoutePreviewSource {
    let target: (RoutePreviewContext) -> String?

    static let defaults = RoutePreviewSource { context in
        RoutePreviewResolver.resolve(context)
    }
}

enum RoutePreviewResolver {
    static func resolve(_ context: RoutePreviewContext) -> String? {
        guard !context.section.isEmpty, !context.artifactType.isEmpty else { return nil }
        guard let fleet = routeMap(context.fleetRouting),
              let project = routeMap(context.projectRouting) else { return nil }
        var resolved = fleet
        for (kind, target) in project where !target.isEmpty { resolved[kind] = target }
        return resolved[context.artifactType]
    }

    /// nil Data means an absent optional layer; malformed JSON means the whole
    /// preview is unavailable. Showing an inherited route after a corrupt
    /// override would hide configuration damage and violate R2.
    private static func routeMap(_ data: Data?) -> [String: String]? {
        guard let data else { return [:] }
        guard let root = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any]
        else { return nil }

        // ONLY "artifact_routes" is recognized — the routing.json key 7.2
        // now writes (a flat {type: target} map, or a rule-array the
        // normalize overload below also accepts). It coexists with the
        // model-routing "phases" key in the same file; a pure model-routing
        // overlay (schema_version/enabled/fallback/phases, no
        // artifact_routes) yields [:] and the chip stays hidden — never a
        // route no engine would take (R2). The engine's rule objects live
        // under a sibling "rules" key this resolver deliberately ignores
        // (the chip previews only the single deterministic target).
        if let raw = root["artifact_routes"] as? [String: Any] { return normalize(raw) }
        if let raw = root["artifact_routes"] as? [[String: Any]] { return normalize(raw) }
        return [:]
    }

    private static func normalize(_ raw: [String: Any]) -> [String: String] {
        var out: [String: String] = [:]
        for (kind, value) in raw {
            if let target = value as? String {
                let clean = target.trimmingCharacters(in: .whitespacesAndNewlines)
                if !clean.isEmpty { out[kind] = clean }
            } else if let rule = value as? [String: Any],
                      let target = target(from: rule) {
                out[kind] = target
            }
        }
        return out
    }

    private static func normalize(_ raw: [[String: Any]]) -> [String: String] {
        var out: [String: String] = [:]
        for rule in raw {
            let match = rule["match"] as? [String: Any]
            let kind = (match?["artifact_type"] as? String)
                ?? (rule["artifact_type"] as? String)
            guard let kind, !kind.isEmpty, let target = target(from: rule) else { continue }
            out[kind] = target
        }
        return out
    }

    private static func target(from rule: [String: Any]) -> String? {
        let raw = (rule["target"] as? String) ?? (rule["target_section"] as? String)
        let clean = raw?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        return clean.isEmpty ? nil : clean
    }
}

enum ArtifactTypeHintParser {
    static func parse(finalOutput: String) -> String? {
        let marker = "```artifact-json"
        guard let start = finalOutput.range(of: marker),
              let end = finalOutput.range(of: "```", range: start.upperBound..<finalOutput.endIndex)
        else { return nil }
        let payload = finalOutput[start.upperBound..<end.lowerBound]
            .trimmingCharacters(in: .whitespacesAndNewlines)
        guard let data = payload.data(using: .utf8),
              let obj = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any]
        else { return nil }
        if let type = obj["type"] as? String, !type.isEmpty { return type }
        if let artifacts = obj["artifacts"] as? [[String: Any]], artifacts.count == 1,
           let type = artifacts[0]["type"] as? String, !type.isEmpty { return type }
        return nil
    }
}
