import Foundation
import SwiftUI

enum OversightDial: String, CaseIterable, Identifiable, Sendable {
    case fullAuto = "full_auto"
    case suggestOnly = "suggest_only"
    case gated
    case loopsGated = "loops_gated"

    var id: String { rawValue }
    var title: String {
        switch self {
        case .fullAuto: "Full auto"
        case .suggestOnly: "Suggest only"
        case .gated: "Gated"
        case .loopsGated: "Loops gated"
        }
    }
}

struct ConductorPendingRoute: Identifiable, Equatable, Sendable {
    let routeID: String
    let artifactID: String
    let target: String
    let ruleID: String
    let requestedBy: String
    let reason: String
    var kind = "route"
    var planVersion: Int? = nil
    var steps: [ConductorPlanStep] = []
    var decisionSubmitted = false
    var id: String { routeID }
    var isPlan: Bool { kind == "plan" }
}

struct ConductorPlanStep: Equatable, Sendable {
    let id: String
    let title: String
    let targetSection: String
}

struct ConductorOversightSnapshot: Equatable, Sendable {
    var available = false
    var dial = OversightDial.loopsGated
    var pending: [ConductorPendingRoute] = []
    var warnings: [String] = []
}

enum ConductorOversightDisk {
    nonisolated static func scan(rootURL: URL) -> ConductorOversightSnapshot {
        let fm = FileManager.default
        let dir = rootURL.appendingPathComponent(".conductor", isDirectory: true)
        guard fm.fileExists(atPath: dir.path) else { return .init() }
        var snapshot = ConductorOversightSnapshot(available: true)
        let stateURL = dir.appendingPathComponent("conductor_state.json")
        if let data = try? Data(contentsOf: stateURL),
           let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
            if let oversight = obj["oversight"] as? [String: Any],
               let raw = oversight["dial"] as? String,
               let dial = OversightDial(rawValue: raw) {
                snapshot.dial = dial
            } else {
                snapshot.warnings.append(
                    "Oversight setting is missing or invalid; Conductor enforces Loops gated.")
            }
            if let fallback = obj["_oversight_fallback_pending"] as? String,
               !fallback.isEmpty {
                snapshot.warnings.append("Conductor fallback pending: \(fallback)")
            }
        } else if fm.fileExists(atPath: stateURL.path) {
            snapshot.warnings.append(
                "Conductor state is unreadable; Conductor falls back to Loops gated.")
        }

        let approvals = dir.appendingPathComponent("approvals", isDirectory: true)
        let files = (try? fm.contentsOfDirectory(
            at: approvals, includingPropertiesForKeys: nil,
            options: [.skipsHiddenFiles])) ?? []
        var byID: [String: ConductorPendingRoute] = [:]
        for file in files.filter({ $0.pathExtension == "pending" })
            .sorted(by: { $0.lastPathComponent < $1.lastPathComponent }) {
            guard let data = try? Data(contentsOf: file),
                  let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
                  let route = decodePending(obj) else {
                snapshot.warnings.append("Unreadable pending route: \(file.lastPathComponent)")
                continue
            }
            byID[route.routeID] = route
        }
        // Upgrade compatibility: actions queued by 7.4 before per-route
        // `.pending` mirrors existed remain visible rather than silently lost.
        let queueURL = dir.appendingPathComponent("pending_actions.jsonl")
        if let text = try? String(contentsOf: queueURL, encoding: .utf8) {
            for (index, line) in text.split(separator: "\n").enumerated() {
                guard let data = String(line).data(using: .utf8),
                      let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
                      let route = decodePending(obj) else {
                    snapshot.warnings.append("Unreadable pending queue line \(index + 1)")
                    continue
                }
                byID[route.routeID] = byID[route.routeID] ?? route
            }
        }
        let submittedSuffixes = ["ok", "changes", "do_not_route", "kill_session", "edit"]
        snapshot.pending = byID.values.map { route in
            var value = route
            value.decisionSubmitted = value.decisionSubmitted
                || submittedSuffixes.contains { suffix in
                fm.fileExists(atPath: approvals.appendingPathComponent(
                    "\(route.routeID).\(suffix)").path)
            }
            return value
        }.sorted { $0.routeID < $1.routeID }
        return snapshot
    }

    nonisolated private static func decodePending(
        _ obj: [String: Any]
    ) -> ConductorPendingRoute? {
        let payload = obj["payload"] as? [String: Any] ?? [:]
        guard let routeID = (obj["route_id"] ?? obj["action_id"]) as? String,
              !routeID.isEmpty,
              let target = obj["target"] as? String, !target.isEmpty else { return nil }
        let kind = obj["kind"] as? String ?? "route"
        let rawSteps = payload["step_summary"] as? [[String: Any]] ?? []
        let steps = rawSteps.compactMap { step -> ConductorPlanStep? in
            guard let id = step["id"] as? String, !id.isEmpty,
                  let title = step["title"] as? String, !title.isEmpty,
                  let target = step["target_section"] as? String,
                  !target.isEmpty else { return nil }
            return ConductorPlanStep(id: id, title: title,
                                     targetSection: target)
        }
        return ConductorPendingRoute(
            routeID: routeID,
            artifactID: (kind == "plan" ? payload["plan_id"] :
                         payload["artifact_id"]) as? String ?? "unknown",
            target: target,
            ruleID: payload["rule_id"] as? String ?? "unknown",
            requestedBy: obj["requested_by"] as? String ?? "unknown",
            reason: obj["reason"] as? String ?? "Route requires approval",
            kind: kind, planVersion: payload["plan_version"] as? Int,
            steps: steps, decisionSubmitted: payload["approved"] as? Bool ?? false)
    }

    nonisolated static func readCurrentPlanBody(
        rootURL: URL, pending: ConductorPendingRoute
    ) -> Result<String, Error> {
        guard pending.isPlan,
              safeDecisionID(pending.artifactID),
              let project = pending.requestedBy.split(separator: "/").first,
              safeDecisionID(String(project))
        else {
            return .failure(NSError(
                domain: "ConductorPlan", code: 1,
                userInfo: [NSLocalizedDescriptionKey: "Plan location is invalid."]))
        }
        let url = rootURL.appendingPathComponent(String(project), isDirectory: true)
            .appendingPathComponent("artifacts", isDirectory: true)
            .appendingPathComponent(pending.artifactID, isDirectory: true)
            .appendingPathComponent("body.md")
        do { return .success(try String(contentsOf: url, encoding: .utf8)) }
        catch { return .failure(error) }
    }

    nonisolated static func safeDecisionID(_ value: String) -> Bool {
        !value.isEmpty && value.count <= 160 && value.allSatisfy {
            $0.isLetter || $0.isNumber || $0 == "-" || $0 == "_"
        }
    }
}

