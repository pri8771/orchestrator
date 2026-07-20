import Foundation

// MARK: - Engine events (DESIGN-NATIVE-PRO.md §6, milestone M4)
//
// The engine appends structured events to <app>/events.jsonl (events.py):
// run/phase/turn transitions, agent_fallback rescues, verify results, agent
// auto-disables. This file is the GUI-side reader + derived run-health state:
// a tolerant line parser, a bounded tail reader (mtime/size-cached by the
// scanner), per-agent runtime state (planned→actual), feed aggregation, and
// the fleet-health rollup behind the toolbar capsule + fallback bell.

struct EngineEvent: Equatable, Identifiable {
    var id: Int                 // line index within the read tail (stable per read)
    var ts: Date
    var kind: String            // events.py KINDS
    var project = ""
    var phase = ""
    var section = ""
    var round = 0
    var agent = ""
    var ok: Bool?
    var exitCode: Int?
    var modelRequested = ""
    var modelUsed = ""
    var reason = ""
    var detail = ""
    var status = ""             // run_finished status / fallback step status
    var fromModel = ""
    var toModel = ""
    var outputLen: Int?
    var dur: Double?
    var artifactID = ""
    var artifactType = ""
    var artifactVersion: Int?
    var artifactPath = ""
    var target = ""
    var targetSession = ""
    var session = ""
    var routeID = ""
    var budget = ""
    var measurement = ""
    var provider = ""

    var isFallback: Bool { kind == "agent_fallback" }
    var isError: Bool {
        (kind == "turn_completed" && ok == false)
            || (kind == "run_finished" && status != "done")
            || kind == "agent_disabled"
            || (kind == "verify_result" && status.lowercased().contains("fail"))
    }
    var isPhase: Bool { kind == "phase_started" || kind == "phase_completed" }

    // One-line human rendering for feed rows and the bell ledger.
    var headline: String {
        switch kind {
        case "agent_fallback":
            let step = toModel.isEmpty ? "fallback" : toModel
            switch status {
            case "rescued": return "\(agentName) \(fromModel) unavailable → rescued by \(step)"
            case "skipped": return "\(agentName) fallback \(step) skipped — \(reason)"
            case "attempt": return "\(agentName) \(fromModel) failed → trying \(step)"
            case "exhausted": return "\(agentName) fallback ladder exhausted"
            case "failed": return "\(agentName) fallback \(step) failed"
            default: return "\(agentName) fallback \(step) \(status)"
            }
        case "turn_completed":
            if ok == false {
                return "\(agentName) turn failed — \(reason.isEmpty ? "error" : reason)"
            }
            let len = outputLen.map { " · \($0) chars" } ?? ""
            return "\(agentName) responded\(len)"
        case "turn_started": return "\(agentName) turn started"
        case "phase_started": return "Phase \(prettyPhase) started"
        case "phase_completed": return "Phase \(prettyPhase) completed"
        case "run_started": return "Run started"
        case "run_finished": return "Run finished — \(status.isEmpty ? "?" : status)"
        case "verify_result": return "Verify: \(status)\(detail.isEmpty ? "" : " — \(detail)")"
        case "agent_disabled": return "\(agentName) disabled — \(reason)"
        case "approval_needed": return "Approval needed for \(target) — \(reason)"
        case "stalled": return "\(prettyPhase) stalled — \(reason)"
        case "converged": return "\(prettyPhase) converged — \(reason)"
        case "budget_exhausted": return "Budget exhausted — \(measurement)"
        default: return kind
        }
    }

    var agentName: String { DS.identity(agent).displayName }
    var prettyPhase: String {
        phase.replacingOccurrences(of: "_", with: " ").capitalized
    }

    // Aggregation key: identical consecutive events fold into one "× N" row.
    var aggregationKey: String {
        [kind, agent, phase, reason, status, fromModel, toModel].joined(separator: "|")
    }

    static let tsFormatter: ISO8601DateFormatter = {
        let f = ISO8601DateFormatter()
        f.formatOptions = [.withInternetDateTime]
        return f
    }()

