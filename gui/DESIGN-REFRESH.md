# Orchestrator GUI — Design Refresh ("Native Pro, finished")

**Status:** direction + rollout plan. Companion mockup: `gui-redesign-mockup.html`
(scratchpad, not in repo). Supersedes nothing — DESIGN-NATIVE-PRO.md remains the
architecture spec; this document is about why the app still *feels* unfinished
and exactly what to change, in shippable tranches.

---

## 1. Honest critique — why "the design sucks" even though the spec is good

The Native Pro token layer (`ThemeTokens.swift`) and component kit are genuinely
solid. The problem is **incomplete adoption**: the app is three design eras
stacked in one window, and the seams are what the eye reads as "bad design."

1. **The banner stack is off-palette and noisy.** `ContentView.swift`'s five
   banners (approval, error, conflict, engine-missing, action-error) use raw
   `Color.orange/.red/.purple.opacity(0.12)` — not the owned palette — with
   five hand-rolled layouts, five padding schemes, and ad-hoc fonts. On a
   troubled run, three of them stack into a wall of clashing tinted strips at
   the top of the window. Worst: the merge-conflict banner is **purple**,
   directly violating the "purple = fallback, exclusively" rule the rest of
   the app is built on.
2. **The WorkflowBuilder sheet is a different app.** It still renders the
   deleted factory dashboard's GitHub-dark lime-terminal skin (forced dark,
   `#9FEF00` accent, all-mono 9.5–14pt type) inside an otherwise native shell.
   Opening it is the single most jarring moment in the app.
3. **Type scale collapse in dense views.** `Configuration.swift` (27 hardcoded
   sizes) and `ModelLibrary.swift` (9) sprinkle 8/9/10pt type — below the
   ramp's 11pt floor — so the settings sheets and agent cards feel cramped and
   subtly "off" next to the tokenized views. `ModelLibrary` also uses purple
   decoratively on BUILD/CRITICAL badges (second grammar violation).
4. **Empty states are four different hand-rolled patterns** — different icon
   sizes (30/48pt), different title fonts, different copy tones. Empty states
   are most of what a new user sees; inconsistency here reads as neglect.
