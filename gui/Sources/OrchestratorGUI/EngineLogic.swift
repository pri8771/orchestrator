import Foundation

// Pure, UI-free logic extracted from OrchestratorStore so it can be unit-tested
// (Tests/OrchestratorGUITests). Nothing in this file touches SwiftUI or the
// main actor: parsing verify_results.json, the approval decision-file contract,
// blocked_conflict parsing, and the engine-dir fallback precedence.

// MARK: - Verification results (<project>/verify_results.json)

// One record of the engine's verify_results.json array (verify.py:
// persist_verify_result). Parsed defensively: any missing/mistyped field falls
// back rather than dropping the record, and non-dict entries are skipped.
struct VerifyRecord: Equatable {
    var status: String          // "verified" | "failed" | "unverified"
    var ok: Bool
    var tool: String
    var scheme: String?
    var summary: String
    var errors: String          // possibly long compiler output
    var attempt: Int
    var repairAttempt: Bool
    var promptHash: String?
    var timestamp: String
    var phase: String?

    var statusLabel: String { status.uppercased() }
}

enum VerifyResultsParser {

    // Parse the raw file contents. Returns [] for empty data, malformed JSON,
    // or a JSON value that isn't an array — never throws.
    static func parse(_ data: Data) -> [VerifyRecord] {
        guard !data.isEmpty,
              let json = try? JSONSerialization.jsonObject(with: data),
              let array = json as? [[String: Any]] else {
            // A top-level array containing non-dict entries still yields the
            // dict ones; anything else is unusable.
            if let mixed = (try? JSONSerialization.jsonObject(with: data)) as? [Any] {
                return mixed.compactMap { ($0 as? [String: Any]).map(record(from:)) }
            }
            return []
        }
        return array.map(record(from:))
    }

    // Missing file (or unreadable) -> [].
    static func parse(fileAt url: URL) -> [VerifyRecord] {
        guard let data = try? Data(contentsOf: url) else { return [] }
        return parse(data)
    }

    // Records are appended oldest-first by the engine; the LATEST is what the
    // UI shows.
    static func latest(_ records: [VerifyRecord]) -> VerifyRecord? { records.last }

    static func repairAttemptCount(_ records: [VerifyRecord]) -> Int {
        records.filter { $0.repairAttempt }.count
    }

    private static func record(from obj: [String: Any]) -> VerifyRecord {
        let ran = (obj["ran"] as? Bool) ?? false
        let ok = (obj["ok"] as? Bool) ?? false
        // Prefer the recorded status; derive it (verify.py: verification_status)
        // when absent so older/partial records still classify sensibly.
        let status = (obj["status"] as? String)
            ?? (ran ? (ok ? "verified" : "failed") : "unverified")
        let attempt = (obj["attempt"] as? Int) ?? Int(obj["attempt"] as? Double ?? 0)
        return VerifyRecord(
            status: status,
            ok: ok,
            tool: (obj["tool"] as? String) ?? "none",
            scheme: obj["scheme"] as? String,
            summary: (obj["summary"] as? String) ?? "",
            errors: (obj["errors"] as? String) ?? "",
            attempt: attempt,
            repairAttempt: (obj["repair_attempt"] as? Bool) ?? (attempt > 0),
            promptHash: obj["prompt_hash"] as? String,
            timestamp: (obj["timestamp"] as? String) ?? (obj["ts"] as? String) ?? "",
            phase: obj["phase"] as? String)
    }
}

// MARK: - Approval decision files (spec §3.1)

// The engine polls <project>/approvals/ for one of three decision files:
//   <phase>.ok       — Approve: continue as-is.
//   <phase>.edit     — Edit & Approve: file BODY replaces the phase output.
//   <phase>.changes  — Request Changes: file body is human feedback; re-run.
enum ApprovalDecision: String, CaseIterable {
    case approve, editAndApprove, requestChanges

    var fileSuffix: String {
        switch self {
        case .approve: return "ok"
        case .editAndApprove: return "edit"
        case .requestChanges: return "changes"
        }
    }

