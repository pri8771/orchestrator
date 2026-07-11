# NEXT_MILESTONES.md

_Refreshed 2026-07-05. Gate: `make verify`
(191 engine unittest + 37 GUI XCTest + release build + doctor — all green)._

## Landed in the 2026-07-03 remediation pass

- **Portable paths**: engine root is `/Users/pchordia/Documents/iOS-App-Factory`;
  `--root` / `ORCH_ROOT` override; `run.sh`,
  `install_launch_agent.sh`, and `run-orchestrator.sh` all derive paths from
  their own location. No hardcoded machine-specific install path remains in
  code or docs.
- **Completeness/stop-target fix**: profiles (prototype/mvp/v1/
  production_draft) and stop targets ("docs complete", "tech spec complete", …)
  now map to the real shipped phase keys, with defensive fallbacks instead of
  silently gutting a run.
- **Worktree conflicts pause honestly**: a real merge conflict records
  `blocked_conflict` in `agent_state.json`, pauses the run (never
  last-write-wins), and keeps lane worktrees for manual resolution.
- **`--resume <slug>`**: refuses missing project/state, no-ops on completed
  projects, clears the abort error + `blocked_conflict` marker, re-enters the
  pipeline.
- **Per-project artifacts**: `tasks.json` + `interfaces.json` machine
  contracts, `live_log.jsonl`, `verify_results.json`, and the deterministic
  `docs/` set (PRD, TECHNICAL_ARCHITECTURE, QA_REPORT, KNOWN_LIMITATIONS,
  PROJECT_DOCUMENTATION, LAUNCH_READINESS, findings.json, phase_outputs.json).
- **Secret hygiene**: deterministic secret scan of `app_build/` feeding a
  launch-readiness gate line; redaction chokepoint on all agent output; gemini
  key file default moved to `~/.orchestrator/gemini_api_key`; gitignore
  patterns + `run.sh` refusal to commit secret-named files.
- **Ollama local models**: curated `local_models.json` registry,
  `models.ollama` + `agents.ollama_enabled` config, never-coordinator rule,
  excluded from sprints by default, doctor `local_models` block, GUI Local
  Models settings pane with the §12.4 privacy text, Start/Delete controls, and
  Mac RAM fit labels.
- **Phase playbooks + research first**: editable `phase_rules.json` injects
  required outputs, acceptance checks, and one-shot quality rules into every
  app-build phase; `app_build` now starts with `product_research`, and PRD
  rendering carries that research forward.
- **GUI**: Stop button (SIGTERM→SIGKILL), aborted-run error banner,
  blocked-conflict banner, VERIFIED/FAILED/UNVERIFIED badge + Verification
  card, Approve / Edit & Approve / Request Changes approval flow
  (`approvals/<phase>.{ok,edit,changes}`), engine resolution with a clear
  missing-engine banner (no hardcoded fallback path).
- **Tests**: engine suite grew 81 → **191** (also clean under
  `-W error::ResourceWarning`); GUI went 0 → **37** XCTest cases over the
  extracted `EngineLogic.swift`. `make verify` is the canonical local gate.

## Genuinely next (in rough priority order)

1. **§19 task claiming** — advisory-locked claim/release over `tasks.json`
   (claimed_by/claimed_at, stale-claim reversion) so lanes pull work instead of
   being statically sliced.
2. **Live validation of the conflict loop** — a real token-spending run with
   `worktree_isolation: true` that hits a conflict, is resolved manually, and
   finishes via `--resume` (mechanics are unit-tested; the human loop isn't
   proven live).
3. **Vote/tally unit tests** — the forced-vote parse path is the largest
   untested engine branch.
4. **Sandbox `http` verification** — `verify.py` currently boots generated
   servers unsandboxed on this Mac.
5. **Move `live_log.jsonl` to `.orchestrator_runtime/`** per spec (engine +
   GUI + .gitignore together).
6. **Stop for externally-launched runs** — persist a PID file so the GUI can
   signal runs it didn't spawn (today Stop is session-local).
7. **Dynamic Type sweep of secondary sheets** (Models/Sub-agents/Rounds/Usage/
   Settings still use fixed point sizes).
8. **Web build targets** — a `verify.py` branch for npm/Playwright (designed,
   not built).
9. **library_mining scaffold phase** — today it produces the extraction
   plan/report; building the package is a follow-on.
10. **Per-phase rollback + side-by-side diff viewer** (full project
    reset/fork + build-history exist).

## Launch / test

    bash run-orchestrator.sh                                              # launch the app
    make verify                                                           # full local gate
    cd orchestrator-v2-source && python3 -m unittest discover -s tests    # 191 tests
    cd orchestrator-v2-source && python3 orchestrator.py --doctor --json  # preflight
