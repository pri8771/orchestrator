<!-- keywords: design, tokens, theme, theme.swift, typography, typographic, fonts, spacing, grid, palette, colors, color, dark, appearance, visual, direction, language, aesthetic, aesthetics, style, styling, styled, premium, polish, polished, beautiful, handoff, spec, screens, cards, card, rows, lists, gradients, materials, hierarchy, depth, motion, animation, animations, spring, haptics, empty, states, component, components, inventory, ux, flows, layout, branding, identity -->

# Design Excellence Cheatsheet (SwiftUI, iOS 17+)

How to turn a design spec into an app that looks deliberately designed, not template-grade. Pair with the HIG/UX cheatsheet (navigation, states, a11y depth); this file is the visual-identity and token-implementation layer.

## Rule zero: one theme file, no scattered literals

Every app gets a single `Theme.swift` implementing the design spec's tokens. Views never contain raw `.font(.system(size: 17))`, hex colors, or magic padding numbers — they reference the theme. This is what makes an app look coherent instead of assembled.

```swift
// Theme.swift — the ONLY place type, spacing, and color values live.
import SwiftUI

enum Theme {

    // MARK: Type scale (from the design spec; relativeTo keeps Dynamic Type)
    enum Font {
        static let display  = SwiftUI.Font.system(.largeTitle, design: .rounded, weight: .bold)   // 34
        static let title    = SwiftUI.Font.system(.title2, design: .rounded, weight: .semibold)   // 22
        static let headline = SwiftUI.Font.headline                                               // 17 semibold
        static let body     = SwiftUI.Font.body                                                   // 17
        static let caption  = SwiftUI.Font.caption                                                // 12
        static let number   = SwiftUI.Font.system(.title, design: .rounded, weight: .bold).monospacedDigit()
    }

    // MARK: Spacing grid (4pt base; use ONLY these steps)
    enum Spacing {
        static let xs: CGFloat = 4
        static let sm: CGFloat = 8
        static let md: CGFloat = 16
        static let lg: CGFloat = 24
        static let xl: CGFloat = 32
    }

    enum Radius {
        static let card: CGFloat = 16
        static let control: CGFloat = 12
        static let chip: CGFloat = 8
    }

    // MARK: Semantic colors — every role has a light AND dark value
    enum Colors {
        static let background   = Color(light: Color(hex: 0xFAF9F6), dark: Color(hex: 0x121212))
        static let surface      = Color(light: .white,               dark: Color(hex: 0x1E1E1E))
        static let textPrimary  = Color(light: Color(hex: 0x1A1A1A), dark: Color(hex: 0xF2F2F2))
        static let textSecondary = Color(light: Color(hex: 0x6B6B6B), dark: Color(hex: 0x9E9E9E))
        static let accent       = Color(light: Color(hex: 0x2F6F5E), dark: Color(hex: 0x63B79F))
        static let success      = Color(light: Color(hex: 0x2E7D32), dark: Color(hex: 0x81C784))
        static let danger       = Color(light: Color(hex: 0xC62828), dark: Color(hex: 0xEF9A9A))
    }
}

// Light/dark-aware Color without an asset catalog:
extension Color {
    init(light: Color, dark: Color) {
        self.init(uiColor: UIColor { $0.userInterfaceStyle == .dark ? UIColor(dark) : UIColor(light) })
    }
    init(hex: UInt32) {
        self.init(red: Double((hex >> 16) & 0xFF) / 255,
                  green: Double((hex >> 8) & 0xFF) / 255,
                  blue: Double(hex & 0xFF) / 255)
    }
}
```

Usage in views — always through the theme:

```swift
Text(entry.title)
    .font(Theme.Font.headline)
    .foregroundStyle(Theme.Colors.textPrimary)
    .padding(Theme.Spacing.md)
```

- Swap the token *values* per the design spec — the structure stays. The values above are placeholders, not a house style.
- Dark mode is free when every color goes through `Theme.Colors`; never `Color.white`/`.black` for surfaces. Set the accent once: `.tint(Theme.Colors.accent)` on the root view.
- Preview both appearances on every screen: `#Preview { ContentView().preferredColorScheme(.dark) }`.

## Lists that don't look default