enum ConductorControlFiles {
    nonisolated static func writeDial(
        rootURL: URL, dial: OversightDial
    ) -> String? {
        let stateURL = rootURL.appendingPathComponent(
            ".conductor/conductor_state.json")
        guard let data = try? Data(contentsOf: stateURL),
              let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              obj["stage"] is String else {
            return "Conductor state is unavailable or corrupt; the dial was not changed."
        }
        // A separate request marker avoids a read-modify-write race that could
        // rewind the Conductor's concurrently advancing ledger cursor/routed
        // state. The next wake ledgers the change, saves authoritative state,
        // and removes this marker; the UI keeps showing the old read-back dial
        // until that happens.
        let url = rootURL.appendingPathComponent(
            ".conductor/oversight_request.json")
        do {
            let output = try JSONSerialization.data(
                withJSONObject: ["dial": dial.rawValue],
                options: [.prettyPrinted, .sortedKeys])
            try output.write(to: url, options: .atomic)
            return nil
        } catch {
            return "Could not save the Conductor dial: \(error.localizedDescription)"
        }
    }

    nonisolated static func writeDecision(
        rootURL: URL, routeID: String, suffix: String, body: String = ""
    ) -> String? {
        let validSuffixes = Set(["ok", "changes", "do_not_route", "kill_session", "edit"])
        guard ConductorOversightDisk.safeDecisionID(routeID),
              validSuffixes.contains(suffix) else {
            return "Invalid Conductor decision target."
        }
        let dir = rootURL.appendingPathComponent(
            ".conductor/approvals", isDirectory: true)
        do {
            try FileManager.default.createDirectory(
                at: dir, withIntermediateDirectories: true)
            try (body + (body.hasSuffix("\n") ? "" : "\n")).write(
                to: dir.appendingPathComponent("\(routeID).\(suffix)"),
                atomically: true, encoding: .utf8)
            return nil
        } catch {
            return "Could not write the Conductor decision: \(error.localizedDescription)"
        }
    }
}

struct ConductorOversightView: View {
    @EnvironmentObject var store: OrchestratorStore

