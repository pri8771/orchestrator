import SwiftUI
import AppKit

// MARK: - Design tokens ("Native Pro", DESIGN-NATIVE-PRO.md §2)
//
// ALL custom colors and font sizes live in this file — a CI grep
// (ci_style_check.sh, run by StyleGuardTests) rejects `Color(red:` and
// hardcoded font sizes anywhere else. Every owned color is a dynamic
// NSColor(name:dynamicProvider:) pair, so light/dark, vibrancy, and
// increased-contrast come out correct without per-view logic.
//
// Grammar (§6): accent = running · green = success · amber = warning/stall/
// quality · purple = fallback, EXCLUSIVELY · red = error/failed ·
// gray = idle/cooldown. Identity hues never carry state; state colors always
// win; no color-only encoding — every status rendering is symbol + word.

enum DS {

    // MARK: §2.1 Surfaces & text — system semantics, no hex, ever

    /// Window canvas.
    static let windowBg = Color(nsColor: .windowBackgroundColor)
    /// Cards, tables, grid.
    static let cardBg = Color(nsColor: .controlBackgroundColor)
    /// Hovered rows, wells — a quaternary fill-step over cardBg (elevation is
    /// by fill-step, never by shadow, §2.4).
    static var raised: AnyShapeStyle { AnyShapeStyle(.quaternary) }
    /// Transcript/log wells.
    static var insetBg: AnyShapeStyle { AnyShapeStyle(.quaternary.opacity(0.5)) }
    static let textPrimary = Color(nsColor: .labelColor)
    static let textSecondary = Color(nsColor: .secondaryLabelColor)
    static let textTertiary = Color(nsColor: .tertiaryLabelColor)
    /// 1px borders and gridlines — always 1px, never 2 (§2.4).
    static let hairline = Color(nsColor: .separatorColor)

    // MARK: §2.2 Owned palette (light / dark pairs; text-on-surface ≥ 4.5:1)

    /// One tint, three renderings — the §2.2 formula: fills at 8% (light) /
    /// 12% (dark), strokes at 25%, content at 100%. Applies to status pills,
    /// assignment chips, filter chips, agent avatars.
    struct Tint {
        let light: UInt32
        let dark: UInt32

        /// Content at 100%.
        var color: Color { DS.dynamic(light: light, dark: dark) }
        /// Fill at 8% light / 12% dark.
        var fill: Color {
            DS.dynamic(light: light, dark: dark, lightAlpha: 0.08, darkAlpha: 0.12)
        }
        /// Stroke at 25%.
        var stroke: Color {
            DS.dynamic(light: light, dark: dark, lightAlpha: 0.25, darkAlpha: 0.25)
        }
    }

    /// "Conductor Indigo" — selection, primary buttons, current-phase fill,
    /// the RUNNING state (status blue is retired), modified-setting dots.
    static let accent = Tint(light: 0x4A56C7, dark: 0x8A93F2)

    /// Agent identity hues — identity ONLY, never state.
    enum agent {
        /// Terracotta.
        static let claude = Tint(light: 0xC4643B, dark: 0xE08D63)
        /// Teal.
        static let codex = Tint(light: 0x0E7E74, dark: 0x4DB8AC)
        /// Cobalt.
        static let gemini = Tint(light: 0x3467D6, dark: 0x7CA2F0)
        /// Slate (local / Ollama).
        static let local = Tint(light: 0x5B6472, dark: 0x9AA3B2)
    }

    enum status {
        /// Completed, healthy.
        static let success = Tint(light: 0x1E7F3C, dark: 0x46C065)
        /// Amber: stalls, quality-floor breaches, cooldown-forecast — NEVER fallback.
        static let warning = Tint(light: 0xB25E09, dark: 0xE8A13D)
        /// Purple: exclusively "a fallback happened / is active". Never decorative.
        static let fallback = Tint(light: 0x8250DF, dark: 0xB394F5)
        /// Timeouts, failures.
        static let error = Tint(light: 0xC4302B, dark: 0xEF6E63)
        /// Idle, waiting, cooldown text.
        static let idle = Color(nsColor: .secondaryLabelColor)
        /// Gray tint pair for idle/cooldown pills (fill/stroke need a
        /// concrete pair; content text should usually prefer `idle`).
        static let idlePill = Tint(light: 0x6E6E73, dark: 0x98989D)
    }

    // MARK: §2.3 Type ramp (SF Pro; SF Mono ONLY for machine output, model
    // ids, file paths, and elapsed-time counters. Semibold is the maximum
    // in-window weight.)

    enum font {
        /// 26 semibold — empty states only.
        static let largeTitle = Font.system(size: 26, weight: .semibold)
        /// 20 semibold — sheet/screen titles.
        static let title = Font.system(size: 20, weight: .semibold)
        /// 15 semibold — card titles, project names.
        static let headline = Font.system(size: 15, weight: .semibold)
        /// 13 regular — the workhorse.
        static let body = Font.system(size: 13)
        /// 12 medium — chip labels, cell model names.
        static let callout = Font.system(size: 12, weight: .medium)
        /// 11 regular — metadata, footnotes, timestamps.
        static let caption = Font.system(size: 11)
        /// 10 medium — micro-badges and rank numerals in dense rows. The
        /// sanctioned floor: nothing in the app renders below 10pt.
        static let caption2 = Font.system(size: 10, weight: .medium)
        /// 28 SF Pro Rounded semibold — Overview stat tiles. Pair with
        /// `.monospacedDigit()` on every live numeral.
        static let stat = Font.system(size: 28, weight: .semibold, design: .rounded)
        /// 11.5 SF Mono regular — log/transcript wells (1.45 line height via
        /// `.lineSpacing(DS.font.monoWellLineSpacing)`).
        static let monoWell = Font.system(size: 11.5, design: .monospaced)
        static let monoWellLineSpacing: CGFloat = 11.5 * 0.45
        /// 12 SF Mono medium — model IDs, paths, timers (tabular digits).
        static let monoInline = Font.system(size: 12, weight: .medium, design: .monospaced)
        /// 10.5 SF Mono — model IDs inside dense chips (fallback-ladder rungs).
        static let monoCaption = Font.system(size: 10.5, design: .monospaced)
        /// 48pt symbol for centered empty states (§4.1).
        static let emptyStateIcon = Font.system(size: 48)
    }

