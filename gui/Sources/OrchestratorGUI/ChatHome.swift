import SwiftUI
import AppKit

// MARK: - Chat Home: the conversational front door.
//
// A concierge chat backed by the locally logged-in `claude` CLI (same
// no-API-key contract as the engine: key env vars are stripped). It explains
// the run modes, asks clarifying questions, and when the idea is concrete it
// emits a ```run-json``` block the GUI turns into a one-click "Create run"
// card. With no claude CLI available it degrades to the mode cards + New App.

struct ConciergeMessage: Identifiable, Equatable, Codable {
    enum Role: String, Codable { case user, concierge }
    let id: UUID
    let role: Role
    var text: String
    var suggestion: RunSuggestion? = nil

    init(id: UUID = UUID(), role: Role, text: String, suggestion: RunSuggestion? = nil) {
        self.id = id
        self.role = role
        self.text = text
        self.suggestion = suggestion
    }
}

struct RunSuggestion: Equatable, Codable {
    var name: String
    var workflow: String
    var prompt: String

    // Parse the first ```run-json``` fence from a concierge reply.
    static func parse(from reply: String) -> (clean: String, suggestion: RunSuggestion?) {
        guard let open = reply.range(of: "```run-json") else { return (reply, nil) }
        let afterOpen = reply[open.upperBound...]
        guard let close = afterOpen.range(of: "```") else { return (reply, nil) }
        let jsonText = String(afterOpen[..<close.lowerBound])
        var clean = reply
        clean.removeSubrange(open.lowerBound..<close.upperBound)
        clean = clean.trimmingCharacters(in: .whitespacesAndNewlines)
        guard let data = jsonText.data(using: .utf8),
              let obj = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any],
              let prompt = obj["prompt"] as? String, !prompt.isEmpty
        else { return (clean, nil) }
        let name = (obj["name"] as? String) ?? ""
        let workflow = (obj["workflow"] as? String) ?? "app_build"
        return (clean, RunSuggestion(name: name, workflow: workflow, prompt: prompt))
    }
}

// The six front-door modes, each mapping to a workflow.
private struct ChatMode: Identifiable {
    let key: String        // workflow name
    let label: String
    let symbol: String
    let blurb: String
    var id: String { key }

    static let all: [ChatMode] = [
        .init(key: "answer_question", label: "Ask", symbol: "questionmark.bubble",
              blurb: "Get a debated, multi-model answer to any question — no project output, just the best answer the panel can agree on."),
        .init(key: "brainstorm", label: "Plan", symbol: "lightbulb",
              blurb: "Fast idea exploration: the agents pitch and stress-test directions, you get a ranked shortlist. Cheapest mode."),
        .init(key: "app_spec", label: "Spec", symbol: "doc.text",
              blurb: "Turn an idea into a full product spec — features, design direction, architecture — WITHOUT building code. Continue into a build later."),
        .init(key: "app_build", label: "Create", symbol: "hammer",
              blurb: "The full pipeline: debate → spec → design → parallel build → verify. Produces a runnable app in the project's app_build folder."),
        .init(key: "research", label: "Research", symbol: "magnifyingglass",
              blurb: "Multi-agent research on a topic or market: findings, sources, and a synthesized report in the project docs."),
        .init(key: "audit", label: "Audit", symbol: "checkmark.shield",
              blurb: "Point the panel at an existing repo (read-only) for a security/bug/update audit with ranked, file:line findings."),
    ]
}

struct ChatHomeView: View {
    @EnvironmentObject var store: OrchestratorStore
    let onOpenProject: (String) -> Void
    let onNewApp: () -> Void


