# Top 100 Tasks

Prioritized backlog produced from a full audit of the engine (`*.py`), the SwiftUI GUI
(`gui/`), the shell scripts, the workflow/config JSON, the docs, and the test suite.
Priorities: **P1** = correctness/security bug, **P2** = robustness/portability,
**P3** = cleanup/enhancement.

## Status: all 100 addressed

Completed across eight commits (P1 → P3). The Python engine suite grew from 390 to
476 tests, green in strict-warnings mode. GUI (SwiftUI) changes were reviewed by hand
and are gated by the new macOS CI job (`.github/workflows/ci.yml`) — there is no Swift
toolchain in the authoring environment.

A few items were **resolved as "already correct" or consciously deferred** rather than
changed, with rationale:

- **#72 (config `rounds:` block):** kept — it is still referenced as a legacy
  round-count fallback in `process_phase`, so it isn't dead.
- **#86 (digest traversal caps):** no change — `build_target_digest` already breaks the
  walk at `tree_max` and char-budgets output, so the traversal is already bounded.
- **#93 (dedup the two demo scripts):** deferred — `seed_demo.py` and
  `simulate_stream.py` already carry *different* phase lists, so forcing a shared list
  would change behavior for marginal benefit. The real bug in that area (non-deterministic
  `hash()`) was fixed under #88.
- **#97 (delete the GUI's duplicate loader methods):** deferred — the methods are
  referenced within a self-contained cluster and deleting ~300 lines of Swift can't be
  compile-verified here; dead code is benign, a broken build is not.
- **#99 (queue-FIFO / transcript-reader tests):** partially done — added tests for the
  now-unified `slugify`; the queue and transcript logic are `@MainActor` + disk I/O and
  aren't unit-testable without first making `OrchestratorStore` constructible in tests.

---

## P1 — Critical: security and correctness bugs (1–21)

