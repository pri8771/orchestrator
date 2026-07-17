# Orchestrator V3 — "Sections": One Modular App of Chat-Based Studios

Status: PLAN (not yet approved for build). Produced 2026-07-17 from a judged
multi-agent design panel (3 independent proposals → 2 judges → completeness
critic) grounded in a subsystem map of this repo.

Verdict up front: **this is an evolution, not a rewrite.** Roughly 80% of the
required machinery already exists in this repo — the debate engine, pluggable
workflows (`brainstorm.json` and `research.json` are already chat-only
workflows), per-phase rules, persona roles, model routing, local-model
manager, events feed, docs renderer, portfolio minting, locks, and a
design-system-enforced SwiftUI GUI. What's missing is the **interactive chat
layer**, the **artifact bus** between sections, and the **Conductor** for
global autonomous mode.

---

## 1. Vocabulary (freeze before any code)

The panel's three proposals used colliding names for every core noun; naming
gets baked into directory layouts and is a migration to change later.

- **Project** — one product/effort (e.g. "CupDeck"). Top-level container.
- **Section** — one specialized studio (Ideas, Research, Documentation, …).
- **Chat** (user-facing) / **session** (on disk) — one conversation inside a
  section. `workspace/<project>/<section>/<chat-slug>/`.
- **Run** — one engine process executing a session.
- **Agent thread** — a backend continuity key (`call_agent_sessioned`); never
  called a "session" in UI or code comments. Forking a chat must NOT clone
  agent-thread ids (codex resume is only sandbox-safe in write phases).
- **Artifact** — a typed, versioned output published to the bus.
- **Conductor** — the global autonomous router (one per workspace).
- **Situation** — a named, applyable bundle of {document flow (required doc
  slots) + pipeline preset + section/phase/cast overrides} (§14). Applying a
  Situation to a project re-scopes what the Conductor considers "done".

Write `GLOSSARY.md` with these before Phase 1; check PRs against it.

## 2. The section taxonomy (11 sections)

Sections are data (see §6), so this list is the default set, not a limit.
It deliberately covers the Notion 11-category standard so the Documentation
section can assemble the full 40-section handoff blueprint.

