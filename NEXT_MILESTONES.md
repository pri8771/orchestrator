# NEXT_MILESTONES.md

_Refreshed 2026-07-15. Gate: CI (engine suite 870+ tests under
`-W error::ResourceWarning` on 3.9/3.11/3.12 + clean-tree guard, ruff, mypy,
macOS `swift build`); `make verify` remains the full local gate on macOS._

## Landed since the 2026-07-05 snapshot

- **Five audit rounds + completeness pass** merged to `main` (portable paths,
  atomic writes, resilience/cooldown layer, SSRF + redaction hardening,
  `--resume`, approvals, verification-honesty fixes). The 2026-07-03
  remediation items previously listed here all shipped and remain in place
  (see CHANGELOG_V2.md for the full record).
- **Quality-gate era** (merged from `dev`): design lint → visual QA → UI crawl
  → adherence release gates, Definition-of-Done tiers, requirements/flows
  machine contracts, fleet learning (incidents → anti-pattern ledger,
  exemplar export), eval harness (`--eval-report`), golden scaffold,
  provider pacing, RAM-aware local-model gating, GUI ChatHome + gate cards.
- **Honesty fixes** (2026-07-14): fail-closed secret redaction; eval scoring
  reads the real `verify_results.json` shape; missing gate evidence scores as
  missing rather than clean; GUI fleet health counts done-but-verify-failed
  projects as failed.
- **Decision-layer upgrade** (2026-07-15): machine-tallied `vote-json`
  ballots (deterministic, confidence-weighted, self-votes invalid; the LLM
  tally survives only as a fallback), independent quality-gate evaluator (the
  coordinator no longer grades its own wrap-up), ballots cast concurrently,
  release-gate timing telemetry (`release_gate_timing` events), ChatHome
  conversation survives navigation, and tests can no longer mutate the repo
  (CI clean-tree guard).

## Genuinely next (in rough priority order)

1. **Contract enforcement as gate failures** — a missing/malformed
   tasks-json/requirements-json block should trigger a targeted repair round,
   not just a `WARN CONTRACT` line.
2. **Requirements-coverage check** — every core requirement ID covered by ≥1
   task before build starts; uncovered IDs listed verbatim in a repair round.
3. **Codex/Gemini session deltas or transcript windowing** — only Claude gets
   delta prompts today; full-transcript resend is the biggest token/latency
   waste in long discussions.
4. **Effort-by-phase routing defaults** — ship a fleet `model_routing.json`
   raising reasoning effort on tech_specs/design_handoff.
5. **Exemplar injection** — feed `--save-exemplar` output back into phase
   prompts (currently written, never read).
6. **§19 task claiming** — engine-assigned claim/release over `tasks.json`
   (claimed_by/claimed_at, stale-claim reversion) so lanes pull work instead
   of being statically sliced.
7. **UNRESOLVED phase state** — surfaced in docs + GUI when a phase closes on
   a failing quality gate, failed tally, or missing contract
   (`state.phase_resolutions` already records the quality-gate case).
8. **Live validation of the conflict loop** — a real token-spending run with
   `worktree_isolation: true` that hits a conflict, is resolved manually, and
   finishes via `--resume` (mechanics are unit-tested; the human loop isn't
   proven live).
9. **Sandbox `http` verification** — `verify.py` currently boots generated
   servers unsandboxed.
10. **Move `live_log.jsonl` to `.orchestrator_runtime/`** per spec (engine +
    GUI + .gitignore together).
11. **Stop for externally-launched runs** — persist a PID file so the GUI can
    signal runs it didn't spawn (today Stop is session-local).
12. **Dynamic Type sweep** — the `DS.font` token layer itself is fixed-point,
    so Dynamic Type is broken app-wide (not just secondary sheets as
    previously noted here).
13. **Web build targets** — a `verify.py` branch for npm/Playwright (designed,
    not built).
14. **library_mining scaffold phase** — today it produces the extraction
    plan/report; building the package is a follow-on.
15. **Per-phase rollback + side-by-side diff viewer** (full project
    reset/fork + build-history exist).

## Launch / test

    bash gui/run_gui.sh                       # build + launch the app (macOS)
    make verify                               # full local gate (macOS)
    python3 -m unittest discover -s tests     # engine suite
    python3 orchestrator.py --doctor --json   # preflight
