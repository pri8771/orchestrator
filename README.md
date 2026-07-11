# Autonomous Multi-Agent Orchestrator (V2 engine)

A local, **no-extra-API-cost** engine that makes the AI CLIs you're **already
logged into** — **Codex**, **Claude** (Claude Code), and **Gemini/Antigravity**,
optionally joined by a **local Ollama model** — debate to consensus and then
*act*, driven through **pluggable workflows**.

It started as an app-builder. It's now a general multi-agent reasoning engine:
the same debate → consensus → forced-vote → (optional) build machinery can build
an app, answer a question, research a topic, audit a codebase, or productionize
a prototype — you pick the workflow.

- **Engine:** `<repo>/orchestrator-v2-source/` (this directory).
- **Workspace (projects):** `/Users/pchordia/Documents/iOS-App-Factory/`.
  Every direct child folder is one project. Override per run with `--root PATH`
  or the `ORCH_ROOT` env var.
- **Dependencies:** Python 3 standard library only. No API keys — `run.sh` and
  the GUI strip `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` / `GEMINI_API_KEY` /
  `GOOGLE_API_KEY` (and related vars) before launching, so every call counts
  only against your normal subscriptions. One exception: the gemini CLI can run
  headless with a key read from `~/.orchestrator/gemini_api_key` (kept outside
  the repo; secret-shaped filenames are gitignored and `run.sh` refuses to
  commit them).

---

## How to run

```bash
# GUI (recommended): from the repo root — builds the SwiftUI app and points it
# at /Users/pchordia/Documents/iOS-App-Factory.
bash run-orchestrator.sh

# CLI: from this directory. The default root is already
# /Users/pchordia/Documents/iOS-App-Factory.
cd orchestrator-v2-source
python3 orchestrator.py --doctor            # environment preflight
python3 orchestrator.py --doctor --json     # machine-readable preflight (§27)
python3 orchestrator.py --app myapp         # process one project
python3 orchestrator.py --resume myapp      # resume a paused/aborted project
python3 orchestrator.py --watch 300         # loop every 300s
python3 orchestrator.py --root /abs/path --app myapp   # explicit workspace

# Wrapper that also unsets API-key env vars and (if the workspace is a git repo)
# commits the results — deriving all paths from its own location:
bash run.sh --doctor
bash run.sh --app myapp
```

Full CLI surface (see `python3 orchestrator.py --help`): `--once` (default),
`--watch SECONDS`, `--app NAME`, `--project SLUG` (alias of `--app`),
`--root PATH`, `--resume SLUG`, `--doctor` (+ `--json`), `--seed`.

**`--resume <slug>`** restarts an *existing* project from its saved
`agent_state.json`. Unlike `--app`, it refuses to run when the project or its
state file doesn't exist, exits cleanly if the project is already complete, and
clears a recorded abort error and any `blocked_conflict` marker so the pipeline
re-enters instead of skipping.

### Tests and the verification gate

```bash
cd orchestrator-v2-source
python3 -m unittest discover -s tests                          # engine suite (191 tests)
python3 -W error::ResourceWarning -m unittest discover -s tests  # strict: warnings fail

# Or from the repo root — the canonical gate (this repo has no git remote and
# therefore no hosted CI; the Makefile is the gate):
make verify        # = test-strict + gui-build + gui-test + doctor
make app           # package gui/dist/Orchestrator.app
make dmg           # package gui/dist/Orchestrator.dmg
```

### Scheduled runs

`install_launch_agent.sh` installs a macOS LaunchAgent that runs `run.sh --once`
on an interval (default 30 min). Like `run.sh`, it derives the engine and
workspace paths from its own location — no absolute install path is baked in.

```bash
bash install_launch_agent.sh              # install + load
INTERVAL=900 bash install_launch_agent.sh # custom interval (seconds)
bash install_launch_agent.sh uninstall
```

---

## How it works

Each project starts as a single file:

```
/Users/pchordia/Documents/iOS-App-Factory/<app_name>/initial_prompt/initial_prompt.md
```

When the orchestrator sees a **new or changed** `initial_prompt.md` (tracked by
a SHA-256 hash), it resolves which **workflow** to run and drives the project
through that workflow's phases, in order. Within each phase the **orchestrator**
(not the agents) controls turn order: each enabled agent speaks, then the
coordinator decides.

