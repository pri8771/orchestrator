# Orchestrator GUI

A native macOS SwiftUI app for driving the autonomous multi-agent
orchestrator — the "Native Pro" shell (see `DESIGN-NATIVE-PRO.md`): one
window, `NavigationSplitView` sidebar (projects) + content (phases, live
transcript, routing grid, run health) + a trailing `.inspector` (⌥⌘I), plus
a toolbar and a ⌘K command palette for the primary actions.

It watches the `agent_state.json`, `verify_results.json`, `live_log.jsonl` and
per-phase Markdown that `orchestrator.py` writes, polling every 1.5s so the
conversation appears in near real time. The only files it writes into a project
are the approval decision files (below).

## Run it

```bash
# Recommended: from the repo root. Builds release + launches, pointed at the
# workspace via ORCH_ROOT (default ~/Documents/iOS-App-Factory) and at this
# engine via ORCH_DIR.
bash gui/run_gui.sh

# Build / test this package directly:
cd gui
swift build -c release
swift test                    # GUI unit tests (Tests/OrchestratorGUITests)

# Package a double-clickable app (bundles the Python engine inside):
bash build_app.sh             # -> dist/Orchestrator.app
bash make_dmg.sh              # -> dist/Orchestrator.dmg  (or: make app / make dmg at repo root)
```

Requires the Swift toolchain (Xcode or Command Line Tools). No external Swift
packages — SwiftUI/AppKit/Combine only.

Note: `gui/run_gui.sh` exports `ORCH_ROOT` (default
`~/Documents/iOS-App-Factory`); set that env var to point the app and the CLI at
the same project folders.

## How the app finds the engine

Resolution precedence (`OrchestratorStore` + `EngineDirResolver`, unit-tested):

1. **`ORCH_DIR` env var** — set by `run-orchestrator.sh` for from-source runs.
2. **Bundled engine** — a packaged `Orchestrator.app` carries the Python engine
   in its Resources; on first launch it is copied to
   `~/Library/Application Support/Orchestrator/engine` (it must be writable for
   logs/config) and re-copied when the bundle's `VERSION` stamp changes.
3. **Repo discovery** — running from source with no bundle, the first ancestor
   directory of the executable containing `orchestrator.py` is used (this
   matches the `gui/.build/<triple>/<config>/` layout).
4. **Nothing found** — the app shows a clear "Engine not found" banner and
   refuses to launch runs. There is **no hardcoded machine-specific fallback
   path**.

The workspace root is resolved as: `ORCH_ROOT` env var → the folder picked in
Settings → General (persisted) → `~/Documents/iOS-App-Factory` as a default.

## What it shows

- **Projects** (left): every `<root>/<app>/` with an
  `initial_prompt/initial_prompt.md`, with status — not started / in progress /
  awaiting approval / stopped / done / aborted — plus a
  **VERIFIED / FAILED / UNVERIFIED** capsule from the latest
  `verify_results.json` record (symbol + text, never color alone).
- **Phases** (middle): the selected project's workflow phases, each marked
  done / active / pending.
- **Transcript** (right): the selected phase rendered as a chat — one bubble
  per agent turn, persona chips (e.g. `Product Strategist · Skeptic`), round
  dividers, a thinking indicator, and the phase's final output +
  consensus/vote marker. Above it, as applicable:
  - **Verification card** — latest verify record with status, summary, repair
    attempt count, and expandable compiler-error output.
  - **Error banner** — when a run aborted, the recorded error with a Copy
    button.
  - **Blocked-conflict banner** — when a worktree merge conflict paused the
    build: the lane, the conflicting files, and the detail; resolve manually,
    then resume.
  - **Approval bar** — when the engine paused at a semi-autonomous/manual
    checkpoint.

## Actions

- **New chat** (+): picks a workflow and sets per-project **Autonomy**
  (fully autonomous / semi-autonomous / manual), **Completeness** (prototype /
  MVP / V1 / production draft), and **Stop at** (idea validated … production
  ready) — written to `run_config.json`; the prompt goes to
  `initial_prompt/initial_prompt.md`.
- **Run**: launches `python3 orchestrator.py --root <workspace> --app <name>`
  with the pay-as-you-go API-key env vars stripped, streaming output into the
  Log sheet.
- **Stop**: sends SIGTERM (the engine releases its locks), escalates to SIGKILL
  after ~5s, then defensively clears the per-app lock. Only runs launched from
  **this GUI session** can be stopped — process handles don't survive an app
  relaunch, and runs started from a terminal can't be signalled from here.
- **Approvals**: **Approve** / **Edit & Approve…** / **Request Changes…**
  write `approvals/<phase>.ok`, `.edit` (body replaces the phase output), or
  `.changes` (body is feedback; the phase re-runs) — the decision-file contract
  the engine polls.
- **Demo stream**: runs `simulate_stream.py --root <workspace>` to append a
  fake conversation so you can watch the live transcript without agent CLIs.
- The bottom text box folds a human message into the live conversation
  (`human_inbox.txt`).

## Settings (Cmd+,)

- **General** — default autonomy/completeness for new projects; workspace
  folder picker.
- **Agents** — enable/disable each agent, model names, reasoning effort
  (saved to the engine's `config.yaml`).
- **Local Models** — Ollama detection (binary + server), guided install, the
  `agents.ollama_enabled` toggle, a model picker fed from the engine's
  `--doctor --json` `local_models` block (installed vs needs-pull, with a Pull
  button restricted to curated registry ids), and the §12.4 privacy-boundary
  text.
- **Resources** — the opt-in machine-wide global worker cap.

Also: menu bar commands, macOS notifications (run finished / needs approval /
failed), a Usage sheet (agent call counts per project — calls, not tokens),
and per-project reset/fork/build-history.

## Tests

`Tests/OrchestratorGUITests/EngineLogicTests.swift` — XCTest coverage of the
pure GUI↔engine bridge logic in `EngineLogic.swift`: `verify_results.json`
parsing, the approval decision-file contract, `blocked_conflict` parsing, the
doctor `local_models` block, shipped workflow coverage, and the engine-dir
resolution precedence.

```bash
cd gui && swift test   # from the repo root
```

## Seeding demo data

```bash
python3 seed_demo.py   # from the repo root; writes demo projects into the workspace
```
