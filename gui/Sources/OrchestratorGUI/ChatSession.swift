import Foundation

// V3 board 1.5: chat-session lifecycle. One explicit state enum (R4/§4.2 —
// never loose booleans), a PURE termination reducer, and a static
// injectable mint — all testable against temp dirs without touching
// OrchestratorStore (whose URLs point at real user data).

enum ChatSessionState: Equatable {
    case idle            // minted, engine never launched this app session
    case launching
    case running
    case waitingForHuman // engine alive AND agent_state says awaiting_human
    case stopping
    case relaunching
    // stopped = interrupted mid-chat; the dir is resumable APPEND-ONLY
    // (conversational resume, engine). ended = the phase completed
    // (done=true) — the engine SKIPS done apps, so Relaunch is meaningless
    // and the composer can only queue for nothing. The distinction is
    // load-bearing for 1.6's composer/relaunch affordances.
    case stopped
    case ended(reason: String)
    case crashed(code: Int32, wasSignal: Bool)

    var isAlive: Bool {
        switch self {
        case .launching, .running, .waitingForHuman, .stopping, .relaunching:
            return true
        default:
            return false
        }
    }

    // Pure reducer for process termination. Review-verified mapping — three
    // clean-exit paths must NOT read as crashes:
    //   · user pressed Stop (wasStopping)              -> stopped
    //   · exit 0 with final state done=true            -> ended (user end OR
    //     idle timeout both finalize honestly and exit 0)
    //   · exit 0, not done (SIGTERM mid-wait exits 0;  -> stopped
    //     relaunch-of-done no-ops also exit 0 w/ done, caught above)
    // A SIGKILL'd child reports uncaughtSignal with status==signal number —
    // that is "killed by signal N", not "exited with code N" (§5.2).
    static func afterTermination(status: Int32, uncaughtSignal: Bool,
                                 wasStopping: Bool, stateDone: Bool,
                                 conversationEnd: String?) -> ChatSessionState {
        if wasStopping { return .stopped }
        if uncaughtSignal { return .crashed(code: status, wasSignal: true) }
        if status != 0 { return .crashed(code: status, wasSignal: false) }
        if stateDone { return .ended(reason: conversationEnd ?? "conversation complete") }
        return .stopped
    }

    // Waiting derivation, gated on process liveness: awaiting_human SURVIVES
    // kill -9 in agent_state.json (the engine's finally can't run), so a
    // scan-only derivation would show "waiting for you" on a dead chat
    // forever. Only an alive session may show waiting (R2).
    static func applyingScan(current: ChatSessionState,
                             awaitingHuman: Bool,
                             processAlive: Bool) -> ChatSessionState {
        guard processAlive else { return current }
        switch current {
        case .running where awaitingHuman:
            return .waitingForHuman
        case .waitingForHuman where !awaitingHuman:
            return .running
        default:
            return current
        }
    }
}

struct ChatSession: Identifiable, Equatable {
    let id: String        // flat dir name: <project>--<section>--<slug>
    let project: String
    let section: String
    let slug: String
    let workflow: String  // chat_ideas / chat_research (a conversational workflow)
    var state: ChatSessionState = .idle
}

enum ChatSessionMint {
    // GLOSSARY "Layout (M1 interim)": each component INDEPENDENTLY slugified,
    // then joined with a literal "--". Never slugify the joined string —
    // slugify collapses consecutive dashes and would destroy the separator.
    static func flatName(project: String, section: String, title: String) -> String {
        let p = OrchestratorStore.slugify(project)
        let s = OrchestratorStore.slugify(section)
        let t = OrchestratorStore.slugify(title)
        return "\(p)--\(s)--\(t)"
    }

    struct Minted: Equatable {
        let name: String
        let dirURL: URL
    }

    // Mints the flat chat dir the engine's find_apps contract requires:
    // initial_prompt/initial_prompt.md (first message as body) + workflow.txt
    // (ALWAYS written — a chat dir missing workflow.txt resolves to app_build
    // on both sides, and a shepherd relaunch would run a full build debate on
    // the chat). Name collisions are suffixed -2, -3, … per GLOSSARY — never
    // refused, never overwritten.
    static func mintChatDir(rootURL: URL, project: String, section: String,
                            title: String, workflow: String,
                            firstMessage: String) throws -> Minted {
        let fm = FileManager.default
        let base = flatName(project: project, section: section, title: title)
        var name = base
        var n = 2
        while fm.fileExists(atPath: rootURL.appendingPathComponent(name).path) {
            name = "\(base)-\(n)"
            n += 1
        }
        let dir = rootURL.appendingPathComponent(name)
        let promptDir = dir.appendingPathComponent("initial_prompt")
        try fm.createDirectory(at: promptDir, withIntermediateDirectories: true)
        try firstMessage.write(to: promptDir.appendingPathComponent("initial_prompt.md"),
                               atomically: true, encoding: .utf8)
        try (workflow + "\n").write(to: dir.appendingPathComponent("workflow.txt"),
                                    atomically: true, encoding: .utf8)
        return Minted(name: name, dirURL: dir)
    }
}
