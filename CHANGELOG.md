# Changelog

This project doesn't yet cut tagged releases; entries below track the two
audit-driven fix passes against `pyproject.toml`'s `version = "2.0.0"`.

## Round 2 (in progress) — `TASKS_ROUND2.md`

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
