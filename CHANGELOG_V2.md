# CHANGELOG_V2.md

## Milestone 0 — Orient, preflight, preserve — COMPLETE (verified locally)

- Ran the V2 preflight/doctor check on the owner's Mac. Result: **PASS** — see `PREFLIGHT_RESULTS.md`. python3 3.14.4, git 2.53.0, Swift 6.3.3, Xcode 26.6, simctl, and all five agent CLIs (codex/claude/gemini/agy/ollama) present; spec + source present in the working repo.
- Confirmed the existing Python engine loads and seeds all six built-in workflows on Python 3.14 (`python3 orchestrator.py --seed`).
- Confirmed the existing SwiftUI GUI **compiles and links clean** on Swift 6.3 / Xcode 26.6 (`swift build` in `./gui`, ~5.5s, no errors). This establishes a working baseline to evolve.

## Milestone 1 — Runnable vertical slice — STARTED

- Added a machine-readable preflight to the engine (V2 spec §27): `python3 orchestrator.py --doctor --json` now emits a structured JSON report (`preflight_report()` + `_tool_version()` + `_PREFLIGHT_TOOLS` in `./orchestrator.py`), covering build-class tools (python3/git/swift/xcodebuild/simctl) and agent-class CLIs (codex/claude/gemini/agy/ollama) with presence, path, and version. Pure stdlib; spends no agent tokens. Verified: emits valid JSON with `build_capable: true`. This is what the GUI first-run onboarding (spec §10.1) consumes.
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
- Added `run-orchestrator.sh` at the repo root: launches the app with `ORCH_ROOT=workspace` + `ORCH_DIR=orchestrator-v2-source`, so generated projects live in `workspace/` and the engine/spec/GUI stay at the repo root. (Superseded by `gui/run_gui.sh`, which does the same thing portably — this script is no longer in the repo as of the main-branch merge.)
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
  against the repo (parent of `./`); `--root PATH` and
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

## 2026-07-11 — Operator-control + quality release (M1–M5)

All verified: 419 engine tests pass, GUI builds clean and all Swift test suites pass.

**Project management (GUI + engine)**
- Unified "Remove…" on every sidebar row: *Remove from list — keep folder* (writes `<project>/.orch_archived`; new collapsed Archived section with Restore) or *Remove and move folder to Trash*. Running projects are stopped first. `find_apps` skips archived projects, so watch/shepherd passes never relaunch them.
- Clicking a project now lands on the Transcript tab (phases + discussions center-stage); Run/Plan/History unchanged one segment away.
- New App sheet: **Save location** picker (workspace switch before creation) and a **profile** picker.
- Staged continuation: `--continue-with <workflow>` + a "Continue with workflow…" menu on done projects. Prior-workflow outputs persist in `carryover_outputs` and are injected ahead of prior phase decisions (research now → build later). Tests: `tests/test_continue_with.py`.
- Fixed `run-orchestrator.sh` pointing at a nonexistent `./` dir.

**Per-run control + library**
- Per-phase `rounds` (0 = unlimited/until natural consensus — no forced vote) and free-text `instructions` in `model_routing.json` (fleet or per-project), honored by the engine (`_apply_phase_routing` → `_routed_rounds` / `OPERATOR INSTRUCTIONS` context block). Plan tab gained a "Phase Rounds & Instructions" editor with an ∞ option and snippet insertion.
- Library: reusable prompt snippets (`library/snippets.json`) and saved run **profiles** (`library/profiles/*.json` — workflow + per-phase models/effort/rounds/instructions + fallback chains). Save from the Plan tab, apply from the New App sheet.

**Models & resilience**
- Per-project fallback chains and `local_model` in `<app>/model_routing.json` are now honored by the engine (non-empty project values win; round-tripped defaults never shadow fleet). The Plan tab's Fallback Overrides editor is no longer write-only.
- Provider pacing: `runtime.provider_min_gap_seconds` (default 8) staggers same-provider calls across threads/lanes to avoid burst rate-limits.
- Local-model RAM gate: `runtime.enforce_local_ram_gate` (default true) benches roster models whose registry `min_ram_gb` exceeds physical RAM (`localmodels.total_ram_gb`).
- Fixed the selected local model (glm-5.2 was never installed → now `qwen2.5-coder:14b`), pulled **qwen3-coder:30b** (19 GB, fits 48 GB RAM) and promoted it to the head of the roster.

