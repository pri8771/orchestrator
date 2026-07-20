import SwiftUI
import UniformTypeIdentifiers

struct TranscriptView: View {
    @EnvironmentObject var store: OrchestratorStore
    let project: Project
    let phaseKey: String
    @State private var lastCount = 0
    @State private var draft = ""
    // Parsed transcript, loaded + refreshed OFF the main thread (the phase .md
    // grows to multi-MB during a live build; reading/parsing it per render
    // beachballed the UI). loadedID gates display so a reused view never shows
    // the previous project/phase's content while the first load is in flight.
    @State private var transcript = PhaseTranscript()
    @State private var loadedID = ""
    @State private var streamPreview: StreamPreview? = nil
    @State private var streamBaselineCount = 0
    @State private var streamLastSeen = Date.distantPast
    // V3 board 1.7: step-in surface state. `stepInPending` flips on send
    // during a live debate and clears ONLY on engine acknowledgment
    // (step_in_joined/step_in_missed events) — never on a timer (R2).
    @State private var stepInPending = false
    @State private var stepInNote: String? = nil

    private var def: PhaseDef? {
        store.phases(for: project).first { $0.key == phaseKey }
            ?? ALL_PHASES.first { $0.key == phaseKey }
    }
    private var transcriptID: String { "\(project.name)/\(project.workflow)/\(phaseKey)" }
    private var isActivePhase: Bool {
        project.currentPhase == phaseKey && project.status == .inProgress
    }

    // V3 board 2.6: land a palette search hit on its turn. The anchor is
    // consumed exactly once, and only when THIS view shows the anchored
    // project+phase with its transcript actually loaded; a turn that isn't
    // in the parsed transcript degrades to the phase-level landing (no
    // scroll) rather than a wrong one.
    private func consumeSearchAnchor(_ t: PhaseTranscript,
                                     proxy: ScrollViewProxy) {
        guard let anchor = store.pendingTranscriptAnchor,
              anchor.project == project.name, anchor.phase == phaseKey,
              loadedID == transcriptID else { return }
        store.pendingTranscriptAnchor = nil
        if let mid = SearchAnchorLogic.messageID(for: anchor, in: t.messages) {
            DispatchQueue.main.async {
                withAnimation { proxy.scrollTo(mid, anchor: .top) }
            }
        }
    }