1. **[P1] Block SSRF in `fetch_url`** — `urlfetch.py:177-197` — any http(s) URL from a user prompt is fetched with no allow/deny-list, so a link to `http://169.254.169.254/…` or `http://127.0.0.1:<port>` reaches cloud metadata / internal services.
2. **[P1] Re-validate redirect targets in `fetch_url`** — `urlfetch.py:197` — urllib follows 3xx to internal hosts, bypassing any check done only on the original URL.
3. **[P1] Fix command injection in `_verify_http`/`_detect_start`** — `verify.py:220,259-263` — a module name derived from a generated filename is interpolated into `uvicorn %s:app` and run via `/bin/sh -lc`, allowing shell execution through a crafted filename.
4. **[P1] Guard `save_state()` and the shared `state` dict with a lock** — `orchestrator.py:2171` (and `_bump_fallback_count` at 1152) — parallel build workers mutate and rewrite the state dict concurrently with the main thread, so `phase_outputs`/`completed_phases` updates can be silently lost.
5. **[P1] Replace the fragile `_FIND_RE` finding-json regex with `schemas.extract_structured_blocks`** — `orchestrator.py:2460` — the lazy `\{.*?\}` cannot match array/multi-object bodies and swallows across unclosed fences, so audit findings are silently dropped.
6. **[P1] Fix the `DEVELOPMENT_TEAM` rewrite in `fix_ios_signing`** — `orchestrator.py:2997` — the alternation has two identical branches, only matches an empty quoted value, and never inserts the key when absent, so signing enforcement quietly no-ops on most pbxproj files.
7. **[P1] Assign every build lane in `build_worker_roster`** — `orchestrator.py:2745` — with the default 3 CLIs only lanes 0–2 get a worker, so `polish_resilience` tasks match no worker and are never built.
8. **[P1] Stop marking a phase completed on an empty/blank agent reply in `run_backfill`** — `backfill.py:153-174` — any reply that isn't literally `NOT_COVERED` (including `""`) is written and the phase marked complete, blessing an empty backfilled doc.
9. **[P1] Include `requires_verification`/`reads_target` phases in `_live_only`** — `backfill.py:111-117` — such phases can currently be backfilled from docs and skip their real gate.
10. **[P1] Enforce the worker cap when the DB is contended in `try_claim`** — `global_resource.py:55-72` — a `BEGIN IMMEDIATE` lock timeout hits the blanket `except: return True`, so under real concurrency the machine-wide cap fails open.
11. **[P1] Exclude secret-shaped files from the bundled engine copy** — `gui/build_app.sh:32-40` — `make_dmg.sh` redistributes the bundle, so a user's `config.json`/`gemini_api_key`/`.env` can ship inside the DMG.
12. **[P1] Move `seedWorkflowsIfMissing()`'s synchronous `proc.run()`/`waitUntilExit()` off the main actor** — `gui/Sources/.../OrchestratorStore.swift:657-666` — it spawns python and blocks the main thread on first launch, freezing the UI.
13. **[P1] Surface config-write failures in the GUI instead of swallowing them** — `OrchestratorStore.swift:791,1637,1656` — the UI toggles `@Published` state after a `try?` write, so on failure the GUI and `config.yaml` silently disagree.
14. **[P1] Replace the `URL(string: "http://127.0.0.1:11434/api/pull")!` force-unwrap** — `OrchestratorStore.swift:1064` — a latent crash in the in-app model pull path.
15. **[P1] Add `set -euo pipefail` to `shepherd.sh`** — `shepherd.sh:1-7` — the fleet loop runs with no error/undefined-var protection, so a failed `cd` silently keeps driving every project.
16. **[P1] Fix README's primary launch instructions** — `README.md` — `bash run-orchestrator.sh` and every `cd orchestrator-v2-source` step reference paths that don't exist in this repo layout.
17. **[P1] Add a Makefile or remove README's `make verify`/`make app`/`make dmg` references** — `README.md` — the documented canonical gate doesn't exist.
18. **[P1] Update README's workflow inventory ("9 built-ins")** — `README.md` — there are 14 workflow JSONs; `app_build_child`, `app_spec`, `brainstorm`, `full_max`, and `prototype` are undocumented.
19. **[P1] Correct README's `app_build` phase list** — `README.md` — the actual `app_build.json` has 21 phases, not the 11 listed.
20. **[P1] Fix README's stale test count ("191 tests")** — `README.md` — the suite has 390 tests.
21. **[P1] Fix fictional/unpullable model tags in the registry** — `local_models.json:20,35,50,65,80,154` — tags like `glm-5.2`, `qwen3-max`, `kimi-k2.6`, `deepseek-v4-pro` aren't pullable via `ollama pull`, so recommendations yield hard failures.

## P2 — Robustness, portability, and safety (22–61)