**Build quality + token diet**
- **Prompt-adherence gate** (`runtime.adherence_gate`, default true): before a build workflow is marked done, one strong agent grades the built app against the ORIGINAL prompt's requirements; verdict + per-requirement grades land in `docs/adherence.json`; unmet core requirements route into the bounded iterate-repair loop. "It compiles" is no longer the only bar for "done".
- **PASS protocol**: from round 2 on, an agent with nothing new replies `PASS` — recorded as one line, keeps the round alive, shrinks every later turn's context.
- Round ceilings tuned: app_build/full_max discussion phases 9→3 (consensus still ends phases early), contract phases 4, build 6.
- `phase_rules.json`: design_handoff now requires a Swift-ready design-system spec (exact hexes light+dark, type ramp, spacing, radii) and per-screen state variants; build_coordination requires DesignSystem.swift first, token-only styling, and empty/loading/error states.

**Chat homepage**
- New default Home pane: a concierge chat backed by the logged-in `claude` CLI (API-key env stripped — subscription only). Explains the six modes (Ask/Plan/Spec/Create/Research/Audit), asks clarifying questions, and proposes runs as ```run-json``` cards with a one-click **Create run**. Degrades to static mode cards when the CLI is unavailable.

## 2026-07-11 — Visual QA gate (the "does it LOOK finished" bar)

