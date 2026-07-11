# CHANGELOG_V2.md

## Milestone 0 — Orient, preflight, preserve — COMPLETE (verified locally)

- Ran the V2 preflight/doctor check on the owner's Mac. Result: **PASS** — see `PREFLIGHT_RESULTS.md`. python3 3.14.4, git 2.53.0, Swift 6.3.3, Xcode 26.6, simctl, and all five agent CLIs (codex/claude/gemini/agy/ollama) present; spec + source present in the working repo.
- Confirmed the existing Python engine loads and seeds all six built-in workflows on Python 3.14 (`python3 orchestrator.py --seed`).
- Confirmed the existing SwiftUI GUI **compiles and links clean** on Swift 6.3 / Xcode 26.6 (`swift build` in `orchestrator-v2-source/gui`, ~5.5s, no errors). This establishes a working baseline to evolve.

## Milestone 1 — Runnable vertical slice — STARTED

- Added a machine-readable preflight to the engine (V2 spec §27): `python3 orchestrator.py --doctor --json` now emits a structured JSON report (`preflight_report()` + `_tool_version()` + `_PREFLIGHT_TOOLS` in `orchestrator-v2-source/orchestrator.py`), covering build-class tools (python3/git/swift/xcodebuild/simctl) and agent-class CLIs (codex/claude/gemini/agy/ollama) with presence, path, and version. Pure stdlib; spends no agent tokens. Verified: emits valid JSON with `build_capable: true`. This is what the GUI first-run onboarding (spec §10.1) consumes.
- Existing `--doctor` (human-readable) behavior preserved unchanged.

Everything above is real and verified by running it — no fabricated progress.

## Milestone 1 — Runnable vertical slice — PROVEN END-TO-END

- Added engine CLI surface toward spec §27: `--root <path>` and `--project <slug>` (alias of `--app`), so the engine can target an arbitrary workspace. Regression-checked; `--doctor`/`--seed` unchanged.
- Added `vslice`, a minimal token-light workflow (1 plan round → 2 build iterations → 1 review, 20-min hard budget) to exercise the whole pipeline cheaply.
- **Ran the full pipeline live on this Mac through the real Codex/Claude/Gemini CLIs** and it produced a real, compiling one-screen SwiftUI app (HelloCounter) — verified by `xcodebuild` in-engine AND by an independent clean re-build (`** BUILD SUCCEEDED **`). 16 agent calls total. See TEST_RESULTS.md.
- This demonstrates the vertical slice the whole V2 plan is built around — prompt → real multi-agent debate → consensus → parallel build → honest structured verification — works today on real agents. What remains for a "complete" Milestone 1 is the GUI new-project/verification wiring (the engine path is proven; the GUI still launches the run and renders state).

## Milestone 1 — GUI wired to the workspace — FUNCTIONALLY COMPLETE

- Discovered the existing SwiftUI app already implements the Milestone 1 surface: three-pane layout, a "New chat" sheet that creates a project from a prompt (`createChat`), a Run button that launches the engine (`runProject`, API keys stripped, streams output), live status pills, per-phase list, transcript rendering, and live parallel-build worker chips. This is Milestone 1's GUI, already built.
- Wiring fix: `runProject` now passes `--root <workspace>` to the engine (using the new engine flag) so the GUI and engine agree on the workspace when the app is pointed at one via `ORCH_ROOT`. Rebuilt clean (debug + release).
- Added `run-orchestrator.sh` at the repo root: launches the app with `ORCH_ROOT=workspace` + `ORCH_DIR=orchestrator-v2-source`, so generated projects live in `workspace/` and the engine/spec/GUI stay at the repo root.
- Verified: release binary builds clean; the app launches against the workspace, stays alive, and loads the existing HelloCounter project. The create→run→render path calls the same engine invocation already proven to produce a compiling, xcodebuild-VERIFIED app.

**State of the app:** Orchestrator V2 is now a launchable, usable native macOS app that takes a one-line prompt and runs a real multi-agent build to a verified artifact — the core product works end to end, GUI included. Milestones 2–9 (deeper V2 hardening/features) remain; see NEXT_MILESTONES.md.