22. **[P2] Replace the hardcoded default root `/Users/pchordia/Documents/iOS-App-Factory`** — `config.yaml:21` — ship a repo-relative default like `./workspace`; `resolve_root` already supports relative roots.
23. **[P2] Remove hardcoded personal paths from the launch scripts** — `run.sh:21`, `shepherd.sh:8`, `install_launch_agent.sh:18`, `gui/run_gui.sh:13` — they only work on the original author's machine and ignore `ORCH_ROOT`.
24. **[P2] Remove hardcoded app names and batch config from `shepherd.sh`** — `shepherd.sh` — `PARENTS=(multi-app-exp7)` and the fixed "51-app" list make the script unrunnable elsewhere; parameterize them.
25. **[P2] Pass the claude prompt over stdin instead of argv in `run_claude`** — `orchestrator.py:686` — the full prompt (including any spliced secret) is visible to every local user via `ps aux`/`/proc/<pid>/cmdline`.
26. **[P2] Guard prompt-injection from fetched page text in `build_url_context`** — `urlfetch.py:355-387` — raw remote HTML is spliced into agent prompts as "authoritative"; delimit/neutralize it.
27. **[P2] Handle PID reuse in global slot reaping** — `global_resource.py:33-49` — `os.kill(pid,0)` treats a recycled PID as alive, and `PermissionError` → alive means another user's PID never frees the slot.
28. **[P2] Track the booted verification server's process group in `_verify_http`** — `verify.py:301-303` — the Popen'd server isn't registered with `procutil._LIVE_PGIDS`, so it can leak past the run.
29. **[P2] Make `persist_verify_result` cross-process safe** — `verify.py:449-457` — the unlocked load-append-replace of `verify_results.json` loses records under concurrent portfolio children.
30. **[P2] Write `.verify_server.log` outside the built project** — `verify.py:298` — the log pollutes the generated app tree that later gets committed/shipped.
31. **[P2] Validate `folder`/`file` phase fields against path traversal in `run_backfill`** — `backfill.py:158-166` — a `../` value from workflow config escapes the app dir.
32. **[P2] Serialize appends to `live_log()` and `emit()`'s log file** — `orchestrator.py:2189,187` — parallel worker threads use plain `open(..., "a")`, so long lines can interleave and corrupt `live_log.jsonl`/`orchestrator.log`.
33. **[P2] Bound `events.emit_event` line size or document the atomicity limit** — `events.py:90` — the per-line atomicity contract only holds below `PIPE_BUF`; longer lines can interleave in `events.jsonl`.
34. **[P2] Avoid blind `git add -A` in `commit_build_state`/`ensure_build_repo`** — `orchestrator.py:3057` — with a missing/edited `.gitignore` it can commit `DerivedData`, large artifacts, and known-scanned secrets.
35. **[P2] Make the codex/gemini probe caches tolerate an unwritable engine dir** — `orchestrator.py:545,564,775` — a read-only GUI install silently re-probes (spending tokens/latency) every run.
36. **[P2] Strip inline `#` comments after scalar values in `parse_min_yaml`** — `orchestrator.py:218` — `key: value  # note` currently keeps the comment in the value, contradicting the config header's claim.
37. **[P2] Cache or skip the full target-tree mtime walk in `_run_app_pipeline` (`_tsig`)** — `orchestrator.py:4806` — every `--watch` pass walks the entire audit target, which is expensive on large codebases.
38. **[P2] Add a `knowledge/general/` domain or change the `domain_for` fallback** — `knowledge.py:88` — general projects fall back to a nonexistent directory and get zero knowledge injection.
39. **[P2] Make `installed_models_cached` refresh single-flight** — `localmodels.py:100-111` — the lock is released between TTL check and refresh, so concurrent turns each pay the 10s `ollama list` subprocess.
40. **[P2] Guard or document POSIX-only process control** — `procutil.py:96-102,133`, `verify.py:324,333` — `os.killpg`/`start_new_session` make the engine Unix-only with no guard or note.
41. **[P2] Isolate `simulate_stream`'s lock from the real engine lock** — `simulate_stream.py:25,175-179` — the demo uses the engine dir's `.lock` and unconditionally deletes it in `finally`, clobbering a concurrent real run.
42. **[P2] Wrap `load_state`'s JSON parse in `simulate_stream`** — `simulate_stream.py:82-83` — a corrupt `agent_state.json` crashes the tool instead of degrading.
43. **[P2] Clean up the leftover `.tmp` file on failed atomic write in `docs._write`** — `docs.py:20-24` — a failed `os.replace` leaves `path.tmp` behind and the outer `except OSError: pass` hides it.
44. **[P2] Fix the `glm-5.2` license_url mismatch** — `local_models.json:20-25` — the entry points at the GLM-4-9B model page, so the surfaced license reference is wrong.
45. **[P2] Normalize missing `context_tokens` across the model registry** — `local_models.json:95,244,258,272` — several entries omit the field while others include it, so consumers get inconsistent data.
46. **[P2] Make `resolvePython()` consult PATH before falling back** — `OrchestratorStore.swift:2370-2374` — it hardcodes three locations and blindly returns `/usr/bin/python3`, which may not exist.
47. **[P2] Reconcile the Ollama default between `enabledAgents` and `BackgroundConfigLoader`** — `OrchestratorStore.swift:468-469` vs `292` — the first pre-refresh render shows Ollama enabled contrary to the engine default.
48. **[P2] Route silent `try?` writes (human messages, target paths, roster) through a visible error path** — `OrchestratorStore.swift:1154,1544,1983` — a failed write drops the user's input with no feedback.
49. **[P2] Handle `runInTerminal`/osascript launch failure** — `OrchestratorStore.swift:1329-1338` — a failed `ollama pull`/`serve` just does nothing with no error surface.
50. **[P2] Give the GUI a persistent error surface instead of the ⌘L-hidden `runLog`** — `OrchestratorStore.swift` — fork/reset/approval/create errors only land in a collapsed panel users miss.
51. **[P2] Report when `stopRun`/`stopProject` fails to kill or unlock** — `OrchestratorStore.swift:2274-2311,2464-2489` — a silently stuck lock leaves a lane permanently "running".
52. **[P2] Guard `readModels`/`readEnabledAgents` regexes against commented/partial YAML lines** — `OrchestratorStore.swift:290-314` — patterns can match inside comments and mis-read config state.
53. **[P2] Quote `$(queue_order_dirs)` and the `"$ROOT"/*/` globs in `shepherd.sh`** — `shepherd.sh:50` — unquoted expansion breaks on paths containing spaces.
54. **[P2] Explain the disabled Run button when no agent CLI is runnable** — `gui/Sources/.../AppShellView.swift:83-85,168` — the button is greyed out via `anyRunnable` with no tooltip or reason.
55. **[P2] Add phase_rules coverage (or documented absence) for non-app workflow phases** — `phase_rules.json` — 19 phases used by audit/research/productionize/library_mining/answer_question/iterate have no quality-playbook entries.
56. **[P2] Standardize top-level workflow schema fields (`budget`, `overrides`)** — `workflows/*.json` — present in only some workflows, so consumers can't rely on them.
57. **[P2] Standardize per-phase optional fields across workflows** — `workflows/*.json` — `reads_target`/`requires_verification`/`checkpoint`/`structurally_required`/`doc_sections`/`test_deliverable` are set on only ~110 of 130 phases with no documented defaults.
58. **[P2] Add a JSON-schema validator + test for `workflows/*.json`** — `tests/` — no test asserts workflow field consistency, so drift goes uncaught.
59. **[P2] Add dedicated test coverage for `knowledge.py`** — `tests/` — the retrieval module has no direct scoring/injection tests.
60. **[P2] Add dedicated tests for `roles.py` persona assignment determinism** — `tests/` — a documented guarantee that is not directly tested.
61. **[P2] Add a doctor/CLI-surface smoke test** — `tests/` — `--doctor`, `--doctor --json`, `--search-models` are documented entrypoints with no test.

