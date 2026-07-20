import Foundation

struct ChatMeta: Equatable {
    var pinned = false
    var tags: [String] = []
}

struct ChatMetaRead: Equatable {
    let meta: ChatMeta
    let warning: String?
}

struct ArchivedChat: Equatable, Identifiable {
    let id: String
    let section: String
    let dirURL: URL
}

struct ChatMetadataSnapshot: Equatable {
    var metadata: [String: ChatMeta] = [:]
    var warnings: [String: String] = [:]
    var archived: [ArchivedChat] = []
    var transcriptAvailable: Set<String> = []
}

enum ChatMetaDocument {
    static let prefix = "<!-- chat-meta:"

    enum WriteError: LocalizedError {
        case changedDuringEdit

        var errorDescription: String? {
            "The chat changed while its metadata was being saved. Try again."
        }
    }

    static func transcriptURL(sessionDir: URL) -> URL {
        sessionDir.appendingPathComponent("chat/chat.md")
    }

    static func read(_ url: URL) -> ChatMetaRead {
        guard let data = try? Data(contentsOf: url), !data.isEmpty else {
            return ChatMetaRead(meta: ChatMeta(), warning: nil)
        }
        let first = firstLine(in: data)
        guard let line = String(data: first, encoding: .utf8),
              line.hasPrefix(prefix) else {
            return ChatMetaRead(meta: ChatMeta(), warning: nil)
        }
        guard line.hasSuffix(" -->") || line.hasSuffix("-->") else {
            return malformed(url)
        }
        let suffix = line.hasSuffix(" -->") ? 4 : 3
        let start = line.index(line.startIndex, offsetBy: prefix.count)
        let end = line.index(line.endIndex, offsetBy: -suffix)
        let json = line[start..<end].trimmingCharacters(in: .whitespaces)
        guard let data = json.data(using: .utf8),
              let obj = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any],
              let pinned = obj["pinned"] as? Bool,
              let tags = obj["tags"] as? [String],
              tags.allSatisfy({ !$0.isEmpty }) else { return malformed(url) }
        return ChatMetaRead(meta: ChatMeta(pinned: pinned, tags: tags), warning: nil)
    }

    static func write(_ meta: ChatMeta, to url: URL,
                      beforeCommit: (() -> Void)? = nil) throws {
        let fm = FileManager.default
        try fm.createDirectory(at: url.deletingLastPathComponent(),
                               withIntermediateDirectories: true)
        var invokedHook = false
        for _ in 0..<2 {
            let original = (try? Data(contentsOf: url)) ?? Data()
            let originalFingerprint = fingerprint(url)
            let replacement = try encodedLine(meta) + Data([0x0a])
                + bodyBytes(from: original)
            let temp = url.deletingLastPathComponent().appendingPathComponent(
                ".chat-meta-\(UUID().uuidString).tmp")
            do {
                try replacement.write(to: temp)
                if !invokedHook {
                    invokedHook = true
                    beforeCommit?()
                }
                guard fingerprint(url) == originalFingerprint else {
                    try? fm.removeItem(at: temp)
                    continue
                }
                if fm.fileExists(atPath: url.path) {
                    _ = try fm.replaceItemAt(url, withItemAt: temp)
                } else {
                    try fm.moveItem(at: temp, to: url)
                }
                guard read(url).meta == meta else { continue }
                return
            } catch {
                try? fm.removeItem(at: temp)
                throw error
            }
        }
        throw WriteError.changedDuringEdit
    }

    static func bodyBytes(from data: Data) -> Data {
        let first = firstLine(in: data)
        guard let line = String(data: first, encoding: .utf8),
              line.hasPrefix(prefix) else { return data }
        guard let newline = data.firstIndex(of: 0x0a) else { return Data() }
        return Data(data.suffix(from: data.index(after: newline)))
    }

    private static func encodedLine(_ meta: ChatMeta) throws -> Data {
        let json = try JSONSerialization.data(
            withJSONObject: ["pinned": meta.pinned, "tags": meta.tags],
            options: [.sortedKeys])
        var line = Data(prefix.utf8)
        line.append(Data(" ".utf8))
        line.append(json)
        line.append(Data(" -->".utf8))
        return line
    }

    private static func firstLine(in data: Data) -> Data {
        guard let newline = data.firstIndex(of: 0x0a) else { return data }
        var end = newline
        if end > data.startIndex, data[data.index(before: end)] == 0x0d {
            end = data.index(before: end)
        }
        return Data(data[..<end])
    }

    private static func fingerprint(_ url: URL) -> String? {
        guard let attrs = try? FileManager.default.attributesOfItem(atPath: url.path),
              let size = attrs[.size] as? NSNumber,
              let date = attrs[.modificationDate] as? Date else { return nil }
        return "\(size)|\(date.timeIntervalSince1970)"
    }

    private static func malformed(_ url: URL) -> ChatMetaRead {
        ChatMetaRead(meta: ChatMeta(), warning:
            "\(url.lastPathComponent) has malformed chat metadata; pins and tags are ignored")
    }
}