    var body: some View {
        let t = loadedID == transcriptID ? transcript : PhaseTranscript()
        let phasePurpose = store.phasePurposes(for: project)[phaseKey]
        let artifacts = store.artifacts(for: project, phaseKey: phaseKey)
        let pending = store.pendingHuman(project)
        let nextAgent = project.nextAgent
        let turnState = PaneTurnState.resolve(
            isActive: isActivePhase, agent: nextAgent, live: project.running,
            supportsStreams: nextAgent.map { DS.identity($0).streams } ?? false,
            preview: streamPreview)
        VStack(spacing: 0) {
            header(t)
            Divider()
            Group {
                if !t.exists && pending.isEmpty {
                    emptyPhase
                } else {
                    ScrollViewReader { proxy in
                        ScrollView {
                            LazyVStack(alignment: .leading, spacing: 14) {
                                if t.exists { promptCard(t) }
                                ForEach(Array(t.messages.enumerated()), id: \.element.id) { idx, msg in
                                    if idx == 0 || t.messages[idx - 1].section != msg.section {
                                        SectionDivider(label: msg.section)
                                    }
                                    if let phasePurpose {
                                        MessageBubble(message: msg)
                                            .help(phasePurpose)
                                    } else {
                                        MessageBubble(message: msg)
                                    }
                                }
                                if isActivePhase {
                                    if let workers = store.parallelBuildWorkers(for: project) {
                                        ParallelBuildBanner(workers: workers)
                                    } else {
                                        switch turnState {
                                        case .waiting(let agent, let live):
                                            ThinkingRow(agent: agent, live: live)
                                        case .streaming(let preview):
                                            StreamingRow(preview: preview)
                                        case .final:
                                            EmptyView()
                                        }
                                    }
                                }
                                if !pending.isEmpty { PendingHumanBubble(text: pending) }
                                ForEach(artifacts) { artifact in
                                    ArtifactCard(summary: artifact,
                                                 sourceSession: project.name)
                                }
                                if let fo = t.finalOutput {
                                    FinalOutputCard(
                                        text: fo,
                                        marker: t.marker,
                                        routeTarget: store.routePreviewTarget(
                                            for: project, finalOutput: fo))
                                }
                                Color.clear.frame(height: 1).id("BOTTOM")
                            }
                            .padding(18)
                        }
                        .onChange(of: t.messages.count) { _, c in
                            if c > lastCount { withAnimation { proxy.scrollTo("BOTTOM", anchor: .bottom) } }
                            lastCount = c
                        }
                        .onAppear {
                            lastCount = t.messages.count
                            consumeSearchAnchor(t, proxy: proxy)
                        }
                        .onChange(of: loadedID) { _, _ in
                            consumeSearchAnchor(t, proxy: proxy)
                        }
                    }
                }
            }
            .frame(maxHeight: .infinity)
            Divider()
            stepInStatus
            inputBar
        }
        .task(id: transcriptID) { await pollTranscript() }
        .onAppear { if project.running { store.setFocusedLivePane(project.name) } }
        .onDisappear {
            if store.focusedLivePane == project.name { store.setFocusedLivePane(nil) }
        }
        .onChange(of: store.eventsByProject[project.name] ?? []) { _, events in
            guard let last = events.last(where: {
                $0.kind.hasPrefix("step_in") && $0.phase == phaseKey
            }) else { return }
            switch last.kind {
            case "step_in_joined":
                stepInPending = false
                stepInNote = nil
            case "step_in_missed" where stepInPending:
                stepInPending = false
                stepInNote = last.detail.contains("preserved")
                    ? "The debate ended before you joined — your message stays queued for what runs next."
                    : "The debate moved on before you joined."
            default:
                break
            }
        }
    }

    // Poll the store's async transcript accessor (disk IO + parse happen in a
    // detached task; the mtime cache makes an unchanged file cost one stat).
    // Cancelled and restarted by .task(id:) whenever the project/phase changes.
    private func pollTranscript() async {
        streamPreview = nil
        streamBaselineCount = 0
        streamLastSeen = .distantPast
        while !Task.isCancelled {
            let t = await store.transcript(for: project, phaseKey: phaseKey)
            guard !Task.isCancelled else { return }
            if loadedID != transcriptID {
                lastCount = t.messages.count   // first load: don't auto-scroll
                loadedID = transcriptID
            }
            if t != transcript { transcript = t }
            if isActivePhase, let agent = project.nextAgent,
               !agent.contains("+") {
                let incoming = await store.streamPreview(for: project, agent: agent)
                guard !Task.isCancelled else { return }
                if let incoming {
                    if streamPreview?.turnID != incoming.turnID {
                        streamBaselineCount = t.messages.count
                    }
                    streamPreview = incoming
                    streamLastSeen = Date()
                } else if let current = streamPreview {
                    let transcriptLanded = t.messages.count > streamBaselineCount
                    let wrongAgent = current.agent != agent
                    // The engine unlinks before appending the final block. Hold
                    // the last live text across that tiny gap so it is replaced
                    // atomically on the next parsed transcript. A dead/failed
                    // stream gets at most this bounded grace, then the existing
                    // ThinkingRow resumes (§12.1 loading must end).
                    if transcriptLanded || wrongAgent
                        || Date().timeIntervalSince(streamLastSeen) > 1.25 {
                        streamPreview = nil
                    }
                }
            } else {
                streamPreview = nil
            }
            // V3 board 1.10: 500ms while this pane is the focused LIVE one;
            // a stopped project never earns the fast tick.
            let fast = store.focusedLivePane == project.name && project.running
            try? await Task.sleep(nanoseconds: fast ? 500_000_000 : 1_500_000_000)
        }
    }

