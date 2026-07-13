# Round 4 Backlog

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

- `dependabot.yml` has no `pip` ecosystem entry for the new
  `[project.optional-dependencies] dev = ["ruff", "mypy"]` surface added in
  Round 3 — only `github-actions` is watched.
- Command Palette's search field holds focus for the whole time the palette
  is open with no `.onKeyPress` wired to the `List`; arrow-key navigation
  reaching the list (vs. the text field consuming it) is unverified without
  a Swift runtime — flagged as a candidate for a manual click-test, not a
  confirmed defect.
- `docs.render_project_management_backfill` still crashes on
  `verify_summary=None` (`docs.py:182`, missing the `or ""` guard its
  siblings in the same file have) — currently unreachable from the one
  in-scope caller and wrapped in a broad `except Exception`, so latent
  rather than active.
- `verify.persist_verify_result` timestamps with naive `datetime.now()`
  (`verify.py:551`) instead of the tz-aware pattern `events.py` already
  uses — minor ordering ambiguity across a DST transition when correlated
  against `events.jsonl`.
- `orchestrator.py:2668,2687` read a `cfg["_installed_ollama_models"]` key
  that's never set anywhere — always falls through to the correct fallback
  branch, just via dead/vestigial code, pre-existing and not introduced
  this round.

## Re-confirmed: Round 3's 5 deferred items — all still open, unchanged

- `phase_rules.load_rules`/`knowledge._load_doc` mtime-cache staleness
  across same-second file replacement — still present.
- `localmodels.report()` bypasses the TTL/single-flight cache — still
  present (`localmodels.py:222`).
- `global_resource._reap`'s dead-row cleanup still rolled back whenever the
  claim it ran inside fails the cap check — still present
  (`global_resource.py:62-79`).
- `install_launch_agent.sh` has zero test coverage — still true.
- `tests/test_packaging.py`'s py-modules guard still skips on Python
  3.9/3.10 (no `tomllib`) — still a genuine stdlib limitation, not closed;
  closing it would require a `tomli` backport dependency, in tension with
  the project's stdlib-only rule.

## Verified clean (no issues found this round)

Test suite: 584 passing. mypy: clean, 21 source files. The `global_resource`
token threading (single call site, correctly paired), the fallback
health-key lane incorporation, `_agent_available`'s installed-model check
(all 4 call sites), the Command Palette `Command.id` fix (type-checks
correctly, no collision risk with today's 6 distinct actions), the
empty-shortcut placeholder, and `LSMinimumSystemVersion` 14.0 were all
independently re-audited and confirmed correct with no regressions.