enum ChatMetadataIndex {
    static func scan(rootURL: URL) -> ChatMetadataSnapshot {
        let fm = FileManager.default
        guard let projects = try? fm.contentsOfDirectory(atPath: rootURL.path)
        else { return ChatMetadataSnapshot() }
        var result = ChatMetadataSnapshot()
        for project in projects.sorted() where SessionLayout.validSlug(project) {
            let pdir = rootURL.appendingPathComponent(project)
            if fm.fileExists(atPath: pdir.appendingPathComponent(
                    "initial_prompt/initial_prompt.md").path) {
                let parts = project.components(separatedBy: "--")
                if parts.count == 3 {
                    add(id: project, section: parts[1], dir: pdir,
                        archived: fm.fileExists(atPath: pdir.appendingPathComponent(
                            ".orch_archived").path), to: &result)
                }
                continue
            }
            if fm.fileExists(atPath: pdir.appendingPathComponent(".orch_archived").path) {
                continue
            }
            guard fm.fileExists(atPath: pdir.appendingPathComponent(
                    SessionLayout.sectionsMarker).path),
                  let sections = try? fm.contentsOfDirectory(atPath: pdir.path)
            else { continue }
            for section in sections.sorted() where SessionLayout.validSlug(section) {
                let sdir = pdir.appendingPathComponent(section)
                if fm.fileExists(atPath: sdir.appendingPathComponent(".orch_archived").path) {
                    continue
                }
                guard let chats = try? fm.contentsOfDirectory(atPath: sdir.path) else { continue }
                for chat in chats.sorted() where SessionLayout.validSlug(chat) {
                    let cdir = sdir.appendingPathComponent(chat)
                    guard fm.fileExists(atPath: cdir.appendingPathComponent(
                        "initial_prompt/initial_prompt.md").path) else { continue }
                    add(id: "\(project)/\(section)/\(chat)", section: section,
                        dir: cdir, archived: fm.fileExists(atPath: cdir
                            .appendingPathComponent(".orch_archived").path), to: &result)
                }
            }
        }
        return result
    }

    private static func add(id: String, section: String, dir: URL,
                            archived: Bool, to result: inout ChatMetadataSnapshot) {
        let transcript = ChatMetaDocument.transcriptURL(sessionDir: dir)
        if FileManager.default.fileExists(atPath: transcript.path) {
            result.transcriptAvailable.insert(id)
        }
        let parsed = ChatMetaDocument.read(transcript)
        result.metadata[id] = parsed.meta
        if let warning = parsed.warning { result.warnings[id] = warning }
        if archived {
            result.archived.append(ArchivedChat(id: id, section: section, dirURL: dir))
        }
    }
}

enum ChatSidebarLogic {
    static func visibleIDs(_ ids: [String], metadata: [String: ChatMeta],
                           tag: String?) -> [String] {
        let filtered = ids.filter { id in
            guard let tag, !tag.isEmpty else { return true }
            return (metadata[id]?.tags ?? []).contains {
                $0.caseInsensitiveCompare(tag) == .orderedSame
            }
        }
        return filtered.sorted { left, right in
            let lp = metadata[left]?.pinned ?? false
            let rp = metadata[right]?.pinned ?? false
            if lp != rp { return lp && !rp }
            let ll = SectionRailLogic.chatLabel(left)
            let rl = SectionRailLogic.chatLabel(right)
            let order = ll.localizedCaseInsensitiveCompare(rl)
            return order == .orderedSame ? left < right : order == .orderedAscending
        }
    }

    static func availableTags(_ ids: [String], metadata: [String: ChatMeta]) -> [String] {
        Array(Set(ids.flatMap { metadata[$0]?.tags ?? [] })).sorted {
            $0.localizedCaseInsensitiveCompare($1) == .orderedAscending
        }
    }
}
