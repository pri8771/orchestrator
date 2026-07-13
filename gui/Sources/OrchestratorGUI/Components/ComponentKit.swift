import SwiftUI

// MARK: - Native Pro component kit (DESIGN-NATIVE-PRO.md M0)
//
// Pure views only — no store access, no file IO — so every piece previews in
// both schemes and drops into any surface. All colors and fonts come from
// ThemeTokens.swift (DS); the CI style guard keeps it that way.

// MARK: - StatusPill
//
// The one state vocabulary at every altitude (§6): always symbol + word,
// never color alone. Purple means fallback, exclusively.

enum StatusKind {
    case running
    case success
    case warning
    case fallback
    case error
    case idle

    var tint: DS.Tint {
        switch self {
        case .running: return DS.accent
        case .success: return DS.status.success
        case .warning: return DS.status.warning
        case .fallback: return DS.status.fallback
        case .error: return DS.status.error
        case .idle: return DS.status.idlePill
        }
    }

    var symbol: String {
        switch self {
        case .running: return "play.fill"
        case .success: return "checkmark"
        case .warning: return "exclamationmark.triangle.fill"
        case .fallback: return "arrow.uturn.down"
        case .error: return "xmark.octagon.fill"
        case .idle: return "pause.fill"
        }
    }
}

/// Tinted capsule: fill 8/12%, stroke 25%, content 100% (§2.2 formula).
/// `label` carries the word ("Running 02:14", "FALLBACK → qwen2.5-7b").
struct StatusPill: View {
    let kind: StatusKind
    let label: String
    /// Optional custom symbol (defaults to the kind's).
    var symbol: String? = nil
    /// Breathing dot for live "running" pills (Reduce Motion → static).
    var breathing: Bool = false

    var body: some View {
        HStack(spacing: DS.space.xxs) {
            if breathing {
                BreathingDot(color: kind.tint.color)
            } else {
                Image(systemName: symbol ?? kind.symbol)
                    .font(DS.font.caption)
                    .accessibilityHidden(true)
            }
            Text(label)
                .font(DS.font.callout)
                .monospacedDigit()
                .lineLimit(1)
        }
        .foregroundStyle(kind.tint.color)
        .padding(.horizontal, DS.space.xs)
        .padding(.vertical, 3)
        .background(Capsule().fill(kind.tint.fill))
        .overlay(Capsule().stroke(kind.tint.stroke, lineWidth: 1))
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(label)
    }
}

/// 1.6s breathing status dot (§2.4 motion). Honors Reduce Motion.
struct BreathingDot: View {
    let color: Color
    var size: CGFloat = 6
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @State private var dim = false

    var body: some View {
        Circle()
            .fill(color)
            .frame(width: size, height: size)
            .opacity(dim ? 0.35 : 1)
            .animation(reduceMotion ? nil
                       : .easeInOut(duration: DS.breathing / 2).repeatForever(autoreverses: true),
                       value: dim)
            .onAppear { if !reduceMotion { dim = true } }
            .accessibilityHidden(true)
    }
}

// MARK: - AgentAvatar
//
// Agent-tinted rounded rect with the identity monogram (or SF symbol).
// Identity hues never carry state (§2.2 hard rule).

struct AgentAvatar: View {
    let identity: DS.AgentIdentity
    var size: CGFloat = 20
    var useSymbol: Bool = false

    init(identity: DS.AgentIdentity, size: CGFloat = 20, useSymbol: Bool = false) {
        self.identity = identity
        self.size = size
        self.useSymbol = useSymbol
    }

    init(agent: String, size: CGFloat = 20, useSymbol: Bool = false) {
        self.init(identity: DS.identity(agent), size: size, useSymbol: useSymbol)
    }

    var body: some View {
        Group {
            if useSymbol {
                Image(systemName: identity.symbol)
                    .font(.system(size: size * 0.5, weight: .medium))
            } else {
                Text(identity.monogram)
                    .font(.system(size: size * 0.42, weight: .medium))
            }
        }
        .foregroundStyle(identity.tint.color)
        .frame(width: size, height: size)
        .background(
            RoundedRectangle(cornerRadius: size * 0.3, style: .continuous)
                .fill(identity.tint.fill))
        .overlay(
            RoundedRectangle(cornerRadius: size * 0.3, style: .continuous)
                .stroke(identity.tint.stroke, lineWidth: 1))
        .accessibilityLabel(identity.displayName)
    }
}

