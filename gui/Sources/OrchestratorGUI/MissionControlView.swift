import Foundation
import SwiftUI

struct MissionDecision: Identifiable, Equatable, Sendable {
    let id: String
    let timestamp: Date
    let decision: String
    let session: String
    let artifactID: String
    let target: String
    let ruleID: String
    let reason: String
    let routeID: String

    var sourceSection: String {
        let parts = session.split(separator: "/")
        return parts.count > 1 ? String(parts[1]) : "workspace"
    }

    var title: String {
        switch decision {
        case "route_approved": return "Route fired"
        case "route_recovered": return "Route recovered"
        case "approval_requested": return "Route waiting for approval"
        case "route_denied", "denied": return "Route rejected"
        case "do_not_route_added": return "Do-not-route saved"
        case "kill_session": return "Session stopped"
        case "goal_met": return "Goal met"
        case "converged_open_items": return "Converged with open items"
        case "stalled": return "Session stalled"
        case "budget_exhausted": return "Budget exhausted"
        case "route_deferred": return "Route deferred"
        case "snapshot": return "Workspace snapshot"
        case "snapshot_failed": return "Snapshot failed"
        case "pipeline_loaded": return "Pipeline loaded"
        case "pipeline_load_failed": return "Pipeline refused"
        default: return decision.replacingOccurrences(of: "_", with: " ").capitalized
        }
    }

    var explanation: String {
        var pieces = [session.isEmpty ? "Workspace" : session]
        if !artifactID.isEmpty { pieces.append("artifact \(artifactID)") }
        if !target.isEmpty { pieces.append("to \(target)") }
        if !ruleID.isEmpty { pieces.append("rule \(ruleID)") }
        if !reason.isEmpty { pieces.append(reason) }
        return pieces.joined(separator: " · ")
    }
}

struct MissionSnapshotTag: Identifiable, Equatable, Sendable {
    let name: String
    let timestamp: Date
    let cursor: Int
    var id: String { name }
}

struct MissionNode: Identifiable, Equatable, Sendable {
    let section: String
    let sessions: [String]
    var id: String { section }
}

struct MissionFrame: Equatable, Sendable {
    let decisions: [MissionDecision]
    let nodes: [MissionNode]
    let routes: [MissionDecision]
}

struct MissionBudgetSnapshot: Equatable, Sendable {
    var byProvider: [String: CostTotals] = [:]
    var total = CostTotals()
    var turnsUsed = 0
    var turnsCap: Int?
    var wallClockSeconds = 0.0
    var wallClockCap: Double?
    var providerSpendCaps: [String: Double] = [:]
    var exhaustedReason: String?
    var overQuota: [String] = []

    var hasCaps: Bool {
        turnsCap != nil || wallClockCap != nil || !providerSpendCaps.isEmpty
    }
}

struct MissionControlSnapshot: Equatable, Sendable {
    var available = false
    var conductorRunning = false
    var stage = "not started"
    var decisions: [MissionDecision] = []
    var snapshotTags: [MissionSnapshotTag] = []
    var oversight = ConductorOversightSnapshot()
    var budget = MissionBudgetSnapshot()
    var newlyFiredRouteIDs: Set<String> = []
    var warnings: [String] = []
    var activePipelineName: String?
    var activePipelineLoadedAt: Date?

    var timeRange: ClosedRange<Date>? {
        guard let first = decisions.first?.timestamp,
              let last = decisions.last?.timestamp else { return nil }
        return first...max(first, last)
    }