    private var inputBar: some View {
        HStack(spacing: 8) {
            ZStack(alignment: .leading) {
                RoundedRectangle(cornerRadius: 8)
                    .fill(Color(nsColor: .textBackgroundColor))
                RoundedRectangle(cornerRadius: 8)
                    .stroke(Color.secondary.opacity(0.25))
                if draft.isEmpty {
                    Text(project.running
                            ? "Join the conversation…"
                            : "Add a note — the agents see it on the next run…")
                        .foregroundStyle(.secondary)
                        .padding(.horizontal, 9)
                        .padding(.vertical, 7)
                        .allowsHitTesting(false)
                }
                TextField("", text: $draft)
                    .textFieldStyle(.plain)
                    .padding(.horizontal, 9)
                    .padding(.vertical, 7)
                    .onSubmit(send)
                    .accessibilityLabel("Message to the agents")
            }
            .frame(height: 36)
            Button(action: send) { Image(systemName: "paperplane.fill") }
                .disabled(draft.trimmingCharacters(in: .whitespaces).isEmpty)
                .keyboardShortcut(.return, modifiers: [])
                .accessibilityLabel("Send message")
                .accessibilityHint("Queues your note for the agents' next turn")
        }
        .padding(10)
    }

    private func send() {
        let text = draft.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty else { return }
        if isActivePhase && project.running {
            // Live debate: marker first (the engine's short wait covers the
            // gap), then the message — it will be drained at the next round
            // barrier and the agents respond to it that round.
            store.requestStepIn(project)
            stepInPending = true
            stepInNote = nil
        }
        store.sendHumanMessage(project, text)
        draft = ""
    }

    // Symbol + word, evidence-based: the countdown derives from persisted
    // round state and clears only on engine events (never a timer).
    @ViewBuilder
    private var stepInStatus: some View {
        if stepInPending {
            HStack(spacing: 6) {
                ProgressView().controlSize(.mini)
                Text("You'll join at round \(project.currentRound + 1) — the agents will respond to you.")
                    .font(.caption).foregroundStyle(.secondary)
                Spacer()
            }
            .padding(.horizontal, 12).padding(.vertical, 6)
            .accessibilityLabel("Joining the debate at round \(project.currentRound + 1)")
        } else if let note = stepInNote {
            HStack(spacing: 6) {
                Image(systemName: "info.circle")
                Text(note).font(.caption)
                Spacer()
                Button("Dismiss") { stepInNote = nil }
                    .buttonStyle(.plain).font(.caption)
                    .foregroundStyle(.secondary)
            }
            .foregroundStyle(.secondary)
            .padding(.horizontal, 12).padding(.vertical, 6)
        }
    }

    private func header(_ t: PhaseTranscript) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: 8) {
            Text(def?.title ?? phaseKey).font(.headline)
            if isActivePhase {
                Text("round \(project.currentRound)").font(.caption).foregroundStyle(.secondary)
            }
            Spacer()
            if let m = t.marker { MarkerBadge(marker: m) }
        }
        .padding(.horizontal, 16).padding(.vertical, 10)
    }

    private func promptCard(_ t: PhaseTranscript) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            if !t.originalPrompt.isEmpty {
                HStack(alignment: .top, spacing: 8) {
                    Image(systemName: "quote.bubble").foregroundStyle(.secondary).font(.caption)
                    VStack(alignment: .leading, spacing: 3) {
                        Text("Original prompt").font(.caption.weight(.medium)).foregroundStyle(.secondary)
                        Text(t.originalPrompt)
                            .font(.callout)
                            .foregroundStyle(.primary)
                            .textSelection(.enabled)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                }
            }
            if !t.purpose.isEmpty {
                HStack(alignment: .top, spacing: 8) {
                    Image(systemName: "target").foregroundStyle(.secondary).font(.caption)
                    Text(t.purpose).font(.callout).foregroundStyle(.secondary)
                }
            }
        }
        .padding(10)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(RoundedRectangle(cornerRadius: 8).fill(Color.secondary.opacity(0.07)))
    }

    private var emptyPhase: some View {
        EmptyStateView(symbol: "hourglass",
                       title: "This phase hasn't started yet",
                       message: project.status == .new
                           ? "Run the project to begin the discussion."
                           : "The agents' conversation appears here when the run reaches this phase.")
    }
}

