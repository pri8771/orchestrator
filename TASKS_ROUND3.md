# Round 3 Backlog

Fresh audit after the Round 2 fix pass (see `TASKS_ROUND2.md`, all 73 items
landed across commits `b958a46`..`d46e960`). Same four-lane split (core
engine / supporting modules / GUI / tests-docs-infra), each agent told to
skip anything already fixed in Round 1 or Round 2 and to specifically
scrutinize Round 2's newest code (the SSRF pinning, atomic-write patterns,
`global_resource` cap validation, `redact_secrets` fence-awareness, the new
mtime-keyed caches, `MenuCommandSpec.all`, Command Palette keyboard nav,
and the new shell-script/packaging tests). 15 findings, deduplicated below.

## P1 — correctness / security (1)

1. **`global_resource.release()` can free a different concurrent call's
   slot, silently defeating the machine-wide cap** — `global_resource.py:141-156`,
   called from `orchestrator.py:1275-1377` — claims are keyed only by
   `(pid, resource_class)` with no per-call token. The same process holding
   multiple concurrent claims of the same resource class (parallel build
   workers / parallel discussion-round agent calls, all sharing one pid) —
   `release()`'s `SELECT rowid ... WHERE pid=? AND resource_class=? LIMIT 1`
   deletes an arbitrary row for that pid, not necessarily the row the
   finishing call itself claimed. A still-in-flight turn's slot can be freed
   early while a third caller's new claim is admitted, oversubscribing the
   cap. Verified empirically. Not covered by `tests/test_global_resource.py`
   (only single-claim-per-pid scenarios are tested). Fix: issue a per-claim
   token (e.g. `rowid` returned from `try_claim`) and require `release()` to
   delete by that exact rowid.

## P2 — robustness (7)

2. **`Phase.__init__` boolean fields accept truthy strings and silently
   invert malformed config** — `workflows.py:74,77,83-85` — `bool(writes)`
   etc. means `"false"`/`"no"`/`"0"` (valid JSON strings) all evaluate to
   `True`. `portfolio._clean_app` already guards this exact pattern for its
   own `"build"` field with an explicit comment explaining why — apply the
   same guard here. Concrete impact: `build_phase = ... next(p for p in
   phases if p.writes)` can pick the wrong phase if `"writes": "false"` is
   present as a string.
3. **`Phase.roles`/`Phase.doc_sections` silently shatter a string value
   into single-character list entries** — `workflows.py:73,86` —
   `list(roles)` on a string produces one-character "ids" instead of
   raising `TypeError`. `_role_pool_for_phase`'s `by_id` lookup then always
   misses, silently falling back to the full role roster with no error
   surfaced — defeats the phase's role restriction.
4. **`modelrouting.load_routing` has the identical string-truthiness bug**
   — `modelrouting.py:85,88` — `"enabled": "false"` or
   `"cloud_to_local": "false"` in `model_routing.json` both silently read
   back as `True`. No error path exists to surface the mistake.
5. **`schemas.redact_secrets`'s fence-span detection breaks on JSON values
   that embed literal triple-backticks** — `schemas.py:225,266-278` —
   `_JSON_FENCE_SPAN_RE` is non-greedy and stops at the *first* subsequent
   ` ``` `, not the matching close fence. A finding/task JSON value quoting
   its own markdown code block (e.g. a `"snippet"` field) truncates the
   recognized span early; the rest of the real JSON body gets run through
   the entropy fallback and can be corrupted — the exact failure mode the
   Round 2 fix was meant to prevent, now via a different trigger. No test
   covers a fenced block whose body itself contains ` ``` `.
6. **Fallback-path circuit-breaker health keys collide across concurrent
   parallel-build lanes** — `orchestrator.py:1165` vs `3345,3464` — build
   workers are deliberately keyed per-lane (`_health_key = w["slug"]`) so
   concurrent threads never race on one shared health dict (per an existing
   comment). But `call_agent`'s fallback ladder overwrites that with
   `"fallback:%s:%s" % (agent, step)` — encodes only agent+step, not the
   calling lane. Two lanes falling back to the same agent/step concurrently
   race `record_failure`/`record_success` on one shared dict entry with no
   lock, undermining the per-lane isolation one call site away.
7. **`_agent_available` for local identities checks only that the Ollama
   server is up, not that the specific roster model is pulled** —
   `orchestrator.py:2762-2768` vs the "no agent runnable" preflight at
   `4497-4504` — a config with only unpulled local roster entries silently
   passes Round 2 item #18's preflight check and only fails later, deep in
   phase 1 — exactly the failure mode that preflight was added to catch.