    func fileName(phase: String) -> String { "\(phase).\(fileSuffix)" }
}

enum ApprovalFiles {
    static func url(projectDir: URL, phase: String, decision: ApprovalDecision) -> URL {
        projectDir.appendingPathComponent("approvals", isDirectory: true)
            .appendingPathComponent(decision.fileName(phase: phase))
    }

    // Creates approvals/ if needed and writes the decision file the engine polls.
    @discardableResult
    static func write(projectDir: URL, phase: String, decision: ApprovalDecision,
                      body: String) throws -> URL {
        let dir = projectDir.appendingPathComponent("approvals", isDirectory: true)
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        let dest = dir.appendingPathComponent(decision.fileName(phase: phase))
        try body.write(to: dest, atomically: true, encoding: .utf8)
        return dest
    }
}

// MARK: - blocked_conflict (agent_state.json)

// The engine pauses a build when merging lane worktrees hits a real conflict:
// agent_state.json gains blocked_conflict = {lane, files, detail} (or null).
struct BlockedConflict: Equatable {
    var lane: String
    var files: [String]
    var detail: String

    var filesDisplay: String { files.isEmpty ? "unknown files" : files.joined(separator: ", ") }

    // From the already-decoded top-level state dictionary. nil when the key is
    // absent, null, or not an object.
    static func parse(fromStateObject obj: [String: Any]) -> BlockedConflict? {
        guard let raw = obj["blocked_conflict"] as? [String: Any] else { return nil }
        var files: [String] = []
        if let list = raw["files"] as? [Any] {
            files = list.compactMap { $0 as? String }
        } else if let one = raw["files"] as? String, !one.isEmpty {
            files = [one]
        }
        return BlockedConflict(lane: (raw["lane"] as? String) ?? "?",
                               files: files,
                               detail: (raw["detail"] as? String) ?? "")
    }

    // From raw agent_state.json bytes; nil on malformed JSON.
    static func parse(stateData: Data) -> BlockedConflict? {
        guard let obj = (try? JSONSerialization.jsonObject(with: stateData)) as? [String: Any] else {
            return nil
        }
        return parse(fromStateObject: obj)
    }
}

// MARK: - Local models (engine `--doctor --json` → local_models block, spec §12/§27)

// One curated registry entry as the doctor report exposes it: the Ollama model
// tag, its human label, and whether `ollama pull` has already fetched it.
struct LocalModelEntry: Equatable, Identifiable {
    var id: String          // e.g. "qwen2.5-coder:7b" — the `ollama run` tag
    var label: String       // e.g. "Fast Local Coding Assistant"
    var installed: Bool
    var license: String
    var commercialUse: Bool
    var minRAMGB: Int?
    var recommendedRAMGB: Int?
    var sizeGB: Double?
    var contextTokens: Int?
    var roles: [String]
    var notes: String

    init(id: String, label: String = "", installed: Bool = false,
         license: String = "", commercialUse: Bool = false,
         minRAMGB: Int? = nil, recommendedRAMGB: Int? = nil,
         sizeGB: Double? = nil, contextTokens: Int? = nil,
         roles: [String] = [], notes: String = "") {
        self.id = id
        self.label = label
        self.installed = installed
        self.license = license
        self.commercialUse = commercialUse
        self.minRAMGB = minRAMGB
        self.recommendedRAMGB = recommendedRAMGB
        self.sizeGB = sizeGB
        self.contextTokens = contextTokens
        self.roles = roles
        self.notes = notes
    }

    // Registry ids are the ONLY thing the GUI ever splices into a pull command
    // (spec §12.3: no arbitrary shell input). Ollama tags are [name][:tag] with
    // dots/dashes/underscores/slashes — anything else means a tampered registry,
    // and the pull affordance refuses to run.
    var idIsSafeForShell: Bool {
        !id.isEmpty && id.allSatisfy {
            $0.isLetter && $0.isASCII || $0.isNumber && $0.isASCII
                || $0 == "." || $0 == "-" || $0 == "_" || $0 == ":" || $0 == "/"
        }
    }