struct ArtifactCard: View {
    @EnvironmentObject var store: OrchestratorStore
    let summary: ArtifactSummary
    let sourceSession: String
    @State private var showLineage = false

    private var state: ArtifactCardState {
        ArtifactCardState.resolve(summary: summary,
                                  route: store.artifactRouteState(
                                    summary.id, sourceSession: sourceSession))
    }

    private var canDrag: Bool {
        summary.unreadableReason == nil && summary.status == "final" && !summary.stale
    }

    @ViewBuilder
    var body: some View {
        if canDrag, let payload = ArtifactDragPayload(
            artifactID: summary.id, type: summary.type, version: summary.version,
            sourceSession: sourceSession).encode() {
            content.onDrag { NSItemProvider(object: payload as NSString) }
        } else {
            content
        }
    }

    private var content: some View {
        VStack(alignment: .leading, spacing: DS.space.xs) {
            HStack(spacing: DS.space.xs) {
                Image(systemName: Self.glyph(for: summary.type))
                    .foregroundStyle(tint)
                Text(summary.unreadableReason == nil
                     ? summary.type.replacingOccurrences(of: "_", with: " ").capitalized
                     : "Unreadable artifact")
                    .font(DS.font.body.weight(.medium))
                Text("v\(summary.version)")
                    .font(DS.font.monoInline)
                    .padding(.horizontal, DS.space.xxs)
                    .background(Capsule().fill(DS.cardBg))
                Spacer()
                statusChip
                if summary.stale {
                    Text("stale")
                        .font(DS.font.caption.weight(.medium))
                        .foregroundStyle(DS.status.warning.color)
                        .padding(.horizontal, DS.space.xs)
                        .padding(.vertical, DS.space.xxs)
                        .background(Capsule().fill(DS.status.warning.fill))
                }
            }
            if let reason = summary.unreadableReason {
                Text(reason).font(DS.font.caption).foregroundStyle(DS.status.error.color)
            } else {
                HStack(spacing: DS.space.xs) {
                    Button {
                        showLineage.toggle()
                    } label: {
                        Label(Self.lineageLabel(summary.lineage),
                              systemImage: "point.3.connected.trianglepath.dotted")
                            .font(DS.font.caption)
                    }
                    .buttonStyle(.plain)
                    .popover(isPresented: $showLineage) {
                        VStack(alignment: .leading, spacing: DS.space.xs) {
                            Text("Version chain").font(DS.font.headline)
                            ForEach(Array(summary.lineage.enumerated()), id: \.offset) { idx, id in
                                Text("v\(idx + 1) · \(id)").font(DS.font.monoInline)
                            }
                        }
                        .padding(DS.space.m)
                    }
                    Spacer()
                    if summary.canHumanFinalize {
                        Button("Finalize") {
                            store.finalizeArtifact(summary, in: sourceSession)
                        }
                        .controlSize(.small)
                        .disabled(store.artifactFinalizeIsInFlight(
                            summary.id, sourceSession: sourceSession))
                    }
                }
                stateDetail
                if let error = store.artifactFinalizeError(
                        summary.id, sourceSession: sourceSession) {
                    Text(error).font(DS.font.caption).foregroundStyle(DS.status.error.color)
                }
            }
        }
        .padding(DS.space.s)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(RoundedRectangle(cornerRadius: DS.radius.card, style: .continuous)
            .fill(tint.opacity(0.08)))
        .overlay(RoundedRectangle(cornerRadius: DS.radius.card, style: .continuous)
            .stroke(tint.opacity(0.28), lineWidth: 1))
        .accessibilityElement(children: .contain)
        .accessibilityLabel(summary.unreadableReason == nil
            ? "Artifact \(summary.id), version \(summary.version), \(summary.status)"
            : "Unreadable artifact \(summary.id)")
    }