A stock plain `List` with default rows is the template look. Keep `List` (it's fast and accessible) but restyle it:

```swift
List {
    ForEach(items) { item in
        ItemCard(item: item)                       // custom row view, not Text(item.name)
            .listRowSeparator(.hidden)
            .listRowBackground(Color.clear)
            .listRowInsets(EdgeInsets(top: 6, leading: Theme.Spacing.md,
                                      bottom: 6, trailing: Theme.Spacing.md))
    }
}
.listStyle(.plain)
.scrollContentBackground(.hidden)                  // required to see your own background
.background(Theme.Colors.background)
```

- A designed row has structure (leading glyph in a tinted well, title + secondary line, trailing value), not a single `Text`:

```swift
HStack(spacing: Theme.Spacing.md) {
    Image(systemName: item.symbol)
        .font(.body.weight(.semibold))
        .foregroundStyle(Theme.Colors.accent)
        .frame(width: 36, height: 36)
        .background(Theme.Colors.accent.opacity(0.12), in: RoundedRectangle(cornerRadius: Theme.Radius.chip))
    VStack(alignment: .leading, spacing: 2) {
        Text(item.title).font(Theme.Font.headline).foregroundStyle(Theme.Colors.textPrimary)
        Text(item.subtitle).font(Theme.Font.caption).foregroundStyle(Theme.Colors.textSecondary)
    }
    Spacer()
    Text(item.value).font(Theme.Font.number).foregroundStyle(Theme.Colors.accent)
}
```

- `Form` is right for settings screens — keep it; grouped-inset styling is the native pattern there, not "default-looking".

## Empty states that sell the app

The empty state is the first screen a user sees — design it like a feature. Required: identity glyph, one-line promise, the action that fills the screen.

```swift
VStack(spacing: Theme.Spacing.md) {
    Image(systemName: "leaf.circle.fill")
        .font(.system(size: 56))
        .symbolRenderingMode(.hierarchical)
        .foregroundStyle(Theme.Colors.accent)
    Text("Start your first entry")
        .font(Theme.Font.title)
        .foregroundStyle(Theme.Colors.textPrimary)
    Text("A minute a day is enough to see patterns emerge.")
        .font(Theme.Font.body)
        .foregroundStyle(Theme.Colors.textSecondary)
        .multilineTextAlignment(.center)
    Button("New Entry", action: create)
        .buttonStyle(.borderedProminent)
        .controlSize(.large)
}
.padding(Theme.Spacing.xl)
.frame(maxWidth: .infinity, maxHeight: .infinity)
```

- `ContentUnavailableView` is the acceptable fast path for *search/error* states; the app's primary empty state deserves the custom version above, in the app's own voice.
- Copy is part of the design language: "Start your first entry" (invitational) vs "No entries" (dead).

## Motion defaults

Animate state changes, sparingly and consistently — one motion personality per app.

```swift
withAnimation(.snappy) { isExpanded.toggle() }            // default choice for interactive UI
withAnimation(.smooth) { selection = item }                // calm apps
withAnimation(.bouncy) { showCelebration = true }          // playful apps, use rarely
.animation(.snappy, value: items.count)                    // implicit, value-scoped only
.contentTransition(.numericText())                         // rolling counters
.transition(.move(edge: .bottom).combined(with: .opacity)) // inserted views
```

- Pick ONE curve family (`.snappy` / `.smooth` / `.bouncy`) as the app default and reuse it — mixed curves read as unpolished.
- Never `.animation(...)` without `value:`. Gate big motion on `@Environment(\.accessibilityReduceMotion)` → cross-fade instead.
- Signature moment: spend the motion budget on the ONE interaction the design spec names (hero expand via `matchedGeometryEffect`, a ring filling, a card snap) and keep everything else quiet.

## Haptics one-liners

```swift
.sensoryFeedback(.success, trigger: didSave)               // task completed
.sensoryFeedback(.impact(weight: .light), trigger: selectedTab)  // snap/toggle
.sensoryFeedback(.selection, trigger: pickerValue)         // discrete steps
.sensoryFeedback(.error, trigger: failureCount)            // action failed
```

One haptic per discrete event, always paired with a visible change; the signature interaction usually earns one.

## HIG type ramp (reference values)

Default (Large) sizes — map every design-spec role to the nearest style so Dynamic Type scaling is preserved:

| Style | Size/weight | Use |
|---|---|---|
| largeTitle | 34 regular | Screen hero, top-level nav title |
| title | 28 regular | Section hero |
| title2 | 22 regular | Card titles |
| title3 | 20 regular | Sub-cards |
| headline | 17 semibold | Row titles, emphasis |
| body | 17 regular | Reading text |
| callout | 16 regular | Secondary body |
| subheadline | 15 regular | Row subtitles |
| footnote | 13 regular | Metadata |
| caption/caption2 | 12/11 regular | Labels, timestamps |

- Differentiate with **weight and color before size**: headline + `textSecondary` subheadline reads more refined than two sizes of gray.
- `design: .rounded` (or `.serif`) on display/title text is the cheapest way to give an app a typographic personality; keep body text `.default`.
- Numbers that update: `.monospacedDigit()` so widths don't jitter.
- Custom brand font: `.font(.custom("FontName", size: 17, relativeTo: .body))` — `relativeTo` is mandatory for scaling.

## Spacing and layout rhythm

- Everything on a 4pt grid; prefer 8/16/24/32 for structure. Same-screen gaps should come from **at most three** distinct steps — more reads as noise.
- Screen edge inset: 16pt (`Theme.Spacing.md`); grouping = proximity: 8 within a cluster, 24 between clusters.
- Breathing room is the #1 premium signal. When a screen looks cheap, the fix is usually more space between groups, not more decoration.
- Cards: 16pt internal padding, 16pt corner radius, 12–16pt between cards.

## Surfaces, depth, and hierarchy

Three-layer model: `background` (screen) → `surface` (cards) → accent/content. Don't stack same-color surfaces.

```swift
// Card that reads as designed in light AND dark:
VStack(alignment: .leading, spacing: Theme.Spacing.sm) { /* content */ }
    .padding(Theme.Spacing.md)
    .frame(maxWidth: .infinity, alignment: .leading)
    .background(Theme.Colors.surface, in: RoundedRectangle(cornerRadius: Theme.Radius.card))
    .shadow(color: .black.opacity(0.06), radius: 8, y: 2)   // whisper, not drop shadow
```

- Dark mode depth comes from **lighter surfaces**, not shadows — shadows vanish on dark backgrounds. The surface/background token pair handles this.
- Materials for overlays/bars: `.background(.regularMaterial)`; they adapt to appearance and Reduce Transparency automatically.
- A single restrained gradient on a hero header or stat card adds identity; gradients on everything destroys it:

```swift
.background(LinearGradient(colors: [Theme.Colors.accent, Theme.Colors.accent.opacity(0.7)],
                           startPoint: .topLeading, endPoint: .bottomTrailing))
```

## Accessibility essentials (design-quality gate)

Full checklist lives in the HIG/UX cheatsheet; these are the items that most often break a *designed* UI:

- All user-facing text uses text styles or `relativeTo:` fonts — a custom design that ignores Dynamic Type is a defect, not a style choice. Test at AX sizes; let cards grow vertically.
- Contrast on your *own* palette: textPrimary-on-surface ≥ 4.5:1, textSecondary ≥ 4.5:1 (it's the one that usually fails, in both appearances). Verify the dark values separately.
- Icon-only buttons get `.accessibilityLabel("…")`; composite custom rows get `.accessibilityElement(children: .combine)`.
- Custom tap targets ≥ 44×44pt with `.contentShape(Rectangle())`.
- Meaning never by color alone — pair the semantic color with an icon or text.

## Pre-ship design audit

- [ ] `Theme.swift` exists; grep finds no `.system(size:` , hex, or magic padding in views.
- [ ] Every color role has light + dark values; app screenshotted in both appearances.
- [ ] Type: max ~4 sizes per screen, hierarchy via weight/color, numbers monospaced.
- [ ] Spacing on the grid; groups separated by space, not lines.
- [ ] Lists/rows match the spec — no default `Text`-only rows where cards were specified.
- [ ] Primary empty state has glyph + promise + CTA in the app's voice.
- [ ] One motion personality; signature interaction implemented; Reduce Motion respected.
- [ ] Dynamic Type AX sizes don't clip; contrast verified; icon buttons labeled.
