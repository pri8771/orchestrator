# Project conventions (golden scaffold)

This folder was seeded by the orchestrator's golden scaffold. Binding rules
for every build lane:

1. **DesignSystem.swift is the only styling source.** Fill its tokens from the
   design handoff, then use `DS.Palette` / `DS.Font` / `DS.Space` /
   `DS.Radius` everywhere. Inline `Color(red:)` and `.font(.system(size:))`
   outside DesignSystem.swift are lint ERRORS that block "done".
2. **Every screen ships four states**: populated, empty (with guidance copy),
   loading, and error. Happy-path-only screens fail review.
3. **Every interactive element sets `.accessibilityIdentifier`** — the UI
   crawl gate and declared flows target elements by it.
4. **Dark mode is not optional.** Both palettes come from the handoff; the
   visual QA gate screenshots and grades both.
5. **Dependencies**: system frameworks first (URLSession, SwiftData, Swift
   Charts, OSLog). Third-party SPM packages must be justified in tech_specs
   and pass tech_stack.json (banned list is a lint ERROR).
6. **Structure**: App entry + Features/<Feature>/ (views, models per feature)
   + DesignSystem.swift + Tests/. Keep files under ~300 lines.
