import Foundation
import SwiftUI

enum OnboardingProgress: Equatable {
    case notStarted
    case inProgress(step: Int)
    case complete

    static func load(from defaults: UserDefaults) -> OnboardingProgress {
        if defaults.bool(forKey: OnboardingPersistence.completeKey) { return .complete }
        let step = defaults.integer(forKey: OnboardingPersistence.stepKey)
        return step > 0 ? .inProgress(step: min(step, 4)) : .notStarted
    }
}

enum OnboardingPersistence {
    static let completeKey = "v3.onboarding.complete"
    static let stepKey = "v3.onboarding.step"
    static let startedAtKey = "v3.onboarding.startedAt"
    static let visibleSectionsKey = "v3.visibleSections"
    static let initialSections: Set<String> = ["ideas", "research"]

    static func visibleSections(from defaults: UserDefaults) -> Set<String> {
        let saved = defaults.stringArray(forKey: visibleSectionsKey) ?? []
        return initialSections.union(saved)
    }

    static func save(_ progress: OnboardingProgress, to defaults: UserDefaults) {
        switch progress {
        case .notStarted:
            defaults.removeObject(forKey: stepKey)
            defaults.set(false, forKey: completeKey)
        case .inProgress(let step):
            defaults.set(max(1, min(step, 4)), forKey: stepKey)
            defaults.set(false, forKey: completeKey)
        case .complete:
            defaults.removeObject(forKey: stepKey)
            defaults.removeObject(forKey: startedAtKey)
            defaults.set(true, forKey: completeKey)
        }
    }

    static func saveVisibleSections(_ sections: Set<String>, to defaults: UserDefaults) {
        defaults.set(Array(sections).sorted(), forKey: visibleSectionsKey)
    }

    static func shouldPresent(progress: OnboardingProgress,
                              projectCount: Int) -> Bool {
        progress != .complete && projectCount == 0
    }
}

enum BackendProbeState: Equatable {
    case probing
    case available(String)
    case missing
    case broken(String)
}

struct BackendChecklistRow: Equatable, Identifiable {
    let id: String
    let title: String
    let state: BackendProbeState
    let fix: String
    let streams: Bool
    let resumes: Bool
}

enum OnboardingProbeLogic {
    static func rows(cliVersions: [String: String], cliAvailable: [String: Bool],
                     localModels: LocalModelsInfo?, cliProbeInFlight: Bool,
                     doctorProbeInFlight: Bool, doctorProbeCompleted: Bool,
                     capabilities: AgentCapabilitiesInfo?) -> [BackendChecklistRow] {
        let specs = [
            ("claude", "Claude", "npm install -g @anthropic-ai/claude-code"),
            ("codex", "Codex", "npm install -g @openai/codex"),
            ("gemini", "Gemini", "npm install -g @google/gemini-cli"),
        ]
        var result = specs.map { id, title, fix in
            let state: BackendProbeState
            if cliProbeInFlight {
                state = .probing
            } else if let version = cliVersions[id] {
                state = .available(version)
            } else if cliAvailable[id] == true {
                state = .broken("CLI found, but its version probe failed")
            } else {
                state = .missing
            }
            let caps = capabilities?.capability(for: id)
            return BackendChecklistRow(id: id, title: title, state: state, fix: fix,
                                       streams: caps?.streams ?? false,
                                       resumes: (caps?.sessionResume ?? "never") != "never")
        }
        let localState: BackendProbeState
        if doctorProbeInFlight || !doctorProbeCompleted {
            localState = .probing
        } else if let localModels, localModels.serverRunning {
            let installed = localModels.registry.filter(\.installed).count
            localState = installed > 0
                ? .available("Ollama running · \(installed) model\(installed == 1 ? "" : "s")")
                : .broken("Ollama is running, but no model is installed")
        } else if cliAvailable["ollama"] == true {
            localState = .broken("Ollama is installed, but the server is not running")
        } else {
            localState = .missing
        }
        let localCaps = capabilities?.capability(for: "local:probe")
        result.append(BackendChecklistRow(
            id: "ollama", title: "Local / Ollama", state: localState,
            fix: "Open Model Library", streams: localCaps?.streams ?? false,
            resumes: (localCaps?.sessionResume ?? "never") != "never"))
        return result
    }

    static func hasAvailableBackend(_ rows: [BackendChecklistRow]) -> Bool {
        rows.contains { if case .available = $0.state { return true }; return false }
    }
}

enum SectionDisclosureLogic {
    static func visible(_ metas: [SectionMeta], revealed: Set<String>) -> [SectionMeta] {
        metas.filter { revealed.contains($0.id) }
    }

    static func routedSection(targetSession: String, target: String) -> String? {
        let parts = targetSession.split(separator: "/").map(String.init)
        if parts.count == 3 { return parts[1] }
        let targetParts = target.split(separator: "/").map(String.init)
        if targetParts.count == 3 { return targetParts[1] }
        return target.isEmpty ? nil : target
    }
}

enum OnboardingGuideLogic {
    static func progressed(step: Int, eventKinds: Set<String>,
                           routedSection: String?) -> OnboardingProgress? {
        if step == 2 && (eventKinds.contains("message_produced")
                         || eventKinds.contains("turn_completed")) {
            return .inProgress(step: 3)
        }
        if step == 3 && eventKinds.contains("step_in_joined") {
            return .inProgress(step: 4)
        }
        if step == 4 && routedSection == "research" { return .complete }
        return nil
    }
}

struct OnboardingCoachBar: View {
    @EnvironmentObject var store: OrchestratorStore
    let onOpenGuide: () -> Void

    private var step: Int {
        if case .inProgress(let value) = store.onboardingProgress { return value }
        return 1
    }