## P3 — Cleanup, dead code, and enhancements (62–100)

62. **[P3] Add a `.github/workflows` CI pipeline** — the unittest gate runs only locally, so regressions aren't caught on push/PR.
63. **[P3] Add a LICENSE file** — repo root — no license despite README discussing commercial-use metadata for bundled models.
64. **[P3] Add a CONTRIBUTING.md** — repo root — no contributor guide documents the test gate or workflow-editing rules.
65. **[P3] Add `pyproject.toml` for packaging/metadata and tool config** — repo root — no packaging manifest or version declaration.
66. **[P3] Add linting config (ruff/flake8)** — repo root — no linter for a ~270KB `orchestrator.py` plus ~15 modules.
67. **[P3] Add type-checking setup (mypy/pyright)** — repo root — `schemas.py`/`workflows.py` carry structured contracts with no checker.
68. **[P3] Split `orchestrator.py` (~267KB) into submodules** — CLI, phases, build, and resume logic in one file hinders testability and review.
69. **[P3] Document or gate macOS-only assumptions** — `install_launch_agent.sh`, `README.md` — LaunchAgent/xcodebuild/`/opt/homebrew` assumptions carry no cross-platform note.
70. **[P3] Remove the dead legacy global-lock code (`LOCK_PATH`, `acquire_lock`, `release_lock`)** — `orchestrator.py:368,398` — never called now that locking is per-app.
71. **[P3] Fix `_apply_phase_routing` writing `_health` instead of `_agent_health`** — `orchestrator.py:3602` — `_health` is written and never read, so the "cooldowns survive across phases" comment is misleading.
72. **[P3] Remove or wire up the dead `rounds:` block in config.yaml** — `config.yaml:93-116` — every `Phase` carries its own `rounds`, so the config block is never consulted.
73. **[P3] Reconcile `models.gemini_api_key_file` references** — `config.yaml:50-52,80-81`, `orchestrator.py:619` — referenced in comments and code but not defined in the `models` block.
74. **[P3] Drop the nonexistent phase key `launch_readiness_review` from `_needs_vlabel`** — `orchestrator.py:3695` — no workflow defines that phase; the condition is dead.
75. **[P3] Align `run_local`'s default model with configuration** — `orchestrator.py:946` — it reads undefined `models.local_default` and is inconsistent with `models.ollama`.
76. **[P3] Make `ensure_signature` strip sign-offs for dynamic `local:` agents** — `orchestrator.py:1404` — it iterates only the four static ids, so a local model's "From …" tail is never removed.
77. **[P3] Skip `_maybe_materialize_portfolio_children` once the manifest exists** — `orchestrator.py:4954` — it currently re-runs at the top of every phase iteration.
78. **[P3] Update `COMMON_RULES` to name local/roster participants** — `orchestrator.py:1483` — agents are told to react to "Codex / Claude / Gemini" only, so `local:*` participants are never addressed.
79. **[P3] Move `_gemini_api_key`'s legacy in-repo key path out of the repo** — `orchestrator.py:622` — it relies solely on `.gitignore` to keep a real key out of commits.
80. **[P3] Make `_await_approval` non-blocking or shorten the 2h default** — `orchestrator.py:4257` — the wait holds a `project_parallel_workers` thread slot for its full duration.
81. **[P3] Use timezone-aware timestamps in `events.emit_event` and `live_log`** — `events.py:83`, `orchestrator.py:2189` — naive `datetime.now()` makes event ordering ambiguous across DST changes.
82. **[P3] Report unknown `owner_lane` values in `parse_tasks_blocks`/`parse_interface_blocks`** — `orchestrator.py:2214,2304` — an unknown lane silently shows every task to every worker.
83. **[P3] Report `depends_on` ids missing from the backlog in `find_task_cycles`** — `orchestrator.py:2259` — a task depending on a nonexistent id is a real planning error but is dropped silently.
84. **[P3] Handle unmatched quotes in `_coerce_scalar`** — `orchestrator.py:196` — `key: "unterminated` keeps the leading quote, silently corrupting the value.
85. **[P3] Ship commented per-phase routing examples in `model_routing.json`** — the file ships with empty `phases:{}`/`chains:{}`, so the documented capability is undiscoverable.
86. **[P3] Cap or stream `build_target_digest`/`build_portfolio_digest` traversal** — `orchestrator.py:2894,2942` — they walk the entire target before applying caps.
87. **[P3] Deep-copy `fallback`/`chains` in `load_routing_for_app`** — `modelrouting.py:147` — `dict(fleet)` shares nested dicts, risking mutation bleed between fleet and per-app views.
88. **[P3] Replace builtin `hash()` in `seed_demo.agent_message`** — `seed_demo.py:95` — per-process hash salting contradicts the "deterministic/idempotent" claim.
89. **[P3] Unify phase-key access across `backfill`/`completeness`/`docs`** — three different accessors for the same Phase objects invite drift.
90. **[P3] Guard `assign_personas` against empty pools** — `roles.py:154-155` — modulo by zero raises if a caller passes empty pools.
91. **[P3] Strengthen `_verify_shell`'s Python check beyond `compileall`** — `verify.py:213` — syntax-only checking reports projects with unresolved imports as "verified".
92. **[P3] Extract and unit-test the `is_portfolio_parent_prompt` heuristics** — `portfolio.py:56-90` — the hand-tuned string-match list is brittle and untested at the boundaries.
93. **[P3] Deduplicate `PHASES`/`save_state`/`now_str` between `seed_demo.py` and `simulate_stream.py`** — the two demo tools copy the phase list and state writer and drift independently.
94. **[P3] Implement the promised Command Palette (⌘K)** — `gui/DESIGN-NATIVE-PRO.md` §3/§8 — the doc commits to a ⌘K overlay dispatching `store.uiCommand`, but no binding exists.
95. **[P3] Add the Pause/Resume Engine toolbar control** — design §3 vs `AppShellView.swift:161-189` — the toolbar ships Run/Stop/New/Inspector only.
96. **[P3] Give MenuBar status marks accessibility labels** — `gui/Sources/.../OrchestratorApp.swift:156-159` — glyph/color-only rows have no VoiceOver text, conflicting with the design's "symbol + word, never color alone".
97. **[P3] Delete `OrchestratorStore`'s dead duplicate loader methods** — `OrchestratorStore.swift:1670-1968` — `loadProject`/`discoverApps`/etc. duplicate the `Background*` loaders and can drift.
98. **[P3] Unify `NewAppIntakeSheet.slugify` with `OrchestratorStore.slugify`** — `NewAppIntake.swift:55-59` — the intake version doesn't collapse consecutive hyphens, producing inconsistent folder names.
99. **[P3] Add tests for the GUI launch-queue FIFO/reorder logic and transcript head+tail reader** — `OrchestratorStore.swift:2236-2424,1861-1891` — index-math-heavy state machines with zero coverage.
100. **[P3] Broaden `detectCLIs`/`ollamaOnPath` PATH handling** — `OrchestratorStore.swift:343-357,1318-1323` — a Finder-launched app inherits a minimal PATH, so CLIs outside three hardcoded dirs read as "not found".