    func frame(at timestamp: Date? = nil) -> MissionFrame {
        let prefix = timestamp.map { instant in
            decisions.filter { $0.timestamp <= instant }
        } ?? decisions
        var sessionsBySection: [String: Set<String>] = [:]
        for entry in prefix where !entry.session.isEmpty {
            let parts = entry.session.split(separator: "/")
            if parts.count > 1 {
                sessionsBySection[String(parts[1]), default: []]
                    .insert(entry.session)
            }
        }
        let routeDecisions = prefix.filter {
            ["route_approved", "route_recovered", "approval_requested",
             "route_deferred"].contains($0.decision) && !$0.target.isEmpty
        }
        for route in routeDecisions {
            if sessionsBySection[route.target] == nil {
                sessionsBySection[route.target] = []
            }
        }
        let nodes = sessionsBySection.map { key, sessions in
            MissionNode(section: key, sessions: sessions.sorted())
        }.sorted { $0.section < $1.section }
        return MissionFrame(decisions: prefix, nodes: nodes,
                            routes: routeDecisions)
    }

    func actualRoute(for session: String) -> MissionDecision? {
        decisions.reversed().first {
            $0.session == session && !$0.target.isEmpty
                && ["approval_requested", "route_approved", "route_recovered",
                    "route_deferred"].contains($0.decision)
        }
    }
}

enum MissionControlDisk {
    private struct LedgerCache {
        var mtime: Date
        var size: Int
        var offset: UInt64
        var remainder: String
        var completedLines: Int
        var decisions: [MissionDecision]
        var warnings: [String]
    }

    private static let lock = NSLock()
    nonisolated(unsafe) private static var ledgerCache: [String: LedgerCache] = [:]
    nonisolated(unsafe) private static var ledgerBytesRead: [String: Int] = [:]

    nonisolated static func resetLedgerCacheForTests(_ url: URL? = nil) {
        lock.lock()
        defer { lock.unlock() }
        if let url {
            ledgerCache.removeValue(forKey: url.path)
            ledgerBytesRead.removeValue(forKey: url.path)
        } else {
            ledgerCache.removeAll()
            ledgerBytesRead.removeAll()
        }
    }

    nonisolated static func ledgerBytesReadForTests(_ url: URL) -> Int {
        lock.lock()
        defer { lock.unlock() }
        return ledgerBytesRead[url.path, default: 0]
    }