    func fitLabel(totalRAMGB: Int) -> String {
        guard totalRAMGB > 0, let min = minRAMGB else { return "fit unknown" }
        if let rec = recommendedRAMGB, totalRAMGB >= rec {
            return "good fit"
        }
        if totalRAMGB >= min {
            return "tight fit"
        }
        return "too large"
    }
}

// The `local_models` block of the engine's `--doctor --json` report: server
// reachability, the selected model (config models.ollama) + whether it's
// pulled, and the curated registry with installed flags.
struct LocalModelsInfo: Equatable {
    var serverRunning: Bool
    var selected: String
    var selectedInstalled: Bool
    var registry: [LocalModelEntry]
}

// V3 6.1: one agent id's capability descriptor from the engine's doctor
// payload — the machine-readable truth about what the backend can do, so no
// GUI surface promises a feature (effort control, streaming, token metering,
// session continuity) the runner lacks.
struct AgentCapability: Equatable {
    var streams: Bool
    var tokenUsage: Bool
    var effortControl: Bool
    /// Mirrors the engine tri-state: "always" (JSON true), "never" (false),
    /// or "build_only" (codex — resumes only in write-enabled build phases).
    var sessionResume: String
}

struct AgentCapabilitiesInfo: Equatable {
    var agents: [String: AgentCapability]
    var dynamicPrefixes: [String: AgentCapability]

    /// Resolve like the engine's resolve_capabilities: exact id first, then
    /// prefix rules (local:/api:) with a non-empty remainder. nil for ids the
    /// engine doesn't describe — callers fall back to the static identity
    /// table and must not enable anything the static map disables.
    func capability(for rawKey: String) -> AgentCapability? {
        let key = rawKey.lowercased()
        if let hit = agents[key] { return hit }
        for (prefix, caps) in dynamicPrefixes
            where key.hasPrefix(prefix) && key.count > prefix.count { return caps }
        return nil
    }
}

// V3 6.3: ephemeral API preview state. The phase transcript remains the only
// final/persisted model; this value is reconstructed from .stream NDJSON only
// while the owning pane is focused.
struct StreamPreview: Equatable, Sendable {
    let agent: String
    let turnID: String
    let text: String
}

enum PaneTurnState: Equatable {
    case waiting(agent: String, live: Bool)
    case streaming(StreamPreview)
    case final

    static func resolve(isActive: Bool, agent: String?, live: Bool,
                        supportsStreams: Bool, preview: StreamPreview?) -> PaneTurnState {
        guard isActive, let agent else { return .final }
        if supportsStreams, let preview, preview.agent == agent, !preview.text.isEmpty {
            return .streaming(preview)
        }
        return .waiting(agent: agent, live: live)
    }
}

enum DoctorReportParser {

