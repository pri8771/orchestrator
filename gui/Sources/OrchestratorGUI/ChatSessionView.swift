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
    @Environment(\.dismiss) private var dismiss
    // V3 board 1.11: pending states are cleared by ENGINE EVIDENCE (a
    // message_produced event / a new transcript block), never by timers.
    @State private var pendingSwap: [String: String] = [:]   // agent -> model
    @State private var retryPending = false
    @State private var snippets: [PromptSnippet] = []
    @State private var snippetForm: SnippetFormDraft?
    @State private var snippetNotice: String?

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
        .onAppear {
            if session?.state.isAlive == true { store.setFocusedLivePane(sessionID) }
            snippets = store.loadSnippets(
                section: session?.section,
                projectDir: session.map {
                    store.rootURL.appendingPathComponent($0.project)
                })
            snippetNotice = store.snippetWarnings.first
        }
        .onDisappear {
            if store.focusedLivePane == sessionID { store.setFocusedLivePane(nil) }
        }
        .onChange(of: store.eventsByProject[sessionID] ?? []) { _, events in
            // Clear pending-swap chips only on ENGINE EVIDENCE: a
            // message_produced whose model matches the requested swap.
            guard !pendingSwap.isEmpty else { return }
            for ev in events where ev.kind == "message_produced" && ev.phase == phaseKey {
                if pendingSwap[ev.agent] == ev.modelUsed {
                    pendingSwap[ev.agent] = nil
                }
            }
        }
        .sheet(item: $snippetForm) { form in
            SnippetVariableForm(
                draft: Binding(get: { snippetForm ?? form },
                               set: { snippetForm = $0 }),
                onInsert: { rendered, warnings in
                    draft = rendered
                    snippetNotice = warnings.first
                    snippetForm = nil
                },
                onCancel: { snippetForm = nil })
        }
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
            modelSwapMenu
            Spacer()
            controls(state)
        }
        .padding(.horizontal, 16).padding(.vertical, 10)
    }

    // V3 board 1.11: mid-chat model swap. Writes the CHAT's routing overlay;
    // the chip shows "→ next round" until a message_produced event proves
    // the new model actually answered (cleared in .onChange below).
    private var modelSwapMenu: some View {
        Menu {
            ForEach(enabledAgentIds, id: \.self) { agent in
                Menu("\(agent) — \(pendingSwap[agent].map { "\($0) → next round" } ?? (store.agentModels[agent] ?? "default"))") {
                    Button("Default") {
                        store.setChatModelOverride(sessionID, phaseKey: phaseKey,
                                                   agent: agent, model: nil)
                        pendingSwap[agent] = nil
                    }
                    ForEach(installedLocalTags, id: \.self) { tag in
                        Button("local:\(tag)") {
                            store.setChatModelOverride(sessionID, phaseKey: phaseKey,
                                                       agent: agent, model: "local:\(tag)")
                            pendingSwap[agent] = "local:\(tag)"
                        }
                    }
                }
            }
        } label: {
            Label("Models", systemImage: "cpu")
                .font(.caption)
        }
        .menuStyle(.borderlessButton)
        .fixedSize()
        .help("Swap an agent's model — applies at the next round barrier")
    }

    private var enabledAgentIds: [String] {
        store.enabledAgents.filter(\.value).keys.sorted()
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
            Button("Fork") { store.forkChatSession(sessionID) }
            discussButton(label: "Let them discuss")
        case .running, .waitingForHuman:
            discussButton(label: "End & discuss")
            Button("End chat") { store.endChatSession(sessionID) }
            Button("Stop") { store.stopChatSession(sessionID) }
        case .launching, .relaunching, .stopping:
            EmptyView()
        case .ended:
            // The engine skips done apps — a Relaunch would be a dead control
            // (§16). Fork and promotion are the meaningful verbs left.
            Button("Fork") { store.forkChatSession(sessionID) }
            discussButton(label: "Let them discuss")
        }
    }

    // V3 board 1.8: hand the chat to an auto debate. The session leaves the
    // chat surface entirely (the dir becomes a plain project) — dismiss and
    // let the run view take over.
    private func discussButton(label: String) -> some View {
        Button(label) {
            store.promoteChatSession(sessionID)
            dismiss()
        }
        .help("Promote this chat to a multi-agent auto debate seeded with the transcript")
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
                            VStack(alignment: .leading, spacing: 2) {
                                MessageBubble(message: msg)
                                attributionCaption(for: msg)
                            }
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
                if c != lastCount { retryPending = false }   // evidence landed
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

    // V3 board 1.11: per-message attribution from message_produced events —
    // the roster id stays on the bubble; the caption says which MODEL
    // actually answered, with an explicit badge when a fallback rescued it.
    private func producedEvent(for msg: ChatMessage) -> EngineEvent? {
        let round = Int(msg.section.split(separator: " ").last.map(String.init) ?? "") ?? 0
        return (store.eventsByProject[sessionID] ?? []).last {
            $0.kind == "message_produced" && $0.phase == phaseKey
                && $0.round == round
                && ($0.agent == msg.speaker.rawValue
                    || (msg.speaker == .ollama && $0.agent.hasPrefix("local:")))
        }
    }

    @ViewBuilder
    private func attributionCaption(for msg: ChatMessage) -> some View {
        if let ev = producedEvent(for: msg) {
            HStack(spacing: 6) {
                Text(ev.modelUsed)
                    .font(.caption2).foregroundStyle(.tertiary)
                if ev.status == "fallback" {
                    Label("fallback", systemImage: "arrow.uturn.down")
                        .font(.caption2)
                        .foregroundStyle(DS.status.fallback.color)
                        .accessibilityLabel("Answered by a fallback model")
                }
                if msg.id == transcript.messages.last?.id,
                   session?.state.isAlive == true {
                    retryMenu(agent: ev.agent)
                }
            }
            .padding(.leading, 44)
        }
    }

    private func retryMenu(agent: String) -> some View {
        Menu("Retry with…") {
            ForEach(installedLocalTags, id: \.self) { tag in
                Button("local:\(tag)") { requestRetry(agent: agent, model: "local:\(tag)") }
            }
            if let current = store.agentModels[agent] {
                Button("\(current) (fresh roll)") { requestRetry(agent: agent, model: current) }
            }
        }
        .menuStyle(.borderlessButton)
        .font(.caption2)
        .disabled(retryPending)
        .help(retryPending ? "A retry is already pending" : "Ask this agent to re-answer on a different model")
        .fixedSize()
    }

    private func requestRetry(agent: String, model: String) {
        store.requestChatRetry(sessionID, phaseKey: phaseKey,
                               agent: agent, model: model)
        retryPending = true
    }

    private var installedLocalTags: [String] {
        (store.localModels?.registry ?? []).filter(\.installed).map(\.id)
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
        VStack(alignment: .leading, spacing: DS.space.xxs) {
            if let snippetNotice {
                Label(snippetNotice, systemImage: "exclamationmark.triangle")
                    .font(DS.font.caption)
                    .foregroundStyle(DS.status.warning.color)
            }
            if !snippetMatches.isEmpty {
                ScrollView(.horizontal, showsIndicators: false) {
                    HStack(spacing: DS.space.xxs) {
                        ForEach(snippetMatches.prefix(6)) { snippet in
                            Button("/\(snippet.name)") { chooseSnippet(snippet) }
                                .buttonStyle(.bordered)
                                .font(DS.font.caption)
                        }
                    }
                }
            }
            HStack(spacing: DS.space.xs) {
                ZStack(alignment: .leading) {
                    RoundedRectangle(cornerRadius: DS.radius.chip)
                        .fill(Color(nsColor: .textBackgroundColor))
                    RoundedRectangle(cornerRadius: DS.radius.chip)
                        .stroke(Color.secondary.opacity(0.25))
                    if draft.isEmpty {
                        Text(placeholder)
                            .foregroundStyle(.secondary)
                            .padding(.horizontal, DS.space.s)
                            .padding(.vertical, DS.space.xs)
                            .allowsHitTesting(false)
                    }
                    TextField("", text: $draft)
                        .textFieldStyle(.plain)
                        .padding(.horizontal, DS.space.s)
                        .padding(.vertical, DS.space.xs)
                        .onSubmit(send)
                        .accessibilityLabel("Message to the room")
                }
                .frame(height: 36)
                Button(action: send) { Image(systemName: "paperplane.fill") }
                    .disabled(draft.trimmingCharacters(in: .whitespaces).isEmpty)
                    .keyboardShortcut(.return, modifiers: [])
                    .accessibilityLabel("Send message")
            }
        }
        .padding(DS.space.s)
    }

    private var snippetMatches: [PromptSnippet] {
        SnippetComposerLogic.matches(draft: draft, snippets: snippets)
    }

    private func chooseSnippet(_ snippet: PromptSnippet) {
        snippetNotice = snippet.warning
        if snippet.variables.isEmpty {
            draft = snippet.text
        } else {
            snippetForm = SnippetFormDraft(snippet: snippet)
        }
    }

    private func handleSnippetCommand() -> Bool {
        switch SnippetComposerLogic.resolveCommand(draft, snippets: snippets) {
        case .notCommand:
            return false
        case .refusal(let message):
            snippetNotice = message
        case .snippet(let snippet):
            chooseSnippet(snippet)
        }
        return true
    }

    private func send() {
        if handleSnippetCommand() { return }
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
            // V3 board 1.10: fast tick only while this chat is alive AND
            // focused (the sheet is up) — never for ended/stopped chats.
            let fast = store.focusedLivePane == sessionID
                && (session?.state.isAlive ?? false)
            try? await Task.sleep(nanoseconds: fast ? 500_000_000 : 1_500_000_000)
        }
    }
}

