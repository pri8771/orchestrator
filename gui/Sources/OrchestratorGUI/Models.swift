import Foundation
import SwiftUI

// MARK: - Phase / Workflow model
//
// Phases are no longer hard-coded: each project runs a *workflow* (build an app,
// answer a question, research, productionize) loaded from
// .orchestrator/workflows/<name>.json. PhaseDef mirrors one phase; WorkflowDef a
// whole pipeline. ALL_PHASES stays as the app_build fallback used when a
// workflow file can't be read.

struct PhaseDef: Identifiable, Hashable {
    let key: String
    let folder: String
    let file: String
    let title: String
    var purpose: String = ""
    var rounds: Int = 9
    var roles: [String] = []
    var writes: Bool = false
    // V3 board 1.3: human-paced chat phase — rounds is ignored (0 = unbounded).
    var conversational: Bool = false
    var id: String { key }
}

struct WorkflowDef: Identifiable, Hashable {
    let name: String
    let title: String
    let description: String
    let target: String        // app / answer / research / productionize
    var phases: [PhaseDef]
    var id: String { name }

    // Short human label + SF Symbol for the target.
    var symbol: String {
        switch target {
        case "answer": return "questionmark.bubble"
        case "research": return "magnifyingglass"
        case "productionize": return "shippingbox"
        default: return "hammer"
        }
    }
    var kindLabel: String {
        switch target {
        case "answer": return "Answer"
        case "research": return "Research"
        case "productionize": return "Productionize"
        default: return "Build"
        }
    }
}

// The built-in app_build pipeline — used as a fallback if no workflow JSON loads.
let ALL_PHASES: [PhaseDef] = [
    .init(key: "prompt_contract", folder: "prompt_contract", file: "prompt_contract.md", title: "Prompt Contract"),
    .init(key: "product_research", folder: "product_research", file: "product_research.md", title: "Product Research"),
    .init(key: "portfolio_selection", folder: "portfolio_selection", file: "portfolio_selection.md", title: "Portfolio Selection"),
    .init(key: "initial_discussion", folder: "initial_discussion", file: "initial_discussion.md", title: "Initial Discussion"),
    .init(key: "per_app_product_brief", folder: "per_app_product_brief", file: "per_app_product_brief.md", title: "Per App Product Brief"),
    .init(key: "next_steps_small", folder: "next_steps_small", file: "next_steps_small.md", title: "Next Steps Small"),
    .init(key: "detailed_discussion", folder: "detailed_discussion", file: "detailed_discussion.md", title: "Detailed Discussion"),
    .init(key: "app_features", folder: "app_features", file: "app_features.md", title: "App Features"),
    .init(key: "design_discussion", folder: "design_discussion", file: "design_discussion.md", title: "Design Discussion"),
    .init(key: "design_handoff", folder: "design_handoff", file: "design_handoff.md", title: "Design Handoff"),
    .init(key: "ios_architecture_review", folder: "ios_architecture_review", file: "ios_architecture_review.md", title: "iOS Architecture Review"),
    .init(key: "tech_specs", folder: "tech_specs", file: "tech_specs.md", title: "Tech Specs"),
    .init(key: "project_plan", folder: "project_plan", file: "project_plan.md", title: "Project Plan"),
    .init(key: "task_assignments", folder: "task_assignments", file: "task_assignments.md", title: "Task Assignments"),
    .init(key: "implementation_readiness_gate", folder: "implementation_readiness_gate", file: "implementation_readiness_gate.md", title: "Implementation Readiness Gate"),
    .init(key: "build_coordination", folder: "build_coordination", file: "agent_messages.md", title: "Build Coordination", writes: true),
    .init(key: "build_verification", folder: "build_verification", file: "build_verification.md", title: "Build Verification"),
    .init(key: "human_qa_checklist", folder: "human_qa_checklist", file: "human_qa_checklist.md", title: "Human QA Checklist"),
    .init(key: "app_store_readiness", folder: "app_store_readiness", file: "app_store_readiness.md", title: "App Store Readiness"),
    .init(key: "final_review", folder: "final_review", file: "final_review.md", title: "Final Review"),
    .init(key: "portfolio_audit", folder: "portfolio_audit", file: "portfolio_audit.md", title: "Portfolio Audit"),
]

// MARK: - Sub-agents: roles + rotating personalities (edit .orchestrator/roles.json)

struct RoleDef: Identifiable, Hashable {
    var id: String
    var name: String
    var focus: String
}

struct PersonalityDef: Identifiable, Hashable {
    var id: String
    var name: String
    var style: String
}

// MARK: - JSON decoding shims (match workflows/*.json and roles.json)

struct PhaseJSON: Codable {
    let key: String
    var folder: String?
    var file: String?
    var title: String?
    var purpose: String?
    var rounds: Int?
    var roles: [String]?
    var writes: Bool?
    var conversational: Bool?
}