    /// Stable project hue: a deterministic pick from the owned palette for
    /// project monogram avatars (identity, never state).
    static func projectTint(_ name: String) -> Tint {
        let pool = [accent, agent.claude, agent.codex, agent.gemini, agent.local]
        var hash: UInt64 = 1125899906842597
        for b in name.utf8 { hash = hash &* 31 &+ UInt64(b) }
        return pool[Int(hash % UInt64(pool.count))]
    }

    // MARK: §2.4 Spacing (4pt base grid), radii (continuous), motion

    enum space {
        static let xxs: CGFloat = 4
        static let xs: CGFloat = 8
        static let s: CGFloat = 12
        static let m: CGFloat = 16
        static let l: CGFloat = 24
        static let xl: CGFloat = 32
        /// 24pt content margins, 16pt between zones, 8pt intra-card.
        static let margin: CGFloat = 24
        static let zone: CGFloat = 16
        static let intraCard: CGFloat = 8
    }

    enum radius {
        /// Badges / controls.
        static let control: CGFloat = 6
        /// Chips.
        static let chip: CGFloat = 8
        /// Cards, sheets.
        static let card: CGFloat = 12
    }

    /// The one spring (response 0.35, damping 0.8) for chips/presets.
    static let spring = Animation.spring(response: 0.35, dampingFraction: 0.8)
    /// Status-dot breathing period.
    static let breathing: Double = 1.6
    /// Fallback pulse: once, then steady.
    static let fallbackPulse: Double = 0.6

    // MARK: - AgentIdentity map (tint / symbol / effort capability)

    struct AgentIdentity {
        let key: String
        let displayName: String
        let monogram: String
        let symbol: String
        let tint: Tint
        /// Whether this agent's CLI exposes a reasoning-effort control
        /// (Claude `--effort`, Codex `model_reasoning_effort`). Gemini and
        /// local models render effort UI disabled at 40% — "no effort control".
        let supportsEffort: Bool
    }

    static let agentIdentities: [String: AgentIdentity] = [
        "claude": AgentIdentity(key: "claude", displayName: "Claude", monogram: "Cl",
                                symbol: "asterisk", tint: agent.claude, supportsEffort: true),
        "codex": AgentIdentity(key: "codex", displayName: "Codex", monogram: "Cx",
                               symbol: "chevron.left.forwardslash.chevron.right",
                               tint: agent.codex, supportsEffort: true),
        "gemini": AgentIdentity(key: "gemini", displayName: "Gemini", monogram: "Gm",
                                symbol: "diamond", tint: agent.gemini, supportsEffort: false),
        "ollama": AgentIdentity(key: "ollama", displayName: "Local", monogram: "Lo",
                                symbol: "desktopcomputer", tint: agent.local,
                                supportsEffort: false),
    ]

    /// Identity lookup tolerant of roster slugs ("codex-a") and the "local"
    /// alias; unknown agents get a neutral slate identity.
    static func identity(_ rawKey: String) -> AgentIdentity {
        let key = rawKey.lowercased()
        if let hit = agentIdentities[key] { return hit }
        if key.hasPrefix("local") { return agentIdentities["ollama"]! }
        for (k, v) in agentIdentities where key.hasPrefix(k) { return v }
        return AgentIdentity(key: rawKey, displayName: rawKey.capitalized,
                             monogram: String(rawKey.prefix(2)).capitalized,
                             symbol: "questionmark", tint: agent.local,
                             supportsEffort: false)
    }

    // MARK: - Dynamic color plumbing

    /// A light/dark NSColor pair as one dynamic color (resolves per appearance,
    /// including inside vibrancy and increased-contrast).
    static func dynamic(light: UInt32, dark: UInt32,
                        lightAlpha: CGFloat = 1, darkAlpha: CGFloat = 1) -> Color {
        Color(nsColor: NSColor(name: nil) { appearance in
            let isDark = appearance.bestMatch(from: [.aqua, .darkAqua]) == .darkAqua
            return NSColor(dsHex: isDark ? dark : light,
                           alpha: isDark ? darkAlpha : lightAlpha)
        })
    }
}

extension NSColor {
    /// 0xRRGGBB → NSColor. Internal to the token file — nothing outside
    /// ThemeTokens.swift constructs colors from raw components.
    convenience init(dsHex hex: UInt32, alpha: CGFloat = 1) {
        self.init(srgbRed: CGFloat((hex >> 16) & 0xFF) / 255,
                  green: CGFloat((hex >> 8) & 0xFF) / 255,
                  blue: CGFloat(hex & 0xFF) / 255,
                  alpha: alpha)
    }
}