    var body: some View {
        VStack(spacing: 0) {
            ScrollViewReader { proxy in
                ScrollView {
                    VStack(alignment: .leading, spacing: DS.space.s) {
                        HStack(alignment: .top) {
                            header
                            Spacer()
                            if !store.chatMessages.isEmpty {
                                Button {
                                    store.startNewChat()
                                } label: {
                                    Label("New Chat", systemImage: "square.and.pencil")
                                }
                                .buttonStyle(.bordered)
                                .controlSize(.small)
                                .accessibilityLabel("Start a new chat")
                            }
                        }
                        if store.chatMessages.isEmpty { modeCards }
                        ForEach(store.chatMessages) { m in
                            ConciergeBubble(message: m) { suggestion in
                                createRun(from: suggestion)
                            }
                            .id(m.id)
                        }
                        if store.chatThinking {
                            HStack(spacing: DS.space.xxs) {
                                ProgressView().controlSize(.small)
                                Text("Thinking…").font(DS.font.caption)
                                    .foregroundStyle(.secondary)
                            }
                        }
                    }
                    .padding(DS.space.margin)
                    .frame(maxWidth: 760, alignment: .leading)
                    .frame(maxWidth: .infinity)
                }
                .onChange(of: store.chatMessages.count) { _, _ in
                    if let last = store.chatMessages.last?.id {
                        withAnimation { proxy.scrollTo(last, anchor: .bottom) }
                    }
                }
            }
            Divider()
            inputBar
        }
    }

    private var header: some View {
        VStack(alignment: .leading, spacing: 2) {
            Text("What do you want to make?").font(DS.font.title)
            Text(store.chatClaudeAvailable
                 ? "Describe an idea, ask a question, or pick a mode. I'll shape it into a run — you approve before anything starts."
                 : "The claude CLI isn't available for chat right now — pick a mode below or press ⌘N to create a run directly.")
                .font(DS.font.caption).foregroundStyle(.secondary)
        }
    }