struct WorkflowJSON: Codable {
    let name: String
    var title: String?
    var description: String?
    var target: String?
    var phases: [PhaseJSON]
}

struct RoleJSON: Codable { var id: String; var name: String; var focus: String }
struct PersonalityJSON: Codable { var id: String; var name: String; var style: String }
struct RolesFileJSON: Codable {
    var personalities: [PersonalityJSON]?
    var roles: [RoleJSON]?
    var agentRoleOverrides: [String: String]?

    enum CodingKeys: String, CodingKey {
        case personalities, roles
        case agentRoleOverrides = "agent_role_overrides"
    }
}

extension WorkflowDef {
    static func from(_ j: WorkflowJSON) -> WorkflowDef {
        let phases = j.phases.map { p in
            PhaseDef(key: p.key,
                     folder: p.folder ?? p.key,
                     file: p.file ?? (p.key + ".md"),
                     title: p.title ?? p.key.replacingOccurrences(of: "_", with: " ").capitalized,
                     purpose: p.purpose ?? "",
                     rounds: p.rounds ?? 9,
                     roles: p.roles ?? [],
                     writes: p.writes ?? false,
                     conversational: p.conversational ?? false)
        }
        return WorkflowDef(name: j.name,
                           title: j.title ?? j.name,
                           description: j.description ?? "",
                           target: j.target ?? "app",
                           phases: phases)
    }
}

// MARK: - Project

enum ProjectStatus: String {
    case new, inProgress, done, aborted

    var label: String {
        switch self {
        case .new: return "Not started"
        case .inProgress: return "In progress"
        case .done: return "Done"
        case .aborted: return "Aborted"
        }
    }
    var symbol: String {
        switch self {
        case .new: return "circle.dashed"
        case .inProgress: return "circle.fill"
        case .done: return "checkmark.circle.fill"
        case .aborted: return "exclamationmark.triangle.fill"
        }
    }
    // DS grammar (§6): accent = running · green = success · red = error.
    var tint: Color {
        switch self {
        case .new: return .secondary
        case .inProgress: return DS.accent.color
        case .done: return DS.status.success.color
        case .aborted: return DS.status.error.color
        }
    }
}

struct Project: Identifiable, Equatable {
    let name: String
    var status: ProjectStatus
    var currentPhase: String?
    var currentRound: Int
    var nextAgent: String?
    var error: String?
    var lastProcessed: String?
    var completedPhases: [String]
    var phaseOutputs: [String: String]
    var dirURL: URL
    var running: Bool = false
    // Which workflow this project runs, and its shape (for progress + phase list).
    var workflow: String = "app_build"
    var workflowTitle: String = "Build an App"
    var workflowKind: String = "Build"
    var phaseCount: Int = ALL_PHASES.count
    var phaseTitles: [String: String] = [:]
    // Set when the engine paused for a semi-autonomous/manual approval after this phase.
    var awaitingApproval: String? = nil
    // Set when a parallel-build lane merge hit a real conflict (agent_state.json
    // blocked_conflict) — the run is paused until the user resolves and resumes.
    var blockedConflict: BlockedConflict? = nil
    // Latest record from <project>/verify_results.json (nil = never verified),
    // plus how many recorded attempts were self-repair retries.
    var latestVerify: VerifyRecord? = nil
    var verifyRepairCount: Int = 0
    // True when the user pressed Stop on this run from this GUI session (the
    // state-file mtime heuristic would otherwise show "running" for minutes).
    var manuallyStopped: Bool = false
    // True when <project>/.orch_archived exists — removed from the sidebar's
    // active sections and invisible to engine scans (find_apps skips it), but
    // kept on disk so Restore can bring it back.
    var archived: Bool = false
    // agent_state.json's phase_resolutions: {phase_key: reason} for a phase
    // that closed WITHOUT a clean resolution — a failing quality gate that
    // exhausted repair, a forced vote nobody actually decided, or a
    // tasks.json/interfaces.json contract repair that stayed broken. A
    // completed phase isn't necessarily a CLEAN one; this is how the GUI
    // tells the difference instead of reading every phase as equally done.
    var phaseResolutions: [String: String] = [:]

    var id: String { name }

    var progressText: String {
        switch status {
        case .new: return "not started"
        case .done: return "\(completedPhases.count)/\(phaseCount) phases"
        case .aborted:
            if let p = currentPhase { return "stopped at \(titleFor(p))" }
            return "aborted"
        case .inProgress:
            if let p = currentPhase { return "\(titleFor(p)) · round \(currentRound)" }
            return "\(completedPhases.count)/\(phaseCount) phases"
        }
    }

    func titleFor(_ key: String) -> String {
        phaseTitles[key] ?? (ALL_PHASES.first { $0.key == key }?.title ?? key)
    }

    func phaseStatus(_ key: String) -> PhaseStatus {
        if completedPhases.contains(key) { return .done }
        if currentPhase == key { return status == .aborted ? .aborted : .active }
        return .pending
    }