// MARK: - EffortGauge
//
// Three ascending bars (3/6/9pt), 1/2/3 filled = Low/Med/High (§5.2). Filled
// bars in the agent tint; unfilled in the tint's stroke shade. Renders dimmed
// with no fill for agents without an effort control.

enum EffortLevel: Int, CaseIterable {
    case low = 1, medium = 2, high = 3

    var label: String {
        switch self {
        case .low: return "Low"
        case .medium: return "Medium"
        case .high: return "High"
        }
    }

    init?(configValue: String) {
        switch configValue.lowercased() {
        case "low": self = .low
        case "medium": self = .medium
        case "high": self = .high
        default: return nil
        }
    }
}

struct EffortGauge: View {
    let level: EffortLevel?
    var tint: DS.Tint = DS.accent

    private let heights: [CGFloat] = [3, 6, 9]

    var body: some View {
        HStack(alignment: .bottom, spacing: 2) {
            ForEach(0..<3, id: \.self) { i in
                RoundedRectangle(cornerRadius: 1, style: .continuous)
                    .fill(filled(i) ? tint.color : tint.stroke)
                    .frame(width: 3, height: heights[i])
            }
        }
        .opacity(level == nil ? 0.4 : 1)
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(level.map { "\($0.label) effort" } ?? "No effort control")
    }

    private func filled(_ index: Int) -> Bool {
        guard let level else { return false }
        return index < level.rawValue
    }
}

// MARK: - EffortPicker (§5.3)
//
// One shared component everywhere effort is set: segmented Low/Med/High with
// the EffortGauge bar glyphs. The footnote names the exact serialization —
// mechanism visible, never mysterious. Agents without an effort control render
// it disabled at 40% with a caption.

struct EffortPicker: View {
    /// nil = inherit (no explicit level chosen).
    @Binding var level: EffortLevel?
    /// The agent key, for capability + the serialization footnote.
    var agent: String = "claude"
    var showFootnote: Bool = true

    private var identity: DS.AgentIdentity { DS.identity(agent) }
    private var supported: Bool { identity.supportsEffort }

