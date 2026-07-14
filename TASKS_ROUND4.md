# Round 4 Backlog

> Historical record (audit round 4, fully closed). See `AUDIT_HISTORY.md` for the consolidated index of all audit rounds.

Fresh audit after the Round 3 fix pass (see `TASKS_ROUND3.md`, all 15 items
landed across commits `42159ad`/`392aace`). Same four-lane split, each agent
told to skip anything already fixed and to specifically stress-test Round
3's newest surface area — the `global_resource` per-claim token redesign,
the `_JSON_FENCE_SPAN_RE` fence-matching fix, `workflows.py`'s new
`_as_bool`/`_as_str_list` helpers, and the Command Palette's `Command.id`
change. 9 new findings, deduplicated below, plus re-confirmation of the 5
items Round 3 explicitly deferred.

## P2 — robustness (3)

1. **`Phase.verify` has no shape validation**, unlike every other field
   `_as_bool`/`_as_str_list` now guard — `workflows.py:95`
   (`self.verify = verify or None`), consumed at `orchestrator.py:3627-3642`.
   A malformed `"verify": "xcodebuild"` (bare string instead of a dict)
   loads without error, then `spec.get("repair_iterations", ...)` raises
   `AttributeError` outside any try/except at that call site — aborting the
   phase instead of the graceful "unverified" degradation
   `verify.run_verification`'s own docstring promises. Same bug class Round
   3 fixed for booleans/lists, missed for this one remaining structured
   field.
2. **`_JSON_FENCE_SPAN_RE` doesn't protect the "labeled fallback" JSON
   fence shape** — `schemas.py:236-237` vs the labeled-fallback fence
   `extract_structured_blocks` explicitly supports (`schemas.py:82-91,149-158`,
   e.g. `**task-json:**` + a bare ` ```json ` fence with no `-json`-suffixed
   info string). The regex only matches fences whose *own* info string ends
   in `-json`, so this shape's body still gets run through the entropy
   redaction fallback — the exact "corrupts a field value downstream
   parsing depends on" failure Round 3 closed for one fence shape, still
   reachable via this one. Verified empirically.
3. **`_JSON_FENCE_SPAN_RE` requires fence delimiters at column 0**, so an
   indented fence (e.g. nested inside a markdown list item, a plausible
   model output shape) isn't recognized and its body is redacted —
   `schemas.py:236-237` (`^```` anchored under `re.MULTILINE`). Verified
   empirically with a 2-space-indented fence.

## P3 — cleanup / hardening (6)

4. **`workflows._as_str_list` only guards against strings, not dicts** —
   `workflows.py:42-48` — `_as_str_list({"product": true})` returns
   `["product"]` (its keys) instead of being rejected, reintroducing the
   silent-misinterpretation risk Round 3 #3 closed for the string case, now
   via a dict shape.
5. **`workflows._as_bool` treats any unrecognized non-empty string as
   `True`** (e.g. `"off"`, `"0.0"`), unlike `modelrouting._as_bool`'s safer
   default-fallback design added in the same Round 3 pass — `workflows.py:34-39`.
   Low practical likelihood (canonical false-ish strings all handled
   correctly) but a real design inconsistency between two sibling functions
   fixed in the same commit.
6. **`global_resource`'s claim happens outside the `try/finally` that
   releases it** — `orchestrator.py:1288` (claim) vs `1312`/`1385`
   (try/finally). `threading.Thread(...).start()` at line 1311 runs between
   claim and the guarded region and can genuinely raise
   (`RuntimeError` under thread exhaustion, realistic under heavy parallel
   load) — if it does, the claimed token slot leaks until the 6-hour
   age-based reap.
7. **`global_resource` rowid-reuse is a latent double-release footgun in
   the token API itself** — `global_resource.py:81-84,151-172` — SQLite's
   default rowid reuse means a released row's rowid can be reassigned to a
   brand-new claim; a future second call site or a retry-path double-release
   bug could silently delete a different, newly-claimed slot. Not currently
   triggered (only one call site exists, correctly claim/release-paired),
   but worth a defensive guard given the whole point of the token redesign
   was to make this class of bug structurally impossible.
