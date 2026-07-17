# Orchestrator V3 — Feature Radar

Produced 2026-07-17 from a six-angle web-research sweep (multi-model chat
workspaces, agent orchestration studios, agentic coding products, AI
knowledge/PKM tools, local-LLM apps, research tools), deduplicated against
the V3 plan, then adversarially filtered by two judges (product value for a
solo app-factory founder; architectural fit with the stdlib/file-based/
no-server design). 47 candidates survived the merge; verdicts below are the
reconciled tiers. Where the judges disagreed, the reconciliation is noted.

Tiers: **ADOPT NOW** = folded into the V3 engineering plan as tasks (see
plan tables; board cards authored). **V3.1** = first fast-follow wave after
M9. **V4** = horizon. **KILLED** = rejected, with reasons kept so the idea
doesn't respawn.

---

## ADOPT NOW — folded into the V3 plan

1. **Factory memory, foundation slice** (ChatGPT Memory, Devin Knowledge,
   CrewAI memory) — a `memory/` directory of founder-pinned markdown facts
   ("SwiftUI only, RevenueCat for IAP") with scope + trigger keywords,
   injected via the existing knowledge.py/ambient-pull path. The repo
   already has three precursors (knowledge.py, mistakes.py, fleetlearn.py)
   to unify. The full learnings pipeline (auto-extraction, per-section
   lesson distillation, approval inbox) is V3.1 — but the injection point
   must exist from the start. → task 4.13.
2. **Pre-push gate: deterministic lifecycle hooks + semantic guardrails**
   (Claude Code hooks; CrewAI guardrails) — ONE gate interface at
   artifact.push: user shell scripts (exit 0 allow / exit 2 block with
   stderr fed back to the agent) AND llm-rule guardrails with
   retry-with-feedback; failures quarantine with a failed-gate badge.
   Load-bearing for overnight trust: one sloppy Planning artifact otherwise
   silently poisons Build, QA, and Docs. → task 4.12.
3. **Editable plan gate + activity trace** (Devin/Cursor plan modes) —
   'plan' becomes a first-class artifact type gated by the oversight dials
   BEFORE autonomous execution; runners append activity.jsonl per step.
   Both judges: the V3 plan's biggest remaining safety gap — 90 seconds
   editing 12 bullets beats an evening reviewing a 900-line wrong diff.
   → task 7.12.
4. **Failure artifacts (designated error handler)** — terminal failures
   emit a typed `failure` artifact (error class, last checkpoint ref, cost
   spent) routed like any artifact; turns circuit-breaker stops into
   diagnosable, routable events. → task 7.13.
5. **LLM trace schema** (LangSmith/AgentOps observability) — every runner
   call writes {rendered_prompt, response, model, tokens, parent_call} to
   traces/. Schema NOW even though the drill-down UI is V3.1: retrofitting
   call-level logging after autonomy ships means the first overnight
   failures are undebuggable. → task 2.8.