## Overflow (found but below the top-100 cut)

- [P3] Make the GUI ratchet/style guard (`ci_style_check.sh`) fail on any legacy hardcoded font size rather than freezing baselines.
- [P3] Add QuickLook preview for attached docs per design §3/§4.5.
- [P3] Replace `try! NSRegularExpression` in `TranscriptParser.swift:21` with lazy validation.
- [P3] Add a GUI-level snapshot/interaction test target for SwiftUI views.
- [P3] Add focused unit tests for `docs.py` renderer sections and completeness `stop_after_phase` fallback edges.
- [P3] Add tests for `run.sh`/`shepherd.sh`/`install_launch_agent.sh` behavior (git-secret refusal, queue ordering).
- [P3] Add a pytest config/dev-requirements or document unittest-only.
- [P3] Suppress the `RELEVANT KNOWLEDGE` header when all chunks truncate to empty in `knowledge.retrieve` (`knowledge.py:144-160`).
- [P3] Handle `verify_summary=None` defensively in `docs.render_project_management_backfill` (`docs.py:161`).
- [P3] Refresh README's file map ("9 built-ins", legacy `locks/` note).
- [P3] Remove the unused `rnd` parameter in `seed_demo.agent_message`/`coordinator_message`.
- [P3] Add a DB index for `_reap`'s `SELECT DISTINCT pid` scan (`global_resource.py:46`).
- [P3] Neutralize the hardcoded macOS Chrome `USER_AGENT` in `urlfetch.py:49-51`.
- [P3] Deduplicate the repeated knowledge-injection comment block in `build_context` (`orchestrator.py:1581-1587`).
- [P3] Add coverage for `docs.py` renderer and shell-script behaviors noted above.
- [P3] Refresh accessibility/tooltip affordances flagged in the design doc beyond the top-100 items.