    private var tint: Color {
        switch state {
        case .unreadable, .refused: return DS.status.error.color
        case .stale, .pendingReview: return DS.status.warning.color
        case .routing: return DS.accent.color
        case .final, .converged, .routed: return DS.status.success.color
        }
    }

    private var statusChip: some View {
        Text(summary.status.replacingOccurrences(of: "_", with: " "))
            .font(DS.font.caption.weight(.medium))
            .foregroundStyle(tint)
            .padding(.horizontal, DS.space.xs)
            .padding(.vertical, DS.space.xxs)
            .background(Capsule().fill(tint.opacity(0.14)))
    }

    @ViewBuilder
    private var stateDetail: some View {
        switch state {
        case .routing(let target):
            ProgressView("Routing to \(target)…").font(DS.font.caption)
        case .routed(let target):
            Label("Routed to \(target)", systemImage: "checkmark.circle.fill")
                .font(DS.font.caption).foregroundStyle(DS.status.success.color)
        case .refused(let reason):
            Label(reason, systemImage: "xmark.octagon.fill")
                .font(DS.font.caption).foregroundStyle(DS.status.error.color)
        default:
            EmptyView()
        }
    }

    static func glyph(for type: String) -> String {
        switch type {
        case "idea": return "lightbulb"
        case "research_brief", "opportunity_signal": return "doc.text.magnifyingglass"
        case "spec_bundle": return "shippingbox"
        case "finding_report": return "exclamationmark.magnifyingglass"
        case "gap": return "square.dashed"
        case "reconcile": return "arrow.triangle.merge"
        default: return "doc.richtext"
        }
    }

    static func lineageLabel(_ lineage: [String]) -> String {
        lineage.isEmpty ? "No lineage" : lineage.joined(separator: " → ")
    }
}

// MARK: - Markdown body (shared by bubbles + final-output card)

// Prose runs render with inline Markdown (bold / italic / `code`); fenced
// ``` blocks render monospaced in their own horizontally-scrollable box.
struct MarkdownBody: View {
    let text: String

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            ForEach(Array(MarkdownRenderer.segments(text).enumerated()), id: \.offset) { _, seg in
                if seg.isCode {
                    ScrollView(.horizontal) {
                        Text(seg.text)
                            .font(.system(.callout, design: .monospaced))
                            .textSelection(.enabled)
                            .padding(8)
                    }
                    .background(RoundedRectangle(cornerRadius: 6)
                        .fill(Color.secondary.opacity(0.08)))
                    .overlay(RoundedRectangle(cornerRadius: 6)
                        .stroke(Color.secondary.opacity(0.15)))
                } else {
                    Text(MarkdownRenderer.attributed(seg.text))
                        .font(.callout)
                        .foregroundStyle(.primary)
                        .textSelection(.enabled)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
        }
    }
}

// MARK: - Message bubble

struct MessageBubble: View {
    let message: ChatMessage

    var body: some View {
        HStack(alignment: .top, spacing: 10) {
            Avatar(speaker: message.speaker)
            VStack(alignment: .leading, spacing: 4) {
                HStack(spacing: 6) {
                    Text(message.speaker.display)
                        .font(.callout.weight(.medium))
                        .foregroundStyle(message.speaker.ink)
                    if !message.persona.isEmpty,
                       message.persona.lowercased() != message.speaker.display.lowercased() {
                        Text(message.persona)
                            .font(.caption2)
                            .foregroundStyle(message.speaker.ink)
                            .padding(.horizontal, 7).padding(.vertical, 1)
                            .background(Capsule().fill(message.speaker.fill))
                    }
                    Text(headerDetail).font(.subheadline).foregroundStyle(.secondary)
                }
                // Agents write Markdown — render inline styles and fenced code
                // blocks rather than showing literal syntax (MarkdownBody).
                MarkdownBody(text: message.body)
            }
            Spacer(minLength: 0)
        }
        .padding(.leading, message.speaker == .coordinator ? 8 : 0)
        .overlay(alignment: .leading) {
            if message.speaker == .coordinator {
                RoundedRectangle(cornerRadius: 2)
                    .fill(message.speaker.ink.opacity(0.5)).frame(width: 3)
            }
        }
    }

