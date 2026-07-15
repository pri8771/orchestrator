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

## Landed since the previous snapshot (2026-07-15, later same day)

- **Final-round consensus fix**: the coordinator's final-round prompt no
  longer forces `CONSENSUS: YES` regardless of real disagreement — that was
  making the just-shipped deterministic vote-tally system effectively
  unreachable. Honest `CONSENSUS: NO` now correctly falls through to the
  weighted vote.
- **4 more audit findings**: Gemini prompts go over stdin, not argv;
  `commit_and_push` now defaults to `false`; `roles.json` no longer ships
  hardcoded agent-role pins; `events.py` redaction recurses into nested
  dict/list fields.
- **GUI**: ChatHome has a "New Chat" reset; chat history persists to disk
  (Application Support) instead of being lost on quit; the background
  refresh loop has a watchdog so a stalled scan can't freeze every tab
  forever.
- **Contract enforcement as gate failures** (was #1 below): a malformed/
  missing tasks-json or interfaces-json block gets up to
  `runtime.contract_repair_limit` (default 2) targeted repair turns before
  falling back to the old warn-and-proceed behavior.
- **Requirements-coverage check** (was #2): every CORE requirement needs a
  task naming it (new `requirement_ids` field), checked mechanically and
  repaired the same way as a malformed contract.
- **Codex session deltas** (was #3, partial): codex now reuses one CLI
  session per phase/lane like claude, but build phases only —
  `codex exec resume` has no `--sandbox` flag and always runs
  workspace-write regardless of the original session, verified against a
  real install. Gemini's `--session-id`/`--resume` flags exist but weren't
  verified to compose correctly with `--yolo` (this session's Gemini quota
  ran out mid-check) — still open, see below.
- **Effort-by-phase routing defaults** (was #2 below): the fleet
  `model_routing.json` now ships real (not just illustrative) entries
  raising `codex_reasoning`/`claude_reasoning` to `"high"` on `tech_specs`
  and `design_handoff`.
- **Exemplar injection — this was stale, not actually missing**: turned out
  `_load_phase_exemplar`/the `cfg["_phase_exemplar"]` splice in
  `build_context` were already added 2026-07-12, three days before this was
  written down as still-open. The feature was real; only its test coverage
  wasn't — `export_exemplars` (write) had a test, `_load_phase_exemplar`
  (read) and the round-trip between them didn't. Added that coverage rather
  than re-implementing something that already worked.
- **§19 task claiming** (was #2 below): `_claim_tasks_for_iteration` replaces
  the old "every worker just filters tasks.json by owner_lane == its
  lane_id" slice with real `claimed_by`/`claimed_at` — sticky across
  iterations (worker session-reuse continuity), reverted the moment a
  claiming worker leaves the roster, and any lane-mismatched/orphaned/
  overflow task round-robined across the whole roster instead of silently
  never being built. Computed single-threaded before each iteration's
  worker fan-out, so there's no concurrent-writer race to lock against.
- **UNRESOLVED phase state** (was #2 below): `state.phase_resolutions` now
  covers all three closes-without-a-clean-resolution cases — failing quality
  gate (already existed), a forced vote that never actually decided
  (`vote_undecided`, new), and an unrepaired tasks/interfaces contract or
  requirements-coverage gap (`contract_error`/`requirements_coverage_gap`,
  new — reusing the mistake classes item 1/2 already introduced). Surfaced
  two ways: every case also gets a `--mistakes`-report-visible ledger entry
  (not just a state marker), and the GUI's phase list now shows an amber
  warning triangle (with the reason on hover) on a phase that closed this
  way instead of reading identically to a clean done.
- **Sandbox `http` verification** (was #3 below): the generated-server boot
  command now runs under a macOS `sandbox-exec` (Seatbelt) profile —
  `(allow default)` plus a deny-list on writes to credentials (`~/.ssh`,
  `~/.aws`, `~/.orchestrator`, `~/.gnupg`, `~/.netrc`, Keychains) and the
  engine's own source. Deliberately a deny-list, not a strict allow-list —
  an allow-list would need to anticipate every legitimate npm/pip/etc.
  cache path, and getting that wrong would make verification unreliable.
  Falls back to the old unsandboxed behavior on any non-macOS host or if
  `sandbox-exec` is missing. Also caught and fixed a genuine (if narrow)
  file-write race while investigating this: `_await_approval`'s poll loop
  did `os.path.exists()` then immediately read — fine against the real
  writer (the GUI already writes atomically), but exposed a flaky CI test
  whose own drop-helper wrote non-atomically.
- **`live_log.jsonl` moved to `.orchestrator_runtime/`** (was #3 below), per
  spec: `<project>/.orchestrator_runtime/live_log.jsonl` instead of
  `<project>/live_log.jsonl`. `.orchestrator_runtime/` already existed for
  worktree isolation, so this just made live_log consistent with it — both
  now share `_orchestrator_runtime_dir(app_dir)`. It has no reader anywhere
  (engine or GUI) today, so this was a pure write-side move. Added
  `_ensure_workspace_gitignore(root)` (engine-owned, mirrors
  `_ensure_build_gitignore`, called once per run) so `run.sh`'s `git add -A`
  can't stage `.orchestrator_runtime/`'s scratch/log content into a
  workspace's git history.

## Genuinely next (in rough priority order)

1. **Gemini session deltas** — verify `--session-id`/`--resume` actually
   compose with `--yolo` (the build-phase write flag) and preserve context
   across a resume, the same way it was verified for codex; wire in if so.
2. **Live validation of the conflict loop** — a real token-spending run with
   `worktree_isolation: true` that hits a conflict, is resolved manually, and
   finishes via `--resume` (mechanics are unit-tested; the human loop isn't
   proven live).
3. **Stop for externally-launched runs** — persist a PID file so the GUI can
   signal runs it didn't spawn (today Stop is session-local).
4. **Dynamic Type sweep** — the `DS.font` token layer itself is fixed-point,
   so Dynamic Type is broken app-wide (not just secondary sheets as
   previously noted here).
5. **Web build targets** — a `verify.py` branch for npm/Playwright (designed,
   not built).
6. **library_mining scaffold phase** — today it produces the extraction
   plan/report; building the package is a follow-on.
7. **Per-phase rollback + side-by-side diff viewer** (full project
   reset/fork + build-history exist).

## Launch / test

    bash gui/run_gui.sh                       # build + launch the app (macOS)
    make verify                               # full local gate (macOS)
    python3 -m unittest discover -s tests     # engine suite
    python3 orchestrator.py --doctor --json   # preflight