    static func parse(line: String, id: Int) -> EngineEvent? {
        guard let data = line.data(using: .utf8),
              let obj = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any],
              let kind = obj["kind"] as? String else { return nil }
        let tsRaw = (obj["ts"] as? String) ?? ""
        // events.py writes isoformat(timespec="seconds") — no zone suffix.
        let ts = tsFormatter.date(from: tsRaw)
            ?? tsFormatter.date(from: tsRaw + "Z").map {
                // Local naive timestamp parsed as UTC: shift back to local.
                $0.addingTimeInterval(-Double(TimeZone.current.secondsFromGMT()))
            }
            ?? .distantPast
        var e = EngineEvent(id: id, ts: ts, kind: kind)
        e.project = (obj["project"] as? String) ?? ""
        e.phase = (obj["phase"] as? String) ?? ""
        e.section = (obj["section"] as? String) ?? ""
        e.round = (obj["round"] as? Int) ?? 0
        e.agent = (obj["agent"] as? String) ?? ""
        e.ok = obj["ok"] as? Bool
        e.exitCode = obj["exit"] as? Int
        e.modelRequested = (obj["model_requested"] as? String) ?? ""
        e.modelUsed = (obj["model_used"] as? String) ?? ""
        e.reason = (obj["reason"] as? String) ?? ""
        e.detail = (obj["detail"] as? String) ?? ""
        e.status = (obj["status"] as? String) ?? ""
        e.fromModel = (obj["from_model"] as? String) ?? ""
        e.toModel = (obj["to_model"] as? String) ?? ""
        e.outputLen = obj["output_len"] as? Int
        e.dur = obj["dur"] as? Double
        e.artifactID = (obj["artifact_id"] as? String) ?? ""
        e.artifactType = (obj["type"] as? String) ?? ""
        e.artifactVersion = obj["version"] as? Int
        e.artifactPath = (obj["path"] as? String) ?? ""
        e.target = (obj["target"] as? String) ?? ""
        e.targetSession = (obj["target_session"] as? String) ?? ""
        e.session = (obj["session"] as? String) ?? ""
        e.routeID = (obj["route_id"] as? String) ?? ""
        e.budget = (obj["budget"] as? String) ?? ""
        e.measurement = (obj["measurement"] as? String) ?? ""
        e.provider = (obj["provider"] as? String) ?? ""
        return e
    }

    // Parse the tail of an events.jsonl text blob (last partial line skipped
    // by starting after the first newline when truncated upstream).
    static func parse(text: String) -> [EngineEvent] {
        var out: [EngineEvent] = []
        var idx = 0
        for raw in text.split(separator: "\n", omittingEmptySubsequences: true) {
            if let e = parse(line: String(raw), id: idx) { out.append(e) }
            idx += 1
        }
        return out
    }
}

// MARK: - Conductor notification requests (V3 7.8)

struct ConductorNotificationRequest: Codable, Equatable, Identifiable {
    let id: String
    let kind: String
    let project: String
    let section: String
    let title: String
    let body: String
}

struct QuietHours: Equatable {
    var enabled: Bool
    var startMinute: Int
    var endMinute: Int

    func contains(_ date: Date, calendar: Calendar = .current) -> Bool {
        guard enabled else { return false }
        let parts = calendar.dateComponents([.hour, .minute], from: date)
        let minute = (parts.hour ?? 0) * 60 + (parts.minute ?? 0)
        if startMinute == endMinute { return true }
        if startMinute < endMinute {
            return minute >= startMinute && minute < endMinute
        }
        return minute >= startMinute || minute < endMinute
    }
}

final class ConductorNotificationCoordinator {
    private let defaults: UserDefaults
    private let namespace: String
    private var queued: [ConductorNotificationRequest]

