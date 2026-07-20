import Foundation
import AppKit
import Combine
import UserNotifications
import SwiftUI

private enum BackgroundProjectLoader {
    static func discoverApps(rootURL: URL) -> [String] {
        // V3 3.0: flat + nested (marker-gated) — one shared implementation.
        SessionLayout.discoverApps(rootURL: rootURL)
    }

    static func loadProjects(names: [String],
                             rootURL: URL,
                             workflowsByName: [String: WorkflowDef],
                             defaultWorkflow: WorkflowDef?,
                             manualStops: [String: Date],
                             runningProcessNames: Set<String>) -> [Project] {
        guard !names.isEmpty else { return [] }
        let lock = NSLock()
        var results = Array<Project?>(repeating: nil, count: names.count)
        DispatchQueue.concurrentPerform(iterations: names.count) { idx in
            let name = names[idx]
            let project = loadProject(name: name,
                                      rootURL: rootURL,
                                      workflowsByName: workflowsByName,
                                      defaultWorkflow: defaultWorkflow,
                                      stopAt: manualStops[name],
                                      processRunning: runningProcessNames.contains(name))
            lock.lock()
            results[idx] = project
            lock.unlock()
        }
        return results.compactMap { $0 }
    }

    private static func loadProject(name: String,
                                    rootURL: URL,
                                    workflowsByName: [String: WorkflowDef],
                                    defaultWorkflow: WorkflowDef?,
                                    stopAt: Date?,
                                    processRunning: Bool) -> Project? {
        let fm = FileManager.default
        let dir = rootURL.appendingPathComponent(name, isDirectory: true)
        let stateURL = dir.appendingPathComponent("agent_state.json")
        var status: ProjectStatus = .new
        var currentPhase: String? = nil
        var currentRound = 0
        var nextAgent: String? = nil
        var error: String? = nil
        var lastProcessed: String? = nil
        var completed: [String] = []
        var outputs: [String: String] = [:]
        var workflowName: String? = nil
        var awaiting: String? = nil
        var awaitingHuman: String? = nil
        var blocked: BlockedConflict? = nil
        var resolutions: [String: String] = [:]

        if let data = try? Data(contentsOf: stateURL),
           let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
            currentPhase = obj["current_phase"] as? String
            currentRound = (obj["current_round"] as? Int) ?? 0
            nextAgent = obj["next_agent"] as? String
            error = obj["error"] as? String
            lastProcessed = obj["last_processed"] as? String
            completed = (obj["completed_phases"] as? [String]) ?? []
            outputs = (obj["phase_outputs"] as? [String: String]) ?? [:]
            workflowName = obj["workflow"] as? String
            awaiting = obj["awaiting_approval"] as? String
            awaitingHuman = obj["awaiting_human"] as? String
            blocked = BlockedConflict.parse(fromStateObject: obj)
            resolutions = (obj["phase_resolutions"] as? [String: String]) ?? [:]
            let done = (obj["done"] as? Bool) ?? false
            if let e = error, !e.isEmpty {
                status = .aborted
            } else if done {
                status = .done
            } else {
                status = .inProgress
            }
        }

        let resolvedName = workflowName ?? readWorkflowFile(dir)
            ?? readWorkflowFrontmatter(dir) ?? "app_build"
        let wf = workflowsByName[resolvedName] ?? defaultWorkflow
        let wfPhases = wf?.phases ?? ALL_PHASES
        var titles: [String: String] = [:]
        for p in wfPhases { titles[p.key] = p.title }

        let stateMTime = (try? fm.attributesOfItem(atPath: stateURL.path))?[.modificationDate] as? Date
        var running = false
        if status == .inProgress, let m = stateMTime, Date().timeIntervalSince(m) < 240 {
            running = true
        }

        var stopped = false
        if let stopAt {
            if let m = stateMTime, m.timeIntervalSince(stopAt) > 10 {
                // A new run resumed after the stop marker; let the fresh state win.
            } else if !processRunning {
                running = false
                stopped = status == .inProgress
            }
        }

        var proj = Project(name: name, status: status, currentPhase: currentPhase,
                           currentRound: currentRound, nextAgent: nextAgent, error: error,
                           lastProcessed: lastProcessed, completedPhases: completed,
                           phaseOutputs: outputs, dirURL: dir, running: running,
                           workflow: resolvedName,
                           workflowTitle: wf?.title ?? "Build an App",
                           workflowKind: wf?.kindLabel ?? "Build",
                           phaseCount: wfPhases.count, phaseTitles: titles)
        proj.awaitingApproval = (awaiting?.isEmpty == false) ? awaiting : nil
        proj.awaitingHuman = (awaitingHuman?.isEmpty == false) ? awaitingHuman : nil
        proj.blockedConflict = blocked
        proj.manuallyStopped = stopped
        proj.archived = fm.fileExists(
            atPath: dir.appendingPathComponent(".orch_archived").path)
        proj.phaseResolutions = resolutions
        let verifyRecords = VerifyResultsParser.parse(
            fileAt: dir.appendingPathComponent("verify_results.json"))
        proj.latestVerify = VerifyResultsParser.latest(verifyRecords)
        proj.verifyRepairCount = VerifyResultsParser.repairAttemptCount(verifyRecords)
        return proj
    }

    private static func readWorkflowFile(_ dir: URL) -> String? {
        let url = dir.appendingPathComponent("workflow.txt")
        guard let s = try? String(contentsOf: url, encoding: .utf8) else { return nil }
        let name = s.trimmingCharacters(in: .whitespacesAndNewlines)
            .components(separatedBy: "\n").first?.trimmingCharacters(in: .whitespaces)
        return (name?.isEmpty == false) ? name : nil
    }

    private static func readWorkflowFrontmatter(_ dir: URL) -> String? {
        let url = dir.appendingPathComponent("initial_prompt/initial_prompt.md")
        guard let text = try? String(contentsOf: url, encoding: .utf8) else { return nil }
        for line in text.components(separatedBy: "\n").prefix(15) {
            let s = line.trimmingCharacters(in: CharacterSet(charactersIn: "# ").union(.whitespaces))
            if s.lowercased().hasPrefix("workflow:") {
                let name = s.dropFirst("workflow:".count).trimmingCharacters(in: .whitespaces)
                if !name.isEmpty { return name }
            }
        }
        return nil
    }

    static func computeParallelBuildWorkers(for project: Project, logsDirURL: URL) -> [BuildWorker]? {
        guard let na = project.nextAgent, let phase = project.currentPhase else { return nil }
        let slugs = na.split(separator: "+").map(String.init)
        guard slugs.count > 1 else { return nil }
        let finished = finishedSlugs(app: project.name,
                                     phase: phase,
                                     round: project.currentRound,
                                     logsDirURL: logsDirURL)
        return slugs.map { slug in
            let (base, tag) = splitWorkerSlug(slug)
            let display = (Speaker(rawValue: base) ?? .system).display
            let label = tag.map { "\(display) \($0.uppercased())" } ?? display
            return BuildWorker(slug: slug, label: label, baseAgent: base,
                               done: finished.contains(slug))
        }
    }

    private static func finishedSlugs(app: String, phase: String, round: Int, logsDirURL: URL) -> Set<String> {
        guard !hasAmbiguousUnderscores(app), !hasAmbiguousUnderscores(phase) else { return [] }
        let fm = FileManager.default
        guard let files = try? fm.contentsOfDirectory(atPath: logsDirURL.path) else { return [] }
        let prefix = "__\(app)__\(phase)__r\(round)."
        let tsLen = 22
        var done = Set<String>()
        for f in files {
            guard f.count > tsLen else { continue }
            let afterTs = f.index(f.startIndex, offsetBy: tsLen)
            guard f[afterTs...].hasPrefix(prefix) else { continue }
            let rest = f[f.index(afterTs, offsetBy: prefix.count)...]
            guard let sep = rest.range(of: "__") else { continue }
            done.insert(String(rest[..<sep.lowerBound]))
        }
        return done
    }

    private static func splitWorkerSlug(_ slug: String) -> (base: String, tag: String?) {
        guard let dash = slug.firstIndex(of: "-") else { return (slug, nil) }
        return (String(slug[..<dash]), String(slug[slug.index(after: dash)...]))
    }

    private static func hasAmbiguousUnderscores(_ s: String) -> Bool {
        s.hasPrefix("_") || s.hasSuffix("_") || s.contains("__")
    }
}

// Reasoning-effort config key per provider — internal (not private) so tests
// can pin the Claude/Codex effort parity without spinning up the store.
func effortConfigKey(_ agent: String) -> String? {
    switch agent {
    case "codex": return "codex_reasoning"
    case "claude": return "claude_reasoning"
    default: return nil
    }
}

// The config.yaml key the engine actually reads for each provider's model.
private func modelKey(_ agent: String) -> String {
    switch agent {
    case "codex": return "codex"
    case "claude": return "claude"
    case "gemini": return "gemini_fallback"
    default: return agent
    }
}

private func firstMatch(in text: String, pattern: String) -> String? {
    guard let re = try? NSRegularExpression(pattern: pattern) else { return nil }
    let range = NSRange(text.startIndex..<text.endIndex, in: text)
    guard let m = re.firstMatch(in: text, range: range), m.numberOfRanges > 1,
          let r = Range(m.range(at: 1), in: text) else { return nil }
    return String(text[r])
}

// Everything else refresh() re-reads from disk each tick — workflows, roles,
// config.yaml settings, CLI availability. Runs on the background refresh queue
// (same pattern as BackgroundProjectLoader above) into a plain value snapshot,
// so a busy disk never stalls the main thread; the main-actor hop publishes
// only the values that changed.
private enum BackgroundConfigLoader {
    // "ollama" last, mirroring the engine's AGENT_ORDER: the local model never
    // shadows a cloud agent anywhere order implies preference (spec §10/§12).
    static let agentOrder = ["codex", "claude", "gemini", "ollama"]

    struct Snapshot {
        var workflows: [WorkflowDef] = []
        var roles: [RoleDef] = []
        var personalities: [PersonalityDef] = []
        // nil when roles.json is missing/unreadable — keep the current value.
        var agentRoleOverrides: [String: String]?
        var customModelPresets: [String: [String]] = [:]
        var enabledAgents: [String: Bool] = [:]
        var agentModels: [String: String] = [:]
        var agentEfforts: [String: String] = [:]
        var cliAvailable: [String: Bool] = [:]
    }

    static func load(workflowsDirURL: URL, rolesURL: URL,
                     modelPresetsURL: URL, configURL: URL) -> Snapshot {
        var snap = Snapshot()
        // Workflows: every .json under .orchestrator/workflows/. Dedupe by
        // internal name (first wins, stable by sorted filename) so a duplicated
        // file can't produce two workflows with the same name.
        var wfs: [WorkflowDef] = []
        var seenNames = Set<String>()
        if let items = try? FileManager.default.contentsOfDirectory(atPath: workflowsDirURL.path) {
            for fn in items.sorted() where fn.hasSuffix(".json") {
                let url = workflowsDirURL.appendingPathComponent(fn)
                if let data = try? Data(contentsOf: url),
                   let j = try? JSONDecoder().decode(WorkflowJSON.self, from: data) {
                    let wf = WorkflowDef.from(j)
                    if seenNames.insert(wf.name).inserted { wfs.append(wf) }
                }
            }
        }
        // app_build first, then alphabetical.
        wfs.sort { ($0.name == "app_build" ? "" : $0.name) < ($1.name == "app_build" ? "" : $1.name) }
        if wfs.isEmpty {
            wfs = [WorkflowDef(name: "app_build", title: "Build an App", description: "",
                               target: "app", phases: ALL_PHASES)]
        }
        snap.workflows = wfs

        // Roles + personalities from roles.json.
        if let data = try? Data(contentsOf: rolesURL),
           let j = try? JSONDecoder().decode(RolesFileJSON.self, from: data) {
            snap.roles = (j.roles ?? []).map { RoleDef(id: $0.id, name: $0.name, focus: $0.focus) }
            snap.personalities = (j.personalities ?? []).map {
                PersonalityDef(id: $0.id, name: $0.name, style: $0.style)
            }
            snap.agentRoleOverrides = j.agentRoleOverrides ?? [:]
        }
        snap.customModelPresets = readCustomModelPresets(modelPresetsURL)
        snap.enabledAgents = readEnabledAgents(configURL)
        snap.agentModels = readModels(configURL)
        snap.agentEfforts = readEfforts(configURL)
        snap.cliAvailable = detectCLIs()
        return snap
    }

    static func readEnabledAgents(_ configURL: URL) -> [String: Bool] {
        // Defaults mirror the engine's: cloud agents on, the local model off.
        var out = ["codex": true, "claude": true, "gemini": true, "ollama": false]
        guard let text = try? String(contentsOf: configURL, encoding: .utf8) else { return out }
        for agent in agentOrder {
            // Anchor to line start (ignoring indent) so a commented-out line like
            // `# ollama_enabled: true` can't be mis-read as the live setting.
            if let m = firstMatch(in: text, pattern: "(?m)^\\s*\(agent)_enabled:\\s*(true|false)") {
                out[agent] = (m == "true")
            }
        }
        return out
    }

    static func readModels(_ configURL: URL) -> [String: String] {
        var out: [String: String] = [:]
        guard let text = try? String(contentsOf: configURL, encoding: .utf8) else { return out }
        for agent in agentOrder {
            let key = modelKey(agent)
            // Match `  <key>: "value"` or `  <key>: value`, key not followed by _ (so
            // `codex:` doesn't collide with `codex_reasoning:`).
            if let m = firstMatch(in: text, pattern: "(?m)^\\s*\(key):\\s*\"?([^\"\\n#]+?)\"?\\s*$") {
                out[agent] = m.trimmingCharacters(in: .whitespaces)
            }
        }
        return out
    }

    static func readEfforts(_ configURL: URL) -> [String: String] {
        var out: [String: String] = [:]
        guard let text = try? String(contentsOf: configURL, encoding: .utf8) else { return out }
        for agent in agentOrder {
            guard let key = effortKey(agent) else { continue }
            if let m = firstMatch(in: text, pattern: "(?m)^\\s*\(key):\\s*\"?([a-zA-Z]+)\"?\\s*$") {
                out[agent] = m.trimmingCharacters(in: .whitespaces).lowercased()
            }
        }
        return out
    }

    static func readCustomModelPresets(_ modelPresetsURL: URL) -> [String: [String]] {
        guard let data = try? Data(contentsOf: modelPresetsURL),
              let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let raw = obj["models"] as? [String: Any] else { return [:] }
        var out: [String: [String]] = [:]
        for agent in agentOrder {
            if let arr = raw[agent] as? [String] {
                out[agent] = arr.map { $0.trimmingCharacters(in: .whitespacesAndNewlines) }
                    .filter { modelIDIsSafe($0) }
            }
        }
        return out
    }

    // Which agent CLIs are actually invokable on PATH (codex/claude/gemini or agy).
    static func detectCLIs() -> [String: Bool] {
        let fm = FileManager.default
        let dirs = OrchestratorStore.cliSearchDirs()
        func has(_ names: [String]) -> Bool {
            for d in dirs { for n in names {
                let p = (d as NSString).appendingPathComponent(n)
                if fm.isExecutableFile(atPath: p) { return true }
            } }
            return false
        }
        return ["codex": has(["codex"]), "claude": has(["claude"]),
                "gemini": has(["gemini", "agy"]), "ollama": has(["ollama"])]
    }

    // Reasoning-effort config key per provider (OrchestratorStore.effortKey
    // delegates here). Claude gained --effort parity with Codex (engine
    // commit e89e403; config key models.claude_reasoning).
    static func effortKey(_ agent: String) -> String? {
        effortConfigKey(agent)
    }

    static func modelIDIsSafe(_ id: String) -> Bool {
        let trimmed = id.trimmingCharacters(in: .whitespacesAndNewlines)
        return !trimmed.isEmpty && trimmed.allSatisfy {
            $0.isLetter && $0.isASCII || $0.isNumber && $0.isASCII
                || $0 == "." || $0 == "-" || $0 == "_" || $0 == ":" || $0 == "/"
        }
    }
}

// One live per-app engine lock (<root>/.orch-locks/<app>.lock): the pid named
// in the lock payload and when the run started.
struct AppLockInfo: Equatable {
    var pid: Int32?
    var since: Date
}

// Workspace-level scans for the factory dashboard: per-app engine locks,
// autorun-disabled markers, and the persisted queue-order file. Pure file
// reads — run on the background refresh queue like the loaders above.
// (internal, not private: unit-tested directly via @testable import.)
enum FactoryScanner {
    // The lock payload's started= stamp uses the engine's now_str() format.
    static let startedFormatter: DateFormatter = {
        let f = DateFormatter()
        f.dateFormat = "yyyy-MM-dd HH:mm:ss"
        f.locale = Locale(identifier: "en_US_POSIX")
        return f
    }()

    static func scanLocks(rootURL: URL) -> [String: AppLockInfo] {
        let fm = FileManager.default
        let dir = rootURL.appendingPathComponent(".orch-locks", isDirectory: true)
        guard let items = try? fm.contentsOfDirectory(atPath: dir.path) else { return [:] }
        var out: [String: AppLockInfo] = [:]
        for f in items where f.hasSuffix(".lock") {
            // Nested stems decode back to the session id (hash-verified);
            // flat stems pass through raw — appLocks keys == Project.name.
            let app = SessionLayout.decodeLockStem(String(f.dropLast(".lock".count)))
            let p = dir.appendingPathComponent(f)
            let text = (try? String(contentsOf: p, encoding: .utf8)) ?? ""
            let pid = text.split(whereSeparator: { $0 == " " || $0 == "\n" })
                .first { $0.hasPrefix("pid=") }
                .flatMap { Int32($0.dropFirst(4)) }
            // Prefer the payload's started= stamp: the run heartbeat re-touches
            // the lock every 30s, so mtime would measure "since last heartbeat",
            // not elapsed run time.
            var since: Date? = nil
            if let r = text.range(of: "started=") {
                since = startedFormatter.date(from: String(text[r.upperBound...].prefix(19)))
            }
            let attrs = try? fm.attributesOfItem(atPath: p.path)
            let fallback = (attrs?[.creationDate] as? Date)
                ?? (attrs?[.modificationDate] as? Date) ?? Date()
            out[app] = AppLockInfo(pid: pid, since: since ?? fallback)
        }
        return out
    }

    static func scanAutorunDisabled(rootURL: URL, names: [String]) -> Set<String> {
        let fm = FileManager.default
        var out = Set<String>()
        for name in names where fm.fileExists(
            atPath: rootURL.appendingPathComponent(name)
                .appendingPathComponent(".orchestrator_autorun_disabled").path) {
            out.insert(name)
        }
        return out
    }

    static func readQueueFile(rootURL: URL) -> (order: [String], lanes: Int)? {
        let url = rootURL.appendingPathComponent(".orch-queue-order.json")
        guard let data = try? Data(contentsOf: url),
              let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else {
            return nil
        }
        return ((obj["order"] as? [String]) ?? [], (obj["lanes"] as? Int) ?? 3)
    }

    // Pid liveness, shepherd locked() parity: kill(pid, 0) probes without
    // signaling. EPERM counts as ALIVE — a process we may not signal can still
    // be running, and a false "alive" only suppresses a resume offer (the safe
    // direction), never launches anything.
    nonisolated(unsafe) static let pidAlive: (Int32) -> Bool = { pid in
        kill(pid, 0) == 0 || errno == EPERM
    }

    /// Names whose lock is STALE: it names no pid, or the pid is dead — the
    /// same rule shepherd.sh locked() uses (a pid-less lock is not locked).
    /// scanLocks stays a pure parser; this is the separate liveness layer.
    static func staleLockNames(in locks: [String: AppLockInfo],
                               isPidAlive: (Int32) -> Bool = pidAlive) -> Set<String> {
        Set(locks.filter { _, info in
            guard let pid = info.pid else { return true }
            return !isPidAlive(pid)
        }.keys)
    }
}

// Commands routed from the menu bar (⌘N / ⌘R / ⌥⌘I / ⌘F / …) into the active
// shell (ContentView / AppShellView), which owns the selection and sheet
// state the actions need. One action layer, several invocation surfaces.
enum UICommand: Equatable, Hashable {
    case newChat, runSelected, toggleLog
    case toggleInspector   // ⌥⌘I — Native Pro shell inspector
    case focusSearch       // ⌘F — focus the sidebar project filter
    case openPlanTab       // Inspector "Open Plan tab" jump (§3 region 3)
    case togglePause       // Pause/Resume Engine (toolbar + Command Palette)
}

// Single source of truth for the commands that appear in both the menu bar
// (OrchestratorApp's .commands{}) and the ⌘K Command Palette
// (CommandPaletteView), so their titles/shortcuts can't drift apart.
// Pause/Resume is deliberately excluded: it's toolbar + palette only (no
// menu-bar entry) and its title is dynamic (depends on store.enginePaused).
struct MenuCommandSpec: Identifiable {
    let action: UICommand
    let title: String
    let key: KeyEquivalent
    let modifiers: EventModifiers
    var id: UICommand { action }

    // "⌘N" / "⌥⌘I" style label for the palette; the menu bar renders its own
    // shortcut glyph from key/modifiers via .keyboardShortcut().
    var shortcutDisplay: String {
        var s = ""
        if modifiers.contains(.control) { s += "⌃" }
        if modifiers.contains(.option) { s += "⌥" }
        if modifiers.contains(.shift) { s += "⇧" }
        if modifiers.contains(.command) { s += "⌘" }
        return s + String(key.character).uppercased()
    }

    static let all: [MenuCommandSpec] = [
        MenuCommandSpec(action: .newChat, title: "New App", key: "n", modifiers: .command),
        MenuCommandSpec(action: .toggleInspector, title: "Toggle Inspector",
                         key: "i", modifiers: [.option, .command]),
        MenuCommandSpec(action: .focusSearch, title: "Find Project", key: "f", modifiers: .command),
        MenuCommandSpec(action: .runSelected, title: "Run Selected Project",
                         key: "r", modifiers: .command),
        MenuCommandSpec(action: .toggleLog, title: "Toggle Run Log", key: "l", modifiers: .command),
    ]

    static func spec(for action: UICommand) -> MenuCommandSpec {
        guard let s = all.first(where: { $0.action == action }) else {
            fatalError("MenuCommandSpec.spec(for:) called with an action not in .all: \(action)")
        }
        return s
    }
}

