# Orchestrator — Final Design Specification ("Native Pro", synthesized)

**Verdict basis:** Native Pro won 2 of 3 lenses (premium brand 9, feasibility 9) and placed a close second on power-user control (8). It is the skeleton. The power-user lens winner (Mission Control) and Conductor lose the shell but win specific organs, grafted below. Every graft and conflict resolution is marked inline.

**The four load-bearing conflict resolutions:**
1. **The routing grid is NOT a modal sheet.** The power-user judge's chief objection to Native Pro stands: you must be able to watch a run degrade and retune routing side-by-side. The grid moves to a persistent **Plan tab** on the project (Mission Control's placement) inside Native Pro's shell. Settings › Defaults embeds the *same component* for the global grid. The sheet dies.
2. **Purple means fallback, exclusively.** Mission Control's semantic replaces Native Pro's amber-for-fallback (two judges independently endorsed it). Amber remains for warnings/stalls/quality-floors; purple is never used decoratively.
3. **Spreadsheet ergonomics are staged, not dropped.** Keyboard navigation, ⌘C/⌘V, context menus, and multi-select ship in the grid's first release; ⌥-drag fill-paint is a fast-follow (the feasibility judge flagged it as SwiftUI's hardest gesture — it must not block the milestone).
4. **Status blue is retired in favor of accent indigo for "running".** Native Pro's running blue (#3467D6) was hex-identical to the Gemini identity cobalt. Identity hues are for identity only (Mission Control's rule); anything "in progress" uses Conductor Indigo.

---

## 1. Design Philosophy

This app should feel like Apple built an internal tool for running build fleets and then polished it for sale — Xcode's structure, TestFlight's clarity, Things 3's calm — where "premium" means correctness (real NavigationSplitView, real inspector, semantic colors, Dynamic Type) and monospace is demoted from costume to tool, appearing only where text is machine output. An orchestration tool earns trust by confessing: degradation is a first-class visual state with its own exclusive color, its own event rows, its own toolbar counter, and its own historical chart — the user must never discover from output quality that the process lied to them.

---

## 2. Design Tokens

All tokens live in one file: `ThemeTokens.swift` (a `DS` enum). Every custom color is a dynamic `NSColor(name:)` provider pair. A CI grep rejects `Color(red:` and hardcoded font sizes outside this file *(graft: Conductor — verified today: 27 `Color(red` call sites across Models.swift ×18, ContentView.swift ×5, TranscriptView.swift ×3, Components.swift ×1)*.

### 2.1 Surfaces & text — system semantics, no hex, ever

| Token | Backing | Use |
|---|---|---|
| `DS.windowBg` | `NSColor.windowBackgroundColor` | window canvas |
| `DS.cardBg` | `NSColor.controlBackgroundColor` | cards, tables, grid |
| `DS.raised` | `.quaternary` fill over cardBg | hovered rows, wells |
| `DS.insetBg` | `.quaternary.opacity(0.5)` | transcript/log wells |
| `DS.textPrimary/Secondary/Tertiary` | `labelColor` family | all text |
| `DS.hairline` | `NSColor.separatorColor` | 1px borders, gridlines |
| Sidebar | system `.sidebar` material | free vibrancy |

This is what makes light/dark, vibrancy, and increased-contrast modes free and correct. No pure black, no pure white, anywhere.

### 2.2 Owned palette (light hex / dark hex; all text-on-surface pairs ≥ 4.5:1)

| Token | Light | Dark | Used for |
|---|---|---|---|
| `DS.accent` "Conductor Indigo" | `#4A56C7` | `#8A93F2` | selection, primary buttons, current-phase fill, **running state**, modified-setting dots |
| `DS.agent.claude` terracotta | `#C4643B` | `#E08D63` | identity only |
| `DS.agent.codex` teal | `#0E7E74` | `#4DB8AC` | identity only |
| `DS.agent.gemini` cobalt | `#3467D6` | `#7CA2F0` | identity only |
| `DS.agent.local` slate | `#5B6472` | `#9AA3B2` | identity only |
| `DS.status.success` | `#1E7F3C` | `#46C065` | completed, healthy |
| `DS.status.warning` amber | `#B25E09` | `#E8A13D` | stalls, quality-floor breaches, cooldown-forecast — **never fallback** |
| `DS.status.fallback` purple | `#8250DF` | `#B394F5` | **exclusively "a fallback happened/is active"** *(graft: Mission Control)* |
| `DS.status.error` | `#C4302B` | `#EF6E63` | timeouts, failures |
| `DS.status.idle` | `secondaryLabelColor` | — | idle, waiting, cooldown text |