    init(defaults: UserDefaults, namespace: String) {
        self.defaults = defaults
        self.namespace = namespace
        if let data = defaults.data(forKey: "conductor.notifications.queue.\(namespace)"),
           let decoded = try? JSONDecoder().decode(
                [ConductorNotificationRequest].self, from: data) {
            queued = decoded
        } else {
            queued = []
        }
    }

    private func key(_ suffix: String, project: String) -> String {
        "conductor.notifications.\(suffix).\(namespace).\(project)"
    }

    private func stableKey(_ event: EngineEvent) -> String {
        [event.kind, event.routeID, event.session,
         String(Int(event.ts.timeIntervalSince1970)), event.reason,
         event.budget].joined(separator: "|")
    }

    private func request(_ event: EngineEvent, source: String) -> ConductorNotificationRequest? {
        let project = event.project.isEmpty
            ? (source == ".conductor" ? "Workspace" : source) : event.project
        let section = event.phase.isEmpty ? event.sectionName : event.phase
        let whereText = section.isEmpty || section == "all"
            ? project : "\(project) / \(section)"
        let body: String
        let title: String
        switch event.kind {
        case "approval_needed":
            guard !event.routeID.isEmpty, !event.target.isEmpty else { return nil }
            title = "Approval needed"
            body = "\(whereText): route \(event.routeID) to \(event.target) — \(event.reason)."
        case "stalled":
            title = "Section stalled"
            body = "\(whereText): \(event.reason)."
        case "converged":
            title = "Section converged"
            body = "\(whereText): \(event.reason)."
        case "budget_exhausted":
            guard !event.budget.isEmpty else { return nil }
            title = "Budget exhausted"
            let measure = event.measurement.isEmpty ? event.reason : event.measurement
            body = "\(whereText): \(event.budget.replacingOccurrences(of: "_", with: " ")) — \(measure)."
        default:
            return nil
        }
        return ConductorNotificationRequest(
            id: stableKey(event), kind: event.kind, project: project,
            section: section, title: title, body: body)
    }

    private func persistQueue() {
        let key = "conductor.notifications.queue.\(namespace)"
        if queued.isEmpty {
            defaults.removeObject(forKey: key)
        } else if let data = try? JSONEncoder().encode(queued) {
            defaults.set(data, forKey: key)
        }
    }

    private func summary() -> ConductorNotificationRequest? {
        guard !queued.isEmpty else { return nil }
        let counts = Dictionary(grouping: queued, by: \.kind).mapValues(\.count)
        var pieces: [String] = []
        if let n = counts["approval_needed"] { pieces.append("\(n) approval\(n == 1 ? "" : "s") waiting") }
        if let n = counts["stalled"] { pieces.append("\(n) stalled section\(n == 1 ? "" : "s")") }
        if let n = counts["converged"] { pieces.append("\(n) section\(n == 1 ? "" : "s") converged") }
        if let n = counts["budget_exhausted"] { pieces.append("\(n) budget limit\(n == 1 ? "" : "s") reached") }
        return ConductorNotificationRequest(
            id: "quiet-summary-\(queued.map(\.id).sorted().joined(separator: ","))",
            kind: "quiet_summary", project: "Workspace", section: "",
            title: "Overnight activity", body: pieces.joined(separator: ", ") + ".")
    }