    // A phase can be .done and still not be a CLEAN close — a failing
    // quality gate that exhausted repair, a forced vote nobody actually
    // decided, or a tasks/interfaces contract repair that stayed broken.
    // Views overlay this on top of phaseStatus's checkmark rather than
    // replacing it — the phase genuinely did finish, just not cleanly.
    func phaseResolutionWarning(_ key: String) -> String? {
        guard let reason = phaseResolutions[key] else { return nil }
        switch reason {
        case "quality_gate_warning":
            return "Closed despite a failing quality gate — repair rounds were exhausted."
        case "vote_undecided":
            return "The forced vote never actually decided — no clear winner."
        case "contract_error":
            return "Closed with a still-malformed tasks.json/interfaces.json contract."
        case "requirements_coverage_gap":
            return "A core requirement has no task covering it."
        default:
            return "Closed without a clean resolution (\(reason))."
        }
    }
}

enum PhaseStatus {
    case done, active, pending, aborted
    var symbol: String {
        switch self {
        case .done: return "checkmark.circle.fill"
        case .active: return "circle.fill"
        case .aborted: return "exclamationmark.triangle.fill"
        case .pending: return "circle"
        }
    }
    // DS grammar (§6): the old status blue is retired — anything "in
    // progress" renders in Conductor Indigo (DS.accent).
    var tint: Color {
        switch self {
        case .done: return DS.status.success.color
        case .active: return DS.accent.color
        case .aborted: return DS.status.error.color
        case .pending: return .secondary.opacity(0.5)
        }
    }
}

// MARK: - Transcript

enum Speaker: String {
    case codex, claude, gemini, ollama, coordinator, human, system

    var display: String {
        switch self {
        case .codex: return "Codex"
        case .claude: return "Claude"
        case .gemini: return "Gemini"
        case .ollama: return "Local (Ollama)"   // must match the engine's DISPLAY map
        case .coordinator: return "Coordinator"
        case .human: return "You"
        case .system: return "System"
        }
    }
    var initials: String {
        switch self {
        case .codex: return "Cx"
        case .claude: return "Cl"
        case .gemini: return "Gm"
        case .ollama: return "Lo"
        case .coordinator: return "Co"
        case .human: return "You"
        case .system: return "Sy"
        }
    }
    // DS §2.2: agent identity hues, rendered with the one tint formula
    // (fill 8%/12%, content 100%) — identity only, never state.
    var fill: Color {
        switch self {
        case .codex: return DS.agent.codex.fill
        case .claude: return DS.agent.claude.fill
        case .gemini: return DS.agent.gemini.fill
        case .ollama: return DS.agent.local.fill
        case .coordinator: return DS.accent.fill
        case .human: return DS.status.warning.fill
        case .system: return Color.secondary.opacity(0.12)
        }
    }
    var ink: Color {
        switch self {
        case .codex: return DS.agent.codex.color
        case .claude: return DS.agent.claude.color
        case .gemini: return DS.agent.gemini.color
        case .ollama: return DS.agent.local.color
        case .coordinator: return DS.accent.color
        case .human: return DS.status.warning.color
        case .system: return .secondary
        }
    }

    static func classify(_ headerInner: String) -> Speaker {
        let lower = headerInner.lowercased()
        if lower.hasPrefix("coordinator") { return .coordinator }
        if lower.hasPrefix("integrator") { return .coordinator }
        if lower.hasPrefix("repair") { return .coordinator }
        if lower.hasPrefix("codex") { return .codex }
        if lower.hasPrefix("claude") { return .claude }
        if lower.hasPrefix("gemini") { return .gemini }
        if lower.hasPrefix("local") || lower.hasPrefix("ollama") { return .ollama }
        if lower.hasPrefix("you") || lower.hasPrefix("human") { return .human }
        return .system
    }
}

// One worker in a live parallel-build fan-out (see OrchestratorStore.parallelBuildWorkers).
// `done` is derived by cross-checking the per-call JSON logs, so it reflects
// which agents have ACTUALLY finished this iteration, not just a guess.
struct BuildWorker: Identifiable, Equatable {
    let slug: String        // e.g. "codex" or "codex-a" — matches orchestrator.py's roster slug
    let label: String       // e.g. "Codex" or "Codex A"
    let baseAgent: String   // the underlying CLI: "codex" / "claude" / "gemini"
    var done: Bool
    var id: String { slug }
    var speaker: Speaker { Speaker(rawValue: baseAgent) ?? .system }
}

struct ChatMessage: Identifiable, Equatable {
    let id: Int
    let speaker: Speaker
    let header: String
    let section: String
    let body: String
    var persona: String = ""   // e.g. "Product Strategist · Skeptic" or "Gemini"
}

struct PhaseTranscript: Equatable {
    var originalPrompt: String = ""
    var purpose: String = ""
    var messages: [ChatMessage] = []
    var finalOutput: String? = nil
    var marker: String? = nil
    var exists: Bool = false
}
