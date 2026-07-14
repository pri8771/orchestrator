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

## Round 5 — post-Round-4 completeness pass (commits `4c764b9`..`40dbdce` + doc closure)

- **Scope:** not a fresh four-lane audit — a targeted completeness sweep
  triggered by an explicit ask to close every remaining known gap: the
  original TASKS.md overflow list (never fully cleared, despite this file's
  Round 1 entry previously claiming it was), a real GUI data-loss bug found
  during that sweep, and four architecture recommendations from an earlier
  research pass that had been intentionally deferred (phase-transition
  summarization, adaptive escalation, round-level crash resume, a
  test-writing phase with real `xcodebuild test`) plus three previously
  "intentional non-decisions" (Ollama effort control, opt-in cost
  estimation, a tomli-free packaging test) that were revisited and finished
  rather than left as permanent gaps.
- **Ground-truth verification first:** a dedicated pass ran the actual test
  suite/mypy/ruff/style-check/syntax-check/shell-syntax-check commands (not
  a review of commit messages) to establish 682 tests green, mypy clean,
  ruff clean, GUI style guard at 0, before any fix work started.
- **Overflow list (7 items, all closed):** `knowledge.retrieve` returning a
  bare header when every matched chunk truncates to empty; a dead `rnd`
  parameter in `seed_demo.py`; no DB index backing `global_resource._reap`'s
  scan; the spoofed `urlfetch` `USER_AGENT` under-documented; `try!` on
  `NSRegularExpression` in `TranscriptParser.swift`; no GUI-level
  snapshot/interaction tests (closed as a documented non-goal — see
  `gui/DESIGN-REFRESH.md` §8 — plus real new pure-logic test coverage
  instead); QuickLook preview for attached docs never implemented.
- **Real bug found and fixed (not in any prior audit):** the GUI's Routing
  Grid could silently DELETE a hand-edited per-role (`roles.worker`/
  `roles.integrator`) effort override the next time an operator saved any
  unrelated change from the grid, because `ModelRouting.load()`/`.save()`
  didn't know that key existed. Fixed with full round-trip support plus a
  minimal editing UI; caught a *third* live instance of the actor-isolation
  `nonisolated` bug class (`CommandPaletteView.fuzzyScore`) while adding
  test coverage for it.
- **"Intentional gap" items, finished rather than left deferred:** real
  Ollama `"think"` reasoning-effort wiring on the HTTP `/api/generate` path
  (the CLI subprocess path has no equivalent flag and honestly stays
  noop-with-a-warning); opt-in, honestly-labeled per-1k-char cost estimation
  for `--postmortem` (absent by default, zero behavior change unless an
  operator configures `cost.pricing`); a stdlib-only hand-rolled parser
  closing the Python 3.9/3.10 packaging-test gap without adding a `tomli`
  dependency.
- **Architecture items, finished:** phase-transition summarization
  (`phase_summaries.json`, a `runtime.phase_summary_policy` knob, recent
  phases still get full transcript, older phases get the compact summary);
  round-level crash resume (parses the on-disk transcript to find the real
  last-complete round rather than trusting a lone counter, scoped to the
  sequential round loop only — the parallel build loop already has
  git-based recovery); adaptive quality-based escalation (bumps effort,
  then model, only on repeated repair/quality-gate failures, reverts
  immediately after, gated by `runtime.adaptive_escalation_enabled`); a
  `write_tests` phase with real `xcodebuild test` execution, scoped to the
  two deep pipelines only (`app_build`, `full_max` — not the speed-oriented
  `sprint`/`vslice`/`prototype`/`iterate`), purely observational by default
  (`runtime.tests_gate_release` defaults false) with a global kill-switch
  (`runtime.run_generated_tests`) and the flakiness/cost tradeoff documented
  in config comments rather than silently defaulted into every run.
- **Self-caught issues during the pass itself:** one implementation attempt
  returned without doing any work (misread its own role as an orchestrator)
  — detected via `git status`/`git log` showing nothing changed, and
  relaunched with explicit "do the work yourself" instructions; a stale
  documentation claim written during this same closure pass (that a
  knowledge-injection comment duplication had already been fixed) was
  caught as false before being committed and the actual one-line mislabeled
  comment was fixed for real.
- **Outcome:** 779 tests green (up from 682 at Round 4's close), mypy clean
  (23 files), ruff clean, GUI style guard at 0. Every item surfaced by the
  completeness audit that triggered this round is closed or, where genuinely
  infeasible (full SwiftUI snapshot testing under the zero-dependency rule;
  Ollama CLI-subprocess effort control absent an actual CLI flag; Gemini
  effort control absent an actual CLI flag), consciously documented as a
  permanent, evidence-based non-goal rather than left as a silent gap.

## Current state

Every item from all five rounds is closed. The repo keeps three enforcing
gates green in CI — the unittest suite (779 tests, strict warnings), `mypy`
(pyproject-pinned config), and `ruff` — plus the GUI style guard at a hard 0
violations. The live, forward-looking record of failure classes is
`MISTAKES.md`; per-project evidence accumulates in each app's
`mistakes.jsonl`, correlated on demand via `--mistakes`/`--postmortem`.

Two things remain genuinely open by deliberate choice, not oversight, and are
recorded here so a future audit doesn't need to rediscover them: (1) CI's
GitHub Actions runners were observed failing at the platform level (jobs
never assigned a runner, not a code issue) during this round — worth
re-checking the Actions/billing status before trusting a red X; (2) GUI
design-refresh Tranche 3 (a Files tab, structured-argument palette verbs,
routing-grid drag-fill-paint, a menu-bar restyle) is net-new feature work
tracked in `gui/DESIGN-REFRESH.md`, not a "close this gap" item — intentionally
out of scope for an audit-completeness pass.
