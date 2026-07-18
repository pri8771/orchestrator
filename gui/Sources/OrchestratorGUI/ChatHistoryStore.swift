import Foundation

// V3 board 1.4: per-chat history persistence, factored out of
// OrchestratorStore so tests can exercise it against a temp directory —
// never the real Application Support path (the store is @MainActor and its
// URLs point at live user data; instantiating it in tests would overwrite
// the developer's actual chat history).
//
// KEY SPACE NOTE (binding): these keys name GUI-side concierge
// conversations ("home" is the legacy Chat Home thread). They are a
// DIFFERENT namespace from engine chat-session directory names
// (<project>--<section>--<chat-slug>, GLOSSARY.md "Layout (M1 interim)") —
// an engine chat's history IS its transcript on disk and never flows
// through this store.
struct ChatHistoryStore {
    let baseDir: URL

    // Pure key→file mapping. Keys are slugified for filename safety; the
    // "chat-" prefix namespaces the files so no future key can collide with
    // anything else that lands in this directory.
    nonisolated static func fileURL(for key: String, baseDir: URL) -> URL {
        let safe = OrchestratorStore.slugify(key)
        return baseDir.appendingPathComponent("chat-\(safe).json")
    }

    func fileURL(for key: String) -> URL {
        Self.fileURL(for: key, baseDir: baseDir)
    }

    // Returns nil when the key has no file — the caller must reset the
    // visible messages to [] rather than keeping the previous chat's
    // messages on screen (review finding: the old early-return kept them).
    func load(key: String) -> [ConciergeMessage]? {
        guard let data = try? Data(contentsOf: fileURL(for: key)),
              let messages = try? JSONDecoder().decode([ConciergeMessage].self, from: data)
        else { return nil }
        return messages
    }

    func save(_ messages: [ConciergeMessage], key: String) throws {
        let data = try JSONEncoder().encode(messages)
        try FileManager.default.createDirectory(at: baseDir,
                                                withIntermediateDirectories: true)
        try data.write(to: fileURL(for: key))
    }

    // Late concierge reply for a chat that is no longer current: merge it
    // straight into that chat's file so the reply lands where it was asked
    // (§12.2 — old requests must not overwrite new state).
    func append(_ message: ConciergeMessage, key: String) throws {
        var messages = load(key: key) ?? []
        messages.append(message)
        try save(messages, key: key)
    }

    // One-shot upgrade: the pre-1.4 build kept ONE global history file. It
    // becomes the Home chat's history. The legacy file is left in place
    // (harmless, and a downgrade keeps working); the copy happens only while
    // no home file exists yet, so it can never clobber post-upgrade history.
    func migrateLegacyIfNeeded(legacyURL: URL, homeKey: String) {
        let home = fileURL(for: homeKey)
        guard !FileManager.default.fileExists(atPath: home.path),
              let data = try? Data(contentsOf: legacyURL),
              (try? JSONDecoder().decode([ConciergeMessage].self, from: data)) != nil
        else { return }
        try? FileManager.default.createDirectory(at: baseDir,
                                                 withIntermediateDirectories: true)
        try? data.write(to: home)
    }
}