    var body: some View {
        VStack(alignment: .leading, spacing: DS.space.m) {
            VStack(alignment: .leading, spacing: DS.space.xs) {
                Text("Conductor oversight").font(DS.font.title)
                Text("Choose which routes need you. Capability-escalating routes always wait for approval.")
                    .font(DS.font.caption).foregroundStyle(DS.textSecondary)
            }
            Picker("Oversight", selection: Binding(
                get: { store.conductorOversight.dial },
                set: { store.setConductorOversight($0) }
            )) {
                ForEach(OversightDial.allCases) { dial in
                    Text(dial.title).tag(dial)
                }
            }
            .pickerStyle(.segmented)
            .accessibilityHint("The displayed value is read from Conductor state")

            ForEach(store.conductorOversight.warnings, id: \.self) { warning in
                InlineBanner(kind: .warning, title: "Conductor fallback",
                             message: warning)
            }
            Divider()
            Text("Pending approvals").font(DS.font.headline)
            if store.conductorOversight.pending.isEmpty {
                Text("No routes are waiting for approval.")
                    .font(DS.font.caption).foregroundStyle(DS.textSecondary)
                Spacer()
            } else {
                ScrollView {
                    LazyVStack(alignment: .leading, spacing: DS.space.s) {
                        ForEach(store.conductorOversight.pending) { route in
                            ConductorPendingApprovalCard(
                                pending: route, rootURL: store.rootURL,
                                decide: { suffix, body in
                                    store.decideConductorRoute(
                                        route, suffix: suffix, body: body)
                                })
                        }
                    }
                }
            }
        }
        .padding(DS.space.l)
    }

}

struct ConductorPendingApprovalCard: View {
    let pending: ConductorPendingRoute
    let rootURL: URL
    let decide: (String, String) -> Void
    @State private var editing = false
    @State private var editBody = ""
    @State private var editError: String?

    var body: some View {
        VStack(alignment: .leading, spacing: DS.space.xs) {
            HStack {
                Text(pending.isPlan
                     ? "Plan \(pending.artifactID) v\(pending.planVersion ?? 1)"
                     : "\(pending.artifactID) → \(pending.target)")
                    .font(DS.font.headline)
                Spacer()
                Text(pending.routeID).font(DS.font.monoCaption)
                    .foregroundStyle(DS.textSecondary)
            }
            Text(pending.reason).font(DS.font.caption)
                .foregroundStyle(DS.textSecondary)
            if pending.isPlan {
                ForEach(Array(pending.steps.enumerated()), id: \.element.id) { index, step in
                    Text("\(index + 1). \(step.title) → \(step.targetSection)")
                        .font(DS.font.caption)
                }
            } else {
                Text("From \(pending.requestedBy) · rule \(pending.ruleID)")
                    .font(DS.font.caption).foregroundStyle(DS.textSecondary)
            }
            if let editError {
                Text(editError).font(DS.font.caption)
                    .foregroundStyle(DS.status.error.color)
            }
            if pending.decisionSubmitted {
                Label("Decision submitted — waiting for Conductor confirmation",
                      systemImage: "hourglass")
                .font(DS.font.caption).foregroundStyle(DS.textSecondary)
            } else {
                HStack {
                Button("Approve") { decide("ok", "") }
                    .buttonStyle(.borderedProminent)
                Button("Reject") {
                    decide("changes", "Rejected in Mission Control")
                }
                if pending.isPlan {
                    Button("Edit & Approve") { openEditor() }
                } else {
                    Button("Do not route") { decide("do_not_route", "") }
                    Button("Kill session", role: .destructive) {
                        decide("kill_session", "")
                    }
                }
                }
                .font(DS.font.caption)
            }
        }
        .padding(DS.space.s)
        .background(DS.raised)
        .clipShape(RoundedRectangle(cornerRadius: DS.radius.card))
        .sheet(isPresented: $editing) {
            VStack(alignment: .leading, spacing: DS.space.m) {
                Text("Edit plan before approval").font(DS.font.title)
                Text("This is the current artifact body read from disk.")
                    .font(DS.font.caption).foregroundStyle(DS.textSecondary)
                TextEditor(text: $editBody)
                    .font(DS.font.monoWell)
                    .frame(minWidth: 620, minHeight: 420)
                    .padding(DS.space.xs)
                    .background(DS.insetBg)
                    .clipShape(RoundedRectangle(cornerRadius: DS.radius.control))
                HStack {
                    Spacer()
                    Button("Cancel") { editing = false }
                    Button("Edit & Approve") {
                        decide("edit", editBody)
                        editing = false
                    }
                    .buttonStyle(.borderedProminent)
                    .disabled(editBody.trimmingCharacters(
                        in: .whitespacesAndNewlines).isEmpty)
                }
            }
            .padding(DS.space.l)
        }
    }

    private func openEditor() {
        switch ConductorOversightDisk.readCurrentPlanBody(
            rootURL: rootURL, pending: pending) {
        case .success(let body):
            editBody = body
            editError = nil
            editing = true
        case .failure(let error):
            editError = "Could not read the current plan: \(error.localizedDescription)"
        }
    }
}
