# Changelog

This project doesn't yet cut tagged releases; entries below track the
audit-driven fix passes against `pyproject.toml`'s `version = "2.0.0"`.

## Round 5 — completeness pass (post-`TASKS_ROUND4.md`)

Not a fresh audit — a targeted sweep closing every remaining known gap: the
original `TASKS.md` overflow list (finally fully cleared), a real GUI
data-loss bug it surfaced (the Routing Grid silently dropping hand-edited
per-role effort overrides on save), and four previously-deferred
architecture recommendations plus three previously-intentional non-decisions
that got finished rather than left open indefinitely. Highlights:

- Routing Grid roles round-trip fix (the real bug of this round) + a minimal
  editing UI; caught a third live instance of the actor-isolation
  `nonisolated` bug class while adding tests for it.
- Phase-transition summarization (`phase_summaries.json`, recent phases keep
  full transcript, older phases get a compact summary) and round-level crash
  resume (parses the on-disk transcript for the real last-complete round
  instead of trusting a lone counter).
- Adaptive quality-based escalation (bump effort/model only on repeated
  repair or quality-gate failure, revert immediately after) and a
  `write_tests` phase running real `xcodebuild test`, scoped to the deep
  pipelines only and purely observational by default.
- Real Ollama `"think"` effort wiring on the HTTP path (the CLI path stays
  honestly noop — no equivalent flag); opt-in, honestly-labeled cost
  estimation for `--postmortem`; a stdlib-only hand-rolled parser closing the
  Python 3.9/3.10 packaging-test gap without adding a dependency.
- `knowledge.retrieve`'s bare-header-on-empty-chunk bug, a `try!`→`try?`
  crash-risk fix in the GUI's transcript parser, QuickLook preview for
  attached docs, and several smaller overflow-list closures.

Suite grew from 682 to 779 tests; mypy and ruff stayed clean throughout. See
`AUDIT_HISTORY.md`'s Round 5 entry for the full record, including two items
left deliberately open (a CI platform-level outage unrelated to this repo's
code, and GUI design-refresh Tranche 3, which is net-new feature work rather
than a gap to close).

## Round 4 — `TASKS_ROUND4.md`

Fresh audit after the Round 3 pass, deliberately stress-testing Round 3's
newest surface area (the per-claim token redesign, the fence-span regex, the
new coercion helpers, the Command Palette `Command.id` change). 9 new findings
plus re-confirmation of the 5 items Round 3 deferred. Highlights:

- Shape validation for `Phase.verify` — a malformed non-dict `verify` spec now
  degrades gracefully instead of aborting the phase, closing the last
  structured field the Round 3 coercion guards missed.
- Extended the redaction fence-span protection to labeled-fallback and
  indented JSON fences, the two fence shapes still reachable by the
  body-corruption bug Round 3 closed for one shape.
- `global_resource` claim hardening: release on the claim→try/finally gap and
  a defensive guard against rowid-reuse double-release.
- CI: an enforcing mypy gate (the exact gap Round 3's typing regression
  slipped through), plus a Dependabot `pip` entry for the dev extras.
- Cleared the previously deferred items that were re-confirmed still open,
  and misc doc/comment rot fixes (stale path comment, stale test count).

See `TASKS_ROUND4.md` for the full itemized list and per-item status.

## Round 3 — `TASKS_ROUND3.md`

Fresh audit after the Round 2 pass; all 15 items landed across commits
`42159ad`/`392aace`. Highlights:

- Redesigned `global_resource` around per-claim tokens so a worker can only
  ever release the slot it claimed.
- String→boolean coercion guards in both `workflows.py` and
  `modelrouting.py`, so `"false"`-style strings in workflow/config JSON no
  longer silently read as truthy.
- Fixed `_JSON_FENCE_SPAN_RE` so entropy redaction no longer corrupts the
  body of matched JSON fences.
- Isolated fallback health keys per lane, and a local-model-pulled preflight
  before routing work to Ollama.
- GUI: Command Palette selection stability via stable `Command` ids;
  `LSMinimumSystemVersion` raised to 14.0 to match `Package.swift`.
- Packaging: `[project.optional-dependencies] dev = ["ruff", "mypy"]`.

See `TASKS_ROUND3.md` for the full itemized list and per-item status.

## Round 2 — `TASKS_ROUND2.md`

Fresh audit after the Round 1 pass, scrutinizing both old code and what Round 1
itself added (`miniyaml.py`, Command Palette, Pause/Resume, `surfaceError`).
~73 findings across four lanes (core engine, supporting modules, GUI,
infra/docs), split P1 (correctness/security) through P3 (cleanup). Highlights:

- Closed a DNS-rebinding TOCTOU in the Round 1 SSRF guard (`urlfetch.py`).
- Fixed `config.yaml` (not just `config.json`) shipping inside the built
  `.app`/DMG.
- Hardened `load_roles`, workflow fallback coverage, and several non-atomic
  writes flagged as a recurring pattern.
- GUI: Command Palette auto-focus, shared menu/palette command source,
  `surfaceError` regression coverage, and misc accessibility/doc fixes.
- Infra: CI hardening (matrix `fail-fast: false`, SPM build cache), issue/PR
  templates, `SECURITY.md`, this changelog, `.gitignore` coverage for Python
  packaging artifacts, and a Dependabot config for pinned Actions.

See `TASKS_ROUND2.md` for the full itemized list and per-item status.

## Round 1 — `TASKS.md`

Prioritized backlog from a full audit of the engine (`*.py`), the SwiftUI GUI
(`gui/`), shell scripts, workflow/config JSON, docs, and the test suite. All
100 items addressed across eight commits (P1 → P3); a handful were resolved
as "already correct" or consciously deferred, with rationale recorded in
`TASKS.md`. Highlights:

- **P1 (security/correctness):** SSRF blocking + redirect re-validation in
  `fetch_url` (`urlfetch.py`), command-injection fix in `verify.py`'s
  `_verify_http`/`_detect_start`, locking around `orchestrator.py`'s shared
  `state` dict for parallel build workers, and other concurrency/correctness
  fixes.
- **P2 (robustness/portability):** atomic writes, portable path handling
  (`~` expansion), and hardening across supporting modules.
- **P3 (cleanup):** GUI Command Palette, Pause/Resume engine control, shared
  `slugify`/`cliSearchDirs` helpers, `miniyaml.py`, and general test coverage
  growth (the engine suite grew from 390 to 476 tests, run in strict
  warnings-as-errors mode).
