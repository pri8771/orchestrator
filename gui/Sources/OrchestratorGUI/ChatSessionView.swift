import SwiftUI

// V3 board 1.6: the chat surface for engine-backed chat sessions.
//
// Renders the session's single conversational phase transcript as bubbles
// (TranscriptParser — the same strict-layout parser the run view uses) with
// a composer that feeds human_inbox.txt. The surface keys EXCLUSIVELY on
// ChatSession state + awaiting_human: Project.running's 240s mtime
// heuristic flips false while the engine is alive but waiting for the
// human, so consuming it here would show "waiting on codex+claude+gemini"
// over a healthy chat (review amendment). It also never renders
// ParallelBuildBanner: a conversational round's next_agent is a '+'-joined
// roster — the same shape as a build fan-out — but it is a panel of chat
// replies, not build lanes.

// Pure derivations, split out for tests (house style: no OrchestratorStore
// in tests).
enum ChatSurfaceLogic {
    enum ComposerMode: Equatable {
        case live                      // engine alive: drained at next round
        case queued(hint: String)      // not running: delivered on next launch
        case disabled(reason: String)  // ended/crashed: no dead controls (§16)
    }

    static func composerMode(for state: ChatSessionState) -> ComposerMode {
        switch state {
        case .launching, .running, .waitingForHuman, .relaunching:
            return .live
        case .idle:
            return .queued(hint: "Queued — the agents see it when the chat starts")
        case .stopped, .stopping:
            return .queued(hint: "Queued — the agents see it on the next launch")
        case .ended(let reason):
            return .disabled(reason: "This chat has ended (\(reason)). Start a new chat to continue the thread.")
        case .crashed(let code, let wasSignal):
            return .disabled(reason: wasSignal
                ? "The chat engine was killed by signal \(code) — relaunch to continue."
                : "The chat engine crashed (exit code \(code)) — relaunch to continue.")
        }
    }

    // Bubbles land one BATCH per round (the engine appends after all turns
    // gather), so the honest shimmer is "the panel is replying" — shown only
    // while the engine is genuinely mid-round: alive, roster assigned, and
    // not waiting on the human (§12.1: loading must end).
    static func showsReplying(state: ChatSessionState, nextAgent: String?) -> Bool {
        state == .running && (nextAgent?.isEmpty == false)
    }

    static func showsWaitingForYou(state: ChatSessionState) -> Bool {
        state == .waitingForHuman
    }
}

struct ChatSessionView: View {
    @EnvironmentObject var store: OrchestratorStore
    let sessionID: String
    @State private var draft = ""
    @State private var transcript = PhaseTranscript()
    @State private var loadedID = ""
    @State private var lastCount = 0

    private var session: ChatSession? { store.chatSessions[sessionID] }
    // The background scan discovers the minted dir on its next tick; until
    // then the surface renders from the store-side ChatSession alone
    // (review amendment: tolerate a missing/.new Project on the first ticks).
    private var project: Project? { store.projects.first { $0.name == sessionID } }
    private var phaseKey: String {
        session.flatMap {
            store.workflow(named: $0.workflow)?.phases.first(where: \.conversational)?.key
        } ?? "chat"
    }

    var body: some View {
        let state = session?.state ?? .idle
        VStack(spacing: 0) {
            header(state)
            Divider()
            messages(state)
                .frame(maxHeight: .infinity)
            Divider()
            composer(state)
        }
        .task(id: "\(sessionID)/\(phaseKey)") { await poll() }
    }

    // MARK: header

    private func header(_ state: ChatSessionState) -> some View {
        HStack(spacing: 10) {
            VStack(alignment: .leading, spacing: 1) {
                Text(session.map { "\($0.slug)" } ?? sessionID)
                    .font(.headline)
                Text(session.map { "\($0.project) · \($0.section)" } ?? "")
                    .font(.caption).foregroundStyle(.secondary)
            }
            stateChip(state)
            Spacer()
            controls(state)
        }
        .padding(.horizontal, 16).padding(.vertical, 10)
    }

    // Symbol + word, never color alone (DS status grammar).
    @ViewBuilder
    private func stateChip(_ state: ChatSessionState) -> some View {
        switch state {
        case .idle:
            Label("Not started", systemImage: "circle.dotted")
                .font(.caption).foregroundStyle(.secondary)
        case .launching, .relaunching:
            Label("Starting…", systemImage: "arrow.triangle.2.circlepath")
                .font(.caption).foregroundStyle(.secondary)
        case .running:
            Label("Live", systemImage: "dot.radiowaves.left.and.right")
                .font(.caption).foregroundStyle(DS.accent.color)
        case .waitingForHuman:
            Label("Waiting for you", systemImage: "person.wave.2")
                .font(.caption).foregroundStyle(DS.status.warning.color)
        case .stopping:
            Label("Stopping…", systemImage: "stop.circle")
                .font(.caption).foregroundStyle(.secondary)
        case .stopped:
            Label("Stopped", systemImage: "pause.circle")
                .font(.caption).foregroundStyle(.secondary)
        case .ended:
            Label("Ended", systemImage: "checkmark.circle")
                .font(.caption).foregroundStyle(DS.status.success.color)
        case .crashed:
            Label("Crashed", systemImage: "exclamationmark.triangle")
                .font(.caption).foregroundStyle(DS.status.error.color)
        }
    }

    @ViewBuilder
    private func controls(_ state: ChatSessionState) -> some View {
        switch state {
        case .idle:
            Button("Start chat") { store.startChatSession(sessionID) }
        case .stopped, .crashed:
            Button("Relaunch") { store.startChatSession(sessionID) }
        case .running, .waitingForHuman:
            Button("End chat") { store.endChatSession(sessionID) }
            Button("Stop") { store.stopChatSession(sessionID) }
        case .launching, .relaunching, .stopping:
            EmptyView()
        case .ended:
            // The engine skips done apps — a Relaunch here would be a dead
            // control (§16). Ended chats offer nothing.
            EmptyView()
        }
    }