private struct SnippetVariableForm: View {
    @Binding var draft: SnippetFormDraft
    let onInsert: (String, [String]) -> Void
    let onCancel: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: DS.space.s) {
            Text(draft.snippet.name).font(DS.font.title)
            Text("Fill the fields, then insert the result into the composer. Nothing is sent automatically.")
                .font(DS.font.caption).foregroundStyle(.secondary)
            ForEach(draft.snippet.variables) { variable in
                VStack(alignment: .leading, spacing: DS.space.xxs) {
                    Text(variable.label + (variable.required ? " · Required" : ""))
                        .font(DS.font.callout)
                    control(for: variable)
                    if draft.blockingVariables.contains(variable) {
                        Text("Enter a valid \(variable.kind.rawValue) value.")
                            .font(DS.font.caption)
                            .foregroundStyle(DS.status.warning.color)
                    }
                }
            }
            ForEach(draft.lintWarnings, id: \.self) { warning in
                Label(warning, systemImage: "exclamationmark.triangle")
                    .font(DS.font.caption)
                    .foregroundStyle(DS.status.warning.color)
            }
            HStack {
                Button("Cancel", action: onCancel)
                Spacer()
                Button("Insert") {
                    onInsert(draft.renderedText, draft.lintWarnings)
                }
                .disabled(!draft.canInsert)
            }
        }
        .padding(DS.space.l)
        .frame(minWidth: 420)
    }

    @ViewBuilder
    private func control(for variable: SnippetVariable) -> some View {
        switch variable.kind {
        case .string:
            TextField(variable.label, text: valueBinding(variable.name))
                .textFieldStyle(.roundedBorder)
        case .number:
            TextField(variable.label, text: valueBinding(variable.name))
                .textFieldStyle(.roundedBorder)
        case .boolean:
            Toggle(variable.label, isOn: Binding(
                get: { draft.values[variable.name] == "true" },
                set: { draft.values[variable.name] = $0 ? "true" : "false" }))
        case .choice:
            Picker(variable.label, selection: valueBinding(variable.name)) {
                Text("Choose…").tag("")
                ForEach(variable.options, id: \.self) { Text($0).tag($0) }
            }
        }
    }

    private func valueBinding(_ name: String) -> Binding<String> {
        Binding(get: { draft.values[name] ?? "" },
                set: { draft.values[name] = $0 })
    }
}