    // The part of the header after the speaker name, e.g. "decision after round 1".
    private var headerDetail: String {
        let parts = message.header.components(separatedBy: "—")
        if parts.count > 1 { return parts.dropFirst().joined(separator: "—").trimmingCharacters(in: .whitespaces) }
        return message.section
    }
}

// A human message that's queued in the inbox but not yet folded into the run.
struct PendingHumanBubble: View {
    let text: String
    var body: some View {
        HStack(alignment: .top, spacing: 10) {
            Avatar(speaker: .human)
            VStack(alignment: .leading, spacing: 4) {
                HStack(spacing: 6) {
                    Text("You").font(.callout.weight(.medium))
                        .foregroundStyle(Speaker.human.ink)
                    Text("queued").font(.subheadline).foregroundStyle(.secondary)
                    Image(systemName: "clock").font(.footnote).foregroundStyle(.secondary)
                }
                Text(MarkdownRenderer.attributed(text)).font(.callout).foregroundStyle(.primary)
                    .fixedSize(horizontal: false, vertical: true)
            }
            Spacer(minLength: 0)
        }
        .padding(10)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(RoundedRectangle(cornerRadius: 10).fill(Speaker.human.fill.opacity(0.5)))
    }
}

struct Avatar: View {
    let speaker: Speaker
    var body: some View {
        Text(speaker.initials)
            .font(DS.font.caption.weight(.medium))
            .foregroundStyle(speaker.ink)
            .frame(width: 28, height: 28)
            .background(Circle().fill(speaker.fill))
    }
}

struct SectionDivider: View {
    let label: String
    var body: some View {
        HStack {
            Spacer()
            Text(label.isEmpty ? "—" : label)
                .font(.subheadline).foregroundStyle(.secondary)
                .padding(.horizontal, 10).padding(.vertical, 2)
                .background(Capsule().fill(Color.secondary.opacity(0.12)))
            Spacer()
        }
        .padding(.vertical, 2)
    }
}

// Proof-of-parallelism banner: shown instead of the single-agent ThinkingRow the
// moment next_agent is a "+"-joined roster (a real parallel build fan-out, not a
// sequential turn). Every chip appears at once — the visual contrast with the
// single-name-at-a-time ThinkingRow IS the evidence the agents are building
// concurrently, not in series. `done` per chip comes from the actual per-call
// logs, so a chip flips to a checkmark the moment that specific worker finishes.
struct ParallelBuildBanner: View {
    let workers: [BuildWorker]
    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 6) {
                // Accent, not amber: parallelism is activity, not a warning.
                Image(systemName: "bolt.fill").font(.subheadline)
                    .foregroundStyle(DS.accent.color)
                Text("\(workers.count) agents building in parallel")
                    .font(.callout.weight(.medium)).foregroundStyle(.secondary)
            }
            HStack(spacing: 8) {
                ForEach(workers) { w in
                    HStack(spacing: 6) {
                        Avatar(speaker: w.speaker)
                        VStack(alignment: .leading, spacing: 1) {
                            Text(w.label).font(.subheadline.weight(.medium))
                            if w.done {
                                Label("done", systemImage: "checkmark")
                                    .font(.caption2)
                                    .foregroundStyle(DS.status.success.color)
                            } else {
                                HStack(spacing: 4) {
                                    PulseDot(color: w.speaker.ink)
                                    Text("building…").font(.caption2).foregroundStyle(.secondary)
                                }
                            }
                        }
                    }
                    .padding(.horizontal, 8).padding(.vertical, 6)
                    .background(RoundedRectangle(cornerRadius: 8)
                        .fill(w.speaker.fill.opacity(w.done ? 0.35 : 0.8)))
                    .accessibilityElement(children: .ignore)
                    .accessibilityLabel(w.label)
                    .accessibilityValue(w.done ? "done" : "building")
                }
            }
        }
        .padding(10)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(RoundedRectangle(cornerRadius: 10).fill(Color.secondary.opacity(0.06)))
    }
}