    var body: some View {
        VStack(alignment: .leading, spacing: DS.space.xxs) {
            HStack(spacing: DS.space.xxs) {
                ForEach(EffortLevel.allCases, id: \.rawValue) { l in
                    Button {
                        level = (level == l) ? nil : l
                    } label: {
                        HStack(spacing: DS.space.xxs) {
                            EffortGauge(level: l, tint: level == l ? identity.tint : DS.status.idlePill)
                            Text(l.label).font(DS.font.callout)
                        }
                        .padding(.horizontal, DS.space.xs)
                        .padding(.vertical, 3)
                        .background(RoundedRectangle(cornerRadius: DS.radius.control, style: .continuous)
                            .fill(level == l ? AnyShapeStyle(identity.tint.fill)
                                             : AnyShapeStyle(.quaternary.opacity(0.5))))
                        .overlay(RoundedRectangle(cornerRadius: DS.radius.control, style: .continuous)
                            .stroke(level == l ? identity.tint.stroke : DS.hairline, lineWidth: 1))
                        .contentShape(Rectangle())
                    }
                    .buttonStyle(.plain)
                    .accessibilityLabel("\(l.label) effort")
                    .accessibilityAddTraits(level == l ? .isSelected : [])
                }
            }
            .disabled(!supported)
            .opacity(supported ? 1 : 0.4)
            if showFootnote {
                Text(footnote)
                    .font(DS.font.caption)
                    .foregroundStyle(.tertiary)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .accessibilityElement(children: .contain)
        .accessibilityLabel("Effort for \(identity.displayName): \(level?.label ?? (supported ? "inherit" : "no effort control"))")
    }

    private var footnote: String {
        switch agent {
        case "claude": return "Serialized as claude --effort low|medium|high."
        case "codex": return "Serialized as codex model_reasoning_effort."
        default: return "This agent has no effort control."
        }
    }
}

// MARK: - Sparkline (a 20-line Path, §2.4)
//
// 5-point output sparkline of recent response lengths and friends. Pure
// geometry — anomaly tagging is the caller's job (amber "Short output" chip).

struct Sparkline: View {
    let values: [Double]
    var tint: Color = DS.accent.color

    var body: some View {
        GeometryReader { geo in
            let pts = points(in: geo.size)
            if pts.count > 1 {
                Path { p in
                    p.move(to: pts[0])
                    for pt in pts.dropFirst() { p.addLine(to: pt) }
                }
                .stroke(tint, style: StrokeStyle(lineWidth: 1.5, lineCap: .round,
                                                 lineJoin: .round))
            }
        }
        .accessibilityHidden(true)   // callers pair it with a textual value
    }

    private func points(in size: CGSize) -> [CGPoint] {
        guard values.count > 1 else { return [] }
        let lo = values.min() ?? 0
        let hi = values.max() ?? 1
        let span = max(hi - lo, 0.000001)
        let stepX = size.width / CGFloat(values.count - 1)
        return values.enumerated().map { i, v in
            CGPoint(x: CGFloat(i) * stepX,
                    y: size.height - CGFloat((v - lo) / span) * size.height)
        }
    }
}

// MARK: - StatTile (§4.1 Overview stat row)
//
// 28pt SF Pro Rounded semibold value, 11pt secondary label, no boxes —
// hairline separation is the container's job.

struct StatTile: View {
    let value: String
    let label: String
    var tint: Color = DS.textPrimary

    var body: some View {
        VStack(alignment: .leading, spacing: DS.space.xxs) {
            Text(value)
                .font(DS.font.stat)
                .monospacedDigit()
                .foregroundStyle(tint)
                .contentTransition(.numericText())
            Text(label)
                .font(DS.font.caption)
                .foregroundStyle(DS.textSecondary)
        }
        .frame(minWidth: 100, alignment: .leading)
        .accessibilityElement(children: .ignore)
        .accessibilityLabel("\(label): \(value)")
    }
}

// MARK: - Chip (filter chips, preset chips)
//
// 8pt radius, the one tint formula, the one spring (§2.4).

struct Chip: View {
    let label: String
    var systemImage: String? = nil
    var selected: Bool = false
    var tint: DS.Tint = DS.accent
    var action: (() -> Void)? = nil

    var body: some View {
        Button {
            action?()
        } label: {
            HStack(spacing: DS.space.xxs) {
                if let systemImage {
                    Image(systemName: systemImage)
                        .font(DS.font.caption)
                        .accessibilityHidden(true)
                }
                Text(label)
                    .font(DS.font.callout)
                    .lineLimit(1)
            }
            .foregroundStyle(selected ? tint.color : DS.textSecondary)
            .padding(.horizontal, DS.space.xs)
            .padding(.vertical, DS.space.xxs)
            .background(
                RoundedRectangle(cornerRadius: DS.radius.chip, style: .continuous)
                    .fill(selected ? AnyShapeStyle(tint.fill) : AnyShapeStyle(.quaternary.opacity(0.5))))
            .overlay(
                RoundedRectangle(cornerRadius: DS.radius.chip, style: .continuous)
                    .stroke(selected ? tint.stroke : DS.hairline, lineWidth: 1))
        }
        .buttonStyle(.plain)
        .animation(DS.spring, value: selected)
        .accessibilityLabel(label)
        .accessibilityAddTraits(selected ? .isSelected : [])
    }
}

// MARK: - InlineBanner
//
// The one banner (DESIGN-REFRESH.md §6): status-tint fill (8/12% formula),
// 3pt leading accent bar — the same vocabulary as fallback/error event rows —
// body-medium title, caption message, and a trailing action slot. Stacked
// banners separate with the built-in bottom hairline and read as one system.

struct InlineBanner<Trailing: View>: View {
    let kind: StatusKind
    /// Optional custom symbol (defaults to the kind's).
    var symbol: String? = nil
    let title: String
    var message: String? = nil
    /// Extra lines allowed for the message (errors can be long).
    var messageLineLimit: Int? = 4
    @ViewBuilder var trailing: Trailing

    var body: some View {
        HStack(alignment: .top, spacing: DS.space.xs) {
            Image(systemName: symbol ?? kind.symbol)
                .font(DS.font.body)
                .foregroundStyle(kind.tint.color)
                .accessibilityHidden(true)
                .padding(.top, 1)
            VStack(alignment: .leading, spacing: 2) {
                Text(title)
                    .font(DS.font.body.weight(.medium))
                    .fixedSize(horizontal: false, vertical: true)
                if let message, !message.isEmpty {
                    Text(message)
                        .font(DS.font.caption)
                        .foregroundStyle(DS.textSecondary)
                        .textSelection(.enabled)
                        .lineLimit(messageLineLimit)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
            .accessibilityElement(children: .combine)
            Spacer(minLength: DS.space.xs)
            trailing
        }
        .padding(.horizontal, DS.space.m)
        .padding(.vertical, 10)
        .background(kind.tint.fill)
        .overlay(alignment: .leading) {
            Rectangle().fill(kind.tint.color).frame(width: 3)
                .accessibilityHidden(true)
        }
        .overlay(Divider(), alignment: .bottom)
    }
}

extension InlineBanner where Trailing == EmptyView {
    init(kind: StatusKind, symbol: String? = nil, title: String,
         message: String? = nil, messageLineLimit: Int? = 4) {
        self.init(kind: kind, symbol: symbol, title: title, message: message,
                  messageLineLimit: messageLineLimit) { EmptyView() }
    }
}

// MARK: - EmptyStateView
//
// Every full-pane empty state is an instance (DESIGN-REFRESH.md §6): the
// symbol, a title, what will appear here and why it's empty, and at most one
// action. `prominent` is the app-level landing state (largeTitle, §4.1).

struct EmptyStateView: View {
    let symbol: String
    let title: String
    var message: String? = nil
    var actionLabel: String? = nil
    var action: (() -> Void)? = nil
    var prominent: Bool = false

    var body: some View {
        VStack(spacing: DS.space.s) {
            Image(systemName: symbol)
                .font(DS.font.emptyStateIcon)
                .symbolRenderingMode(.hierarchical)
                .foregroundStyle(.tertiary)
                .accessibilityHidden(true)
            Text(title)
                .font(prominent ? DS.font.largeTitle : DS.font.headline)
            if let message {
                Text(message)
                    .font(prominent ? DS.font.body : DS.font.caption)
                    .foregroundStyle(DS.textSecondary)
                    .multilineTextAlignment(.center)
                    .fixedSize(horizontal: false, vertical: true)
                    .frame(maxWidth: 380)
            }
            if let actionLabel, let action {
                Button(actionLabel, action: action)
                    .buttonStyle(.borderedProminent)
                    .padding(.top, DS.space.xxs)
            }
        }
        .padding(DS.space.margin)
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        // No .combine here: the optional action button must stay individually
        // focusable for VoiceOver; the texts already read in order.
    }
}

// MARK: - Previews (both schemes)

private struct ComponentKitGallery: View {
    var body: some View {
        VStack(alignment: .leading, spacing: DS.space.m) {
            HStack(spacing: DS.space.xs) {
                StatusPill(kind: .running, label: "Running 02:14", breathing: true)
                StatusPill(kind: .success, label: "Verified")
                StatusPill(kind: .warning, label: "Stalled 4 min")
                StatusPill(kind: .fallback, label: "FALLBACK → qwen2.5-7b")
                StatusPill(kind: .error, label: "Timed out")
                StatusPill(kind: .idle, label: "Cooldown 0:45")
            }
            HStack(spacing: DS.space.xs) {
                ForEach(["claude", "codex", "gemini", "ollama"], id: \.self) {
                    AgentAvatar(agent: $0, size: 24)
                }
                AgentAvatar(agent: "claude", size: 24, useSymbol: true)
            }
            HStack(spacing: DS.space.m) {
                EffortGauge(level: .low, tint: DS.agent.claude)
                EffortGauge(level: .medium, tint: DS.agent.codex)
                EffortGauge(level: .high, tint: DS.accent)
                EffortGauge(level: nil, tint: DS.agent.gemini)
            }
            Sparkline(values: [820, 640, 910, 18, 780], tint: DS.agent.codex.color)
                .frame(width: 60, height: 16)
            HStack(spacing: DS.space.l) {
                StatTile(value: "3", label: "Running", tint: DS.accent.color)
                StatTile(value: "7", label: "Queued")
                StatTile(value: "2", label: "Needs Attention", tint: DS.status.warning.color)
                StatTile(value: "12", label: "Done Today", tint: DS.status.success.color)
            }
            HStack(spacing: DS.space.xs) {
                Chip(label: "Balanced", selected: true)
                Chip(label: "Max Quality")
                Chip(label: "Economy")
                Chip(label: "Fallbacks", systemImage: "arrow.uturn.down",
                     selected: true, tint: DS.status.fallback)
            }
            InlineBanner(kind: .warning, symbol: "pause.circle.fill",
                         title: "Paused for your approval",
                         message: "Finished Tech Specs. Approve to continue.") {
                Button("Approve") {}
            }
            InlineBanner(kind: .error, title: "Run aborted",
                         message: "codex exited 1 — see the run log.")
        }
        .padding(DS.space.margin)
        .background(DS.windowBg)
    }
}

#Preview("Component kit — light") {
    ComponentKitGallery().preferredColorScheme(.light)
}

#Preview("Component kit — dark") {
    ComponentKitGallery().preferredColorScheme(.dark)
}