    nonisolated static func scan(
        rootURL: URL, costs: [String: ProjectCosts], now: Date,
        isPidAlive: (Int32) -> Bool = FactoryScanner.pidAlive
    ) -> MissionControlSnapshot {
        let fm = FileManager.default
        let dir = rootURL.appendingPathComponent(".conductor", isDirectory: true)
        guard fm.fileExists(atPath: dir.path) else {
            return MissionControlSnapshot()
        }
        var snapshot = MissionControlSnapshot(available: true)
        snapshot.oversight = ConductorOversightDisk.scan(rootURL: rootURL)
        let stateURL = dir.appendingPathComponent("conductor_state.json")
        var state: [String: Any] = [:]
        if let data = try? Data(contentsOf: stateURL),
           let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
            state = obj
            snapshot.stage = (obj["stage"] as? String) ?? "unknown"
        } else if fm.fileExists(atPath: stateURL.path) {
            snapshot.warnings.append("Conductor state is unreadable; showing the last durable ledger.")
        }
        snapshot.conductorRunning = conductorIsRunning(
            dir: dir, isPidAlive: isPidAlive)
        let ledger = scanLedger(dir.appendingPathComponent(
            "conductor_ledger.jsonl"))
        snapshot.decisions = ledger.decisions
        let confirmedRoutes = Set(ledger.decisions.compactMap { entry in
            ["route_approved", "route_recovered", "route_denied", "denied",
             "do_not_route_added", "kill_session"].contains(entry.decision)
                ? entry.routeID : nil
        })
        snapshot.oversight.pending.removeAll {
            confirmedRoutes.contains($0.routeID)
        }
        snapshot.newlyFiredRouteIDs = Set(ledger.newEntries.compactMap {
            ["route_approved", "route_recovered"].contains($0.decision)
                ? $0.routeID : nil
        })
        snapshot.activePipelineName = ((state["pipeline"] as? [String: Any])?["preset_name"] as? String)
        snapshot.activePipelineLoadedAt = ledger.decisions.reversed().first {
            $0.decision == "pipeline_loaded"
        }?.timestamp
        snapshot.snapshotTags = scanSnapshotTags(rootURL: rootURL)
        snapshot.decisions.append(contentsOf: snapshot.snapshotTags.map { tag in
            MissionDecision(id: "snapshot:\(tag.name)", timestamp: tag.timestamp,
                            decision: "snapshot", session: "", artifactID: "",
                            target: "", ruleID: "", reason: "cursor \(tag.cursor)",
                            routeID: "")
        })
        snapshot.decisions.sort { ($0.timestamp, $0.id) < ($1.timestamp, $1.id) }
        snapshot.budget = budgetSnapshot(
            rootURL: rootURL, state: state, decisions: ledger.decisions,
            costs: costs, now: now)
        snapshot.warnings.append(contentsOf: ledger.warnings)
        return snapshot
    }

    nonisolated private static func conductorIsRunning(
        dir: URL, isPidAlive: (Int32) -> Bool
    ) -> Bool {
        let lockURL = dir.appendingPathComponent("conductor.lock")
        guard let text = try? String(contentsOf: lockURL, encoding: .utf8) else {
            return false
        }
        for token in text.split(whereSeparator: { $0.isWhitespace })
            where token.hasPrefix("pid=") {
            if let pid = Int32(token.dropFirst(4)), pid > 0 {
                return isPidAlive(pid)
            }
        }
        return false
    }

    nonisolated private static func scanLedger(
        _ url: URL
    ) -> (decisions: [MissionDecision], newEntries: [MissionDecision],
          warnings: [String]) {
        let fm = FileManager.default
        guard let attrs = try? fm.attributesOfItem(atPath: url.path),
              let mtime = attrs[.modificationDate] as? Date,
              let size = attrs[.size] as? Int else { return ([], [], []) }
        lock.lock()
        defer { lock.unlock() }
        let key = url.path
        if let hit = ledgerCache[key], hit.mtime == mtime, hit.size == size {
            return (hit.decisions, [], hit.warnings)
        }
        let previous = ledgerCache[key]
        let appendOnly = previous.map { size >= $0.size } ?? false
        let start = appendOnly ? previous?.offset ?? 0 : 0
        var chunk = Data()
        do {
            let handle = try FileHandle(forReadingFrom: url)
            try handle.seek(toOffset: start)
            chunk = try handle.readToEnd() ?? Data()
            try handle.close()
            ledgerBytesRead[key, default: 0] += chunk.count
        } catch {
            return (previous?.decisions ?? [], [],
                    ["Conductor ledger could not be read: \(error.localizedDescription)"])
        }
        let prefix = appendOnly ? previous?.remainder ?? "" : ""
        let text = prefix + (String(data: chunk, encoding: .utf8) ?? "")
        var lines = text.components(separatedBy: "\n")
        let remainder = text.hasSuffix("\n") ? "" : (lines.popLast() ?? "")
        if text.hasSuffix("\n"), lines.last == "" { lines.removeLast() }
        var decisions = appendOnly ? previous?.decisions ?? [] : []
        var parsed: [MissionDecision] = []
        var warnings: [String] = appendOnly ? previous?.warnings ?? [] : []
        let base = appendOnly ? previous?.completedLines ?? 0 : 0
        for (index, line) in lines.enumerated() where !line.isEmpty {
            guard let data = line.data(using: .utf8),
                  let obj = try? JSONSerialization.jsonObject(with: data)
                    as? [String: Any],
                  let decision = decodeDecision(obj, index: base + index) else {
                warnings.append("Skipped unreadable Conductor ledger line \(base + index + 1).")
                continue
            }
            parsed.append(decision)
        }
        decisions.append(contentsOf: parsed)
        ledgerCache[key] = LedgerCache(
            mtime: mtime, size: size, offset: UInt64(size), remainder: remainder,
            completedLines: base + lines.count, decisions: decisions,
            warnings: warnings)
        // Opening Mission Control should not animate history. Only records
        // appended after a prior cached scan count as newly fired.
        return (decisions, previous == nil ? [] : parsed, warnings)
    }

    nonisolated private static func decodeDecision(
        _ obj: [String: Any], index: Int
    ) -> MissionDecision? {
        guard let raw = obj["decision"] as? String, !raw.isEmpty,
              let seconds = (obj["ts"] as? NSNumber)?.doubleValue else { return nil }
        let detail = obj["detail"] as? [String: Any] ?? [:]
        let routeID = obj["route_id"] as? String ?? ""
        let id = routeID.isEmpty ? "line:\(index):\(raw)" : "\(routeID):\(raw):\(index)"
        let reason = (detail["reason"] as? String)
            ?? ((detail["evidence"] as? [String: Any])?["reason"] as? String)
            ?? (detail["preset_name"] as? String).map { name in
                let path = detail["preset_path"] as? String ?? "saved preset"
                return "preset \(name) · \(path)"
            } ?? ""
        return MissionDecision(
            id: id, timestamp: Date(timeIntervalSince1970: seconds), decision: raw,
            session: (obj["session"] as? String)
                ?? (detail["requested_by"] as? String) ?? "",
            artifactID: detail["artifact_id"] as? String ?? "",
            target: detail["target"] as? String ?? "",
            ruleID: detail["rule_id"] as? String ?? "",
            reason: reason, routeID: routeID)
    }

    nonisolated private static func scanSnapshotTags(
        rootURL: URL
    ) -> [MissionSnapshotTag] {
        let refs = rootURL.appendingPathComponent(
            ".git/refs/tags/conductor", isDirectory: true)
        var names = ((try? FileManager.default.contentsOfDirectory(
            atPath: refs.path)) ?? []).map { "conductor/\($0)" }
        if let packed = try? String(contentsOf: rootURL.appendingPathComponent(
            ".git/packed-refs"), encoding: .utf8) {
            names.append(contentsOf: packed.components(separatedBy: "\n")
                .compactMap { line in
                    line.split(separator: " ").last.map(String.init)
                }.filter { $0.hasPrefix("refs/tags/conductor/") }
                .map { String($0.dropFirst("refs/tags/".count)) })
        }
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "en_US_POSIX")
        formatter.timeZone = TimeZone(secondsFromGMT: 0)
        formatter.dateFormat = "yyyyMMdd'T'HHmmss'Z'"
        return Set(names).compactMap { name in
            let leaf = name.replacingOccurrences(of: "conductor/", with: "")
            guard let split = leaf.lastIndex(of: "-"),
                  let cursor = Int(leaf[leaf.index(after: split)...]),
                  let date = formatter.date(from: String(leaf[..<split])) else { return nil }
            return MissionSnapshotTag(name: name, timestamp: date, cursor: cursor)
        }.sorted { $0.timestamp < $1.timestamp }
    }

    nonisolated private static func budgetSnapshot(
        rootURL: URL, state: [String: Any], decisions: [MissionDecision],
        costs: [String: ProjectCosts], now: Date
    ) -> MissionBudgetSnapshot {
        var result = MissionBudgetSnapshot()
        for value in costs.values {
            result.total.merge(value.total)
            for (provider, totals) in value.byProvider {
                result.byProvider[provider, default: CostTotals()].merge(totals)
            }
        }
        result.turnsUsed = decisions.filter {
            ["route_approved", "route_recovered"].contains($0.decision)
        }.count
        if let first = decisions.first?.timestamp {
            result.wallClockSeconds = max(0, now.timeIntervalSince(first))
        }
        let pipeline = state["pipeline"] as? [String: Any]
        let pipelineGoal = pipeline?["goal_manifest"] as? [String: Any]
        var manifest = pipelineGoal ?? [:]
        if manifest.isEmpty,
           let data = try? Data(contentsOf: rootURL.appendingPathComponent(
                "goal_manifest.json")),
           let disk = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
            manifest = disk
        }
        let budgets = manifest["budgets"] as? [String: Any] ?? [:]
        result.turnsCap = budgets["turns"] as? Int
        result.wallClockCap = (budgets["wall_clock_s"] as? NSNumber)?.doubleValue
        if let providers = budgets["per_provider"] as? [String: Any] {
            for (provider, raw) in providers {
                if let caps = raw as? [String: Any],
                   let spend = (caps["spend"] as? NSNumber)?.doubleValue {
                    result.providerSpendCaps[provider] = spend
                }
            }
        }
        result.exhaustedReason = (state["halted"] as? [String: Any])?["reason"] as? String
        result.overQuota = (state["over_quota"] as? [String]) ?? []
        return result
    }
}