8. **`make typecheck` currently fails** — `knowledge.py:33`, `phase_rules.py:27`
   — the new mtime-keyed caches (`_DOC_CACHE = {}`, `_CACHE = {}`, added in
   R2 Batch D) have no type annotation, tripping mypy's `var-annotated`
   check — the same regression class Round 2 item #32 already fixed once,
   reappearing in the same round. Neither `ci.yml`'s `lint` nor `test` job
   runs mypy, so this has no CI gate at all right now.

## P3 — cleanup / minor robustness (7)

9. **Command Palette's `Command.id` regenerates a fresh `UUID()` on every
   access**, defeating Round 2's own keyboard-navigation feature —
   `gui/Sources/OrchestratorGUI/ContentView.swift:342-372` — `commands`
   became a computed `var` in Round 2 (so the Pause/Resume title can react
   to `store.enginePaused`), but `Command` still does `let id = UUID()`, so
   every evaluation of `commands`/`filtered` manufactures new identities.
   `List(filtered, selection: $selection)` can never keep a stable
   selection across renders; `runSelectedOrFirst()`'s `list.first(where:
   $0.id == sel)` essentially always misses. Fix: `var id: UICommand {
   action }`, mirroring `MenuCommandSpec`'s own pattern one file away.
10. **Built `.app`'s `Info.plist` still declares `LSMinimumSystemVersion`
    13.0** despite the app hard-requiring macOS 14 — `gui/build_app.sh:96`
    vs the new preflight at `14-21` and `Package.swift`'s `.v14`. The
    preflight only protects the *builder's* machine; a macOS 13 user who
    receives the DMG has Launch Services happily launch an app that then
    hits unavailable API instead of being refused outright. Bump to 14.0.
11. **`phase_rules.load_rules`'s and `knowledge._load_doc`'s mtime-keyed
    caches can serve stale data across a same-second file replacement** —
    `phase_rules.py:44-50`, `knowledge.py:129-148` — `os.path.getmtime()`
    resolution can be identical before/after a same-second rewrite (e.g. a
    GUI save immediately followed by an engine read), serving stale content.
12. **`localmodels.report()` bypasses the TTL/single-flight cache** —
    `localmodels.py:222` — calls the raw, uncached `installed_models()`
    instead of `installed_models_cached()` (already used by
    `search_remote`), re-paying the full `ollama list` subprocess cost on
    every `--doctor`/GUI status-refresh call.
13. **`global_resource._reap`'s dead-row cleanup is rolled back whenever the
    claim it ran inside fails (cap full)** — `global_resource.py:62-83` —
    `_reap` runs inside the same transaction as the capacity check; a
    still-full cap triggers `conn.rollback()`, undoing the reap's deletes
    too. Under sustained contention, dead/stale rows are never actually
    purged — `worker_slots` can grow without bound.
14. **`gui/README.md` (and `make_dmg.sh`/`run_gui.sh`/`DESIGN-NATIVE-PRO.md`)
    still reference a nonexistent `orchestrator-v2-source/` directory** in
    copy-pasteable commands (`gui/README.md:130,136`) — same class of bug
    Round 1 item #16 fixed in the root README, missed in the GUI's own doc,
    including in text rewritten this very round.
15. **Round 2 item #70 (`[project.optional-dependencies]` for
    `ruff`/`mypy`) was dropped** — explicitly deferred by Batch C as
    "concurrently edited by Batch D," but Batch D never picked it up.
    `pyproject.toml` still has no dev-extras block; `pip install .[dev]`
    remains undocumented and unavailable.

## Noted, not tracked as action items (informational)

- `_probe_cache_path`'s model-probe cache write isn't atomic (plain
  `open(..., "w")`, no temp+`os.replace`), unlike every other write path in
  the codebase — low impact (self-healing via re-probe on the next 4h
  boundary; corrupt reads are swallowed and re-probed) but inconsistent.
  `orchestrator.py:525-528`.
- Command Palette's Pause/Resume row renders an empty shortcut cell instead
  of a placeholder — purely cosmetic. `ContentView.swift:361-362,423-426`.
- `CHANGELOG.md` still headers Round 2 as "(in progress)" though it's fully
  landed on this branch as of `d46e960`.
- `install_launch_agent.sh` has zero test coverage, unlike its siblings
  `run.sh`/`shepherd.sh` which got full suites this round — carried over
  from Round 1's overflow list, still open.
- `Makefile`'s `clean` target doesn't remove the packaging/tooling caches
  this round added to `.gitignore` (`.mypy_cache/`, `.ruff_cache/`,
  `*.egg-info/`, `dist/`).
- `tests/test_packaging.py`'s py-modules drift guard is skipped on Python
  3.9/3.10 (no `tomllib`), so the declared minimum-supported CI leg (3.9)
  gives no signal on it.
- `ci.yml`'s GUI SPM build-cache key hashes a `gui/Package.resolved` that
  doesn't exist (zero external dependencies today) — harmless now, but
  silently stale if a dependency is ever added without committing the
  resulting resolved file.
