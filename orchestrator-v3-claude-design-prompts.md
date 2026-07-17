# Claude Design prompts — Orchestrator V3 "Sections"

Run these at claude.ai/design, in order, in ONE design project so later
prompts build on the system the first one establishes. Each prompt is
self-contained (paste as-is). They encode the repo's Native Pro doctrine
(`gui/DESIGN-NATIVE-PRO.md`) so the output matches what `StyleGuardTests`
and `ci_style_check.sh` will enforce when the design comes back into
SwiftUI. The `/design-sync` skill can later pull the resulting design
system back into this repo.

Model/effort: use the strongest model in the picker with extended
thinking ON for Prompt 1 (it sets every token downstream) and Prompt 3
(densest information design). Iterations and variant passes can drop to a
faster model. See the chat summary for specifics.

---

## Prompt 1 — Design system foundations + core components

```
Design a component system for "Sections" — a macOS desktop app (design at
1280×800+, light AND dark for every component) where a solo founder runs
multiple AI agents that debate each other in chat rooms, organized into
specialized studios (Ideas, Research, Planning, Design, Build, QA,
Documentation, Go-to-Market, Legal, Operations, Library). The feel:
"Apple built an internal tool and polished it for sale" — Xcode's
structure, Things 3's calm. Modern, simple, magical-but-never-flashy. The
magic is speed of comprehension: who is talking, what is flowing where,
what needs me. No gradients, no glassmorphism, no decoration.

HARD TOKEN CONSTRAINTS (this system already exists in code — extend it,
don't reinvent it):
- Surfaces: neutral layered macOS semantics. No pure black or white. Cards
  elevate by a fill step, not shadows — no shadows at rest, 1px hairline
  borders everywhere, never 2px.
- Accent "Conductor Indigo" #4A56C7 (light) / #8A93F2 (dark): selection,
  primary buttons, and EVERYTHING "running/in-progress".
- Agent identity hues (identity ONLY, never state): Claude terracotta
  #C4643B/#E08D63 · Codex teal #0E7E74/#4DB8AC · Gemini cobalt
  #3467D6/#7CA2F0 · Local-model slate #5B6472/#9AA3B2.
- Status: success #1E7F3C/#46C065 · warning amber #B25E09/#E8A13D ·
  fallback purple #8250DF/#B394F5 (purple means "a fallback happened",
  exclusively, never decorative) · error #C4302B/#EF6E63.
- Tint formula for every pill/chip/avatar: fill at 8% (light) / 12%
  (dark), stroke at 25%, content at 100% of the hue.
- No color-only encoding: every status is symbol + word.
- Type: SF Pro; 26 semibold (empty states only), 20 semibold (titles),
  15 semibold (card titles), 13 regular (body workhorse), 12 medium
  (chip labels), 11 regular (metadata). SF Mono ONLY for machine output,
  model IDs, file paths, elapsed timers. Semibold is the max weight —
  hierarchy comes from size + color.
- Spacing on a 4pt grid: 4/8/12/16/24/32; 24pt content margins. Radii:
  6 (badges), 8 (chips), 12 (cards/sheets).
- Motion: one spring (response 0.35, damping 0.8); a 1.6s breathing
  status dot for "running"; a single 600ms pulse when a fallback occurs,
  then steady. Reduce-motion variant: opacity fades only.

COMPONENTS TO DESIGN (each in light + dark, with states):
1. Chat bubble set: human message, agent message (agent-hue avatar +
   persona chip like "Skeptic" or "Investigator"), a "thinking…" shimmer
   bubble (for CLI agents that emit nothing until done), and a
   token-streaming bubble (for API agents typing live).
2. Round divider ("Round 3 of 9") and a PASS slip (a quiet gray line for
   an agent that abstained).
3. Consensus card ("Final Output"): the debate's settled result — a calm,
   satisfying landing, with a route-preview chip showing WHERE this
   result will go next ("→ Research").
4. Artifact card (in-stream): type glyph, title, version chip (v2),
   lineage breadcrumb (idea-7 → brief-12), status badge
   (draft/final/superseded/stale-input), drag affordance.
5. Composer: text field + "brains in the room" strip (agent avatars,
   add/remove) + two-tier ask control: "Quick take" vs "Deep dive" when
   @-mentioning another studio (e.g. "@Research").
6. "Step in" bar: a composer variant shown under a LIVE agent debate —
   typing pauses the debate at the next round; show a subtle "joining at
   round 4" countdown state.
7. Status pills (running/waiting-on-you/converged/stalled/fallback/error)
   obeying the tint formula, symbol + word.
8. Approval card: a proposed autonomous action ("Route brief-12 →
   Ideas · rule #3") with Approve / Redirect / Dismiss.
9. Budget meter: a compact gauge for turns / cost / provider quota with a
   quota-cooldown state.
10. Section rail row: studio name, one-line live status ("Ideas —
    debating, round 3" / "Research — 2 running, 1 waiting on you"), quiet
    activity glyph, unread/needs-attention badge.
```

