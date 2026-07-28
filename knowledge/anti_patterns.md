# Fleet anti-patterns (auto-generated — do not repeat these)

Recurring failures recorded across this factory's runs: build verification, prompt adherence, visual QA, UI crawl, design lint, and human ratings. Treat every bullet as a design/build rule.

- Do not release visual QA with a blank screen or placeholder text.
- Ensure all screens have meaningful content, not just placeholders.
- Avoid releasing designs with unresolved dependency lint errors.
- Use variables for font sizes and colors to prevent hardcoding.

## Raw clusters

### visual_qa — 5 incident(s)
- visual QA failed (score 0) — main_light.png: gemma3:4b: BAD It’s a blank screen with only placeholder text.; m
- visual QA failed (score 0) — main_light.png: gemma3:4b: BAD It's a very basic, empty screen with just placehol
- visual QA failed (score 0) — main_light.png: gemma3:4b: BAD; main_dark.png: gemma3:4b: BAD
- visual QA failed (score 0) — main_light.png: gemma3:4b: BAD; main_dark.png: gemma3:4b: BAD
- visual QA failed (score 0) — main_light.png: gemma3:4b: BAD; main_dark.png: gemma3:4b: BAD

### design_lint — 2 incident(s)
- design lint: 1 design/dependency lint error(s) — e.g. raw_font_size at Tether/Resilience/ResilienceHelpers.swi
- design lint: 2 design/dependency lint error(s) — e.g. inline_color at Theme/Theme.swift:5 (hardcoded color — u