struct ThinkingRow: View {
    let agent: String
    let live: Bool
    private var speaker: Speaker { Speaker(rawValue: agent) ?? .system }
    var body: some View {
        HStack(spacing: 10) {
            Avatar(speaker: speaker)
            if live {
                HStack(spacing: 6) {
                    Text("\(speaker.display) is thinking")
                        .font(.callout).italic().foregroundStyle(.secondary)
                    PulseDot(color: speaker.ink)
                }
            } else {
                Text("waiting on \(speaker.display)")
                    .font(.callout).italic().foregroundStyle(.secondary)
            }
            Spacer()
        }
        .accessibilityElement(children: .combine)
    }
}

struct StreamingRow: View {
    let preview: StreamPreview
    private var identity: DS.AgentIdentity { DS.identity(preview.agent) }

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 8) {
                AgentAvatar(identity: identity, size: 24)
                Text("\(identity.displayName) is responding live")
                    .font(.callout.weight(.medium))
                BreathingDot(color: identity.tint.color)
                Spacer()
            }
            MarkdownBody(text: preview.text)
                .frame(maxWidth: .infinity, alignment: .leading)
        }
        .padding(12)
        .background(RoundedRectangle(cornerRadius: 10)
            .fill(identity.tint.fill.opacity(0.55)))
        .overlay(RoundedRectangle(cornerRadius: 10)
            .stroke(identity.tint.color.opacity(0.25)))
        .accessibilityElement(children: .combine)
        .accessibilityLabel("Live response from \(identity.displayName)")
    }
}

struct FinalOutputCard: View {
    let text: String
    let marker: String?
    var routeTarget: String? = nil
    // Green tint over the SYSTEM background (not a fixed light mint) so the
    // card — and its .primary text — stays readable in Dark Mode too.
    private let tint = DS.status.success.color
    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(spacing: 6) {
                Image(systemName: "flag.checkered").font(.caption)
                Text("Final output").font(.callout.weight(.medium))
                Spacer()
                if let m = marker { MarkerBadge(marker: m) }
            }
            if let routeTarget {
                RoutePreviewChip(target: routeTarget)
            }
            MarkdownBody(text: text)
        }
        .padding(12)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(RoundedRectangle(cornerRadius: 10).fill(tint.opacity(0.10)))
        .overlay(RoundedRectangle(cornerRadius: 10).stroke(tint.opacity(0.3)))
    }
}

struct RoutePreviewChip: View {
    let target: String
    var body: some View {
        Text("→ would route to \(target.replacingOccurrences(of: "_", with: " ").capitalized)")
            .font(DS.font.caption.weight(.medium))
            .foregroundStyle(DS.accent.color)
            .padding(.horizontal, DS.space.xs)
            .padding(.vertical, DS.space.xxs)
            .background(Capsule().fill(DS.accent.fill))
            .accessibilityLabel("Default route preview: would route to \(target)")
    }
}

struct MarkerBadge: View {
    let marker: String
    private var positive: Bool { marker.contains("YES") }
    // Mid-tone foregrounds legible on BOTH light and dark backgrounds (the old
    // near-black greens/browns vanished in Dark Mode).
    private var tint: Color {
        positive ? DS.status.success.color : DS.status.warning.color
    }
    var body: some View {
        Text(marker)
            .font(.footnote.weight(.medium))
            .foregroundStyle(tint)
            .padding(.horizontal, 8).padding(.vertical, 2)
            .background(Capsule().fill(tint.opacity(0.16)))
    }
}