**Personas.** At the start of every phase each active agent is handed a
`(role, personality)` pair. Roles bias the discussion toward the disciplines
that matter for that phase; personalities rotate so the same agent argues from
a different temperament each phase. The pairing is deterministic (a resumed run
reproduces it).

**Knowledge.** For phases that benefit, the orchestrator scores the curated
cheatsheets in `knowledge/<domain>/` (ios / backend / web) against the phase
text and splices the top few into the agents' context.

**Consensus / vote.** Each phase runs up to a configurable number of rounds. If
the coordinator declares `CONSENSUS: YES`, the phase ends. Otherwise a **forced
weighted vote** runs (no agent votes for its own idea), the coordinator tallies,
and the phase ends with `VOTE_DECISION: YES`.

**Machine contracts.** Two phases must emit structured blocks alongside their
prose, parsed and persisted for the parallel build:

- `tech_specs` → a ` ```interfaces-json``` ` block → `<app>/interfaces.json`
  (the shared type/signature contract every build lane codes against).
- `task_assignments` → a ` ```tasks-json``` ` block → `<app>/tasks.json`
  (the lane-routed task backlog).

**Build + verify.** When `build_code_changes_enabled` is true, a build phase
fans the agents out to build **in parallel** (each owns a lane) with an
integrator turn between iterations. Afterward the **verification loop**
(`verify.py`) compiles the result and, on failure, runs bounded repair
iterations feeding the compiler errors back to the integrator. Every attempt is
persisted to `<app>/verify_results.json` with an honest status:
`verified` / `failed` / `unverified`. A deterministic pass then guarantees any
generated Xcode project is signable on a real device.

**Worktree isolation + conflicts.** With `runtime.worktree_isolation: true`,
each build lane works in its own git worktree and lanes are merged back after
every iteration. A real merge conflict is **never** resolved by
last-write-wins: the engine records `blocked_conflict` ({lane, files, detail})
in `agent_state.json`, pauses the run (even in fully-autonomous mode), and
**keeps the lane worktrees on disk** so you can inspect and resolve both sides
manually — then `--resume <slug>` clears the marker and re-enters the pipeline.

**Secret scan + launch gate.** After every build phase a deterministic scan of
the generated source (`app_build/`) looks for hardcoded secrets. Findings carry
only the secret *type* and `file:line` — never the value — and are merged into
`docs/findings.json`. Any open `secret_hardcoded` finding puts a
`⛔ LAUNCH BLOCKED` line at the top of `docs/LAUNCH_READINESS.md`; otherwise the
report states `Secret scan: PASS` (or "not run" when nothing was scanned).
Separately, everything any agent prints passes through a redaction chokepoint
(`schemas.redact_secrets`) before reaching any persisted artifact.

**Approvals (autonomy modes).** A project can carry a `run_config.json` with
`autonomy`: `fully_autonomous` (default), `semi_autonomous`, or `manual`. At a
checkpoint the engine pauses and polls `<app>/approvals/` for one of three
decision files, which the GUI's buttons write:

| File | GUI button | Effect |
|---|---|---|
| `<phase>.ok` | Approve | continue as-is |
| `<phase>.edit` | Edit & Approve | the file body **replaces** the phase output |
| `<phase>.changes` | Request Changes | the body is human feedback; the phase re-runs |

**Completeness + stop targets.** `run_config.json` may also set `completeness`
(`prototype` / `mvp` / `v1` / `production_draft` — a phase subset + round
multiplier) and `stop_after_phase` (friendly labels like `"docs complete"` or
`"tech spec complete"`, mapped to the real phase keys of the shipped
workflows). Both are defensive: a profile that would gut a workflow falls back
to all phases, and an unresolvable stop target is ignored with a warning —
never a silent 1–2-phase run.

### Which workflow does a project run?

Resolved in priority order:

1. `<app>/workflow.txt` — a single workflow name (what the GUI's New-chat
   picker writes).
2. A `workflow: <name>` line in the first ~15 lines of `initial_prompt.md`.
3. `runtime.default_workflow` in `config.yaml` (default `app_build`).

### Built-in workflows

