# KNOWN_LIMITATIONS.md

Honest gaps as of 2026-07-05, verified against the current tree. "Spec" =
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

## Engine — untested / unsandboxed paths

- **Forced-vote / tally parsing has no dedicated unit tests.** The consensus
  path is exercised heavily; the vote-fallback prompt/parse path has only been
  exercised in live runs, not in the 191-test suite.
- **`verify.py`'s `http` verification runs generated code unsandboxed.** For
  `productionize` it boots the agent-written server via `/bin/sh -lc`
  (`npm start` / uvicorn / Flask autodetect) on this Mac with no sandbox,
  polls `/health`, then kills it. `xcodebuild`/`swift build` verification
  compiles but does not execute generated app code.
- **`blocked_conflict` → manual resolution → `--resume` has not been proven in
  a live token-spending run.** The pause/persist/clear mechanics are
  unit-tested (`test_worktree*.py`, `test_resume.py`); the end-to-end human
  loop has not been driven against real agents. `worktree_isolation` also
  remains **off by default**.

## GUI

- **Stop only works for runs launched from the same GUI session.** Process
  handles aren't persisted, so after an app relaunch — or for a run started
  from a terminal / LaunchAgent — the Stop button cannot signal the run.
- **Accessibility font sweep is partial.** Primary surfaces (banners,
  verification card, approval bar) use semantic text styles and accessibility
  labels; secondary sheets (Models, Sub-agents, Rounds, Usage, parts of
  Settings) still use fixed point sizes that don't scale with Dynamic Type.
- **Packaged app / source-launch workspace split is possible.** Source launchers
  point at `~/Documents/iOS-App-Factory`; a packaged app launched
  without `ORCH_ROOT` uses its saved Settings value or the same default path.

## Distribution / infrastructure

- **No hosted CI.** The repo has no git remote; nothing runs automatically.
  `make verify` (test-strict + gui-build + gui-test + doctor) is the canonical
  gate and must be run manually before shipping.
- **`gui/dist/` artifacts go stale.** The checked-out
  `Orchestrator.app` / `Orchestrator.dmg` are whatever was last packaged —
  rebuild with `make app` / `make dmg` before distributing; never ship a
  `dist/` that predates the current engine source.

## Environment

- **Local models need a running Ollama server.** The binary being installed is
  not enough; `ollama serve` must be up (the doctor and the GUI's Local Models
  pane both report server reachability), and the selected model must be pulled.