    func process(eventsByProject: [String: [EngineEvent]], quietHours: QuietHours,
                 now: Date = Date()) -> [ConductorNotificationRequest] {
        let quiet = quietHours.contains(now)
        var output: [ConductorNotificationRequest] = []
        if !quiet, let summary = summary() {
            output.append(summary)
            queued = []
            persistQueue()
        }
        for source in eventsByProject.keys.sorted() {
            let relevant = (eventsByProject[source] ?? []).filter {
                ["approval_needed", "stalled", "converged", "budget_exhausted"]
                    .contains($0.kind)
            }
            let seenKey = key("seen", project: source)
            let cursorKey = key("cursor", project: source)
            guard defaults.bool(forKey: seenKey) else {
                defaults.set(true, forKey: seenKey)
                defaults.set(relevant.last.map(stableKey) ?? "", forKey: cursorKey)
                continue
            }
            let cursor = defaults.string(forKey: cursorKey) ?? ""
            let fresh: ArraySlice<EngineEvent>
            if cursor.isEmpty {
                fresh = relevant[...]
            } else if let index = relevant.lastIndex(where: { stableKey($0) == cursor }) {
                fresh = relevant.suffix(from: relevant.index(after: index))
            } else {
                // The bounded tail no longer contains the cursor. Re-baseline;
                // replaying an ambiguous history is worse than one missed ping.
                defaults.set(relevant.last.map(stableKey) ?? "", forKey: cursorKey)
                continue
            }
            for event in fresh {
                guard let item = request(event, source: source) else { continue }
                if quiet {
                    if !queued.contains(where: { $0.id == item.id }) { queued.append(item) }
                } else {
                    output.append(item)
                }
            }
            if let last = relevant.last { defaults.set(stableKey(last), forKey: cursorKey) }
        }
        if quiet { persistQueue() }
        return output
    }
}

private extension EngineEvent {
    var sectionName: String {
        if !section.isEmpty { return section }
        if !phase.isEmpty { return phase }
        let parts = session.split(separator: "/")
        return parts.count > 1 ? String(parts[1]) : ""
    }
}

// MARK: - Aggregation ("× 133" rows, graft: Conductor)

struct AggregatedEvent: Identifiable, Equatable {
    var event: EngineEvent      // the LAST event of the streak
    var count: Int
    var id: Int { event.id }
}

enum EventAggregator {
    static func collapse(_ events: [EngineEvent]) -> [AggregatedEvent] {
        var out: [AggregatedEvent] = []
        for e in events {
            if var last = out.last, last.event.aggregationKey == e.aggregationKey {
                last.event = e
                last.count += 1
                out[out.count - 1] = last
            } else {
                out.append(AggregatedEvent(event: e, count: 1))
            }
        }
        return out
    }
}

// MARK: - Per-agent runtime state (planned → actual; §4.2 Agent Board)

struct AgentRuntimeState: Identifiable, Equatable {
    enum Phase: Equatable {
        case running(since: Date)
        case fallback(to: String)
        case failed(reason: String)
        case waiting
        case idle
    }
    var agent: String
    var plannedModel = ""
    var actualModel = ""        // last model that actually produced output
    var state: Phase = .idle
    var recentOutputLens: [Double] = []   // newest last, for the sparkline
    var consecutiveFallbacks = 0
    var lastTurnDur: Double?
    var id: String { agent }

    var degraded: Bool {
        if case .fallback = state { return true }
        return !actualModel.isEmpty && !plannedModel.isEmpty && actualModel != plannedModel
    }
}

enum RunHealthDeriver {
    // Fold a project's event stream into per-agent board state. `running`
    // gates the live states (a dead run shows idle, not a stale "Running").
    static func agentStates(events: [EngineEvent], projectRunning: Bool) -> [AgentRuntimeState] {
        var byAgent: [String: AgentRuntimeState] = [:]
        var order: [String] = []
        // Only the CURRENT run's events should drive live state.
        let runStart = events.lastIndex { $0.kind == "run_started" } ?? events.startIndex
        for e in events[runStart...] {
            guard !e.agent.isEmpty else { continue }
            let key = DS.identity(e.agent).key
            var s = byAgent[key] ?? AgentRuntimeState(agent: key)
            if byAgent[key] == nil { order.append(key) }
            switch e.kind {
            case "turn_started":
                s.plannedModel = e.modelRequested
                s.state = .running(since: e.ts)
            case "turn_completed":
                s.lastTurnDur = e.dur
                if e.ok == true {
                    s.actualModel = e.modelUsed.isEmpty ? e.modelRequested : e.modelUsed
                    s.state = .waiting
                    s.consecutiveFallbacks = 0
                    if let len = e.outputLen {
                        s.recentOutputLens.append(Double(len))
                        if s.recentOutputLens.count > 5 { s.recentOutputLens.removeFirst() }
                    }
                } else {
                    s.state = .failed(reason: e.reason)
                }
            case "agent_fallback":
                switch e.status {
                case "rescued":
                    s.actualModel = e.toModel
                    s.state = .fallback(to: e.toModel)
                    s.consecutiveFallbacks += 1
                case "attempt":
                    s.state = .fallback(to: e.toModel)
                case "exhausted", "failed":
                    s.state = .failed(reason: "fallback \(e.status)")
                default: break
                }
            case "agent_disabled":
                s.state = .failed(reason: e.reason.isEmpty ? "disabled" : e.reason)
            default: break
            }
            byAgent[key] = s
        }
        if !projectRunning {
            for key in byAgent.keys {
                if case .running = byAgent[key]!.state { byAgent[key]!.state = .idle }
                if case .waiting = byAgent[key]!.state { byAgent[key]!.state = .idle }
            }
        }
        return order.compactMap { byAgent[$0] }
    }

