import Foundation
import AppKit
import Combine
import UserNotifications
import SwiftUI

struct FileFingerprint: Equatable, Sendable {
    let mtime: Date
    let size: UInt64

    static func read(_ url: URL, fileManager: FileManager = .default) -> FileFingerprint {
        guard let attrs = try? fileManager.attributesOfItem(atPath: url.path) else {
            return FileFingerprint(mtime: .distantPast, size: 0)
        }
        let mtime = attrs[.modificationDate] as? Date ?? .distantPast
        let size = (attrs[.size] as? NSNumber)?.uint64Value ?? 0
        return FileFingerprint(mtime: mtime, size: size)
    }
}

struct ProjectScanCacheEntry: @unchecked Sendable {
    var fingerprints: [String: FileFingerprint]
    var workflowSignature: Int
    var project: Project
    var scannedAt: Date
}

struct ProjectScanBatch {
    var projects: [Project]
    var cache: [String: ProjectScanCacheEntry]
}

enum BackgroundProjectLoader {
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
        loadProjectsCached(
            names: names, rootURL: rootURL,
            workflowsByName: workflowsByName,
            defaultWorkflow: defaultWorkflow, manualStops: manualStops,
            runningProcessNames: runningProcessNames,
            cache: [:], dueIntervals: [:], now: Date()).projects
    }

    static func loadProjectsCached(
        names: [String], rootURL: URL,
        workflowsByName: [String: WorkflowDef], defaultWorkflow: WorkflowDef?,
        manualStops: [String: Date], runningProcessNames: Set<String>,
        cache: [String: ProjectScanCacheEntry],
        dueIntervals: [String: TimeInterval], now: Date,
        onParse: ((String) -> Void)? = nil
    ) -> ProjectScanBatch {
        guard !names.isEmpty else { return ProjectScanBatch(projects: [], cache: [:]) }
        let lock = NSLock()
        var results = Array<Project?>(repeating: nil, count: names.count)
        var nextCache = cache.filter { names.contains($0.key) }
        DispatchQueue.concurrentPerform(iterations: names.count) { idx in
            let name = names[idx]
            let prior = cache[name]
            let interval = dueIntervals[name] ?? 5.0
            let due = prior == nil || now.timeIntervalSince(prior!.scannedAt) >= interval
            let project: Project?
            let entry: ProjectScanCacheEntry?
            if let prior, !due {
                project = prior.project
                entry = prior
            } else {
                let fingerprints = scanFingerprints(name: name, rootURL: rootURL)
                let signature = workflowSignature(
                    for: prior?.project.workflow,
                    workflowsByName: workflowsByName,
                    defaultWorkflow: defaultWorkflow)
                if let prior, prior.fingerprints == fingerprints,
                   prior.workflowSignature == signature {
                    let refreshed = refreshDerivedState(
                        prior.project, fingerprints: fingerprints,
                        stopAt: manualStops[name],
                        processRunning: runningProcessNames.contains(name), now: now)
                    project = refreshed
                    entry = ProjectScanCacheEntry(fingerprints: fingerprints,
                                                  workflowSignature: signature,
                                                  project: refreshed,
                                                  scannedAt: now)
                } else {
                    onParse?(name)
                    project = loadProject(
                        name: name, rootURL: rootURL,
                        workflowsByName: workflowsByName,
                        defaultWorkflow: defaultWorkflow,
                        stopAt: manualStops[name],
                        processRunning: runningProcessNames.contains(name))
                    entry = project.map {
                        ProjectScanCacheEntry(fingerprints: fingerprints,
                                              workflowSignature: workflowSignature(
                                                for: $0.workflow,
                                                workflowsByName: workflowsByName,
                                                defaultWorkflow: defaultWorkflow),
                                              project: $0, scannedAt: now)
                    }
                }
            }
            lock.lock()
            results[idx] = project
            nextCache[name] = entry
            lock.unlock()
        }
        return ProjectScanBatch(projects: results.compactMap { $0 },
                                cache: nextCache)
    }

    private static func workflowSignature(
        for name: String?, workflowsByName: [String: WorkflowDef],
        defaultWorkflow: WorkflowDef?
    ) -> Int {
        (name.flatMap { workflowsByName[$0] } ?? defaultWorkflow)?.hashValue ?? 0
    }

    private static func refreshDerivedState(
        _ cached: Project, fingerprints: [String: FileFingerprint],
        stopAt: Date?, processRunning: Bool, now: Date
    ) -> Project {
        var project = cached
        let statePath = project.dirURL.appendingPathComponent("agent_state.json").path
        let stateMTime = fingerprints[statePath]?.mtime
        var running = project.status == .inProgress
            && stateMTime.map { now.timeIntervalSince($0) < 240 } == true
        var stopped = false
        if let stopAt {
            if let stateMTime, stateMTime.timeIntervalSince(stopAt) > 10 {
                // A later run owns the unchanged cached parse.
            } else if !processRunning {
                running = false
                stopped = project.status == .inProgress
            }
        }
        project.running = running
        project.manuallyStopped = stopped
        return project
    }

    private static func scanFingerprints(name: String, rootURL: URL)
        -> [String: FileFingerprint] {
        let dir = rootURL.appendingPathComponent(name, isDirectory: true)
        var urls = [
            dir.appendingPathComponent("agent_state.json"),
            dir.appendingPathComponent("workflow.txt"),
            dir.appendingPathComponent("initial_prompt/initial_prompt.md"),
            dir.appendingPathComponent("verify_results.json"),
            dir.appendingPathComponent("run_config.json"),
            dir.appendingPathComponent(".orch_archived")
        ]
        let parts = name.split(separator: "/")
        if parts.count >= 3 {
            urls.append(rootURL.appendingPathComponent(String(parts[0]))
                .appendingPathComponent("run_config.json"))
        }
        return Dictionary(uniqueKeysWithValues: urls.map {
            ($0.path, FileFingerprint.read($0))
        })
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
        var sensitivity = "normal"
        var promotedFromEnroll = false

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
            promotedFromEnroll = obj["promoted_from_enroll"] is [String: Any]
            sensitivity = (obj["sensitivity"] as? String) == "private"
                ? "private" : "normal"
            let done = (obj["done"] as? Bool) ?? false
            status = ProjectStatus.decode(engineValue: obj["status"] as? String,
                                          error: error, done: done)
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
        let projectDir = name.split(separator: "/").count >= 3
            ? rootURL.appendingPathComponent(
                String(name.split(separator: "/")[0]), isDirectory: true)
            : dir
        proj.sensitivity = ProjectSensitivityFile.effective(
            projectDir: projectDir, sessionDir: dir) ?? sensitivity
        proj.hasFinalComplianceReport = EnrollmentEvidence
            .hasFinalComplianceReport(projectDir: dir)
        proj.enrolled = resolvedName == "enroll" || promotedFromEnroll
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
enum BackgroundConfigLoader {
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

enum ProjectArchivePresentation {
    static func projectSlug(for sessionID: String) -> String {
        sessionID.split(separator: "/").first.map(String.init) ?? sessionID
    }

    static func confirmation(project: String, stopping: Bool) -> String {
        let prefix = stopping ? "The active run will be stopped first. " : ""
        return prefix + "The whole \(project) folder moves to workspace/.archive/"
            + "\(project), disappears from engine/search/GUI discovery, and can be "
            + "restored with --unarchive-project. Nothing is deleted."
    }
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
    // V3 7.0: the per-session run.pid fallback (bare int, engine-written
    // after lock acquisition, removed on clean exit). Nested ids embed
    // slashes; appendingPathComponent resolves them as path segments.
    static func readRunPid(rootURL: URL, id: String) -> Int32? {
        let url = rootURL.appendingPathComponent(id)
            .appendingPathComponent("run.pid")
        guard let text = try? String(contentsOf: url, encoding: .utf8),
              let pid = Int32(text.trimmingCharacters(
                  in: .whitespacesAndNewlines)), pid > 0 else { return nil }
        return pid
    }

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
    case newBrainstorm, sendToSection, openConductor, showOnboarding
    case focusPane1, focusPane2, focusPane3, closeFocusedPane
    case toggleInspector   // ⌥⌘I — Native Pro shell inspector
    case focusSearch       // ⌘F — focus the sidebar project filter
    case openPlanTab       // Inspector "Open Plan tab" jump (§3 region 3)
    case togglePause       // Pause/Resume Engine (toolbar + Command Palette)
}

struct PaneCanvasState: Equatable {
    static let maximumPanes = 3
    var panes: [String] = []
    var focusedSessionID: String?
    var overflow: [String] = []

    mutating func open(_ sessionID: String, split: Bool) {
        if panes.contains(sessionID) {
            focusedSessionID = sessionID
            return
        }
        removeEverywhere(sessionID)
        if panes.isEmpty {
            panes = [sessionID]
        } else if split && panes.count < Self.maximumPanes {
            panes.append(sessionID)
        } else if split {
            overflow.append(sessionID)
        } else if let focusedSessionID,
                  let index = panes.firstIndex(of: focusedSessionID) {
            panes[index] = sessionID
        } else {
            panes[0] = sessionID
        }
        focusedSessionID = panes.contains(sessionID) ? sessionID : focusedSessionID
        normalize()
    }

    mutating func replace(pane target: String, with sessionID: String) {
        guard target != sessionID else {
            focus(sessionID)
            return
        }
        guard panes.contains(target) else {
            open(sessionID, split: true)
            return
        }
        removeEverywhere(sessionID)
        guard let index = panes.firstIndex(of: target) else {
            open(sessionID, split: true)
            return
        }
        let displaced = panes[index]
        panes[index] = sessionID
        if displaced != sessionID { appendOverflow(displaced) }
        focusedSessionID = sessionID
        normalize()
    }

    mutating func focus(_ sessionID: String) {
        guard panes.contains(sessionID) else { return }
        focusedSessionID = sessionID
    }

    mutating func focusPane(at index: Int) {
        guard panes.indices.contains(index) else { return }
        focusedSessionID = panes[index]
    }

    mutating func closeFocused() {
        guard let focusedSessionID else { return }
        close(focusedSessionID)
    }

    mutating func close(_ sessionID: String) {
        guard let index = panes.firstIndex(of: sessionID) else {
            overflow.removeAll { $0 == sessionID }
            return
        }
        panes.remove(at: index)
        if focusedSessionID == sessionID {
            self.focusedSessionID = panes.isEmpty
                ? nil : panes[min(index, panes.count - 1)]
        }
        normalize()
    }

    mutating func activateOverflow(_ sessionID: String) {
        guard overflow.contains(sessionID) else { return }
        let target = focusedSessionID ?? panes.first
        guard let target else {
            open(sessionID, split: false)
            return
        }
        replace(pane: target, with: sessionID)
    }

    mutating func bringIntoVisiblePrefix(_ sessionID: String, count: Int) {
        guard let source = panes.firstIndex(of: sessionID), source >= count,
              count > 0 else {
            focus(sessionID)
            return
        }
        let target = min(max(0, count - 1), panes.count - 1)
        panes.swapAt(source, target)
        focusedSessionID = sessionID
    }

    func pollingInterval(for sessionID: String) -> TimeInterval {
        if focusedSessionID == sessionID { return 0.5 }
        if panes.contains(sessionID) { return 1.5 }
        return 5.0
    }

    func visibleCount(availableWidth: CGFloat, minimumPaneWidth: CGFloat = 300) -> Int {
        guard !panes.isEmpty else { return 0 }
        return min(panes.count, max(1, Int(availableWidth / minimumPaneWidth)))
    }

    private mutating func removeEverywhere(_ sessionID: String) {
        panes.removeAll { $0 == sessionID }
        overflow.removeAll { $0 == sessionID }
    }

    private mutating func appendOverflow(_ sessionID: String) {
        guard !panes.contains(sessionID), !overflow.contains(sessionID) else { return }
        overflow.append(sessionID)
    }

    private mutating func normalize() {
        var seen = Set<String>()
        panes = panes.filter { seen.insert($0).inserted }
        if panes.count > Self.maximumPanes {
            let extra = panes.dropFirst(Self.maximumPanes)
            panes = Array(panes.prefix(Self.maximumPanes))
            overflow.append(contentsOf: extra)
        }
        overflow = overflow.filter { !panes.contains($0) && seen.insert($0).inserted }
        if let focusedSessionID, !panes.contains(focusedSessionID) {
            self.focusedSessionID = panes.first
        } else if focusedSessionID == nil {
            focusedSessionID = panes.first
        }
    }
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

    // Fleet ordering remains a compatibility surface while SessionModel is the
    // canonical owner of each element's run state.
    @Published var projects: [Project] = []
    @Published private(set) var sessionModels: [String: SessionModel] = [:]
    private var sessionModelSubscriptions: [String: AnyCancellable] = [:]
    @Published var chatMetadata: [String: ChatMeta] = [:]
    @Published var chatMetaWarnings: [String: String] = [:]
    @Published var archivedChats: [ArchivedChat] = []
    @Published var chatMetaEditable: Set<String> = []
    @Published var orchestratorRunning = false
    @Published var runLog: String = ""   // tail of the most recent action's output
    // A failed action's message, shown as a dismissible top banner so errors
    // don't hide in the ⌘L-collapsed run log. Set via surfaceError().
    @Published var lastError: String?
    @Published var snippetWarnings: [String] = []
    @Published var commandWarnings: [String] = []
    @Published private(set) var enrollmentInFlight = false

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
    @Published private(set) var paneCanvas = PaneCanvasState()
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
    // V3 8.7: persisted first-run + progressive-disclosure state. Only Ideas
    // and Research begin visible; real routes or an explicit reveal grow it.
    @Published var onboardingProgress = OnboardingProgress.load(
        from: OrchestratorStore.defaults)
    @Published var showOnboarding = false
    @Published var visibleSectionIDs = OnboardingPersistence.visibleSections(
        from: OrchestratorStore.defaults)
    @Published var newlyRevealedSection: String?
    private var onboardingEvaluated = false
    private var onboardingStartedAt = OrchestratorStore.defaults.object(
        forKey: OnboardingPersistence.startedAtKey) as? Date
    // 4.10 seam: 7.10 can replace default-file resolution with actual pending
    // Conductor route truth without changing the transcript view.
    var routePreviewSource = RoutePreviewSource.defaults
    @Published var commandProjectName: String?
    @Published var commandRoutableArtifact: ArtifactRouteRef?
    @Published var conductorSurfaceAvailable = true
    @Published var conductorOversight = ConductorOversightSnapshot()
    @Published var missionControl = MissionControlSnapshot()
    @Published var pipelinePresetWarning: String?
    @Published var artifactRouteStates: [String: ArtifactRouteState] = [:]
    @Published var artifactsByProject: [String: [ArtifactSummary]] = [:]
    @Published var artifactFinalizeInFlight: Set<String> = []
    @Published var artifactFinalizeErrors: [String: String] = [:]
    // No manual message-publish entry point exists in the engine as of 4.9.
    // Keep the context action absent; a later engine card flips this only when
    // it can perform a real publish rather than mutate pixels.
    @Published var manualArtifactPublishAvailable = false
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
    @Published var quietHoursEnabled = OrchestratorStore.defaults.bool(
        forKey: "notificationQuietHoursEnabled") {
        didSet { Self.defaults.set(quietHoursEnabled,
                                   forKey: "notificationQuietHoursEnabled") }
    }
    @Published var quietHoursStartMinute: Int = {
        let d = OrchestratorStore.defaults
        return d.object(forKey: "notificationQuietHoursStart") == nil
            ? 22 * 60 : d.integer(forKey: "notificationQuietHoursStart")
    }() {
        didSet { Self.defaults.set(quietHoursStartMinute,
                                   forKey: "notificationQuietHoursStart") }
    }
    @Published var quietHoursEndMinute: Int = {
        let d = OrchestratorStore.defaults
        return d.object(forKey: "notificationQuietHoursEnd") == nil
            ? 7 * 60 : d.integer(forKey: "notificationQuietHoursEnd")
    }() {
        didSet { Self.defaults.set(quietHoursEndMinute,
                                   forKey: "notificationQuietHoursEnd") }
    }
    // V3 6.4: per-project cost rollups from costs.jsonl (CostsScanner, same
    // background tick). Empty until a project has cost records on disk.
    @Published var projectCosts: [String: ProjectCosts] = [:]

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
        didSet {
            if !isLoadingChatHistory {
                sessionModel(for: currentChatKey).chatMessages = chatMessages
            }
            saveChatHistory()
        }
    }
    @Published var chatInput = "" {
        didSet { sessionModel(for: currentChatKey).chatInput = chatInput }
    }
    @Published var chatThinking = false {
        didSet { sessionModel(for: currentChatKey).chatThinking = chatThinking }
    }
    // V3 board 1.4: per-chat history. currentChatKey selects which SessionModel
    // and history file the legacy fields mirror ("home" = Chat Home). Keeping
    // thinking state and drafts on that model means switching mid-reply can't
    // show a "Thinking…" the new chat never asked for (§12.1/R2).
    private(set) var currentChatKey = "home"
    // Load-guard: loadChatHistory assigns chatMessages, which fires didSet →
    // saveChatHistory; without this flag a load-during-switch would write
    // chat A's messages under chat B's key.
    private var isLoadingChatHistory = false
    // V3 board 1.5: engine-backed chat sessions minted by this GUI instance,
    // keyed by flat dir name. Lifecycle is the ChatSession state enum; the
    // scan-merge in refresh() only derives waiting/running transitions.
    var chatSessions: [String: ChatSession] {
        Dictionary(sessionModels.compactMap { key, model in
            model.chatSession.map { (key, $0) }
        }, uniquingKeysWith: { current, _ in current })
    }
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

    private let runController = RunController()
    private var runControllerSubscription: AnyCancellable?
    var stoppableProjects: Set<String> { runController.stoppableProjects }

    private var timer: Timer?
    private(set) var refreshTimerInstallCount = 0
    private var refreshInFlight = false
    private var refreshPending = false
    private var refreshGeneration = 0
    private let fm = FileManager.default

    @discardableResult
    func sessionModel(for name: String) -> SessionModel {
        if let existing = sessionModels[name] { return existing }
        let model = SessionModel(id: name)
        model.runController = runController
        sessionModelSubscriptions[name] = model.objectWillChange.sink { [weak self] _ in
            self?.objectWillChange.send()
        }
        sessionModels[name] = model
        return model
    }

    func openPane(_ sessionID: String, asSplit: Bool) {
        let hadFocus = paneCanvas.focusedSessionID != nil
        paneCanvas.open(sessionID, split: asSplit)
        syncFocusedPane(rescheduleFrom: hadFocus)
    }

    func replacePane(_ target: String, with sessionID: String) {
        let hadFocus = paneCanvas.focusedSessionID != nil
        paneCanvas.replace(pane: target, with: sessionID)
        syncFocusedPane(rescheduleFrom: hadFocus)
    }

    func focusPane(_ sessionID: String) {
        let hadFocus = paneCanvas.focusedSessionID != nil
        paneCanvas.focus(sessionID)
        syncFocusedPane(rescheduleFrom: hadFocus)
    }

    func focusPane(at index: Int) {
        let hadFocus = paneCanvas.focusedSessionID != nil
        paneCanvas.focusPane(at: index)
        syncFocusedPane(rescheduleFrom: hadFocus)
    }

    func closeFocusedPane() {
        let hadFocus = paneCanvas.focusedSessionID != nil
        paneCanvas.closeFocused()
        syncFocusedPane(rescheduleFrom: hadFocus)
    }

    func closePane(_ sessionID: String) {
        let hadFocus = paneCanvas.focusedSessionID != nil
        paneCanvas.close(sessionID)
        syncFocusedPane(rescheduleFrom: hadFocus)
    }

    func activateOverflowPane(_ sessionID: String) {
        let hadFocus = paneCanvas.focusedSessionID != nil
        paneCanvas.activateOverflow(sessionID)
        syncFocusedPane(rescheduleFrom: hadFocus)
    }

    func bringPaneIntoVisiblePrefix(_ sessionID: String, count: Int) {
        paneCanvas.bringIntoVisiblePrefix(sessionID, count: count)
        syncFocusedPane(rescheduleFrom: true)
    }

    private func syncFocusedPane(rescheduleFrom hadFocus: Bool) {
        focusedLivePane = paneCanvas.focusedSessionID
        updateCommandContext(projectName: focusedLivePane)
        let hasFocus = focusedLivePane != nil
        if hadFocus != hasFocus { rescheduleRefreshTimer() }
    }

    private func setChatSession(_ session: ChatSession?, for name: String) {
        sessionModel(for: name).chatSession = session
    }

    private func applyProjects(_ loaded: [Project], workers: [String: [BuildWorker]]) {
        let loadedNames = Set(loaded.map(\.name))
        for model in sessionModels.values where model.project != nil
            && !loadedNames.contains(model.id) {
            model.project = nil
            model.buildWorkers = nil
        }
        for project in loaded {
            let model = sessionModel(for: project.name)
            if model.project != project { model.project = project }
            let nextWorkers = workers[project.name]
            if model.buildWorkers != nextWorkers { model.buildWorkers = nextWorkers }
        }
        if loaded != projects { projects = loaded }
    }

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
        runControllerSubscription = runController.objectWillChange.sink { [weak self] _ in
            self?.objectWillChange.send()
        }
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
        conductorNotificationCoordinator = ConductorNotificationCoordinator(
            defaults: Self.defaults, namespace: url.path)
        // Resume offers are per-workspace; clearing here (belt) plus the
        // scannedRoot guard in updateResumeOffers (braces) keeps a stale
        // tick's offers from ever leaking across roots.
        staleLockFirstSeen = [:]
        crashedRuns = []
        staleLocks = []
        paneCanvas = PaneCanvasState()
        focusedLivePane = nil
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

    // One scheduler, never one timer per pane. A focused canvas runs the base
    // tick at 500ms; the project-cache schedule below admits other visible
    // panes every 1.5s and background sessions every 5s.
    private(set) var focusedLivePane: String? = nil

    nonisolated static func refreshInterval(focusedLive: Bool) -> Double {
        focusedLive ? 0.5 : 1.5
    }

    func setFocusedLivePane(_ name: String?) {
        if let name, paneCanvas.focusedSessionID != name { return }
        let desired = paneCanvas.focusedSessionID
        guard focusedLivePane != desired else { return }
        let before = focusedLivePane != nil
        focusedLivePane = desired
        if before != (desired != nil) { rescheduleRefreshTimer() }
    }

    private func scheduleRefreshTimer() {
        let interval = Self.refreshInterval(focusedLive: focusedLivePane != nil)
        let t = Timer.scheduledTimer(withTimeInterval: interval, repeats: true) { [weak self] _ in
            Task { @MainActor in self?.refresh() }
        }
        RunLoop.main.add(t, forMode: .common)
        timer = t
        refreshTimerInstallCount += 1
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
        let artifactRegistryURL = self.orchDirURL.appendingPathComponent("artifact_types.json")
        let manualStops = runController.manualStops
        let commandProjectName = self.commandProjectName
        let runningProcessNames = runController.runningProcessNames
        let delayForTests = Self.scanDelayForTests
        let scanDate = Date()
        let projectCache = Dictionary(uniqueKeysWithValues:
            sessionModels.compactMap { id, model in
                model.projectScanCache.map { (id, $0) }
            })
        let projectIntervals = Dictionary(uniqueKeysWithValues:
            projects.map { ($0.name, paneCanvas.pollingInterval(for: $0.name)) })
        let input = FleetScanInput(
            rootURL: rootURL, logsDirURL: logsDirURL,
            workflowsDirURL: workflowsDirURL, rolesURL: rolesURL,
            modelPresetsURL: modelPresetsURL, configURL: configURL,
            artifactRegistryURL: artifactRegistryURL, manualStops: manualStops,
            runningProcessNames: runningProcessNames,
            commandProjectName: commandProjectName,
            delayForTests: delayForTests,
            projectCache: projectCache,
            projectIntervals: projectIntervals,
            scanDate: scanDate)

        DispatchQueue.global(qos: .utility).async { [weak self] in
            let result = FleetScanner.scan(input)
            DispatchQueue.main.async {
                // A stale generation means the watchdog already recovered from
                // this attempt hanging — a fresher refresh may already be in
                // flight or applied, so this late result must not overwrite it.
                guard let self, self.refreshGeneration == myGeneration else { return }
                self.refreshInFlight = false
                self.apply(result.config)
                self.orchestratorRunning = result.projects.contains { $0.running }
                AppDelegate.runsActive = self.orchestratorRunning
                self.reloadSectionRail()
                self.detectTransitions(result.projects)
                self.applyProjects(result.projects, workers: result.workers)
                for (id, entry) in result.projectCache {
                    self.sessionModel(for: id).projectScanCache = entry
                }
                self.evaluateOnboarding(projectCount: result.projects.count)
                if result.chat.metadata != self.chatMetadata {
                    self.chatMetadata = result.chat.metadata
                }
                if result.chat.warnings != self.chatMetaWarnings {
                    self.chatMetaWarnings = result.chat.warnings
                }
                if result.chat.archived != self.archivedChats {
                    self.archivedChats = result.chat.archived
                }
                if result.chat.transcriptAvailable != self.chatMetaEditable {
                    self.chatMetaEditable = result.chat.transcriptAvailable
                }
                if commandProjectName == self.commandProjectName,
                   result.commandArtifact != self.commandRoutableArtifact {
                    self.commandRoutableArtifact = result.commandArtifact
                }
                if result.artifacts != self.artifactsByProject {
                    self.artifactsByProject = result.artifacts
                }
                if result.events != self.eventsByProject { self.eventsByProject = result.events }
                self.processConductorNotifications(result.events)
                self.revealSectionsFromRoutes(result.events)
                if result.health != self.fleetHealth { self.fleetHealth = result.health }
                if result.conductor != self.conductorOversight {
                    self.conductorOversight = result.conductor
                }
                if result.missionControl != self.missionControl {
                    self.missionControl = result.missionControl
                }
                // Mission Control has a designed pre-run empty state; the
                // surface remains available before .conductor/ exists.
                self.conductorSurfaceAvailable = true
                if result.costs != self.projectCosts { self.projectCosts = result.costs }
                self.escalateFallbacksIfNeeded(result.events)
                if result.locks != self.appLocks { self.appLocks = result.locks }
                if result.staleLocks != self.staleLocks { self.staleLocks = result.staleLocks }
                self.syncChatSessions(with: result.projects)
                if result.autorunDisabled != self.autorunDisabled {
                    self.autorunDisabled = result.autorunDisabled
                }
                self.updateResumeOffers(
                    scannedRoot: rootURL, loaded: result.projects,
                    locks: result.locks, stale: result.staleLocks,
                    autorun: result.autorunDisabled)
                if self.queueDragActive, let t = self.queueDragStarted,
                   Date().timeIntervalSince(t) > 30 {
                    self.endQueueDrag()   // abandoned drag — persist what's shown
                }
                if let qf = result.queueFile, !self.queueDragActive {
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
            guiOwnedLive: runController.runningProcessNames,
            queuedOrLaunching: queuedOrLaunching,
            manuallyStopped: Set(runController.manualStops.keys))
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
            setChatSession(session, for: minted.name)
            refresh()
            return session
        } catch {
            surfaceError("Couldn't create the chat: \(error.localizedDescription)")
            return nil
        }
    }

    @discardableResult
    func mintBrainstorm(project: String) -> ChatSession? {
        mintChatSession(project: project, section: "ideas", title: "brainstorm",
                        workflow: "chat_ideas", firstMessage: "Let's brainstorm.")
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
        setChatSession(s, for: id)
        launch(args: ["orchestrator.py", "--root", rootURL.path, "--app", id],
               project: id)
        // R2: 'running' only with a live handle backing it.
        if runController.isRunning(id) {
            var updated = chatSessions[id]
            updated?.state = .running
            setChatSession(updated, for: id)
        } else if chatSessions[id]?.state.isAlive == true {
            var updated = chatSessions[id]
            updated?.state = .crashed(code: -1, wasSignal: false)
            setChatSession(updated, for: id)
            surfaceError("The chat engine failed to launch — see the run log.")
        }
    }

    func stopChatSession(_ id: String) {
        guard chatSessions[id]?.state.isAlive == true else { return }
        var updated = chatSessions[id]
        updated?.state = .stopping
        setChatSession(updated, for: id)
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
        // writeJSON: atomic + surfaces failure — a swallowed error here left
        // the user believing a retry was queued that never reached disk.
        writeJSON(["agent": agent, "model": model],
                  to: dir.appendingPathComponent("\(phaseKey).retry"))
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
        // writeJSON: .atomic matters here — the engine re-reads this file at
        // the round barrier, and a read landing mid-write sees truncated JSON,
        // which load_routing fails open to defaults (dropping EVERY per-chat
        // override, not just this swap). Failure surfaces instead of leaving
        // the chip "pending" forever.
        writeJSON(obj, to: url)
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
            do {
                try proc.run()
            } catch {
                await MainActor.run { [weak self] in
                    self?.surfaceError("Fork failed to start: \(error.localizedDescription)")
                }
                return
            }
            // Read to EOF BEFORE waitUntilExit — the safe order when the
            // output could exceed the 64KB pipe buffer (a verbose traceback
            // would deadlock child-writer against GUI-waiter and leak the
            // process; conciergeAsk documents the same rule).
            let out = String(data: pipe.fileHandleForReading.readDataToEndOfFile(),
                             encoding: .utf8) ?? ""
            proc.waitUntilExit()
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
                self.setChatSession(ChatSession(
                    id: name,
                    project: parts.count == 3 ? parts[0] : name,
                    section: parts.count == 3 ? parts[1] : "",
                    slug: parts.count == 3 ? parts[2] : name,
                    workflow: workflow), for: name)
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

    func promoteEnrollment(_ project: Project) {
        guard project.status == .enrolledAwaitingApproval else {
            surfaceError("Enrollment report is not awaiting approval.")
            return
        }
        guard project.hasFinalComplianceReport else {
            surfaceError("A final compliance report is required before promotion.")
            return
        }
        launch(args: ["orchestrator.py", "--root", rootURL.path,
                      "--promote", project.name], project: project.name)
    }

    func enrollExisting(_ source: URL, onCreated: @escaping (String) -> Void) {
        guard engineAvailable else {
            surfaceError("Enrollment intake is unavailable — \(engineMissingMessage)")
            return
        }
        guard !enrollmentInFlight else { return }
        enrollmentInFlight = true
        let py = resolvePython()
        let engine = orchDirURL.appendingPathComponent("orchestrator.py")
        let root = rootURL
        let scoped = source.startAccessingSecurityScopedResource()
        Task.detached { [weak self] in
            defer { if scoped { source.stopAccessingSecurityScopedResource() } }
            let proc = Process()
            proc.executableURL = URL(fileURLWithPath: py)
            proc.currentDirectoryURL = root
            proc.arguments = EnrollmentCLI.arguments(
                engine: engine, root: root, source: source)
            var environment = ProcessInfo.processInfo.environment
            for key in APIKeyEnv.strippedAPIKeyVars {
                environment.removeValue(forKey: key)
            }
            proc.environment = environment
            let pipe = Pipe()
            proc.standardOutput = pipe
            proc.standardError = pipe
            do {
                try proc.run()
            } catch {
                await MainActor.run { [weak self] in
                    self?.enrollmentInFlight = false
                    self?.surfaceError("Enrollment intake could not start: \(error.localizedDescription)")
                }
                return
            }
            let output = String(
                data: pipe.fileHandleForReading.readDataToEndOfFile(),
                encoding: .utf8) ?? ""
            proc.waitUntilExit()
            let slug = EnrollmentCLI.createdSlug(output: output)
            await MainActor.run { [weak self] in
                guard let self else { return }
                self.enrollmentInFlight = false
                self.runLog = RunLogBuffer.trim(self.runLog + output)
                guard proc.terminationStatus == 0, let slug else {
                    self.surfaceError("Enrollment intake was refused — see the run log.")
                    return
                }
                self.refresh()
                onCreated(slug)
            }
        }
    }

    private func performPromotion(_ id: String) {
        pendingPromote.remove(id)
        setChatSession(nil, for: id)
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
        var updated = chatSessions[name]
        updated?.state = next
        setChatSession(updated, for: name)
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
            let alive = runController.isRunning(id)
            let awaiting = byName[id]?.awaitingHuman != nil
            let next = ChatSessionState.applyingScan(
                current: session.state, awaitingHuman: awaiting, processAlive: alive)
            if next != session.state {
                var updated = session
                updated.state = next
                setChatSession(updated, for: id)
            }
        }
    }

    // MARK: - Workflows + sub-agents (roles / personalities)

    var workflowsDirURL: URL { orchDirURL.appendingPathComponent("workflows", isDirectory: true) }
    var situationsDirURL: URL { orchDirURL.appendingPathComponent("situations", isDirectory: true) }
    var documentMapURL: URL {
        orchDirURL.appendingPathComponent("sections/documentation/doc_map.json")
    }
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

    func readDocumentMap() -> PipelineResult<DocumentMap> {
        guard let data = try? Data(contentsOf: documentMapURL) else {
            return .failure("Could not read \(documentMapURL.path)")
        }
        return DocumentMapCodec.decode(data)
    }

    func readSituationFiles() -> [SituationFileRecord] {
        SituationFileIO.load(root: situationsDirURL)
    }

    // situations.py owns the six defaults and its disk-wins seeding contract.
    // Invoke that existing contract instead of copying seed content into Swift.
    func ensureSituationSeeds() -> String? {
        if !SituationFileIO.load(root: situationsDirURL).isEmpty { return nil }
        let proc = Process()
        proc.executableURL = URL(fileURLWithPath: resolvePython())
        proc.currentDirectoryURL = orchDirURL
        proc.arguments = ["-c", "import situations,sys; situations.ensure_seeded(sys.argv[1])",
                          orchDirURL.path]
        let stderr = Pipe(); proc.standardError = stderr
        do {
            try proc.run(); proc.waitUntilExit()
            guard proc.terminationStatus == 0 else {
                let data = stderr.fileHandleForReading.readDataToEndOfFile()
                return String(data: data, encoding: .utf8) ?? "Situation seed command failed"
            }
            return nil
        } catch { return error.localizedDescription }
    }

    func writeSituation(_ canvas: SituationCanvas, to url: URL) throws -> Data {
        guard url.standardizedFileURL.path.hasPrefix(situationsDirURL.standardizedFileURL.path + "/") else {
            throw NSError(domain: "DocumentBuilder", code: 2,
                          userInfo: [NSLocalizedDescriptionKey: "Refusing to write outside situations/"])
        }
        return try SituationFileIO.save(canvas, to: url)
    }

    func duplicateSituation(_ source: SituationCanvas) -> URL? {
        let names = Set(readSituationFiles().map(\.name))
        let occupied = Set(((try? fm.contentsOfDirectory(atPath: situationsDirURL.path)) ?? [])
            .map { $0.lowercased() })
        let candidate = SituationLibraryNaming.copyName(
            source: source.name, existingNames: names, occupiedSlugs: occupied)
        var copy = source
        copy.name = candidate; copy.rawRoot["name"] = .string(candidate)
        copy.originalData = nil; copy.isDirty = true
        let url = situationsDirURL.appendingPathComponent(Self.slugify(candidate), isDirectory: true)
            .appendingPathComponent("situation.json")
        do { _ = try SituationFileIO.save(copy, to: url); return url }
        catch { surfaceError("Couldn't duplicate Situation: \(error.localizedDescription)"); return nil }
    }

    func deleteSituation(_ record: SituationFileRecord) {
        removeRecoverably(record.url.deletingLastPathComponent())
        runLog += "Moved Situation \(record.name) to the Trash.\n"
    }

    func situationWorkflowPhases(named name: String) -> [SituationWorkflowPhase] {
        guard let pair = readRawWorkflows().first(where: {
            (($0.obj["name"] as? String) ?? $0.fileURL.deletingPathExtension().lastPathComponent) == name
        }) else { return [] }
        return ((pair.obj["phases"] as? [[String: Any]]) ?? []).enumerated().map { index, raw in
            SituationWorkflowPhase(
                key: (raw["key"] as? String) ?? "phase\(index + 1)",
                title: (raw["title"] as? String) ?? (raw["key"] as? String) ?? "Phase \(index + 1)",
                docSections: (raw["doc_sections"] as? [String]) ?? [])
        }
    }

    func allSituationWorkflowPhases() -> [SituationWorkflowPhase] {
        var seen = Set<String>(), result: [SituationWorkflowPhase] = []
        for pair in readRawWorkflows() {
            for (index, raw) in ((pair.obj["phases"] as? [[String: Any]]) ?? []).enumerated() {
                let key = (raw["key"] as? String) ?? "phase\(index + 1)"
                guard seen.insert(key).inserted else { continue }
                result.append(SituationWorkflowPhase(
                    key: key, title: (raw["title"] as? String) ?? key,
                    docSections: (raw["doc_sections"] as? [String]) ?? []))
            }
        }
        return result
    }

    func engineSituationDiff(project: Project, candidate: String) -> PipelineResult<SituationApplyDiff> {
        SituationEngineQuery.diff(python: resolvePython(), moduleRoot: orchDirURL,
                                  orchDir: orchDirURL, projectDir: project.dirURL,
                                  workflow: project.workflow, candidate: candidate)
    }

    func engineSituationPreview(slotIDs: [String], workflow: String,
                                projectDir: URL?) async -> PipelineResult<SituationImpact> {
        let python = resolvePython(), moduleRoot = orchDirURL, orchDir = orchDirURL
        let result: (SituationImpact?, String?) = await Task.detached(priority: .utility) {
            () -> (SituationImpact?, String?) in
            switch SituationEngineQuery.preview(
                    python: python, moduleRoot: moduleRoot, orchDir: orchDir,
                    projectDir: projectDir, workflow: workflow,
                    slotIDs: slotIDs) {
            case .success(let impact): return (impact, nil)
            case .failure(let error): return (nil, error)
            }
        }.value
        if let impact = result.0 { return .success(impact) }
        return .failure(result.1 ?? "Situation impact query failed")
    }

    // The phases a given project runs (from its workflow; falls back to app_build).
    func phases(for project: Project) -> [PhaseDef] {
        (workflow(named: project.workflow) ?? workflow(named: "app_build"))?.phases ?? ALL_PHASES
    }

    func phasePurposes(for project: Project) -> [String: String] {
        Dictionary(uniqueKeysWithValues: phases(for: project).compactMap { phase in
            let purpose = phase.purpose.trimmingCharacters(in: .whitespacesAndNewlines)
            return purpose.isEmpty ? nil : (phase.key, purpose)
        })
    }

    func routePreviewTarget(
        for project: Project, finalOutput: String
    ) -> RoutePreviewPresentation? {
        let parts = project.name.contains("/")
            ? project.name.components(separatedBy: "/")
            : project.name.components(separatedBy: "--")
        guard parts.count == 3 else { return nil }
        let projectID = parts[0]
        let section = parts[1]
        guard let artifactType = ArtifactTypeHintParser.parse(finalOutput: finalOutput)
            ?? soleEmittedArtifactType(section: section) else { return nil }
        if let actual = missionControl.actualRoute(for: project.name) {
            return RoutePreviewPresentation(target: actual.target,
                                            truth: .conductor)
        }
        let fleetURL = orchDirURL.appendingPathComponent(
            "sections/\(section)/routing.json")
        let projectURL = rootURL.appendingPathComponent(projectID)
            .appendingPathComponent("routing.json")
        guard let fallback = routePreviewSource.target(RoutePreviewContext(
            section: section,
            artifactType: artifactType,
            fleetRouting: try? Data(contentsOf: fleetURL),
            projectRouting: try? Data(contentsOf: projectURL))) else { return nil }
        return RoutePreviewPresentation(target: fallback,
                                        truth: .routingDefault)
    }

    private func soleEmittedArtifactType(section: String) -> String? {
        let url = orchDirURL.appendingPathComponent("sections/\(section)/section.json")
        guard let data = try? Data(contentsOf: url),
              let obj = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any],
              let types = obj["artifact_types_emitted"] as? [String],
              types.count == 1 else { return nil }
        return types[0]
    }

    func updateCommandContext(projectName: String?) {
        commandProjectName = projectName
        guard let projectName else {
            commandRoutableArtifact = nil
            return
        }
        let projectID = projectName.components(separatedBy: "/").first ?? projectName
        commandRoutableArtifact = ArtifactRouteIndex.latestRoutable(
            projectDir: rootURL.appendingPathComponent(projectID))
    }

    func routeArtifact(_ artifact: ArtifactRouteRef, from sourceSession: String,
                       to targetSection: String) {
        let stateKey = artifactRouteStateKey(artifact.id, sourceSession: sourceSession)
        let parts = sourceSession.components(separatedBy: "/")
        guard let projectID = parts.first, !projectID.isEmpty,
              SessionLayout.validSlug(targetSection) else {
            artifactRouteStates[stateKey] = .refused(reason: "Invalid route target")
            return
        }
        let targetSession = "\(projectID)/\(targetSection)/\(artifact.id)"
        artifactRouteStates[stateKey] = .routing(target: targetSection)
        let python = resolvePython()
        let engine = orchDirURL.appendingPathComponent("orchestrator.py").path
        let root = rootURL.path
        Task.detached { [weak self] in
            let result = ArtifactRouteCommand.run(
                python: python, engine: engine, root: root,
                artifactID: artifact.id, sourceSession: sourceSession,
                targetSession: targetSession)
            await MainActor.run { [weak self] in
                guard let self else { return }
                let message = ArtifactRouteCommand.summary(
                    result.output, fallback: "route exited \(result.code)")
                self.runLog += result.output.hasSuffix("\n")
                    ? result.output : result.output + "\n"
                if result.code == 0 {
                    self.artifactRouteStates[stateKey] = .routed(target: targetSection)
                    self.refresh()
                } else {
                    self.artifactRouteStates[stateKey] = .refused(reason: message)
                    self.surfaceError(message)
                }
            }
        }
    }

    func artifactRouteState(_ artifactID: String,
                            sourceSession: String) -> ArtifactRouteState? {
        artifactRouteStates[artifactRouteStateKey(
            artifactID, sourceSession: sourceSession)]
    }

    private func artifactRouteStateKey(_ artifactID: String,
                                       sourceSession: String) -> String {
        sourceSession + "\u{1f}" + artifactID
    }

    func artifacts(for project: Project, phaseKey: String) -> [ArtifactSummary] {
        let projectID = project.name.components(separatedBy: "/").first ?? project.name
        let all = artifactsByProject[projectID] ?? []
        let eventIDs = Set((eventsByProject[project.name] ?? []).compactMap { event in
            event.kind == "artifact_published" && event.phase == phaseKey
                && !event.artifactID.isEmpty ? event.artifactID : nil
        })
        return all.filter { summary in
            summary.sourcePhase == phaseKey || eventIDs.contains(summary.id)
        }
    }

    func finalizeArtifact(_ artifact: ArtifactSummary, in sourceSession: String) {
        let stateKey = artifactActionStateKey(artifact.id, sourceSession: sourceSession)
        guard artifact.canHumanFinalize,
              !artifactFinalizeInFlight.contains(stateKey) else { return }
        artifactFinalizeErrors[stateKey] = nil
        artifactFinalizeInFlight.insert(stateKey)
        let python = resolvePython()
        let engine = orchDirURL.appendingPathComponent("orchestrator.py").path
        let root = rootURL.path
        Task.detached { [weak self] in
            let result = ArtifactFinalizeCommand.run(
                python: python, engine: engine, root: root,
                artifactID: artifact.id, sourceSession: sourceSession)
            await MainActor.run { [weak self] in
                guard let self else { return }
                self.artifactFinalizeInFlight.remove(stateKey)
                self.runLog += result.output.hasSuffix("\n")
                    ? result.output : result.output + "\n"
                if result.code == 0 {
                    self.artifactFinalizeErrors[stateKey] = nil
                    self.refresh()
                } else {
                    let reason = ArtifactRouteCommand.summary(
                        result.output, fallback: "finalize exited \(result.code)")
                    self.artifactFinalizeErrors[stateKey] = reason
                    self.surfaceError(reason)
                }
            }
        }
    }

    func artifactFinalizeIsInFlight(_ artifactID: String,
                                    sourceSession: String) -> Bool {
        artifactFinalizeInFlight.contains(artifactActionStateKey(
            artifactID, sourceSession: sourceSession))
    }

    func artifactFinalizeError(_ artifactID: String,
                               sourceSession: String) -> String? {
        artifactFinalizeErrors[artifactActionStateKey(
            artifactID, sourceSession: sourceSession)]
    }

    private func artifactActionStateKey(_ artifactID: String,
                                        sourceSession: String) -> String {
        sourceSession + "\u{1f}" + artifactID
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
    private lazy var conductorNotificationCoordinator =
        ConductorNotificationCoordinator(defaults: Self.defaults,
                                          namespace: rootURL.path)
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

    private func processConductorNotifications(
        _ eventsByProject: [String: [EngineEvent]], now: Date = Date()
    ) {
        let quiet = QuietHours(enabled: quietHoursEnabled,
                               startMinute: quietHoursStartMinute,
                               endMinute: quietHoursEndMinute)
        let requests = conductorNotificationCoordinator.process(
            eventsByProject: eventsByProject, quietHours: quiet, now: now)
        if !requests.isEmpty { requestNotificationAuthIfNeeded() }
        for request in requests {
            notify(request.title, request.body)
        }
    }

    private func statusKey(_ p: Project) -> String {
        if p.awaitingApproval != nil { return "awaiting" }
        switch p.status {
        case .done: return "done"
        case .aborted: return "aborted"
        case .inProgress: return "running"
        case .new: return "new"
        case .enrolledAwaitingApproval: return "enrolled_awaiting_approval"
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
    @Published private(set) var doctorProbeInFlight = false
    @Published private(set) var doctorProbeCompleted = false
    private var doctorProbeGeneration = 0

    // Re-run the engine doctor and republish the local_models block. Async and
    // best-effort: a dead python/engine just leaves the previous value in place.
    func refreshLocalModels() {
        guard !doctorProbeInFlight else { return }
        guard engineAvailable else {
            doctorProbeCompleted = true
            return
        }
        doctorProbeInFlight = true
        doctorProbeCompleted = false
        doctorProbeGeneration += 1
        let generation = doctorProbeGeneration
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
                guard self?.doctorProbeGeneration == generation else { return }
                self?.doctorProbeInFlight = false
                self?.doctorProbeCompleted = true
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
        do { try proc.run() } catch {
            doctorProbeInFlight = false
            doctorProbeCompleted = true
        }
        DispatchQueue.main.asyncAfter(deadline: .now() + 15) { [weak self] in
            guard let self, self.doctorProbeGeneration == generation,
                  self.doctorProbeInFlight else { return }
            self.doctorProbeGeneration += 1
            if proc.isRunning { proc.terminate() }
            self.doctorProbeInFlight = false
            self.doctorProbeCompleted = true
        }
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

    // MARK: Reload-before-mutate writers
    //
    // Editor views hold a loaded-once ModelRouting snapshot for display, so
    // writing the whole snapshot back would clobber every MODELED field some
    // other editor (the Defaults grid, the Inspector, applyProfile, a hand
    // edit) changed since the snapshot was taken — save(to:)'s raw-preserving
    // merge only protects UNmodeled keys. Each writer below re-reads the file
    // fresh (ModelRouting.load, bypassing the TTL cache: a mutation is a rare
    // click and the cached copy may itself be seconds stale), overlays ONLY
    // the section the calling editor owns, and saves the union.

    /// Fleet fallback.chains — the only section Models & Agents edits.
    func writeModelRoutingChains(_ chains: [String: [String]]) {
        var current = ModelRouting.load(from: modelRoutingURL)
        current.chains = chains
        writeModelRouting(current)
    }

    /// One phase of a project's routing file (Plan tab tuning editor).
    /// nil route deletes the phase key (an all-defaults row).
    func writeProjectRoutingPhase(_ key: String, _ route: PhaseRoute?,
                                  for project: Project) {
        var current = ModelRouting.load(from: projectRoutingURL(project))
        current.phases[key] = route.flatMap { $0.isEmpty ? nil : $0 }
        writeProjectRouting(current, for: project)
    }

    /// One agent's chain in a project's routing file (Fallback Overrides).
    /// An empty chain deletes the key — the project inherits the fleet ladder.
    func writeProjectRoutingChain(_ agent: String, _ steps: [String],
                                  for project: Project) {
        var current = ModelRouting.load(from: projectRoutingURL(project))
        current.chains[agent] = steps.isEmpty ? nil : steps
        writeProjectRouting(current, for: project)
    }

    // MARK: - Library: reusable phase-prompt snippets + saved run profiles
    //
    // Snippets layer fleet -> section -> project by name; phase "" means
    // usable anywhere. Profiles: <engine>/library/profiles/<slug>.json —
    // a model_routing.json-shaped file (per-phase models/effort/rounds/
    // instructions + fallback chains) plus profile_name/workflow keys the
    // engine loader ignores. Applying a profile materializes the project's
    // model_routing.json and workflow.txt, so different apps can carry
    // different requirements with one click.

    var libraryDirURL: URL { orchDirURL.appendingPathComponent("library", isDirectory: true) }
    var snippetsURL: URL { libraryDirURL.appendingPathComponent("snippets.json") }
    var profilesDirURL: URL { libraryDirURL.appendingPathComponent("profiles", isDirectory: true) }
    var pipelinePresetsDirURL: URL {
        Self.appSupportBaseURL.appendingPathComponent(
            "pipeline_presets", isDirectory: true)
    }

    func knownPipelineSections() -> Set<String> {
        let dir = orchDirURL.appendingPathComponent("sections")
        return Set(((try? fm.contentsOfDirectory(atPath: dir.path)) ?? []).filter {
            !$0.hasPrefix("_") && !$0.hasPrefix(".")
                && fm.fileExists(atPath: dir.appendingPathComponent(
                    "\($0)/section.json").path)
        })
    }

    func listPipelinePresets() -> [PipelinePresetRecord] {
        let loaded = PipelinePresetLibrary.load(
            dir: pipelinePresetsDirURL,
            knownSections: knownPipelineSections())
        pipelinePresetWarning = loaded.warning
        if let warning = loaded.warning { runLog += "\(warning)\n" }
        return loaded.records
    }

    @discardableResult
    func savePipelinePreset(_ canvas: PipelineCanvas,
                            replacing: URL? = nil) -> URL? {
        switch PipelineCodec.encode(canvas) {
        case .failure(let error):
            surfaceError(error); return nil
        case .success(let data):
            do {
                let url = try PipelinePresetLibrary.save(
                    data, name: canvas.name, dir: pipelinePresetsDirURL,
                    replacing: replacing)
                runLog += "Saved pipeline preset “\(canvas.name)”.\n"
                return url
            } catch {
                surfaceError("Couldn't save pipeline preset: \(error.localizedDescription)")
                return nil
            }
        }
    }

    func deletePipelinePreset(_ preset: PipelinePresetRecord) {
        do {
            try fm.trashItem(at: preset.url, resultingItemURL: nil)
            runLog += "Moved pipeline preset “\(preset.name)” to the Trash.\n"
        } catch {
            surfaceError("Couldn't move pipeline preset to the Trash: \(error.localizedDescription)")
        }
    }

    @discardableResult
    func runPipeline(_ canvas: PipelineCanvas, presetURL: URL,
                     project: String, idea: String) -> String? {
        let cleanIdea = idea.trimmingCharacters(in: .whitespacesAndNewlines)
        let cleanProject = project.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !cleanIdea.isEmpty, !cleanProject.isEmpty else {
            surfaceError("Run pipeline needs a project name and seed idea.")
            return nil
        }
        if missionControl.conductorRunning
            && !PipelineRunFiles.runningConductorHasRouting(root: rootURL) {
            surfaceError("The running Conductor is observation-only (or its --route mode cannot be verified). Stop it, then run the pipeline so the GUI can launch routing explicitly.")
            return nil
        }
        let manifestURL = orchDirURL.appendingPathComponent(
            "sections/\(canvas.seedSection)/section.json")
        guard let data = try? Data(contentsOf: manifestURL) else {
            surfaceError("seed.section: no readable manifest for '\(canvas.seedSection)'")
            return nil
        }
        // workflow.txt resolves only named fleet workflows. An inline
        // section workflow has a display name but is not addressable by the
        // engine resolver; treating that name as runnable would silently
        // fall through to the wrong default workflow.
        let workflow = PipelineRunFiles.namedWorkflow(fromSectionManifest: data)
        guard let workflow, !workflow.isEmpty else {
            surfaceError("seed.section: '\(canvas.seedSection)' uses an inline workflow that cannot be launched by name")
            return nil
        }
        do {
            let seeded = try PipelineRunFiles.seed(
                root: rootURL, project: cleanProject, canvas: canvas,
                idea: cleanIdea, workflow: workflow)
            try PipelineRunFiles.writeRequest(root: rootURL, presetURL: presetURL)
            if !runController.isRunning(seeded.sessionID) {
                launch(args: ["orchestrator.py", "--root", rootURL.path,
                              "--app", seeded.sessionID],
                       project: seeded.sessionID)
            }
            // A live Conductor consumes the marker on its next ordinary poll;
            // otherwise launch the exact same engine with routing enabled.
            if !missionControl.conductorRunning
                && !runController.isRunning("__conductor__") {
                launch(args: ["conductor.py", "--root", rootURL.path,
                              "--route"], project: "__conductor__")
            }
            runLog += seeded.newlyMinted
                ? "Pipeline “\(canvas.name)” seeded \(seeded.sessionID).\n"
                : "Pipeline seed already exists at \(seeded.sessionID); no duplicate minted.\n"
            refresh()
            return seeded.sessionID
        } catch {
            surfaceError("Couldn't run pipeline: \(error.localizedDescription)")
            return nil
        }
    }

    func loadSnippets(section: String? = nil,
                      projectDir: URL? = nil) -> [PromptSnippet] {
        let sectionURL = section.map {
            orchDirURL.appendingPathComponent("sections/\($0)/snippets.json")
        }
        let result = SnippetLibrary.load(
            fleetURL: snippetsURL, sectionURL: sectionURL,
            projectURL: projectDir?.appendingPathComponent("snippets.json"))
        snippetWarnings = result.warnings
        return result.snippets
    }

    func loadCommands(section: String? = nil,
                      projectDir: URL? = nil) -> [ComposerCommand] {
        let sectionURL = section.map {
            orchDirURL.appendingPathComponent("sections/\($0)/commands.json")
        }
        let result = CommandLibrary.load(
            fleetURL: orchDirURL.appendingPathComponent("commands.json"),
            sectionURL: sectionURL,
            projectURL: projectDir?.appendingPathComponent("commands.json"))
        commandWarnings = result.warnings
        return result.commands
    }

    func saveSnippets(_ snippets: [PromptSnippet],
                      scope: SnippetSaveScope = .fleet) {
        let url: URL
        switch scope {
        case .fleet: url = snippetsURL
        case .section(let section):
            url = orchDirURL.appendingPathComponent("sections/\(section)/snippets.json")
        case .project(let projectDir):
            url = projectDir.appendingPathComponent("snippets.json")
        }
        // A failed write must surface — the editor treats this call as
        // success, so a swallowed error meant edits vanished on relaunch.
        guard SnippetLibrary.save(snippets, to: url) else {
            surfaceError("Couldn't save \(url.lastPathComponent) — the snippet edit is not on disk.")
            return
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
        // Success is only claimed when the write landed — writeJSON surfaces
        // a failure banner; a "Saved" line over a lost file silently reverts.
        if writeJSON(obj, to: url) {
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
        // Both writes must land before "Applied" is claimed — a user creating
        // a project from a profile would otherwise get default routing with
        // no warning. writeJSON / the catch surface the failure banner.
        guard writeJSON(obj, to: routingURL) else {
            objectWillChange.send()
            return
        }
        modelRoutingCache[routingURL] = nil
        if let wf, !wf.isEmpty {
            do {
                try (wf + "\n").write(to: dir.appendingPathComponent("workflow.txt"),
                                      atomically: true, encoding: .utf8)
            } catch {
                surfaceError("Couldn't save workflow.txt for \(name): \(error.localizedDescription)")
                objectWillChange.send()
                return
            }
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
            // Gate BOTH the "Rated" line and the exemplar launch on the write:
            // fleet learning (presort / anti-pattern ledger) reads rating.json,
            // so claiming a rating that never landed makes the fleet silently
            // disagree with what the user was told. writeJSON surfaces failure.
            if writeJSON(obj, to: url) {
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
                for k in APIKeyEnv.strippedAPIKeyVars {
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

    /// For editors that save(to:) a routing URL directly (RoutingGridView's
    /// per-scope Apply — the section scope has no typed writer): drop the TTL
    /// entry so the next read hits disk. Skipping this served pre-save routing
    /// for up to modelRoutingTTL, and a reload-before-mutate writer overlaying
    /// onto that stale copy could silently revert the Apply.
    func invalidateRoutingCache(at url: URL) {
        modelRoutingCache[url] = nil
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

    // MARK: - V3 8.7 onboarding + progressive disclosure

    var onboardingChecklistRows: [BackendChecklistRow] {
        OnboardingProbeLogic.rows(
            cliVersions: cliVersions, cliAvailable: cliAvailable,
            localModels: localModels, cliProbeInFlight: probeAllInFlight,
            doctorProbeInFlight: doctorProbeInFlight,
            doctorProbeCompleted: doctorProbeCompleted,
            capabilities: agentCapabilities)
    }

    func probeOnboardingBackends() {
        probeAgentVersions()
        refreshLocalModels()
    }

    func evaluateOnboarding(projectCount: Int) {
        guard !onboardingEvaluated else { return }
        onboardingEvaluated = true
        if OnboardingPersistence.shouldPresent(
            progress: onboardingProgress, projectCount: projectCount) {
            beginOnboarding()
        } else if projectCount > 0 && onboardingProgress != .complete {
            revealAllSections()
            completeOnboarding()
        }
    }

    func beginOnboarding() {
        if onboardingProgress == .notStarted {
            onboardingProgress = .inProgress(step: 1)
            onboardingStartedAt = Date()
            if let onboardingStartedAt {
                Self.defaults.set(onboardingStartedAt,
                                  forKey: OnboardingPersistence.startedAtKey)
            }
            OnboardingPersistence.save(onboardingProgress, to: Self.defaults)
        }
        showOnboarding = true
    }

    func advanceOnboarding() {
        let next: Int
        if case .inProgress(let step) = onboardingProgress {
            next = min(4, step + 1)
        } else {
            next = 1
        }
        onboardingProgress = .inProgress(step: next)
        OnboardingPersistence.save(onboardingProgress, to: Self.defaults)
    }

    func skipOnboarding() { completeOnboarding() }

    func completeOnboarding() {
        onboardingProgress = .complete
        showOnboarding = false
        onboardingStartedAt = nil
        OnboardingPersistence.save(.complete, to: Self.defaults)
    }

    func revealSection(_ id: String, announce: Bool = true) {
        guard !id.isEmpty, visibleSectionIDs.insert(id).inserted else { return }
        OnboardingPersistence.saveVisibleSections(visibleSectionIDs,
                                                   to: Self.defaults)
        if announce { newlyRevealedSection = id }
    }

    func revealAllSections() {
        if case .populated(let metas) = sectionRail {
            for meta in metas { revealSection(meta.id, announce: false) }
        }
        newlyRevealedSection = nil
    }

    private func revealSectionsFromRoutes(_ events: [String: [EngineEvent]]) {
        let allEvents = events.values.flatMap({ $0 })
        let guideEvents = onboardingStartedAt.map { started in
            allEvents.filter { $0.ts >= started }
        } ?? []
        let kinds = Set(guideEvents.map(\.kind))
        var routedSection: String?
        for event in allEvents where event.kind == "artifact_routed" {
            if let section = SectionDisclosureLogic.routedSection(
                targetSession: event.targetSession, target: event.target) {
                revealSection(section)
                if guideEvents.contains(where: { $0 == event }) {
                    routedSection = section
                }
            }
        }
        if case .inProgress(let step) = onboardingProgress,
           let progress = OnboardingGuideLogic.progressed(
               step: step, eventKinds: kinds, routedSection: routedSection) {
            onboardingProgress = progress
            OnboardingPersistence.save(progress, to: Self.defaults)
            if progress == .complete { showOnboarding = false }
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
                writeConfig(text)
            } else if let runtimeRange = text.range(of: "runtime:\n") {
                // Older engine copies predate recently added runtime keys —
                // insert directly under the section header, matching the
                // "models:\n" insert sites. (This once searched for a literal
                // backslash-n and never fired, so the toggle looked saved
                // while nothing reached disk.)
                text.insert(contentsOf: "  \(key): \(v)\n", at: runtimeRange.upperBound)
                writeConfig(text)
            } else {
                // No key and no runtime: section — be honest instead of
                // writing unchanged text (matches setAgentEnabled).
                surfaceError("Could not find \(key) or a runtime: section in \(configURL.lastPathComponent).")
            }
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
                writeConfig(text)
            } else if let runtimeRange = text.range(of: "runtime:\n") {
                // See setRuntimeInt: real-newline search, or the insert is dead code.
                text.insert(contentsOf: "  \(key): \(value ? "true" : "false")\n",
                            at: runtimeRange.upperBound)
                writeConfig(text)
            } else {
                surfaceError("Could not find \(key) or a runtime: section in \(configURL.lastPathComponent).")
            }
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
            if runController.hasTrackedProcess(name) { stopProject(name) } else { stopRun(name) }
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
        } else if wasRunning {
            runLog += "Stopping \(name), then moving its project to .archive…\n"
            Task { @MainActor [weak self] in
                try? await Task.sleep(nanoseconds: 6_000_000_000)
                self?.archiveProject(project)
            }
        } else {
            archiveProject(project)
        }
        refresh()
    }

    // Engine-owned archive semantics: it validates every flat/nested live lock
    // and performs the intact directory move. The GUI never invents a marker
    // that the engine helper did not authorize.
    func archiveProject(_ project: Project) {
        let slug = ProjectArchivePresentation.projectSlug(for: project.name)
        let py = resolvePython()
        let engine = orchDirURL.appendingPathComponent("orchestrator.py").path
        let root = rootURL.path
        Task.detached { [weak self] in
            let proc = Process()
            let output = Pipe()
            proc.executableURL = URL(fileURLWithPath: py)
            proc.arguments = [engine, "--root", root, "--archive-project", slug]
            proc.standardOutput = output
            proc.standardError = output
            do {
                try proc.run()
                proc.waitUntilExit()
            } catch {
                await MainActor.run { [weak self] in
                    self?.runLog += "Archive failed for \(slug): \(error.localizedDescription)\n"
                }
                return
            }
            let detail = String(data: output.fileHandleForReading.readDataToEndOfFile(),
                                encoding: .utf8)?
                .trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
            await MainActor.run { [weak self] in
                guard let self else { return }
                if proc.terminationStatus == 0 {
                    self.runLog += "Archived \(slug) to workspace/.archive/\(slug). "
                        + "Nothing was deleted.\n"
                } else {
                    self.runLog += "Archive refused for \(slug): \(detail)\n"
                }
                self.refresh()
            }
        }
    }

    // Restore an archived project: delete the marker; it reappears in its
    // status-appropriate sidebar section on the next tick.
    func unarchiveProject(_ project: Project) {
        try? fm.removeItem(at: project.dirURL.appendingPathComponent(".orch_archived"))
        runLog += "Restored \(project.name) from the archive.\n"
        refresh()
    }

    func chatMeta(for project: Project) -> ChatMeta {
        chatMetadata[project.name] ?? ChatMeta()
    }

    func setChatPinned(_ project: Project, pinned: Bool) {
        var meta = chatMeta(for: project)
        meta.pinned = pinned
        writeChatMeta(meta, project: project)
    }

    func setChatTags(_ project: Project, tags: [String]) {
        var meta = chatMeta(for: project)
        meta.tags = tags
        writeChatMeta(meta, project: project)
    }

    private func writeChatMeta(_ meta: ChatMeta, project: Project) {
        let url = ChatMetaDocument.transcriptURL(sessionDir: project.dirURL)
        guard fm.fileExists(atPath: url.path) else {
            surfaceError("Pins and tags become available after the chat writes its first message.")
            return
        }
        do {
            try ChatMetaDocument.write(meta, to: url)
            chatMetadata[project.name] = meta
            chatMetaWarnings[project.name] = nil
            refresh()
        } catch {
            surfaceError("Could not save pins and tags for \(project.name): "
                         + error.localizedDescription)
        }
    }

    func archiveChat(_ project: Project) {
        removeProject(project, deleteFolder: false)
    }

    func restoreArchivedChat(_ chat: ArchivedChat) {
        do {
            try fm.removeItem(at: chat.dirURL.appendingPathComponent(".orch_archived"))
            runLog += "Restored \(chat.id) from the archive.\n"
            refresh()
        } catch {
            surfaceError("Could not restore \(chat.id): \(error.localizedDescription)")
        }
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

    private func sensitivityDirectory(for project: Project) -> URL {
        let parts = project.name.split(separator: "/")
        if parts.count >= 3 {
            return rootURL.appendingPathComponent(String(parts[0]),
                                                   isDirectory: true)
        }
        return project.dirURL
    }

    @discardableResult
    func setProjectPrivate(_ project: Project, enabled: Bool) -> Bool {
        let value = enabled ? "private" : "normal"
        guard ProjectSensitivityFile.write(
            value, to: sensitivityDirectory(for: project)) else {
            surfaceError("Could not update privacy for \(project.name). "
                         + "Its run_config.json may be corrupt or unwritable.")
            return false
        }
        runLog += enabled
            ? "\(project.name) is private — local models only.\n"
            : "\(project.name) privacy disabled; existing private artifacts stay private.\n"
        refresh()
        return true
    }

    func privateRosterConflict() -> String? {
        let localEnabled = enabledAgents["ollama"] ?? false
        let installed = (localModels?.registry ?? []).filter(\.installed)
        return ProjectSensitivityFile.conflict(
            localEnabled: localEnabled, installedLocalCount: installed.count)
    }

    // Writes <project>/run_config.json, which the engine reads (completeness picks
    // the phase subset; stop_after_phase truncates; autonomy sets the mode).
    // Read-merge like the file's OTHER writers (ProjectSensitivityFile.write,
    // SituationApplyService.confirm): run_config.json also carries the engine-
    // read "sensitivity" privacy floor and "situation", so a wholesale rewrite
    // on an existing project would silently strip a user's private flag. A
    // default value DELETES its key, so reverting to defaults actually clears
    // an earlier write instead of being skipped.
    func writeRunConfig(project name: String, autonomy: String,
                        completeness: String, stopAfter: String) {
        let url = rootURL.appendingPathComponent(name).appendingPathComponent("run_config.json")
        var cfg: [String: Any] = [:]
        if let data = try? Data(contentsOf: url),
           let existing = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any] {
            cfg = existing
        }
        if !autonomy.isEmpty && autonomy != "fully_autonomous" { cfg["autonomy"] = autonomy }
        else { cfg.removeValue(forKey: "autonomy") }
        if !completeness.isEmpty { cfg["completeness"] = completeness }
        else { cfg.removeValue(forKey: "completeness") }
        if !stopAfter.isEmpty { cfg["stop_after_phase"] = stopAfter }
        else { cfg.removeValue(forKey: "stop_after_phase") }
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
        var promotedFromEnroll = false

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
            promotedFromEnroll = obj["promoted_from_enroll"] is [String: Any]
            let done = (obj["done"] as? Bool) ?? false
            status = ProjectStatus.decode(engineValue: obj["status"] as? String,
                                          error: error, done: done)
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
        if let stopAt = runController.manualStops[name] {
            if let m = stateMTime, m.timeIntervalSince(stopAt) > 10 {
                runController.clearManualStop(name)
            } else if !runController.isRunning(name) {
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
        proj.hasFinalComplianceReport = EnrollmentEvidence
            .hasFinalComplianceReport(projectDir: dir)
        proj.enrolled = resolvedName == "enroll" || promotedFromEnroll
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
    private nonisolated static let transcriptReadLimitBytes = 1_500_000
    private nonisolated static let transcriptHeadReadBytes = 256_000

    typealias StreamTailCache = SessionModel.StreamTailCache

    nonisolated static func shouldReadStream(focusedPane: String?, project: String,
                                             running: Bool, supportsStreams: Bool) -> Bool {
        focusedPane == project && running && supportsStreams
    }

    /// Incrementally tail the one stream belonging to `agent`. The focus guard
    /// executes BEFORE Task.detached and therefore before any directory stat or
    /// open: background panes and the fleet refresh scan never touch .stream.
    func streamPreview(for project: Project, agent: String) async -> StreamPreview? {
        let model = sessionModel(for: project.name)
        let supports = DS.identity(agent).streams
        guard Self.shouldReadStream(focusedPane: focusedLivePane,
                                    project: project.name,
                                    running: project.running,
                                    supportsStreams: supports) else {
            model.streamTailCache = nil
            return nil
        }
        let prior = model.streamTailCache
        let dir = project.dirURL.appendingPathComponent(".stream", isDirectory: true)
        let (next, preview) = await Task.detached(priority: .utility) {
            Self.readStreamTail(in: dir, agent: agent, prior: prior)
        }.value
        // Focus may have moved while the detached read was in flight. Never
        // publish a preview into a now-background pane.
        guard Self.shouldReadStream(focusedPane: focusedLivePane,
                                    project: project.name,
                                    running: project.running,
                                    supportsStreams: supports) else {
            model.streamTailCache = nil
            return nil
        }
        model.streamTailCache = next
        return preview
    }

    nonisolated static func readStreamTail(
        in dir: URL, agent: String, prior: StreamTailCache?
    ) -> (StreamTailCache?, StreamPreview?) {
        let fm = FileManager.default
        let keys: Set<URLResourceKey> = [.isRegularFileKey, .contentModificationDateKey,
                                         .fileSizeKey]
        guard let files = try? fm.contentsOfDirectory(at: dir,
                                                       includingPropertiesForKeys: Array(keys),
                                                       options: [.skipsHiddenFiles]) else {
            return (nil, nil)
        }
        let suffix = ":\(agent):turn"
        let candidates: [(URL, String, Date, UInt64)] = files.compactMap { url in
            guard url.pathExtension == "ndjson" else { return nil }
            let encoded = url.deletingPathExtension().lastPathComponent
            guard let tid = encoded.removingPercentEncoding, tid.hasSuffix(suffix),
                  let values = try? url.resourceValues(forKeys: keys),
                  values.isRegularFile == true else { return nil }
            return (url, tid, values.contentModificationDate ?? .distantPast,
                    UInt64(max(values.fileSize ?? 0, 0)))
        }
        guard let selected = candidates.max(by: {
            if $0.2 != $1.2 { return $0.2 < $1.2 }
            return $0.0.lastPathComponent < $1.0.lastPathComponent
        }) else { return (nil, nil) }

        var cache = prior
        if cache?.path != selected.0.path || cache?.agent != agent
            || selected.3 < (cache?.offset ?? 0) {
            cache = StreamTailCache(path: selected.0.path, turnID: selected.1,
                                    agent: agent, offset: 0, remainder: Data(),
                                    text: "", mtime: .distantPast, lastSeq: 0)
        }
        guard var cache else { return (nil, nil) }
        if cache.offset == selected.3 && cache.mtime == selected.2 {
            return (cache, StreamPreview(agent: agent, turnID: cache.turnID,
                                         text: cache.text))
        }
        guard let handle = try? FileHandle(forReadingFrom: selected.0) else {
            return (nil, nil)
        }
        defer { try? handle.close() }
        do { try handle.seek(toOffset: cache.offset) } catch { return (nil, nil) }
        let data = handle.readDataToEndOfFile()
        cache.offset += UInt64(data.count)
        cache.mtime = selected.2
        var joined = cache.remainder
        joined.append(data)
        let lines = joined.split(separator: 0x0A, omittingEmptySubsequences: false)
        cache.remainder = Data(lines.last ?? Data.SubSequence())
        for rawLine in lines.dropLast() where !rawLine.isEmpty {
            let line = Data(rawLine)
            guard let obj = try? JSONSerialization.jsonObject(with: line) as? [String: Any],
                  let seq = obj["seq"] as? Int, seq > cache.lastSeq,
                  let delta = obj["delta"] as? String else { continue }
            cache.lastSeq = seq
            cache.text += delta
        }
        return (cache, StreamPreview(agent: agent, turnID: cache.turnID,
                                     text: cache.text))
    }

    func transcript(for project: Project, phaseKey: String) async -> PhaseTranscript {
        let model = sessionModel(for: project.name)
        guard let def = phases(for: project).first(where: { $0.key == phaseKey })
                ?? ALL_PHASES.first(where: { $0.key == phaseKey }) else {
            return PhaseTranscript()
        }
        let url = project.dirURL.appendingPathComponent(def.folder).appendingPathComponent(def.file)
        let cachedFingerprint = model.transcriptCache[url.path]?.fingerprint
        let (fingerprint, fresh) = await Task.detached(priority: .utility) {
            Self.readAndParseTranscript(
                at: url, ifFingerprintDiffersFrom: cachedFingerprint)
        }.value
        if let fresh {
            model.transcriptCache[url.path] = (fingerprint, fresh)
        }
        return model.transcriptCache[url.path]?.value ?? PhaseTranscript()
    }

    // fresh == nil means both mtime and size are unchanged. The optional hook
    // instruments actual read/parse work in tests; stat-only cache hits never
    // call it.
    nonisolated static func readAndParseTranscript(
        at url: URL, ifFingerprintDiffersFrom cached: FileFingerprint?,
        onParse: (() -> Void)? = nil
    ) -> (fingerprint: FileFingerprint, fresh: PhaseTranscript?) {
        let fingerprint = FileFingerprint.read(url)
        if let cached, cached == fingerprint { return (fingerprint, nil) }
        onParse?()
        guard let text = readTranscriptText(at: url) else {
            var empty = PhaseTranscript()
            empty.exists = false
            return (fingerprint, empty)
        }
        return (fingerprint, TranscriptParser.parse(text))
    }

    // Compatibility seam for the 6.3 streaming regression test. New callers
    // use the two-field fingerprint overload above.
    nonisolated static func readAndParseTranscript(
        at url: URL, ifChangedSince cachedMtime: Date?
    ) -> (mtime: Date, fresh: PhaseTranscript?) {
        let fingerprint = FileFingerprint.read(url)
        let mtime = fingerprint.mtime
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
        sessionModels[project.name]?.buildWorkers
    }

    // The live activity log under the parallel-build banner: the last few
    // turn_started/turn_completed/agent_fallback events for THIS phase+round,
    // newest first — reuses eventsByProject (already tailed+parsed by the
    // scanner on its existing polling cadence), so this is a filter, not a
    // new reader. Scoped to the round so switching phases/rounds doesn't
    // show stale activity from an earlier one. The actual filter is a free
    // function (below) so it's testable without a store/project fixture.
    func recentBuildActivity(for project: Project, phase: String, round: Int,
                             limit: Int = 6) -> [EngineEvent] {
        filterBuildActivity(eventsByProject[project.name] ?? [],
                            phase: phase, round: round, limit: limit)
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
            // .atomic: the engine reads these files mid-run — a torn
            // half-written JSON is worse than a stale one.
            try data.write(to: url, options: .atomic)
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
        sessionModel(for: currentChatKey).chatMessages = chatMessages
    }

    // Switch the visible conversation to another history key: persist the
    // outgoing chat's draft, swap keys, restore the incoming chat's state.
    // (No UI caller mints non-"home" keys in M1 — engine chat sessions render
    // transcripts, not this store — but the isolation contract is load-bearing
    // and test-proven now so later keys can't cross-write histories.)
    func switchChat(to key: String) {
        guard key != currentChatKey else { return }
        sessionModel(for: currentChatKey).chatInput = chatInput
        currentChatKey = key
        let model = sessionModel(for: key)
        chatInput = model.chatInput
        chatThinking = model.chatThinking
        loadChatHistory()
    }

    // Concierge bookkeeping, keyed so a reply that lands after a chat switch
    // is delivered to the chat that asked — never the newly-focused one.
    func setChatThinking(_ thinking: Bool, for key: String) {
        sessionModel(for: key).chatThinking = thinking
        if key == currentChatKey { chatThinking = thinking }
    }

    func deliverConciergeReply(_ message: ConciergeMessage, to key: String) {
        setChatThinking(false, for: key)
        if key == currentChatKey {
            chatMessages.append(message)   // didSet persists under this key
        } else {
            do {
                try chatHistory.append(message, key: key)
                sessionModel(for: key).chatMessages.append(message)
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
        runController.canStop(name)
    }

    func stopProject(_ name: String) {
        runController.stopOwned(
            name, rootURL: rootURL,
            legacyLockURL: orchDirURL.appendingPathComponent("locks/\(name).lock"),
            hooks: runHooks())
    }

    private func launch(args: [String], project: String? = nil) {
        guard engineAvailable else {
            runLog += "Cannot launch — \(engineMissingMessage)\n"
            return
        }
        runController.launch(
            python: resolvePython(), script: orchDirURL.appendingPathComponent(args[0]),
            arguments: Array(args.dropFirst()), rootURL: rootURL,
            project: project, hooks: runHooks())
    }

    private func runHooks() -> RunController.Hooks {
        RunController.Hooks(
            appendLog: { [weak self] text in
                guard let self else { return }
                self.runLog = RunLogBuffer.trim(self.runLog + text)
            },
            surfaceError: { [weak self] message in self?.surfaceError(message) },
            refresh: { [weak self] in self?.refresh() },
            terminated: { [weak self] name, status, uncaught in
                self?.noteChatTermination(name: name, status: status,
                                          uncaughtSignal: uncaught)
            })
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
    func stopRun(_ name: String) {
        if canStop(name) {
            stopProject(name)
            return
        }
        let lockURL = SessionLayout.lockURL(rootURL: rootURL, id: name)
        // Lock payload's pid first; else the engine's belt-and-braces
        // run.pid (V3 7.0) — a foreign run whose lock is unreadable or
        // damaged is still stoppable. The liveness check below applies to
        // whichever source supplied the pid.
        let lockPid = appLocks[name]?.pid ?? 0
        let pid = lockPid > 0 ? lockPid
            : (FactoryScanner.readRunPid(rootURL: rootURL, id: name) ?? 0)
        runController.stopExternal(name, pid: pid, lockURL: lockURL,
                                   hooks: runHooks())
    }

    // V3 7.6: write only the file contract, then wait for the next scan to
    // read it back. No optimistic assignment: the segmented control always
    // shows the dial the Conductor can actually read from disk.
    func setConductorOversight(_ dial: OversightDial) {
        if let error = ConductorControlFiles.writeDial(
            rootURL: rootURL, dial: dial) {
            surfaceError(error)
            return
        }
        refresh()
    }

    func decideConductorRoute(_ route: ConductorPendingRoute,
                              suffix: String, body: String = "") {
        if let error = ConductorControlFiles.writeDecision(
            rootURL: rootURL, routeID: route.routeID,
            suffix: suffix, body: body) {
            surfaceError(error)
            return
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
