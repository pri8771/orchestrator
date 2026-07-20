import Foundation

// Shared by 4.11's session verb and 4.9's cards/drop targets. Eligibility is
// conservative: if the meta graph cannot prove one live final head, the
// command stays hidden and the engine remains the final authority on route.

struct ArtifactRouteRef: Equatable, Identifiable {
    let id: String
    let type: String
    let version: Int
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
        let superseded = Set(metas.compactMap { meta -> String? in
            guard let parent = meta["supersedes"] as? String, ids.contains(parent) else { return nil }
            return parent
        })
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
            process.waitUntilExit()
            let data = pipe.fileHandleForReading.readDataToEndOfFile()
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
