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
        for name in names {
            let url = rootURL.appendingPathComponent(name)
                .appendingPathComponent("events.jsonl")
            guard let attrs = try? fm.attributesOfItem(atPath: url.path),
                  let mtime = attrs[.modificationDate] as? Date,
                  let size = (attrs[.size] as? NSNumber)?.intValue else {
                cache[name] = nil
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
