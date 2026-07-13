# Audit History

Consolidated index of the four full-codebase audit/fix rounds. The per-round
backlogs (`TASKS.md`, `TASKS_ROUND2.md`, `TASKS_ROUND3.md`, `TASKS_ROUND4.md`)
are kept verbatim as historical records — commit messages reference their item
numbers — and this file is the map across them. For the *live* failure-mode
record (taxonomy + the runtime `mistakes.jsonl` ledger and `--mistakes` /
`--postmortem` reports), see `MISTAKES.md`.

## Round 1 — TASKS.md (commits `50b7167`..`6ac93eb`)

- **Scope:** first full audit of everything — engine (`*.py`), SwiftUI GUI,
  shell scripts, workflow/config JSON, docs, tests.
- **Items:** 100 tracked (21 P1, 40 P2, 39 P3) plus ~16 untracked overflow notes.
- **Headline fixes (P1):** SSRF in `urlfetch.fetch_url` (metadata/localhost
  reachable) and unvalidated redirect targets; command injection through
  filenames in `verify.py`'s `_verify_http`/`_detect_start`; unlocked
  `save_state()` losing concurrent updates; the fragile finding-JSON regex
  dropping audit findings; `fix_ios_signing` no-opping on most pbxproj files;
  an unassigned build lane (`polish_resilience` never built); empty agent
  replies marking phases complete; verification-gate skip for
  `requires_verification` phases; the machine-wide worker cap failing open;
  secret-shaped files shipping inside the DMG; a main-actor `proc.run()` GUI
  freeze; silent GUI config-write failures; a force-unwrap crash; plus README/
  Makefile/model-registry corrections.
- **Consequential P2s:** hardcoded personal paths in config/launch scripts;
  prompts passed on argv (visible via `ps`); prompt injection from fetched
  HTML; `verify_results.json` records lost under concurrency; parallel log
  appends corrupting `live_log.jsonl`/`orchestrator.log`.
- **Deferred:** a few items closed as no-change-by-design (#72, #86, #93) or
  partial (#97 dead-Swift removal, #99 untestable queue logic), plus the
  overflow list — picked back up by Round 2 (e.g. shepherd queue-order tests
  → R2 #30) and fully cleared by Rounds 3–4.
- **Outcome:** all 100 addressed across eight commits; suite grew 390 → 476.

## Round 2 — TASKS_ROUND2.md (commits `b958a46`..`d46e960`)

- **Scope:** fresh audit after the Round 1 fix pass, deliberately re-auditing
  Round 1's new code (miniyaml, Command Palette, pause/resume, `surfaceError`).
- **Items:** 73 tracked (6 P1, 30 P2, 37 P3).
- **Headline fixes (P1):** SSRF DNS-rebinding TOCTOU that bypassed Round 1's
  guard; `load_roles` returning mutable built-in defaults by reference;
  `config.yaml` still shipping in the DMG; 7 of 14 workflows silently
  substituting `app_build` when their JSON was missing; `resolve_root`
  docstring/config contradiction; Command Palette focus.
- **Consequential P2s:** racy `portfolio.materialize_children`; fixed
  verification port colliding under parallel builds; `redact_secrets` entropy
  fallback corrupting legitimate JSON; no preflight that *any* agent was
  runnable; non-atomic writes in backfill/seed_demo/urlfetch.
- **Deferred:** #70 (dev extras) slipped between batches → closed as R3 #15;
  the redaction fix later reopened on new trigger shapes (R3 #5, R4 #2–3);
  the agent preflight found incomplete for local models (R3 #7).
- **Outcome:** all 73 landed (recorded in TASKS_ROUND3.md's header).

## Round 3 — TASKS_ROUND3.md (commits `392aace`, `42159ad`)

- **Scope:** fresh audit stress-testing Round 2's newest surfaces (SSRF
  pinning, atomic-write patterns, cap validation, fence-aware redaction,
  mtime caches, palette keyboard nav).
- **Items:** 15 tracked (1 P1, 7 P2, 7 P3) + 7 informational notes.
- **Headline fix (P1):** `global_resource.release()` could free a *different*
  concurrent claim's slot (claims keyed only by pid + resource class),
  oversubscribing the machine-wide cap — fixed with per-claim tokens.
- **Consequential P2s:** string-truthiness bugs (`"false"` → `True`) in
  `Phase.__init__` and `modelrouting.load_routing`; fence-span redaction
  breaking on embedded triple-backticks; circuit-breaker health keys colliding
  across parallel build lanes; local-model preflight not checking the model
  was actually pulled.
- **Deferred:** 5 items (mtime same-second staleness, `localmodels.report()`
  cache bypass, `_reap` rollback, `install_launch_agent.sh` coverage,
  py-modules guard on old Pythons) — all cleared in Round 4's fix pass.
- **Outcome:** all 15 landed (recorded in TASKS_ROUND4.md's header).

## Round 4 — TASKS_ROUND4.md (commits `1188b28`, `4766574`)

- **Scope:** fresh audit stress-testing Round 3's fixes (per-claim token
  redesign, fence regexes, `_as_bool`/`_as_str_list`, palette `Command.id`).
- **Items:** 11 numbered (3 P2, 8 P3/cosmetic) — no P1s left — plus 5
  informational notes.
- **Headline fixes:** `Phase.verify` shape validation (bare string aborted the
  phase with `AttributeError`); two verified redaction escapes (labeled
  fallback fences, indented fences); the resource-cap claim leaking outside
  its `try/finally` on `Thread.start()` failure; CI finally running mypy as an
  enforcing gate (the exact gap a Round 3 regression slipped through).
- **Outcome:** all items *and* Round 3's deferred backlog cleared in the same
  pass ("R4 Batch A ... + deferred-backlog clearance"); independently
  re-audited six Round 3 fixes as correct; 584 tests + mypy clean at close.

## Current state

Every item from all four rounds is closed. Since Round 4 the repo has kept
three enforcing gates green in CI — the unittest suite (682 tests, strict
warnings), `mypy` (pyproject-pinned config), and `ruff` — while adding the
mistakes ledger + verification rollup, per-iteration build verification,
structured `decisions.json` phase handoffs, effort routing, and the
`--postmortem` correlated failure report. The live, forward-looking record of
failure classes is `MISTAKES.md`; per-project evidence accumulates in each
app's `mistakes.jsonl`.
