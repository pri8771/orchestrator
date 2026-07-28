import Foundation

// Shared by 4.11's session verb and 4.9's cards/drop targets. Eligibility is
// conservative: if the meta graph cannot prove one live final head, the
// command stays hidden and the engine remains the final authority on route.

struct ArtifactRouteRef: Equatable, Identifiable {
    let id: String
    let type: String
    let version: Int
}

struct ArtifactSummary: Equatable, Identifiable {
    let id: String
    let type: String
    let version: Int
    let status: String
    let stale: Bool
    let intentStale: Bool
    let lineage: [String]
    let sourcePhase: String
    let sourceSession: String
    let finalizationPolicy: String
    let unreadableReason: String?

    var canHumanFinalize: Bool {
        unreadableReason == nil && ["pending_review", "draft"].contains(status)
            && ["auto_final_on_consensus", "requires_review_gate", "requires_human"]
                .contains(finalizationPolicy)
    }
}

enum ArtifactCardState: Equatable {
    case pendingReview, final, converged, stale, intentStale, unreadable
    case routing(target: String), routed(target: String), refused(reason: String)

    static func resolve(summary: ArtifactSummary,
                        route: ArtifactRouteState?) -> ArtifactCardState {
        if let route {
            switch route {
            case .routing(let target): return .routing(target: target)
            case .routed(let target): return .routed(target: target)
            case .refused(let reason): return .refused(reason: reason)
            }
        }
        if summary.unreadableReason != nil { return .unreadable }
        if summary.stale { return .stale }
        if summary.intentStale { return .intentStale }
        switch summary.status {
        case "final": return .final
        case "converged": return .converged
        default: return .pendingReview
        }
    }
}

enum ArtifactIndex {
    static func scan(rootURL: URL, projectNames: [String], registryURL: URL)
        -> [String: [ArtifactSummary]] {
        let ids = Set(projectNames.map {
            $0.components(separatedBy: "/").first ?? $0
        })
        let policies = registryPolicies(registryURL)
        return Dictionary(uniqueKeysWithValues: ids.map { projectID in
            (projectID, scanProject(rootURL.appendingPathComponent(projectID),
                                    policies: policies))
        })
    }

    static func scanProject(_ projectDir: URL,
                            policies: [String: String]) -> [ArtifactSummary] {
        let root = projectDir.appendingPathComponent("artifacts", isDirectory: true)
        let fm = FileManager.default
        guard let names = try? fm.contentsOfDirectory(atPath: root.path) else { return [] }
        var parsed: [(name: String, meta: [String: Any])] = []
        var unreadable: [ArtifactSummary] = []
        for name in names.sorted() where !name.hasPrefix(".") {
            let url = root.appendingPathComponent(name).appendingPathComponent("meta.json")
            guard let data = try? Data(contentsOf: url),
                  var meta = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any]
            else {
                unreadable.append(ArtifactSummary(
                    id: name, type: "artifact", version: 0, status: "unreadable",
                    stale: false, intentStale: false, lineage: [],
                    sourcePhase: "", sourceSession: "",
                    finalizationPolicy: "", unreadableReason: "meta.json is unreadable"))
                continue
            }
            if meta["id"] == nil { meta["id"] = name }
            parsed.append((name, meta))
        }
        let ids = Set(parsed.compactMap { $0.meta["id"] as? String })
        var liveChildren: [String: Int] = [:]
        var reconciled = Set<String>()
        for item in parsed {
            if let parent = item.meta["supersedes"] as? String,
               ids.contains(parent),
               (item.meta["status"] as? String) != "converged" {
                liveChildren[parent, default: 0] += 1
            }
            let parents = (item.meta["parents"] as? [String])
                ?? ((item.meta["fields"] as? [String: Any])?["parents"] as? [String]) ?? []
            reconciled.formUnion(parents.filter(ids.contains))
        }
        // Match artifacts.is_stale: one live linear child is authoritative;
        // two branch heads are unresolved and must not make the parent stale.
        let stale = Set(ids.filter { liveChildren[$0] == 1 || reconciled.contains($0) })
        let byID = Dictionary(uniqueKeysWithValues: parsed.compactMap { item ->
            (String, [String: Any])? in
            guard let id = item.meta["id"] as? String else { return nil }
            return (id, item.meta)
        })
        let summaries = parsed.map { item -> ArtifactSummary in
            let meta = item.meta
            let id = (meta["id"] as? String) ?? item.name
            let source = meta["source"] as? [String: Any]
            let type = (meta["type"] as? String) ?? "artifact"
            let planRef = meta["plan_ref"] as? [String: Any]
            let planID = planRef?["plan_id"] as? String
            let citedVersion = planRef?["plan_version"] as? Int
            let planMeta = planID.flatMap { byID[$0] }
            let intentStale = planID.map(stale.contains) ?? false
                || (citedVersion != nil && planMeta != nil
                    && (planMeta?["version"] as? Int) != citedVersion)
            return ArtifactSummary(
                id: id, type: type, version: (meta["version"] as? Int) ?? 1,
                status: (meta["status"] as? String) ?? "pending_review",
                stale: stale.contains(id), intentStale: intentStale,
                lineage: (meta["lineage"] as? [String]) ?? [id],
                sourcePhase: (source?["phase"] as? String) ?? "",
                sourceSession: (source?["session"] as? String) ?? "",
                finalizationPolicy: policies[type] ?? "requires_human",
                unreadableReason: nil)
        }
        return (summaries + unreadable).sorted { left, right in
            if left.version != right.version { return left.version > right.version }
            return left.id < right.id
        }
    }

    private static func registryPolicies(_ url: URL) -> [String: String] {
        guard let data = try? Data(contentsOf: url),
              let root = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any],
              let types = root["types"] as? [String: Any] else { return [:] }
        return types.reduce(into: [:]) { out, pair in
            guard let entry = pair.value as? [String: Any],
                  let policy = entry["finalization"] as? String else { return }
            out[pair.key] = policy
        }
    }
}