6. **Per-message producer attribution + mid-chat model swap** (every
   serious chat workspace) — each message records its producing runner;
   a model chip in the chat header swaps mid-conversation with history
   preserved; "retry with…" re-runs the last turn. The manual complement
   to fallback ladders and the daily quota-stretching loop ("cheap local
   until it stalls, then one expensive cloud turn"). → task 1.11.
7. **Chat pins, tags, archive** (all chat workspaces) — frontmatter keys on
   chat files, sidebar sorting, feeds FTS5 and GC. Cheapest fit on the
   list. → task 8.9.
8. **Local-model JIT lifecycle, basic slice** (LM Studio JIT/TTL) —
   keep_alive per request, LRU evict, runtime/loaded_models.json. Required
   for V3's own promise: 11 sections sharing one Mac's unified memory
   overnight without beachballing. Memory strip + pressure-based eviction
   UI is V3.1. → task 8.10.
9. **Sampling-parameter presets as files** (LM Studio .preset.json) —
   attachable per persona; makes debate casts behaviorally distinct
   (temperature 1.1 Visionary vs 0.3 Verifier), preset id stamped into
   lineage. → folded into 9.4 (Agent Library).
10. **Typed {{variables}} on snippets/templates** (Open WebUI prompts,
    TypingMind library) — snippet/command templates declare variables
    (type, options, required) in miniyaml-subset frontmatter; picking one
    shows a small form, then injects the rendered prompt. → folded into
    9.7.
11. **Schema-constrained decoding for local models** (Ollama structured
    outputs) — compile the (already-data) contracts.json to JSON Schema
    passed as response_format on local runners; validation + repair-
    reprompt recorded per capability descriptor. What makes routing
    bus-writing steps to free local models responsible at all. → folded
    into 3.3/6.1 acceptance criteria (new subtask on 3.3).

## V3.1 — the first fast-follow wave (priority order)

1. **Web grounding layer** — search-provider adapter (Brave/Tavily/
   Perplexity key; SearXNG for privacy-tier) + fetcher + every page
   persisted as a content-addressed `source` artifact. THE prerequisite
   for the research half of the product; without it Research debates its
   training data. Judge note: cost overstated — `urlfetch.py` already
   implements the robots-respecting fetcher, extraction, caching, and the
   "UNVERIFIED — say so" degradation; what remains is the search adapter,
   source artifacts, and capability gating.
2. **Source library + claim-level citations + citation linter**
   (Perplexity, NotebookLM, Elicit) — frozen dated hash-addressed
   snapshots; `[^s:HASH:range]` citation spans resolved against snapshots;
   a deterministic linter verifies every range resolves before push. Ship
   WITH the web layer — uncited research in the interim trains bad habits.
   Plus **source-policy profiles** (domain/type/recency scoping, S).
3. **Checkpoint time travel** (Devin, Cursor checkpoints) — shadow git
   commit before each workspace-touching turn keyed to message ids;
   autonomous runs checkpoint per step; "re-run from here" forks a run
   whose outputs enter the bus as a branch. The single biggest token/time
   saver for overnight runs that fail at step 12 of 30.
4. **Scheduled + event-triggered runs** (ChatGPT tasks, cron agents) —
   triggers/*.json + the existing LaunchAgent headless path wakes the
   engine, enqueues a Conductor goal; versioned bus computes diff-only
   "what changed" delivery into a suggestions inbox. Review-sentiment
   sweeps, ASO checks, dependency audits — the periodic work a solo
   founder drops.
5. **Factory memory, full pipeline** — post-chat extraction pass, close-out
   lesson distillation from the decision ledger + QA findings, approval
   inbox riding the oversight dials.
6. **Founder Daily Note** (PKM daily notes) — a morning journal file
   pre-filled from the decision ledger, budgets, stale badges; "promote" a
   jotted line into an Ideas seed or Conductor goal. Highest daily touch
   per unit of work. Plus **spaced resurfacing of parked ideas** riding it.
7. **Canvas-style artifact editor** (ChatGPT canvas, Claude artifacts) —
   selection-anchored edit requests + difflib word-level version diff.
   The plan has versioning but zero editing UX; App Store copy and launch
   posts get polished weekly.
8. **Global-hotkey capture window** (ChatGPT Option+Space, BoltAI) —
   menu-bar panel grabs selection/clipboard from any app into the Ideas
   inbox as typed capture artifacts.
9. **Trace inspector UI** — collapsible call-tree drill-down over the 2.8
   trace files, from artifact lineage back through parent_call ids.
10. **Model bake-off** (Msty split chats, LangSmith comparisons) — one
    prompt fanned to 2–4 runner configs side-by-side with cost/latency
    readouts; "Keep" reorders that section's fallback ladder and logs to
    bakeoffs/history.jsonl feeding capability descriptors. (`/compare` in
    M9 is the embryo; this adds the write-back.)
11. **MCP client, stdio-only v1** — JSON-RPC over subprocess pipes (pure
    stdlib), mcp-registry.json, tools allowlisted per section via the
    capability model, calls in the decision ledger. Remote HTTP/OAuth
    servers explicitly deferred.
12. **Warm workspace snapshots** (Devin machine snapshots) — per-app
    environment manifest: simulator preboot, DerivedData/SPM cache
    restore, startup commands, before Build/QA runners start. Protects
    overnight wall-clock in an iOS factory.
13. **Editable context-length dial + overflow feedback**; **hardware-fit
    badges + quantization picker** (closer to S than estimated: sysctl →
    hardware_profile.json + static memory arithmetic in the download
    sheet); **JIT memory strip UI**; **golden-example regression sets**
    (evals/golden/ is the precursor) once real outputs accumulate;
    **'Ask the Factory' v1** (FTS5-only, mandatory artifact@version
    footnotes).

## V4 — horizon (revisit when the trigger fires)

- **Knowledge Stacks** (local embedding RAG over external corpora — App
  Review Guidelines, HIG, the founder's vault). Trigger: source library
  seasoned; share ONE embedding sidecar (Ollama HTTP embeddings, SQLite
  BLOBs, FTS-prefilter + pure-Python cosine re-rank — no numpy, no
  sqlite-vec) with "Related thinking". Bridge until then: an FTS-subscribed
  docs folder.
- **Semantic cross-project recall**; **entity layer** (registry +
  unlinked-mention lint first, query views second); **AI autofill columns
  on the Library**; **structured extraction tables** (the competitor-grid
  payoff feature); **criteria-based source screening** — all scale
  features: they pay at 5–10 apps or after months of accumulation.
- **Playbooks** (Devin) — largely superseded by M9 Situations for the
  workflow half; what remains for v4 is "save this finished project as a
  parameterized template" (manifest-set zip with {{params}}).
- **Best-of-N parallel attempts** in worktrees (anti-economic under
  today's quotas; needs mature meters + compare UI).
- **Confidence probes / question-first escalation** (calibration unproven;
  revisit with real run data).
- **Phone-side inbox via iCloud Drive** (clever no-server design but
  fragile sync plumbing; Mac loop first).
- **Managed inference runtimes** (MLX/llama.cpp packs — a package manager;
  only when the Ollama ceiling measurably hurts). Unblocks speculative
  decoding if ever needed.
- **Per-app codebase wiki** (DeepWiki) — parked: factory apps are small;
  CLI agents re-map them in minutes. Revisit only if app complexity grows.
- **Audio briefings** — parked; the Daily Note delivers the oversight in
  text at a tenth the cost. The footnoted-report exporter half can revive
  as a small renderer (textutil for .docx — never python-docx).

## KILLED (kept so they don't respawn)

- **Voice dictation** — macOS system dictation already types into any
  composer; a bespoke whisper path duplicates the OS for marginal gain.
- **Localhost OpenAI-compatible server** — genuinely bends no-server into
  an always-on daemon with auth; MCP + hooks cover integration in the
  direction that matters; Ollama already serves shared local models.
- **Sense-making whiteboards** — an L-cost infinite-canvas editor for a
  monthly ritual; artifacts are plain files, so Freeform/Obsidian Canvas
  already work.
- **Evidence-stance meter** — pseudo-rigor over web sludge; the debate
  forced-vote already renders a stance panel.
- **Speculative decoding** — a config flag on a runtime (managed MLX/
  llama.cpp) that doesn't exist; not a feature. Resurrect as a descriptor
  field if managed runtimes ever land.

## Standing implementation constraints (from the fit judge, apply to ALL)

- No PyYAML — frontmatter stays within the vendored miniyaml subset or
  uses JSON. No numpy — vector math is pure Python over SQLite BLOBs with
  FTS prefilter. No python-docx — shell to `textutil`. Mermaid → WKWebView
  (system framework), never a JS dep in the engine.
- Every capability-expanding feature (web, MCP, hooks) routes through the
  per-section capability/permission model and the budget meters — no side
  doors.