**Tint formula (one formula, total consistency):** fills at 8% (light) / 12% (dark), strokes at 25%, content at 100%. Applies to status pills, assignment chips, filter chips, agent avatars.

**Hard rules:** identity hues never carry state; state colors always win; no color-only encoding — every status pill is symbol + word; all custom colors defined once in `ThemeTokens.swift`.

### 2.3 Type ramp (SF Pro; maps to Dynamic Type styles)

| Token | Spec | Maps to | Use |
|---|---|---|---|
| `DS.font.largeTitle` | 26 semibold | `.largeTitle` | empty states only |
| `DS.font.title` | 20 semibold | `.title2` | sheet/screen titles |
| `DS.font.headline` | 15 semibold | `.headline` | card titles, project names |
| `DS.font.body` | 13 regular | `.body` | the workhorse |
| `DS.font.callout` | 12 medium | `.callout` | chip labels, cell model names |
| `DS.font.caption` | 11 regular | `.caption` | metadata, footnotes, timestamps |
| `DS.font.stat` | 24–28 SF Pro Rounded semibold | fixed + `.monospacedDigit()` | Overview stat tiles |
| `DS.font.monoWell` | 11.5 SF Mono regular, 1.45 line height | fixed | log/transcript wells |
| `DS.font.monoInline` | 12 SF Mono medium | fixed | model IDs, paths, timers (tabular) |

**Rules:** SF Mono ONLY for machine output, model IDs, file paths, and elapsed-time counters. Semibold is the maximum in-window weight — hierarchy comes from size + color. Every live numeral uses `.monospacedDigit()`. Dynamic Type everywhere except two documented opt-outs: routing-grid cells and the phase timeline (geometry is load-bearing); both compensate with full VoiceOver labels.

### 2.4 Spacing, radii, elevation, motion

| Scale | Values |
|---|---|
| Spacing (4pt base grid) | 4 / 8 / 12 / 16 / 24 / 32 — 24pt content margins, 16pt between zones, 8pt intra-card |
| Radii (continuous) | 6 (badges/controls) / 8 (chips) / 12 (cards, sheets) |
| Borders | 1px `DS.hairline` everywhere, never 2px |
| Elevation | **No shadows at rest** — elevation by fill-step (`cardBg` → `raised`). System-drawn shadows only on popovers/sheets |
| Motion | One spring (response 0.35, damping 0.8) for chips/presets; status-dot breathing 1.6s; fallback pulse 600ms-once-then-steady; `contentTransition(.numericText())` on counters. Reduce Motion → opacity fades, no loops |

Custom controls are capped at five, each earning its existence: **EffortGauge**, **AssignmentChip**, **PhaseTimeline**, **Sparkline** (20-line Path), **IntegrityStrip**. Everything else is system-native (`borderedProminent` for the one primary action per surface, `bordered` secondary, plain text tertiary; mini toggles in dense rows; SF Symbols only, `.hierarchical` in lists — the ● ✓ ✗ ◌ glyph vocabulary is retired).

---

## 3. Window & Navigation Architecture

One window. `NavigationSplitView` (sidebar + content) + trailing `.inspector`. **Min 1,040 × 700.** Unified/transparent toolbar; window title = selected project, subtitle = live phase ("BackTimer — Building · round 2 of 3"). Requires the macOS platform bump `13 → 14` in `gui/Package.swift` (currently `.macOS(.v13)`) for `.inspector` — trivial, flagged in M0.

### Region 1 — Sidebar (220pt default, 200–300, system `.sidebar` material)

- **FACTORY** — Overview (`square.grid.2x2`) · Activity (`chart.bar.xaxis`)
- **PROJECTS** (the queue, live-sectioned)
  - *Running* (count badge) — rows: monogram avatar (project-hue rounded rect), name, inline phase capsule, 6pt health dot
  - *Queued* — native `.onMove` drag-reorder writing `.orch-queue-order.json`; position number in secondary text
  - *Needs Attention* — materializes only when non-empty; amber badge (purple-ringed if fallback-caused)
  - *Done* (collapsed by default)