struct ArtifactDragPayload: Codable, Equatable {
    let artifactID: String
    let type: String
    let version: Int
    let sourceSession: String

    func encode() -> String? {
        guard let data = try? JSONEncoder().encode(self) else { return nil }
        return "orchestrator-artifact:" + data.base64EncodedString()
    }

    static func decode(_ text: String) -> ArtifactDragPayload? {
        let prefix = "orchestrator-artifact:"
        guard text.hasPrefix(prefix),
              let data = Data(base64Encoded: String(text.dropFirst(prefix.count))) else { return nil }
        return try? JSONDecoder().decode(ArtifactDragPayload.self, from: data)
    }
}

enum ArtifactRouteState: Equatable {
    case routing(target: String)
    case routed(target: String)
    case refused(reason: String)
}

enum ArtifactRouteIndex {
    static func latestRoutable(projectDir: URL) -> ArtifactRouteRef? {
        let root = projectDir.appendingPathComponent("artifacts", isDirectory: true)
        let fm = FileManager.default
        guard let names = try? fm.contentsOfDirectory(atPath: root.path) else { return nil }
        var metas: [[String: Any]] = []
        for name in names.sorted() where !name.hasPrefix(".") {
            let url = root.appendingPathComponent(name).appendingPathComponent("meta.json")
            guard let data = try? Data(contentsOf: url),
                  var meta = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any]
            else { continue }
            if meta["id"] == nil { meta["id"] = name }
            metas.append(meta)
        }
        let ids = Set(metas.compactMap { $0["id"] as? String })
        var superseded = Set(metas.compactMap { meta -> String? in
            guard let parent = meta["supersedes"] as? String, ids.contains(parent) else { return nil }
            return parent
        })
        // Reconcile edges retire branch heads too: a reconcile artifact lists
        // the merged heads in fields.parents (its supersedes stays null),
        // matching ArtifactIndex.scanProject's staleness walk. Without this,
        // one branch+reconcile left rival "heads" behind forever and
        // permanently hid the project's routable artifact.
        for meta in metas {
            guard let fields = meta["fields"] as? [String: Any],
                  let parents = fields["parents"] as? [Any] else { continue }
            for parent in parents.compactMap({ $0 as? String })
            where ids.contains(parent) { superseded.insert(parent) }
        }
        let heads = metas.filter { meta in
            guard let id = meta["id"] as? String else { return false }
            return !superseded.contains(id) && (meta["status"] as? String) != "converged"
        }
        let headsByRoot = Dictionary(grouping: heads) { meta -> String in
            let lineage = meta["lineage"] as? [String]
            return lineage?.first ?? (meta["id"] as? String ?? "")
        }
        let candidates = heads.filter { meta in
            guard (meta["status"] as? String) == "final" else { return false }
            let lineage = meta["lineage"] as? [String]
            let rootID = lineage?.first ?? (meta["id"] as? String ?? "")
            return headsByRoot[rootID]?.count == 1
        }
        let newest = candidates.max { left, right in
            let lts = (left["ts"] as? String) ?? ""
            let rts = (right["ts"] as? String) ?? ""
            if lts != rts { return lts < rts }
            let lv = (left["version"] as? Int) ?? 1
            let rv = (right["version"] as? Int) ?? 1
            if lv != rv { return lv < rv }
            return ((left["id"] as? String) ?? "") < ((right["id"] as? String) ?? "")
        }
        guard let newest, let id = newest["id"] as? String else { return nil }
        return ArtifactRouteRef(id: id, type: (newest["type"] as? String) ?? "artifact",
                                version: (newest["version"] as? Int) ?? 1)
    }
}

