# Orchestrator V3 "Sections" — Engineering Plan

Status: VERIFIED engineering plan (rev 2) derived from
`orchestrator-v3-sections-plan.md`. Rev 2 incorporates 23 findings from an
adversarial 3-lens verification pass against the repo (grounding,
sequencing, feasibility) — every task below survived or was corrected by
that pass. The task tables are AUTHORITATIVE; §11's model/effort summary is
generated from them.

Companion docs: `orchestrator-v3-sections-plan.md` (what/why),
`orchestrator-v3-claude-design-prompts.md` (design),
`orchestrator-v3-task-board.md` (operator-ready task cards, generated from
this plan), `orchestrator-v3-feature-radar.md` (researched feature tiers —
tasks marked "(radar adopt)" below came from its ADOPT NOW tier; V3.1/V4
waves live there).

## 0. Ground rules (apply to every task)

1. **Delivery rigor:** adversarial design review of the task → implement ONE
   task → full gate (`make verify` on macOS; engine subset on CI) green
   before the next task. No batching of risky changes.
2. **Branching:** work on `dev`, PR to `main` per milestone or per coherent
   task group. Never commit secrets; `run.sh` secret rules stay in force.
3. **Every engine task ships with tests.** The canonical gate is
   `make verify` (= test-strict + gui-build + gui-test + doctor). New
   transcript-touching code additionally runs the golden-file suite (T2.1).
4. **Every fallback added or touched must surface a visible banner** — the
   fail-open batch-factory culture is a bug in an interactive product.