    private var modeCards: some View {
        LazyVGrid(columns: [GridItem(.adaptive(minimum: 220), spacing: DS.space.xs)],
                  spacing: DS.space.xs) {
            ForEach(ChatMode.all) { mode in
                Button {
                    send(seed: mode)
                } label: {
                    VStack(alignment: .leading, spacing: DS.space.xxs) {
                        HStack(spacing: DS.space.xxs) {
                            Image(systemName: mode.symbol)
                                .foregroundStyle(DS.accent.color)
                            Text(mode.label).font(DS.font.headline)
                            Spacer()
                        }
                        Text(mode.blurb)
                            .font(DS.font.caption).foregroundStyle(.secondary)
                            .multilineTextAlignment(.leading)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    .padding(DS.space.s)
                    .frame(maxWidth: .infinity, alignment: .topLeading)
                    .background(RoundedRectangle(cornerRadius: DS.radius.card,
                                                 style: .continuous)
                        .fill(.quaternary.opacity(0.5)))
                    .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                .accessibilityLabel("\(mode.label): \(mode.blurb)")
            }
        }
    }

    private var inputBar: some View {
        HStack(spacing: DS.space.xs) {
            TextField("Describe an idea, ask a question…", text: $store.chatInput, axis: .vertical)
                .textFieldStyle(.plain)
                .lineLimit(1...4)
                .onSubmit { send() }
            Button { send() } label: {
                Image(systemName: "paperplane.fill")
                    .foregroundStyle(store.chatInput.trimmingCharacters(in: .whitespaces).isEmpty
                                     ? AnyShapeStyle(.tertiary)
                                     : AnyShapeStyle(DS.accent.color))
            }
            .buttonStyle(.plain)
            .disabled(store.chatInput.trimmingCharacters(in: .whitespaces).isEmpty || store.chatThinking)
            .keyboardShortcut(.return, modifiers: .command)
            .accessibilityLabel("Send")
        }
        .padding(DS.space.s)
        .background(.bar)
    }

    // When a mode card is clicked with an empty box, ask the concierge to
    // explain that mode + prompt for the input it needs.
    private func send(seed mode: ChatMode? = nil) {
        var text = store.chatInput.trimmingCharacters(in: .whitespacesAndNewlines)
        if let mode {
            text = text.isEmpty
                ? "I want to use \(mode.label) mode (workflow \(mode.key)). What do you need from me?"
                : "[\(mode.label) mode — workflow \(mode.key)] " + text
        }
        guard !text.isEmpty, !store.chatThinking else { return }
        store.chatInput = ""
        store.chatMessages.append(ConciergeMessage(role: .user, text: text))
        store.chatThinking = true
        let history = store.chatMessages
        let workflows = store.workflows.map { "\($0.name): \($0.title)" }
        // The reply lands on the store, so it survives the user navigating
        // away from Home while the concierge is still thinking.
        let store = self.store
        Task {
            let reply = await OrchestratorStore.conciergeAsk(
                history: history.map { ($0.role == .user ? "USER" : "ASSISTANT", $0.text) },
                workflowList: workflows)
            await MainActor.run {
                store.chatThinking = false
                guard let reply else {
                    store.chatClaudeAvailable = false
                    store.chatMessages.append(ConciergeMessage(
                        role: .concierge,
                        text: "I couldn't reach the claude CLI (is it installed and logged in?). You can still create a run directly with ⌘N — pick a workflow that matches what you're after."))
                    return
                }
                let (clean, suggestion) = RunSuggestion.parse(from: reply)
                store.chatMessages.append(ConciergeMessage(role: .concierge,
                                                 text: clean,
                                                 suggestion: suggestion))
            }
        }
    }

    private func createRun(from s: RunSuggestion) {
        let base = s.name.isEmpty ? String(s.prompt.prefix(40)) : s.name
        var slug = NewAppIntakeSheet.slugify(base)
        if slug.isEmpty { slug = "chat-run" }
        var candidate = slug; var n = 2
        while FileManager.default.fileExists(
            atPath: store.rootURL.appendingPathComponent(candidate).path) {
            candidate = "\(slug)-\(n)"; n += 1
        }
        guard store.createFactoryApp(slug: candidate, idea: s.prompt,
                                     workflow: s.workflow, docs: [],
                                     backfillFromDocs: false) else { return }
        store.chatMessages.append(ConciergeMessage(
            role: .concierge,
            text: "Created **\(candidate)** with the \(s.workflow) workflow and added it to the queue. You can tune per-phase models, rounds, and instructions in its Plan tab before it runs."))
        onOpenProject(candidate)
    }
}

private struct ConciergeBubble: View {
    let message: ConciergeMessage
    let onCreate: (RunSuggestion) -> Void

    var body: some View {
        HStack {
            if message.role == .user { Spacer(minLength: 60) }
            VStack(alignment: .leading, spacing: DS.space.xs) {
                MarkdownBody(text: message.text)
                if let s = message.suggestion {
                    VStack(alignment: .leading, spacing: DS.space.xxs) {
                        HStack(spacing: DS.space.xxs) {
                            Image(systemName: "sparkles")
                                .foregroundStyle(DS.accent.color)
                            Text(s.name.isEmpty ? "Proposed run" : s.name)
                                .font(DS.font.headline)
                            Text(s.workflow)
                                .font(DS.font.caption).foregroundStyle(.secondary)
                                .padding(.horizontal, 5)
                                .background(Capsule().fill(.quaternary.opacity(0.6)))
                        }
                        Text(s.prompt)
                            .font(DS.font.caption).foregroundStyle(.secondary)
                            .lineLimit(4)
                        Button("Create run") { onCreate(s) }
                            .buttonStyle(.borderedProminent)
                            .controlSize(.small)
                    }
                    .padding(DS.space.s)
                    .background(RoundedRectangle(cornerRadius: DS.radius.chip,
                                                 style: .continuous)
                        .fill(DS.accent.fill))
                    .overlay(RoundedRectangle(cornerRadius: DS.radius.chip,
                                              style: .continuous)
                        .stroke(DS.accent.stroke, lineWidth: 1))
                }
            }
            .padding(DS.space.s)
            .background(RoundedRectangle(cornerRadius: DS.radius.card, style: .continuous)
                .fill(message.role == .user
                      ? AnyShapeStyle(DS.accent.fill)
                      : AnyShapeStyle(.quaternary.opacity(0.5))))
            if message.role == .concierge { Spacer(minLength: 60) }
        }
    }
}
