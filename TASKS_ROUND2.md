# Round 2 Backlog

Fresh audit after the Round 1 fix pass (see `TASKS.md`). Same four-lane split
(core engine / supporting modules / GUI / tests-docs-infra), each agent told
to skip anything already fixed in Round 1 and to specifically scrutinize the
new code from that pass (`miniyaml.py`, Command Palette, Pause/Resume,
`cliSearchDirs`, `surfaceError`). ~64 findings, deduplicated below.

## P1 — correctness / security (6)

1. **SSRF DNS-rebinding TOCTOU** — `urlfetch.py:201-218,276` — `_host_is_safe` resolves the host once via `getaddrinfo`, then `urlopen` re-resolves independently on connect. A host with a short-TTL DNS record can answer a safe IP for the check and a private/metadata IP moments later, bypassing the whole SSRF guard added in Round 1.
2. **`load_roles` returns mutable built-in defaults by reference** — `roles.py:93` — when `roles.json` is absent/invalid, `DEFAULT_PERSONALITIES`/`DEFAULT_ROLES` are returned unc opied; any in-place mutation by a caller corrupts the built-ins for the rest of the process, including concurrent projects.
3. **`config.yaml` (not just `config.json`) ships inside the DMG** — `gui/build_app.sh:32-46` — the exclusion list only names `config.json`; the repo's real config file is `config.yaml`, so every built `.app`/DMG currently bundles the machine's live config — including any custom `root:` or model keys — despite the "exclude secret-shaped files" comment from Round 1.
4. **7 of 14 workflows have no code-level fallback** — `workflows.py:585-586` vs `workflows/{app_build_child,brainstorm,full_max,iterate,library_mining,prototype,vslice}.json` — `_BUILTINS` only defines 7 names; if any of the other 7 JSON files is deleted or fails to parse, `load_workflow()` silently substitutes `app_build`, turning e.g. an `iterate` run into a full rebuild pipeline with no warning.
5. **`resolve_root` docstring contradicts the shipped config** — `orchestrator.py:224-226` claims the default is `./workspace`, "never a hardcoded absolute path," but `config.yaml:22` ships `root: "~/Documents/iOS-App-Factory"` — a personal-looking path. Round 1's portability fix (`~` expansion) is real, but the docstring oversells it.
6. **Command Palette doesn't auto-focus its search field** — `gui/Sources/OrchestratorGUI/ContentView.swift:335-393` — `CommandPaletteView` has no `@FocusState`, so ⌘K opens a sheet that needs a mouse click before typing — defeats the "type a command" affordance for keyboard users. (Borderline P1/P2 — flagged P1 since it breaks the primary interaction of a feature shipped this session.)

## P2 — robustness (30)

**Non-atomic / racy writes (recurring pattern Round 1 fixed in some files, missed in others):**
7. Backfilled phase file write isn't atomic — `backfill.py:181-186` (crash mid-write silently "completes" a truncated doc)
8. `seed_demo.save_state` isn't atomic — `seed_demo.py:186-189` (unlike the already-fixed `simulate_stream.save_state`)
9. `portfolio.materialize_children`'s writes + folder-claim check are racy — `portfolio.py:236-247,362-426` (TOCTOU between two concurrent orchestrator processes expanding the same manifest)
10. URL fetch cache file write isn't atomic — `urlfetch.py:314-331` (a killed process can leave a corrupt cache `.md`)