1. **Ideas (Brainstorm Studio)** — diverge/converge idea generation. Seeded
   from `workflows/brainstorm.json`. Cast: Visionary/Skeptic/Closer.
   Outputs: `idea` artifacts, "investigate this" requests, kill/park
   decisions (so dead ideas don't resurface).
2. **Research** — chat-first investigation of an idea or any topic. Seeded
   from `workflows/research.json`. Outputs: `research_brief` artifacts and
   `opportunity_signal`s routed back to Ideas (the bidirectional loop you
   described is literally these two artifact types routed in opposite
   directions).
3. **Planning & Spec** — idea + research → PRD, scope tiers, task graph.
   Seeded from `app_spec.json` + project_plan/task_assignments phases. The
   hinge between thinking sections and doing sections.
4. **Design** — flows, IA, design language, copy. Seeded from
   design_discussion/design_handoff phases + the Native Pro doctrine.
5. **Build (Prototype & Engineering)** — the existing app factory as one
   section among peers (app_build, prototype, vslice, productionize, lanes,
   verify, repair). The only section whose phases write code.
6. **QA & Red Team** — adversarial critique of ANY artifact, not just builds:
   specs, docs, marketing claims, legal positions. Seeded from `audit.json`,
   red-team roles, quality-grader, visualqa, evalharness.
7. **Documentation** — the assembly hall. Maintains the canonical doc set
   mapped 1:1 onto the App Factory 11-category / 40-section standard.
   Mostly deterministic (`docs.py` render from artifact map); LLM phases only
   for gap-filling and coherence. Its **gap report drives the global loop**.
8. **Go-to-Market** — Business + Marketing + User Acquisition categories in
   one section (splittable later, since sections are data).
9. **Legal & Compliance** — privacy policy, ToS, App Store compliance,
   claims review. Emits blocking `legal_flag`s the Conductor treats as gates.
10. **Execution & Operations** — release checklists, runbooks, postmortems;
    consumes `portfolio.py` to mint follow-on projects (v1.1, siblings).
11. **Library & Knowledge** — cross-project long-term memory; wraps
    `knowledge.py` + `library_mining.json`; surfaces "you've solved this
    before" hints in Ideas and Planning.

## 3. Chat modes per section (all reuse one engine)

**Auto mode (LLMs talk to each other, watched live).** This is
`process_phase` unchanged: round-barrier debate, persona rotation,
coordinator `CONSENSUS: YES`, quality gate, forced weighted vote. The watch
surface already works (transcript .md + events.jsonl → TranscriptParser
bubbles). Upgrades: 500ms polling for the focused pane; token streaming for
API backends (§9).

**Manual chat mode.** Not a new engine — a **`conversational` phase flag**:
each round = drain `human_inbox.txt` → selected agents respond → wait for the
next human message (generalizing `_await_approval`'s poll into an
inbox-wait); no coordinator/consensus; the phase ends when the user ends it.
Continuity via `call_agent_sessioned` delta prompts. Event-driven inbox wake
(~250ms) so chat feels immediate.

**Lifecycle verbs, uniform everywhere:** New chat (mint session dir),
Continue (reopen; crash resume free via `_resume_round_state`), Fork (copy
dir minus locks + agent-thread ids), **"Let them discuss"** (promote a manual
chat into an auto debate seeded with the transcript), **"Step in"** (typing
into a live debate pauses it at the next round barrier — the room visibly
turns to face you; this is the product's signature moment and rides existing
inbox machinery), and **Promote** (publish any message as an artifact so
casual chats can feed the bus).

**Legible autonomy rule (adopt verbatim):** always show WHO is speaking
(persona chip), WHY (phase purpose on hover), and WHERE the result will go
(route-preview chip on the coordinator's final bubble).

## 4. The artifact bus (any section → any section)

A typed, versioned, file-based store generalizing three proven mechanisms:
`carryover_outputs` (push), `knowledge.retrieve()` (pull), `portfolio.py`
(output-mints-work).

- **Structure:** `workspace/<project>/artifacts/<id>/` = `body.md` +
  `meta.json` {id, type, source {section, session, phase, turn}, version,
  supersedes, lineage[], content_hash, keywords[], doc_slots[], status}.
  Artifact **types are a JSON registry** — extensible without code.
- **Publish:** consensus output + a fenced ```artifact-json``` block
  (extracted by `schemas.extract_structured_blocks`, same pattern as
  portfolio-json/tasks-json). Emits `artifact_published` into events.jsonl
  (new KINDS entries, test-covered).
- **Consume, three coupling grades:**
  1. **PUSH** — Conductor route or manual "Send to →" / drag a card onto a
     section in the rail; lands via the `prepare_continue`/carryover path as
     authoritative prior context.
  2. **PULL** — `build_context()` gains an artifact-retrieval layer
     (knowledge.py-style scoring, top-k under a char budget, provenance
     headers). Any section passively benefits from any other's outputs.
  3. **SUBSCRIBE** (grafted from the runner-up design) — Documentation
     declares declarative source→doc-slot mappings and passively ingests
     matching artifacts on every render.
- **Loops:** Research→Ideas = `opportunity_signal` routed to Ideas;
  Ideas→Research = `idea` routed back. Loop control lives **on the bus**, not
  in sections: content-hash dedup (identical descendant auto-marked
  "converged", not re-routed), lineage depth caps (default 4), per-chain hop
  budgets.
- **Updates:** re-running publishes v(n+1) with supersedes=v(n); consumers of
  v(n) get a "stale input" badge; supersedes-triggered re-routing is exactly
  your "sections updating each other based on each other's updates."

## 5. Global autonomous mode — the Conductor

A separate small stdlib process (`conductor.py`), one per workspace
(flock-guarded). Sections stay strictly sequential internally (crash-resume
depends on it); the Conductor provides concurrency ACROSS sections. It polls
authoritative state (`agent_state.json`) and treats events.jsonl only as a
wake-up hint.

- **Routing rules are data** (`routing.json`, fleet default + per-project
  overlay, same merge as model_routing.json). Three strategies, matching your
  "one | every | custom" requirement: route-to-one (deterministic
  type→section defaults, falling back to one cheap LLM-classifier turn on a
  local model), broadcast, and user-authored rule chains editable as a visual
  routing table.
- **Driving objective:** a goal manifest — by default "Documentation complete
  to tier T." The Documentation gap report IS the work queue: each empty doc
  slot generates a fill request routed to the owning section. Pull-based
  direction instead of open-ended chatter.
- **Termination, four layers:** (1) goal predicate: gap report empty + DoD
  checklists + `evalharness.score_project` ≥ threshold; (2) quiescence: no
  new non-superseded artifact for N cycles → converged-with-open-items
  report; (3) budgets: turns, wall-clock, per-provider spend/requests
  (quota-aware route deferral — Gemini free tier is ~20 req/day); (4) stall
  detection: `vote_undecided` twice or an oscillating supersedes chain
  escalates to the human.
- **Oversight dials, four positions:** full-auto / **suggest-only** (routes
  queued as tappable approval cards — the gentle on-ramp) / gated (every
  route approved) / **loops-gated (default)** — forward routes auto-execute,
  feedback-loop routes need approval. All via the existing
  `approvals/<phase>.ok|.changes` file contract. Per-decision undo (mark
  do-not-route, kill spawned session).
- **Mission Control view:** calm node map (sections as tiles, session chips,
  artifact motes animating along edges), budget meters, and a chronological
  decision ledger — every choice one explainable line. Every decision is an
  event, so an overnight run **replays as a scrubbable timeline** next
  morning.
- **Crisp distinction:** per-section auto is LLMs debating INSIDE one session
  until consensus (a conversation); global autonomous is the Conductor moving
  artifacts BETWEEN sections until the goal holds (an economy). They compose.
- **Shipping gate:** an eval suite that replays recorded artifact streams and
  asserts routing/termination decisions, including crash-kill at each step of
  the route state machine, before full-auto is enabled by default.

## 6. Customization: SECTION = DATA

`sections/<name>/` is a directory manifest, not a plugin:

- `section.json` — identity, workflow (today's exact Workflow/Phase JSON
  schema), default mode, artifact types emitted/accepted, DoD tier.
- `rules.json` — per-phase playbooks in phase_rules.json format but
  **section-scoped** (lookup order: section → project-override → global —
  fixes the global-key-collision problem, backward compatible).
- `roles.json` — the persona cast.
- `contracts.json` — machine-output contracts externalized from
  `_phase_contract`: {phase_key, fence_tag, required_fields, prompt_snippet}.
  The single most important flexibility unlock.
- `routing.json` — default outbound routes.
- `doc_map.json` — artifact/phase → 40-section blueprint slot + 11-category
  assignment (externalizes docs.py's PRD_SOURCES tuples; the App Factory
  standard ships as the default and is user-editable data).

Editing: seed-then-disk-wins (the proven `ensure_seeded` pattern) — but any
fallback raises a **visible banner**, never a silent swap. Three override
layers, most-specific wins: fleet default → per-project → per-session.
Tooling: `--new-section` scaffolder from `sections/_template/`,
`--lint-section` validator surfaced in the GUI.

**Config vs plugin boundary:** JSON covers 95% (phases, prompts, rules,
casts, routing, contracts, doc maps). Python plugins only at two narrow
seams — new RUNNERS (an existing dict registry, orchestrator.py:1000) and
new VERIFY types (today a hardcoded if/elif chain in verify.py that the
engineering plan first converts into a registry, task 3.9). No plugin can
touch the engine loop.

## 7. Composition: mixed casts, in-chat delegation, and pipelines

(Added 2026-07-17.) Two ways to blend sections, plus a builder for custom
cross-section workflows. The design line to hold throughout: **a persona is
an opinion; a section is a process.** Blur them and provenance and bus
admission control (gap #9) die.

**Guest personas (cheap tier).** A section's cast may reference another
section's roles: `"roles": ["visionary", "skeptic", "research:investigator"]`.
The Investigator thinks like a researcher inside the brainstorm — same
debate, same rounds. Great for flavor and challenge, but it is only a
viewpoint: a guest persona will confidently improvise "research" it never
actually did.

**In-chat delegation (real tier).** `@Research <question>` in any chat fires
a scoped sub-session running the target section's ACTUAL workflow
(decomposition → investigation → adversarial verification → synthesis) and
returns the Research Brief into the calling chat as an artifact card.
Mechanically this is a bus route with a **return edge** — a route whose rule
says "expect a reply artifact; deliver it back into the originating
session's carryover/inbox." It rides `prepare_continue` + `portfolio.py`
minting; lineage stays intact; it counts against budgets, obeys capability
manifests, and appears in the decision ledger — no new safety surface.
Async by design: the brainstorm continues and the card lands when ready.
The composer offers the two tiers explicitly: **"Quick take"** (guest
persona, one turn, seconds) vs **"Deep dive"** (delegated sub-session,
minutes).

**Pipeline builder (custom cross-section workflows).** A pipeline like
brainstorm → research → plan is NOT a new engine construct — it is a saved
preset of Conductor routing rules plus a goal manifest: {when `idea`
published → route to Research; when `research_brief` published → route to
Planning; goal: spec exists}. The builder is a canvas — sections as nodes,
drag edges, per-edge conditions and modes — that compiles to `routing.json`
+ a goal manifest and saves as a named preset (RunProfile pattern;
`WorkflowBuilderSheet` is the editing precedent). "Run pipeline" seeds the
first section with a prompt and hands the preset to the Conductor. Because a
pipeline IS Conductor config, it inherits Mission Control, budgets,
oversight dials, replay, and termination for free — there is no second
execution engine to build or debug. Keep the two workflow layers distinct:
within-section workflows (phase JSON inside one session) and cross-section
pipelines (routing presets). One format spanning both would re-entangle the
sections the architecture just separated. `--continue-with` is this
feature's primitive ancestor.

## 8. Architecture (three processes, file-coordinated)

1. **Section runtime** (evolved orchestrator.py, one process per running
   session). Refactors in priority order: extract the generic debate core
   from `process_phase` (~800 lines; build/iOS special cases behind
   verify/contract seams); replace the mutated shallow-copied cfg dict with
   an explicit TurnContext; add the conversational flag; dual-write each turn
   as byte-compatible markdown (resume + TranscriptParser keep working,
   golden-file tested) AND a `messages.jsonl` line (new UI features read
   JSONL; the regex-parsed-markdown database is retired incrementally).
2. **Conductor** (new, small). Drives sessions through the identical file
   contracts the GUI uses, so the GUI can't tell who launched a run.
3. **GUI** (evolved SwiftUI). Keep file-tailing; split the 3,456-line
   mega-store into fleet store + per-session models (incremental, per repo
   doctrine); pane canvas replaces single selection — one pane full-width by
   default, **drag a second chat in to split** (2–3 max, overflow strip);
   pid-file Stop adoption so Conductor-launched runs survive GUI relaunch.

No sockets, no server, no DB for state. The stdlib-files constraint is why
concurrent sessions, crash resume, and GUI adoption come cheap. Your
concurrency scenario — manual research in one pane while another research
task and a brainstorm run autonomously — is three session dirs, three engine
processes, one `global_resource.py` broker. Structurally supported today.

## 9. LLM backends: CLI + API + local, one seam

Everything passes through `resolve_runner(agent_id)`; fallback ladders,
circuit breakers, pacing, redaction, and events apply to any runner
automatically.

- **CLI agents (keep, default):** claude/codex/gemini headless subprocesses;
  subscription-auth-first; env-key stripping stays.
- **Direct API (new):** `api:<model>` runners (urllib, stdlib) for
  Anthropic/OpenAI/Google. Keys stored in `~/.orchestrator/<provider>_api_key`
  files (the gemini FILE-STORAGE precedent); for api: runners the key goes
  only into the HTTP auth header — never argv, never child env. (The
  existing gemini CLI path injects GEMINI_API_KEY into its subprocess env
  because the CLI requires it; that path is exempt and unchanged.)
  Explicit per-project opt-in with a cost warning. What they buy: true token
  streaming and exact token/cost accounting.
- **Local models (keep nearly verbatim):** localmodels.py IS the
  "download/choose local LLMs" requirement — curated registry, RAM gating,
  HF GGUF search, one-click pull, GUI Model Library. Endpoint of every
  fallback chain so overnight runs degrade instead of dying.
- **Honest capability descriptors** per agent id: {streams, token_usage,
  effort_control, session_resume} — the UI never promises continuity or
  streaming a backend can't deliver.
- **Streaming contract (adopt exactly):** API token deltas write to
  `<session>/.stream/<turn_id>.ndjson`; the file is DELETED on turn
  completion; the transcript block is authoritative. Resume logic and
  TranscriptParser provably never see partial turns. CLI agents render an
  honest "thinking…" shimmer until the turn lands.

## 10. Design language

Keep and extend **Native Pro** — it's a finished asset: ThemeTokens dynamic
light/dark, the 8/12% fill + 25% stroke tint formula, 12-role type ramp,
semantic status grammar, ComponentKit, all StyleGuard/CI-enforced. That
enforcement is what keeps eleven sections feeling like one calm app.

Shell: left rail (project switcher → section list with one-line live status:
"Ideas — debating, round 3"), center pane canvas, right inspector, ⌘K with
session verbs ("new brainstorm", "send to Research", "open conductor").
The magic is **speed of comprehension** — who's talking, what's flowing
where, what needs me — through motion restraint, not gradients-and-particles.
Signature moments: the debate theater, "Step in" (the room turns to face
you), drag-to-route artifact cards, and Mission Control's overnight replay.

## 11. Gaps the design panel missed (now first-class requirements)

From the adversarial completeness pass — these are the "what am I missing"
answers, ranked:

1. **Capability/permission model for autonomous ACTIONS** (the biggest
   safety hole): all oversight gates ROUTING, none gates SIDE-EFFECTS. Add a
   per-section capability manifest enforced by the engine:
   `{writes: none|workspace|repo, exec: none|allowlisted-verify-only,
   external: none|github|notion|network}`. Routes into a section exceeding
   workspace-only always require approval regardless of the dial; external
   effects queue as pending actions. Chat-pure sections ship with zero
   capabilities so the default overnight run is provably side-effect-free.
2. **Same-lineage concurrent writes:** user edits an idea in a pane WHILE an
   autonomous loop routes an update to it → silent fork. Per-lineage
   advisory flock; on collision publish named branches (v3-a/v3-b) + a
   mandatory `reconcile` artifact treated as a blocking gap. Never let
   "latest final" silently pick a branch.
3. **GitHub/Notion sync contract:** docs render into a per-project git
   worktree; Documentation commits with a bot identity at coverage
   milestones; push is an `external` capability. Before every render, diff
   the tree — human edits flip the slot to "human-overridden" (preserved,
   reconcile artifact routed back) instead of being clobbered. Notion
   delivery is an explicit exporter with dry-run diff, never fire-and-forget.
4. **Whole-run rollback:** make the workspace a git repo; Conductor commits
   at every quiescence point and before every routing wave, tagged with the
   ledger cursor. "Roll back the night" = git reset + replay. Free offsite
   backup; makes the replay scrub trivially real. Ship WITH the Conductor.
5. **Idempotent routing under crash:** deterministic `route_id`
   (hash of artifact+target+rule) written to the ledger BEFORE acting; inbox
   injections carry it as a marker line; restart dedups by scanning.
6. **Out-of-app notifications:** macOS UserNotifications for
   approval-needed / stalled / converged / budget-exhausted (one new
   events.jsonl consumer), with quiet-hours batching — otherwise "autonomous
   until done" becomes "autonomous until the first question at 1am."
7. **Privacy tiering:** a `sensitivity` field at project/artifact level
   enforced at the `resolve_runner`/`build_context` seam: local-only material
   never reaches a cloud runner; fallback chains for sensitive sessions
   terminate at local models. One "Private (local models only)" toggle.
8. **Search:** SQLite FTS5 (stdlib) over messages.jsonl + artifacts, indexed
   incrementally; ⌘K jump-to-turn. Sequence WITH the dual-write phase —
   retrofitting after months of markdown-only history is much worse.
9. **Bus admission control:** finalization policy per artifact type:
   `auto_final_on_consensus | requires_review_gate | requires_human`.
   High-blast-radius types (spec, doc_section, anything Build consumes)
   default to review-gated — the existing quality-grader machinery becomes
   the bus's admission control instead of an optional detour.
10. **Doc-slot ownership manifest:** one section owns each blueprint slot;
    cross-lineage conflicts surface as gap artifacts (never silent
    last-write-wins) and are listed loudly in the quiescence report.
11. **Data lifecycle:** archive/tombstone superseded artifacts, exclude
    killed lineages from pull/search, `--gc` with dry-run, one "archive
    project" verb.
12. **First-run onboarding:** probe backends → "brains available" checklist
    with fix-it actions; start with only Ideas + Research visible, sections
    appearing when first routed to (progressive disclosure = the natural
    two-section MVP); one guided five-minute "seed an idea, watch the
    debate, route it to Research" moment.

## 12. Build milestones (value-first ordering)

Sequencing corrections grafted from the judges: chat experience lands FIRST
(prove the magic on the un-refactored engine), and cost accounting lands
BEFORE the Conductor (autonomous budgets meter real spend from day one).

- **M0 — Glossary + layout freeze.** GLOSSARY.md, on-disk layout decision
  (`workspace/<project>/<section>/<chat>/`). Days, not weeks.
- **M1 — Chat spine (user-visible immediately):** conversational phase flag
  prototyped against the existing `process_phase`; per-session chat history;
  event-driven inbox wake; "Step in" / "Let them discuss" on
  brainstorm.json/research.json. Exit: Ideas + Research usable daily as
  manual chats and watchable auto debates.
- **M2 — Engine seams (under the running product):** debate-core extraction,
  TurnContext, messages.jsonl dual-write (byte-compatible markdown,
  golden-file tests, simulate_stream.py as harness), new event KINDS, FTS5
  indexer. Exit gate: all existing workflows AND the current GUI run
  unchanged.
- **M3 — Section manifests:** sections/<name>/ format, section-scoped rules,
  contracts.json externalization, scaffolder + linter, visible-fallback
  banners. Seed Ideas/Research/QA/Planning from existing workflows. Exit: a
  new section works end-to-end from JSON alone.
- **M4 — Artifact bus, manual routing first:** type registry, artifact-json
  publication, Promote/Send-to/drag verbs, pull retrieval, superseded
  badges, lineage locks + branch/reconcile (gap #2). Exit: Ideas↔Research
  round trips by hand, provenance visible.
- **M5 — Documentation section:** doc_map.json, 40-section blueprint as
  default map, slot ownership manifest, gap reports, deterministic render +
  GitHub/Notion sync contract (gap #3). Exit: artifacts assemble into the
  App Factory handoff standard with an honest gap list.
- **M6 — API backends + streaming + cost accounting:** `api:` runners, key
  files, capability descriptors, .stream lifecycle, real token/cost meters.
  (Deliberately BEFORE the Conductor.)
- **M7 — Conductor:** routing rules, goal manifest, four-layer termination,
  oversight dials incl. suggest-only, decision ledger, Mission Control +
  replay, capability/permission model (gap #1), workspace git snapshots
  (gap #4), route idempotency (gap #5), notifications (gap #6), routing eval
  suite as the full-auto shipping gate. Exit: "idea → researched → planned →
  documented" completes unattended overnight and stops by itself.
- **M8 — Full shell + remaining sections + polish:** pane canvas, store
  split, pid-file Stop adoption, seed Design/GTM/Legal/Execution/Library,
  de-iOS-ify global rules into the Build manifest, privacy tiering (gap #7),
  lifecycle/GC (gap #11), onboarding (gap #12), StyleGuard coverage,
  one-shot migrator with dry-run diff for existing tuned configs.

Discipline rule from the winning proposal's own risk list: **M1–M4 must ship
as a usable two-section product before the Conductor is allowed to exist.**

## 13. Top risks

1. Transcript-as-database fragility during dual-write — golden-file tests
   are non-negotiable.
2. Runaway autonomous loops vs. real quota walls — budgets/caps/dedup ship
   WITH the Conductor, loops-gated is the default dial.
3. Termination is genuinely hard — quiescence + oscillation detection are
   load-bearing; without them global mode is a money pump.
4. The engine's fail-open culture (silent fallbacks) is correct for a batch
   factory and harmful in an interactive product — every fallback needs a
   surfaced banner; easy to under-scope.
5. CLI chat latency (zero bytes until a turn completes) — honest "thinking"
   presentation until API streaming lands in M6.
6. SwiftUI mega-store split under a live app — incremental only, golden-path
   UI tests budgeted before touching it.
7. API keys reverse an explicit product decision — runner-private key files,
   env stripping stays for CLI, per-project opt-in.
8. Scope gravity: eleven sections × modes × Conductor is a v3-scale bet —
   the two-section-MVP gate is the defense.

## 14. Situations, the document builder, and the command layer

(Added 2026-07-17, second scope addition.)

### 14.1 Situations

A **Situation** is a named, reusable bundle applied to a project:

- **Document flow** — the subset (and ordering) of the 40 blueprint slots
  this kind of work requires.
- **Pipeline preset** — the cross-section routing + goal manifest (§7's
  pipeline builder output).
- **Overrides** — per-section phase toggles, casts, model/effort routing.

The architectural insight that makes this cheap: the Conductor is already
pull-based — the Documentation gap report IS its work queue. So "apply
Situation XYZ to this build and the phases readjust automatically" is not a
new engine: the Situation's doc flow becomes the goal manifest's required
slots, the gap report only demands those slots, and only the sections/phases
that own them run. Phase filtering reuses the existing completeness-profile
machinery (`completeness.py`, `definition_of_done.json` tiers) — a Situation
is a completeness profile generalized to doc slots + pipeline + casts.
Changing a project's Situation mid-run: running phases finish, the gap
report recomputes, queued routes re-evaluate against the new goal.

Ships with seeded defaults (data, editable): **Full Production App** (all 40
slots), **Prototype Sprint** (PRD, screens, build, QA smoke), **Research
Spike** (research brief + decision log), **Launch Push** (legal, marketing,
launch-readiness slots), **V2 Iteration** (delta docs + V2 handoff),
**Compliance Pass** (legal/privacy slots + red-team).

### 14.2 The document builder

A flow-style canvas for editing a Situation's document flow: the 40 slots
rendered as draggable chips grouped by the 11 categories; add/remove/reorder;
each chip shows its owning section (from the doc-slot ownership manifest)
and its fill status when applied to a live project. A live impact preview
answers "what will this cost": *"this Situation will run Research, Planning,
Build, QA — ~23 phases"* — computed from slot→owner→phase mappings. The
builder writes the Situation's doc-flow half; `doc_map.json` (slot→section
mapping) stays a separate, rarely-edited file.

### 14.3 Subagent (cast) controls

- **Per-phase roster size and composition** — how many debaters, which
  personas, which model/effort each — extending the existing
  RoutingGridView editing surface.
- **Agent Library** — custom subagents as data: a persona (name, preamble)
  bound to a backend + model + default effort. Saved at fleet or section
  scope; usable in any cast, including cross-section (`research:investigator`
  style references).
- **Recommended defaults, surfaced inline** (data, not hardcode): ideation
  debates → 3 diverse personas + coordinator; adversarial verification →
  2 skeptics; mechanical/format phases → 1 fast agent; synthesis → 1 strong
  agent at high effort. Shown as hints next to the controls, never silently
  enforced.

### 14.4 The command layer

Composer **slash commands**, distinct from the ⌘K palette (⌘K = app
navigation/verbs; `/` = things done to *this chat*). Commands are data
(`commands.json`, fleet → section → project layers) so custom commands are
user-addable. Kinds: `builtin` (wired verbs), `template` (expand a prompt
template), `delegation` (route to a section), `meta` (run a single cheap
advisory turn and render a card — NEVER auto-run the user's text).

**Meta commands (the two requested):**

- `/model-effort <prompt>` — does not run the prompt. One cheap turn (local
  model preferred) against a rubric distilled from the engineering plan's
  §11 guidance + the live capability/cost descriptors; renders an advisor
  card: recommended model + effort + one-line rationale + est. cost, with a
  "Run with this" button.
- `/gen-prompt <rough text>` — turns rough text into a structured prompt
  (goal, context, constraints, output format, acceptance criteria) rendered
  as an editable preview card; insert-on-confirm, never auto-send.

**Base command set (seeded defaults):**

| Command | Kind | Does |
|---|---|---|
| `/audit` | delegation | red-team the current chat/artifact via QA section (quick or deep tier) |
| `/research <topic>` | delegation | alias of @Research quick-take/deep-dive |
| `/summarize` | template | compress the chat into a summary artifact |
| `/decision <text>` | template | record a decision-log artifact (feeds slot 37) |
| `/promote <type>` | builtin | promote last output to an artifact |
| `/send <section>` | builtin | route the selected artifact |
| `/compare` | builtin | fan the next prompt to N selected models side-by-side in one comparison card |
| `/vote` | builtin | force the debate to a weighted vote now |
| `/consensus` | builtin | ask the coordinator to attempt consensus now |
| `/cast add\|remove <agent>` | builtin | edit the room's roster mid-chat |
| `/mode auto\|manual` | builtin | "Let them discuss" / "Step in" aliases |
| `/fork` | builtin | fork this chat (agent-thread ids stripped) |
| `/model-effort <prompt>` | meta | advisor card (above) |
| `/gen-prompt <text>` | meta | prompt-structurer card (above) |
| `/snippet <name>` | template | insert a saved phrase |
| `/status` · `/cost` · `/help` | builtin | run status · this chat's cost meter · command list |

**Snippets (saved phrases):** named canned prompts with placeholders,
layered fleet → section → project, autocompleted in the composer, riding
the existing PromptSnippet GUI component. Seeded default set: `audit-this`,
`poke-holes`, `simplify`, `devils-advocate`, `whats-missing`,
`summarize-decisions`, `make-it-concrete`, `ship-check`.

### 14.5 New UI surfaces this adds

Situation Library + Situation editor (doc-flow canvas half + pipeline half +
overrides tab + "Apply to project…" impact diff), Document Builder canvas,
Agent Library, Snippet Library, composer autocomplete popover, the
model-effort advisor card, and the gen-prompt preview card. Design prompts
for all of these are in `orchestrator-v3-claude-design-prompts.md` Prompt 5.
Engineering tasks are epic M9 in `orchestrator-v3-engineering-plan.md`.
