# KNOWN_LIMITATIONS.md

Honest gaps as of 2026-07-15, verified against the current tree. "Spec" =
`orchestrator-v2-master-spec.md`.

## Engine — spec deviations

- **§19 advisory-lock task claiming is not implemented.** `tasks.json` is
  written and validated (backlog, lanes, acyclic `depends_on`), and build
  workers are prompted with their lane's task slice — but there is no
  claim/release protocol (`claimed_by`/`claimed_at` stay null), no OS advisory
  locking around read-modify-write, and no stale-claim reversion.
- **§20's dedicated `interface_contract` phase does not exist.** No shipped
  workflow has that phase; `interfaces.json` is extracted from the
  `tech_specs` coordinator wrap-up (a ` ```interfaces-json``` ` machine-contract
  block) instead. Same artifact, different phase.
- **`live_log.jsonl` location deviates from the spec.** The spec places it at
  `<project>/.orchestrator_runtime/live_log.jsonl`; the engine writes
  `<project>/live_log.jsonl` (project root). The GUI and `.gitignore` follow
  the engine.
- **Completeness profiles are keyed to the shipped workflows' phase keys**, not
  the spec's aspirational 17–25-phase workflow (which is not implemented).
  Profiles and stop targets resolve correctly against the real `app_build`
  workflow and fall back defensively elsewhere.
- **The spec's §3 scope exclusions drifted.** Portfolio/library-mining
  orchestration shipped (portfolio.py, `library_mining` workflow) despite
  being listed as out-of-scope for the first build; the spec has an addendum
  noting this rather than a rewrite.

## Engine — enforcement gaps (documented behavior, weaker than it looks)

- **Machine contracts are requested, not enforced.** A phase whose wrap-up
  omits or mangles its required tasks-json/requirements-json/flows-json block
  closes anyway with a `WARN CONTRACT` line and a mistakes-ledger entry — no
  repair round is triggered. (Top of NEXT_MILESTONES.md.)
- **No requirements-coverage check.** Nothing verifies that every core
  requirement ID in `requirements.json` is covered by a task before build.
- **A phase can close on a failing quality gate.** When repair rounds are
  exhausted, the phase closes with a warning appended to its output and a
  `phase_resolutions` marker in state; the GUI does not yet surface it.
- **`designlint`'s `todo_marker` is a soft signal only.** Despite the DoD v1
  tier listing "no TODO/FIXME markers", strict mode promotes only
  `unlisted_package` to an error.
- **UI crawl's virgin-install guarantee has an escape hatch.** An
  internally-inconsistent `_sim_ctx` (udid/bundle set, empty `app_path`)
  silently skips the fresh-install step.

## Engine — untested / unsandboxed paths

- **`verify.py`'s `http` verification runs generated code unsandboxed.** For
  `productionize` it boots the agent-written server via `/bin/sh -lc`
  (`npm start` / uvicorn / Flask autodetect) with no sandbox, polls
  `/health`, then kills it. Flask-autodetected apps are additionally never
  told the allocated port. `xcodebuild`/`swift build` verification compiles
  but does not execute generated app code.
- **`blocked_conflict` → manual resolution → `--resume` has not been proven in
  a live token-spending run.** The pause/persist/clear mechanics are
  unit-tested (`test_worktree*.py`, `test_resume.py`); the end-to-end human
  loop has not been driven against real agents. `worktree_isolation` also
  remains **off by default**.
- **Forced-vote ballots are tallied deterministically as of 2026-07-15**
  (`tests/test_vote_tally.py`); the legacy LLM-tally fallback still depends on
  one model's prose reading when fewer than two ballots parse.

## GUI

- **Stop only works for runs launched from the same GUI session.** Process
  handles aren't persisted, so after an app relaunch — or for a run started
  from a terminal / LaunchAgent — the Stop button cannot signal the run.
- **Dynamic Type is broken app-wide.** Every `DS.font` token is a fixed-point
  `Font.system(size:)` (and `ComponentKit` adds raw sizes of its own), so no
  text in the app scales with the user's Dynamic Type setting — a stronger
  statement than the earlier "secondary sheets" note.
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