5. **Hierarchy is flat inside the content column.** Section headers ("Agent
   Board", "Event Feed") are bare 15pt semibold text floating over content
   with no rhythm; cards are all the same one-step quaternary fill, so nothing
   recedes and nothing advances.
6. **The command palette looks like a debug sheet**, not a palette: a plain
   `TextField` over a default `List`, centered as a modal sheet. It works; it
   doesn't feel premium.

None of this needs new architecture. It needs the token layer finished,
enforced, and applied to the last three legacy corners.

## 2. Design principles

1. **Native, calm, correct.** System materials, system semantics, real
   controls. No shadows at rest; elevation by fill-step. If AppKit gives it to
   us free (vibrancy, dark mode, increased contrast), never re-implement it.
2. **One grammar, zero exceptions.** Accent indigo = running/selection · green
   = success · amber = warning/stall/attention · purple = fallback ONLY · red
   = error · gray = idle. Symbol + word, never color alone. Identity hues
   (terracotta/teal/cobalt/slate) never carry state. Every violation found in
   audit gets fixed, and the CI ratchet only tightens.
3. **Information-dense but hierarchical.** Density comes from the spacing
   system and tabular numerals, not from shrinking type below 10pt. Hierarchy
   comes from size + color, never from weight above semibold.
4. **Machine text is mono; nothing else is.** Model IDs, paths, timers, raw
   output. Prose, labels, and titles are SF Pro.
5. **Empty states teach.** Every empty pane says what will appear here, why
   it's empty, and (when actionable) the one action that fills it.

## 3. Type scale (SF Pro; roles, not sizes, in call sites)

| Token | Spec | Role |
|---|---|---|
| `DS.font.largeTitle` | 26 semibold | prominent empty-state titles only |
| `DS.font.title` | 20 semibold | sheet/screen titles |
| `DS.font.headline` | 15 semibold | card titles, project names, section headers |
| `DS.font.body` | 13 regular | workhorse text |
| `DS.font.callout` | 12 medium | chip labels, control labels, cell values |
| `DS.font.caption` | 11 regular | metadata, timestamps, footnotes |
| `DS.font.caption2` | 10 medium | **new** — micro-badges and rank numerals in dense rows; the sanctioned floor. Nothing renders below 10pt, ever |
| `DS.font.stat` | 28 rounded semibold | stat tiles (`.monospacedDigit()`) |
| `DS.font.monoWell` | 11.5 mono | log/transcript wells (1.45 line height) |
| `DS.font.monoInline` | 12 mono medium | model IDs, paths, timers |
| `DS.font.monoCaption` | 10.5 mono | **new** — model IDs inside dense chips (fallback-ladder rungs) |

The two new tokens exist to *delete* the 8/9/10pt scatter — every legacy size
maps to one of these twelve roles or it doesn't ship.

## 4. Spacing & shape (4/8pt system — unchanged, now enforced everywhere)

- **Scale:** 4 / 8 / 12 / 16 / 24 / 32 (`DS.space`). 24pt content margins,
  16pt between zones, 8pt intra-card, 4pt icon-to-label.
- **Radii:** 6 control · 8 chip · 12 card, all continuous.
- **Borders:** 1px hairline, never 2px. No shadows at rest.
- **Banners:** 16pt horizontal / 10pt vertical padding, 3pt full-height accent
  bar on the leading edge (same vocabulary as fallback/error event rows), tint
  fill from the status tint's 8/12% formula.

## 5. Color (restrained, semantic)

Already specified in DESIGN-NATIVE-PRO §2.2 and correct; the refresh changes
**usage**, not values:

- **Background elevations:** window → card (`controlBackgroundColor`) → raised
  (`.quaternary`) → inset well (`.quaternary.opacity(0.5)`). Exactly four.
- **One accent:** Conductor Indigo. Nothing else may be decorative.
- **Status colors only for status.** Fixes required by audit: conflict banner
  purple → amber; ModelLibrary BUILD/CRITICAL badges purple → accent;
  scattered `.green`/`.orange` literals → `DS.status.success/.warning`.
- **Raw `Color.red/.orange/.purple/...` system-literal shorthands are treated
  as violations** in new code even though the CI grep can't see them; reviews
  enforce it, and tranche 1 clears the existing ones outside legacy files.

## 6. Component specs

- **InlineBanner** (new, ComponentKit): the one banner. Status tint fill, 3pt
  leading bar, `body`-medium title + `caption` secondary message, trailing
  action slot. All five ContentView banners and the stall banner become
  instances. Banners stack with hairline separators; when three stack, they
  still read as one system.
- **EmptyStateView** (new, ComponentKit): 48pt hierarchical symbol, headline
  title (largeTitle in `prominent` mode for the app-level landing state),
  caption message ≤ 380pt wide, optional single `.borderedProminent` action.
  Every full-pane empty state is an instance.
- **StatusPill / Chip / AgentAvatar / EffortGauge / StatTile / Sparkline:**
  unchanged (already correct); adoption completed in legacy corners.
- **Cards:** 12pt radius, `cardBg` fill + hairline for primary cards (agent
  board), `insetBg` for passive lists (workflows, events). Section headers
  above cards: headline + optional caption count, 16pt above / 8pt below.
- **Sidebar rows:** 22pt monogram (tint formula), body name, caption detail,
  6pt trailing health dot — as shipped; no change.
- **Toolbar:** as shipped (health capsule + bell + actions); no change.
- **Command palette (target, tranche 2):** borderless field with ⌘K glyph,
  38pt rows, symbol + title + right-aligned shortcut in monoInline, selected
  row = accent fill 8/12%, floating 12pt-radius panel over a dimmed scrim
  rather than a default sheet frame.

## 7. Rollout plan

### Tranche 1 — token layer + global polish (this change; zero structural risk)
- ThemeTokens: add `caption2`, `monoCaption` (the sanctioned dense-row floor).
- ComponentKit: add `InlineBanner`, `EmptyStateView` (pure views, previewed).
- ContentView: all five banners → InlineBanner; conflict banner purple → amber;
  ad-hoc fonts/paddings → tokens.
- TranscriptView: last 2 hardcoded sizes gone; orange/green literals → tokens;
  empty phase → EmptyStateView.
- Components (RunLogPanel): header fonts → tokens (2 hardcoded sizes gone).
- ModelLibrary: all 9 hardcoded sizes → ramp roles; decorative purple → accent;
  `.green` → success token.
- AppShellView / RunHealthViews: placeholder panes and history empty state →
  EmptyStateView; Overview empty state → prominent EmptyStateView.
- `ci_style_check.sh`: ratchet Components 2→0, TranscriptView 2→0,
  ModelLibrary 9→0. Only `Configuration.swift` (27) remains frozen.

### Tranche 2 — per-view restructures
- **WorkflowBuilder sheet** rebuilt on the DS ramp (light+dark, SF Pro labels,
  mono only for phase keys); delete the legacy `ThemeTokens` struct in
  `Components.swift` — the last hex palette outside the token file.
- **Configuration.swift** onto the ramp (27 → 0; baseline deleted; the grep
  then bans hardcoded sizes everywhere unconditionally).
- **Command palette** → floating panel per §6 spec, with fuzzy project jump.
- Overview: Active Runs cards gain the per-phase timeline capsule from §4.1.

### Tranche 3 — new surfaces
- Files tab on projects (spec §3 region 2 lists it; not yet built).
- Structured-argument palette verbs ("Set lanes to…", "Apply preset…").
- ⌥-drag fill-paint on the routing grid (the documented fast-follow).
- Menu-bar extra restyle on the status-dot grammar.