## Prompt 2 — App shell + the four chat moments

```
Using the Sections design system in this project, design the app shell
and its four defining screens (macOS desktop, 1440×900, light + dark).

SHELL: One window, three regions. Left rail (~230pt, sidebar material):
project switcher at top, then the studio list (Ideas, Research, Planning,
Design, Build, QA, Documentation, Go-to-Market, Legal, Operations,
Library) using the section-rail rows from the system — plus a Mission
Control entry pinned at the bottom with a tiny live activity glyph.
Center: a pane canvas — ONE chat pane full-width by default; a second
session can be dragged in to split 50/50 (max 3 columns; an overflow
strip holds the rest as tabs). Right: a 340pt inspector for the focused
pane (phase plan, cast, artifacts consumed/produced, session settings).
Toolbar: fleet-health capsule, fallback bell with count, New Chat
(primary), ⌘K hint.

SCREEN 1 — Manual chat (Ideas studio): user mid-conversation with two
agents; composer with the brains strip; one earlier message promoted to
an artifact card inline.

SCREEN 2 — The debate theater (auto mode, the signature screen): three
agents (terracotta/teal/slate avatars, persona chips "Visionary" /
"Skeptic" / "Closer") debating live in Ideas, round divider "Round 3 of
9", one thinking-shimmer bubble, one PASS slip, and the "Step in" bar
below. The user is an audience — make watching feel calm and legible,
not chaotic.

SCREEN 3 — Consensus: the debate just ended; the Final Output card lands
with the route-preview chip "→ Research"; a subtle settle animation
frame; the section rail shows Research ticking to "1 queued".

SCREEN 4 — Concurrency: split canvas — left pane is the user manually
chatting in Research; right pane is a LIVE autonomous brainstorm running
unattended (auto-mode header badge); the rail shows a third session
running in QA in the background. This screen must instantly answer "what
is happening everywhere, and does anything need me?"
```

## Prompt 3 — Mission Control + pipeline builder

```
Using the Sections design system in this project, design the two
autonomous-orchestration surfaces (macOS desktop, 1440×900, light+dark).
These views are where trust in autonomy is won: every machine decision
must be visible, attributable, and reversible. Calm information design;
indigo = running, purple = fallback only, amber = needs attention.

SCREEN 1 — Mission Control: the live map of a global autonomous run.
(a) Node map: 11 studio tiles arranged in a loose flow (thinking studios
left, doing studios center, Documentation right as the terminal
assembler); under each tile, small session chips with status dots; small
artifact motes travel along edges when something routes (show one
mid-flight). (b) Below: the decision ledger — chronological one-liners
("23:41 routed brief-12 → Ideas · rule #3 · v2 supersedes v1"), each
with a jump link. (c) Right column: pending-approval tray (approval
cards in suggest-only mode), budget meters (turns, spend, per-provider
quota with one provider in cooldown), and the goal readout:
"Documentation 31/40 slots filled · 3 gaps assigned · 2 unresolved
conflicts" — the run's finish line, always visible.
(d) A replay scrubber along the bottom: drag to reconstruct the night;
show a scrubbed-to-2:13am state variant.

SCREEN 2 — Pipeline builder: a canvas where the user composes a custom
cross-studio workflow. Studio nodes, directional edges with condition
labels ("when idea published", "when brief has confidence ≥ medium"),
an edge being dragged, a goal card ("done when: spec exists"), a preset
bar (save/load named pipelines like "Idea → Validated Spec"), and a
prominent but calm "Run pipeline" action with an oversight-dial control
(Full auto / Suggest only / Gated / Loops-gated ← default). Include an
empty state that teaches the mental model in one sentence.
```

## Prompt 4 — Artifact provenance, studio settings, onboarding