    // Parse a full doctor JSON report down to its local_models block. Returns
    // nil for malformed JSON or a report without the block (older engines) —
    // callers fall back to static recommendations. Field-level problems are
    // defaulted, not fatal, so a partially-populated block still renders.
    static func localModels(fromDoctorJSON data: Data) -> LocalModelsInfo? {
        guard let root = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any],
              let block = root["local_models"] as? [String: Any] else { return nil }
        var registry: [LocalModelEntry] = []
        for raw in (block["registry"] as? [Any]) ?? [] {
            guard let m = raw as? [String: Any],
                  let id = m["id"] as? String, !id.isEmpty else { continue }
            registry.append(LocalModelEntry(id: id,
                                            label: (m["label"] as? String) ?? "",
                                            installed: (m["installed"] as? Bool) ?? false,
                                            license: (m["license"] as? String) ?? "",
                                            commercialUse: (m["commercial_use"] as? Bool) ?? false,
                                            minRAMGB: m["min_ram_gb"] as? Int,
                                            recommendedRAMGB: m["recommended_ram_gb"] as? Int,
                                            sizeGB: m["size_gb"] as? Double,
                                            contextTokens: m["context_tokens"] as? Int,
                                            roles: (m["roles"] as? [String]) ?? [],
                                            notes: (m["notes"] as? String) ?? ""))
        }
        return LocalModelsInfo(serverRunning: (block["server_running"] as? Bool) ?? false,
                               selected: (block["selected"] as? String) ?? "",
                               selectedInstalled: (block["selected_installed"] as? Bool) ?? false,
                               registry: registry)
    }

    // V3 6.1: parse the agent_capabilities block ({"agents": {...},
    // "dynamic_prefixes": {...}}). nil when absent or empty (older engine) —
    // the GUI then serves the static identity table unchanged. Missing or
    // malformed fields default to the no-capability value, never to enabled:
    // a parse problem must not light up a control the backend may lack.
    static func agentCapabilities(fromDoctorJSON data: Data) -> AgentCapabilitiesInfo? {
        guard let root = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any],
              let block = root["agent_capabilities"] as? [String: Any] else { return nil }
        func caps(_ raw: Any) -> AgentCapability? {
            guard let m = raw as? [String: Any] else { return nil }
            let resume: String
            switch m["session_resume"] {
            case let flag as Bool: resume = flag ? "always" : "never"
            case let mode as String: resume = mode
            default: resume = "never"
            }
            return AgentCapability(streams: (m["streams"] as? Bool) ?? false,
                                   tokenUsage: (m["token_usage"] as? Bool) ?? false,
                                   effortControl: (m["effort_control"] as? Bool) ?? false,
                                   sessionResume: resume)
        }
        var agents: [String: AgentCapability] = [:]
        for (key, raw) in (block["agents"] as? [String: Any]) ?? [:] {
            if let c = caps(raw) { agents[key.lowercased()] = c }
        }
        var prefixes: [String: AgentCapability] = [:]
        for (key, raw) in (block["dynamic_prefixes"] as? [String: Any]) ?? [:] {
            if let c = caps(raw) { prefixes[key.lowercased()] = c }
        }
        if agents.isEmpty && prefixes.isEmpty { return nil }
        return AgentCapabilitiesInfo(agents: agents, dynamicPrefixes: prefixes)
    }
}

// MARK: - Markdown rendering (transcript bubbles)

// Agents emit Markdown; the transcript should render at least the inline
// styles (bold / italic / inline code) instead of showing literal syntax.
// Parsing failures fall back to the raw text — a bubble must never go blank.
enum MarkdownRenderer {
    private final class AttributedBox: NSObject {
        let value: AttributedString
        init(_ value: AttributedString) { self.value = value }
    }

    private final class SegmentsBox: NSObject {
        let value: [Segment]
        init(_ value: [Segment]) { self.value = value }
    }

    private static let attributedCache: NSCache<NSString, AttributedBox> = {
        let cache = NSCache<NSString, AttributedBox>()
        cache.countLimit = 1024
        cache.totalCostLimit = 8_000_000
        return cache
    }()

    private static let segmentsCache: NSCache<NSString, SegmentsBox> = {
        let cache = NSCache<NSString, SegmentsBox>()
        cache.countLimit = 1024
        cache.totalCostLimit = 4_000_000
        return cache
    }()

    static func attributed(_ body: String) -> AttributedString {
        let key = body as NSString
        if let cached = attributedCache.object(forKey: key) {
            return cached.value
        }
        let options = AttributedString.MarkdownParsingOptions(
            allowsExtendedAttributes: false,
            interpretedSyntax: .inlineOnlyPreservingWhitespace,
            failurePolicy: .returnPartiallyParsedIfPossible)
        let parsed = (try? AttributedString(markdown: body, options: options))
            ?? AttributedString(body)
        attributedCache.setObject(AttributedBox(parsed), forKey: key, cost: body.utf8.count)
        return parsed
    }

    // Fenced ``` blocks split out so the view can render them monospaced in
    // their own scrollable box. An unclosed fence just runs to the end.
    struct Segment: Equatable {
        let isCode: Bool
        let text: String
    }