    var body: some View {
        HStack(spacing: DS.space.s) {
            Image(systemName: "sparkles").foregroundStyle(DS.accent.color)
            VStack(alignment: .leading, spacing: 1) {
                Text("First brainstorm · step \(step) of 4")
                    .font(DS.font.callout.weight(.semibold))
                Text(prompt).font(DS.font.caption).foregroundStyle(.secondary)
            }
            Spacer()
            Button("Guide") { onOpenGuide() }.font(DS.font.caption)
            Button("Skip") { store.skipOnboarding() }.font(DS.font.caption)
        }
        .padding(.horizontal, DS.space.m)
        .padding(.vertical, DS.space.xs)
        .background(.bar)
        .overlay(alignment: .bottom) { Divider() }
    }

    private var prompt: String {
        switch step {
        case 2: return "Watch a real agent reply; this advances when a turn lands."
        case 3: return "Use Step in; this advances when the engine confirms you joined."
        case 4: return "Route the finished artifact to Research to complete the guide."
        default: return "Open the guide to start a real Ideas brainstorm."
        }
    }
}

struct OnboardingView: View {
    @EnvironmentObject var store: OrchestratorStore
    let onStartBrainstorm: () -> Void
    let onOpenModels: () -> Void
    let onDismiss: () -> Void

    private var rows: [BackendChecklistRow] { store.onboardingChecklistRows }
    private var step: Int {
        if case .inProgress(let value) = store.onboardingProgress { return value }
        return 1
    }

    var body: some View {
        VStack(alignment: .leading, spacing: DS.space.m) {
            HStack {
                VStack(alignment: .leading, spacing: DS.space.xxs) {
                    Text("Meet your agent team").font(DS.font.title)
                    Text("First, verify which brains can actually answer on this Mac.")
                        .font(DS.font.body).foregroundStyle(.secondary)
                }
                Spacer()
                if step > 1 { Button("Back to brainstorm") { onDismiss() } }
                Button("Skip") { store.skipOnboarding(); onDismiss() }
            }
            backendChecklist
            Divider()
            guide
        }
        .padding(DS.space.margin)
        .frame(minWidth: 620, minHeight: 520)
        .onAppear { store.probeOnboardingBackends() }
    }

    private var backendChecklist: some View {
        VStack(alignment: .leading, spacing: DS.space.xs) {
            HStack {
                Text("Brains available").font(DS.font.headline)
                Spacer()
                Button("Probe again") { store.probeOnboardingBackends() }
                    .disabled(store.probeAllInFlight || store.doctorProbeInFlight)
            }
            ForEach(rows) { row in
                HStack(spacing: DS.space.s) {
                    probeIcon(row.state)
                    VStack(alignment: .leading, spacing: 2) {
                        Text(row.title).font(DS.font.body.weight(.medium))
                        Text(detail(row)).font(DS.font.caption).foregroundStyle(.secondary)
                    }
                    Spacer()
                    if row.streams { StatusPill(kind: .idle, label: "Streams") }
                    if row.resumes { StatusPill(kind: .idle, label: "Resumes") }
                    if case .missing = row.state { fixButton(row) }
                    if case .broken = row.state { fixButton(row) }
                }
                .padding(DS.space.xs)
                .background(RoundedRectangle(cornerRadius: DS.radius.chip)
                    .fill(DS.cardBg))
            }
        }
    }

    @ViewBuilder
    private func probeIcon(_ state: BackendProbeState) -> some View {
        switch state {
        case .probing: ProgressView().controlSize(.small)
        case .available: Image(systemName: "checkmark.circle.fill")
                .foregroundStyle(DS.status.success.color)
        case .missing, .broken: Image(systemName: "exclamationmark.triangle.fill")
                .foregroundStyle(DS.status.warning.color)
        }
    }

    private func detail(_ row: BackendChecklistRow) -> String {
        switch row.state {
        case .probing: return "Checking…"
        case .available(let detail): return detail
        case .missing: return "Not found · \(row.fix)"
        case .broken(let reason): return "\(reason) · \(row.fix)"
        }
    }

    private func fixButton(_ row: BackendChecklistRow) -> some View {
        Button("Fix…") {
            if row.id == "ollama" { onOpenModels() }
            else { store.runInTerminal(row.fix) }
        }
        .font(DS.font.caption)
        .help(row.fix)
    }

    private var guide: some View {
        VStack(alignment: .leading, spacing: DS.space.s) {
            Text("Five-minute first brainstorm · \(step) of 4")
                .font(DS.font.headline)
            Text(guideText).font(DS.font.body)
            HStack {
                Button("Skip guide") { store.skipOnboarding(); onDismiss() }
                Spacer()
                if step == 1 {
                    Button("Start a real brainstorm") {
                        store.advanceOnboarding()
                        onStartBrainstorm()
                    }
                    .buttonStyle(.borderedProminent)
                    .disabled(!OnboardingProbeLogic.hasAvailableBackend(rows))
                } else {
                    Button("Back to brainstorm") { onDismiss() }
                        .buttonStyle(.borderedProminent)
                }
            }
            if step == 1 && !OnboardingProbeLogic.hasAvailableBackend(rows) {
                Label("No backend passed a probe. Fix one above before starting — this guide never simulates a debate.",
                      systemImage: "exclamationmark.triangle")
                    .font(DS.font.caption).foregroundStyle(DS.status.warning.color)
            }
        }
    }

    private var guideText: String {
        switch step {
        case 1: return "Seed one idea. This creates an Ideas chat and launches the real engine."
        case 2: return "Watch the live agent debate in the transcript; replies come from the backends that passed above."
        case 3: return "Use Step in during a round to add your judgment before consensus closes."
        default: return "When the idea is ready, route its artifact to Research. That real route also reveals Research destinations in the rail."
        }
    }
}
