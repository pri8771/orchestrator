# Contributing

Thanks for helping improve the orchestrator. A few conventions keep the engine
predictable.

## Ground rules

- **Standard library only (engine).** The Python engine has zero third-party
  runtime dependencies, and `run.sh` / the GUI strip API-key env vars so nothing
  incurs extra API cost. Don't add a runtime dependency without a strong reason.
- **Never let a best-effort path take a run down.** Logging, event emission,
  caching, git steps, and doc rendering all catch their own errors and degrade —
  match that style when you touch them.
- **No secrets in the repo.** Secret-shaped filenames are gitignored and
  `run.sh` refuses to commit them; the build/DMG excludes them too. Keep it that
  way.

## The test gate

The engine suite is stdlib `unittest`. Run it before every push:

```bash
make test          # python3 -m unittest discover -s tests
make test-strict   # same, warnings promoted to errors (CI runs this)
```

`make verify` is the full local gate on macOS (test-strict + GUI build + GUI
tests + doctor). CI (`.github/workflows/ci.yml`) runs the Python suite on Linux
across 3.9/3.11/3.12 and builds the GUI on macOS.

Add or update tests for any behavior change. New modules should ship with a
`tests/test_<module>.py`.

## Editing workflows

Built-in workflows live in `workflows.py` and are seeded to `workflows/*.json`
on first run (`--seed`); on-disk JSON always wins so GUI edits persist. Every
workflow/phase carries a **uniform set of fields** — `tests/test_workflows_schema.py`
enforces the shape (`WF_FIELDS` for each workflow, `PHASE_FIELDS` for each of
its phases), referential integrity (a `build_phase` must be a real phase, no
duplicate phase keys), and that every phase key has a `phase_rules.json`
quality-playbook entry. If you add a phase, add its rules and keep the schema
test green.

## Style

- Match the surrounding code: comment density, naming, and idiom. Comments state
  constraints the code can't show — not what the next line does.
- Lint config lives in `pyproject.toml` (`ruff`, `mypy`). Both are lenient today
  (the engine is a large, untyped, stdlib monolith mid-decomposition); prefer not
  to introduce new lint regressions in code you touch.
- Keep atomic writes atomic (temp file + `os.replace`, per-writer temp names) and
  guard shared state / cross-process files with the existing locks.

## Platform notes

The engine runs on POSIX (macOS/Linux). The GUI, iOS build/verify
(`xcodebuild`), and the LaunchAgent installer are macOS-only. See the platform
note in `README.md`.
