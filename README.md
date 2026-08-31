# Autonomous Multi-Agent Orchestrator (V2 engine)

[![CI](https://github.com/pri8771/orchestrator/actions/workflows/ci.yml/badge.svg)](https://github.com/pri8771/orchestrator/actions/workflows/ci.yml)

A local, **no-extra-API-cost** engine that makes the AI CLIs you're **already
logged into** — **Codex**, **Claude** (Claude Code), and **Gemini/Antigravity**,
optionally joined by a **local Ollama model** — debate to consensus and then
*act*, driven through **pluggable workflows**.

The repository also hosts the fail-closed **Wait, How Big?** social publishing
experiment. Its verified account state, security boundaries, known blockers,
and continuation prompt are documented in
[`wait-how-big-social/README.md`](wait-how-big-social/README.md).

It started as an app-builder. It's now a general multi-agent reasoning engine:
the same debate → consensus → forced-vote → (optional) build machinery can build
an app, answer a question, research a topic, audit a codebase, or productionize
a prototype — you pick the workflow.

- **Engine:** this repository (stdlib Python at the repo root).
- **Workspace (projects):** `~/Documents/iOS-App-Factory/` by default. Every
  direct child folder is one project. Override per run with `--root PATH` or the
  `ORCH_ROOT` env var (or `root:` in `config.yaml`).
- **Platform:** the engine runs on POSIX (macOS/Linux; process-group control and
  the file locks are Unix APIs). The **GUI**, the **iOS build/verify**
  (`xcodebuild`, simulator, code-signing), and `install_launch_agent.sh` are
  **macOS-only**; on Linux use the CLI and schedule `run.sh` with cron/systemd.
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
# GUI (recommended, macOS): build the SwiftUI app from source and launch it.
# Defaults the workspace to ~/Documents/iOS-App-Factory (override with ORCH_ROOT).
bash gui/run_gui.sh

# CLI: from the repo root. The default root is ~/Documents/iOS-App-Factory.
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
`--root PATH`, `--resume SLUG`, `--continue-with WORKFLOW` (staged
continuation: research now, build later), `--doctor` (+ `--json`),
`--mistakes` (+ `--app`/`--json`), `--postmortem` (requires `--app`,
+ `--json`), `--search-models QUERY`, `--seed`, `--fleet-report` (label
queue + phase scorecards + anti-pattern ledger), `--eval-report [SLUGS]`,
`--save-exemplar SLUG`, `--distill-doc PATH_OR_URL --domain ios`.

**`--resume <slug>`** restarts an *existing* project from its saved
`agent_state.json`. Unlike `--app`, it refuses to run when the project or its
state file doesn't exist, exits cleanly if the project is already complete, and
clears a recorded abort error and any `blocked_conflict` marker so the pipeline
re-enters instead of skipping.

### Tests and the verification gate

```bash
# From the repo root:
python3 -m unittest discover -s tests                            # engine suite
python3 -W error::ResourceWarning -m unittest discover -s tests  # strict: warnings fail

# Or via the Makefile — the canonical gate. `make verify` is the full macOS
# gate; CI (.github/workflows) runs the engine subset on Linux.
make test          # engine unittest suite
make verify        # = test-strict + gui-build + gui-test + doctor (macOS)
make app           # package gui/dist/Orchestrator.app (macOS)
make dmg           # package gui/dist/Orchestrator.dmg (macOS)
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
~/Documents/iOS-App-Factory/<app_name>/initial_prompt/initial_prompt.md
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
cheatsheets in `knowledge/<domain>/` (ios / backend / web / general) against
the phase text and splices the top few into the agents' context.

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

14 workflows ship (14 `workflows/*.json` files). Phase counts and the exact
phase keys are authoritative in the JSON; the summaries below are a guide.

| Name | Target | Phases | What it's for |
|---|---|---|---|
| `app_build` | app | 21 | The full production pipeline (prompt_contract → product_research → portfolio_selection → initial_discussion → per_app_product_brief → next_steps_small → detailed_discussion → app_features → design_discussion → design_handoff → ios_architecture_review → tech_specs → project_plan → task_assignments → implementation_readiness_gate → **build_coordination** → build_verification → human_qa_checklist → app_store_readiness → final_review → portfolio_audit) |
| `full_max` | app | 21 | Same phases as `app_build`, discussion phases run at maximum effort |
| `prototype` | app | 19 | `app_build` trimmed for a working prototype (no human-QA / app-store phases) |
| `app_build_child` | app | 18 | `app_build` minus parent-scoped phases, for portfolio children |
| `app_spec` | app_spec | 18 | Per-app specification pipeline for portfolio child projects (no build) |
| `sprint` | app | 5 | Time-boxed build with a hard wall-clock ceiling |
| `brainstorm` | app | 3 | Fast idea-shaping: prompt_contract → product_research → convergence |
| `vslice` | app | 3 | Minimal end-to-end build to smoke-test the pipeline |
| `iterate` | app | 3 | Add a feature / fix a bug on an existing app without rebuilding |
| `audit` | audit | 5 | Read-only prioritized findings over a pre-existing codebase |
| `productionize` | productionize | 6 | Turn a working prototype into something deployable (backend/infra) |
| `research` | research | 3 | Gather → weigh evidence → report |
| `answer_question` | answer | 2 | Point the agents at a question instead of an app |
| `library_mining` | library_mining | 3 | Read-only: find shared/reusable patterns across several repos |

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
  `deepseek-r1:14b`, `deepseek-r1:32b`, `qwen3:14b`, `glm4:9b`, `mistral:7b`).
  Every id is a real `ollama pull <id>` target; pull from the CLI or the GUI's
  Settings → Local Models pane.
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
orchestrator/             # repo root (engine at top level)
├── orchestrator.py        # main engine (stdlib only): CLI, phases, build, resume
├── workflows.py           # pluggable workflow model + loader/seeder
├── workflows/*.json       # editable workflow definitions (14 built-ins)
├── Makefile               # canonical gate: test / test-strict / verify / app / dmg
├── phase_rules.py / phase_rules.json   # editable per-phase quality playbooks
├── modelrouting.py / model_routing.json  # per-phase model routing + cloud→local fallback
├── localmodels.py / local_models.json    # Ollama detection, curated registry, HF search
├── roles.py / roles.json  # roles + rotating-personality assignment
├── knowledge.py           # keyword retrieval / RAG injection
├── knowledge/{ios,backend,web,general}/*.md
├── verify.py              # compile/boot-the-build verification loop
├── docs.py                # deterministic per-project docs renderer (docs/)
├── schemas.py             # structured-block parsing + secret redaction
├── completeness.py        # completeness profiles + stop-target selection
├── resilience.py          # per-agent circuit breaker
├── global_resource.py     # machine-wide worker cap (opt-in)
├── procutil.py            # hardened subprocess helpers
├── config.yaml            # root, models, agents, rounds, runtime, knowledge, ios
├── run.sh                 # unset API keys → run with --root $ORCH_ROOT (default ~/Documents/iOS-App-Factory) → git steps
├── install_launch_agent.sh
├── seed_demo.py / simulate_stream.py    # demo data / fake live transcript
├── events.py / mistakes.py / postmortem.py  # events.jsonl + mistakes.jsonl writers, --postmortem report
├── AUDIT_HISTORY.md       # consolidated index of the five audit rounds (TASKS*.md)
├── MISTAKES.md            # live failure-mode taxonomy + mistakes-ledger docs
├── tests/                 # unittest suite (700+ tests)
├── gui/                   # native SwiftUI front-end (SwiftPM, zero deps) — see gui/README.md
└── logs/                  # event log + per-call JSON records (gitignored)
```

Per-app run locks live in `<workspace>/.orch-locks/`.

The doctor (`python3 orchestrator.py --doctor`) is the fastest way to see what's
wired up: CLIs found, resolved models, local-model status, workflows, roles,
personalities, and knowledge domains.


## Quality gates (the "done" bar) and fleet learning

A build workflow is only marked done after passing, in order (all
best-effort — a missing tool skips a gate, never blocks a run):

1. **Release gate** — the app compiles for the iOS Simulator (verify.py).
2. **Design lint** (`designlint.py`, zero tokens) — no inline colors/font
   sizes outside DesignSystem.swift; banned packages (tech_stack.json) are
   errors. Findings: `docs/design_lint.json`.
3. **Visual QA** (`visualqa.py`, local vision panel) — light+dark screenshots
   graded by every installed vision model; unanimous BAD fails.
4. **UI crawl** (`uicrawl.py` + `uitest-runner/`) — taps every element, checks
   back-navigation, screenshots every screen, runs an iOS 17 accessibility
   audit, and replays the spec's declared flows (flows.json). Crashes learn
   back into flows.json as permanent regression flows.
5. **Adherence gate** — one strong agent grades the build against the
   numbered requirements (requirements.json) and the active Definition of
   Done tier (definition_of_done.json). Verdict: `docs/adherence.json`.

Failures route into the bounded iterate-repair loop with the exact artifacts
(screenshot paths, lint findings, failing flow step) in the repair prompt.

Every gate failure also records a blamed **incident** (fleetlearn.py) against
the upstream phase whose output permitted it. `--fleet-report` prints the
label-confirmation queue (rate projects 👍/👎 in the GUI) and per-phase
scorecards, and rebuilds `knowledge/anti_patterns.md` — which the knowledge
splicer injects into future runs. `--save-exemplar <slug>` turns a rated-good
project's phase outputs into few-shot exemplars. Builds seed from
`scaffold/ios_app/` (golden scaffold) and slice tasks.json into dependency
waves (`runtime.build_vertical_slices`).