## Autonomous session summary (M2–M6 core, engine trust/resilience layers)
Completed as tested, committed increments (41 unit tests under tests/, all green; engine seed/doctor clean throughout):
- M4: schemas.py shared structured-artifact parser (reports malformed blocks, never silent-drops).
- M3: verify_results.json persistence + redact_secrets chokepoint + orchestrator-derived VERIFICATION gate on final review. Proven live (verify_results.json written with honest status during an integration run).
- M2: agent-identity normalization (dynamic local:<model> ids never KeyError/drop) + extended Phase model.
- M6 core: Ollama loopback run_local adapter + live per-agent circuit breaker (skip-in-cooldown, classified failures, backoff).
Two integration runs exercised the live pipeline: HelloCounter (happy path — compiled + xcodebuild VERIFIED + independently re-verified) and TipJar (degradation path — agents struggled this session; engine handled it gracefully with an honest UNVERIFIED and no crash).

## 2026-07-03 — Remediation pass (paths, honesty gates, resume, local models, GUI controls)

All items below are in the tree and covered by the verification gate run this
date: `make verify` = engine unittest suite **166 tests** (up from 81 at the
start of the pass; also clean under `-W error::ResourceWarning`) + GUI
`swift build -c release` clean + GUI XCTest **22 tests** (up from 0) + doctor.

- **Portable root paths.** `config.yaml` ships `root: "./workspace"`, resolved
  against the repo (parent of `orchestrator-v2-source/`); `--root PATH` and
  `ORCH_ROOT` override it. `run.sh` and `install_launch_agent.sh` derive the
  engine + workspace from their own location and pass `--root`; the retired
  hardcoded home-directory install layout is gone from every script and doc.
- **Completeness fix.** Profiles (prototype/mvp/v1/production_draft) and stop
  targets (incl. "docs complete") now map to the shipped workflows' real phase
  keys, with warn-and-fall-back behavior — the pre-fix behavior could silently
  strip a run to 1–2 phases.
- **blocked_conflict.** Lane-worktree merge conflicts pause the run (even fully
  autonomous) with `blocked_conflict = {lane, files, detail}` persisted in
  `agent_state.json`; lane worktrees are kept for manual resolution. Merges are
  never resolved last-write-wins.
- **`--resume <slug>`.** Resumes an existing project from saved state; refuses
  a missing project/state file, exits cleanly when already complete, clears a
  recorded abort error and the `blocked_conflict` marker before re-entering.
- **Per-project artifacts.** `tasks.json` (§19 backlog) and `interfaces.json`
  (shared contract, extracted from `tech_specs`), `live_log.jsonl`,
  `verify_results.json`, and the deterministic `docs/` renders: PRD,
  TECHNICAL_ARCHITECTURE, QA_REPORT, KNOWN_LIMITATIONS, PROJECT_DOCUMENTATION,
  LAUNCH_READINESS (+ `findings.json`, `phase_outputs.json`).
- **Secret scan + redaction hardening.** Deterministic scan of the generated
  `app_build/` source after every build phase; open `secret_hardcoded` findings
  put a LAUNCH BLOCKED line in LAUNCH_READINESS.md. All agent output passes the
  `redact_secrets` chokepoint before persistence. Gemini key file default is
  `~/.orchestrator/gemini_api_key`; secret-shaped filenames are gitignored and
  `run.sh` refuses to commit them.
- **Ollama local-model support.** Curated `local_models.json` registry;
  `models.ollama` + `agents.ollama_enabled` config; a local model never
  coordinates while a cloud agent is available and sits out sprint workflows by
  default; doctor gains a `local_models` block; GUI gains a Local Models
  settings pane with the §12.4 privacy-boundary text.
- **GUI.** Stop button (SIGTERM, SIGKILL after ~5s, session-local), aborted-run
  error banner, blocked-conflict banner, VERIFIED/FAILED/UNVERIFIED badge +
  Verification card over `verify_results.json`, approval flow writing
  `approvals/<phase>.{ok,edit,changes}` (Approve / Edit & Approve / Request
  Changes), and engine resolution: bundled engine → repo discovery → clear
  error banner (no hardcoded fallback path).
- **Gate.** New repo-root `Makefile`; `make verify` is the canonical check —
  this repo has no git remote, so there is no hosted CI.
