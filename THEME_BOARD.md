# Theme board: align the GUI to the Sections design handoff (7 cards)

Status: PLAN, approved by Priyansh 2026-07-21. Author: Claude (planning);
implementer: Codex. The delivery rhythm and git safety notes in
`CODEX_HANDOFF.md` apply. Design source: `design/sections-handoff-2/`
(Claude Design export, 4 files — read each `.dc.html` IN FULL before
touching its screens; the user's instruction: "it doesn't have to look
exactly like this, but do follow the themes").

## The one big fact (verified 2026-07-21, saves you a rebuild)

`ThemeTokens.swift` ALREADY matches this handoff token-for-token: accent
0x4A56C7/0x8A93F2, claude 0xC4643B/0xE08D63, codex 0x0E7E74/0x4DB8AC,
gemini 0x3467D6/0x7CA2F0, local 0x5B6472/0x9AA3B2, success/warning/
fallback/error pairs, the 8%-light/12%-dark tint-fill + 25% stroke
formula, and the state grammar (purple = fallback EXCLUSIVELY). The
palette work is DONE. This board is about **layout, composition, spacing,
and motion** — making the existing views read like the handoff screens.

## Invariants

1. **ThemeTokens.swift stays the single source.** Any new token (spacing,
   radius, motion curve) is added THERE; `ci_style_check.sh` /
   StyleGuardTests already reject hex or font sizes anywhere else — do not
   weaken that check, extend it to any new token family you add.
2. **Native surfaces stay native.** The handoff's `--bg/--surface-0..3`
   hex ladder is the WEB approximation of macOS materials. The mapping is
   `--bg` → `DS.windowBg`, `--surface-3` → `DS.cardBg`, hover/wells →
   `DS.raised`/`DS.insetBg`. Never replace system semantic colors with the
   handoff's literal surface hexes — vibrancy and increased-contrast would
   break.
3. **Identity never carries state** (§6 grammar): agent hues for
   who-is-speaking only; status colors always win; every status rendering
   stays symbol + word, never color alone.
4. **Hairlines are 1px, elevation is by fill-step, never shadow** — the
   handoff agrees (its borders are 12%/7% alpha, radius 6/8/12); keep it.
5. Respect Reduce Motion: every animation this board adds gets a
   `accessibilityReduceMotion` fallback.

## Cards (order: foundations first, then user-facing surfaces by traffic)

### T1. Foundations: spacing/radius/motion tokens
From `Sections - Components.dc.html` (read the "Space, radius & motion"
screen): spacing scale 4/8/12/16/24/32, radii badge=6 chip=8 card=12,
spring curve cubic-bezier(.3,.9,.25,1), `breathe` (1.6s ease-in-out,
opacity .35→1 + scale .78→1) and `shimmer` keyframes. Add whichever are
missing to ThemeTokens.swift as named tokens (DS.space, DS.radius,
DS.motion). Migrate magic numbers in views you touch in later cards to
these tokens as you go — no big-bang sweep. Tests: StyleGuardTests
extended so a new hardcoded corner radius outside ThemeTokens fails.

### T2. Chat surface (App Shell screens 1–2 + Components chat section)
The highest-traffic surface. Align `ChatSessionView`/`TranscriptView`
with: per-agent bubbles (identity-tint avatar chip + "name · model"
mono byline), the round divider ("Round 3 of 9") treatment, the PASS
slip, and the consensus/Final Output card. Layout only — transcript
parsing/data stays untouched. Tests: view-model mapping (agent → tint,
round → divider label) in XCTest; no AppKit chrome tests.

### T3. Shell chrome: sidebar, topbar, composer (App Shell screens)
`AppShellView`/`SectionRail`: nav groups (Fleet / Studios / Mission
Control), running-project row with breathing accent dot, New Chat
primary button, search affordance with ⌘K chip, theme-consistent
composer bar with Send. Keep existing navigation logic; this is visual
composition.

### T4. Session side panels (App Shell screens 3–4)
"In the room" cast panel (Claude · Skeptic, Codex · Investigator rows
with identity chips), Phase plan rail, Artifacts rail with artifact
cards. Map to the existing cast/phase/artifact data — no new data
plumbing; where the design shows a field we don't track, omit it (do
not invent placeholder data).

### T5. Supporting surfaces (`Sections - Supporting.dc.html`)
Artifact detail + lineage view (version chain Brief v1 → v2 · current →
v3-a/v3-b drafts, branch-conflict marker, "Consumed by" list, "Rebuild
with v2" action, metadata/keywords/doc-slots panels) aligned in
`DocumentBuilderView`/`ProjectInspectorView`; studio settings sheet
(`SectionSettings`); model library + agent sign-in rows
(`ModelLibrary`/`ModelsAgentsView` — "codex · logged in", "gemini · not
signed in" status rows). Same rule: existing data only.

### T6. Autonomy surfaces (`Sections - Autonomy.dc.html`)
`MissionControlView`: Fleet/Flow columns, Decision ledger, Finish line,
Pending approval card with Route/Approve actions, Budget meter.
`PipelineBuilderView`: the "Idea → Validated Spec" canvas treatment,
studio chips. These views shipped recently (7.10/7.11) — expect them to
be closest already; this is a refinement pass, not a redo.

### T7. Motion & empty states polish
Breathe dot everywhere "running" appears (already partially done),
shimmer for loading placeholders, the handoff's "Nothing here yet"
empty-state pattern (styled message + hint, mirroring what we just
taught the visual-QA judge to respect). Reduce Motion fallbacks
(invariant 5) verified by a StyleGuard-style grep: no `animation(` on
the breathe/shimmer tokens without a reduceMotion branch.

## Dependency graph

T1 first (tokens the rest consume). Then T2 → T3 → T4 (shell+chat
cluster), T5, T6 independent after T1; T7 last. One card per commit,
`swift test` green + `make verify` before each.

## Queue position

Recommended: THEME before PERF_BOARD (user-facing, and the user just
supplied the designs), UNLESS PERF P1–P3 are already in flight — never
abandon a started card. Priyansh can reorder by saying so.

## Open questions (write here if blocking, don't guess)

- (none yet)