**Other robustness gaps:**
11. Fixed verification port under concurrent builds — `verify.py:315,327-328` — no free-port allocation; two parallel HTTP verifications collide on 8000/3000.
12. Node auto-detect checks `node` but runs `npm` — `verify.py:230-233` — a node install without `npm` on PATH fails misleadingly instead of skipping.
13. `cache_filename` truncates to 80 chars with no disambiguation — `urlfetch.py:306-311` — two distinct long URLs with the same 80-char slug prefix silently overwrite each other's cache.
14. `owner_lane` not validated before use as a Jira label — `docs.py:83,96` — a lane name with whitespace produces an invalid label for Jira import.
15. `agent_role_overrides` bypasses the phase's `phase_role_ids` restriction — `roles.py:137-163` — an override can hand an agent a role the phase explicitly excluded.
16. Half-open probe (`resilience.due_for_probe`) is defined/tested but never called — `orchestrator.py` — a flapping agent gets no bounded probe once `retry_after` elapses; only `in_cooldown` gates it.
17. `redact_secrets`'s entropy fallback can corrupt legitimate structured content — `schemas.py:213,241-249` — a long hash/base64/localization-key string can get replaced with `[REDACTED:high_entropy]`, corrupting the JSON block it's embedded in.
18. No load-time check that at least one agent is runnable — `orchestrator.py`/`config.yaml:76-93` — all-disabled/no-ollama-model configs fail confusingly deep in phase 1 instead of upfront.
19. `runtime.approval_timeout_seconds` (added Round 1) undocumented in `config.yaml` — the only runtime knob not mentioned in the file everyone else is documented in.
20. `local_active_limit`/roster trimming doesn't reconcile with `model_routing.json` overrides — `orchestrator.py:2645-2657` — a routing override referencing a dropped roster model degrades silently.
21. `try_claim` doesn't validate `cap` — `global_resource.py:71-83` — `cap=0`/negative silently fails every claim with no diagnostic.
22. Zero-cap/misconfig aside, `global_resource.py` cap validation is otherwise unguarded (see #21) — grouped for fix efficiency.
23. `cliSearchDirs()` doesn't dedupe/filter empty PATH entries — `gui/Sources/OrchestratorGUI/OrchestratorStore.swift:348-356` — empty PATH components resolve to the app's own cwd; duplicate dirs waste redundant stats on every `detectCLIs()` call.
24. `enginePaused` isn't persisted — `gui/Sources/OrchestratorGUI/OrchestratorStore.swift:490` — resets to `false` on relaunch with no on-disk record or user-visible indication anything changed.
25. Command Palette omits the new Pause/Resume Engine action — `gui/Sources/OrchestratorGUI/ContentView.swift:347-353` — already out of sync with the toolbar button added in the same session.
26. Command Palette rows aren't keyboard-navigable (Return/↑↓) — `gui/Sources/OrchestratorGUI/ContentView.swift:378-388` — plain buttons in a non-selectable List, so the palette is mouse-only.
27. No tests for `cliSearchDirs()`/`detectCLIs()` dedup/precedence — new Round 1 helper, zero coverage.
28. No test for `toggleEnginePaused()`/`advanceQueueIfIdle()` pause semantics — the documented "queued won't launch, running work continues" contract can regress silently.
29. `run.sh`'s secret-refusal and commit/push logic untested — `run.sh:65-74` — a regression could silently start committing key-shaped files.
30. `shepherd.sh`'s queue-order/retry-cap logic untested — carried over from Round 1's overflow list, still open.
31. README's file map missing `knowledge/general/` — `README.md:335` — added in Round 1, tree not updated.
32. Two real mypy errors under the existing lenient config — `events.py:40`, `verify.py:38` — `Module`-typed name reassigned to `None`; trivial `Optional[ModuleType]` fix.
33. `ruff`/`mypy` have no Makefile target — CONTRIBUTING.md references them, no `make lint`/`make typecheck` exists.
34. No `--doctor`/`--doctor --json` smoke step in CI — only exercised indirectly through mocked tests.
35. Makefile's CI description comment is stale — `Makefile:45` — says CI runs `make test-strict doctor`; `ci.yml` actually runs raw `unittest discover` and never `--doctor`.
36. `seed_demo`/`simulate_stream` missing from `pyproject.toml`'s `py-modules` — every other root module is listed except these two.

## P3 — cleanup / enhancement (~28, grouped)

**Engine:**
37. Add `phase_rules.json` schema validation (mirror `test_workflows_schema.py`) — a bad GUI edit (e.g. `rules` as a string) silently mis-renders.
38. Cache `load_rules` per run instead of re-reading from disk twice per phase.
39. Add a referential-integrity test tying workflow phase keys to `phase_rules.json` coverage over time (not just once).
40. Normalize `status`/`source` fields in finding/task blocks at ingestion, not just severity.
41. Note Windows-incompatible `os.path`/tilde assumptions in `resolve_root`/`load_config` (same POSIX-only theme as `procutil`).
42. Add `tests/test_miniyaml.py` round-trip coverage for every scalar shape actually in `config.yaml` (a basic version exists; extend it).
43. `events.KINDS` vocabulary isn't enforced — a typo'd event kind is silently written and invisible to any UI filtering on `KINDS`.
44. Precompute `by_role_id` once per run in `roles.assign_personas` instead of once per phase call.
45. `load_routing_for_app` silently no-ops when a per-app routing file has empty `phases` but legitimate `fallback`/`enabled` overrides — log instead of discarding silently.
46. `Workflow.overrides`/`budget` are shallow-copied, unlike the Round-1-fixed `modelrouting` deep-copy — harmless today (flat scalars) but inconsistent.
47. `local_models.json` registry entries aren't type-validated beyond `id` presence.
48. `portfolio.slugify`/`_unique_slug` has no length cap — can produce a filesystem-rejected 255+-byte folder name.
49. `modelrouting.json` per-phase `timeout` isn't cross-checked against the agent's own hard timeout.
50. `simulate_stream.py`'s lock file isn't per-`--app`, so two concurrent demo runs delete each other's lock.
51. `extract_urls` dedupes only on exact string match — near-duplicate URLs (trailing slash, `?utm_source=`) can crowd out a genuinely distinct link within the 5-URL cap.
52. `knowledge.retrieve` re-reads/re-scores every file from disk on every call with no mtime-keyed cache — fine today, won't scale.

**GUI:**
53. `surfaceError()` has no regression test despite ~8 call sites depending on its exact behavior.
54. GUI README's opening description still describes the retired three-pane classic browser.
55. DESIGN-NATIVE-PRO.md's ⌘K spec (fuzzy project jump, structured-argument verbs) is much broader than the 5-command palette shipped — either trim the doc or track the gap explicitly.
56. Command Palette's empty state has no "what would Return do" affordance beyond "No matching commands."
57. Pause/Resume button has no explicit `.accessibilityValue` beyond `.help()`.
58. Command titles/shortcuts are hand-duplicated between the menu bar and the palette — extract to one shared source.
59. `writeConfig`'s `setAgentEnabled` silently no-ops if the config key isn't found at all, unlike its sibling read/write-failure paths.
60. `ci_style_check.sh`'s color/font ratchet doesn't scan `Tests/`.
61. `build_app.sh`'s exclusion filter is name-only (not path-based) — a same-named file in a future subdirectory would also be silently dropped.
62. No macOS-version preflight before `swift build` — `Package.swift` requires `.v14`; running on macOS 13 fails with a generic compiler error instead of a clear message.

**Infra/docs:**
63. `.gitignore` doesn't cover packaging artifacts (`*.egg-info/`, `dist/`, `.mypy_cache/`, `.ruff_cache/`, `.pytest_cache/`).
64. No CI cache for the GUI's Swift build (`.build`/SPM) — full cold compile every push.
65. CI test matrix lacks `fail-fast: false` — one Python version failing cancels the other two, hiding whether it's version-specific.
66. No CI status badge in README.
67. No PR/issue templates.
68. No SECURITY.md (notable given this cycle fixed SSRF + command injection).
69. No coverage reporting wired into `make test-strict`/CI.
70. No `[project.optional-dependencies]` for `ruff`/`mypy` (`pip install .[dev]`).
71. No CHANGELOG.md despite `pyproject.toml` declaring `version = "2.0.0"`.
72. No Dependabot config for the three pinned GitHub Actions.
73. CONTRIBUTING.md doesn't name the actual `PHASE_FIELDS`/`WF_FIELDS` constants it references.