- **LIBRARY** — Workflows (`arrow.triangle.branch`) · Models & Agents (`cpu`) — fallback ladders live *here*, on the agent cards (folded in; ladders are properties of agents, per-project overrides live on the project's Plan tab)
- **Footer:** persistent **Lanes control** — "3 lanes" with popover slider 1–9 + live "2 running · 1 idle" annotation. Concurrency is fleet state; it never hides in Settings.

### Region 2 — Content (min 480pt)

Selection-driven, always *one thing, deep*. A project shows a segmented scope bar: **Run · Plan · Transcript · Files · History**. *(Plan tab is the grafted Mission Control placement — the routing grid is a persistent surface, not a sheet.)*

### Region 3 — Inspector (340pt default, 300–420, ⌥⌘I)

Context-sensitive, Xcode-style icon tabs:
1. **Plan Summary** (`slider.horizontal.3`) — one compact row per phase ("Spec — Claude opus ▮▮▮ · Codex ▮▮"), provenance line ("Customized · 4 overrides — Revert"), "Open Plan tab" jump. **When a grid cell is selected, this tab becomes the persistent cell editor** *(graft: Mission Control's non-modal editing)*.
2. **Agents** (`person.2.badge.gearshape`) — participation toggles, per-agent ladder summary, live planned→actual status.
3. **Info** (`info.circle`) — idea prompt, attached docs with QuickLook, dates, workspace path, Reveal in Finder.

Nothing selected → fleet defaults with caption "These are global defaults — select a project to override."

### Toolbar (real `.toolbar` items)

Leading: sidebar toggle. Principal: **Fleet Health capsule** + **Fallback bell** *(graft: Mission Control — `bell.badge` with purple 24h fallback count; click → popover ledger of why-one-liners with jump links; the "133" becomes a number you physically see)*. Trailing: **New App** (⌘N, borderedProminent), Pause/Resume Engine, Inspector toggle. `.searchable` ⌘F scoped to projects.

### Command Palette (⌘K) *(graft: Conductor, scoped for feasibility)*

A floating overlay dispatching **through the existing `store.uiCommand` enum — one action layer, two invocation surfaces.** v1 scope: fuzzy project jump ("bri…" → Brinekeeper), parameterless verbs (Pause queue, Retry failed), and structured-argument verbs via picker steps ("Set lanes to…" → number row; "Apply preset…" → preset list; "Retry codex on…" → project list). No natural-language parsing — arguments are palette pages, which is buildable and faster anyway.

### Secondary surfaces

- **New App intake** — sheet 560×620 (§4.5)
- **Settings (⌘,)** — shrinks 7 tabs → 4: General · Engine (paths, heartbeats, launchd, escalation thresholds) · Defaults (the *same* RoutingGridView + ladders + gates, titled "Defaults for new projects") · Advanced. Everything per-project has left Settings.
- **Menu bar extra** — kept, restyled with the same status-dot grammar.

---

## 4. Key Screens

### 4.1 Factory Overview (default landing)

24pt margins. **Top:** 4-stat row — Running · Queued · Needs Attention · Done Today — 140pt tiles, 28pt SF Pro Rounded semibold values, 11pt secondary labels, no boxes, hairline-separated. **Middle: Active Runs** — one full-width 88pt card per running project (12pt radius, `.quaternary.opacity(0.5)` fill, 1px hairline): monogram + name (15pt semibold), horizontal phase-timeline capsule with per-phase agent avatars, right-aligned agent status pills, health dot. Click-through to Project · Run. **Bottom: Recent Events** — last 20 engine events in a plain inset list; **identical consecutive events aggregate into one row with "× 133" and a disclosure** *(graft: Conductor)*; fallback rows carry a 3pt purple left accent bar. **Empty state:** centered 48pt `sparkles`, "No apps building", "Press ⌘N to queue your first idea."

### 4.2 Project · Run (the screen users stare at)

Three stacked zones.

**Zone A — Phase Timeline (64pt, pinned):** connected capsule chain (Spec → Build → Verify → Iterate, driven by workflow JSON). Completed = accent fill; current = accent + shimmer (TimelineView; Reduce Motion → static) + "round 2/3" sublabel; future = `separatorColor` outline. Each segment carries its per-phase **agent avatars** (16pt, agent-tinted) — routing readable at a glance without opening the grid. Stalled phase = amber pulse + non-modal banner.

**Zone B — Agent Board:** one 56pt row per active worker:
- avatar + name ("Claude · opus-4.5") + EffortGauge glyph
- **planned→actual route line, 11pt SF Mono: `codex-o4 → claude-haiku`** — rendered whenever intent and reality differ; this diff is the canonical rendering of degradation everywhere *(graft: Conductor — the best single visibility primitive of the exercise)*
- live status pill: `Running 02:14` (accent, breathing dot) / `Waiting` / `Cooldown 0:45` (live countdown — the user always knows *when*) / `FALLBACK → qwen2.5-7b` (**purple**, `arrow.uturn.down`, rescue model named in the pill) / `Timed out` (red, inline Retry)
- 5-point **output sparkline** of recent response lengths + automatic "Short output" amber tag below per-phase floors (reviews < 200 chars, specs < 500) — the 18-char-garbage detector, pure client-side
- ⋯ menu: **Retry now · Skip this round · Promote fallback for this run** (writes a temp routing override + undo-able confirmation banner) · Open transcript · Copy raw output.

**Zone C — Event Feed:** filter chips (All · Fallbacks · Errors · Phases); fallback rows = 3pt **purple** left bar, `arrow.triangle.branch`, "Claude opus unavailable → rescued by llama3.2-3b (rung 3/3) · output 18 chars — flagged"; error rows = red bar + right-aligned Retry re-invoking the exact call; **aggregation** collapses repeats to "× N". SF Mono appears only inside per-event "Raw output" disclosures. Bottom bar: Open logs folder + elapsed in 11pt mono tabular.

### 4.3 Project · Plan (routing grid — full spec in §5)

Top bar (52pt): workflow pop-up · preset chips (Balanced · Max Quality · Economy · Custom · saved presets) · right-aligned "Save as Preset…" and an unsaved-changes pill ("Edited — Revert / Apply"). **Center:** the Routing Grid in a 12pt-radius card, horizontal scroll past 4 agent columns with frozen phase gutter (page never scrolls sideways). **Below: Consequence Strip** (§5.6). **Bottom disclosures:** Quality Gates (repair-rounds stepper, min-output-length guard) · Timeouts (per-agent override table) · Fallback Overrides (project-level ladder overrides, rendered dimmed-inherited until edited).

### 4.4 Project · History

Master-detail. Left (280pt): run list — date, outcome badge, duration, fallback count (purple badge if > 0). Right: per-run **IntegrityStrip** — one thin horizontal bar per phase colored by who *actually* did the work: primary agent tint for primary calls, **45°-hatched purple segments labeled with the rescue model's name** for rescued spans *(graft: Mission Control's hatched-block treatment)* — "round 2's reviews were 100% rescued" is visible at a glance. Below: phase-by-phase accordion reusing `TranscriptView`, with restyled verify-chip badges (build ✓ · tests ✓ · lint ⚠). A quality-gate rejection chip ("review ⚠ rejected ×3") **deep-links to the exact grid cell that caused it** *(graft: Mission Control — config and consequence one click apart)*.

### 4.5 New App intake (sheet, 560×620)

20pt padding. Title "New App" (20pt semibold) + caption "It will be checked for uniqueness against past apps." **Name field** (auto-slugged from idea). **Idea prompt:** borderless TextEditor in a 12pt-radius quaternary well, 140pt. **Docs:** dashed drop-well (`arrow.down.doc`, "Drop .md .txt .pdf or Browse…"); attached docs as 32pt QuickLook rows; "Backfill spec from docs" toggle slides in at ≥ 1 doc. **Workflow:** horizontal 120×80 cards with phase-dot strips. **Routing:** preset segmented control + "Customize after creation opens the Plan tab" hint — **the sheet never shows the matrix; intake stays fast** *(Mission Control's rule)*. **Position:** segmented End of queue · Next · Run now, with live caption "Will start in lane 2, position 4." Footer: Cancel / **Add to Queue** (⌘↩, borderedProminent).

### 4.6 Activity (usage — honest by design)

Header: date-range segmented (Today · 7d · 30d). Provider tiles (Claude/Codex/Gemini/Local): call count in 24pt rounded semibold + delta. Directly beneath, always visible: *"Subscription CLIs report invocations, not tokens. Counts are CLI calls; costs are not estimable."* Below: stacked calls/day bars (capsule stacks, agent identity hues). Below: **Fallback burden** table — per agent: primary calls vs rescued calls (count + %), purple-tinted when rescue % > 10 — the weekly-review surface where 133 silent fallbacks become a trend. Below: local-model utilization with active-limit indicator ("2 of 3 active slots"). Optional **user-entered plan cost/month** enables one derived "calls per plan-dollar" stat, explicitly labeled as user-supplied *(graft: Conductor)*. Where Ollama reports tokens, a separate section shows them. No invented dollar signs anywhere.

### 4.7 Library · Models & Agents

Grouped by provider. Agent rows (64pt): identity avatar, name + CLI version (from probe), default model popup, default **EffortPicker** (Claude and Codex both; Gemini/local render it disabled at 40% with "no effort control" caption — the user learns the capability map by seeing it), install status with Fix button. **"Probe All"** toolbar button pings every agent/model and updates badges + grid INVALID states *(graft: Mission Control)*. Each agent card carries its **Fallback Ladder**: drag-reorderable rungs (rank, model in mono 12, effort notches, 7-day rescue-count badge that turns purple > 10); local rungs show size captions ("M2 · 3B"); a warning banner when a < 7B model sits at a review-capable rung: "Small models produce low-quality reviews — 133 rescues went to llama3.2-3b last week." **Local Models** section: Ollama roster (size badges, enable toggles) with the **Active limit** stepper pinned in the section header.

---

## 5. The Routing Grid — signature control

One component, three altitudes, one source of truth: **glance** (agent avatars on the phase timeline) → **summary** (Inspector › Plan Summary with provenance + Revert) → **editor** (the Plan tab grid; the same component embedded in Settings › Defaults for the global grid — users learn it once).

### 5.1 Geometry

- **Rows = phases** of the selected workflow, execution order, from workflow JSON (custom workflows just work). Row gutter (140pt, frozen): phase name 13pt semibold, rounds stepper ("× 2", ± on hover; workflow-fixed phases show "× 1" locked), workers-per-round where supported. Row context menu: Copy row · Paste row · **Apply row to all phases below** *(graft: Mission Control)*.
- **Columns = agents**, 132pt. Header: 20pt tinted avatar, name 12pt medium, **enable switch** (off dims column to 30% and removes the agent everywhere), **ladder badge** (`link` + chain depth; click → ladder popover with rescue-count history). Column context menu: Copy column · Paste column · Reset column to defaults.
- Frozen gutter + horizontal scroll implemented as synchronized ScrollViews (LazyVGrid has no native frozen column — known cost, budgeted in M3).

### 5.2 Cells — `AssignmentChip`, 124×44pt, 8pt radius, agent tint 8/12% fill + 25% stroke

- Line 1: model short-name, 12pt medium (full IDs only in the picker, 11pt mono).
- Line 2: **EffortGauge** — three ascending bars (3/6/9pt), 1/2/3 filled = Low/Med/High, filled bars in agent tint.
- **Provenance states** *(merged Native Pro + Conductor)*:
  - *Inherited* (fleet default / preset): normal rendering
  - *Overridden here*: 5pt accent dot top-right + stronger stroke; hover "Overrides default (codex-o4 medium) — Revert"
  - *Pinned by workflow*: small lock glyph, read-only, explainer popover *(graft: Conductor)*
  - *Off* (agent skips phase): dashed hairline outline, faint `plus` on hover
  - *Invalid* (fails `codex_model_probe`/roster check): amber dashed border + `exclamationmark`, tooltip "model not found — will use fallback chain" *(graft: Mission Control — surfaced at config time, before a run wastes an hour)*
- **Telemetry reconciliation** *(graft: Conductor — the grid is an instrument, not a form)*: if the last run executed this cell on a rescue, a persistent **2pt purple underline** appears; hover: "ran on fallback llama3.2-3b × 133"; click actions: **Make fallback primary · Fix chain**.
- **Degradation forecast**: if the cell's model is currently in cooldown/unavailable, a 10pt amber triangle; tooltip "opus in cooldown — next run would start on fallback: sonnet."

### 5.3 Editing

**Single click** selects (accent focus ring) → the **Inspector becomes the persistent cell editor** *(non-modal; graft: Mission Control)*. **Return / double-click** opens the fast popover (280pt):
1. **Model picker** — searchable, grouped "Recommended for {phase}" / tier (Frontier / Fast / Local); rows: name, 11pt mono ID, size caption, **relative-cost glyph $ / $$ / $$$ by tier** *(graft: Mission Control — honest cost intuition, no fake economics)*; local < 7B rows carry a persistent caution glyph. Type-to-filter opens palette-style *(graft: Conductor)*.
2. **EffortPicker** — segmented Low/Med/High with bar glyphs; one shared component everywhere effort is set. Footnote names the exact serialization: Claude → `--effort low|medium|high`, Codex → `model_reasoning_effort` — mechanism visible, never mysterious. Gemini/local: disabled at 40% + "This agent has no effort control."
3. Rounds override stepper ("Inherit (2)") · "Remove from phase" destructive text button · read-only fallback preview ("Rescue: sonnet-4 → qwen2.5-7b") linking to the ladder.

### 5.4 Spreadsheet ergonomics *(graft: Mission Control — what makes it Numbers, not a settings form)*

Arrow keys move cell focus; Tab/⇧Tab horizontal; Space/Return edit; **⌘1/2/3 set effort directly**; scroll-wheel on a focused cell nudges effort *(graft: Conductor)*; **⌘C/⌘V copy/paste cells, rows, columns** (payload `{model, effort}`); **⇧-arrows multi-select, then any edit applies to all selected** ("set 4 cells to medium" in two clicks); Esc deselects. **⌥-drag fill-paint across cells is a documented fast-follow**, not a v1 blocker (feasibility ruling). Full VoiceOver labels: "Build phase, Codex, gpt-5.2, medium effort, 2 rounds, overridden."

### 5.5 Presets

Chips: **Balanced · Max Quality · Economy · Custom** + saved presets + "Save current as Preset…". Selecting animates all chips (0.25s spring). **Presets are starting points, not modes**: touching any cell flips to Custom and never silently discards edits. Definitions editable in Settings › Defaults. Semantics: Max Quality = opus-high everywhere + codex-high build; Economy = sonnet-medium spec, codex-medium build, local-7B reviews, claude-sonnet verify.

### 5.6 Consequence Strip *(graft: Mission Control — the grid's conscience)*

Live-computed, 72pt below the grid: **"≈ 38–52 CLI calls per run · 3 phases at high effort · heaviest: spec (opus, high)"** — call arithmetic from rounds × agents × phases, never fake dollars *(Conductor's honest cost hint, same line)*. Quality-floor warnings render here in amber with **jump-to-cell links**: "⚠ review → qwen2.5:3b (3B) — models under 7B produced invalid reviews in 92% of past runs." Grounded in `events.jsonl` history when it exists; static size-floor rule before then.

### 5.7 Persistence

The grid IS `model_routing.json` — per-project file in the project dir, resolved over the fleet default (engine already merges workflow `overrides` presets, commit `fc4b69f`). Apply writes atomically; Revert re-reads; changes apply from the next phase boundary (caption says so). A **"file changed on disk" banner** appears if the engine or another editor touches the JSON while the grid is dirty — the store already file-watches, so this is nearly free *(graft: Mission Control)*.

---

## 6. Run Health & Fallback Visibility

**Grammar:** one state vocabulary at every altitude. Accent = running · green = success · amber = warning/stall/quality · **purple = fallback, exclusively** · red = error/failed · gray = idle/cooldown. Always symbol + word, never color alone.

**Level 1 — Toolbar (never hidden):** Fleet Health capsule — green dot "All healthy" / amber "1 stalled" / **purple "2 fallbacks active"** / red "1 failed"; colored by the worst current state; click → popover with jump links. Beside it, the **Fallback bell** with purple 24h count + ledger popover. Silence is structurally impossible: the 133-fallback incident would have read "133" in the toolbar all week.

**Level 2 — Sidebar:** Needs Attention section materializes with amber badge (purple ring when fallback-caused); running rows carry 6pt health dots.

**Level 3 — Agent Board (§4.2):** status pills + **planned→actual mono diff line** + output sparkline + short-output tags. Fallback transition pulses the row's border purple once (600ms), then holds a steady 3pt purple left border for the remainder of degraded operation — **degradation is a state you see, not a moment you miss** *(Conductor's rule)*.

**Level 4 — Event feed + Activity:** every engine event is a row; fallbacks never fold into "info"; identical events aggregate "× N"; errors carry exact-call Retry; raw stderr behind mono disclosures. Activity's Fallback-burden table is the historical record; History's IntegrityStrip is the per-run record.

**Controls at the point of pain:** agent-row menu (Retry now · Skip this round · **Promote fallback for this run** with undo banner) · phase-rail Retry on failed segments · stall banner ("Build hasn't reported for 4 min — Wait / Restart phase / Kill run"; banner, not alert — the engine often recovers) · run-failure banner offers "Resume with current plan / Edit plan first" (Plan tab is one tab away, by design).

**Escalation thresholds (defaults, editable in Settings › Engine):** 1 fallback → purple event row. ≥ 3 consecutive fallbacks on one agent, or any rescue to a < 7B model on a review/verify phase → project health purple, macOS notification ("BackTimer: reviews degraded to llama3.2-3b"), Fleet capsule counts it. Timeout/stall → red + notification + Needs Attention.

**Data contract:** the engine appends structured events to per-project `events.jsonl`: `{ts, project, phase, round, agent, kind: call|fallback|timeout|cooldown|retry|phase|gate, model_requested, model_used, dur, exit, output_len, detail}`. The engine already knows these moments (`procutil.py`, `localmodels.py` make the fallback decisions); today it just doesn't emit them — which is *why* fallbacks were silent. Surfacing, not new logic.

---

## 7. Per-Project Run-Config Flow

**Lifecycle:** ⌘N sheet (idea, docs + backfill, workflow, preset, queue position) → project created inheriting fleet defaults → **Plan tab** to customize (grid + gates + timeouts + ladder overrides, all with provenance dots and Revert) → Inspector shows the live summary while you watch the Run tab → mid-run changes apply at the next phase boundary → point-of-pain overrides from the Agent Board ("Promote fallback for this run" = temp override, undo-able) → History reconciles intent vs reality (IntegrityStrip, cell underlines) → "Save current as Preset…" promotes a good plan to the fleet.

**Every required control surface has exactly one home:**

| Control | Home | Persists to |
|---|---|---|
| Workflow choice | Plan tab top bar (intake sets initial) | project config |
| Agent participation | Grid column toggles / Inspector › Agents | model_routing.json |
| Per-agent model + effort per phase | Grid cells (**Claude `--effort` and Codex `model_reasoning_effort` at parity**) | model_routing.json |
| Rounds per phase | Grid row gutter steppers | workflow overrides |
| Fallback chains | Models & Agents cards (fleet) / Plan tab overrides (project) | agent config |
| Fallback visibility | Bell · capsule · pills · purple events · burden table · IntegrityStrip | events.jsonl |
| Run health + retry | Agent Board pills/menus, event-row Retry, stall banners | — |
| Concurrency (lanes 1–9) | Sidebar footer, always visible | engine config |
| Queue reorder | Sidebar Queued `.onMove` | .orch-queue-order.json |
| Local roster + active limit | Models & Agents section header | config.yaml |
| Timeouts / heartbeats | Settings › Engine (fleet) / Plan disclosures (project) | config |
| Quality gates (repair rounds, output floors) | Plan tab disclosures; defaults in Settings | config |
| Intake (idea, docs, backfill, workflow) | ⌘N sheet | project |
| Cost/usage (honest) | Activity screen | call counts + events.jsonl |

---

## 8. Migration Plan — six milestones, each independently shippable

All GUI paths under `orchestrator-v2-source/gui/Sources/OrchestratorGUI/`. `OrchestratorStore.swift` (2,378 lines) remains the single ObservableObject throughout; new surfaces get lightweight derived models, never a store rewrite. Realistic total: **4–6 weeks part-time** (the judges rated 3 weeks as ~50% optimistic).

**M0 — Tokens + guardrails (1–2 days, zero visual risk).** Create `ThemeTokens.swift` (`DS` enum: dynamic NSColor pairs from §2, spacing/radius scales, type ramp, `AgentIdentity` map with tint/symbol/effort-capability). Bump `Package.swift` platforms `.macOS(.v13)` → `.v14` (required for `.inspector`). Mechanically sweep the 27 `Color(red` sites (Models.swift ×18, ContentView.swift ×5, TranscriptView.swift ×3, Components.swift ×1) — both legacy UIs instantly share one palette with correct light/dark. Add the CI grep rejecting `Color(red:` and hardcoded font sizes outside `ThemeTokens.swift`. Build the component kit in `Components.swift` (or a `Components/` folder): StatusPill, AgentAvatar, EffortGauge, Sparkline, StatTile, Chip — pure views, `#Preview` in both schemes.

**M1 — New shell around old organs (3–5 days).** `AppShellView.swift`: NavigationSplitView + `.inspector`, behind a three-state flag **`ui.mode = classic | factory | pro`** *(graft: Mission Control — matches the real two-UI starting state)* with a Settings picker. Sidebar reads the same store arrays the 460pt queue panel reads today (`Project`/`ProjectStatus` in Models.swift map 1:1 to the four sections); `.onMove` writes `.orch-queue-order.json` via the existing store call. Toolbar items + ⌘N/⌥⌘I/⌘F wiring in `OrchestratorApp.swift`. Content embeds the existing `TranscriptView` and detail views unchanged — the shell ships hollow but functional.

**M2 — Inspector + Settings shrink + intake (3–5 days).** `ProjectInspectorView` (three tabs) absorbs per-project config from `Configuration.swift`'s 921-line tab pile, reading/writing the same files through existing store methods. Settings shrinks to General/Engine/Defaults/Advanced. New App sheet ports `WorkflowBuilder.swift`'s intake + docs-attach/backfill logic (already landed in commits `d54d573`/`fc4b69f`) into the §4.5 sheet.

**M3 — Plan tab: the Routing Grid (1.5–2 weeks, the centerpiece).** `RoutingGridView.swift` + `AssignmentChip` + `EffortPicker` + ladder editor (rework `ModelLibrary.swift` into Models & Agents with agent cards + Probe All). Model layer: `RoutingMatrix` struct (phase × agent → model/effort/rounds, with inherited/overridden/pinned resolution) serializing to `model_routing.json`, per-project scoped. Engine (additive): per-project routing-file resolution in `workflows.py`; Claude `--effort` pass-through in the agent runner (one-line CLI-arg addition). Ship keyboard nav + copy/paste + multi-select + context menus + presets + Consequence Strip + file-changed banner. ⌥-drag fill deferred to M5. Same component embedded in Settings › Defaults.

**M4 — Run health + Activity (1.5–2 weeks).** Engine: emit `events.jsonl` per project (§6 schema) from `procutil.py`/`localmodels.py` decision points. GUI: grow the file-watching bridge (`TranscriptParser.swift` pattern) with a JSONL tail-reader; store publishes `[EngineEvent]` + per-agent `AgentRuntimeState` (plannedModel vs actualModel). Build `PhaseTimelineView`, `AgentBoardView`, `EventFeedView` (with aggregation), Fleet Health capsule, Fallback bell, sparkline anomaly tags (client-side thresholds), stall banners, macOS notifications, History `IntegrityStrip`, and the Activity screen (call counts + fallback-burden from events.jsonl).

**M5 — Palette, polish, soak, then delete (1 week).** ⌘K palette as an overlay dispatching the extended `store.uiCommand` enum. ⌥-drag fill-paint on the grid. Accessibility gate: Dynamic Type verified on every screen except the two documented opt-outs; VoiceOver labels on AssignmentChip and Agent Board rows required before merge. Flip `ui.mode` default to `pro`; keep `classic` one release as the escape hatch; then execute §9.

---

## 9. What Gets Deleted — the two-UI split ends here

- **`FactoryDashboard.swift` — all 1,447 lines.** The terminal well ("$ tail -f agent_messages.md"), the lime #9FEF00 accent, the GitHub-dark hardcoded palette, the 460pt queue panel, the ● ✓ ✗ ◌ glyph vocabulary, forced-dark, the all-mono type system. Gone whole.
- **The Classic browser body in `ContentView.swift` (~891 lines today)** — the parallel Apple-Mail-style split view and its duplicated magic-number RGBs. `AppShellView` is the only shell.
- **The `ui.mode` flag itself**, after one release of soak — no permanent dual-UI maintenance tax.
- **Per-project tabs in `Configuration.swift`** (921 → ~4 fleet-level tabs): Model Routing tab (→ Plan tab / Settings › Defaults), fallback-chain tab (→ Models & Agents), per-project rounds/timeouts/gates (→ Plan tab), lanes (→ sidebar footer).
- **All hardcoded colors and font sizes outside `ThemeTokens.swift`** — enforced forever by the M0 CI grep.
- **JSON-file-as-interface**: `model_routing.json` and queue-order files remain the persistence format but are never again the primary control surface; "Export plan…" is the escape hatch.

One adaptive UI, light and dark from one token file, one store, one action layer — and an engine that is no longer allowed to fail quietly.