// One build-history commit in <project>/app_build/.git. Parsed with unit/record
// separators (not the old "%h  %ad  %s" double-space split, which broke on any
// subject containing a double space). File-scope so views + tests see it without
// crossing the store's @MainActor isolation.
struct BuildCommit: Identifiable, Hashable {
    let sha: String
    let shortSha: String
    let date: String
    let subject: String
    let refs: String        // e.g. "tag: run-0001"
    let phase: String?      // parsed from "orchestrator: <phase> iteration <n>", else nil
    var id: String { sha }
}

// Outcome of a rollback attempt — a total enum so the UI can message each case
// precisely and a test can assert on it.
enum RollbackResult: Equatable {
    case success(newShortSha: String)
    case noChange           // target tree == current tree; nothing to commit
    case dirtyWorkingTree   // uncommitted/untracked changes — refuse (would be unrecoverable)
    case shaNotFound
    case notARepo
    case running            // a live build owns the repo — refuse
    case gitFailed(String)
}

// One file's worth of a diff, its lines already classified for +/- styling.
struct FileDiff: Identifiable, Hashable {
    let path: String
    let lines: [DiffLine]
    var id: String { path }
}

struct DiffLine: Hashable {
    enum Kind { case fileHeader, hunk, add, remove, context, meta }
    let kind: Kind
    let text: String
}

// Reads everything the orchestrator writes to disk and republishes it on a
// timer so the UI updates in near-real-time. Also drives the write actions
// (new project, run a pass, demo stream).
@MainActor
final class OrchestratorStore: ObservableObject {
    // MARK: - Test seams (V3 board 2.7)
    // The golden-path suite constructs a REAL store against a temp
    // workspace. These four statics are the only injection points; each
    // defaults to production behavior and tests must reset them in
    // teardown. STATIC by necessity: enginePaused's property initializer
    // runs before any instance value could land.
    static var defaults: UserDefaults = .standard
    // Both chat-history paths (legacy file + per-chat dir) derive from
    // this ONE base — pinning only one of them would let the legacy
    // migration copy the user's REAL history into a test dir.
    static var appSupportBaseURL: URL = FileManager.default
        .urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
        .appendingPathComponent("Orchestrator", isDirectory: true)
    // The xctest runner HAS a bundle id, so the bundle-id guard alone
    // would let refresh transitions post real user notifications (and
    // runProject trigger a real authorization prompt) during tests.
    static var suppressNotifications = false
    // The 20s watchdog and an artificial scan delay: the watchdog fires
    // via asyncAfter armed in refresh() itself (not the start() timer),
    // but a deterministic test can neither wait 20s nor wedge the static
    // scan without these.
    var watchdogSeconds: TimeInterval = 20
    static var scanDelayForTests: TimeInterval = 0

    @Published var projects: [Project] = []
    @Published var orchestratorRunning = false
    @Published var runLog: String = ""   // tail of the most recent action's output
    // A failed action's message, shown as a dismissible top banner so errors
    // don't hide in the ⌘L-collapsed run log. Set via surfaceError().
    @Published var lastError: String?

    /// Report a user-facing error both in the run log and as a banner.
    func surfaceError(_ msg: String) {
        let (logLine, banner) = OrchestratorStore.formatSurfacedError(msg)
        runLog += logLine
        lastError = banner
    }

    // Pure formatting split out of surfaceError() so it's unit-testable
    // without a live store instance: (line appended to runLog, banner text).
    // nonisolated: touches no actor state, and XCTest calls it synchronously
    // from a nonisolated context.
    nonisolated static func formatSurfacedError(_ msg: String) -> (logLine: String, banner: String) {
        let logLine = msg.hasSuffix("\n") ? msg : msg + "\n"
        let banner = msg.trimmingCharacters(in: .whitespacesAndNewlines)
        return (logLine, banner)
    }
    // Run queue: projects waiting to run one at a time (FIFO). advanceQueueIfIdle()
    // launches the next as soon as nothing is running.
    @Published var runQueue: [String] = []
    // Pause/resume the engine (design §3 toolbar): when paused, queued projects
    // are NOT auto-launched. In-flight runs are left alone — pause holds the
    // queue, it doesn't kill work. Persisted (UserDefaults, matching
    // workspaceRoot's pattern) so a user who paused and quit isn't silently
    // un-paused on next launch with no indication anything changed.
    @Published var enginePaused = OrchestratorStore.defaults.bool(forKey: "enginePaused") {
        didSet { Self.defaults.set(enginePaused, forKey: "enginePaused") }
    }
    // Menu-bar command relay (⌘N/⌘R/⌘L) — ContentView observes and handles.
    @Published var uiCommand: UICommand?
    // ⌘K command palette overlay (design §3/§8). Commands chosen in it are
    // dispatched through uiCommand, so there's one command path.
    @Published var showCommandPalette = false
    // V3 board 2.6: transcript search (search.py index) surfaced in the
    // palette. searchStatus carries the engine's degraded signal verbatim —
    // the palette must render it, never show silently-empty results.
    @Published var searchHits: [SearchHit] = []
    @Published var searchStatus: String = "ok"
    @Published var searchInFlight = false
    // Where a chosen hit should land: ProjectRunContent selects the phase,
    // TranscriptView consumes the anchor for the turn-level scroll.
    @Published var pendingTranscriptAnchor: TranscriptAnchor?
    // Stale-query guard (§12.2): only the newest query's results may land.
    private var searchGeneration = 0
    // V3 3.8: the section rail (explicit R4 state) + selected section.
    @Published var sectionRail: SectionRailState = .loading
    @Published var selectedSection: String?
    // Per-section lint: missing key = not yet run; .some(nil) = the lint
    // run FAILED (surfaced as unavailable, never as "clean"); .some(.some)
    // = a parsed report.
    @Published var sectionLint: [String: SectionLintSummary?] = [:]
    private var launchingName: String?      // just-launched, not yet seen as running
    private var launchingAt: Date?
    // Pre-refresh seed must match the engine default (local model OFF) so the
    // first render doesn't briefly show Ollama enabled; refresh() then loads the
    // real config value.
    @Published var enabledAgents: [String: Bool] = ["codex": true, "claude": true,
                                                    "gemini": true, "ollama": false]
    @Published var agentModels: [String: String] = [:]   // provider -> chosen model
    @Published var agentEfforts: [String: String] = [:]  // provider -> reasoning effort
    @Published var customModelPresets: [String: [String]] = [:] // provider -> user-added menu options
    @Published var cliAvailable: [String: Bool] = [:]   // codex / claude / gemini / ollama
    @Published var agentRoleOverrides: [String: String] = [:]
    // Parallel-build roster status, keyed by project name. Computed at most once
    // per refresh() tick (below) — NOT on every SwiftUI body re-evaluation, which
    // would otherwise re-scan the logs directory on every render/animation frame.
    @Published private var buildWorkerStatus: [String: [BuildWorker]] = [:]

    // Pluggable workflows + editable sub-agents, loaded from disk.
    @Published var workflows: [WorkflowDef] = []
    @Published var roles: [RoleDef] = []
    @Published var personalities: [PersonalityDef] = []

    // Factory dashboard state: live per-app engine locks (<root>/.orch-locks),
    // autorun-disabled markers, the persisted queue order + build lanes, and
    // whether a shepherd.sh loop is running on this machine. All re-scanned on
    // the same background refresh tick as everything else.
    // M4 run health: parsed events.jsonl tails per project + the fleet rollup
    // (toolbar capsule + fallback bell). Populated on the background refresh
    // tick by EventsScanner — views never read the files themselves.
    @Published var eventsByProject: [String: [EngineEvent]] = [:]
    @Published var fleetHealth = FleetHealthSummary()

    @Published var appLocks: [String: AppLockInfo] = [:]
    @Published var autorunDisabled: Set<String> = []
    @Published var queueOrder: [String] = []
    // Crashed-run detection (ResumeLogic.swift): locks whose pid is dead or
    // absent, and the settled offers derived from them. Display subtracts
    // staleLocks from "running"; crashedRuns drives the resume banner. The
    // refresh path only ever PUBLISHES these — the sole launch entry point is
    // the user's click on resumeCrashedRun.
    @Published var staleLocks: Set<String> = []
    @Published var crashedRuns: [ResumeOffer] = []
    private var staleLockFirstSeen: [String: Date] = [:]

    // Chat Home conversation state lives on the store, NOT in the view:
    // navigating to a project and back recreates ChatHomeView, and view-local
    // @State would silently wipe the conversation — including a concierge
    // reply still in flight (its Task would write into detached storage).
    // Persisted to disk (Application Support, not the workspace or engine dir —
    // this is pure GUI state, not a project or a config file) so a conversation
    // survives quitting the app, not just navigating within one session.
    @Published var chatMessages: [ConciergeMessage] = [] {
        didSet { saveChatHistory() }
    }
    @Published var chatInput = ""
    @Published var chatThinking = false
    // V3 board 1.4: per-chat history. currentChatKey selects which history
    // file chatMessages mirrors ("home" = the legacy Chat Home thread; see
    // ChatHistoryStore's key-space note). thinkingKeys/draftsByChat keep
    // thinking state and composer drafts per chat so switching mid-reply
    // can't show a "Thinking…" the new chat never asked for (§12.1/R2).
    private(set) var currentChatKey = "home"
    private var thinkingKeys: Set<String> = []
    private var draftsByChat: [String: String] = [:]
    // Load-guard: loadChatHistory assigns chatMessages, which fires didSet →
    // saveChatHistory; without this flag a load-during-switch would write
    // chat A's messages under chat B's key.
    private var isLoadingChatHistory = false
    // V3 board 1.5: engine-backed chat sessions minted by this GUI instance,
    // keyed by flat dir name. Lifecycle is the ChatSession state enum; the
    // scan-merge in refresh() only derives waiting/running transitions.
    @Published var chatSessions: [String: ChatSession] = [:]
    @Published var chatClaudeAvailable = true
    @Published var buildLanes = 3
    @Published var shepherdActive = false
    // True while the queued list is mid-drag: skip re-reading the queue file so
    // a background tick can't revert an in-flight reorder before it persists.
    // The timestamp expires an abandoned drag (esc / dropped outside a row —
    // performDrop never fires) so ticks can't be blocked indefinitely.
    private var queueDragActive = false
    private var queueDragStarted: Date?
    private var lastShepherdCheck = Date.distantPast
    private var shepherdCheckInFlight = false
    private var swiftCountCache: [String: (count: Int, at: Date)] = [:]

    // "ollama" last, mirroring the engine's AGENT_ORDER: the local model never
    // shadows a cloud agent anywhere order implies preference (spec §10/§12).
    let agentOrder = BackgroundConfigLoader.agentOrder

    // Mutable so the workspace can be changed from Settings without relaunch.
    @Published var rootURL: URL
    let orchDirURL: URL
    // False when no engine could be found anywhere — the UI shows a clear
    // error and launches are refused instead of failing cryptically.
    let engineAvailable: Bool

    // Process handles for runs launched FROM THIS GUI session, keyed by project
    // name, so a running project can be stopped. Handles don't survive a GUI
    // relaunch — a run started elsewhere can't be signalled from here.
    private var runningProcesses: [String: Process] = [:]
    @Published private(set) var stoppableProjects: Set<String> = []
    // Project name -> when the user pressed Stop. Overrides the state-file
    // mtime "running" heuristic until the engine writes state again.
    private var manualStops: [String: Date] = [:]

    private var timer: Timer?
    private var refreshInFlight = false
    private var refreshPending = false
    private var refreshGeneration = 0
    private let fm = FileManager.default

    // TTL-cached model_routing.json reads. readModelRouting()/
    // readProjectRouting() are called straight from view bodies
    // (PhaseTimelineView renders per-phase agent chips), and SwiftUI can
    // re-run those bodies many times a second while any `.repeatForever`
    // animation is on screen — without this cache that meant a synchronous
    // file read + JSON parse on the main thread every single time, which was
    // the root cause of the M5.1 sustained-CPU/beachball bug.
    private var modelRoutingCache: [URL: (loadedAt: Date, routing: ModelRouting)] = [:]

    init() {
        let env = ProcessInfo.processInfo.environment

        // Engine dir. Precedence: ORCH_DIR (the run-from-source launcher) → the
        // engine copied out of the .app bundle to a writable spot (double-click /
        // DMG install) → the repo checkout the executable was built inside →
        // nothing (surface a clear error; no hardcoded machine-specific paths).
        if let d = env["ORCH_DIR"] {
            self.orchDirURL = URL(fileURLWithPath: d, isDirectory: true)
            self.engineAvailable = FileManager.default.fileExists(
                atPath: self.orchDirURL.appendingPathComponent("orchestrator.py").path)
        } else if let resolved = OrchestratorStore.resolveEngineDir() {
            self.orchDirURL = resolved
            self.engineAvailable = true
        } else {
            // Keep pointing at the writable engine location so reads fail soft;
            // engineAvailable=false drives the error banner + disabled launches.
            self.orchDirURL = FileManager.default
                .urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
                .appendingPathComponent("Orchestrator/engine", isDirectory: true)
            self.engineAvailable = false
        }

        // Workspace root. Precedence: ORCH_ROOT (launcher) → the folder the user
        // picked in Settings → the shared factory workspace.
        if let r = env["ORCH_ROOT"] {
            self.rootURL = URL(fileURLWithPath: r, isDirectory: true)
        } else if let saved = Self.defaults.string(forKey: "workspaceRoot"), !saved.isEmpty {
            self.rootURL = URL(fileURLWithPath: saved, isDirectory: true)
        } else {
            self.rootURL = FileManager.default.homeDirectoryForCurrentUser
                .appendingPathComponent("Documents/iOS-App-Factory", isDirectory: true)
        }
        try? fm.createDirectory(at: rootURL, withIntermediateDirectories: true)
    }

    // The engine (orchestrator.py + workflows/config/knowledge) writes logs, seeds
    // workflows, and has its config edited — so it can't live in the read-only app
    // bundle. On first launch we copy the bundled engine template to Application
    // Support and run from there; a VERSION mismatch re-copies on upgrade.
    // Running from source (no bundled engine), the first ancestor of the
    // executable containing orchestrator.py is the repo engine dir. nil when
    // neither exists (see EngineDirResolver.pick for the precedence).
    static func resolveEngineDir() -> URL? {
        let fm = FileManager.default
        let hasEngine: (URL) -> Bool = {
            fm.fileExists(atPath: $0.appendingPathComponent("orchestrator.py").path)
        }
        let choice = EngineDirResolver.pick(
            bundledTemplate: Bundle.main.resourceURL?
                .appendingPathComponent("engine", isDirectory: true),
            repoCandidates: EngineDirResolver.repoLayoutCandidates(
                executableURL: Bundle.main.executableURL),
            hasEngine: hasEngine)
        switch choice {
        case .bundled(let tmpl):
            let dest = fm.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
                .appendingPathComponent("Orchestrator/engine", isDirectory: true)
            let ver = { (u: URL) in (try? String(contentsOf: u.appendingPathComponent("VERSION"),
                                                  encoding: .utf8)) ?? "" }
            if !fm.fileExists(atPath: dest.appendingPathComponent("orchestrator.py").path)
                || ver(tmpl) != ver(dest) {
                try? fm.createDirectory(at: dest.deletingLastPathComponent(), withIntermediateDirectories: true)
                try? fm.removeItem(at: dest)
                try? fm.copyItem(at: tmpl, to: dest)
            }
            return dest
        case .repo(let url):
            return url
        case .missing:
            return nil
        }
    }

    // Shown when engineAvailable == false.
    var engineMissingMessage: String {
        "Engine not found: no bundled engine in the app's Resources and no "
        + "orchestrator.py in any folder above the executable. Reinstall the app, "
        + "or run from the repo checkout (or set ORCH_DIR to the engine directory)."
    }

    // Change the workspace folder at runtime (Settings). Persists + rescans.
    func setWorkspaceRoot(_ url: URL) {
        Self.defaults.set(url.path, forKey: "workspaceRoot")
        rootURL = url
        // Resume offers are per-workspace; clearing here (belt) plus the
        // scannedRoot guard in updateResumeOffers (braces) keeps a stale
        // tick's offers from ever leaking across roots.
        staleLockFirstSeen = [:]
        crashedRuns = []
        staleLocks = []
        try? fm.createDirectory(at: url, withIntermediateDirectories: true)
        refresh()
    }

    func start() {
        seedWorkflowsIfMissing()
        // Queue order + lanes come up before the first background tick lands so
        // the dashboard doesn't flash defaults.
        if let qf = FactoryScanner.readQueueFile(rootURL: rootURL) {
            queueOrder = qf.order
            buildLanes = qf.lanes
        }
        loadChatHistory()
        refresh()
        refreshLocalModels()   // async engine-doctor probe (Ollama server/models)
        // Idempotent: the window can be closed and re-opened (Dock / menu bar)
        // while the store lives for the whole app, and each re-open re-fires the
        // root view's onAppear. Without this guard we'd stack a second 1.5s timer
        // every reopen, each doing a full disk rescan. One timer, ever.
        guard timer == nil else { return }
        scheduleRefreshTimer()
    }

    // V3 board 1.10: one timer, ever — but its cadence follows focus. 500ms
    // while a LIVE session pane is focused (render latency the user is
    // actively watching), 1.5s otherwise. Cadence parameter ONLY: the
    // refresh coalescing / generation guard / watchdog machinery is
    // untouched, and time-based throttles inside refresh (shepherd poll,
    // routing-cache TTL) stay time-based so 3x ticks never mean 3x work.
    private(set) var focusedLivePane: String? = nil

    nonisolated static func refreshInterval(focusedLive: Bool) -> Double {
        focusedLive ? 0.5 : 1.5
    }

    func setFocusedLivePane(_ name: String?) {
        guard focusedLivePane != name else { return }
        let before = Self.refreshInterval(focusedLive: focusedLivePane != nil)
        focusedLivePane = name
        let after = Self.refreshInterval(focusedLive: focusedLivePane != nil)
        if before != after { rescheduleRefreshTimer() }
    }

    private func scheduleRefreshTimer() {
        let interval = Self.refreshInterval(focusedLive: focusedLivePane != nil)
        let t = Timer.scheduledTimer(withTimeInterval: interval, repeats: true) { [weak self] _ in
            Task { @MainActor in self?.refresh() }
        }
        RunLoop.main.add(t, forMode: .common)
        timer = t
    }

    private func rescheduleRefreshTimer() {
        guard timer != nil else { return }   // start() not reached yet
        timer?.invalidate()
        timer = nil
        scheduleRefreshTimer()   // preserves the one-timer invariant
    }

    // First launch on a machine that never ran the engine: materialize the
    // built-in workflow JSON files so the picker/editors have something to show.
    // The python seed is spawned and waited on OFF the main thread — doing it
    // inline (as before) froze the whole UI on first launch until it finished.
    private func seedWorkflowsIfMissing() {
        guard engineAvailable, !fm.fileExists(atPath: workflowsDirURL.path) else { return }
        // Capture main-actor state here, run the blocking Process on a utility
        // queue, then refresh back on the main actor so the seeded workflows show.
        let py = resolvePython()
        let scriptPath = orchDirURL.appendingPathComponent("orchestrator.py").path
        let cwd = rootURL
        DispatchQueue.global(qos: .utility).async { [weak self] in
            let proc = Process()
            proc.executableURL = URL(fileURLWithPath: py)
            proc.arguments = [scriptPath, "--seed"]
            proc.currentDirectoryURL = cwd
            do {
                try proc.run()
                proc.waitUntilExit()
            } catch {
                return
            }
            Task { @MainActor in self?.refresh() }
        }
    }

    deinit { timer?.invalidate() }

    // MARK: - Scanning