    // Output floors (§4.2): reviews < 200 chars, specs < 500, else < 40.
    static func shortOutputFloor(phase: String) -> Int {
        let p = phase.lowercased()
        if p.contains("review") || p.contains("verif") || p.contains("audit") { return 200 }
        if p.contains("spec") || p.contains("brief") || p.contains("plan") { return 500 }
        return 40
    }
}

// MARK: - Per-run history + IntegrityStrip segments (§4.4)

struct RunRecord: Identifiable, Equatable {
    var id: Int                 // index of run_started event
    var started: Date
    var finished: Date?
    var status = ""             // done / failed / … ("" = still running / cut off)
    var fallbackCount = 0
    var events: [EngineEvent] = []

    var duration: TimeInterval? { finished.map { $0.timeIntervalSince(started) } }
}

struct IntegritySegment: Identifiable, Equatable {
    var id: Int
    var agent: String
    var rescuedBy: String       // "" = primary call; else the rescue model
    var isRescue: Bool { !rescuedBy.isEmpty }
}

enum RunHistoryDeriver {
    static func runs(events: [EngineEvent]) -> [RunRecord] {
        var out: [RunRecord] = []
        var current: RunRecord?
        for e in events {
            switch e.kind {
            case "run_started":
                if let c = current { out.append(c) }
                current = RunRecord(id: e.id, started: e.ts)
            case "run_finished":
                if current == nil { current = RunRecord(id: e.id, started: e.ts) }
                current?.finished = e.ts
                current?.status = e.status
                current?.events.append(e)
                out.append(current!)
                current = nil
            default:
                if current != nil {
                    current?.events.append(e)
                    if e.isFallback && e.status == "rescued" { current?.fallbackCount += 1 }
                }
            }
        }
        if let c = current { out.append(c) }
        return out.reversed()   // newest first
    }

    // One thin bar per phase, one segment per completed turn, colored by who
    // ACTUALLY did the work; rescued turns carry the rescue model's name.
    static func integritySegments(run: RunRecord, phase: String) -> [IntegritySegment] {
        var out: [IntegritySegment] = []
        var pendingRescue: [String: String] = [:]   // agent -> rescue model
        for e in run.events where e.phase == phase {
            switch e.kind {
            case "agent_fallback" where e.status == "rescued":
                pendingRescue[DS.identity(e.agent).key] = e.toModel
            case "turn_completed" where e.ok == true:
                let key = DS.identity(e.agent).key
                out.append(IntegritySegment(id: e.id, agent: key,
                                            rescuedBy: pendingRescue[key] ?? ""))
                pendingRescue[key] = nil
            default: break
            }
        }
        return out
    }

    static func phasesInRun(_ run: RunRecord) -> [String] {
        var seen = Set<String>()
        var out: [String] = []
        for e in run.events where !e.phase.isEmpty {
            if seen.insert(e.phase).inserted { out.append(e.phase) }
        }
        return out
    }
}

// MARK: - Fleet rollup (toolbar capsule + fallback bell, §6 levels 1–2)

