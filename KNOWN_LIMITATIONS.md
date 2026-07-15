# KNOWN_LIMITATIONS.md

Honest gaps as of 2026-07-15, verified against the current tree. "Spec" =
`orchestrator-v2-master-spec.md`.

## Engine — spec deviations

- **§19 task claiming has no OS-level advisory lock, by design, not by gap.**
  `tasks.json` now carries a real `claimed_by`/`claimed_at` protocol with
  stale-claim reversion (`_claim_tasks_for_iteration`), but claims are
  computed once, single-threaded, immediately before each iteration's worker
  fan-out — there is never a concurrent writer to lock against, since the
  parallel build lanes are threads inside one process, not separate OS
  processes racing on the file. An OS advisory lock would only matter if that
  ever changed.
- **§20's dedicated `interface_contract` phase does not exist.** No shipped
  workflow has that phase; `interfaces.json` is extracted from the
  `tech_specs` coordinator wrap-up (a ` ```interfaces-json``` ` machine-contract
  block) instead. Same artifact, different phase.
- **Completeness profiles are keyed to the shipped workflows' phase keys**, not
  the spec's aspirational 17–25-phase workflow (which is not implemented).
  Profiles and stop targets resolve correctly against the real `app_build`
  workflow and fall back defensively elsewhere.
- **The spec's §3 scope exclusions drifted.** Portfolio/library-mining
  orchestration shipped (portfolio.py, `library_mining` workflow) despite
  being listed as out-of-scope for the first build; the spec has an addendum
  noting this rather than a rewrite.

## Engine — enforcement gaps (documented behavior, weaker than it looks)

- **`designlint`'s `todo_marker` is a soft signal only.** Despite the DoD v1
  tier listing "no TODO/FIXME markers", strict mode promotes only
  `unlisted_package` to an error.
- **UI crawl's virgin-install guarantee has an escape hatch.** An
  internally-inconsistent `_sim_ctx` (udid/bundle set, empty `app_path`)
  silently skips the fresh-install step.

## Engine — untested / unsandboxed paths

- **`verify.py`'s `http` verification boot command is macOS-sandboxed
  (`sandbox-exec`) as of 2026-07-15, not fully unsandboxed anymore.** For
  `productionize` it boots the agent-written server via `/bin/sh -lc`
  (`npm start` / uvicorn / Flask autodetect), wrapped in a Seatbelt profile
  that denies writes to credentials (`~/.ssh`, `~/.aws`, `~/.orchestrator`,
  `~/.gnupg`, `~/.netrc`, Keychains) and the engine's own source, then polls
  `/health` and kills it. Deliberately a deny-list on top of `(allow
  default)`, not a full lockdown — an allow-list would need to anticipate
  every legitimate npm/pip/etc. cache path, and getting that wrong would
  make verification unreliable. No resource limits (CPU/memory/process
  count) yet, and non-macOS hosts get no sandboxing at all (falls back to
  plain unsandboxed, matching the old behavior). Flask-autodetected apps are
  additionally never told the allocated port. `xcodebuild`/`swift build`
  verification compiles but does not execute generated app code.
- **`blocked_conflict` → manual resolution → `--resume` has not been proven in
  a live token-spending run.** The pause/persist/clear mechanics are
  unit-tested (`test_worktree*.py`, `test_resume.py`); the end-to-end human
  loop has not been driven against real agents. `worktree_isolation` also
  remains **off by default**.
- **Forced-vote ballots are tallied deterministically as of 2026-07-15**
  (`tests/test_vote_tally.py`); the legacy LLM-tally fallback still depends on
  one model's prose reading when fewer than two ballots parse.

## GUI

- **Stop already signals externally-launched runs, via the per-app engine
  lock rather than a dedicated PID file.** `<root>/.orch-locks/<app>.lock`
  (written by every run, GUI or shepherd/terminal-launched, to serialize
  access to that app) carries `pid=`; `stopRun` falls back to reading it and
  sending the signal directly when the GUI has no in-process handle. This
  was previously (incorrectly) documented here as missing. Hardened
  2026-07-15 to check liveness (`kill(pid, 0)`) before signaling, so a stale
  lock left by a crashed run can't cause a SIGTERM to a recycled, unrelated
  pid.
- **Dynamic Type now scales (2026-07-15), unverified visually.** The seven
  text tokens `DS.font` documents as mapping to Dynamic Type styles scale
  via `Font.custom(".AppleSystemUIFont", size:, relativeTo:)`; the display/
  machine-output tokens and the two geometry-load-bearing views (routing
  grid, phase timeline) stay pinned, per spec. `ComponentKit` carries no raw
  sizes of its own (the earlier note here was stale — `ci_style_check.sh`
  already enforces zero hardcoded sizes outside `ThemeTokens.swift` and
  passes clean). What's unverified: an actual screenshot of the app at a
  non-default text size — `swift run`'s unbundled debug binary has no `.app`
  bundle for this environment's computer-use tooling to grant, so this
  shipped on build success + the unit suite + code review of the spec's own
  mapping table, not a human/visual check. Confirm on a real interactive
  session before trusting the "dense rows don't reflow" claim fully.
- **Packaged app / source-launch workspace split is possible.** Source launchers
  point at `~/Documents/iOS-App-Factory`; a packaged app launched
  without `ORCH_ROOT` uses its saved Settings value or the same default path.
- **Concierge chat requires the `claude` CLI at a known path.** The probe list
  is hardcoded (`~/.local/bin`, `/opt/homebrew/bin`, `/usr/local/bin`); a
  non-standard install degrades silently to "claude unavailable".

## Distribution / infrastructure

- **`gui/dist/` artifacts go stale.** The checked-out
  `Orchestrator.app` / `Orchestrator.dmg` are whatever was last packaged —
  rebuild with `make app` / `make dmg` before distributing; never ship a
  `dist/` that predates the current engine source.
- **CI cannot exercise simulators or agent CLIs.** GitHub Actions runs the
  engine suite, lint/typecheck, and a macOS `swift build`; visual QA / UI
  crawl / live-agent behavior are only exercised on a real Mac.

## Environment

- **Local models need a running Ollama server.** The binary being installed is
  not enough; `ollama serve` must be up (the doctor and the GUI's Local Models
  pane both report server reachability), and the selected model must be pulled.
