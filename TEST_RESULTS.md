# TEST_RESULTS.md

> **Historical snapshot (2026-07-05).** Kept as a dated record; counts and
> paths reflect that day, not the current tree. The live gate is CI plus
> `python3 -W error::ResourceWarning -m unittest discover -s tests` (870+
> tests as of 2026-07-15).


Every command below was actually run on this Mac on **2026-07-05**, from
`/Users/pchordia/Documents/core_apps/ios-app-factory`. Results are stated
verbatim, not summarized optimistically.

| Command | Result (2026-07-05) |
|---------|---------------------|
| `python3 -m unittest discover -s tests` | **PASS — covered by the strict run below** |
| `python3 -W error::ResourceWarning -m unittest discover -s tests` | **PASS — `Ran 191 tests / OK`** (clean with ResourceWarning promoted to error — no leaked file handles) |
| `cd ./gui && swift build -c release` | **PASS — `Build complete!`** (zero warnings/errors) |
| `cd ./gui && swift test` | **PASS — `Executed 37 tests, with 0 failures`** (OrchestratorGUITests / EngineLogicTests) |
| `python3 orchestrator.py --doctor` | PASS — root resolves to `~/Documents/iOS-App-Factory` (exists=True); CLIs found: codex, claude, gemini, agy, ollama, git, python3; 9 workflows listed; no leaked API-key env vars |
| `python3 orchestrator.py --doctor --json` | PASS — valid JSON; `build_capable: true`; includes the `local_models` block (server_running, selected, registry with installed flags) |

The single canonical gate is **`make verify`** at the repo root
(= `test-strict` + `gui-build` + `gui-test` + `doctor`). This repo has **no git
remote and no hosted CI** — the Makefile run locally is the gate.

Notes from the doctor run: the Ollama *binary* is present but its server was
not running at probe time, and no registry model is pulled — expected on a
machine that hasn't opted into local models; this does not fail the doctor.

## Historical live-run evidence (earlier sessions, kept for the record)

- **HelloCounter** (`vslice` workflow): full pipeline through the real
  Codex/Claude/Gemini CLIs produced a compiling one-screen SwiftUI counter —
  verified by `xcodebuild` in-engine **and** by an independent
  `xcodebuild … clean build` (`** BUILD SUCCEEDED **`). 16 agent calls total.
  The generated project is still in `workspace/hello-counter/app_build/`.
- **TipJar** (degradation path): engine exited 0 with zero tracebacks while the
  agents struggled that session; `verify_results.json` recorded an honest
  `unverified`. Evidence: `evidence/integration_run_tipjar.json`,
  `workspace/tip-jar/`.

These live runs predate this remediation pass; the pass's engine/GUI changes
are covered by the 191 + 37 unit tests above. No new token-spending live run
was performed on 2026-07-05.