    func refresh() {
        if refreshInFlight {
            refreshPending = true
            return
        }
        refreshInFlight = true
        refreshGeneration += 1
        let myGeneration = refreshGeneration
        // Watchdog: the background scan below has no timeout of its own, and if
        // it ever stalls (a huge/locked log file, a wedged file-coordination
        // call), refreshInFlight would stay true forever — every future 1.5s
        // tick becomes a silent no-op and every tab stops updating. Recover
        // after a generous window instead. The generation check makes a LATE
        // completion from the abandoned attempt a safe no-op rather than
        // clobbering fresher state gathered by a refresh that started after
        // this recovery.
        DispatchQueue.main.asyncAfter(deadline: .now() + watchdogSeconds) { [weak self] in
            guard let self, self.refreshInFlight, self.refreshGeneration == myGeneration else { return }
            self.refreshInFlight = false
            self.runLog += "WARN: a background refresh didn't finish within 20s — recovering so live updates don't stall.\n"
            if self.refreshPending {
                self.refreshPending = false
                self.refresh()
            }
        }
        let rootURL = self.rootURL
        let logsDirURL = self.logsDirURL
        let workflowsDirURL = self.workflowsDirURL
        let rolesURL = self.rolesURL
        let modelPresetsURL = self.modelPresetsURL
        let configURL = self.configURL
        let manualStops = self.manualStops
        let runningProcessNames = Set(runningProcesses.compactMap { $0.value.isRunning ? $0.key : nil })

        DispatchQueue.global(qos: .utility).async { [weak self] in
            // 2.7 seam D: lets the watchdog/stale-generation tests wedge
            // the scan deterministically; 0 in production.
            if OrchestratorStore.scanDelayForTests > 0 {
                Thread.sleep(forTimeInterval: OrchestratorStore.scanDelayForTests)
            }
            // Config + workflows first, so each project's shape can be resolved.
            let snap = BackgroundConfigLoader.load(workflowsDirURL: workflowsDirURL,
                                                   rolesURL: rolesURL,
                                                   modelPresetsURL: modelPresetsURL,
                                                   configURL: configURL)
            let workflowIndex = Dictionary(snap.workflows.map { ($0.name, $0) },
                                           uniquingKeysWith: { a, _ in a })
            let defaultWorkflow = workflowIndex["app_build"]
            let names = BackgroundProjectLoader.discoverApps(rootURL: rootURL)
            let loaded = BackgroundProjectLoader.loadProjects(
                names: names,
                rootURL: rootURL,
                workflowsByName: workflowIndex,
                defaultWorkflow: defaultWorkflow,
                manualStops: manualStops,
                runningProcessNames: runningProcessNames)
            var bws: [String: [BuildWorker]] = [:]
            for p in loaded where p.running && (p.nextAgent?.contains("+") ?? false) {
                if let workers = BackgroundProjectLoader.computeParallelBuildWorkers(for: p,
                                                                                     logsDirURL: logsDirURL) {
                    bws[p.name] = workers
                }
            }
            let locks = FactoryScanner.scanLocks(rootURL: rootURL)
            // Dead/absent-pid detection on the scan thread: cheap kill(2)
            // probes over captured locals, same seam as every other scanner.
            let stale = FactoryScanner.staleLockNames(in: locks)
            let autorun = FactoryScanner.scanAutorunDisabled(rootURL: rootURL, names: names)
            let queueFile = FactoryScanner.readQueueFile(rootURL: rootURL)
            // M4: events.jsonl tails + the fleet-health rollup (only files whose
            // mtime/size moved are re-read — the scanner caches parses).
            let events = EventsScanner.scan(rootURL: rootURL, names: names)
            // A stale lock is a corpse, not a running project — counting it
            // as running would hide the crash from the fleet-health rollup.
            let running = Set(loaded.filter(\.running).map(\.name))
                .union(locks.keys.filter { !stale.contains($0) })
            // "Done" only means the pipeline finished — it says nothing about
            // whether the last verify_results.json record actually passed.
            // Fold a done-but-failed-verification project into the same
            // "failed" rollup as an aborted run so the toolbar capsule can't
            // read "All healthy" while a project's build is known-broken.
            let failed = Set(loaded.filter {
                $0.status == .aborted
                    || ($0.status == .done && $0.latestVerify?.ok == false)
            }.map(\.name))
            let health = EventsScanner.summarize(eventsByProject: events,
                                                 runningProjects: running,
                                                 failedProjects: failed)
            DispatchQueue.main.async {
                // A stale generation means the watchdog already recovered from
                // this attempt hanging — a fresher refresh may already be in
                // flight or applied, so this late result must not overwrite it.
                guard let self, self.refreshGeneration == myGeneration else { return }
                self.refreshInFlight = false
                self.apply(snap)
                self.orchestratorRunning = loaded.contains { $0.running }
                AppDelegate.runsActive = self.orchestratorRunning
                self.reloadSectionRail()
                self.detectTransitions(loaded)
                if loaded != self.projects { self.projects = loaded }
                if bws != self.buildWorkerStatus { self.buildWorkerStatus = bws }
                if events != self.eventsByProject { self.eventsByProject = events }
                if health != self.fleetHealth { self.fleetHealth = health }
                self.escalateFallbacksIfNeeded(events)
                if locks != self.appLocks { self.appLocks = locks }
                if stale != self.staleLocks { self.staleLocks = stale }
                self.syncChatSessions(with: loaded)
                if autorun != self.autorunDisabled { self.autorunDisabled = autorun }
                self.updateResumeOffers(scannedRoot: rootURL, loaded: loaded,
                                        locks: locks, stale: stale, autorun: autorun)
                if self.queueDragActive, let t = self.queueDragStarted,
                   Date().timeIntervalSince(t) > 30 {
                    self.endQueueDrag()   // abandoned drag — persist what's shown
                }
                if let qf = queueFile, !self.queueDragActive {
                    if qf.order != self.queueOrder { self.queueOrder = qf.order }
                    if qf.lanes != self.buildLanes { self.buildLanes = qf.lanes }
                }
                self.pollShepherdIfDue()
                self.advanceQueueIfIdle()
                if self.refreshPending {
                    self.refreshPending = false
                    self.refresh()
                }
            }
        }
    }

    // MARK: - Crashed-run resume offers (ResumeLogic.swift)

    /// Derive the settled resume offers from this tick's scan. Main-actor:
    /// exclusion sets read CURRENT launch state (queue, live processes,
    /// manual stops) at apply time, so a scan snapshotted before a Resume
    /// click can never resurrect the offer for the app just launched.
    private func updateResumeOffers(scannedRoot: URL, loaded: [Project],
                                    locks: [String: AppLockInfo],
                                    stale: Set<String>, autorun: Set<String>) {
        // A coalesced refresh (refreshInFlight → refreshPending) does NOT bump
        // refreshGeneration, so a setWorkspaceRoot mid-scan can land an
        // old-root completion here. Offers must never cross workspaces — a
        // click would launch --app <name> against the NEW root.
        guard scannedRoot == rootURL else { return }
        let now = Date()
        staleLockFirstSeen = ResumeAdvisor.settledFirstSeen(
            previous: staleLockFirstSeen, nowStale: stale, now: now)
        var queuedOrLaunching = Set(runQueue)
        if let ln = launchingName { queuedOrLaunching.insert(ln) }
        let offers = ResumeAdvisor.candidates(
            staleLocks: ResumeAdvisor.settled(staleLockFirstSeen, now: now),
            locks: locks,
            autorunDisabled: autorun,
            doneOrMissing: Set(loaded.filter { $0.status == .done }.map(\.name))
                .union(stale.subtracting(loaded.map(\.name))),
            guiOwnedLive: Set(runningProcesses.compactMap {
                $0.value.isRunning ? $0.key : nil
            }),
            queuedOrLaunching: queuedOrLaunching,
            manuallyStopped: Set(manualStops.keys))
        if offers != crashedRuns { crashedRuns = offers }
    }

    /// The ONLY launch entry this feature adds — reachable solely from the
    /// banner button (a user click), never from the refresh path. Uses plain
    /// `--app`: the engine detects its own stale running state and resumes
    /// (same mechanism shepherd uses), and its flock'd per-app lock arbitrates
    /// a simultaneous shepherd launch — one proceeds, the loser logs
    /// "already running (locked)" and exits. The stale lock is deliberately
    /// NOT deleted here: acquire_app_lock reclaims dead-pid locks itself, and
    /// leaving it preserves the engine's mutual exclusion.
    func resumeCrashedRun(_ name: String) {
        guard !autorunDisabled.contains(name) else {
            runLog += "\(name) is paused (autorun disabled) — enable it before resuming.\n"
            return
        }
        guard staleLocks.contains(name) else {
            runLog += "\(name) no longer looks crashed — its lock is live again (shepherd may have relaunched it).\n"
            refresh()
            return
        }
        let pid = appLocks[name]?.pid
        runLog += pid.map { "Resuming \(name) — its previous run (pid \($0)) is gone.\n" }
            ?? "Resuming \(name) — its run lock named no pid.\n"
        crashedRuns.removeAll { $0.name == name }
        staleLockFirstSeen[name] = nil
        // orchestratorRunning is a 240s state-mtime heuristic: the crashed app
        // counts ITSELF as running for a while, and a plain runOrQueue would
        // park the resume behind the corpse it is resuming. Launch immediately
        // when the crashed app is the only thing that looks busy.
        if ResumeAdvisor.immediateLaunchAllowed(
            resuming: name,
            runningProjectNames: Set(projects.filter(\.running).map(\.name)),
            launchingName: launchingName,
            queueEmpty: runQueue.isEmpty) {
            launchQueued(name)
        } else {
            runOrQueue(name)
        }
    }

    // MARK: - Chat sessions (V3 board 1.5)

    // Mint a flat chat dir per GLOSSARY "Layout (M1 interim)" and register the
    // session SYNCHRONOUSLY — the 1.5s background scan hasn't discovered the
    // dir yet, and navigation must not race it (the chat surface renders from
    // this ChatSession, tolerating a missing Project for the first ticks).
    @discardableResult
    func mintChatSession(project: String, section: String, title: String,
                         workflow: String, firstMessage: String) -> ChatSession? {
        do {
            let minted = try ChatSessionMint.mintChatDir(
                rootURL: rootURL, project: project, section: section,
                title: title, workflow: workflow, firstMessage: firstMessage)
            let session = ChatSession(
                id: minted.name,
                project: OrchestratorStore.slugify(project),
                section: OrchestratorStore.slugify(section),
                slug: OrchestratorStore.slugify(title),
                workflow: workflow)
            chatSessions[minted.name] = session
            refresh()
            return session
        } catch {
            surfaceError("Couldn't create the chat: \(error.localizedDescription)")
            return nil
        }
    }

    func startChatSession(_ id: String) {
        guard var s = chatSessions[id] else { return }
        switch s.state {
        case .launching, .running, .waitingForHuman, .stopping, .relaunching:
            return   // already alive — never double-launch one dir
        case .ended:
            // The engine SKIPS done apps ("already done"): relaunching an
            // ended chat is a no-op that would flash launching→stopped.
            surfaceError("This chat has ended — start a new chat instead.")
            return
        case .stopped, .crashed:
            s.state = .relaunching
        case .idle:
            s.state = .launching
        }
        chatSessions[id] = s
        launch(args: ["orchestrator.py", "--root", rootURL.path, "--app", id],
               project: id)
        // R2: 'running' only with a live handle backing it.
        if runningProcesses[id]?.isRunning == true {
            chatSessions[id]?.state = .running
        } else if chatSessions[id]?.state.isAlive == true {
            chatSessions[id]?.state = .crashed(code: -1, wasSignal: false)
            surfaceError("The chat engine failed to launch — see the run log.")
        }
    }

    func stopChatSession(_ id: String) {
        guard chatSessions[id]?.state.isAlive == true else { return }
        chatSessions[id]?.state = .stopping
        stopProject(id)   // SIGTERM→grace→SIGKILL + owner-checked lock cleanup
    }

    // 'End chat' = the 1.1 engine contract: approvals/<phaseKey>.ok. Only
    // meaningful while the engine is alive to consume it — a leftover end
    // file on a STOPPED chat would arm an instant end on the next launch
    // (the engine deliberately honors a pre-existing .ok on resume).
    func endChatSession(_ id: String) {
        guard let s = chatSessions[id], s.state.isAlive else {
            surfaceError("The chat isn't running — an end command now would "
                         + "end it instantly on the next launch instead.")
            return
        }
        // Phase key from the workflow def's conversational phase — never
        // hardcoded, never derived by splitting the dir name.
        let key = workflow(named: s.workflow)?.phases.first(where: \.conversational)?.key ?? "chat"
        let apprDir = rootURL.appendingPathComponent("\(id)/approvals")
        do {
            try fm.createDirectory(at: apprDir, withIntermediateDirectories: true)
            try Data().write(to: apprDir.appendingPathComponent("\(key).ok"))
        } catch {
            surfaceError("Couldn't end the chat: \(error.localizedDescription)")
        }
    }

    // V3 board 1.11: "retry with…" — one agent re-answers the current round
    // on a chosen model. File contract: approvals/<phase>.retry {agent,
    // model}; the engine renames it .consumed before running (rename-then-
    // run: a crash loses one retry rather than double-running it).
    func requestChatRetry(_ id: String, phaseKey: String, agent: String,
                          model: String) {
        let dir = rootURL.appendingPathComponent("\(id)/approvals")
        try? fm.createDirectory(at: dir, withIntermediateDirectories: true)
        if let data = try? JSONSerialization.data(
                withJSONObject: ["agent": agent, "model": model]) {
            try? data.write(to: dir.appendingPathComponent("\(phaseKey).retry"))
        }
    }