enum ArtifactRouteCommand {
    static func arguments(engine: String, root: String, artifactID: String,
                          sourceSession: String, targetSession: String) -> [String] {
        [engine, "--root", root,
         "--route-artifact", artifactID,
         "--route-from", sourceSession,
         "--route-to", targetSession]
    }

    static func run(python: String, engine: String, root: String,
                    artifactID: String, sourceSession: String,
                    targetSession: String) -> (code: Int32, output: String) {
        let process = Process()
        process.executableURL = URL(fileURLWithPath: python)
        process.currentDirectoryURL = URL(fileURLWithPath: root, isDirectory: true)
        process.arguments = arguments(engine: engine, root: root,
                                      artifactID: artifactID,
                                      sourceSession: sourceSession,
                                      targetSession: targetSession)
        let pipe = Pipe()
        process.standardOutput = pipe
        process.standardError = pipe
        do {
            try process.run()
            // Read to EOF BEFORE waitUntilExit — the safe order when the
            // output could exceed the 64KB pipe buffer (waiting first
            // deadlocks child-writer against GUI-waiter).
            let data = pipe.fileHandleForReading.readDataToEndOfFile()
            process.waitUntilExit()
            return (process.terminationStatus,
                    String(data: data, encoding: .utf8) ?? "")
        } catch {
            return (127, error.localizedDescription)
        }
    }

    static func summary(_ output: String, fallback: String) -> String {
        output.split(separator: "\n").last.map(String.init) ?? fallback
    }
}

enum ArtifactFinalizeCommand {
    static func arguments(engine: String, root: String, artifactID: String,
                          sourceSession: String) -> [String] {
        [engine, "--root", root,
         "--finalize-artifact", artifactID,
         "--finalize-in", sourceSession,
         "--by", "human:gui", "--by-human"]
    }

    static func run(python: String, engine: String, root: String,
                    artifactID: String, sourceSession: String)
        -> (code: Int32, output: String) {
        let process = Process()
        process.executableURL = URL(fileURLWithPath: python)
        process.currentDirectoryURL = URL(fileURLWithPath: root, isDirectory: true)
        process.arguments = arguments(engine: engine, root: root,
                                      artifactID: artifactID,
                                      sourceSession: sourceSession)
        let pipe = Pipe()
        process.standardOutput = pipe
        process.standardError = pipe
        do {
            try process.run()
            // Read to EOF BEFORE waitUntilExit — same pipe-buffer rule as
            // ArtifactRouteCommand.run above.
            let data = pipe.fileHandleForReading.readDataToEndOfFile()
            process.waitUntilExit()
            return (process.terminationStatus,
                    String(data: data, encoding: .utf8) ?? "")
        } catch {
            return (127, error.localizedDescription)
        }
    }
}