5. **Quality rulebook applies to our own GUI** (QUALITY_RULES.md "target
   2"): R2 interface-never-lies, R4 explicit state models, §16 no fake
   controls, §23 verify-don't-self-report. Task cards cite specific rules.
6. **Sizing:** S = one focused session, M = 1–3 sessions, L = a week-scale
   workstream split into sub-PRs before starting.
7. **Model/effort notation** is `model @ effort` for the Claude Code session
   doing the task (rationale in §11).

---

## M0 — Vocabulary + layout freeze  (total: S)

| # | Task | Files | Gate | Size | Model |
|---|------|-------|------|------|-------|
| 0.1 | Write `GLOSSARY.md`: Project > Section > Chat(session dir) > Run; "agent thread" for backend continuity keys; Artifact; Conductor; pipeline vs workflow | GLOSSARY.md | reviewed once, then referenced in PR template | S | sonnet @ medium |
| 0.2 | ✅ DONE — see GLOSSARY.md "Layout (M1 interim)". Layout decision doc: target layout `workspace/<project>/<section>/<chat-slug>/` (+ `…/artifacts/`, `…/conductor/`) is IMPLEMENTED in 3.0, not before. Until then, M1 chats live as FLAT legacy project dirs named `<project>--<section>--<chat-slug>` (engine discovery `find_apps` is flat and ignores nested dirs — orchestrator.py:6400–6421 — and GUI discovery + `.orch-locks/<name>.lock` naming are flat too). Documents the flat naming convention (component-wise slugify then join with literal `--`), the lock-name collision rule, and the back-compat rule: a dir with `agent_state.json` at its root is a legacy single-chat project forever | GLOSSARY.md, this plan | design review | S | fable @ high |

M0 exit: both docs merged; every later PR uses the vocabulary.

## M1 — Chat spine (user-visible first; built on the UN-refactored engine)

Goal: Ideas + Research usable daily as manual chats and watchable auto
debates, before any deep refactor. M1 chats are flat-layout per 0.2.

| # | Task | Files | Gate | Size | Model |
|---|------|-------|------|------|-------|
| 1.1 | `conversational` phase flag in workflow schema + `process_phase`: rounds become drain `human_inbox.txt` → roster responds → wait for next human message (generalize `_await_approval`'s poll into an inbox-wait helper); skip coordinator/consensus; phase ends on user command or promote | orchestrator.py (process_phase ~5270, drain_human_inbox ~2592, _await_approval ~6342), workflows.py schema | unit tests: conversational phase with scripted inbox; existing workflows unchanged (full suite) | M | fable @ xhigh |
| 1.2 | Event-driven inbox wake: replace fixed 2s polls with mtime check at ~250ms tick for conversational + approval waits | orchestrator.py | latency test with simulated writer; no busy-loop CPU regression | S | fable @ high |
| 1.3 | Chat workflow seeds: `workflows/chat_ideas.json`, `workflows/chat_research.json` — single conversational phase, casts from existing roles.json personas | workflows/*.json | seeds load via wflib.load_workflow and appear in list_workflows; manual CLI smoke | S | sonnet @ medium |
| 1.4 | GUI: per-chat history — re-key **OrchestratorStore**'s saveChatHistory/loadChatHistory (OrchestratorStore.swift:2951/:2962; `chatMessages` didSet at :629 → Application Support chat_history.json) from one global history to per-chat keys | gui OrchestratorStore.swift | history isolation test: two chats, no bleed | S | fable @ high |
| 1.5 | GUI: chat-session lifecycle in the store — mint a flat chat dir (0.2 naming), launch + supervise a conversational engine run (Process spawn like existing run paths), stop/crash/relaunch states surfaced honestly (R2/R4) | gui OrchestratorStore.swift | lifecycle test incl. engine crash → visible error state, not silent | M | fable @ xhigh |
| 1.6 | GUI: chat rendering via transcript tail + events (reuse TranscriptParser/TranscriptView, today keyed to project runs) replacing ChatHome's one-shot `conciergeAsk` claude call (ChatHome.swift:211–229) | gui ChatHome.swift, TranscriptView.swift | simulate_stream.py renders a conversational session; gui-test | M | fable @ high |
| 1.7 | "Step in": composer under a live auto debate; send = human_inbox write + checkpoint marker → debate pauses at next round barrier; "joining at round N" countdown | gui TranscriptView.swift + engine checkpoint path | scripted run via simulate_stream.py; StyleGuard | M | fable @ high |
| 1.8 | "Let them discuss": promote a manual chat to an auto phase seeded with the chat transcript (workflow-phase transition + state edit) | orchestrator.py transition helper + gui action | integration test: manual→auto→consensus on a stub | M | fable @ xhigh |
| 1.9 | Fork verb: copy session dir minus locks AND minus `call_agent_sessioned` agent-thread ids (safety rule: codex resume is only sandbox-safe in write phases — a cloned thread id is the unsafe version) | orchestrator.py or sessions helper, gui action | test asserts no agent-thread id survives the copy | S | fable @ high |
| 1.10 | Focused-pane poll cadence 1.5s→500ms — cadence parameter ONLY; the store's refresh watchdog/generation/coalescing machinery (OrchestratorStore.swift:823–897) is not restructured here (mtime-cached focused-scan work belongs to 8.2) | gui OrchestratorStore.swift | CPU/energy sanity on a 3-session run; no watchdog regression | S | fable @ high |
| 1.11 | (radar adopt) Per-message producer attribution + mid-chat model swap: each message records its producing runner id (2.8's traces need it anyway); chat-header model chip swaps model mid-conversation with history preserved; "retry with…" re-runs the last turn | orchestrator.py, gui | attribution round-trip; swap + retry tests | S | fable @ high |

M1 exit gate: scripted end-to-end demo — new Ideas chat, converse, "let
them discuss", watch 3 agents debate live, "step in", reach consensus —
on the current engine, all existing workflows still green.

## M2 — Engine seams (under the shipped chat product)

| # | Task | Files | Gate | Size | Model |
|---|------|-------|------|------|-------|
| 2.1 | Golden-file suite FIRST: freeze current transcript bytes for representative runs (debate, vote, PASS, resume mid-round, conversational) as fixtures; assert byte equality | tests/test_transcript_golden.py + fixtures | suite passes on unmodified engine | M | fable @ high |
| 2.2 | Extract the generic debate core from `process_phase` (~800 lines). Sub-PRs: (a) extract round loop, (b) extract consensus+vote, (c) extract build hooks behind verify/contract seams, (d) **re-base the conversational rounds + inbox-wait (1.1/1.2) onto the extracted core** — gate for (d): conversational golden fixture byte-green + 1.1's scripted-inbox tests pass against the new core | orchestrator.py | golden suite + full suite byte-green after EACH sub-PR | L → 4×M | fable @ max |
| 2.3 | `TurnContext` replacing the mutated cfg-dict keys: real surface is **~54 unique `cfg["_…"]` keys across ~96 assignment sites (~146 refs), including reads in urlfetch.py (3) and visualqa.py (1)**. Sub-PR (a) is a generated key inventory (writer/reader map per key) before any code moves | orchestrator.py (call_agent ~1193, _call_agent_once ~1337, runners 627–1010), urlfetch.py, visualqa.py | full suite; grep gate: no new `cfg["_` writes | L → 3×M | fable @ max |
| 2.4 | `messages.jsonl` dual-write: every turn appends the markdown block (unchanged bytes) AND a JSONL line {turn_id, agent, role, persona, round, ts, content_path} | orchestrator.py append path | golden suite + JSONL schema test + crash-resume test (kill mid-round, resume, no dupes) | M | fable @ xhigh |
| 2.5 | events.KINDS extension: message_appended, artifact_published, artifact_routed, artifact_consumed, route_proposed, route_approved | events.py, tests | KINDS-contract tests; 3500B cap respected (ids+paths only) | S | sonnet @ high |
| 2.6 | SQLite FTS5 indexer (stdlib): incremental index of messages.jsonl + artifact publishes; `--reindex`; GUI ⌘K search hook (jump-to-turn) | new search.py, gui palette | index round-trip tests; <50ms on 10k messages | M | fable @ high |
| 2.7 | Golden-path UI test suite for the CURRENT OrchestratorStore (none exists today — gui/Tests are logic tests; uitest-runner targets built apps, not this GUI). Covers launch/stop, refresh/watchdog, chat lifecycle. De-risks 1.4–1.6 retro-actively and is the hard prerequisite of 8.1 | gui/Tests | suite green on current store | M | fable @ high |
| 2.8 | (radar adopt) LLM trace schema: every runner call writes traces/<run>/<seq>.json {rendered_prompt, response, model, tokens, parent_call}; GC'd by 8.6 policy; schema ships NOW so autonomy is debuggable from day one — the drill-down UI is V3.1 | orchestrator.py call sites | trace-write tests; kill mid-call leaves a valid partial trace | S | fable @ high |

M2 exit gate: all existing workflows AND the current GUI run
byte-identically (golden suite is the proof), with messages.jsonl + new
events accumulating silently underneath.

## M3 — Section manifests + the nested layout (SECTION = DATA)

| # | Task | Files | Gate | Size | Model |
|---|------|-------|------|------|-------|
| 3.0 | **Implement the nested layout** decided in 0.2: session-minting helper creates `workspace/<project>/<section>/<chat-slug>/`; `find_apps` (orchestrator.py:6400–6410) learns nested discovery while still honoring legacy flat dirs; state/lock paths + lock NAMES become collision-free for nested dirs; GUI discovery (BackgroundProjectLoader.discoverApps, OrchestratorStore.swift:866) + `.orch-locks` naming (:3312) updated; one-shot migration of M1 flat chats into the nest | orchestrator.py, gui OrchestratorStore.swift | nested chat runs end-to-end; legacy flat projects still discovered; lock-collision test; migration dry-run diff | L → 2×M | fable @ max |
| 3.1 | `sections/<name>/` loader: section.json (identity, workflow, default mode, artifact types, DoD tier), seed-then-disk-wins via ensure_seeded; **visible-fallback banner event** on any parse failure | new sections.py, workflows.py | loader tests incl. corrupt-file banner | M | fable @ high |
| 3.2 | Section-scoped rule lookup: phase_rules.py order section → project-override → global; global file untouched | phase_rules.py | collision test: two sections, same phase key, different rules | S | fable @ high |
| 3.3 | Externalize `_phase_contract` to contracts.json ({phase_key, fence_tag, required_fields, prompt_snippet}) per section; extraction stays schemas.extract_structured_blocks | orchestrator.py, schemas.py, sections/*/contracts.json | contract round-trip tests; existing fences unchanged | M | fable @ xhigh |
| 3.4 | Per-section roles + routing files with modelrouting.py's overlay merge reused for the three layers (fleet → project → session) | sections.py, roles.py, modelrouting.py | merge-precedence tests | M | fable @ high |
| 3.5 | Cross-section cast references: `"roles": ["visionary", "research:investigator"]` resolves via the other section's roles.json; missing-ref banner | roles.py | resolution + banner tests | S | fable @ high |
| 3.6 | Seed sections from existing workflows: Ideas←brainstorm, Research←research, QA←audit, Planning←app_spec | sections/*/ | each runs end-to-end from JSON alone | M | sonnet @ high |
| 3.7 | `--new-section` scaffolder + `--lint-section` validator, CLI-only. **All scaffold/lint logic lives in sections.py** (3.1 already holds the schema knowledge); orchestrator.py gets only a thin argparse hook. GUI surfacing of lint warnings belongs to 3.8 | sections.py, sections/_template/, thin CLI hook | scaffold→lint→run round-trip test | M | sonnet @ high |
| 3.8 | GUI: section rail + section Settings sheet re-skinning existing editors (WorkflowBuilderSheet → Phases; RoutingGridView → Models/Routing; PromptSnippet → Rules); surfaces 3.7's lint warnings in the section gallery | gui | gui-test + StyleGuard | L → 2×M | fable @ high |
| 3.9 | Convert verify.py's type dispatch (hardcoded if/elif on spec["type"], verify.py ~:747–772) into a dict registry — the second plugin seam the spec promises (RUNNERS at orchestrator.py:1000 is the only registry today) | verify.py | dispatch-parity tests for all existing types | S | fable @ high |

M3 exit gate: create a brand-new "Naming" section via scaffolder, edit five
JSON files, run it end-to-end in a NESTED chat dir — zero core-code changes.

## M4 — Artifact bus (manual routing first)

| # | Task | Files | Gate | Size | Model |
|---|------|-------|------|------|-------|
| 4.1 | Artifact store: `workspace/<project>/artifacts/<id>/` body.md + meta.json; type registry as JSON (required-fields validation); atomic publish | new artifacts.py | store unit tests, concurrent-publish test | M | fable @ xhigh |
| 4.2 | Publication: engine extracts fenced `artifact-json` from Final Outputs, materializes, emits artifact_published | orchestrator.py, artifacts.py, schemas.py | end-to-end publish from a stub debate | M | fable @ high |
| 4.3 | Lineage + versioning + **bus-level loop guards**: supersedes chains, content_hash, **lineage depth + hop count recorded in meta.json with publish-time cap enforcement** (identical-descendant auto-marked "converged"); per-lineage advisory flock; concurrent derivation → named branches (v3-a/v3-b) + mandatory `reconcile` artifact; "latest final" resolver REFUSES on unreconciled branches | artifacts.py | two-writer collision test (branch+reconcile, no silent pick); depth-cap + identical-descendant fixtures | M | fable @ max |
| 4.4 | **Session mint + spawn helper** (the conductor-less delegation machinery): factor portfolio-style dir minting (portfolio.py:373 mints flat, batch-driven — not reusable as-is) into a session-level helper that mints a nested chat dir, launches the engine run as a subprocess, and on publish honors `reply_to` by writing the artifact card reference into the originating session's human_inbox | new sessions helper, artifacts.py | mint→run→reply round-trip test, idempotent re-mint | M | fable @ max |
| 4.5 | PUSH: "Send to →" — materialize a routed artifact reference into a target session's carryover context (the carryover_outputs injection FORMAT from prepare_continue ~7843; note prepare_continue itself only rewrites existing-project state — minting/launching is 4.4's helper) | orchestrator.py, artifacts.py | routed round-trip Ideas→Research→Ideas | M | fable @ xhigh |
| 4.6 | Return edge + @-mention delegation: `@Research <question>` = 4.4 mint with reply_to; Quick-take (guest persona single turn) vs Deep-dive (sub-session) tiers | artifacts.py, gui composer | delegation round-trip test | M | fable @ xhigh |
| 4.7 | PULL: build_context() artifact-retrieval layer (knowledge.py-style scoring, top-k, char budget, provenance headers, sensitivity filter stub for 8.5) | orchestrator.py, knowledge.py | injection-budget tests | M | fable @ high |
| 4.8 | Admission control: per-type finalization policy {auto_final_on_consensus, requires_review_gate, requires_human}; only `final` artifacts route/retrieve | artifacts.py registry | policy tests | S | fable @ high |
| 4.9 | GUI: artifact cards in-stream (type glyph, version chip, lineage breadcrumb, stale badge), drag-to-route onto rail sections, "Promote to artifact" | gui | gui-test + StyleGuard + manual demo | L → 2×M | fable @ high |
| 4.10 | Legible-autonomy GUI: phase-purpose hover on bubbles (WHY) + route-preview chip on the final bubble (WHERE) resolved from section routing defaults (3.4); 7.10 later upgrades it to the Conductor's actual pending route | gui TranscriptView.swift | StyleGuard; chip resolves from routing.json fixture | S | fable @ high |
| 4.11 | ⌘K session verbs: register "new brainstorm", "send to …", "open conductor" in the EXISTING command palette (ContentView.swift:273–434 filteredCommands) — verb registration, not a new surface | gui ContentView.swift | palette dispatch test | S | sonnet @ high |
| 4.12 | (radar adopt) Pre-push gate — hooks + guardrails on ONE artifact.push call site: hooks.json matchers (workspace/section scope), subprocess contract exit 0 = allow / exit 2 = block with stderr fed back into the producing agent's context; semantic guardrail llm_rules with bounded retry-with-feedback; failures quarantine with a failed-gate badge, never silently dropped | artifacts.py gate, sections manifests | hook matrix tests incl. stderr-feedback; guardrail retry-then-quarantine test | M | fable @ xhigh |
| 4.13 | (radar adopt) Factory memory, foundation: memory/ dir of founder-pinned markdown facts with {scope: global\|project\|section, triggers[]} frontmatter; injected via the knowledge.py retrieval slot + 4.7's ambient-pull path; unifies the knowledge.py / mistakes.py / fleetlearn.py precursors' injection point (full learnings pipeline with approval inbox is V3.1) | new memory.py (or knowledge.py), minimal gui editor | injection-precedence tests; scope filtering | M | fable @ high |

M4 exit gate: full manual loop — brainstorm → promote idea → drag to
Research → deep-dive @Research from inside a brainstorm → brief card
returns (via 4.4's conductor-less path) — provenance visible on every hop.

## M5 — Documentation section

| # | Task | Files | Gate | Size | Model |
|---|------|-------|------|------|-------|
| 5.0 | Seed the full `sections/documentation/` manifest (section.json, roles.json, contracts.json, routing.json) — without it the Conductor has no Documentation section to route gap-fills to and M7's exit gate is unevaluable | sections/documentation/ | Documentation section runs end-to-end from JSON | S | sonnet @ high |
| 5.1 | Externalize docs.py source tuples (PRD_SOURCES/ARCH_SOURCES/QA_SOURCES, docs.py:451–456) to doc_map.json; 40-section/11-category standard as default map; N/A-never-fabricate preserved | docs.py, sections/documentation/doc_map.json | render parity vs current output on sample-run | M | fable @ high |
| 5.2 | **Artifact→slot ingestion (SUBSCRIBE mode)**: docs.py render pulls `final` artifacts from artifacts.py via doc_map slot mappings — the mechanism the gap-report work queue depends on | docs.py, artifacts.py | fixture: a doc renders from artifacts ALONE | M | fable @ high |
| 5.3 | Doc-slot ownership manifest: one owning section per blueprint slot; cross-lineage conflicts become `gap` artifacts, never last-write-wins | docs.py, artifacts.py | conflict test | M | fable @ xhigh |
| 5.4 | Gap report generation: empty/thin slots → structured gap artifacts routed (manually for now) to owning sections | docs.py | gap-report fixture test | S | fable @ high |
| 5.5 | GitHub sync contract: render into a per-project git worktree; bot-identity commits at milestones; pre-render diff detects human edits → slot flips "human-overridden", edit preserved, reconcile artifact routed back | new docsync.py | human-edit-preservation test (the load-bearing one) | M | fable @ max |
| 5.6 | Notion exporter: explicit step with dry-run diff over render_project_management_backfill payload; delivery is a separately-authorized step, never fire-and-forget | docsync.py | dry-run diff test | S | fable @ high |

M5 exit gate: Documentation section runs end-to-end from JSON; a sample
project's artifacts assemble into docs/PROJECT_DOCUMENTATION.md with an
honest gap list; a hand-edit to the rendered doc survives the next render
and produces a reconcile artifact.

## M6 — API backends + streaming + cost accounting (BEFORE the Conductor)

| # | Task | Files | Gate | Size | Model |
|---|------|-------|------|------|-------|
| 6.1 | Capability descriptors per agent id {streams, token_usage, effort_control, session_resume}; UI reads them (no promised feature a backend lacks) | orchestrator.py registry, gui | descriptor tests | S | fable @ high |
| 6.2 | `api:` runners (run_anthropic_api / run_openai_api / run_google_api) via urllib. Keys: stored ONLY in `~/.orchestrator/<provider>_api_key` files (the gemini FILE-STORAGE precedent, orchestrator.py:577–592); for api: runners the key goes ONLY into the HTTP auth header — never argv, never child env. (The existing run_gemini env injection at :783/:831 is exempt: the CLI requires it and stays as-is.) Per-project opt-in with cost warning; startup key probe with cached result | orchestrator.py runners | mocked-HTTP tests; secret-leak grep gate scoped to the api: runner paths (no key in argv/env/logs) | L → 2×M | fable @ max |
| 6.3 | Streaming lifecycle: API deltas → `<session>/.stream/<turn_id>.ndjson`; file DELETED on turn completion; transcript block authoritative; GUI tails .stream for focused panes; CLI agents keep the honest "thinking" shimmer | orchestrator.py, gui | invariant test: kill mid-stream → resume sees no partial turn; golden suite green | M | fable @ max |
| 6.4 | Token/cost accounting: per-turn usage from API responses (and CLI where reported) → per-session/section/project meters, persisted, EffortGauge surfaces | new costs.py, gui | accounting tests with fixture payloads | M | fable @ high |
| 6.5 | (radar adopt) Schema-constrained decoding for local models: compile contracts.json (3.3) to JSON Schema passed as `format`/response_format on the Ollama runner; validate on write; repair-reprompt fallback recorded per capability descriptor — what makes routing bus-writing steps to free local models responsible | orchestrator.py run_local, schemas.py | constrained-output fixture tests; malformed-then-repair test | S | fable @ high |

M6 exit gate: a mixed-cast debate (CLI + api: + local) with live token
streaming in the focused pane and a correct cost meter; run.sh env
stripping still verified for CLI runners.

## M7 — Conductor (global autonomous mode)

| # | Task | Files | Gate | Size | Model |
|---|------|-------|------|------|-------|
| 7.0 | Stop coverage for runs the GUI didn't launch: the GUI's stopRun ALREADY signals the engine pid named in the lock file with SIGTERM→SIGKILL (OrchestratorStore.swift:3307–3342) — audit it, extend lock naming/stop coverage to NESTED session dirs (3.0) and Conductor-minted runs, engine writes `<session>/run.pid` as belt-and-braces | orchestrator.py, gui OrchestratorStore.swift | kill-after-GUI-relaunch test incl. nested + conductor-launched runs | S | fable @ high |
| 7.1 | conductor.py skeleton: one per workspace (flock), wake on events growth + authoritative state poll, conductor_state.json + routing ledger JSONL, resumable | conductor.py | crash-resume test (kill at each loop stage) | M | fable @ max |
| 7.2 | Routing rules engine: routing.json (fleet+project overlay); route-to-one (deterministic type→section, then one cheap local-model classifier turn), broadcast, custom chains; **pre-route loop guards: descendant content-hash check + per-chain hop budget (reading 4.3's meta fields) BEFORE any route fires**; minting via 4.4's helper | conductor.py, sessions helper | rule-eval tests; TOCTOU mint test; oscillating-chain fixture is NOT routed | L → 2×M | fable @ max |
| 7.3 | Route idempotency: deterministic route_id = hash(artifact, target, rule) written to ledger BEFORE acting; inbox injections carry route_id marker; restart dedups | conductor.py, drain path | crash-between-append-and-record test | M | fable @ max |
| 7.4 | Capability/permission model: per-section manifest {writes, exec, external}; engine-enforced; routes into >workspace-only capability always require approval; external effects queue as pending actions | sections.py, conductor.py, orchestrator.py | enforcement tests: capability exceeded → blocked + surfaced | M | fable @ max |
| 7.5 | Goal manifest + termination stack: gap-report goal predicate (5.2/5.4 + DoD + evalharness threshold), quiescence (N idle cycles → converged-with-open-items), budgets (turns/wall-clock/per-provider spend via costs.py + pacing state), stall detection (vote_undecided ×2, supersedes oscillation) | conductor.py | termination unit tests incl. oscillation fixture | L → 2×M | fable @ max |
| 7.6 | Oversight dials: full-auto / suggest-only / gated / loops-gated (default) via approvals file contract; per-decision undo (do-not-route, kill spawned session) | conductor.py, gui | dial matrix tests | M | fable @ xhigh |
| 7.7 | Workspace snapshots: workspace as git repo; Conductor commits at quiescence points and before routing waves, tagged with ledger cursor; "roll back the night" = reset + ledger replay | conductor.py, docsync.py | rollback integration test | M | fable @ xhigh |
| 7.8 | Notifications: macOS UserNotifications on approval-needed / stalled / converged / budget-exhausted; quiet-hours batching | gui events consumer | batching unit tests + manual | S | sonnet @ high |
| 7.9 | Routing eval suite (the full-auto shipping gate): replay recorded artifact streams; assert routing, termination, crash-kill at each route-state step | tests/conductor_evals/ | suite green = full-auto may default on | M | fable @ max |
| 7.10 | GUI Mission Control: node map (sections/chips/motes), decision ledger, approval tray, budget meters, replay timeline scrub; upgrades 4.10's route-preview chip to show actual pending routes | gui | StyleGuard + scripted replay demo | L → 2×M | fable @ high |
| 7.11 | Pipeline builder: canvas (nodes=sections, edges=routes with conditions) compiling to routing.json + goal manifest; save/load named presets (RunProfile pattern); "Run pipeline" verb | gui + conductor.py preset loader | compile round-trip: canvas→JSON→canvas | M | fable @ xhigh |
| 7.12 | (radar adopt) Plan gate + activity trace: 'plan' becomes a first-class artifact type gated by the oversight dials BEFORE autonomous execution — editable in place before approval; runners append activity.jsonl per step; output artifacts cite the plan step that produced them (stale badges cover intent) | conductor.py, artifacts.py, gui | plan-gate dial matrix; edited-plan-is-what-executes test | M | fable @ xhigh |
| 7.13 | (radar adopt) Failure artifacts: terminal failures (circuit-breaker stop, budget exhaustion, crash) emit a typed `failure` artifact {error class, last checkpoint ref, cost spent} routed via routing presets to notification or a Fixer persona — diagnosable, routable, never a bare "run failed" | orchestrator.py, conductor.py | failure-emit test per error class; routed round-trip | S | fable @ high |

M7 exit gate: seed one idea at 11pm in loops-gated mode; by morning it is
researched, planned, and documented (5.0's Documentation section closes the
loop); Mission Control replays the night; every external side-effect sat in
the pending queue; `git reset` cleanly unwinds it.

## M8 — Full shell, remaining sections, hardening

| # | Task | Files | Gate | Size | Model |
|---|------|-------|------|------|-------|
| 8.1 | Store split: OrchestratorStore (fleet) + per-session SessionModel derived objects — incremental, never a rewrite; hard prerequisite: 2.7's UI suite green | gui | UI tests green before/after each extraction | L → 3×M | fable @ max |
| 8.2 | Pane canvas: 1 pane default, drag-in split (2–3 max), overflow strip; relaxed background polling + mtime-cached focused-scan (deferred from 1.10) | gui | StyleGuard + energy check | M | fable @ high |
| 8.3 | Seed remaining sections: Design, Go-to-Market, Legal, Execution, Library | sections/*/ | each end-to-end | M | sonnet @ high |
| 8.4 | De-iOS-ify: global_app_rules / tech_stack / xcodebuild DoD items into the Build section manifest, conditional on target | sections/build/, config | non-Build sections carry zero iOS assumptions (grep gate) | M | fable @ high |
| 8.5 | Privacy tiering: sensitivity field (project/artifact), enforced at resolve_runner + build_context (4.7's stub); sensitive fallback chains terminate at local; "Private (local models only)" toggle | orchestrator.py, artifacts.py, gui | enforcement test: local-only content never reaches a cloud runner (mock) | M | fable @ max |
| 8.6 | Lifecycle/GC: tombstone superseded artifacts >N versions, exclude killed lineages from pull/search, `--gc` dry-run, "archive project" verb | artifacts.py | GC dry-run diff test | M | fable @ high |
| 8.7 | Onboarding: backend probe checklist, progressive disclosure (Ideas+Research first), 5-minute guided first-brainstorm | gui | manual walkthrough + StyleGuard | M | fable @ high |
| 8.8 | Config migration: one-shot migrator with dry-run diff for tuned workflows/*.json, phase_rules.json, model_routing.json → section manifests (workspace DIR migration already happened in 3.0) | migrate_v3.py | migrator tests on copies of real configs | M | fable @ xhigh |
| 8.9 | (radar adopt) Chat pins/tags/archive: frontmatter keys on chat files (no new storage); sidebar sort/filter; tags feed the FTS5 index; archived chats excluded from default scans and pull retrieval | gui, search.py | frontmatter round-trip; filter + FTS tag tests | S | sonnet @ high |
| 8.10 | (radar adopt) Local-model JIT lifecycle, basic: keep_alive per request, LRU evict on load, runtime/loaded_models.json, per-section pins — 11 sections must share one Mac's unified memory overnight without beachballing (memory strip + pressure-eviction UI is V3.1) | localmodels.py, orchestrator.py run_local | evict-order tests; multi-model overnight simulation | M | fable @ high |

M8 exit gate: the one-app vision — 11 sections, concurrent panes,
Conductor, onboarding — with `make verify` + StyleGuard + golden + UI +
conductor-eval suites all green.

## M9 — Situations, document builder, command layer  (spec §14)

Internal sequencing: the command layer (9.5–9.8) only needs M1–M4 and may
start any time after M4; Situations (9.0–9.3) need M5 (doc slots/gap
reports) and M7 (goal manifest, pipeline builder).

| # | Task | Files | Gate | Size | Model |
|---|------|-------|------|------|-------|
| 9.0 | Situation manifest + seeds: `situations/<name>/situation.json` {doc_slots[], pipeline_ref, section/phase/cast overrides}; seed the six defaults (Full Production, Prototype Sprint, Research Spike, Launch Push, V2 Iteration, Compliance Pass) mapped onto existing completeness tiers (completeness.py, definition_of_done.json) | new situations.py (or sections.py), situations/*/ | loader + lint tests; each seed validates | M | fable @ high |
| 9.1 | Apply-situation resolution: project's situation ref → goal-manifest required slots; phase filtering (a phase runs iff its doc_sections intersect the required slots OR it is structurally required — reuse completeness-profile machinery); mid-run situation change → running phases finish, gap report recomputes, queued routes re-evaluate | orchestrator.py, conductor.py, completeness.py | phase-filter matrix test; live situation-switch test | M | fable @ xhigh |
| 9.2 | Document Builder UI: flow canvas over the 40 slots grouped by 11 categories; add/remove/reorder chips; owning-section + fill-status on each chip; live impact preview ("will run Research, Planning, Build, QA — ~N phases") computed from slot→owner→phase maps | gui | canvas→JSON→canvas round-trip; StyleGuard | L → 2×M | fable @ high |
| 9.3 | Situation editor + apply flow: doc-flow half (9.2 canvas) + pipeline half (reuse 7.11 builder) + overrides tab; "Apply to project…" shows an impact DIFF (phases added/removed, sections activated) before confirming | gui, conductor.py preset loader | apply-diff correctness test | M | fable @ high |
| 9.4 | Agent Library + roster controls: custom subagent = persona {name, preamble} bound to backend+model+default-effort, saved fleet- or section-scope; per-phase roster size control; recommended-count defaults as data (ideation 3+coordinator, verification 2 skeptics, mechanical 1 fast, synthesis 1 strong@high) shown as inline hints, never enforced; (radar adopt) sampling-parameter presets as files (presets/*.json) attachable per persona, preset id stamped into artifact lineage | roles.py, sections manifests, presets/*, gui | resolution tests incl. cross-section refs; hints render; preset pass-through + lineage stamp | M | fable @ high |
| 9.5 | Command registry (data): `commands.json` fleet → section → project layers; {name, kind: builtin\|template\|delegation\|meta, template, target}; composer parses `/cmd`; meta commands execute ONE cheap call_agent turn and render a card — user text is NEVER auto-run; unknown command → visible banner | new commands.py, gui composer | registry + layering tests; unknown-command banner test | M | fable @ high |
| 9.6 | `/model-effort` advisor + `/gen-prompt` structurer: advisor rubric = §11 guidance + live capability/cost descriptors (6.1/6.4) as data; advisor card (model, effort, rationale, est. cost, "Run with this"); gen-prompt card (goal/context/constraints/output-format/acceptance, editable, insert-on-confirm) | commands.py, gui cards | rubric fixture tests; guarantee test: neither command ever executes the input prompt | M | fable @ high |
| 9.7 | Snippet library: `snippets.json` fleet → section → project; seed defaults (audit-this, poke-holes, simplify, devils-advocate, whats-missing, summarize-decisions, make-it-concrete, ship-check); composer autocomplete; reuse the existing PromptSnippet component; (radar adopt) snippets/templates may declare typed `{{variables}}` (type, options, required — miniyaml-subset or JSON frontmatter, no PyYAML) rendered as a small form before insert | gui, snippets seeds | layering precedence + autocomplete tests; variable-form render + required-field test | S | sonnet @ high |
| 9.8 | Base builtin commands + autocomplete popover: /mode /vote /consensus /cast /fork /promote /send /audit /research /decision /summarize /compare /status /cost /help wired to existing verbs; `/compare` fans one prompt to N selected models side-by-side in a comparison card | gui, commands.py | per-command dispatch tests; StyleGuard | L → 2×M | fable @ high |

M9 exit gate: create a "Prototype Sprint" situation in the builder, apply
it to a project, watch the impact diff and the Conductor run only the
scoped phases; in any chat, `/model-effort` and `/gen-prompt` return cards
without running the input, `/audit` round-trips through QA, and snippets
autocomplete.

---

## 9. Test strategy summary

- **Golden transcript suite (2.1)** — the compatibility keel; every engine
  PR from M2 onward.
- **Golden-path UI suite (2.7)** — the GUI keel; prerequisite of 8.1.
- **simulate_stream.py** — live-rendering harness for every chat surface.
- **Conductor eval suite (7.9)** — replay-based; gates full-auto default.
- **Crash-kill matrix** — every stateful component (dual-write, publish,
  route, stream) gets a kill-at-each-step test.
- **Grep gates in CI** — no new `cfg["_` writes (2.3), no key in
  argv/env/logs on api: runner paths (6.2), no iOS strings outside Build
  (8.4), StyleGuard for GUI.

## 10. Sequencing constraints (the hard ones)

1. 2.1 (golden suite) strictly before any 2.x refactor; 2.2(d) re-bases the
   conversational path onto the extracted core.
2. 0.2's flat-chat convention governs all of M1; 3.0 implements the nested
   layout BEFORE 3.8's section rail and before any of M4.
3. 4.4 (mint+spawn helper) before 4.5/4.6; it is also 7.2's minting
   machinery.
4. 4.3's depth/hop meta fields before 7.2's pre-route guards read them.
5. 5.0 + 5.2 before M7's exit gate is evaluable (the Conductor needs a
   runnable Documentation section and artifact-fed gap reports).
6. M6 cost accounting strictly before 7.5 budgets (real spend, not chars).
7. 7.0 (stop coverage) before 7.2 autonomous minting.
8. 7.4 permissions + 7.9 eval suite before full-auto is ever the default.
9. M1 ships before M2 starts; 2.6 (FTS5) lands with 2.4 (dual-write).
10. 2.7 (UI suite) before 8.1 (store split).
11. M9 split: 9.5–9.8 (command layer) may start after M4; 9.0–9.3
    (Situations) require 5.2/5.4 (slots + gap reports) and 7.5/7.11
    (goal manifest, pipeline builder); 9.6 requires 6.1/6.4 descriptors.

## 11. Model & effort guidance (Claude Code)

The task tables above are authoritative; this section summarizes them.
Default: **Fable 5 (`claude-fable-5`) @ high** for engine and GUI work.

- **fable @ max** (correctness-catastrophic): 2.2, 2.3, 3.0, 4.3, 4.4,
  5.5, 6.2, 6.3, 7.1, 7.2, 7.3, 7.4, 7.5, 7.9, 8.1, 8.5.
- **fable @ xhigh** (cross-cutting engine changes with good test cover):
  1.1, 1.5, 1.8, 2.4, 3.3, 4.1, 4.5, 4.6, 4.12, 5.3, 7.6, 7.7, 7.11,
  7.12, 8.8, 9.1.
- **fable @ high** (everything else engine/GUI): 0.2, 1.2, 1.4, 1.6, 1.7,
  1.9, 1.10, 1.11, 2.1, 2.6, 2.7, 2.8, 3.1, 3.2, 3.4, 3.5, 3.8, 3.9, 4.2,
  4.7, 4.8, 4.9, 4.10, 4.13, 5.1, 5.2, 5.4, 5.6, 6.1, 6.4, 6.5, 7.0,
  7.10, 7.13, 8.2, 8.4, 8.6, 8.7, 8.10, 9.0, 9.2, 9.3, 9.4, 9.5, 9.6, 9.8.
- **sonnet @ medium–high** (config seeds, JSON plumbing, verb/notification
  registration, section seeding): 0.1, 1.3, 2.5, 3.6, 3.7, 4.11, 5.0, 7.8,
  8.3, 8.9, 9.7.
- Use **plan mode** before 2.2, 2.3, 3.0, 7.x, 8.1; `/code-review high`
  after each milestone; `/verify` before merging anything with a runtime
  surface.
- Haiku 4.5: not recommended (the single 8k-line engine file punishes
  shallow context).

## 12. Parked ideas (deliberately out of scope now)

- **Jira/Notion board integration in the app** (import/export the task
  board, sync section tasks to a Notion database). Parked by decision
  2026-07-17. The operator task board (`orchestrator-v3-task-board.md` +
  `.json`) is authored import-ready so this becomes a script, not a
  rewrite, when picked up.