```
Using the Sections design system in this project, design three supporting
surfaces (macOS desktop, light + dark).

SCREEN 1 — Artifact detail + lineage: an inspector-style panel for one
artifact (research brief v2): metadata (type, source studio/session/
phase, keywords, doc slots it fills), body preview, and a horizontal
lineage trail (idea-7 → brief-12 v1 → brief-12 v2) with a BRANCH
CONFLICT state: two v3 branches (v3-a / v3-b) produced concurrently by
the user and an autonomous run, with a "Reconcile" call-to-action.
Also show the stale-input badge state on a consuming card ("built from
v1 — rebuild with v2?").

SCREEN 2 — Studio settings sheet (for the Research studio): tabbed —
Phases (ordered phase list with rounds + purpose), Rules (per-phase
playbook text), Cast (persona cards with agent-hue avatars; one guest
persona borrowed from another studio, labeled "from Ideas"), Models
(per-phase model/effort grid), Routing (default outbound routes), and a
provenance footer ("Customized · 3 overrides — Revert"). Show the Cast
tab active.

SCREEN 3 — First-run onboarding, two frames: (a) "Your brains" checklist
— detected CLI logins (Claude ✓, Codex ✓, Gemini — sign in), local
models (none — "Get one" opens a model library row), API keys (optional,
"metered" tag); one primary action "Start your first brainstorm".
(b) The guided first moment: an Ideas chat pre-seeded with a starter
prompt, a coach-mark pointing at the "Let them discuss" control, and
only TWO studios visible in the rail (Ideas, Research) with a hint that
others appear as work flows to them. Progressive disclosure — the app
must not open as a wall of eleven empty studios.
```

## Prompt 5 — Situations, document builder, and the command layer

```
Using the Sections design system in this project, design the customization
and command surfaces (macOS desktop, 1440×900, light + dark). These are
power-user surfaces — keep them calm and information-dense, never busy.

SCREEN 1 — Situation Library + editor: left, a list of saved Situations
(Full Production App, Prototype Sprint, Research Spike, Launch Push, V2
Iteration, Compliance Pass) each with a one-line scope summary ("12 of 40
doc slots · 4 studios"). Right, the editor for "Prototype Sprint" with
three tabs: Documents (active), Pipeline, Overrides. The Documents tab is
the DOCUMENT BUILDER: a flow canvas of 40 document-slot chips grouped
into 11 category lanes (Product, Design, Frontend, Backend, Business,
Marketing, UA, Execution, QA, Legal, Operations); included chips are
solid with their owning studio's tint, excluded chips are ghosted;
drag-to-include affordance visible. A live impact preview bar at the
bottom: "Will run: Research, Planning, Build, QA — ~23 phases". 

SCREEN 2 — "Apply Situation" flow: a sheet applying Prototype Sprint to a
live project, showing an impact DIFF before confirming: phases added
(green rows), phases removed (struck rows), studios activated, estimated
turns/cost delta, and a note "3 slots already filled — kept". Primary
action "Apply", secondary "Preview gap report".

SCREEN 3 — Agent Library: a grid of custom subagent cards — each card:
persona name ("Investigator", "Skeptic", "App Store Reviewer"), a
one-line charter, backend badge (CLI subscription / API metered / local),
bound model + default effort, and which studios use it. One card in edit
mode showing the persona preamble field. A roster-size control strip
shows the recommended-defaults hint style: "Ideation: 3 diverse + closer
(recommended)" as a quiet, dismissible hint — never an enforced rule.

SCREEN 4 — The command layer in the composer, three states stacked:
(a) autocomplete popover open after typing "/" — commands grouped
(Conversation: /mode /vote /consensus /cast /fork · Artifacts: /promote
/send /audit /research /decision /summarize · Prompt tools: /model-effort
/gen-prompt /snippet /compare · Info: /status /cost /help), each with a
one-line description, fuzzy-match highlighting;
(b) the /model-effort ADVISOR CARD rendered in-stream: recommended model +
effort ("Sonnet @ high"), one-line rationale, estimated cost, and a "Run
with this" primary button — clearly NOT having run the prompt;
(c) the /gen-prompt PREVIEW CARD: the user's rough text transformed into a
structured prompt (Goal / Context / Constraints / Output format /
Acceptance criteria) in an editable card with "Insert" and "Discard" —
again clearly not yet sent. Also show the snippet autocomplete state
(typing "/snippet au…" → "audit-this" preview).
```