struct FleetHealthSummary: Equatable {
    var failed: [String] = []          // project names, worst first
    var fallbacksActive: [String] = [] // projects currently degraded
    var stalled: [String] = []
    var fallbacks24h: [EngineEvent] = []   // rescued steps, newest first

    var worst: StatusKind {
        if !failed.isEmpty { return .error }
        if !fallbacksActive.isEmpty { return .fallback }
        if !stalled.isEmpty { return .warning }
        return .success
    }
    var headline: String {
        if !failed.isEmpty { return "\(failed.count) failed" }
        if !fallbacksActive.isEmpty { return "\(fallbacksActive.count) fallback\(fallbacksActive.count == 1 ? "" : "s") active" }
        if !stalled.isEmpty { return "\(stalled.count) stalled" }
        return "All healthy"
    }
}

// Background scanner: stat every project's events.jsonl, re-read only files
// whose (mtime, size) moved, keep a bounded parsed tail per project. Called
// from the store's existing background refresh tick — never the main thread.
enum EventsScanner {
    static let tailBytes = 262_144
    private static let lock = NSLock()
    nonisolated(unsafe) private static var cache:
        [String: (mtime: Date, size: Int, events: [EngineEvent])] = [:]

    static func scan(rootURL: URL, names: [String]) -> [String: [EngineEvent]] {
        let fm = FileManager.default
        var out: [String: [EngineEvent]] = [:]
        lock.lock()
        defer { lock.unlock() }
        let scanNames = names.contains(".conductor") ? names : names + [".conductor"]
        for name in scanNames {
            let url = rootURL.appendingPathComponent(name)
                .appendingPathComponent("events.jsonl")
            guard let attrs = try? fm.attributesOfItem(atPath: url.path),
                  let mtime = attrs[.modificationDate] as? Date,
                  let size = (attrs[.size] as? NSNumber)?.intValue else {
                cache[name] = nil
                if name == ".conductor" { out[name] = [] }
                continue
            }
            if let hit = cache[name], hit.mtime == mtime, hit.size == size {
                out[name] = hit.events
                continue
            }
            guard let text = OrchestratorStore.tailText(url: url, maxBytes: tailBytes) else {
                continue
            }
            let events = EngineEvent.parse(text: text).map { e in
                var e = e
                if e.project.isEmpty { e.project = name }   // ledger jump links
                return e
            }
            cache[name] = (mtime, size, events)
            out[name] = events
        }
        return out
    }

    static func summarize(eventsByProject: [String: [EngineEvent]],
                          runningProjects: Set<String>,
                          failedProjects: Set<String>,
                          now: Date = Date()) -> FleetHealthSummary {
        var s = FleetHealthSummary()
        s.failed = failedProjects.sorted()
        let dayAgo = now.addingTimeInterval(-86_400)
        var rescued: [EngineEvent] = []
        for (name, events) in eventsByProject {
            rescued.append(contentsOf: events.lazy
                .filter { $0.isFallback && $0.status == "rescued" && $0.ts > dayAgo }
                .map { e in
                    var e = e
                    if e.project.isEmpty { e.project = name }   // ledger jump links
                    return e
                })
            guard runningProjects.contains(name) else { continue }
            let states = RunHealthDeriver.agentStates(events: events, projectRunning: true)
            if states.contains(where: \.degraded) { s.fallbacksActive.append(name) }
            // Stall: a live run whose newest event is older than 4 minutes.
            if let last = events.last?.ts, now.timeIntervalSince(last) > 240 {
                s.stalled.append(name)
            }
        }
        s.fallbacksActive.sort()
        s.stalled.sort()
        s.fallbacks24h = rescued.sorted { $0.ts > $1.ts }
        return s
    }
}

// MARK: - Costs (V3 6.4 — <project>/costs.jsonl written by costs.py)