- New `visualqa.py` + `runtime.visual_qa_*` config: after the release gate, the engine builds the app for the simulator (proper ad-hoc signing — verify's CODE_SIGNING_ALLOWED=NO bundles are uninstallable), boots/reuses a simulator, installs (with a one-shot rescue that strips un-installable app-extension placeholders), launches, and screenshots the app in BOTH light and dark mode into `<app>/docs/screenshots/`.
- Screens are graded by a PANEL of installed local vision models (gemma3 + qwen2.5vl over the Ollama loopback — zero cloud tokens) using a binary OK/BAD contract per image (measured: small VLMs echo JSON rubrics back; qwen2.5vl:3b alone false-fails good screens). Only a UNANIMOUS BAD fails a screen — a false FAIL burns a repair loop, a false PASS just stays quiet.
- A FAIL routes into the same bounded iterate-repair loop as the release/adherence gates (`status="visual_qa_repair"`), with the graders' notes in the repair reason. Verdict persists to `<app>/docs/visual_qa.json`.
- Verified END-TO-END on a real factory app (backtimer): install-rescue exercised, both screenshots captured, panel graded PASS on a genuinely finished onboarding screen in ~53s. 437 engine tests pass.

## 2026-07-11 — UI crawl gate (tap everything, replay every declared journey)

- New `uitest-runner/` (one generic XCUITest bundle, XcodeGen-generated, built once and cached): drives ANY installed app by bundle id. `testCrawl` BFS-crawls the app — taps every hittable element, fingerprints screens via an accessibility signature, records DEAD TAPS (tap changed nothing), verifies back-navigation restores the previous screen, screenshots every screen, and records crashes WITH the tap path that reached them. `testFlows` interprets declared user journeys (flows.json steps as DATA — agents never generate test code).
- New `uicrawl.py` gate after visual QA: crashes and failed declared flows FAIL the run into the bounded repair loop; dead buttons/back violations warn until promoted (`runtime.ui_crawl_fail_on_dead_buttons`). Every flow and the crawl each start from a VIRGIN install (one-shot onboarding state cannot leak between scenarios).
- Self-learning: a crash's recorded tap path is appended to `<app>/flows.json` as a permanent regression flow (origin=ui_crawl_crash) — future runs replay the exact edge case; the spec's flows never overwrite learned regressions.
- New ```flows-json``` machine contract on task_assignments (+ phase rule): 3-8 declared journeys covering every primary promise; persisted to `<app>/flows.json`. Build rule added: every interactive element sets .accessibilityIdentifier.
- Verified END-TO-END on backtimer: found a real dead button ("Restore Purchases" does nothing), a real back-navigation violation, passed a genuine onboarding flow on virgin install, and precisely caught a deliberately-missing feature ("no tappable element 'Export as PDF'"). 446 engine tests pass.

## 2026-07-11 — The 17-task batch: gates, fleet learning, per-phase teaching

All verified: 467 engine tests pass, GUI + crawler runner build clean, fleet report runs on the real factory.

- **Design lint gate** (designlint.py + tech_stack.json): deterministic errors for inline colors/font sizes outside DesignSystem.swift and banned SPM packages; warnings for TODOs, missing design system, unlisted packages. **Golden scaffold** (scaffold/ios_app) seeds empty builds with a starter DesignSystem.swift + binding conventions.
- **Screenshot-driven repair**: every gate failure's repair prompt now carries the exact artifacts (screenshot paths to OPEN, lint findings, failing flow step, crawl report).
- **Fleet learning** (fleetlearn.py): blamed incidents per gate (per-phase attribution), 👍/👎 project ratings (GUI Rate menu → rating.json), presort of the whole factory by implicit failure signals, anti-pattern ledger into knowledge/anti_patterns.md, per-phase exemplar export (--save-exemplar), phase scorecards (--fleet-report).
- **Definition of Done** (definition_of_done.json + completeness.dod_items): editable per-tier checklists, inherited upward, graded by the adherence gate alongside the new **requirements-json contract** (app_features emits numbered requirements → requirements.json → adherence grades exactly those).
- **Discussion-phase quality**: research source check (local model cross-checks claims against fetched URL text → docs/research_verification.json + incident), Red Team role in prompt_contract/tech_specs, per-role standards in personas, model-role pinning (Claude=design, Codex=backend), fattened phase rubrics.
- **Vertical slices**: tasks.json topo-layered into dependency waves; build iteration k works wave k only ("extend, don't rewrite"). **Rolling summaries**: local model compresses each closed discussion phase; later phases read summaries instead of raw transcripts.
- **Eval harness** (evalharness.py + evals/golden/): --eval-report scores any project set on compile/adherence/visual/flows/crawl/lint + composite, with human ratings alongside.
- **A11y audit** in the UI crawler (iOS 17 performAccessibilityAudit per screen, recorded as warnings). **GUI**: gate verdict chips in the Run tab, Rate… menu. **Doc distiller**: --distill-doc PATH_OR_URL → dense knowledge/ cheatsheet via the claude CLI.
- Cleanups: due_for_probe dead code removed; portfolio build-children now use app_build_child; README refreshed.

## 2026-07-12 — Calibration findings + closing wires

- **Calibration run (calib-tip-splitter) validated the whole batch live**: all 11 planning phases closed under the 2-round prototype ceilings; machine contracts landed (25 requirements, 11 flows, 24 tasks, 32 interfaces); golden scaffold seeded; 6 dependency waves computed; rolling summaries generated for every discussion phase; Red Team + model-role pinning active in personas.
- **Calibration finding — locals in build lanes**: local models debate in 10-30s but take 25+ min WRITING a lane, and each build round waits for its slowest worker. Locals are now excluded from build lanes by default (`runtime.locals_in_build_lanes: false` — they keep discussing and backstopping fallbacks), and any single local turn is capped (`runtime.local_turn_timeout_seconds: 600`). Tests cover lane exclusion, opt-in, and the local-only fallback.
- **Multi-screen visual grading**: the UI crawl's per-screen screenshots now go through the same local vision panel (warn-only; verdicts recorded as screen_grades in docs/ui_crawl.json) — visual coverage extends past the main screen for free.
- **Learning loop closed tighter**: the anti-pattern ledger auto-refreshes after every finished run, and a 👍 rating in the GUI auto-exports that project's phase outputs as exemplars (engine --save-exemplar).
- Pulled gemma3:12b as a sharper third judge for the vision panel (auto-joins VISION_CANDIDATES).
