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
- **Stop for externally-launched runs — this was stale, not actually
  missing** (was #3 below): `stopRun` already fell back to the per-app
  `.orch-locks/<app>.lock` file's `pid=` when the GUI has no in-process
  handle, which covers shepherd/terminal-launched runs — the same job a
  dedicated PID file would do. That fallback had no test coverage and no
  liveness check, so a stale lock (left by a crashed run, pid since
  recycled) could have SIGTERM'd an unrelated process; added the
  `kill(pid, 0)` guard and `FactoryScannerLockTests` for the lock-parsing
  logic it depends on.
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
- **Dynamic Type sweep** (was #3 below): the seven text tokens `DS.font`
  documents as "maps to Dynamic Type styles" (§2.3's own table — largeTitle,
  title, headline, body, callout, caption, caption2) now actually scale,
  via `Font.custom(".AppleSystemUIFont", size:, relativeTo:)` — the
  standard way to keep a custom base pixel size while still tracking a
  Dynamic Type text style, since `Font.system(size:)` never scales. The
  four SF Mono / display tokens (`stat`, `monoWell`, `monoInline`,
  `monoCaption`) and the icon token stay fixed, matching the same table's
  "fixed" column. The two geometry-load-bearing opt-outs the spec already
  named — routing-grid cells, phase timeline — are pinned back to the
  system default via `.dynamicTypeSize(.large)` on their container view;
  both already carry full VoiceOver labels, satisfying the spec's stated
  compensation. Verified by build + the full GUI unit suite; **not**
  verified visually against a running app with a non-default text size —
  `swift run`'s unbundled binary isn't addressable by this environment's
  screenshot tooling (no `.app` bundle for the OS permission model to
  grant), so the layout claim (dense rows/chips don't reflow) rests on the
  opt-out pins and code review, not an actual screenshot at
  `.accessibility3` or similar. Worth a real look next time this machine
  has an interactive session.
- **Web build targets** (was #3 below): `verify.py` now has a real
  `_verify_web` install-then-build branch — closing the gap the old node
  path left open (it ran `npm run build --if-present || node -e exit(0)`
  with NO install, so a deps-having app couldn't build and the `||` masked
  failures as `ok=True`). Now `npm install` → `npm run build`, routed to
  from both the explicit `type: web|npm|node` spec and the auto `node`
  detection. Shipped the security-hardened v1 an adversarial design review
  converged on: install+build only (Playwright cut — a separate heavy/flaky
  surface); the child env is scrubbed of secret-shaped vars and given a
  per-verify npm cache; macOS-sandboxed with the build_dir carved back into
  the write-allow (so a workspace-inside-the-repo layout doesn't hard-fail);
  and network/registry/auth/timeout install failures classified as
  `ran=False` (unverified), NOT a release-blocking `ok=False` — a registry
  blip must not fail a build the toolchain never got to judge. Residual `npm
  install` read-exfil risk documented in KNOWN_LIMITATIONS.
- **Per-phase rollback + diff viewer** (was #4 below): the Build-history sheet
  now lists structured commits (short sha · date · phase badge · subject ·
  run-tag) instead of a raw `git log` dump, and adds two capabilities against
  the same `app_build/.git`: roll the build back to any historical commit, and
  compare two commits. The rollback is deliberately NOT `git reset --hard` —
  it materializes the chosen commit's tree as a NEW forward commit
  ("orchestrator: rolled back to <sha>"), so history is preserved and the
  action is itself undoable (roll back again). Following the adversarial
  design review: history is filtered to the orchestrator's own commit
  subjects (a worktree-isolation lane commit fast-forwards onto the mainline,
  so `--first-parent` alone would surface a partial-tree lane snapshot as a
  rollback target — the subject filter is the real guard); the dirty-tree gate
  includes untracked files (an untracked non-ignored file has no git backing,
  so `git clean` would destroy it — refuse rather than lose it); the rollback
  commit passes an explicit `-c user.name/email` so it never depends on
  ambient git config; and it hard-refuses whenever any live-run signal
  (`running`/lock/`canStop`) is set. The diff is a per-file unified view with
  +/- coloring (a true two-column side-by-side is a large custom-SwiftUI lift
  for marginal gain over the format engineers already read in PRs). 22 new
  XCTest cases against a real temp git repo cover the forward-commit tree
  identity, the git-restore deletion gap, reversibility round-trip, the dirty/
  untracked/unknown-sha/no-change guards, ignored-artifact preservation, and
  the lane/merge-commit exclusion.

## Genuinely next (in rough priority order)

1. **Gemini session deltas — mechanism verified 2026-07-15, wiring not yet
   shipped.** With a real key: `--session-id` + `--yolo` create a persisted,
   resumable session, and `--resume` reloads it and replays the full prior
   conversation (confirmed by inspecting the stored session JSONL — a
   resume-turn was appended to the same session that still held the earlier
   context). So the flags compose and context survives, same as codex. Not
   yet wired into the concurrent orchestrator: gemini's `--resume` addresses
   sessions by `latest`/index, not by UUID, so parallel gemini lanes in one
   project dir need `--session-file <path>` (id-addressable) to avoid a
   "latest" collision — that concurrency wiring wants a live multi-lane run
   to validate, and the free-tier quota (20 req/day/model) caps how much can
   be exercised per day.
2. **Live validation of the conflict loop** — a real token-spending run with
   `worktree_isolation: true` that hits a conflict, is resolved manually, and
   finishes via `--resume` (mechanics are unit-tested; the human loop isn't
   proven live).
3. **library_mining scaffold phase** — today it produces the extraction
   plan/report; building the package is a follow-on.

## Launch / test

    bash gui/run_gui.sh                       # build + launch the app (macOS)
    make verify                               # full local gate (macOS)
    python3 -m unittest discover -s tests     # engine suite
    python3 orchestrator.py --doctor --json   # preflight