    // MARK: messages

    private func messages(_ state: ChatSessionState) -> some View {
        ScrollViewReader { proxy in
            ScrollView {
                LazyVStack(alignment: .leading, spacing: 14) {
                    if !transcript.exists {
                        startingPlaceholder(state)
                    }
                    ForEach(Array(transcript.messages.enumerated()), id: \.element.id) { idx, msg in
                        if idx == 0 || transcript.messages[idx - 1].section != msg.section {
                            SectionDivider(label: msg.section)
                        }
                        if msg.speaker == .human {
                            HStack {
                                Spacer(minLength: 60)
                                MessageBubble(message: msg)
                            }
                        } else {
                            MessageBubble(message: msg)
                        }
                    }
                    if let p = project, !store.pendingHuman(p).isEmpty {
                        PendingHumanBubble(text: store.pendingHuman(p))
                    }
                    if ChatSurfaceLogic.showsReplying(state: state,
                                                      nextAgent: project?.nextAgent) {
                        replyingRow
                    }
                    if ChatSurfaceLogic.showsWaitingForYou(state: state) {
                        waitingRow
                    }
                    if case .ended(let reason) = state {
                        endedDivider(reason)
                    }
                    Color.clear.frame(height: 1).id("BOTTOM")
                }
                .padding(18)
            }
            .onChange(of: transcript.messages.count) { _, c in
                if c > lastCount { withAnimation { proxy.scrollTo("BOTTOM", anchor: .bottom) } }
                lastCount = c
            }
        }
    }

    private func startingPlaceholder(_ state: ChatSessionState) -> some View {
        HStack(spacing: 8) {
            if state.isAlive { ProgressView().controlSize(.small) }
            Text(state.isAlive
                    ? "Setting up the room — the first replies appear here."
                    : "This chat hasn't started yet.")
                .font(.callout).foregroundStyle(.secondary)
        }
    }

    private var replyingRow: some View {
        HStack(spacing: 8) {
            ProgressView().controlSize(.small)
            Text("The agents are replying…")
                .font(.callout).italic().foregroundStyle(.secondary)
        }
        .accessibilityLabel("The agents are replying")
    }

    private var waitingRow: some View {
        Label("The room is waiting for your message.", systemImage: "person.wave.2")
            .font(.callout).foregroundStyle(.secondary)
            .accessibilityLabel("Waiting for your message")
    }

    // ENDED BY USER is not a MarkerBadge marker and the closure note is not a
    // FinalOutputCard — a chat ends quietly (review amendment).
    private func endedDivider(_ reason: String) -> some View {
        HStack(spacing: 8) {
            Rectangle().fill(.separator).frame(height: 1)
            Text("Conversation ended — \(reason)")
                .font(.caption).foregroundStyle(.secondary)
                .fixedSize()
            Rectangle().fill(.separator).frame(height: 1)
        }
    }

    // MARK: composer

    @ViewBuilder
    private func composer(_ state: ChatSessionState) -> some View {
        switch ChatSurfaceLogic.composerMode(for: state) {
        case .disabled(let reason):
            HStack(spacing: 8) {
                Image(systemName: "info.circle")
                Text(reason).font(.callout)
                Spacer()
            }
            .foregroundStyle(.secondary)
            .padding(12)
        case .live:
            inputBar(placeholder: "Message the room…")
        case .queued(let hint):
            inputBar(placeholder: hint)
        }
    }

    private func inputBar(placeholder: String) -> some View {
        HStack(spacing: 8) {
            ZStack(alignment: .leading) {
                RoundedRectangle(cornerRadius: 8)
                    .fill(Color(nsColor: .textBackgroundColor))
                RoundedRectangle(cornerRadius: 8)
                    .stroke(Color.secondary.opacity(0.25))
                if draft.isEmpty {
                    Text(placeholder)
                        .foregroundStyle(.secondary)
                        .padding(.horizontal, 9).padding(.vertical, 7)
                        .allowsHitTesting(false)
                }
                TextField("", text: $draft)
                    .textFieldStyle(.plain)
                    .padding(.horizontal, 9).padding(.vertical, 7)
                    .onSubmit(send)
                    .accessibilityLabel("Message to the room")
            }
            .frame(height: 36)
            Button(action: send) { Image(systemName: "paperplane.fill") }
                .disabled(draft.trimmingCharacters(in: .whitespaces).isEmpty)
                .keyboardShortcut(.return, modifiers: [])
                .accessibilityLabel("Send message")
        }
        .padding(10)
    }

    private func send() {
        let text = draft.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty, let p = project else { return }
        // sendHumanMessage appends to <chat>/human_inbox.txt; the engine
        // drains it at the next round open (or wakes within ~250ms while
        // waiting). The bubble appears once the TRANSCRIPT records it — the
        // pending bubble is visually distinct, never a fake confirmation (R2).
        store.sendHumanMessage(p, text)
        draft = ""
    }

    // MARK: polling

    private func poll() async {
        while !Task.isCancelled {
            if let p = project {
                let t = await store.transcript(for: p, phaseKey: phaseKey)
                guard !Task.isCancelled else { return }
                if loadedID != "\(sessionID)/\(phaseKey)" {
                    lastCount = t.messages.count
                    loadedID = "\(sessionID)/\(phaseKey)"
                }
                if t != transcript { transcript = t }
            }
            try? await Task.sleep(nanoseconds: 1_500_000_000)
        }
    }
}
