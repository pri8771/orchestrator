# Quality Rules — Adoption of the Vibe-Coding Quality Rulebook

This maps the product-quality rulebook the operator adopted onto **how the
orchestrator actually enforces each rule** for the apps it generates. The
rulebook's own thesis is *"don't trust a self-report that something is done —
verify it,"* so this doc is deliberately honest about the difference between a
rule that is **mechanically gated** (the engine checks it) and one that is only
**prompted** (an agent is told to follow it, but nothing verifies compliance).
Gated rules compound; prompted-only rules are aspirational until gated.

## Enforcement surfaces

- **Build prompt** — `_QUALITY_RULES_INSTRUCTION` (orchestrator.py), injected
  into every `build_coordination` turn (initial build AND every release-gate
  repair). Prose the build agents are told to follow.
- **Machine rules** — `phase_rules.json` `global_app_rules`, the shipped rule
  list every build phase is held to.
- **Knowledge corpus** — `knowledge/ios/*.md`, retrieved and injected per
  phase. Includes `observed-mistakes.md`, a **curated, growing log of real
  defects seen in shipped builds**, generalized into rules so the same mistake
  is not reproduced. New real defects get appended here.
- **designlint gate** (`designlint.py`) — deterministic, zero-token scan of
  `app_build/` Swift after the build. Hard errors route into the bounded repair
  loop; soft signals are warnings.
- **Visual-QA gate** (`visualqa.py`) — boots the built app in the simulator,
  screenshots it (light + dark), and a vision model grades each screen OK/BAD.
- **UI-crawl / adherence gates** — drive the built app and grade it against the
  declared requirements/flows.

## Rulebook → enforcement

| Rulebook rule | Enforced by | Gated? |
|---|---|---|
| R2 interface must never lie (Saved/Sent/Synced only after real success) | build prompt + `phase_rules.json` | prompt-only |
| R3 no sample data shown as real; honest empty states | build prompt + `phase_rules.json` (empty-state rule) | prompt-only |
| R4 explicit state model (enum, not loose booleans) | build prompt | prompt-only |
| R5 adaptive layout — reflow, not clip; min window size; Dynamic Type | build prompt + `phase_rules.json` | prompt-only |
| **§3 / R5 no overlapping / colliding / clipped text** | build prompt + `phase_rules.json` + **visual-QA gate** + `observed-mistakes.md` (M-001) | **GATED (visual QA)** |
| §3.4 dark mode complete (contrast, not just background) | `phase_rules.json` + **visual-QA gate** (grades a dark screenshot) | **GATED (visual QA)** |
| §3.5 shared design tokens, no scattered color/font literals | `phase_rules.json` + **designlint** (`inline_color`, `raw_font_size`) | **GATED (designlint, hard error)** |
| **§16 fake-feature prohibition (empty actions / decorative controls)** | build prompt + **designlint** (`empty_action`, warning) | **GATED (designlint, soft)** |
| R6 real persistence; preserve input on failure | build prompt | prompt-only |
| R4.3 no duplicate submissions; R4.4 cancellation ≠ error | build prompt | prompt-only |
| §10 accessibility (labels, non-color status, keyboard) | build prompt + `phase_rules.json` | prompt-only |
| §5.3 never surface raw internal errors | build prompt | prompt-only |
| R22 honest completion report (name placeholders/mocks) | build prompt | prompt-only |
| §14 iOS-native (signing, real project) | build-phase signing rules + `xcodebuild`/visual-QA compile gate | **GATED (compile)** |

## Honest coverage note

Roughly the layout/design/build-hygiene band is mechanically gated today
(compile, design tokens, overlapping text, dark mode, fake controls). The
behavioral band — persistence-survives-relaunch, duplicate-submission
protection, error specificity, accessibility correctness, migration, offline —
is currently **prompt-only**: the agents are told, but nothing verifies. Those
are the next gate candidates (a state-model lint, an extra window-size + a
relaunch/persistence probe in the crawl gate). Until then, treat a passing
build as "compiles, looks clean, uses tokens, no overlap" — NOT as a guarantee
of the behavioral rules.

## Adding a newly observed mistake

When a real defect is seen in a built app: append an entry to
`knowledge/ios/observed-mistakes.md` (what was seen → a GENERIC rule → how to
avoid it), and — if it's mechanically detectable — add a `designlint` check or
extend the visual-QA rubric. A prompt line alone lets the mistake recur; a gate
stops it.