struct MissionControlView: View {
    @EnvironmentObject var store: OrchestratorStore
    @State private var replayPosition = 1.0
    @State private var replayPlaying = false

    private var replayDate: Date? {
        guard replayPosition < 1, let range = store.missionControl.timeRange else {
            return nil
        }
        let duration = range.upperBound.timeIntervalSince(range.lowerBound)
        return range.lowerBound.addingTimeInterval(duration * replayPosition)
    }

    private var frame: MissionFrame { store.missionControl.frame(at: replayDate) }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: DS.space.l) {
                header
                if !store.missionControl.available {
                    EmptyStateView(symbol: "point.3.connected.trianglepath.dotted",
                                   title: "No Conductor run yet",
                                   message: "Mission Control will fill from the durable ledger when an autonomous run starts.")
                } else {
                    if !store.missionControl.conductorRunning {
                        InlineBanner(kind: .warning, title: "Conductor not running",
                                     message: "Showing accurate last-known state from disk. No routes are moving now.")
                    }
                    warnings
                    oversight
                    nodeMap
                    replay
                    budgetMeters
                    approvalTray
                    decisionLedger
                }
            }
            .padding(DS.space.l)
        }
        .task(id: replayPlaying) {
            while replayPlaying && replayPosition < 1 {
                try? await Task.sleep(for: .milliseconds(500))
                if Task.isCancelled { return }
                replayPosition = min(1, replayPosition + 0.02)
            }
            if replayPosition >= 1 { replayPlaying = false }
        }
    }

    private var header: some View {
        VStack(alignment: .leading, spacing: DS.space.xs) {
            Text("Mission Control").font(DS.font.title)
            Text("Who is working, what moved, why it moved, and what needs you.")
                .font(DS.font.caption).foregroundStyle(DS.textSecondary)
        }
    }

    @ViewBuilder private var warnings: some View {
        ForEach(store.missionControl.warnings
            + store.missionControl.oversight.warnings, id: \.self) { warning in
            InlineBanner(kind: .warning, title: "Conductor data warning",
                         message: warning)
        }
    }

    private var oversight: some View {
        VStack(alignment: .leading, spacing: DS.space.s) {
            Text("Oversight").font(DS.font.headline)
            Picker("Oversight", selection: Binding(
                get: { store.missionControl.oversight.dial },
                set: { store.setConductorOversight($0) }
            )) {
                ForEach(OversightDial.allCases) { dial in Text(dial.title).tag(dial) }
            }
            .pickerStyle(.segmented)
            .accessibilityHint("The displayed value is read from Conductor state")
        }
    }

    private var nodeMap: some View {
        VStack(alignment: .leading, spacing: DS.space.s) {
            Text("Sections").font(DS.font.headline)
            if frame.nodes.isEmpty {
                Text("No observed sessions or routes at this replay position.")
                    .font(DS.font.caption).foregroundStyle(DS.textSecondary)
            } else {
                LazyVGrid(columns: [GridItem(.adaptive(minimum: 180),
                                              spacing: DS.space.s)],
                          spacing: DS.space.s) {
                    ForEach(frame.nodes) { node in
                        VStack(alignment: .leading, spacing: DS.space.xs) {
                            Text(node.section.replacingOccurrences(
                                of: "_", with: " ").capitalized)
                                .font(DS.font.headline)
                            if node.sessions.isEmpty {
                                Text("Route destination")
                                    .font(DS.font.caption)
                                    .foregroundStyle(DS.textSecondary)
                            } else {
                                ForEach(node.sessions, id: \.self) { session in
                                    Label(session.split(separator: "/").last.map(String.init)
                                          ?? session, systemImage: "bubble.left")
                                        .font(DS.font.caption)
                                        .lineLimit(1)
                                }
                            }
                        }
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .padding(DS.space.s)
                        .background(DS.raised)
                        .clipShape(RoundedRectangle(cornerRadius: DS.radius.card))
                    }
                }
                ForEach(frame.routes.suffix(5)) { route in
                    HStack(spacing: DS.space.xs) {
                        Image(systemName: store.missionControl.newlyFiredRouteIDs
                            .contains(route.routeID) ? "circle.fill" : "circle")
                            .foregroundStyle(DS.accent.color)
                        Text("\(route.sourceSection) → \(route.target)")
                            .font(DS.font.caption)
                        Text(route.artifactID).font(DS.font.monoCaption)
                            .foregroundStyle(DS.textSecondary)
                    }
                    .animation(DS.spring,
                               value: store.missionControl.newlyFiredRouteIDs)
                }
            }
        }
    }

    private var replay: some View {
        VStack(alignment: .leading, spacing: DS.space.xs) {
            HStack {
                Text("Overnight replay").font(DS.font.headline)
                Spacer()
                Button(replayPlaying ? "Pause" : "Play") {
                    if replayPosition >= 1 { replayPosition = 0 }
                    replayPlaying.toggle()
                }
                .disabled(store.missionControl.timeRange == nil)
            }
            Slider(value: $replayPosition, in: 0...1)
                .disabled(store.missionControl.timeRange == nil)
                .accessibilityLabel("Replay position")
            HStack {
                Text(replayDate.map { $0.formatted(date: .abbreviated, time: .standard) }
                     ?? "Live durable state")
                Spacer()
                Text("\(frame.decisions.count) decisions")
            }
            .font(DS.font.caption).foregroundStyle(DS.textSecondary)
            if !store.missionControl.snapshotTags.isEmpty {
                Text(store.missionControl.snapshotTags.map {
                    "Snapshot \($0.cursor)"
                }.joined(separator: " · "))
                .font(DS.font.caption).foregroundStyle(DS.textSecondary)
            }
        }
    }

    private var budgetMeters: some View {
        let budget = store.missionControl.budget
        return VStack(alignment: .leading, spacing: DS.space.s) {
            Text("Budgets").font(DS.font.headline)
            if let exhausted = budget.exhaustedReason {
                Label("Exhausted: \(exhausted.replacingOccurrences(of: "_", with: " "))",
                      systemImage: "stop.circle.fill")
                    .font(DS.font.callout).foregroundStyle(DS.status.error.color)
            }
            if !budget.overQuota.isEmpty {
                Label("Quota deferred: \(budget.overQuota.joined(separator: ", "))",
                      systemImage: "clock.badge.exclamationmark")
                    .font(DS.font.callout).foregroundStyle(DS.status.warning.color)
            }
            if !budget.hasCaps {
                Text("No budget caps configured. Persisted usage remains visible below.")
                    .font(DS.font.caption).foregroundStyle(DS.textSecondary)
            }
            meter(label: "Autonomous turns", used: Double(budget.turnsUsed),
                  cap: budget.turnsCap.map(Double.init),
                  value: "\(budget.turnsUsed) turns")
            meter(label: "Wall clock", used: budget.wallClockSeconds,
                  cap: budget.wallClockCap,
                  value: duration(budget.wallClockSeconds))
            if budget.byProvider.isEmpty {
                Text("No persisted provider cost records yet — no $0 estimate is shown.")
                    .font(DS.font.caption).foregroundStyle(DS.textSecondary)
            } else {
                ForEach(budget.byProvider.keys.sorted(), id: \.self) { provider in
                    let totals = budget.byProvider[provider] ?? CostTotals()
                    let cap = budget.providerSpendCaps[provider]
                    meter(label: provider.capitalized,
                          used: Double(totals.costMicroUSD) / 1_000_000,
                          cap: cap, value: totals.display ?? "unmetered")
                }
            }
        }
    }

    private func meter(label: String, used: Double, cap: Double?,
                       value: String) -> some View {
        VStack(alignment: .leading, spacing: DS.space.xxs) {
            HStack { Text(label); Spacer(); Text(value).monospacedDigit() }
                .font(DS.font.caption)
            if let cap, cap > 0 {
                ProgressView(value: min(used, cap), total: cap)
            }
        }
    }

    private func duration(_ seconds: Double) -> String {
        let value = max(0, Int(seconds))
        return "\(value / 3600)h \((value % 3600) / 60)m"
    }

    private var approvalTray: some View {
        VStack(alignment: .leading, spacing: DS.space.s) {
            Text("Pending approvals").font(DS.font.headline)
            if store.missionControl.oversight.pending.isEmpty {
                Text("No routes are waiting for approval.")
                    .font(DS.font.caption).foregroundStyle(DS.textSecondary)
            } else {
                ForEach(store.missionControl.oversight.pending) { route in
                    pendingCard(route)
                }
            }
        }
    }

    private func pendingCard(_ route: ConductorPendingRoute) -> some View {
        VStack(alignment: .leading, spacing: DS.space.xs) {
            Text("\(route.artifactID) → \(route.target)").font(DS.font.headline)
            Text("\(route.reason) · from \(route.requestedBy) · rule \(route.ruleID)")
                .font(DS.font.caption).foregroundStyle(DS.textSecondary)
            if route.decisionSubmitted {
                Label("Decision submitted — waiting for Conductor confirmation",
                      systemImage: "hourglass")
                    .font(DS.font.caption).foregroundStyle(DS.status.warning.color)
            } else {
                HStack {
                    Button("Approve") {
                        store.decideConductorRoute(route, suffix: "ok")
                    }.buttonStyle(.borderedProminent)
                    Button("Reject") {
                        store.decideConductorRoute(
                            route, suffix: "changes", body: "Rejected in Mission Control")
                    }
                    Button("Do not route") {
                        store.decideConductorRoute(route, suffix: "do_not_route")
                    }
                    Button("Kill session", role: .destructive) {
                        store.decideConductorRoute(route, suffix: "kill_session")
                    }
                }.font(DS.font.caption)
            }
        }
        .padding(DS.space.s).background(DS.raised)
        .clipShape(RoundedRectangle(cornerRadius: DS.radius.card))
    }

    private var decisionLedger: some View {
        VStack(alignment: .leading, spacing: DS.space.s) {
            Text("Decision ledger").font(DS.font.headline)
            if frame.decisions.isEmpty {
                Text("No durable decisions at this replay position.")
                    .font(DS.font.caption).foregroundStyle(DS.textSecondary)
            } else {
                ForEach(frame.decisions.reversed()) { entry in
                    VStack(alignment: .leading, spacing: DS.space.xxs) {
                        HStack {
                            Text(entry.title).font(DS.font.callout)
                            Spacer()
                            Text(entry.timestamp.formatted(date: .omitted,
                                                           time: .standard))
                                .font(DS.font.monoCaption)
                                .foregroundStyle(DS.textSecondary)
                        }
                        Text(entry.explanation).font(DS.font.caption)
                            .foregroundStyle(DS.textSecondary)
                    }
                    .padding(.vertical, DS.space.xxs)
                }
            }
        }
    }
}