| Name | Target | Phases |
|---|---|---|
| `app_build` | app | product_research → initial_discussion → next_steps_small → detailed_discussion → app_features → design_discussion → tech_specs → project_plan → task_assignments → **build_coordination** → final_review |
| `sprint` | app | initial_discussion → design_discussion → tech_specs → **build_coordination** → final_review (time-budgeted) |
| `vslice` | app | tech_specs → **build_coordination** → final_review (minimal, token-light) |
| `iterate` | app | iterate_scope → **build_coordination** → final_review (feature/fix on an existing app) |
| `answer_question` | answer | deliberation → answer |
| `research` | research | gather → analyze → report |
| `audit` | audit | recon → security → bugs → modernization → report (read-only over a target repo) |
| `productionize` | productionize | assess_prototype → backend_design → infra_and_security → integration_plan → **build_backend** → production_review |
| `library_mining` | library_mining | portfolio_recon → commonality_analysis → extraction_candidates |

Built-ins are seeded to `workflows/*.json` on first run (`--seed`) so you can
edit them; a deleted file falls back to the built-in. On-disk JSON always wins —
that's how GUI edits to rounds persist.

### Per-project state and artifacts

```
<workspace>/<app>/
├── initial_prompt/initial_prompt.md   # you write this
├── workflow.txt                       # optional workflow pick
├── run_config.json                    # optional autonomy/completeness/stop_after_phase
├── agent_state.json                   # phase/round/next-agent state, error, blocked_conflict
├── <phase_key>/agent_messages.md      # per-phase transcript + final output
├── live_log.jsonl                     # append-only event log (ts, lane, agent, kind, summary)
├── tasks.json                         # validated task backlog (from task_assignments)
├── interfaces.json                    # shared type/signature contract (from tech_specs)
├── verify_results.json                # every verification attempt, honest status
├── approvals/                         # <phase>.{ok,edit,changes} decision files
├── app_build/                         # generated code (a git repo; per-iteration commits)
└── docs/                              # deterministic, non-AI renders after each run:
    ├── PRD.md  TECHNICAL_ARCHITECTURE.md  QA_REPORT.md
    ├── KNOWN_LIMITATIONS.md  PROJECT_DOCUMENTATION.md  LAUNCH_READINESS.md
    ├── findings.json                  # secret-scan / review findings
    └── phase_outputs.json
```

Re-running resumes from the first incomplete phase; changing
`initial_prompt.md` restarts the pipeline; `--resume` clears a recorded
error/conflict first.

---

## Local models (Ollama, V2 §12)

- Curated registry: `local_models.json` next to the engine
  (`qwen3-coder:30b`, `qwen2.5-coder:7b`, `deepseek-r1:8b`,
  `deepseek-r1:14b`, `deepseek-v4-pro:latest`, `mistral:7b`). Pull one with
  `ollama pull <id>` or from the GUI's Settings -> Local Models pane.
- Registry entries include license/commercial-use metadata and are tested to
  stay permissive for commercial use.