    // V3 board 1.11: mid-chat model swap — merge one per-agent override into
    // the CHAT dir's model_routing.json (fleet file untouched). The engine's
    // conversational loop re-reads it at the next round barrier; the chip
    // shows a pending state until a message_produced event confirms the new
    // model actually ran (R2 — never claim the swap before it is real).
    func setChatModelOverride(_ id: String, phaseKey: String, agent: String,
                              model: String?) {
        let url = rootURL.appendingPathComponent("\(id)/model_routing.json")
        var obj: [String: Any] = ["schema_version": 1, "enabled": true]
        if let data = try? Data(contentsOf: url),
           let existing = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any] {
            obj = existing
        }
        var phases = obj["phases"] as? [String: Any] ?? [:]
        var ph = phases[phaseKey] as? [String: Any] ?? [:]
        if let model { ph[agent] = model } else { ph.removeValue(forKey: agent) }
        phases[phaseKey] = ph
        obj["phases"] = phases
        if let data = try? JSONSerialization.data(withJSONObject: obj,
                                                  options: [.prettyPrinted, .sortedKeys]) {
            try? data.write(to: url)
        }
    }

    // V3 board 1.8 ("Let them discuss"): promote a chat session to an auto
    // debate. A LIVE chat is ended first (pendingPromote defers the handoff
    // to the termination reducer); the promotion itself runs through the
    // engine CLI (--promote does the state surgery, then the SAME process
    // runs the debate), launched via the ordinary tracked project path — so
    // from this moment the dir is a plain project: the ChatSession entry is
    // removed and the run surface takes over (never the chat reducer's
    // .ended dead-end).
    private var pendingPromote: Set<String> = []

    // V3 board 1.9: fork a (not-running) chat session via the engine CLI,
    // which strips agent threads/locks/approvals/inbox; the printed
    // "FORKED: <name>" line names the new dir, which is registered as an
    // idle ChatSession so it appears in the strip immediately.
    func forkChatSession(_ id: String) {
        guard let s = chatSessions[id], !s.state.isAlive else {
            surfaceError("Stop the chat before forking — a mid-write copy could tear a round in half.")
            return
        }
        let py = resolvePython()
        let engine = orchDirURL.appendingPathComponent("orchestrator.py").path
        let root = rootURL.path
        let workflow = s.workflow
        Task.detached { [weak self] in
            let proc = Process()
            proc.executableURL = URL(fileURLWithPath: py)
            proc.arguments = [engine, "--root", root, "--fork", id]
            let pipe = Pipe()
            proc.standardOutput = pipe
            proc.standardError = pipe
            try? proc.run()
            proc.waitUntilExit()
            let out = String(data: pipe.fileHandleForReading.readDataToEndOfFile(),
                             encoding: .utf8) ?? ""
            let name = out.split(separator: "\n")
                .first { $0.hasPrefix("FORKED: ") }
                .map { String($0.dropFirst("FORKED: ".count)) }
            await MainActor.run { [weak self] in
                guard let self else { return }
                guard proc.terminationStatus == 0, let name else {
                    self.surfaceError("Fork failed — see the run log.")
                    self.runLog += out
                    return
                }
                let parts = name.contains("/")
                    ? name.components(separatedBy: "/")
                    : name.components(separatedBy: "--")
                self.chatSessions[name] = ChatSession(
                    id: name,
                    project: parts.count == 3 ? parts[0] : name,
                    section: parts.count == 3 ? parts[1] : "",
                    slug: parts.count == 3 ? parts[2] : name,
                    workflow: workflow)
                self.refresh()
            }
        }
    }

    func promoteChatSession(_ id: String) {
        guard let s = chatSessions[id] else { return }
        if s.state.isAlive {
            pendingPromote.insert(id)
            endChatSession(id)
            return
        }
        performPromotion(id)
    }

    private func performPromotion(_ id: String) {
        pendingPromote.remove(id)
        chatSessions.removeValue(forKey: id)
        launch(args: ["orchestrator.py", "--root", rootURL.path, "--promote", id],
               project: id)
        refresh()
    }

    // Termination reducer entry (from launch()'s terminationHandler): reads
    // the FINAL agent_state.json to distinguish ended (done=true) from
    // stopped — the pure mapping lives in ChatSessionState.afterTermination.
    func noteChatTermination(name: String, status: Int32, uncaughtSignal: Bool) {
        guard let s = chatSessions[name] else { return }
        let wasStopping = (s.state == .stopping)
        var done = false
        var endReason: String? = nil
        let stateURL = rootURL.appendingPathComponent("\(name)/agent_state.json")
        if let data = try? Data(contentsOf: stateURL),
           let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
            done = (obj["done"] as? Bool) ?? false
            let phaseKey = workflow(named: s.workflow)?.phases.first(where: \.conversational)?.key ?? "chat"
            endReason = (obj["conversation_end"] as? [String: String])?[phaseKey]
        }
        let next = ChatSessionState.afterTermination(
            status: status, uncaughtSignal: uncaughtSignal,
            wasStopping: wasStopping, stateDone: done, conversationEnd: endReason)
        chatSessions[name]?.state = next
        if case .crashed(let code, let wasSignal) = next {
            // A crash cancels a pending promotion — promoting a half-written
            // chat must be the user's explicit second decision, not automatic.
            pendingPromote.remove(name)
            surfaceError(wasSignal
                ? "Chat '\(name)' was killed by signal \(code)."
                : "Chat '\(name)' crashed with exit code \(code).")
            return
        }
        if pendingPromote.contains(name) {
            performPromotion(name)
        }
    }

    // Scan merge: only waiting↔running transitions, gated on process
    // liveness (awaiting_human survives kill -9 in agent_state.json — a dead
    // chat must never show 'waiting for you'). Terminal states are owned by
    // the termination reducer, not the scan.
    func syncChatSessions(with projects: [Project]) {
        guard !chatSessions.isEmpty else { return }
        let byName = Dictionary(projects.map { ($0.name, $0) },
                                uniquingKeysWith: { a, _ in a })
        for (id, session) in chatSessions {
            let alive = runningProcesses[id]?.isRunning == true
            let awaiting = byName[id]?.awaitingHuman != nil
            let next = ChatSessionState.applyingScan(
                current: session.state, awaitingHuman: awaiting, processAlive: alive)
            if next != session.state { chatSessions[id]?.state = next }
        }
    }

    // MARK: - Workflows + sub-agents (roles / personalities)

    var workflowsDirURL: URL { orchDirURL.appendingPathComponent("workflows", isDirectory: true) }
    var rolesURL: URL { orchDirURL.appendingPathComponent("roles.json") }
    private var modelPresetsURL: URL { orchDirURL.appendingPathComponent("model_presets.json") }

    // Publish a background-loaded config snapshot, touching each @Published
    // property only when its value actually changed (no spurious re-renders).
    private func apply(_ snap: BackgroundConfigLoader.Snapshot) {
        if snap.workflows != workflows { workflows = snap.workflows }
        if snap.roles != roles { roles = snap.roles }
        if snap.personalities != personalities { personalities = snap.personalities }
        if let overrides = snap.agentRoleOverrides, overrides != agentRoleOverrides {
            agentRoleOverrides = overrides
        }
        if snap.customModelPresets != customModelPresets { customModelPresets = snap.customModelPresets }
        if snap.enabledAgents != enabledAgents { enabledAgents = snap.enabledAgents }
        if snap.agentModels != agentModels { agentModels = snap.agentModels }
        if snap.agentEfforts != agentEfforts { agentEfforts = snap.agentEfforts }
        if snap.cliAvailable != cliAvailable { cliAvailable = snap.cliAvailable }
    }

    func workflow(named name: String) -> WorkflowDef? {
        workflows.first { $0.name == name }
    }

    // The phases a given project runs (from its workflow; falls back to app_build).
    func phases(for project: Project) -> [PhaseDef] {
        (workflow(named: project.workflow) ?? workflow(named: "app_build"))?.phases ?? ALL_PHASES
    }

    // MARK: - Config (agents on/off) — edits config.yaml lines in place so the
    // rest of the file (comments, models, rounds) is preserved.

    private var configURL: URL { orchDirURL.appendingPathComponent("config.yaml") }

    /// Persist updated config.yaml text. On failure the error is surfaced in the
    /// run log instead of being swallowed by `try?`, and false is returned so
    /// callers skip the matching @Published update — otherwise the UI would show
    /// a setting that never reached disk. Returns true on a successful write.
    @discardableResult
    private func writeConfig(_ text: String) -> Bool {
        do {
            try text.write(to: configURL, atomically: true, encoding: .utf8)
            return true
        } catch {
            surfaceError("Failed to save settings to \(configURL.lastPathComponent): "
                + error.localizedDescription)
            return false
        }
    }

    func setAgentEnabled(_ agent: String, _ on: Bool) {
        guard var text = try? String(contentsOf: configURL, encoding: .utf8) else {
            surfaceError("Could not read \(configURL.lastPathComponent) to update \(agent).")
            return
        }
        let pattern = "(\(agent)_enabled:\\s*)(true|false)"
        guard let re = try? NSRegularExpression(pattern: pattern) else { return }
        let range = NSRange(text.startIndex..<text.endIndex, in: text)
        // No match means the key doesn't exist in config.yaml — surface that
        // instead of silently no-op'ing, matching the read/write-failure siblings.
        guard re.firstMatch(in: text, range: range) != nil else {
            surfaceError("Could not find \(agent)_enabled in \(configURL.lastPathComponent).")
            return
        }
        text = re.stringByReplacingMatches(in: text, range: range,
                                           withTemplate: "$1\(on ? "true" : "false")")
        // Only reflect the toggle in the UI if the write actually landed.
        if writeConfig(text) {
            enabledAgents[agent] = on
        }
    }

    // MARK: - macOS notifications (run finished / needs approval / failed)

    private(set) var notifAuthRequested = false
    private var lastStatusKey: [String: String] = [:]
    // Notifications need a bundle identity; skip when run as a raw executable so we
    // never crash on UNUserNotificationCenter with a nil bundle id. The
    // suppression seam exists because the xctest runner HAS a bundle id.
    private var notificationsAvailable: Bool {
        !Self.suppressNotifications && Bundle.main.bundleIdentifier != nil
    }

    func requestNotificationAuthIfNeeded() {
        guard notificationsAvailable, !notifAuthRequested else { return }
        notifAuthRequested = true
        UNUserNotificationCenter.current().requestAuthorization(options: [.alert, .sound]) { _, _ in }
    }

    private func notify(_ title: String, _ body: String) {
        guard notificationsAvailable else { return }
        let c = UNMutableNotificationContent()
        c.title = title; c.body = body
        UNUserNotificationCenter.current().add(
            UNNotificationRequest(identifier: UUID().uuidString, content: c, trigger: nil))
    }

    private func statusKey(_ p: Project) -> String {
        if p.awaitingApproval != nil { return "awaiting" }
        switch p.status {
        case .done: return "done"
        case .aborted: return "aborted"
        case .inProgress: return "running"
        case .new: return "new"
        }
    }

    // §6 escalation thresholds: ≥3 consecutive fallbacks on one agent, or any
    // rescue to a <7B model on a review/verify phase → macOS notification.
    // Tracks the last-notified rescue count per project so each escalation
    // fires once, and never on first sight (relaunches don't replay history).
    private var notifiedRescueCounts: [String: Int] = [:]

    private func escalateFallbacksIfNeeded(_ eventsByProject: [String: [EngineEvent]]) {
        for (project, events) in eventsByProject {
            let rescues = events.filter { $0.isFallback && $0.status == "rescued" }
            let prev = notifiedRescueCounts[project]
            notifiedRescueCounts[project] = rescues.count
            guard let prev, rescues.count > prev else { continue }
            let fresh = rescues.suffix(rescues.count - prev)
            // Any rescue to a small model on a review/verify phase.
            if let bad = fresh.first(where: { e in
                let p = e.phase.lowercased()
                let tag = e.toModel.hasPrefix("local:") ? String(e.toModel.dropFirst(6)) : e.toModel
                return (p.contains("review") || p.contains("verif"))
                    && (RoutingConsequences.parameterBillions(tag) ?? 99) < 7
            }) {
                notify("\(project): reviews degraded",
                       "Rescued by \(bad.toModel) on \(bad.prettyPhase).")
                continue
            }
            // ≥3 consecutive rescues on one agent within the current run.
            let states = RunHealthDeriver.agentStates(events: events, projectRunning: true)
            if let worst = states.max(by: { $0.consecutiveFallbacks < $1.consecutiveFallbacks }),
               worst.consecutiveFallbacks >= 3 {
                notify("\(project): \(DS.identity(worst.agent).displayName) degraded",
                       "\(worst.consecutiveFallbacks) consecutive fallbacks — running on \(worst.actualModel).")
            }
        }
    }

    // Fire a notification on meaningful state transitions. Never fires on first
    // sight of a project (no prior state), so relaunch doesn't spam old results.
    private func detectTransitions(_ loaded: [Project]) {
        for p in loaded {
            let key = statusKey(p)
            defer { lastStatusKey[p.name] = key }
            guard let prev = lastStatusKey[p.name], prev != key else { continue }
            if key == "done", prev == "running" || prev == "awaiting" {
                notify("Build finished", "\(p.name) completed.")
            } else if key == "aborted", prev == "running" || prev == "awaiting" {
                notify("Run failed", "\(p.name) stopped. Check the transcript.")
            } else if key == "awaiting", prev != "awaiting" {
                notify("Needs your approval", "\(p.name) paused at a checkpoint.")
            }
        }
    }

    // MARK: - Local models (Ollama)

    // Curated recommended local models (V2 spec §12.3) — a static fallback for
    // before the first doctor fetch (the live truth is `localModels` below).
    let recommendedLocalModels: [(id: String, label: String, license: String, minRAM: Int)] = [
        ("qwen3-coder:30b", "Best local coding agent", "Apache-2.0", 32),
        ("glm-5.2:latest", "GLM 5.2 reasoner", "Apache-2.0", 16),
        ("qwen3-max:latest", "Qwen 3 Max", "Apache-2.0", 48),
        ("qwen3.7:latest", "Qwen 3.7", "Apache-2.0", 24),
        ("kimi-k2-thinking:latest", "Kimi K2 Thinking", "Apache-2.0", 32),
        ("kimi-k2.6:latest", "Kimi K2.6", "Apache-2.0", 24),
        ("qwen2.5-coder:7b", "Fast local coding assistant", "Apache-2.0", 16),
        ("devstral:24b", "Agentic local coding worker", "Apache-2.0", 32),
        ("deepseek-r1:8b", "Local reasoning reviewer", "MIT / Apache-2.0 base", 16),
        ("deepseek-r1:14b", "Stronger local reasoner", "MIT / Apache-2.0 base", 24),
        ("deepseek-v4-pro:latest", "DeepSeek V4 Pro", "Apache-2.0", 32),
        ("mistral:7b", "Small local reviewer", "Apache-2.0", 16),
    ]

    var systemMemoryGB: Int {
        Int((ProcessInfo.processInfo.physicalMemory + 1_073_741_823) / 1_073_741_824)
    }

    // The engine's `--doctor --json` local_models block: server running,
    // selected model, installed flags per curated registry entry. nil until
    // the first fetch completes (or when the engine/JSON is unavailable).
    @Published var localModels: LocalModelsInfo?
    // V3 6.1: the doctor's agent_capabilities block. DS.identity(_:) merges
    // it over the static fallback table for every reader (EffortPicker and
    // friends); this published copy exists so SwiftUI invalidates observing
    // views the moment the block lands or changes.
    @Published var agentCapabilities: AgentCapabilitiesInfo?
    private var doctorFetchInFlight = false

    // Re-run the engine doctor and republish the local_models block. Async and
    // best-effort: a dead python/engine just leaves the previous value in place.
    func refreshLocalModels() {
        guard engineAvailable, !doctorFetchInFlight else { return }
        doctorFetchInFlight = true
        let proc = Process()
        proc.executableURL = URL(fileURLWithPath: resolvePython())
        proc.arguments = [orchDirURL.appendingPathComponent("orchestrator.py").path,
                          "--root", rootURL.path, "--doctor", "--json"]
        proc.currentDirectoryURL = rootURL
        let out = Pipe()
        proc.standardOutput = out
        proc.standardError = Pipe()
        proc.terminationHandler = { [weak self] _ in
            let data = out.fileHandleForReading.readDataToEndOfFile()
            Task { @MainActor in
                self?.doctorFetchInFlight = false
                if let info = DoctorReportParser.localModels(fromDoctorJSON: data) {
                    self?.localModels = info
                }
                // V3 6.1: same payload carries the capability descriptors.
                // Absent block (older engine) leaves the static fallback in
                // force — never clears a previously-loaded map to nil.
                if let caps = DoctorReportParser.agentCapabilities(fromDoctorJSON: data) {
                    self?.agentCapabilities = caps
                    DS.engineCapabilities = caps
                }
            }
        }
        do { try proc.run() } catch { doctorFetchInFlight = false }
    }

    // Pull a local model — but ONLY one named by the engine's curated registry
    // (or its static fallback). No arbitrary/user text ever becomes a command
    // (spec §12.3), and a registry id with shell-unsafe characters is refused.
    func pullLocalModel(_ id: String) {
        guard localModelIdIsKnownAndSafe(id) else { return }
        runInTerminal("ollama pull \(id)")
    }

    func deleteLocalModel(_ id: String) {
        guard localModelIdIsKnownAndSafe(id) else { return }
        runInTerminal("ollama rm \(id)")
    }

    func startOllamaServer() {
        guard ollamaOnPath() else { return }
        runInTerminal("ollama serve")
    }

    func localModelIdIsSafe(_ id: String) -> Bool {
        BackgroundConfigLoader.modelIDIsSafe(id)
    }

    private func localModelIdIsKnownAndSafe(_ id: String) -> Bool {
        if let reg = localModels?.registry, !reg.isEmpty {
            guard let entry = reg.first(where: { $0.id == id }) else { return false }
            return entry.idIsSafeForShell
        }
        return recommendedLocalModels.contains { $0.id == id }
    }

    @discardableResult
    func addLocalModel(id rawID: String, license: String) -> Bool {
        let id = rawID.trimmingCharacters(in: .whitespacesAndNewlines)
        guard localModelIdIsSafe(id) else { return false }
        let url = orchDirURL.appendingPathComponent("local_models.json")
        var obj: [String: Any] = ["schema_version": 1, "models": []]
        if let data = try? Data(contentsOf: url),
           let existing = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
            obj = existing
        }
        var models = obj["models"] as? [[String: Any]] ?? []
        if models.contains(where: { ($0["id"] as? String) == id }) {
            setModel("ollama", id)
            return true
        }
        models.append([
            "id": id,
            "label": "Custom Local Model",
            "runtime": "ollama",
            "pull_command": ["ollama", "pull", id],
            "license": license,
            "license_url": "",
            "commercial_use": true,
            "min_ram_gb": 16,
            "recommended_ram_gb": 32,
            "roles": ["custom", "review", "implementation"],
            "notes": "Added from the GUI. Confirm the upstream model card before redistribution."
        ])
        obj["schema_version"] = obj["schema_version"] ?? 1
        obj["models"] = models
        guard writeJSON(obj, to: url) else { return false }
        setModel("ollama", id)
        refreshLocalModels()
        return true
    }

    func updateInstalledLocalModels() {
        let installed = (localModels?.registry ?? []).filter { $0.installed && $0.idIsSafeForShell }
        guard !installed.isEmpty else {
            runLog += "No installed local models to update.\n"
            return
        }
        let command = installed.map { "ollama pull \($0.id)" }.joined(separator: " && ")
        runInTerminal(command)
    }

    // MARK: - Model Library: search, in-app downloads, roster (V2 spec §12)

    // Live results of the last open-model search (engine --search-models --json:
    // curated registry + Hugging Face GGUF repos, both pullable via ollama).
    @Published var modelSearchResults: [RemoteModelHit] = []
    @Published var modelSearchNote = ""
    @Published var modelSearchInFlight = false

    // In-app pull progress per model id: -1 = starting/indeterminate, 0..1 =
    // fraction of the largest layer. Absent = not pulling.
    @Published var pullProgress: [String: Double] = [:]

    func searchOpenModels(_ query: String) {
        let q = query.trimmingCharacters(in: .whitespacesAndNewlines)
        guard engineAvailable, !q.isEmpty, !modelSearchInFlight else { return }
        modelSearchInFlight = true
        let proc = Process()
        proc.executableURL = URL(fileURLWithPath: resolvePython())
        proc.arguments = [orchDirURL.appendingPathComponent("orchestrator.py").path,
                          "--search-models", q, "--json"]
        proc.currentDirectoryURL = rootURL
        let out = Pipe()
        proc.standardOutput = out
        proc.standardError = Pipe()
        proc.terminationHandler = { [weak self] _ in
            let data = out.fileHandleForReading.readDataToEndOfFile()
            Task { @MainActor in
                self?.modelSearchInFlight = false
                let parsed = RemoteModelHit.parse(searchJSON: data)
                self?.modelSearchResults = parsed.hits
                self?.modelSearchNote = parsed.note
            }
        }
        do { try proc.run() } catch { modelSearchInFlight = false }
    }

    // Download a model through Ollama's loopback API with live progress —
    // no Terminal window. Falls back to the Terminal path when the server
    // isn't running (the API needs a live daemon; `ollama pull` self-starts one).
    func pullModelInApp(_ id: String) {
        guard localModelIdIsSafe(id), pullProgress[id] == nil else { return }
        guard localModels?.serverRunning == true else {
            runInTerminal("ollama pull \(id)")
            return
        }
        guard let pullURL = URL(string: "http://127.0.0.1:11434/api/pull") else {
            runInTerminal("ollama pull \(id)")
            return
        }
        pullProgress[id] = -1
        Task { [weak self] in
            do {
                var req = URLRequest(url: pullURL)
                req.httpMethod = "POST"
                req.httpBody = try JSONSerialization.data(withJSONObject: ["name": id])
                req.timeoutInterval = 3600
                let (bytes, _) = try await URLSession.shared.bytes(for: req)
                for try await line in bytes.lines {
                    guard let obj = try? JSONSerialization.jsonObject(with: Data(line.utf8))
                            as? [String: Any] else { continue }
                    if let err = obj["error"] as? String {
                        throw NSError(domain: "ollama", code: 1,
                                      userInfo: [NSLocalizedDescriptionKey: err])
                    }
                    let total = (obj["total"] as? Double) ?? 0
                    let done = (obj["completed"] as? Double) ?? 0
                    if total > 0 {
                        await MainActor.run { self?.pullProgress[id] = min(0.999, done / total) }
                    }
                }
                await MainActor.run {
                    self?.pullProgress[id] = nil
                    self?.runLog += "Pulled local model \(id).\n"
                    self?.modelSearchResults = (self?.modelSearchResults ?? []).map {
                        var hit = $0
                        if hit.id == id { hit.installed = true }
                        return hit
                    }
                    self?.refreshLocalModels()
                }
            } catch {
                await MainActor.run {
                    self?.pullProgress[id] = nil
                    self?.runLog += "Pull failed for \(id): \(error.localizedDescription)\n"
                }
            }
        }
    }

    // Add a search hit to local_models.json (making it a first-class curated
    // entry the engine and roster can use), then start the download. License
    // metadata is carried over honestly — commercial_use is only asserted for
    // known-permissive licenses; anything else is flagged for review.
    func addOpenModel(_ hit: RemoteModelHit) {
        guard localModelIdIsSafe(hit.id) else { return }
        let permissive = ["apache-2.0", "mit", "bsd", "bsd-2-clause", "bsd-3-clause"]
        let lic = hit.license.lowercased()
        let url = orchDirURL.appendingPathComponent("local_models.json")
        var obj: [String: Any] = ["schema_version": 1, "models": []]
        if let data = try? Data(contentsOf: url),
           let existing = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
            obj = existing
        }
        var models = obj["models"] as? [[String: Any]] ?? []
        if !models.contains(where: { ($0["id"] as? String) == hit.id }) {
            models.append([
                "id": hit.id,
                "label": hit.label.isEmpty ? "From Model Library search" : hit.label,
                "runtime": "ollama",
                "pull_command": ["ollama", "pull", hit.id],
                "license": hit.license.isEmpty ? "unknown" : hit.license,
                "license_url": "",
                "commercial_use": permissive.contains(lic),
                "min_ram_gb": 16,
                "recommended_ram_gb": 32,
                "roles": ["custom", "review", "implementation"],
                "notes": "Added from Model Library search (\(hit.source)). Confirm the upstream model card and license before commercial use."
            ])
            obj["models"] = models
            writeJSON(obj, to: url)
        }
        pullModelInApp(hit.id)
        refreshLocalModels()
    }

    // The mix-and-match roster (config models.ollama_roster): which local
    // models join runs as extra participants alongside the cloud agents.
    func readLocalRoster() -> [String] {
        guard let text = try? String(contentsOf: configURL, encoding: .utf8),
              let raw = firstMatch(in: text, pattern: "(?m)^\\s*ollama_roster:\\s*\"?([^\"\\n#]*)") else {
            return []
        }
        return raw.split(whereSeparator: { $0 == "," || $0 == ";" })
            .map { $0.trimmingCharacters(in: .whitespaces) }
            .filter { !$0.isEmpty }
    }

    func setRosterMembership(_ id: String, _ include: Bool) {
        guard localModelIdIsSafe(id) else { return }
        var roster = readLocalRoster()
        if include, !roster.contains(id) { roster.append(id) }
        if !include { roster.removeAll { $0 == id } }
        guard var text = try? String(contentsOf: configURL, encoding: .utf8) else { return }
        let value = roster.joined(separator: ", ")
        let pattern = "(?m)^(\\s*ollama_roster:\\s*).*$"
        if let re = try? NSRegularExpression(pattern: pattern) {
            let range = NSRange(text.startIndex..<text.endIndex, in: text)
            if re.firstMatch(in: text, range: range) != nil {
                text = re.stringByReplacingMatches(in: text, range: range,
                                                   withTemplate: "$1\"\(value)\"")
            } else if let modelsRange = text.range(of: "models:\n") {
                text.insert(contentsOf: "  ollama_roster: \"\(value)\"\n",
                            at: modelsRange.upperBound)
            }
            writeConfig(text)
        }
        objectWillChange.send()
    }

    // MARK: - Per-phase model routing (model_routing.json)

    var modelRoutingURL: URL { orchDirURL.appendingPathComponent("model_routing.json") }

    func readModelRouting() -> ModelRouting {
        cachedModelRouting(at: modelRoutingURL)
    }

    func writeModelRouting(_ routing: ModelRouting) {
        routing.save(to: modelRoutingURL)
        modelRoutingCache[modelRoutingURL] = nil
        objectWillChange.send()
    }

    // Per-project routing overrides (<project>/model_routing.json) — the Plan
    // tab's file (M3). Same schema as the fleet file; resolved over it by the
    // grid. (Engine-side per-project resolution is the tracked follow-up.)
    func projectRoutingURL(_ project: Project) -> URL {
        project.dirURL.appendingPathComponent("model_routing.json")
    }

    func readProjectRouting(_ project: Project) -> ModelRouting {
        cachedModelRouting(at: projectRoutingURL(project))
    }

    func writeProjectRouting(_ routing: ModelRouting, for project: Project) {
        let url = projectRoutingURL(project)
        routing.save(to: url)
        modelRoutingCache[url] = nil
        objectWillChange.send()
    }

    // MARK: - Library: reusable phase-prompt snippets + saved run profiles
    //
    // Snippets: <engine>/library/snippets.json — [{name, phase, text}]; phase
    // "" = usable anywhere. Profiles: <engine>/library/profiles/<slug>.json —
    // a model_routing.json-shaped file (per-phase models/effort/rounds/
    // instructions + fallback chains) plus profile_name/workflow keys the
    // engine loader ignores. Applying a profile materializes the project's
    // model_routing.json and workflow.txt, so different apps can carry
    // different requirements with one click.

    var libraryDirURL: URL { orchDirURL.appendingPathComponent("library", isDirectory: true) }
    var snippetsURL: URL { libraryDirURL.appendingPathComponent("snippets.json") }
    var profilesDirURL: URL { libraryDirURL.appendingPathComponent("profiles", isDirectory: true) }

    func loadSnippets() -> [PromptSnippet] {
        guard let data = try? Data(contentsOf: snippetsURL),
              let arr = (try? JSONSerialization.jsonObject(with: data)) as? [[String: Any]]
        else { return [] }
        return arr.compactMap { o in
            guard let name = o["name"] as? String, !name.isEmpty,
                  let text = o["text"] as? String, !text.isEmpty else { return nil }
            return PromptSnippet(name: name, phase: (o["phase"] as? String) ?? "",
                                 text: text)
        }
    }

    func saveSnippets(_ snippets: [PromptSnippet]) {
        try? fm.createDirectory(at: libraryDirURL, withIntermediateDirectories: true)
        let arr = snippets.map { ["name": $0.name, "phase": $0.phase, "text": $0.text] }
        if let data = try? JSONSerialization.data(withJSONObject: arr,
                                                  options: [.prettyPrinted, .sortedKeys]) {
            try? data.write(to: snippetsURL, options: .atomic)
        }
        objectWillChange.send()
    }

    func listProfiles() -> [RunProfile] {
        guard let items = try? fm.contentsOfDirectory(atPath: profilesDirURL.path)
        else { return [] }
        return items.filter { $0.hasSuffix(".json") }.sorted().compactMap { fn in
            let url = profilesDirURL.appendingPathComponent(fn)
            guard let data = try? Data(contentsOf: url),
                  let obj = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any]
            else { return nil }
            return RunProfile(name: (obj["profile_name"] as? String)
                                ?? fn.replacingOccurrences(of: ".json", with: ""),
                              workflow: (obj["workflow"] as? String) ?? "",
                              url: url)
        }
    }

    // Snapshot a project's per-phase setup (routing + rounds + instructions +
    // fallback chains) and its workflow as a named, reusable profile.
    func saveProfile(named name: String, from project: Project) {
        let trimmed = name.trimmingCharacters(in: .whitespaces)
        guard !trimmed.isEmpty else { return }
        try? fm.createDirectory(at: profilesDirURL, withIntermediateDirectories: true)
        var obj: [String: Any] = [:]
        if let data = try? Data(contentsOf: projectRoutingURL(project)),
           let parsed = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any] {
            obj = parsed
        } else {
            obj = ["schema_version": 1, "enabled": true,
                   "fallback": ["cloud_to_local": true, "local_model": ""],
                   "phases": [:] as [String: Any]]
        }
        obj["profile_name"] = trimmed
        obj["workflow"] = project.workflow
        let slug = NewAppIntakeSheet.slugify(trimmed)
        let url = profilesDirURL.appendingPathComponent(slug + ".json")
        if let data = try? JSONSerialization.data(withJSONObject: obj,
                                                  options: [.prettyPrinted, .sortedKeys]) {
            try? data.write(to: url, options: .atomic)
            runLog += "Saved profile “\(trimmed)” (\(slug).json).\n"
        }
        objectWillChange.send()
    }

    // Materialize a profile onto a project: per-phase routing file + workflow.
    func applyProfile(_ profile: RunProfile, toProjectNamed name: String) {
        guard let data = try? Data(contentsOf: profile.url),
              var obj = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any]
        else { return }
        obj.removeValue(forKey: "profile_name")
        let wf = obj.removeValue(forKey: "workflow") as? String
        let dir = rootURL.appendingPathComponent(name)
        let routingURL = dir.appendingPathComponent("model_routing.json")
        if let out = try? JSONSerialization.data(withJSONObject: obj,
                                                 options: [.prettyPrinted, .sortedKeys]) {
            try? out.write(to: routingURL, options: .atomic)
            modelRoutingCache[routingURL] = nil
        }
        if let wf, !wf.isEmpty {
            try? (wf + "\n").write(to: dir.appendingPathComponent("workflow.txt"),
                                   atomically: true, encoding: .utf8)
        }
        runLog += "Applied profile “\(profile.name)” to \(name).\n"
        objectWillChange.send()
    }

    func deleteProfile(_ profile: RunProfile) {
        try? fm.trashItem(at: profile.url, resultingItemURL: nil)
        objectWillChange.send()
    }

    // Human rating (fleet learning): <project>/rating.json — consumed by the
    // engine's presort/anti-pattern ledger/eval harness (--fleet-report).
    func rateProject(_ project: Project, verdict: String?) {
        let url = project.dirURL.appendingPathComponent("rating.json")
        if let verdict {
            let obj: [String: Any] = ["verdict": verdict,
                                      "ts": ISO8601DateFormatter().string(from: Date())]
            if let data = try? JSONSerialization.data(withJSONObject: obj,
                                                      options: [.prettyPrinted, .sortedKeys]) {
                try? data.write(to: url, options: .atomic)
                runLog += "Rated \(project.name) \(verdict).\n"
                // A good project immediately teaches: its phase outputs
                // become few-shot exemplars future phase runs see.
                if verdict == "good" {
                    launch(args: ["orchestrator.py", "--root", rootURL.path,
                                  "--save-exemplar", project.name])
                }
            }
        } else {
            try? fm.removeItem(at: url)
            runLog += "Cleared rating on \(project.name).\n"
        }
        objectWillChange.send()
    }

    // MARK: - Concierge chat (Home)
    //
    // One headless `claude -p` call per user message — the same logged-in CLI
    // and no-API-key contract as engine launches (key env vars stripped, so
    // the chat costs subscription tokens only). nonisolated static: runs off
    // the main actor. nil = CLI missing / errored / empty (the view degrades
    // to the static mode cards).
    nonisolated static func conciergeAsk(history: [(String, String)],
                                         workflowList: [String]) async -> String? {
        let fm = FileManager.default
        let home = fm.homeDirectoryForCurrentUser.path
        let candidates = ["\(home)/.local/bin/claude", "/opt/homebrew/bin/claude",
                          "/usr/local/bin/claude"]
        guard let bin = candidates.first(where: { fm.isExecutableFile(atPath: $0) })
        else { return nil }

        let system = """
        You are the concierge for Orchestrator, a local multi-agent app factory. \
        The user chats with you to shape work into a "run". A run = a project \
        folder + a workflow the debate engine executes with several AI CLIs.

        Available workflows:
        \(workflowList.joined(separator: "\n"))

        The six front-door modes: Ask (answer_question) answers a question; \
        Plan (brainstorm) explores ideas cheaply; Spec (app_spec) produces a \
        full product spec with no code; Create (app_build) runs the full \
        debate→spec→design→build→verify pipeline; Research (research) produces \
        a sourced report; Audit (audit) reviews an existing repo read-only.

        Be brief and concrete. Ask at most ONE clarifying question at a time, \
        and only when genuinely blocking. As soon as the idea is actionable, \
        propose a run by appending exactly one fenced block:

        ```run-json
        {"name": "short-app-name", "workflow": "<workflow name>", "prompt": "<the full initial prompt the agents will receive — self-contained, specific, includes the user's requirements verbatim where possible>"}
        ```

        The user sees a Create button for your proposal — never claim a run \
        was started. Plain text otherwise; no other fenced blocks.
        """
        var convo = ""
        for (role, text) in history.suffix(12) {
            convo += "\n\(role): \(text)\n"
        }
        let prompt = system + "\n===== CONVERSATION =====" + convo + "\nASSISTANT:"

        return await withCheckedContinuation { cont in
            DispatchQueue.global(qos: .userInitiated).async {
                let p = Process()
                p.executableURL = URL(fileURLWithPath: bin)
                p.arguments = ["-p", prompt]
                var env = ProcessInfo.processInfo.environment
                for k in ["OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY",
                          "GOOGLE_API_KEY", "ANTHROPIC_AUTH_TOKEN",
                          "ANTHROPIC_BASE_URL", "OPENAI_BASE_URL",
                          "GOOGLE_APPLICATION_CREDENTIALS"] {
                    env.removeValue(forKey: k)
                }
                p.environment = env
                let out = Pipe()
                p.standardOutput = out
                p.standardError = Pipe()
                do { try p.run() } catch {
                    cont.resume(returning: nil)
                    return
                }
                // Watchdog: a hung CLI must not strand the chat forever.
                DispatchQueue.global().asyncAfter(deadline: .now() + 120) {
                    if p.isRunning { p.terminate() }
                }
                // Read to EOF BEFORE waitUntilExit — the safe order when the
                // reply could exceed the 64KB pipe buffer.
                let data = out.fileHandleForReading.readDataToEndOfFile()
                p.waitUntilExit()
                let s = String(data: data, encoding: .utf8)?
                    .trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
                cont.resume(returning: (p.terminationStatus == 0 && !s.isEmpty) ? s : nil)
            }
        }
    }

    // Short wall-clock TTL, not a stat()-based cache: measured, an
    // attributesOfItem(atPath:) call to check (mtime, size) costs MORE than
    // just re-reading+parsing these small (~1KB) warm-cache files outright —
    // the win here is avoiding *any* filesystem call on the hot path, not
    // avoiding the parse. Date()+dictionary lookup is a few nanoseconds, so
    // repeated calls within the window are effectively free. 2s comfortably
    // covers a single display-link frame burst while staying inside the
    // store's own 1.5s refresh cadence, so routing edits (which invalidate
    // the cache explicitly below) still show up promptly.
    private let modelRoutingTTL: TimeInterval = 2.0

    private func cachedModelRouting(at url: URL) -> ModelRouting {
        let now = Date()
        if let hit = modelRoutingCache[url], now.timeIntervalSince(hit.loadedAt) < modelRoutingTTL {
            return hit.routing
        }
        let routing = ModelRouting.load(from: url)
        modelRoutingCache[url] = (now, routing)
        return routing
    }

    // Routing-file mtime — powers the grid's "file changed on disk" banner.
    func routingFileMTime(at url: URL) -> Date? {
        (try? fm.attributesOfItem(atPath: url.path))?[.modificationDate] as? Date
    }

    // The intake sheet's forward-compatible preset marker (<app>/routing_preset.txt).
    func readRoutingPresetMarker(_ project: Project) -> String? {
        let url = project.dirURL.appendingPathComponent("routing_preset.txt")
        guard let s = try? String(contentsOf: url, encoding: .utf8) else { return nil }
        let v = s.trimmingCharacters(in: .whitespacesAndNewlines)
        return v.isEmpty ? nil : v
    }

    func clearRoutingPresetMarker(_ project: Project) {
        try? fm.removeItem(at: project.dirURL.appendingPathComponent("routing_preset.txt"))
    }

    // Switch which workflow a project runs (Plan tab top bar). Writes
    // workflow.txt — the same file the engine's resolve_workflow_for_app and
    // the loaders above read.
    func setWorkflow(project: Project, workflow name: String) {
        guard workflows.contains(where: { $0.name == name }) else { return }
        try? (name + "\n").write(to: project.dirURL.appendingPathComponent("workflow.txt"),
                                 atomically: true, encoding: .utf8)
        refresh()
    }

    // MARK: - Plan-tab cell selection → the Inspector's persistent cell editor
    // (DESIGN-NATIVE-PRO.md §5.3: single click selects; the Plan Summary tab
    // becomes the non-modal editor for the selected cell).

    struct PlanCellSelection: Equatable {
        var project: String    // "" = the fleet grid in Settings › Defaults
        var phaseKey: String
        var agent: String
    }
    @Published var planCellSelection: PlanCellSelection?

    // MARK: - Agent CLI versions ("Probe All", Models & Agents)

    @Published var cliVersions: [String: String] = [:]
    @Published var probeAllInFlight = false

    // Ping every agent CLI for its version string. Best-effort and async; a
    // missing binary just leaves no version (the install-status row already
    // covers "not found").
    func probeAgentVersions() {
        guard !probeAllInFlight else { return }
        probeAllInFlight = true
        let commands: [(agent: String, names: [String])] = [
            ("codex", ["codex"]), ("claude", ["claude"]),
            ("gemini", ["gemini", "agy"]), ("ollama", ["ollama"]),
        ]
        Task.detached(priority: .utility) { [weak self] in
            var out: [String: String] = [:]
            for (agent, names) in commands {
                for name in names {
                    if let v = Self.cliVersion(binary: name) {
                        out[agent] = v
                        break
                    }
                }
            }
            let versions = out
            await MainActor.run { [weak self] in
                guard let self else { return }
                self.probeAllInFlight = false
                if self.cliVersions != versions { self.cliVersions = versions }
            }
        }
    }

    // `<binary> --version`, first line, trimmed. nil when the binary is
    // missing or misbehaves. Never raises; 10s cap so a wedged CLI can't
    // stall the probe forever.
    private nonisolated static func cliVersion(binary: String) -> String? {
        let p = Process()
        p.executableURL = URL(fileURLWithPath: "/usr/bin/env")
        p.arguments = [binary, "--version"]
        let pipe = Pipe()
        p.standardOutput = pipe
        p.standardError = Pipe()
        do { try p.run() } catch { return nil }
        let deadline = Date().addingTimeInterval(10)
        while p.isRunning && Date() < deadline { usleep(50_000) }
        if p.isRunning { p.terminate(); return nil }
        guard p.terminationStatus == 0 else { return nil }
        let data = pipe.fileHandleForReading.readDataToEndOfFile()
        let line = String(decoding: data, as: UTF8.self)
            .split(separator: "\n").first.map(String.init) ?? ""
        let v = line.trimmingCharacters(in: .whitespacesAndNewlines)
        return v.isEmpty ? nil : String(v.prefix(60))
    }

    // PATH plus the common install locations a Finder-launched app (which
    // inherits a minimal PATH) would otherwise miss: Homebrew, /usr/local,
    // user-local, and the per-tool bins these CLIs ship into (npm global, codex,
    // bun, cargo). Shared by detectCLIs / ollamaOnPath so both look in one place.
    // nonisolated: called from the background refresh queue (detectCLIs) and
    // from tests; it touches no store state.
    nonisolated static func cliSearchDirs() -> [String] {
        let home = NSHomeDirectory()
        let extra = ["/opt/homebrew/bin", "/usr/local/bin", "/usr/bin",
                     "\(home)/.local/bin", "\(home)/.codex/bin",
                     "\(home)/.npm-global/bin", "\(home)/.bun/bin",
                     "\(home)/.cargo/bin"]
        let fromPath = (ProcessInfo.processInfo.environment["PATH"] ?? "")
            .split(separator: ":", omittingEmptySubsequences: true).map(String.init)
        // Drop empties (a leading/trailing/doubled ":" in PATH would otherwise
        // resolve to the process's own cwd) and de-duplicate while preserving
        // order, so detectCLIs()/ollamaOnPath() don't repeat the same stat
        // calls for a directory listed in both PATH and the extras above.
        var seen = Set<String>()
        var out: [String] = []
        for dir in fromPath + extra where !dir.isEmpty && !seen.contains(dir) {
            seen.insert(dir)
            out.append(dir)
        }
        return out
    }

    func ollamaOnPath() -> Bool {
        return OrchestratorStore.cliSearchDirs().contains {
            fm.isExecutableFile(atPath: ($0 as NSString).appendingPathComponent("ollama"))
        }
    }

    // Open Terminal running a fixed command (install/pull). No user free-text reaches
    // the shell — callers pass only curated model ids (V2 spec §12.2/§12.3) — but the
    // command is still escaped for the AppleScript string literal so a quote or
    // backslash in a future caller's argument can't break out of the `do script`.
    func runInTerminal(_ command: String) {
        let escaped = command
            .replacingOccurrences(of: "\\", with: "\\\\")
            .replacingOccurrences(of: "\"", with: "\\\"")
        let script = "tell application \"Terminal\"\nactivate\ndo script \"\(escaped)\"\nend tell"
        let p = Process()
        p.executableURL = URL(fileURLWithPath: "/usr/bin/osascript")
        p.arguments = ["-e", script]
        do {
            try p.run()
        } catch {
            surfaceError("Could not open Terminal for `\(command)`: "
                + error.localizedDescription)
        }
    }

    // MARK: - Global worker cap toggle (config.yaml)

    func readGlobalCapEnabled() -> Bool {
        guard let text = try? String(contentsOf: configURL, encoding: .utf8) else { return false }
        return firstMatch(in: text, pattern: "global_worker_cap_enabled:\\s*(true|false)") == "true"
    }

    func setGlobalCapEnabled(_ on: Bool) {
        guard var text = try? String(contentsOf: configURL, encoding: .utf8) else { return }
        let pattern = "(global_worker_cap_enabled:\\s*)(true|false)"
        if let re = try? NSRegularExpression(pattern: pattern) {
            let range = NSRange(text.startIndex..<text.endIndex, in: text)
            text = re.stringByReplacingMatches(in: text, range: range,
                                               withTemplate: "$1\(on ? "true" : "false")")
            writeConfig(text)
        }
    }

    func readRuntimeInt(_ key: String, defaultValue: Int) -> Int {
        guard let text = try? String(contentsOf: configURL, encoding: .utf8),
              let m = firstMatch(in: text, pattern: "(?m)^\\s*\(key):\\s*(-?\\d+)\\s*$") else {
            return defaultValue
        }
        return Int(m.trimmingCharacters(in: .whitespaces)) ?? defaultValue
    }

    func readRuntimeBool(_ key: String, defaultValue: Bool) -> Bool {
        guard let text = try? String(contentsOf: configURL, encoding: .utf8),
              let m = firstMatch(in: text, pattern: "(?m)^\\s*\(key):\\s*(true|false)\\s*$") else {
            return defaultValue
        }
        return m == "true"
    }

    func setRuntimeInt(_ key: String, _ value: Int) {
        guard var text = try? String(contentsOf: configURL, encoding: .utf8) else { return }
        let v = max(0, value)
        let pattern = "(?m)^(\\s*\(key):\\s*).*$"
        if let re = try? NSRegularExpression(pattern: pattern) {
            let range = NSRange(text.startIndex..<text.endIndex, in: text)
            if re.firstMatch(in: text, range: range) != nil {
                text = re.stringByReplacingMatches(in: text, range: range,
                                                   withTemplate: "$1\(v)")
            } else if let runtimeRange = text.range(of: "runtime:\\n") {
                text.insert(contentsOf: "  \(key): \(v)\n", at: runtimeRange.upperBound)
            }
            writeConfig(text)
        }
    }

    func setRuntimeBool(_ key: String, _ value: Bool) {
        guard var text = try? String(contentsOf: configURL, encoding: .utf8) else { return }
        let pattern = "(?m)^(\\s*\(key):\\s*).*$"
        if let re = try? NSRegularExpression(pattern: pattern) {
            let range = NSRange(text.startIndex..<text.endIndex, in: text)
            if re.firstMatch(in: text, range: range) != nil {
                text = re.stringByReplacingMatches(in: text, range: range,
                                                   withTemplate: "$1\(value ? "true" : "false")")
            } else if let runtimeRange = text.range(of: "runtime:\\n") {
                text.insert(contentsOf: "  \(key): \(value ? "true" : "false")\n",
                            at: runtimeRange.upperBound)
            }
            writeConfig(text)
        }
    }

    // MARK: - Usage (agent call counts from the run logs)

    var logsDirURL: URL { orchDirURL.appendingPathComponent("logs", isDirectory: true) }

    // Tally agent calls per project per agent from the per-call log filenames
    // (<ts>__<app>__<phase>__r<round>__<agent>.json) — cheap, no file reads.
    // Returns (rows sorted by total desc, perAgentTotals, grandTotal).
    func usageStats() -> (rows: [(project: String, byAgent: [String: Int], total: Int)],
                          agentTotals: [String: Int], grandTotal: Int) {
        var perProject: [String: [String: Int]] = [:]
        var agentTotals: [String: Int] = [:]
        var grand = 0
        if let files = try? fm.contentsOfDirectory(atPath: logsDirURL.path) {
            for f in files where f.hasSuffix(".json") {
                let parts = f.replacingOccurrences(of: ".json", with: "").components(separatedBy: "__")
                guard parts.count >= 5 else { continue }
                let project = parts[1]
                let agent = parts[parts.count - 1]
                perProject[project, default: [:]][agent, default: 0] += 1
                agentTotals[agent, default: 0] += 1
                grand += 1
            }
        }
        let rows = perProject.map { (project: $0.key, byAgent: $0.value,
                                     total: $0.value.values.reduce(0, +)) }
            .sorted { $0.total > $1.total }
        return (rows, agentTotals, grand)
    }

    // MARK: - Project actions (reset / fork / build history)

    // Move to the Trash so a destructive action is recoverable; hard-delete is
    // only the fallback (e.g. a volume with no Trash).
    private func removeRecoverably(_ url: URL) {
        guard fm.fileExists(atPath: url.path) else { return }
        do { try fm.trashItem(at: url, resultingItemURL: nil) }
        catch { try? fm.removeItem(at: url) }
    }

    // Reset a project to its inputs: moves all generated state to the TRASH (so
    // an accidental reset is recoverable) and the next run starts fresh. Keeps
    // initial_prompt, workflow.txt, and run_config.json.
    func resetProject(_ project: Project) {
        guard !project.running else {
            runLog += "Stop \(project.name) before resetting it.\n"
            return
        }
        let dir = project.dirURL
        if let items = try? fm.contentsOfDirectory(atPath: dir.path) {
            for name in items where name != "initial_prompt" && !name.hasPrefix(".") {
                var isDir: ObjCBool = false
                let p = dir.appendingPathComponent(name)
                if fm.fileExists(atPath: p.path, isDirectory: &isDir), isDir.boolValue {
                    removeRecoverably(p)   // phase folders, docs, review, app_build
                }
            }
        }
        for f in ["agent_state.json", "verify_results.json"] {
            removeRecoverably(dir.appendingPathComponent(f))
        }
        removeRecoverably(dir.appendingPathComponent(".orchestrator_runtime"))
        runLog += "Reset \(project.name) to its prompt (removed items are in the Trash).\n"
        refresh()
    }

    // Delete the whole project folder — to the Trash, never a hard rm.
    func deleteProject(_ project: Project) {
        removeProject(project, deleteFolder: true)
    }

    // Unified "Remove…" flow (any sidebar section): stop the run if we own or
    // can signal one, drop the project from both queues, then either archive
    // (marker file — folder untouched, engine scans skip it) or move the whole
    // folder to the Trash. A live run gets a grace period before the Trash
    // move so the SIGTERM→SIGKILL escalation in stopProject can finish.
    func removeProject(_ project: Project, deleteFolder: Bool) {
        let name = project.name
        let wasRunning = project.running || canStop(name) || appLocks[name] != nil
        if wasRunning {
            if runningProcesses[name] != nil { stopProject(name) } else { stopRun(name) }
        }
        removeFromQueue(name)
        removeFromQueueOrder(name)
        if deleteFolder {
            if wasRunning {
                runLog += "Stopping \(name), then moving it to the Trash…\n"
                Task { @MainActor [weak self] in
                    try? await Task.sleep(nanoseconds: 6_000_000_000)
                    self?.removeRecoverably(project.dirURL)
                    self?.refresh()
                }
            } else {
                removeRecoverably(project.dirURL)
                runLog += "Moved \(name) to the Trash.\n"
            }
        } else {
            let marker = project.dirURL.appendingPathComponent(".orch_archived")
            fm.createFile(atPath: marker.path, contents: Data())
            runLog += "Archived \(name) — hidden from the queue and engine scans; "
                + "the folder is untouched.\n"
        }
        refresh()
    }

    // Restore an archived project: delete the marker; it reappears in its
    // status-appropriate sidebar section on the next tick.
    func unarchiveProject(_ project: Project) {
        try? fm.removeItem(at: project.dirURL.appendingPathComponent(".orch_archived"))
        runLog += "Restored \(project.name) from the archive.\n"
        refresh()
    }

    // Staged continuation: re-open a (typically done) project under a DIFFERENT
    // workflow. The engine's --continue-with does the state surgery: rewrites
    // workflow.txt, re-arms the new workflow's phases, and carries prior phase
    // outputs forward as context (research now → full build later).
    func continueProject(_ name: String, workflow: String) {
        requestNotificationAuthIfNeeded()
        runLog = "Continuing \(name) with the \(workflow) workflow…\n"
        launch(args: ["orchestrator.py", "--root", rootURL.path,
                      "--app", name, "--continue-with", workflow],
               project: name)
    }

    // The generated Xcode project/workspace under <project>/app_build, if any —
    // powers the "Open in Xcode" affordance for finished iOS builds.
    func xcodeProjectURL(for project: Project) -> URL? {
        XcodeProjectLocator.find(under: project.dirURL.appendingPathComponent("app_build"))
    }

    // Fork = an independent copy (including the app_build git history) under a new
    // slug, so you can explore an alternate direction without touching the original.
    @discardableResult
    func forkProject(_ project: Project) -> String? {
        let base = project.name + "-fork"
        var candidate = base; var n = 2
        while fm.fileExists(atPath: rootURL.appendingPathComponent(candidate).path) {
            candidate = "\(base)-\(n)"; n += 1
        }
        do {
            try fm.copyItem(at: project.dirURL, to: rootURL.appendingPathComponent(candidate))
        } catch {
            runLog += "Fork failed: \(error.localizedDescription)\n"; return nil
        }
        runLog += "Forked \(project.name) -> \(candidate).\n"
        refresh()
        return candidate
    }

    // Iterate on an existing app: append the requested change to the prompt, switch
    // to the short "iterate" workflow, and run. app_build persists across prompt
    // changes, so the build phase extends the existing app instead of rebuilding.
    func iterateProject(_ project: Project, feature: String) {
        let f = feature.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !f.isEmpty else { return }
        let promptURL = project.dirURL
            .appendingPathComponent("initial_prompt").appendingPathComponent("initial_prompt.md")
        var text = (try? String(contentsOf: promptURL, encoding: .utf8)) ?? ""
        text += "\n\n## Change requested\n\(f)\n"
        try? text.write(to: promptURL, atomically: true, encoding: .utf8)
        try? "iterate\n".write(to: project.dirURL.appendingPathComponent("workflow.txt"),
                               atomically: true, encoding: .utf8)
        runLog += "Iterating on \(project.name): \(f)\n"
        runOrQueue(project.name)
    }

    // ---- app_build version history + per-phase rollback / diff (git) --------
    // All nonisolated static + synchronous subprocess: callers hop off the main
    // actor (Task.detached) so a slow git call can never beachball the UI. No
    // shell — args go straight to `git` via /usr/bin/env, so a crafted subject
    // or path can never be interpreted as a command. Mirrors the old
    // buildHistory() pattern (now replaced by structuredBuildHistory).

    nonisolated private static func runGit(_ args: [String], in build: URL,
                                           timeout: TimeInterval = 60)
        -> (code: Int32, out: String, err: String) {
        let p = Process()
        p.executableURL = URL(fileURLWithPath: "/usr/bin/env")
        p.arguments = ["git", "-C", build.path] + args
        let outPipe = Pipe(); let errPipe = Pipe()
        p.standardOutput = outPipe; p.standardError = errPipe
        do { try p.run() } catch { return (127, "", "\(error)") }
        // Drain before wait so a big diff can't deadlock on a full pipe buffer.
        let outData = outPipe.fileHandleForReading.readDataToEndOfFile()
        let errData = errPipe.fileHandleForReading.readDataToEndOfFile()
        p.waitUntilExit()
        return (p.terminationStatus,
                String(data: outData, encoding: .utf8) ?? "",
                String(data: errData, encoding: .utf8) ?? "")
    }

    nonisolated private static func isRepo(_ build: URL) -> Bool {
        FileManager.default.fileExists(atPath: build.appendingPathComponent(".git").path)
    }

    // "orchestrator: <phase> iteration <n>" -> "<phase>"; nil for the init
    // commit and "rolled back to …" commits (they aren't a phase build).
    nonisolated static func phase(fromSubject subject: String) -> String? {
        guard let m = subject.range(
            of: #"^orchestrator: (.+) iteration [0-9]+$"#, options: .regularExpression)
        else { return nil }
        // Extract capture group 1 by re-matching (Swift's range API has no group
        // accessor); strip the fixed prefix/suffix off the matched span.
        let matched = String(subject[m])
        let body = matched.dropFirst("orchestrator: ".count)
        guard let iterRange = body.range(of: #" iteration [0-9]+$"#,
                                         options: .regularExpression) else { return nil }
        return String(body[body.startIndex..<iterRange.lowerBound])
    }

    // Only the orchestrator's own commits are coherent, rollback-safe snapshots:
    // "orchestrator: build repo initialized", "orchestrator: <phase> iteration
    // <n>", and "orchestrator: rolled back to <sha>". A worktree-isolation lane
    // commit ("lane <slug>") or a "Merge branch lane-…" commit is an
    // intermediate/partial tree that must never be a rollback or diff target.
    nonisolated static let buildCommitSubjectPrefix = "orchestrator: "

    // Structured build history. --first-parent skips the merge side of lane
    // merges; the subject-prefix filter is the real guarantee, because the
    // FIRST lane merge fast-forwards (the engine merges with plain
    // `git merge --no-edit`, no --no-ff), putting that lane's "lane <slug>"
    // commit directly on the first-parent mainline — --first-parent alone
    // would surface it as a (partial-tree, unsafe) rollback target. Fields are
    // split on unit (0x1f) / record (0x1e) separators so arbitrary subject text
    // (incl. double spaces) parses safely. We over-fetch (×4) before filtering
    // so `limit` counts orchestrator commits, not raw log entries.
    nonisolated static func structuredBuildHistory(buildDir build: URL,
                                                   limit: Int = 40) -> [BuildCommit] {
        guard isRepo(build) else { return [] }
        let fmt = "%H%x1f%h%x1f%ad%x1f%s%x1f%D%x1e"
        let fetch = max(1, limit) * 4
        let r = runGit(["log", "--first-parent",
                        "--pretty=format:\(fmt)", "--date=short", "-\(fetch)"],
                       in: build)
        guard r.code == 0 else { return [] }
        var out: [BuildCommit] = []
        for record in r.out.components(separatedBy: "\u{1e}") {
            let rec = record.trimmingCharacters(in: .whitespacesAndNewlines)
            if rec.isEmpty { continue }
            let f = rec.components(separatedBy: "\u{1f}")
            guard f.count >= 5, f[3].hasPrefix(buildCommitSubjectPrefix) else { continue }
            out.append(BuildCommit(sha: f[0], shortSha: f[1], date: f[2],
                                   subject: f[3], refs: f[4],
                                   phase: phase(fromSubject: f[3])))
            if out.count >= limit { break }
        }
        return out
    }

    // True if <sha> resolves to a commit in this repo (accepts short or full).
    nonisolated static func shaExists(buildDir build: URL, _ sha: String) -> Bool {
        guard isRepo(build), !sha.isEmpty else { return false }
        return runGit(["cat-file", "-e", "\(sha)^{commit}"], in: build).code == 0
    }

    // SAFE, fully-reversible rollback: materialize <sha>'s tree as a NEW forward
    // commit rather than `git reset --hard` (which discards history and isn't
    // undoable). Recipe: verify clean tree -> read-tree <sha> -> checkout-index
    // -a -f -> clean -fdq (no -x: ignored build artifacts/secrets survive) ->
    // commit. Guards a dirty/untracked tree (the ONLY thing that makes this
    // reversible — `git clean` would irrecoverably delete untracked non-ignored
    // files) and a non-existent sha. The caller (rollbackProject) additionally
    // refuses when a build is live.
    nonisolated static func rollbackBuild(buildDir build: URL, toSha sha: String) -> RollbackResult {
        guard isRepo(build) else { return .notARepo }
        guard shaExists(buildDir: build, sha) else { return .shaNotFound }
        // Dirty check INCLUDING untracked (no -uno): an untracked non-ignored
        // file has no git backing, so `git clean` below would destroy it with no
        // way to restore — refuse rather than lose it.
        let porcelain = runGit(["status", "--porcelain"], in: build)
        guard porcelain.code == 0 else { return .gitFailed(porcelain.err) }
        guard porcelain.out.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
        else { return .dirtyWorkingTree }

        // Point the index at the target tree; if it equals HEAD's tree there's
        // nothing to commit — report noChange instead of failing on an empty commit.
        let readTree = runGit(["read-tree", sha], in: build)
        guard readTree.code == 0 else { return .gitFailed(readTree.err) }
        if runGit(["diff", "--cached", "--quiet"], in: build).code == 0 {
            // Restore the index to HEAD (read-tree left it pointed at <sha>).
            _ = runGit(["reset", "-q", "HEAD"], in: build)
            return .noChange
        }
        // Materialize the target tree into the working copy, then remove any
        // now-untracked files that existed after <sha> (all git-backed, so
        // reversible). -x is intentionally OMITTED so ignored artifacts survive.
        let co = runGit(["checkout-index", "-a", "-f"], in: build)
        guard co.code == 0 else { return .gitFailed(co.err) }
        _ = runGit(["clean", "-fdq"], in: build)
        // Commit with an explicit identity so a repo lacking git config (created
        // outside ensure_build_repo, or with cleared local + no global config)
        // still succeeds instead of failing "tell me who you are".
        let shortTarget = String(sha.prefix(7))
        let commit = runGit(["-c", "user.name=Orchestrator",
                             "-c", "user.email=orchestrator@local",
                             "commit", "-q", "-m",
                             "orchestrator: rolled back to \(shortTarget)"], in: build)
        guard commit.code == 0 else { return .gitFailed(commit.err) }
        // Post-assert: worktree == index == new tree, so the next
        // commit_build_state can't re-commit a phantom diff.
        let after = runGit(["status", "--porcelain"], in: build)
        if !after.out.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
            return .gitFailed("worktree not clean after rollback")
        }
        let head = runGit(["rev-parse", "--short", "HEAD"], in: build)
        return .success(newShortSha: head.out.trimmingCharacters(in: .whitespacesAndNewlines))
    }

    // Unified diff between two commits, grouped per file, lines pre-classified
    // for +/- styling. `--` terminates options so a value can never be
    // misparsed as a flag/pathspec.
    nonisolated static func buildDiff(buildDir build: URL, from a: String, to b: String,
                                      maxLinesPerFile: Int = 600) -> [FileDiff] {
        guard isRepo(build) else { return [] }
        let r = runGit(["diff", a, b, "--"], in: build)
        guard r.code == 0 else { return [] }
        var files: [FileDiff] = []
        var path = ""
        var lines: [DiffLine] = []
        func flush() {
            if !path.isEmpty { files.append(FileDiff(path: path, lines: lines)) }
            lines = []
        }
        for raw in r.out.split(separator: "\n", omittingEmptySubsequences: false).map(String.init) {
            if raw.hasPrefix("diff --git ") {
                flush()
                // "diff --git a/<path> b/<path>" — take the b-side path.
                if let bRange = raw.range(of: " b/") {
                    path = String(raw[bRange.upperBound...])
                } else {
                    path = raw
                }
                lines = [DiffLine(kind: .fileHeader, text: path)]
            } else if path.isEmpty {
                continue    // preamble before the first file header
            } else if lines.count - 1 >= maxLinesPerFile {
                if lines.last?.kind != .meta || lines.last?.text != "… (diff truncated)" {
                    lines.append(DiffLine(kind: .meta, text: "… (diff truncated)"))
                }
            } else if raw.hasPrefix("@@") {
                lines.append(DiffLine(kind: .hunk, text: raw))
            } else if raw.hasPrefix("+++") || raw.hasPrefix("---")
                        || raw.hasPrefix("index ") || raw.hasPrefix("new file")
                        || raw.hasPrefix("deleted file") || raw.hasPrefix("rename ")
                        || raw.hasPrefix("similarity ") || raw.hasPrefix("Binary files") {
                lines.append(DiffLine(kind: .meta, text: raw))
            } else if raw.hasPrefix("+") {
                lines.append(DiffLine(kind: .add, text: raw))
            } else if raw.hasPrefix("-") {
                lines.append(DiffLine(kind: .remove, text: raw))
            } else {
                lines.append(DiffLine(kind: .context, text: raw))
            }
        }
        flush()
        return files
    }

    // Rollback is destructive-if-racing: refuse whenever ANY live-run signal is
    // set (the same triple AppShellView uses for isLive). project.running is a
    // 240s-mtime heuristic, not authoritative — appLocks/canStop are the real
    // engine-owns-the-repo signals, and workers write into app_build directly
    // (not through git), so there's no index lock to rely on.
    func canRollback(_ project: Project) -> Bool {
        // A stale (dead-pid) lock is a corpse, not a live engine — without the
        // subtraction a crashed run would pin rollback forever.
        !(project.running || canStop(project.name)
          || (appLocks[project.name] != nil && !staleLocks.contains(project.name)))
    }

    // Roll the app_build repo back to a chosen historical commit. Guards the
    // running state, then runs the git recipe off the main actor.
    func rollbackProject(_ project: Project, toSha sha: String) {
        guard canRollback(project) else {
            runLog += "Can't roll back \(project.name) while it's running — stop it first.\n"
            return
        }
        let build = project.dirURL.appendingPathComponent("app_build")
        let name = project.name
        runLog += "Rolling back \(name) to \(String(sha.prefix(7)))…\n"
        Task { @MainActor [weak self] in
            let result = await Task.detached {
                OrchestratorStore.rollbackBuild(buildDir: build, toSha: sha)
            }.value
            guard let self else { return }
            switch result {
            case .success(let newShort):
                self.runLog += "Rolled back \(name) — new build commit \(newShort) "
                    + "(history preserved; this is undoable by rolling back again).\n"
            case .noChange:
                self.runLog += "\(name) is already at that build — nothing to roll back.\n"
            case .dirtyWorkingTree:
                self.runLog += "\(name) has uncommitted changes in app_build — "
                    + "refusing to roll back so nothing is lost.\n"
            case .shaNotFound:
                self.runLog += "That commit isn't in \(name)'s build history anymore.\n"
            case .notARepo:
                self.runLog += "\(name) has no build history to roll back to.\n"
            case .running:
                self.runLog += "\(name) is running — stop it before rolling back.\n"
            case .gitFailed(let msg):
                self.surfaceError("Rollback of \(name) failed: \(msg)")
            }
            self.refresh()
        }
    }

    // Write <project>/target_path.txt (one repo path per line) for audit /
    // library_mining workflows.
    func writeTargetPaths(project name: String, paths: String) {
        let lines = paths.split(separator: "\n")
            .map { $0.trimmingCharacters(in: .whitespaces) }.filter { !$0.isEmpty }
        guard !lines.isEmpty else { return }
        let url = rootURL.appendingPathComponent(name).appendingPathComponent("target_path.txt")
        do {
            try (lines.joined(separator: "\n") + "\n").write(to: url, atomically: true, encoding: .utf8)
        } catch {
            surfaceError("Could not save target paths for \(name): \(error.localizedDescription)")
        }
    }

    // MARK: - Per-project run config (autonomy / completeness / stop target)

    // Writes <project>/run_config.json, which the engine reads (completeness picks
    // the phase subset; stop_after_phase truncates; autonomy sets the mode). Only
    // non-default values are written; an empty config file is skipped entirely.
    func writeRunConfig(project name: String, autonomy: String,
                        completeness: String, stopAfter: String) {
        var cfg: [String: Any] = [:]
        if !autonomy.isEmpty && autonomy != "fully_autonomous" { cfg["autonomy"] = autonomy }
        if !completeness.isEmpty { cfg["completeness"] = completeness }
        if !stopAfter.isEmpty { cfg["stop_after_phase"] = stopAfter }
        guard !cfg.isEmpty else { return }
        let url = rootURL.appendingPathComponent(name).appendingPathComponent("run_config.json")
        writeJSON(cfg, to: url)
    }

    // MARK: - Model selection (which GPT / Claude / Gemini model each provider uses)

    // Curated quick-pick presets per provider (users can also type any value).
    private func builtInModelPresets(_ agent: String) -> [String] {
        switch agent {
        case "codex": return ["gpt-5.4-mini", "gpt-5.3-codex-spark", "gpt-5.4", "o4-mini"]
        case "claude":
            return [
                "sonnet", "opus", "haiku",
                "claude-sonnet-5", "claude-opus-4-8", "claude-fable-5",
                "claude-haiku-4-5", "claude-sonnet-4-6", "claude-opus-4-7"
            ]
        case "gemini": return ["gemini-3.1-flash-lite-preview", "gemini-3.5-flash", "gemini-2.5-pro"]
        // Local models come from the engine's registry (via --doctor --json),
        // falling back to the shipped curated list before the first fetch.
        case "ollama":
            let fromDoctor = (localModels?.registry ?? []).map(\.id)
            return fromDoctor.isEmpty ? recommendedLocalModels.map(\.id) : fromDoctor
        default: return []
        }
    }

    func modelPresets(_ agent: String) -> [String] {
        var seen = Set<String>()
        var out: [String] = []
        for value in builtInModelPresets(agent)
            + (customModelPresets[agent] ?? [])
            + [agentModels[agent] ?? ""] {
            let v = value.trimmingCharacters(in: .whitespacesAndNewlines)
            if !v.isEmpty && !seen.contains(v) {
                seen.insert(v)
                out.append(v)
            }
        }
        return out
    }

    func defaultModel(_ agent: String) -> String {
        modelPresets(agent).first ?? ""
    }

    func modelIDIsSafe(_ id: String) -> Bool {
        localModelIdIsSafe(id)
    }

    @discardableResult
    func addModelPreset(agent: String, id rawID: String) -> Bool {
        guard agentOrder.contains(agent) else { return false }
        let id = rawID.trimmingCharacters(in: .whitespacesAndNewlines)
        guard modelIDIsSafe(id) else { return false }
        var presets = customModelPresets
        var list = presets[agent] ?? []
        if !list.contains(id) { list.append(id) }
        presets[agent] = list
        // Don't report success (and don't publish the new preset) unless the
        // file actually saved — writeJSON surfaces the error banner on failure.
        guard writeJSON(["schema_version": 1, "models": presets], to: modelPresetsURL)
        else { return false }
        customModelPresets = presets
        setModel(agent, id)
        return true
    }

    // Reasoning-effort config key per provider. Only Codex exposes an effort knob
    // that the engine actually passes to the CLI (model_reasoning_effort); the
    // others return nil so the picker isn't shown where it would do nothing.
    func effortKey(_ agent: String) -> String? {
        BackgroundConfigLoader.effortKey(agent)
    }

    let effortOptions = ["low", "medium", "high"]

    func setReasoningEffort(_ agent: String, _ value: String) {
        guard let key = effortKey(agent),
              var text = try? String(contentsOf: configURL, encoding: .utf8) else { return }
        let pattern = "(?m)^(\\s*\(key):\\s*).*$"
        if let re = try? NSRegularExpression(pattern: pattern) {
            let range = NSRange(text.startIndex..<text.endIndex, in: text)
            if re.firstMatch(in: text, range: range) != nil {
                let escaped = NSRegularExpression.escapedTemplate(for: "\"\(value)\"")
                text = re.stringByReplacingMatches(in: text, range: range, withTemplate: "$1\(escaped)")
            } else if let modelsRange = text.range(of: "models:\n") {
                // Older engine copies predate the key (e.g. claude_reasoning) —
                // insert it under models: so the edit still lands.
                text.insert(contentsOf: "  \(key): \"\(value)\"\n", at: modelsRange.upperBound)
            }
            if writeConfig(text) {
                agentEfforts[agent] = value
            }
        }
    }

    func setModel(_ agent: String, _ value: String) {
        let v = value.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !v.isEmpty, var text = try? String(contentsOf: configURL, encoding: .utf8) else { return }
        let key = modelKey(agent)
        let pattern = "(?m)^(\\s*\(key):\\s*).*$"
        if let re = try? NSRegularExpression(pattern: pattern) {
            let range = NSRange(text.startIndex..<text.endIndex, in: text)
            let escaped = NSRegularExpression.escapedTemplate(for: "\"\(v)\"")
            text = re.stringByReplacingMatches(in: text, range: range, withTemplate: "$1\(escaped)")
            if writeConfig(text) {
                agentModels[agent] = v
            }
        }
    }

    private func discoverApps() -> [String] {
        SessionLayout.discoverApps(rootURL: rootURL)
    }

    // V3 3.8: rail discovery — a cheap dir scan of the ENGINE sections/;
    // called on each refresh apply so seeding/edits surface within a tick.
    func reloadSectionRail() {
        sectionRail = SectionRailLogic.discover(
            sectionsDirURL: orchDirURL.appendingPathComponent(
                "sections", isDirectory: true))
    }

    /// Run --lint-section --lint-json off-main and publish the summary;
    /// a failed run publishes nil (unavailable, not clean — R2).
    func lintSection(_ name: String) {
        let py = resolvePython()
        let engine = orchDirURL.appendingPathComponent("orchestrator.py").path
        let root = rootURL.path
        Task.detached { [weak self] in
            let proc = Process()
            proc.executableURL = URL(fileURLWithPath: py)
            proc.arguments = [engine, "--root", root,
                              "--lint-section", name, "--lint-json"]
            let out = Pipe()
            proc.standardOutput = out
            proc.standardError = Pipe()
            try? proc.run()
            proc.waitUntilExit()
            let data = out.fileHandleForReading.readDataToEndOfFile()
            let summary = SectionLintParser.parse(data)
            await MainActor.run { [weak self] in
                self?.sectionLint[name] = summary
            }
        }
    }

    /// The empty state's "seed defaults" action: any engine invocation
    /// seeds sections (ensure_seeded_sections rides main); --seed is the
    /// cheapest honest one. Rail reloads on the refresh that follows.
    func seedDefaultSections() {
        let py = resolvePython()
        let engine = orchDirURL.appendingPathComponent("orchestrator.py").path
        let root = rootURL.path
        Task.detached { [weak self] in
            let proc = Process()
            proc.executableURL = URL(fileURLWithPath: py)
            proc.arguments = [engine, "--root", root, "--seed"]
            proc.standardOutput = Pipe()
            proc.standardError = Pipe()
            try? proc.run()
            proc.waitUntilExit()
            await MainActor.run { [weak self] in
                self?.reloadSectionRail()
                self?.refresh()
            }
        }
    }

    private func loadProject(_ name: String) -> Project? {
        let dir = rootURL.appendingPathComponent(name, isDirectory: true)
        let stateURL = dir.appendingPathComponent("agent_state.json")
        var status: ProjectStatus = .new
        var currentPhase: String? = nil
        var currentRound = 0
        var nextAgent: String? = nil
        var error: String? = nil
        var lastProcessed: String? = nil
        var completed: [String] = []
        var outputs: [String: String] = [:]
        var workflowName: String? = nil
        var awaiting: String? = nil
        var blocked: BlockedConflict? = nil
        var resolutions: [String: String] = [:]

        if let data = try? Data(contentsOf: stateURL),
           let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
            currentPhase = obj["current_phase"] as? String
            currentRound = (obj["current_round"] as? Int) ?? 0
            nextAgent = obj["next_agent"] as? String
            error = obj["error"] as? String
            lastProcessed = obj["last_processed"] as? String
            completed = (obj["completed_phases"] as? [String]) ?? []
            outputs = (obj["phase_outputs"] as? [String: String]) ?? [:]
            workflowName = obj["workflow"] as? String
            awaiting = obj["awaiting_approval"] as? String
            blocked = BlockedConflict.parse(fromStateObject: obj)
            resolutions = (obj["phase_resolutions"] as? [String: String]) ?? [:]
            let done = (obj["done"] as? Bool) ?? false
            if let e = error, !e.isEmpty {
                status = .aborted
            } else if done {
                status = .done
            } else {
                status = .inProgress
            }
        }

        // Resolve the workflow the same way the engine does (workflows.py:
        // resolve_workflow_for_app): recorded state, else workflow.txt, else a
        // `workflow:` line in the prompt frontmatter, else app_build.
        let resolvedName = workflowName ?? readWorkflowFile(dir)
            ?? readWorkflowFrontmatter(dir) ?? "app_build"
        let wf = workflow(named: resolvedName) ?? workflow(named: "app_build")
        let wfPhases = wf?.phases ?? ALL_PHASES
        var titles: [String: String] = [:]
        for p in wfPhases { titles[p.key] = p.title }

        let stateMTime = (try? fm.attributesOfItem(atPath: stateURL.path))?[.modificationDate] as? Date
        var running = false
        if status == .inProgress, let m = stateMTime, Date().timeIntervalSince(m) < 240 {
            running = true
        }
        // A manual Stop overrides the mtime heuristic — the state file stays
        // "fresh" for minutes after the process is dead. A state write clearly
        // NEWER than the stop means a new run started, so the override is
        // dropped; writes within 10s are the engine's own SIGTERM shutdown.
        var stopped = false
        if let stopAt = manualStops[name] {
            if let m = stateMTime, m.timeIntervalSince(stopAt) > 10 {
                manualStops[name] = nil
            } else if !(runningProcesses[name]?.isRunning ?? false) {
                running = false
                stopped = status == .inProgress
            }
        }
        var proj = Project(name: name, status: status, currentPhase: currentPhase,
                       currentRound: currentRound, nextAgent: nextAgent, error: error,
                       lastProcessed: lastProcessed, completedPhases: completed,
                       phaseOutputs: outputs, dirURL: dir, running: running,
                       workflow: resolvedName,
                       workflowTitle: wf?.title ?? "Build an App",
                       workflowKind: wf?.kindLabel ?? "Build",
                       phaseCount: wfPhases.count, phaseTitles: titles)
        proj.awaitingApproval = (awaiting?.isEmpty == false) ? awaiting : nil
        proj.blockedConflict = blocked
        proj.manuallyStopped = stopped
        proj.archived = fm.fileExists(
            atPath: dir.appendingPathComponent(".orch_archived").path)
        proj.phaseResolutions = resolutions
        // Latest verification outcome (defensive parse; [] on any problem).
        let verifyRecords = VerifyResultsParser.parse(
            fileAt: dir.appendingPathComponent("verify_results.json"))
        proj.latestVerify = VerifyResultsParser.latest(verifyRecords)
        proj.verifyRepairCount = VerifyResultsParser.repairAttemptCount(verifyRecords)
        return proj
    }

    // Approve a semi-autonomous/manual checkpoint: drops <project>/approvals/<phase>.ok,
    // which the paused engine is polling for.
    func approve(_ project: Project, phase: String) {
        submitApproval(project, phase: phase, decision: .approve, body: "ok")
    }

    // Full spec §3.1 checkpoint flow. The decision file the engine polls for:
    //   .ok       Approve — continue as-is (body ignored)
    //   .edit     Edit & Approve — body REPLACES the phase output, then continue
    //   .changes  Request Changes — body is human feedback; the phase re-runs
    func submitApproval(_ project: Project, phase: String,
                        decision: ApprovalDecision, body: String) {
        do {
            try ApprovalFiles.write(projectDir: project.dirURL, phase: phase,
                                    decision: decision, body: body)
        } catch {
            runLog += "Couldn't write approval decision: \(error.localizedDescription)\n"
            return
        }
        switch decision {
        case .approve:
            runLog += "Approved checkpoint after \(phase).\n"
        case .editAndApprove:
            runLog += "Approved checkpoint after \(phase) with an edited output.\n"
        case .requestChanges:
            runLog += "Requested changes to \(phase); the engine will re-run it.\n"
        }
        refresh()
    }

    private func readWorkflowFile(_ dir: URL) -> String? {
        let url = dir.appendingPathComponent("workflow.txt")
        guard let s = try? String(contentsOf: url, encoding: .utf8) else { return nil }
        let name = s.trimmingCharacters(in: .whitespacesAndNewlines)
            .components(separatedBy: "\n").first?.trimmingCharacters(in: .whitespaces)
        return (name?.isEmpty == false) ? name : nil
    }

    // A `workflow: <name>` line in the first ~15 lines of initial_prompt.md,
    // mirroring the engine's frontmatter resolution so the GUI never disagrees
    // with the backend about which workflow an app runs.
    private func readWorkflowFrontmatter(_ dir: URL) -> String? {
        let url = dir.appendingPathComponent("initial_prompt/initial_prompt.md")
        guard let text = try? String(contentsOf: url, encoding: .utf8) else { return nil }
        for line in text.components(separatedBy: "\n").prefix(15) {
            let s = line.trimmingCharacters(in: CharacterSet(charactersIn: "# ").union(.whitespaces))
            if s.lowercased().hasPrefix("workflow:") {
                let name = s.dropFirst("workflow:".count).trimmingCharacters(in: .whitespaces)
                if !name.isEmpty { return name }
            }
        }
        return nil
    }

    // Parsed transcripts cached by file path + mtime: a live build appends to a
    // multi-MB phase .md every few seconds, so an unchanged mtime must cost
    // nothing. The stat + read + Markdown parse all run in a detached task —
    // never on the main actor, where a busy disk beachballed the UI — and only
    // the cache lookup/update happens here. Content one tick late is fine.
    private var transcriptCache: [String: (mtime: Date, value: PhaseTranscript)] = [:]
    private nonisolated static let transcriptReadLimitBytes = 1_500_000
    private nonisolated static let transcriptHeadReadBytes = 256_000

    func transcript(for project: Project, phaseKey: String) async -> PhaseTranscript {
        guard let def = phases(for: project).first(where: { $0.key == phaseKey })
                ?? ALL_PHASES.first(where: { $0.key == phaseKey }) else {
            return PhaseTranscript()
        }
        let url = project.dirURL.appendingPathComponent(def.folder).appendingPathComponent(def.file)
        let cachedMtime = transcriptCache[url.path]?.mtime
        let (mtime, fresh) = await Task.detached(priority: .utility) {
            Self.readAndParseTranscript(at: url, ifChangedSince: cachedMtime)
        }.value
        if let fresh { transcriptCache[url.path] = (mtime, fresh) }
        return transcriptCache[url.path]?.value ?? PhaseTranscript()
    }

    // fresh == nil means the file hasn't changed since `ifChangedSince` (keep
    // the cached parse). Runs off the main actor — see transcript(for:phaseKey:).
    private nonisolated static func readAndParseTranscript(
        at url: URL, ifChangedSince cachedMtime: Date?
    ) -> (mtime: Date, fresh: PhaseTranscript?) {
        let mtime = ((try? FileManager.default.attributesOfItem(atPath: url.path))?[.modificationDate] as? Date)
            ?? .distantPast
        if let cachedMtime, cachedMtime == mtime { return (mtime, nil) }
        guard let text = readTranscriptText(at: url) else {
            var empty = PhaseTranscript()
            empty.exists = false
            return (mtime, empty)
        }
        return (mtime, TranscriptParser.parse(text))
    }

    private nonisolated static func readTranscriptText(at url: URL) -> String? {
        let fm = FileManager.default
        guard let attrs = try? fm.attributesOfItem(atPath: url.path),
              let sizeNumber = attrs[.size] as? NSNumber else {
            return try? String(contentsOf: url, encoding: .utf8)
        }
        let size = sizeNumber.intValue
        guard size > transcriptReadLimitBytes else {
            return try? String(contentsOf: url, encoding: .utf8)
        }
        guard let handle = try? FileHandle(forReadingFrom: url) else {
            return try? String(contentsOf: url, encoding: .utf8)
        }
        defer { try? handle.close() }

        let headBytes = min(transcriptHeadReadBytes, size)
        let tailBytes = max(transcriptReadLimitBytes - headBytes, 0)
        let headData = handle.readData(ofLength: headBytes)
        let tailOffset = UInt64(max(size - tailBytes, 0))
        try? handle.seek(toOffset: tailOffset)
        let tailData = handle.readDataToEndOfFile()

        let head = String(decoding: headData, as: UTF8.self)
        var tail = String(decoding: tailData, as: UTF8.self)
        if let newline = tail.firstIndex(of: "\n") {
            tail = String(tail[tail.index(after: newline)...])
        }
        return head
            + "\n\n## Transcript Notice\nThis transcript is very large, so the app is showing the beginning and the latest portion.\n\n"
            + tail
    }

    func initialPrompt(for project: Project) -> String {
        let url = project.dirURL.appendingPathComponent("initial_prompt/initial_prompt.md")
        return (try? String(contentsOf: url, encoding: .utf8)) ?? ""
    }

    // MARK: - Parallel build roster (live "who's building right now" proof)

    // Cheap O(1) lookup from a cache populated once per refresh() tick (see
    // above) — safe to call from a SwiftUI body as often as it re-evaluates.
    // Returns nil for every phase that isn't a real parallel-build fan-out, so
    // callers fall back to the normal single-agent ThinkingRow.
    func parallelBuildWorkers(for project: Project) -> [BuildWorker]? {
        buildWorkerStatus[project.name]
    }

    // The actual computation — called ONLY from refresh(), never from a view.
    // During a parallel build fan-out, next_agent is a "+"-joined slug list (e.g.
    // "codex-a+codex-b+codex-c") — orchestrator.py:1049. `done` is derived from
    // the per-call JSON logs so this is a real, verifiable signal, not a guess —
    // the same evidence a `ps`/log check would show, just rendered as chips.
    private func computeParallelBuildWorkers(for project: Project) -> [BuildWorker]? {
        guard let na = project.nextAgent, let phase = project.currentPhase else { return nil }
        let slugs = na.split(separator: "+").map(String.init)
        guard slugs.count > 1 else { return nil }
        let finished = finishedSlugs(app: project.name, phase: phase, round: project.currentRound)
        return slugs.map { slug in
            let (base, tag) = splitWorkerSlug(slug)
            let display = (Speaker(rawValue: base) ?? .system).display
            let label = tag.map { "\(display) \($0.uppercased())" } ?? display
            return BuildWorker(slug: slug, label: label, baseAgent: base,
                               done: finished.contains(slug))
        }
    }

    // "codex" -> ("codex", nil); "codex-a" -> ("codex", "a") — mirrors the
    // single-letter tag build_worker_roster() appends when one CLI is replicated
    // into several concurrent workers.
    private func splitWorkerSlug(_ slug: String) -> (base: String, tag: String?) {
        guard let dash = slug.lastIndex(of: "-") else { return (slug, nil) }
        let tail = slug[slug.index(after: dash)...]
        guard tail.count == 1, let ch = tail.first, ch.isLetter else { return (slug, nil) }
        return (String(slug[..<dash]), String(ch))
    }

    // write_call_log's filename is "<ts>__<app>__<phase>__r<round>.<slug>__<agent>.json"
    // where <ts> is ALWAYS exactly 22 chars ("%Y%m%d_%H%M%S_%f" — Python's %f is
    // always zero-padded to 6 digits). A file matching this app/phase/round
    // existing means that worker's call has already completed.
    //
    // The "__" separator isn't escaped, so an app/phase value containing a
    // leading/trailing underscore or an internal "__" could in principle make a
    // DIFFERENT (app, phase) pair produce a byte-identical prefix (e.g.
    // app="sample_app", phase="build" and app="sample_app_build", phase=""
    // collide if either half ends/starts with "_"). Slugified app names never
    // contain underscores at all, and built-in workflow phase keys never start
    // or end with one, so this is unreachable in practice — but for a
    // hand-created folder name it's theoretically possible, so refuse to trust
    // the match at all in that case (skip -> stays "still building") rather than
    // risk a false "done".
    private func finishedSlugs(app: String, phase: String, round: Int) -> Set<String> {
        guard !hasAmbiguousUnderscores(app), !hasAmbiguousUnderscores(phase) else { return [] }
        let dir = orchDirURL.appendingPathComponent("logs")
        guard let files = try? fm.contentsOfDirectory(atPath: dir.path) else { return [] }
        let prefix = "__\(app)__\(phase)__r\(round)."
        let tsLen = 22
        var done = Set<String>()
        for f in files {
            guard f.count > tsLen else { continue }
            let afterTs = f.index(f.startIndex, offsetBy: tsLen)
            guard f[afterTs...].hasPrefix(prefix) else { continue }
            let rest = f[f.index(afterTs, offsetBy: prefix.count)...]
            guard let sep = rest.range(of: "__") else { continue }
            done.insert(String(rest[..<sep.lowerBound]))
        }
        return done
    }

    private func hasAmbiguousUnderscores(_ s: String) -> Bool {
        s.hasPrefix("_") || s.hasSuffix("_") || s.contains("__")
    }

    // MARK: - Human interjection (the "join the conversation" text box)

    private func inboxURL(_ project: Project) -> URL {
        project.dirURL.appendingPathComponent("human_inbox.txt")
    }

    // Queue a human message. A live run folds it into the conversation on the
    // next turn (orchestrator drains this file); if nothing is running it stays
    // queued and shown as pending until the next run picks it up.
    // V3 board 1.7: ask a LIVE auto debate to pause at the next round barrier.
    // Marker-FIRST ordering is load-bearing: the engine's short step-in wait
    // only has to cover the gap between this write and sendHumanMessage's
    // inbox append (seconds), never a long approval-style timeout.
    func requestStepIn(_ project: Project) {
        try? Data().write(to: project.dirURL.appendingPathComponent(".step_in"))
    }

    func sendHumanMessage(_ project: Project, _ text: String) {
        let msg = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !msg.isEmpty else { return }
        let url = inboxURL(project)
        var existing = (try? String(contentsOf: url, encoding: .utf8)) ?? ""
        if !existing.isEmpty && !existing.hasSuffix("\n") { existing += "\n" }
        do {
            try (existing + msg + "\n").write(to: url, atomically: true, encoding: .utf8)
        } catch {
            // Don't silently drop the user's message — tell them it didn't queue.
            surfaceError("Could not queue your message for \(project.name): "
                + error.localizedDescription)
        }
        refresh()
    }

    func pendingHuman(_ project: Project) -> String {
        let s = (try? String(contentsOf: inboxURL(project), encoding: .utf8)) ?? ""
        return s.trimmingCharacters(in: .whitespacesAndNewlines)
    }

    // The orchestrator's global lock exists only while a pass is running.
    private func lockIsFresh() -> Bool {
        let lock = orchDirURL.appendingPathComponent(".lock")
        guard let attrs = try? fm.attributesOfItem(atPath: lock.path),
              let mtime = attrs[.modificationDate] as? Date else { return false }
        return Date().timeIntervalSince(mtime) < 180
    }

    // MARK: - Actions

    // Create a new chat (= project). The title is auto-slugified into a safe
    // folder name, so the user can type anything — including pasting the whole
    // idea. Only an empty prompt is rejected. Returns the created folder name.
    func createChat(title: String, prompt: String,
                    workflow: String = "app_build") -> (name: String?, error: String?) {
        let body = prompt.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !body.isEmpty else { return (nil, "Describe what you want in the prompt.") }
        let basis = title.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
            ? firstLine(body) : title
        var slug = OrchestratorStore.slugify(basis)
        // Ensure uniqueness.
        var candidate = slug
        var n = 2
        while fm.fileExists(atPath: rootURL.appendingPathComponent(candidate).path) {
            candidate = "\(slug)-\(n)"; n += 1
        }
        slug = candidate
        let appDir = rootURL.appendingPathComponent(slug)
        let promptDir = appDir.appendingPathComponent("initial_prompt")
        do {
            try fm.createDirectory(at: promptDir, withIntermediateDirectories: true)
            try body.write(to: promptDir.appendingPathComponent("initial_prompt.md"),
                           atomically: true, encoding: .utf8)
            // Record the chosen workflow so the engine runs the right pipeline.
            if workflow != "app_build" {
                try? (workflow + "\n").write(to: appDir.appendingPathComponent("workflow.txt"),
                                             atomically: true, encoding: .utf8)
            }
        } catch { return (nil, error.localizedDescription) }
        refresh()
        return (slug, nil)
    }

    // MARK: - Editing sub-agents (roles.json) and rounds (workflows/<name>.json)

    // Persist the current roles + personalities, preserving any other keys
    // (e.g. the _comment) already in roles.json.
    func saveRoles(_ newRoles: [RoleDef], _ newPersonalities: [PersonalityDef]) {
        var obj: [String: Any] = [:]
        if let data = try? Data(contentsOf: rolesURL),
           let existing = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
            obj = existing
        }
        obj["roles"] = newRoles.map { ["id": $0.id, "name": $0.name, "focus": $0.focus] }
        obj["personalities"] = newPersonalities.map { ["id": $0.id, "name": $0.name, "style": $0.style] }
        // Only publish the edits if they actually persisted — otherwise the
        // editor's values would silently revert on the next relaunch.
        guard writeJSON(obj, to: rolesURL) else { return }
        roles = newRoles
        personalities = newPersonalities
    }

    func setAgentRole(_ agent: String, _ roleID: String) {
        var obj: [String: Any] = [:]
        if let data = try? Data(contentsOf: rolesURL),
           let existing = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
            obj = existing
        }
        var overrides = (obj["agent_role_overrides"] as? [String: String]) ?? agentRoleOverrides
        if roleID.isEmpty {
            overrides.removeValue(forKey: agent)
        } else {
            overrides[agent] = roleID
        }
        obj["agent_role_overrides"] = overrides
        guard writeJSON(obj, to: rolesURL) else { return }
        agentRoleOverrides = overrides
    }

    // Update one phase's round count inside a workflow file. We deliberately edit
    // via JSONSerialization on the raw dictionary (NOT Codable) so EVERY other
    // field — purpose, roles, and especially the `verify` build spec — is carried
    // through untouched. Do not "simplify" this to a Codable round-trip: PhaseJSON
    // doesn't model `verify`, so that would silently drop it.
    func setPhaseRounds(workflow name: String, phaseKey: String, rounds: Int) {
        let url = workflowsDirURL.appendingPathComponent(name + ".json")
        guard let data = try? Data(contentsOf: url),
              var obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              var phases = obj["phases"] as? [[String: Any]] else { return }
        for i in phases.indices where (phases[i]["key"] as? String) == phaseKey {
            phases[i]["rounds"] = max(0, rounds)
        }
        obj["phases"] = phases
        writeJSON(obj, to: url)
        refresh()   // reloads workflows (and everything else) off the main thread
    }

    // MARK: - Workflow builder (custom workflows/<slug>.json authoring)

    // The workflows the engine ships (and re-materializes with --seed) — the
    // builder treats these as READ-ONLY so an upgrade or re-seed can never
    // clobber user edits. Duplicate one to get an editable copy.
    nonisolated static let builtInWorkflowNames: Set<String> = [
        "app_build", "app_build_child", "app_spec", "answer_question", "audit",
        "iterate", "library_mining", "productionize", "research", "sprint", "vslice"
    ]

    func isBuiltInWorkflow(_ name: String) -> Bool {
        Self.builtInWorkflowNames.contains(name)
    }

    // Every workflow file as a RAW top-level JSON dictionary (JSONSerialization,
    // never Codable) so the builder can round-trip fields the GUI doesn't model
    // — verify specs, budgets, checkpoints — byte-for-byte-in-meaning. Deduped
    // by internal name (first wins, stable by sorted filename), mirroring
    // BackgroundConfigLoader's workflow scan.
    func readRawWorkflows() -> [(fileURL: URL, obj: [String: Any])] {
        guard let items = try? fm.contentsOfDirectory(atPath: workflowsDirURL.path) else { return [] }
        var out: [(URL, [String: Any])] = []
        var seen = Set<String>()
        for fn in items.sorted() where fn.hasSuffix(".json") {
            let url = workflowsDirURL.appendingPathComponent(fn)
            guard let data = try? Data(contentsOf: url),
                  let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
            else { continue }
            let name = (obj["name"] as? String) ?? url.deletingPathExtension().lastPathComponent
            if seen.insert(name).inserted { out.append((url, obj)) }
        }
        return out
    }

    private func rawWorkflowFileURL(named name: String) -> URL? {
        readRawWorkflows().first {
            (($0.obj["name"] as? String)
             ?? $0.fileURL.deletingPathExtension().lastPathComponent) == name
        }?.fileURL
    }

    // Clone a workflow (built-in or custom) to a new editable file. Returns the
    // new workflow's name, or nil when the source can't be read.
    @discardableResult
    func duplicateWorkflow(named name: String) -> String? {
        guard let srcURL = rawWorkflowFileURL(named: name),
              let data = try? Data(contentsOf: srcURL),
              var obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
        else { return nil }
        let taken = Set(readRawWorkflows().map {
            ($0.obj["name"] as? String) ?? $0.fileURL.deletingPathExtension().lastPathComponent
        })
        var candidate = "\(name)-copy"
        var n = 2
        while taken.contains(candidate) || isBuiltInWorkflow(candidate)
                || fm.fileExists(atPath: workflowsDirURL.appendingPathComponent(candidate + ".json").path) {
            candidate = "\(name)-copy-\(n)"; n += 1
        }
        obj["name"] = candidate
        obj["title"] = ((obj["title"] as? String) ?? name) + " (copy)"
        writeJSON(obj, to: workflowsDirURL.appendingPathComponent(candidate + ".json"))
        refresh()
        return candidate
    }

    // Persist a CUSTOM workflow's raw dictionary to workflows/<name>.json.
    // Refuses built-in names outright, so no preset can ever be overwritten.
    @discardableResult
    func saveCustomWorkflow(_ obj: [String: Any], named name: String) -> Bool {
        guard !name.isEmpty, !isBuiltInWorkflow(name) else { return false }
        writeJSON(obj, to: workflowsDirURL.appendingPathComponent(name + ".json"))
        refresh()
        return true
    }

    // Delete a custom workflow file — to the Trash, never a hard rm.
    func deleteCustomWorkflow(named name: String) {
        guard !isBuiltInWorkflow(name), let url = rawWorkflowFileURL(named: name) else { return }
        removeRecoverably(url)
        runLog += "Moved workflow \(name) to the Trash.\n"
        refresh()
    }

    // Returns whether the write actually landed. A failed config write used to
    // only whisper into runLog while callers reported success to the UI and
    // updated published state — so a lost setting looked saved and silently
    // reverted on relaunch. Now failure surfaces the error banner and callers
    // that mutate state on the strength of a save MUST check the result.
    @discardableResult
    private func writeJSON(_ obj: [String: Any], to url: URL) -> Bool {
        do {
            let data = try JSONSerialization.data(
                withJSONObject: obj, options: [.prettyPrinted, .sortedKeys])
            try data.write(to: url)
            // A successful write clears a stale error banner: otherwise a red
            // banner from an earlier transient failure lingers after the retry
            // that fixed it (rulebook: clear errors that no longer apply).
            if lastError != nil { lastError = nil }
            return true
        } catch {
            surfaceError("Couldn't save \(url.lastPathComponent): \(error.localizedDescription)")
            return false
        }
    }

    // Chat Home history: lives in Application Support (not orchDirURL, which can
    // resolve to a from-source repo checkout — this must never land as a stray
    // file in a git working tree) and not rootURL (the project workspace, not
    // GUI state). Sibling to the bundled-engine-copy destination.
    // V3 board 1.4: chat_history.json is the LEGACY single-file location,
    // kept only as the migration source; per-chat files live under
    // chat_history/ via ChatHistoryStore.
    var chatHistoryURL: URL {
        Self.appSupportBaseURL.appendingPathComponent("chat_history.json")
    }

    var chatHistory: ChatHistoryStore {
        ChatHistoryStore(baseDir:
            Self.appSupportBaseURL.appendingPathComponent("chat_history",
                                                          isDirectory: true))
    }

    private func saveChatHistory() {
        guard !isLoadingChatHistory else { return }
        do {
            try chatHistory.save(chatMessages, key: currentChatKey)
        } catch {
            runLog += "Couldn't save chat history: \(error.localizedDescription)\n"
        }
    }

    private func loadChatHistory() {
        chatHistory.migrateLegacyIfNeeded(legacyURL: chatHistoryURL, homeKey: "home")
        isLoadingChatHistory = true
        // Missing file == genuinely empty chat: reset rather than keeping the
        // previous chat's messages on screen.
        chatMessages = chatHistory.load(key: currentChatKey) ?? []
        isLoadingChatHistory = false
    }

    // Switch the visible conversation to another history key: persist the
    // outgoing chat's draft, swap keys, restore the incoming chat's state.
    // (No UI caller mints non-"home" keys in M1 — engine chat sessions render
    // transcripts, not this store — but the isolation contract is load-bearing
    // and test-proven now so later keys can't cross-write histories.)
    func switchChat(to key: String) {
        guard key != currentChatKey else { return }
        draftsByChat[currentChatKey] = chatInput
        currentChatKey = key
        chatInput = draftsByChat[key] ?? ""
        chatThinking = thinkingKeys.contains(key)
        loadChatHistory()
    }

    // Concierge bookkeeping, keyed so a reply that lands after a chat switch
    // is delivered to the chat that asked — never the newly-focused one.
    func setChatThinking(_ thinking: Bool, for key: String) {
        if thinking { thinkingKeys.insert(key) } else { thinkingKeys.remove(key) }
        if key == currentChatKey { chatThinking = thinking }
    }

    func deliverConciergeReply(_ message: ConciergeMessage, to key: String) {
        setChatThinking(false, for: key)
        if key == currentChatKey {
            chatMessages.append(message)   // didSet persists under this key
        } else {
            do {
                try chatHistory.append(message, key: key)
            } catch {
                runLog += "Couldn't deliver chat reply to '\(key)': \(error.localizedDescription)\n"
            }
        }
    }

    // The only way back to a blank conversation: chatMessages has no other
    // reset path anywhere, so once it's non-empty the mode-card picker (gated
    // on chatMessages.isEmpty) is gone for good without this. Clears ONLY the
    // current chat's messages/file.
    func startNewChat() {
        chatMessages = []
        chatInput = ""
        setChatThinking(false, for: currentChatKey)
    }

    // nonisolated: pure string transform, exercised synchronously by
    // SlugifyTests from a nonisolated XCTest context.
    nonisolated static func slugify(_ s: String) -> String {
        var out = ""
        var prevDash = false
        for ch in s.lowercased() {
            if ch.isASCII && (ch.isLetter || ch.isNumber) {
                out.append(ch); prevDash = false
            } else if !prevDash && !out.isEmpty {
                out.append("-"); prevDash = true
            }
        }
        while out.hasSuffix("-") { out.removeLast() }
        if out.count > 40 { out = String(out.prefix(40)); while out.hasSuffix("-") { out.removeLast() } }
        return out.isEmpty ? "new-chat" : out
    }

    private func firstLine(_ s: String) -> String {
        for raw in s.components(separatedBy: "\n") {
            let line = raw.trimmingCharacters(in: CharacterSet(charactersIn: "# ").union(.whitespaces))
            if !line.isEmpty { return line }
        }
        return "new-chat"
    }

    // Launch one orchestrator pass for a single app. Mirrors run.sh by stripping
    // pay-as-you-go API key env vars so nothing incurs extra API cost.
    // Run now if idle, otherwise add to the queue (one project runs at a time).
    func runOrQueue(_ name: String) {
        if orchestratorRunning || launchingName != nil || !runQueue.isEmpty {
            if !runQueue.contains(name) && name != launchingName { runQueue.append(name) }
        } else {
            launchQueued(name)
        }
    }

    func removeFromQueue(_ name: String) {
        runQueue.removeAll { $0 == name }
    }

    func queuePosition(_ name: String) -> Int? {
        runQueue.firstIndex(of: name).map { $0 + 1 }
    }

    private func launchQueued(_ name: String) {
        launchingName = name
        launchingAt = Date()
        runProject(name)
    }

    // Called each refresh tick: launch the next queued project once the current one
    // has actually registered as finished (guarded against the state-write lag).
    private func advanceQueueIfIdle() {
        if let ln = launchingName {
            let running = projects.first { $0.name == ln }?.running ?? false
            let stale = Date().timeIntervalSince(launchingAt ?? Date()) > 20
            if running || stale { launchingName = nil }
        }
        // Paused: hold the queue (don't auto-launch the next project).
        if enginePaused { return }
        if !orchestratorRunning && launchingName == nil, let next = runQueue.first {
            runQueue.removeFirst()
            launchQueued(next)
        }
    }

    func toggleEnginePaused() {
        enginePaused.toggle()
        if !enginePaused { advanceQueueIfIdle() }   // resume: pick up where we left off
    }

    func runProject(_ name: String) {
        requestNotificationAuthIfNeeded()
        runLog = "Launching orchestrator for \(name)…\n"
        // Pass --root explicitly so the engine targets THIS workspace regardless of
        // what config.yaml's root says — keeps the GUI and engine in agreement when
        // the app is pointed at a workspace via ORCH_ROOT (V2 spec §27).
        launch(args: ["orchestrator.py", "--root", rootURL.path, "--app", name], project: name)
    }

    // Optional live demo: stream fake agent turns into a phase so you can watch
    // the transcript fill in real time without the agent CLIs installed.
    func demoStream(_ name: String) {
        runLog = "Streaming a demo conversation into \(name)…\n"
        // --root explicitly, mirroring runProject, so the demo writes into THIS
        // workspace rather than the script's own default.
        launch(args: ["simulate_stream.py", "--root", rootURL.path, "--app", name], project: name)
    }

    // True when this GUI session owns a live process for the project (Stop works).
    func canStop(_ name: String) -> Bool {
        stoppableProjects.contains(name) && (runningProcesses[name]?.isRunning ?? false)
    }

    // Stop a running project: SIGTERM (the engine's signal handler releases its
    // locks), escalate to SIGKILL after a ~5s grace, then defensively clear the
    // per-app lock (<engine>/locks/<app>.lock) in case it was left behind.
    func stopProject(_ name: String) {
        guard let proc = runningProcesses[name] else {
            runLog += "\(name) wasn't launched from this window, so it can't be stopped here.\n"
            return
        }
        manualStops[name] = Date()
        runLog += "Stopping \(name)…\n"
        let pid = proc.processIdentifier
        if proc.isRunning { proc.terminate() }   // SIGTERM
        Task { @MainActor [weak self] in
            try? await Task.sleep(nanoseconds: 5_000_000_000)
            guard let self else { return }
            if proc.isRunning {
                kill(pid, SIGKILL)
                self.runLog += "\(name) didn't exit within 5s — killed.\n"
            }
            // The engine normally removes its own lock on SIGTERM; clean up
            // defensively in case it didn't get the chance. Locks live in the
            // workspace (<root>/.orch-locks) so both engine copies contend for
            // the same file; the engine-local path is the legacy location.
            // Only delete a lock the STOPPED run (or a dead process) owns — a
            // relaunch within this 5s grace may already hold a fresh lock.
            for lockURL in [SessionLayout.lockURL(rootURL: self.rootURL, id: name),
                            self.orchDirURL.appendingPathComponent("locks/\(name).lock")] {
                guard let text = try? String(contentsOf: lockURL, encoding: .utf8) else { continue }
                let ownerPid = text.split(separator: " ")
                    .first { $0.hasPrefix("pid=") }
                    .flatMap { Int32($0.dropFirst(4)) }
                let ownedByStopped = ownerPid == pid
                let ownerAlive = ownerPid.map { kill($0, 0) == 0 } ?? false
                if ownedByStopped || !ownerAlive {
                    do {
                        try self.fm.removeItem(at: lockURL)
                    } catch {
                        // A lock we can't clear leaves the lane looking "running"
                        // forever — surface it (skip the benign already-gone case).
                        if self.fm.fileExists(atPath: lockURL.path) {
                            self.surfaceError("Could not clear stale lock for \(name) at "
                                + "\(lockURL.path): \(error.localizedDescription)")
                        }
                    }
                }
            }
            self.refresh()
        }
        refresh()
    }

    private func launch(args: [String], project: String? = nil) {
        guard engineAvailable else {
            runLog += "Cannot launch — \(engineMissingMessage)\n"
            return
        }
        let py = resolvePython()
        let proc = Process()
        proc.executableURL = URL(fileURLWithPath: py)
        proc.currentDirectoryURL = rootURL
        proc.arguments = [orchDirURL.appendingPathComponent(args[0]).path] + Array(args.dropFirst())

        var env = ProcessInfo.processInfo.environment
        for k in ["OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY",
                  "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL", "OPENAI_BASE_URL",
                  "GOOGLE_APPLICATION_CREDENTIALS"] {
            env.removeValue(forKey: k)
        }
        proc.environment = env

        let pipe = Pipe()
        proc.standardOutput = pipe
        proc.standardError = pipe
        pipe.fileHandleForReading.readabilityHandler = { [weak self] handle in
            let data = handle.availableData
            guard !data.isEmpty, let s = String(data: data, encoding: .utf8) else { return }
            Task { @MainActor in
                self?.runLog += s
                // Bounded tail, trimmed on a LINE boundary (RunLogBuffer) so the
                // panel never shows a garbled half-line after truncation.
                if let r = self?.runLog {
                    self?.runLog = RunLogBuffer.trim(r)
                }
                self?.refresh()
            }
        }
        proc.terminationHandler = { [weak self] p in
            let status = p.terminationStatus
            let uncaught = p.terminationReason == .uncaughtSignal
            Task { @MainActor in
                // §5.2: a SIGKILL'd child reports uncaughtSignal with the
                // SIGNAL number in terminationStatus — "killed by signal 9"
                // is not "exited with code 9".
                self?.runLog += uncaught
                    ? "\n[killed by signal \(status)]\n"
                    : "\n[exited with code \(status)]\n"
                pipe.fileHandleForReading.readabilityHandler = nil
                if let name = project {
                    self?.runningProcesses[name] = nil
                    self?.stoppableProjects.remove(name)
                    self?.noteChatTermination(name: name, status: status,
                                              uncaughtSignal: uncaught)
                }
                self?.refresh()
            }
        }
        do {
            try proc.run()
            // Retain the handle so the run can be stopped from the UI.
            if let name = project {
                runningProcesses[name] = proc
                stoppableProjects.insert(name)
                manualStops[name] = nil   // a fresh launch clears any old Stop
            }
        } catch { runLog += "Failed to launch: \(error.localizedDescription)\n" }
    }

    // V3 board 2.6: one debounced-upstream query against the engine's
    // search.py (the palette debounces; this guards staleness). Results
    // from a superseded query are DISCARDED — an old slow query must not
    // overwrite a newer one's rows (§12.2).
    func searchTranscripts(_ text: String) {
        let trimmed = text.trimmingCharacters(in: .whitespaces)
        searchGeneration += 1
        let gen = searchGeneration
        guard !trimmed.isEmpty else {
            searchHits = []
            searchInFlight = false
            return
        }
        searchInFlight = true
        let py = resolvePython()
        let script = orchDirURL.appendingPathComponent("search.py").path
        let root = rootURL.path
        Task.detached { [weak self] in
            let proc = Process()
            proc.executableURL = URL(fileURLWithPath: py)
            proc.arguments = [script, "--root", root, "--query", trimmed,
                              "--json", "--limit", "12"]
            let out = Pipe()
            proc.standardOutput = out
            proc.standardError = Pipe()   // degraded warning rides the JSON too
            try? proc.run()
            proc.waitUntilExit()
            let data = out.fileHandleForReading.readDataToEndOfFile()
            let parsed = SearchResultParser.parse(data)
            await MainActor.run { [weak self] in
                guard let self, gen == self.searchGeneration else { return }
                self.searchInFlight = false
                guard let parsed else {
                    // Malformed/failed run is a capability loss, not "no
                    // matches" — surface it like the degraded mode.
                    self.searchHits = []
                    self.searchStatus = "degraded:search-unavailable"
                    return
                }
                self.searchHits = parsed.hits
                self.searchStatus = parsed.status
            }
        }
    }

    func clearSearch() {
        searchGeneration += 1
        searchHits = []
        searchStatus = "ok"
        searchInFlight = false
    }

    private func resolvePython() -> String {
        // Common install locations first (a Finder-launched app inherits a
        // minimal PATH), THEN whatever is actually on PATH — the old version
        // hardcoded three dirs and blindly returned /usr/bin/python3 even when
        // it didn't exist, aborting every launch.
        var dirs = ["/opt/homebrew/bin", "/usr/local/bin", "/usr/bin"]
        if let path = ProcessInfo.processInfo.environment["PATH"] {
            dirs += path.split(separator: ":").map(String.init)
        }
        for dir in dirs {
            for name in ["python3", "python"] {
                let p = (dir as NSString).appendingPathComponent(name)
                if fm.isExecutableFile(atPath: p) { return p }
            }
        }
        return "/usr/bin/python3"
    }

    // MARK: - Factory dashboard (queue order / lanes / autorun / retry / stop / create)

    var queueOrderURL: URL { rootURL.appendingPathComponent(".orch-queue-order.json") }

    private func persistQueueFile() {
        writeJSON(["order": queueOrder, "lanes": buildLanes], to: queueOrderURL)
    }

    func setBuildLanes(_ n: Int) {
        buildLanes = min(9, max(1, n))
        persistQueueFile()
    }

    func appendToQueueOrder(_ name: String) {
        if !queueOrder.contains(name) { queueOrder.append(name) }
        persistQueueFile()
    }

    // Drop a removed/archived project from the persisted queue order so the
    // shepherd never tries to launch it again.
    func removeFromQueueOrder(_ name: String) {
        guard queueOrder.contains(name) else { return }
        queueOrder.removeAll { $0 == name }
        persistQueueFile()
    }

    // New App intake "Next" position: move (or insert) the app at the front
    // of the persisted queue order so the shepherd launches it first.
    func prioritizeInQueueOrder(_ name: String) {
        queueOrder.removeAll { $0 == name }
        queueOrder.insert(name, at: 0)
        persistQueueFile()
    }

    // Begin a drag over the queued list: pin the persisted order to exactly
    // what's displayed (apps missing from the file sort after listed ones), so
    // index math during the drag is stable; ticks stop re-reading the file
    // until endQueueDrag() persists the result.
    func beginQueueDrag(displayedOrder: [String]) {
        queueDragActive = true
        queueDragStarted = Date()
        if queueOrder != displayedOrder { queueOrder = displayedOrder }
    }

    func moveQueued(_ dragged: String, over target: String) {
        guard let from = queueOrder.firstIndex(of: dragged),
              let to = queueOrder.firstIndex(of: target), from != to else { return }
        queueOrder.move(fromOffsets: IndexSet(integer: from),
                        toOffset: to > from ? to + 1 : to)
    }

    func endQueueDrag() {
        guard queueDragActive else { return }
        queueDragActive = false
        queueDragStarted = nil
        persistQueueFile()
    }

    // Autorun toggle: <app>/.orchestrator_autorun_disabled is the marker the
    // shepherd honours (it skips the app while the file exists).
    func setAutorunEnabled(_ name: String, _ enabled: Bool) {
        let url = rootURL.appendingPathComponent(name)
            .appendingPathComponent(".orchestrator_autorun_disabled")
        if enabled {
            try? fm.removeItem(at: url)
            autorunDisabled.remove(name)
        } else {
            try? "disabled from the GUI\n".write(to: url, atomically: true, encoding: .utf8)
            autorunDisabled.insert(name)
        }
    }

    // Retry a failed app: clear agent_state.json's error (keeping every other
    // field) and drop the shepherd's retry counter so it relaunches the app on
    // its next pass.
    func retryFailedApp(_ name: String) {
        let dir = rootURL.appendingPathComponent(name)
        let stateURL = dir.appendingPathComponent("agent_state.json")
        guard let data = try? Data(contentsOf: stateURL),
              var obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else {
            runLog += "Couldn't read \(name)/agent_state.json to clear its error.\n"
            return
        }
        obj["error"] = ""
        writeJSON(obj, to: stateURL)
        try? fm.removeItem(at: dir.appendingPathComponent(".shepherd_retries"))
        runLog += "Cleared error on \(name) — the shepherd will relaunch it.\n"
        refresh()
    }

    // Stop a run no matter who launched it. A GUI-owned process takes the clean
    // stopProject path; otherwise signal the engine pid named in the lock file
    // (SIGTERM → its handler releases locks and reaps its agents, which run in
    // their own sessions; SIGKILL after 5s), then clear the lock so the lane
    // frees. Deliberately NOT killpg: a shepherd-launched run shares its
    // process group with shepherd.sh and every other lane.
    // Remove a run lock, surfacing failure (a lock we can't delete keeps the
    // lane pinned "running"). Silent on the benign already-gone case.
    private func clearLockFile(_ url: URL, _ name: String) {
        do {
            try fm.removeItem(at: url)
        } catch {
            if fm.fileExists(atPath: url.path) {
                surfaceError("Could not clear the run lock for \(name) at \(url.path): "
                    + error.localizedDescription)
            }
        }
    }

    func stopRun(_ name: String) {
        if canStop(name) {
            stopProject(name)
            return
        }
        let lockURL = SessionLayout.lockURL(rootURL: rootURL, id: name)
        manualStops[name] = Date()
        guard let pid = appLocks[name]?.pid, pid > 0 else {
            clearLockFile(lockURL, name)
            refresh()
            return
        }
        // A lock left behind by a process that died without cleaning up (crash,
        // SIGKILL from elsewhere) still names a pid — but that pid can since have
        // been recycled by an unrelated OS process. Signaling a dead pid is a
        // silent no-op; confirm liveness first so we never SIGTERM a stranger.
        guard kill(pid, 0) == 0 else {
            runLog += "\(name)'s lock names pid \(pid), which is no longer running — clearing the stale lock.\n"
            clearLockFile(lockURL, name)
            refresh()
            return
        }
        kill(pid, SIGTERM)
        runLog += "Stopping \(name) (pid \(pid))…\n"
        Task { @MainActor [weak self] in
            try? await Task.sleep(nanoseconds: 5_000_000_000)
            guard let self else { return }
            if kill(pid, 0) == 0 {
                kill(pid, SIGKILL)
                self.runLog += "\(name) didn't exit within 5s — killed.\n"
            }
            self.clearLockFile(lockURL, name)
            self.refresh()
        }
        refresh()
    }

    // Create a new app the shepherd way: <slug>/initial_prompt/initial_prompt.md
    // + workflow.txt, then append it to the persisted queue order. Attached
    // docs are copied into <slug>/docs/ and — when requested — an empty
    // <slug>/.backfill_requested marker tells the engine to distill the docs
    // into the early phases instead of debating them from scratch.
    @discardableResult
    func createFactoryApp(slug: String, idea: String, workflow: String,
                          docs: [URL] = [], backfillFromDocs: Bool = false) -> Bool {
        let body = idea.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !slug.isEmpty, !body.isEmpty else { return false }
        let appDir = rootURL.appendingPathComponent(slug)
        guard !fm.fileExists(atPath: appDir.path) else { return false }
        let promptDir = appDir.appendingPathComponent("initial_prompt")
        do {
            try fm.createDirectory(at: promptDir, withIntermediateDirectories: true)
            try (body + "\n").write(to: promptDir.appendingPathComponent("initial_prompt.md"),
                                    atomically: true, encoding: .utf8)
            try (workflow + "\n").write(to: appDir.appendingPathComponent("workflow.txt"),
                                        atomically: true, encoding: .utf8)
            if !docs.isEmpty {
                let docsDir = appDir.appendingPathComponent("docs", isDirectory: true)
                try fm.createDirectory(at: docsDir, withIntermediateDirectories: true)
                for src in docs {
                    // Unique-ify on filename collision so no attachment is lost.
                    var dest = docsDir.appendingPathComponent(src.lastPathComponent)
                    var n = 2
                    let base = src.deletingPathExtension().lastPathComponent
                    let ext = src.pathExtension
                    while fm.fileExists(atPath: dest.path) {
                        let candidate = ext.isEmpty ? "\(base)-\(n)" : "\(base)-\(n).\(ext)"
                        dest = docsDir.appendingPathComponent(candidate)
                        n += 1
                    }
                    try fm.copyItem(at: src, to: dest)
                }
                if backfillFromDocs {
                    fm.createFile(atPath: appDir.appendingPathComponent(".backfill_requested").path,
                                  contents: Data())
                }
            }
        } catch {
            runLog += "Couldn't create \(slug): \(error.localizedDescription)\n"
            return false
        }
        appendToQueueOrder(slug)
        refresh()
        return true
    }

    // Count of *.swift under <app>/app_build — the "done" row caption. Cached
    // for five minutes; a finished app's tree rarely changes.
    func swiftFileCount(for name: String) async -> Int? {
        if let hit = swiftCountCache[name], Date().timeIntervalSince(hit.at) < 300 {
            return hit.count > 0 ? hit.count : nil
        }
        let dir = rootURL.appendingPathComponent(name).appendingPathComponent("app_build")
        let count = await Task.detached(priority: .utility) {
            Self.countSwiftFiles(under: dir)
        }.value
        swiftCountCache[name] = (count, Date())
        return count > 0 ? count : nil
    }

    private nonisolated static func countSwiftFiles(under dir: URL) -> Int {
        let fm = FileManager.default
        var isDir: ObjCBool = false
        guard fm.fileExists(atPath: dir.path, isDirectory: &isDir), isDir.boolValue,
              let en = fm.enumerator(at: dir, includingPropertiesForKeys: nil,
                                     options: [.skipsHiddenFiles]) else { return 0 }
        var n = 0
        for case let url as URL in en where url.pathExtension == "swift" { n += 1 }
        return n
    }

    // "● shepherd active" in the top bar: is a shepherd.sh loop alive? Checked
    // via pgrep at most every 5s (the refresh timer ticks every 1.5s).
    private func pollShepherdIfDue() {
        guard !shepherdCheckInFlight,
              Date().timeIntervalSince(lastShepherdCheck) > 5 else { return }
        shepherdCheckInFlight = true
        lastShepherdCheck = Date()
        let proc = Process()
        proc.executableURL = URL(fileURLWithPath: "/usr/bin/pgrep")
        proc.arguments = ["-f", "shepherd.sh"]
        proc.standardOutput = Pipe()
        proc.standardError = Pipe()
        proc.terminationHandler = { [weak self] p in
            let active = p.terminationStatus == 0
            Task { @MainActor in
                self?.shepherdCheckInFlight = false
                if self?.shepherdActive != active { self?.shepherdActive = active }
            }
        }
        do { try proc.run() } catch { shepherdCheckInFlight = false }
    }

    // Last ~64KB of a file as text (whole file when smaller), starting on a
    // whole line. nil when the file doesn't exist or can't be opened. Powers
    // the dashboard's terminal well without ever reading a multi-MB log fully.
    nonisolated static func tailText(url: URL, maxBytes: Int = 65_536) -> String? {
        guard let handle = try? FileHandle(forReadingFrom: url) else { return nil }
        defer { try? handle.close() }
        let size = Int((try? handle.seekToEnd()) ?? 0)
        let offset = max(0, size - maxBytes)
        try? handle.seek(toOffset: UInt64(offset))
        let data = handle.readDataToEndOfFile()
        var text = String(decoding: data, as: UTF8.self)
        if offset > 0, let nl = text.firstIndex(of: "\n") {
            text = String(text[text.index(after: nl)...])
        }
        return text
    }
}