8. **`CHANGELOG.md` has no Round 3 entry** — jumps from Round 2 straight to
   nothing, so the global_resource token-release fix, the workflow
   bool-coercion fix, and the fence-span redaction fix — the most
   correctness/security-relevant fixes in the audit history — are
   undocumented there.
9. **CI never runs `mypy`** — no step in `.github/workflows/ci.yml`
   invokes it (only `test`/unittest and advisory `lint`/ruff exist) despite
   `make typecheck` and `[tool.mypy]` config existing — this is the exact
   gap Round 3 #8's regression slipped through; mypy is clean today only
   because someone ran it manually before committing.

## P3 — minor/cosmetic (2)

10. **Stale `orchestrator-v2-source` reference survives in a shell comment**
    missed by Round 3's doc-path cleanup — `gui/build_app.sh:36`
    (`# orchestrator-v2-source` trailing comment). No functional effect.
11. **`gui/README.md:123` test-count claim is stale** — says
    "37 XCTest cases" for `EngineLogicTests.swift`; actual count is 38.

## Informational (not tracked as action items)

> All four items below are CLOSED as of the post-Round-4 completeness pass
> (commits through `4c764b9`) — kept here for history, not because they're
> still open. See `AUDIT_HISTORY.md` for the full closure record.

- ~~`dependabot.yml` has no `pip` ecosystem entry~~ — closed (Round 4 Batch B,
  `.github/dependabot.yml` now has a `pip` ecosystem entry).
- ~~Command Palette's search field holds focus ... arrow-key navigation
  unverified~~ — resolved by design: `.onKeyPress` handlers are attached
  directly to the search field (not the `List`), which manually drives
  selection state — confirmed working as intended, not a defect.
- ~~`docs.render_project_management_backfill` still crashes on
  `verify_summary=None`~~ — closed, `docs.py` now guards with `(verify_summary
  or "").upper()...`.
- ~~`verify.persist_verify_result` timestamps with naive `datetime.now()`~~ —
  closed, now uses `.astimezone().isoformat(...)` matching `events.py`.
- ~~`orchestrator.py`'s dead `_installed_ollama_models` cfg key~~ — closed,
  `_installed_local_models(cfg)` now memoizes a real value onto it.

## Re-confirmed: Round 3's 5 deferred items — all still open, unchanged

> All 5 items below are now CLOSED — see `AUDIT_HISTORY.md` for the closure
> record (all five landed in Round 4 Batch A, commit `1188b28`).

- ~~`phase_rules.load_rules`/`knowledge._load_doc` mtime-cache staleness~~ —
  closed, both re-keyed on `(st_mtime_ns, st_size)`.
- ~~`localmodels.report()` bypasses the TTL/single-flight cache~~ — closed,
  now calls `installed_models_cached()`.
- ~~`global_resource._reap`'s dead-row cleanup rolled back~~ — closed, `_reap`
  now commits in its own transaction before the claim transaction.
- ~~`install_launch_agent.sh` has zero test coverage~~ — closed,
  `tests/test_install_launch_agent.py` added.
- ~~`tests/test_packaging.py`'s py-modules guard skips on Python 3.9/3.10~~ —
  closed in the post-Round-4 completeness pass via a narrowly-scoped,
  stdlib-only hand-rolled parser for pyproject.toml's `py-modules` array
  (no `tomli` dependency added, consistent with the stdlib-only rule).

## Verified clean (no issues found this round)

Test suite: 584 passing. mypy: clean, 21 source files. The `global_resource`
token threading (single call site, correctly paired), the fallback
health-key lane incorporation, `_agent_available`'s installed-model check
(all 4 call sites), the Command Palette `Command.id` fix (type-checks
correctly, no collision risk with today's 6 distinct actions), the
empty-shortcut placeholder, and `LSMinimumSystemVersion` 14.0 were all
independently re-audited and confirmed correct with no regressions.