// Aggregated spend for one scope (an agent, or a whole project). Honesty
// invariants mirror costs.py's rollup: metered/unmetered/unpriced are kept
// distinct so a mixed scope can never render as a bare dollar total, and
// unmetered is NEVER shown as $0.00 (the engine has no numbers for it).
struct CostTotals: Equatable {
    var meteredTurns = 0
    var unmeteredTurns = 0
    var unpricedTurns = 0     // metered tokens, but no verified price
    var costMicroUSD = 0

    var isEmpty: Bool { meteredTurns == 0 && unmeteredTurns == 0 }

    // The three honest states: "$X.XX" (fully metered+priced),
    // "≥ $X.XX · N unmetered" (mixed — the true total is at least this),
    // "unmetered" (no real numbers at all). Rounding happens ONLY here.
    var display: String? {
        if isEmpty { return nil }
        let dollars = String(format: "$%.2f", Double(costMicroUSD) / 1_000_000)
        let unknown = unmeteredTurns + unpricedTurns
        if meteredTurns > unpricedTurns && unknown == 0 { return dollars }
        if meteredTurns > unpricedTurns { return "≥ \(dollars) · \(unknown) unmetered" }
        return "unmetered"
    }

    mutating func fold(metered: Bool, cost: Int?) {
        if metered {
            meteredTurns += 1
            if let cost { costMicroUSD += cost } else { unpricedTurns += 1 }
        } else {
            unmeteredTurns += 1
        }
    }
}

struct ProjectCosts: Equatable {
    var total = CostTotals()
    var byAgent: [String: CostTotals] = [:]
}

// Background scanner over each project's costs.jsonl — same shape as
// EventsScanner: static mtime/size cache behind an NSLock, called from the
// store's scan tick, never the main thread. Reads the WHOLE file (records
// are ~150 bytes each; rollups need every line, unlike the events tail) with
// a sanity cap so a pathological file can't stall the tick.
enum CostsScanner {
    static let maxBytes = 4 * 1_048_576
    private static let lock = NSLock()
    nonisolated(unsafe) private static var cache:
        [String: (mtime: Date, size: Int, costs: ProjectCosts)] = [:]

    static func scan(rootURL: URL, names: [String]) -> [String: ProjectCosts] {
        let fm = FileManager.default
        var out: [String: ProjectCosts] = [:]
        lock.lock()
        defer { lock.unlock() }
        for name in names {
            let url = rootURL.appendingPathComponent(name)
                .appendingPathComponent("costs.jsonl")
            guard let attrs = try? fm.attributesOfItem(atPath: url.path),
                  let mtime = attrs[.modificationDate] as? Date,
                  let size = attrs[.size] as? Int else { continue }
            if let hit = cache[name], hit.mtime == mtime, hit.size == size {
                out[name] = hit.costs
                continue
            }
            if size > maxBytes {
                // Append-only file over the cap: it will never shrink, so a
                // hard skip would vanish the meter permanently on exactly the
                // heaviest-spend projects. Serve the last parsed totals
                // (stale floor — still honest: real spend is >= shown) and
                // never re-read; cold starts with no cache show nothing.
                if let hit = cache[name] { out[name] = hit.costs }
                continue
            }
            guard let data = try? Data(contentsOf: url),
                  let text = String(data: data, encoding: .utf8) else { continue }
            var costs = ProjectCosts()
            for line in text.split(separator: "\n") {
                // Malformed/alien lines are skipped, mirroring costs.py's
                // read_records: a corrupt record must not hide the rest.
                guard let obj = (try? JSONSerialization.jsonObject(
                        with: Data(line.utf8))) as? [String: Any] else { continue }
                let metered = (obj["metered"] as? Bool) ?? false
                let cost = obj["cost_micro_usd"] as? Int
                let agent = (obj["agent"] as? String) ?? "?"
                costs.total.fold(metered: metered, cost: cost)
                costs.byAgent[agent, default: CostTotals()]
                    .fold(metered: metered, cost: cost)
            }
            cache[name] = (mtime, size, costs)
            out[name] = costs
        }
        return out
    }
}