    static func segments(_ body: String) -> [Segment] {
        let key = body as NSString
        if let cached = segmentsCache.object(forKey: key) {
            return cached.value
        }
        var out: [Segment] = []
        var current: [String] = []
        var inCode = false
        func flush() {
            let text = current.joined(separator: "\n")
            if !text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
                out.append(Segment(isCode: inCode, text: text))
            }
            current = []
        }
        for line in body.components(separatedBy: "\n") {
            if line.trimmingCharacters(in: .whitespaces).hasPrefix("```") {
                flush()
                inCode.toggle()
                continue
            }
            current.append(line)
        }
        flush()
        segmentsCache.setObject(SegmentsBox(out), forKey: key, cost: body.utf8.count)
        return out
    }
}

// MARK: - Run-log tail buffer

// The GUI keeps only the tail of a streaming run log. Trimming must happen on
// a LINE boundary — a mid-line byte cut used to leave a garbled first line.
enum RunLogBuffer {
    static let cap = 64_000
    static let keep = 48_000

    // Over `cap` characters -> keep the last `keep`, advanced to the next
    // newline so the retained text starts on a whole line.
    static func trim(_ log: String, cap: Int = cap, keep: Int = keep) -> String {
        guard log.count > cap else { return log }
        var tail = String(log.suffix(keep))
        if let nl = tail.firstIndex(of: "\n") {
            tail = String(tail[tail.index(after: nl)...])
        }
        return tail
    }
}

// MARK: - Generated Xcode project discovery ("Open in Xcode")

enum XcodeProjectLocator {
    // First *.xcodeproj (or *.xcworkspace, preferred) under the build dir,
    // searched shallowly (maxDepth levels) so a huge generated tree stays cheap.
    static func find(under buildDir: URL, maxDepth: Int = 3) -> URL? {
        let fm = FileManager.default
        var isDir: ObjCBool = false
        guard fm.fileExists(atPath: buildDir.path, isDirectory: &isDir), isDir.boolValue,
              let en = fm.enumerator(at: buildDir,
                                     includingPropertiesForKeys: [.isDirectoryKey],
                                     options: [.skipsHiddenFiles]) else { return nil }
        var project: URL? = nil
        for case let url as URL in en {
            if en.level > maxDepth { en.skipDescendants(); continue }
            switch url.pathExtension {
            case "xcworkspace":
                if !url.path.contains(".xcodeproj/") { return url }  // top-level workspace wins
            case "xcodeproj":
                if project == nil { project = url }
                en.skipDescendants()   // don't descend into the bundle
            default:
                break
            }
        }
        return project
    }
}

// MARK: - Engine directory precedence

// Where the GUI finds the engine (orchestrator.py + workflows/config/locks):
//   1. a bundled engine template in the .app's Resources (copied out to a
//      writable spot by the caller — the template itself is read-only), else
//   2. the first ancestor of the executable that contains orchestrator.py
//      (the repo layout: <engine>/gui/.build/<triple>/<config>/OrchestratorGUI), else
//   3. nothing — the caller shows a clear error. No hardcoded user paths.
enum EngineDirChoice: Equatable {
    case bundled(URL)   // the *template* inside the bundle; caller copies it out
    case repo(URL)
    case missing
}

enum EngineDirResolver {

    // Pure precedence over candidate paths; existence is injected so tests can
    // exercise every branch without touching the filesystem.
    static func pick(bundledTemplate: URL?, repoCandidates: [URL],
                     hasEngine: (URL) -> Bool) -> EngineDirChoice {
        if let tmpl = bundledTemplate, hasEngine(tmpl) { return .bundled(tmpl) }
        for candidate in repoCandidates where hasEngine(candidate) {
            return .repo(candidate)
        }
        return .missing
    }

    // Every ancestor directory of the executable, nearest first. Running from
    // source (`swift run` / .build) the engine dir is a few levels up; walking
    // all ancestors keeps this robust to debug/release/triple path shapes.
    static func repoLayoutCandidates(executableURL: URL?, maxDepth: Int = 8) -> [URL] {
        guard var dir = executableURL?.deletingLastPathComponent() else { return [] }
        var out: [URL] = []
        for _ in 0..<maxDepth {
            out.append(dir)
            let parent = dir.deletingLastPathComponent()
            if parent.path == dir.path { break }
            dir = parent
        }
        return out
    }
}
