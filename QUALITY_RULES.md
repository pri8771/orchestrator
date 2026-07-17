# Quality Rules — Adoption of the Vibe-Coding Quality Rulebook

The full rulebook is [VIBE_CODING_QUALITY_RULEBOOK.md](VIBE_CODING_QUALITY_RULEBOOK.md)
(saved verbatim as the fixed reference). This doc is the living, honest status
report: how each rule maps onto **how the orchestrator actually enforces it**
for the apps it generates — and, separately, how the rulebook applies to the
orchestrator's own engine/GUI codebase.

The rulebook's own thesis is *"software that looks more complete than it
actually is"* (§23) — *"don't trust a self-report that something is done,
verify it."* So this doc is deliberately honest about the difference between a
rule that is **mechanically gated** (the engine checks it) and one that is only
**prompted** (an agent is told to follow it, but nothing verifies compliance).
Gated rules compound; prompted-only rules are aspirational until gated.

## Two different targets

1. **Rules applied to the apps the engine generates** (gloam, tether, tipjar,
   …) — the bulk of this doc. Enforced via the build prompt, machine rules,
   the knowledge corpus, and the gates below.
2. **Rules applied to the orchestrator's own GUI/engine** — adopted directly,
   not diluted. Several bugs fixed in the engine this cycle ARE these rules,
   found by audit rather than injected via a prompt: `writeJSON()` silently
   reporting a failed config save as success was Rule 2 in our own app;
   `shepherd.sh` treating a stale dead-pid lock as "still running" and a
   release-gate repair budget leaking across runs were §12 (async/state
   correctness) in our own engine. Not applicable wholesale, since this is not
   AI-generated end-user software: the fix is a normal engineering review, not
   a new gate.

Deliberately **not** injected wholesale into every generated app: most of the
rulebook isn't mechanically verifiable today, and for the "simple/local" app
tier much of it (migration, offline, backend) is inapplicable — a blanket
mandate would over-scope narrow apps and just produce more confident claims of
compliance, the exact failure the rulebook warns against (see the honest
coverage note below).

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
- **Compile verification** (`verify.py`) — `xcodebuild`/`swift build`; the one
  gate that runs the actual toolchain rather than reading source or a
  screenshot.
- **UI-crawl / adherence gates** — drive the built app and grade it against the
  declared requirements/flows.

## Rulebook → enforcement

| Rulebook rule | Enforced by | Gated? |
|---|---|---|
| R2 interface must never lie (Saved/Sent/Synced only after real success) | build prompt + `phase_rules.json` | prompt-only |
| R3 no sample data shown as real; honest empty states | build prompt + `phase_rules.json` (empty-state rule) | prompt-only |
| R4 explicit state model (enum, not loose booleans) | build prompt | prompt-only |
| R5 adaptive layout — reflow, not clip; min window size; Dynamic Type | build prompt + `phase_rules.json` | prompt-only |
| **§3 / R5 no overlapping / colliding / clipped text** | build prompt + `phase_rules.json` + **visual-QA gate** + `observed-mistakes.md` (M-001, M-003) | **GATED (visual QA)** |
| §3.4 dark mode complete (contrast, not just background) | `phase_rules.json` + **visual-QA gate** (grades a dark screenshot) | **GATED (visual QA)** |
| §3.5 shared design tokens, no scattered color/font literals | `phase_rules.json` + **designlint** (`inline_color`, `raw_font_size`) | **GATED (designlint, hard error)** |
| **§16 fake-feature prohibition (empty actions / decorative controls)** | build prompt + **designlint** (`empty_action`, warning) | **GATED (designlint, soft)** |
| **§16 / §23 "looks done but isn't" — a build with no runnable app target** | `verify.py` `_verify_xcode`: no `.xcodeproj`/`.xcworkspace` (and none generatable) is now a hard verification FAILURE, not "unverified" pass-through | **GATED (verify, hard error)** |
| R6 real persistence; preserve input on failure | build prompt | prompt-only |
| R4.3 no duplicate submissions; R4.4 cancellation ≠ error | build prompt | prompt-only |
| §10 accessibility (labels, non-color status, keyboard) | build prompt + `phase_rules.json` | prompt-only |
| §5.3 never surface raw internal errors | build prompt | prompt-only |
| R22 honest completion report (name placeholders/mocks) | build prompt | prompt-only |
| §14 iOS-native (signing, real project) | build-phase signing rules + `xcodebuild`/visual-QA compile gate | **GATED (compile)** |

## Honest coverage note

Roughly the layout/design/build-hygiene band is mechanically gated today
(compile, design tokens, overlapping text, dark mode, fake controls, and now
"did this even produce a runnable app"). The behavioral band —
persistence-survives-relaunch, duplicate-submission protection, error
specificity, accessibility correctness, migration, offline — is currently
**prompt-only**: the agents are told, but nothing verifies. Those are the next
gate candidates (a state-model lint, an extra window-size + a
relaunch/persistence probe in the crawl gate). Until then, treat a passing
build as "compiles into a runnable app, looks clean, uses tokens, no overlap"
— NOT as a guarantee of the behavioral rules.

**Known residual gap in the visual-QA gate itself**, found live: `simctl
privacy grant all/location` does not reliably suppress the iOS Simulator's
first-launch location-permission dialog, so a location-using app's screenshot
can still be graded against a dialog rather than the real screen. The gate's
own vision models (`qwen2.5vl:3b`, `gemma3:4b`) are also documented as
unreliable at specifically calling out overlap versus other defects (they
correctly flag the screen BAD but may cite the wrong reason) — the *build-time*
prevention (prompt rule + `observed-mistakes.md`) is the real first line of
defense; the gate is a backstop of limited precision, not the primary check.

## Observed mistakes logged so far

`knowledge/ios/observed-mistakes.md` — real defects seen in shipped builds,
each generalized into a rule:

- **M-001** — ribbon/timeline anchor labels overlapping when data values land
  close together (a high-latitude day bunching sunrise/golden-hour times).
  Fixed in the next build: a real, unit-tested `LabelDeclutter` collision
  algorithm that quotes the rule and the edge case by name.
- **M-002** — claiming an outcome that didn't happen / sample data shown as
  real (the rulebook's Rule 2 / Rule 3, restated as a build-time reminder).
- **M-003** — a label and a nearby control overlapping at a screen edge (found
  in the SAME rebuilt app that fixed M-001 — the de-clutter fix covered its
  designed scope, the ribbon axis, but not every text+control cluster on the
  screen). Generalizes to: overlap avoidance must be checked per-region, not
  solved once for the hero component.

## Adding a newly observed mistake

When a real defect is seen in a built app: append an entry to
`knowledge/ios/observed-mistakes.md` (what was seen → a GENERIC rule → how to
avoid it), and — if it's mechanically detectable — add a `designlint` check or
extend the visual-QA rubric. A prompt line alone lets the mistake recur; a gate
stops it.