- Enable with `models.ollama: "<id>"` **and** `agents.ollama_enabled: true` in
  `config.yaml` (or the GUI's Settings → Local Models pane).
- A local model joins as a debate/review/build worker only: it **never becomes
  the coordinator while any cloud agent is enabled**, and it sits out
  time-budgeted (sprint) workflows unless `runtime.local_models_in_sprints` is
  true.
- `--doctor` (and `--doctor --json`'s `local_models` block) reports server
  reachability, the selected model, and per-registry-model installed status.
  The server must be running (`ollama serve` or the app) for a local run.
- Privacy boundary (§12.4): Codex/Claude/Gemini calls leave this Mac through
  their CLIs; Ollama-routed calls stay on this Mac.

### Model Library: search + download open models

- `python3 orchestrator.py --search-models "qwen coder" [--json]` merges the
  curated registry with a live **Hugging Face GGUF** search; every result id is
  directly pullable (`ollama pull hf.co/<org>/<repo>`). Gated repos are dropped;
  licenses are surfaced per hit. Offline degrades to curated-only with a note.
- GUI: Settings → Local Models has the same search with one-click **in-app
  downloads** (streamed progress from the Ollama API, no Terminal window) and a
  per-model **roster** checkbox (`models.ollama_roster`) to mix-and-match local
  participants alongside the cloud agents.

## Per-phase model routing + cloud→local fallback

`model_routing.json` next to the engine (GUI: Settings → Routing):

- **Per-phase overrides** — `phases.<phase_key>` may set `claude` / `codex` /
  `gemini` / `ollama` model ids, `codex_reasoning` (low|medium|high), and an
  `agents` participant filter (agent ids, local model tags, or the groups
  `cloud` / `local`). Spend frontier models only on build/verification phases
  and run discussion phases on cheap or local models. The GUI ships **Cost
  Saver** and **Max Quality** presets. A filter that matches nobody is ignored
  (fail-open); everything is best-effort — a corrupt file can't kill a run.
- **Fallback** — `fallback.cloud_to_local: true` retries any failed cloud turn
  (timeout, rate limit, missing/logged-out CLI, empty output) on an installed
  local model (`fallback.local_model`, else `models.ollama`, else the first
  installed roster model). The rescued reply is clearly attributed in the
  transcript, and a local model still never *coordinates* while a cloud agent
  is healthy.
- `--doctor --json` exposes the active routing in its `model_routing` block.

## Verification loop

```yaml
runtime:
  verify_build_enabled: true      # compile the build + repair on failure
  verify_timeout_seconds: 1200
  verify_repair_iterations: 3     # per-phase override lives in the workflow's `verify` block
```

Each build phase declares a `verify` type in its workflow JSON:

- `xcodebuild` — compile for the **iOS Simulator with signing disabled**;
  device-signing correctness is enforced separately by the deterministic
  `fix_ios_signing` pass.
- `http` — used by `productionize`: boots the server the agents built
  (auto-detecting `npm start` / `uvicorn main:app` / Flask), polls
  `GET /health`, then tears the process down. Note: the generated server runs
  **unsandboxed** on your machine (see the repo's KNOWN_LIMITATIONS.md).
- `swift` / `shell` — `swift build` or a custom/auto-detected command.

A missing toolchain leaves the build **unverified** — recorded honestly in
`verify_results.json`, never crashing the run.

## Configuring sub-agents, rounds, knowledge

- **Roles + personalities:** edit `roles.json` (or GUI → Configure →
  Sub-agents). Missing/invalid file falls back to built-ins in `roles.py`.
- **Rounds per phase:** in the workflow JSON (`workflows/<name>.json`), by hand
  or via GUI → Configure → Rounds & phases. Build phases count **iterations**.
- **Knowledge:** drop markdown into `knowledge/<domain>/`; optional keyword
  hints (`<!-- keywords: ... -->`). `knowledge:` block in `config.yaml`
  controls domain (blank = auto), `max_chars`, `top_k`.

---

## File map

```
orchestrator-v2-source/
├── orchestrator.py        # main engine (stdlib only): CLI, phases, build, resume
├── workflows.py           # pluggable workflow model + loader/seeder
├── workflows/*.json       # editable workflow definitions (9 built-ins)
├── phase_rules.py / phase_rules.json   # editable per-phase quality playbooks
├── modelrouting.py / model_routing.json  # per-phase model routing + cloud→local fallback
├── localmodels.py / local_models.json    # Ollama detection, curated registry, HF search
├── roles.py / roles.json  # roles + rotating-personality assignment
├── knowledge.py           # keyword retrieval / RAG injection
├── knowledge/{ios,backend,web}/*.md
├── verify.py              # compile/boot-the-build verification loop
├── docs.py                # deterministic per-project docs renderer (docs/)
├── schemas.py             # structured-block parsing + secret redaction
├── completeness.py        # completeness profiles + stop-target selection
├── resilience.py          # per-agent circuit breaker
├── global_resource.py     # machine-wide worker cap (opt-in)
├── procutil.py            # hardened subprocess helpers
├── config.yaml            # root, models, agents, rounds, runtime, knowledge, ios
├── run.sh                 # unset API keys → run with --root /Users/pchordia/Documents/iOS-App-Factory → git steps
├── install_launch_agent.sh
├── seed_demo.py / simulate_stream.py    # demo data / fake live transcript
├── tests/                 # unittest suite (191 tests)
├── gui/                   # native SwiftUI front-end (SwiftPM, zero deps) — see gui/README.md
├── logs/                  # event log + per-call JSON records (gitignored)
└── locks/                 # legacy lock dir — per-app run locks now live in <workspace>/.orch-locks/
```

The doctor (`python3 orchestrator.py --doctor`) is the fastest way to see what's
wired up: CLIs found, resolved models, local-model status, workflows, roles,
personalities, and knowledge domains.
