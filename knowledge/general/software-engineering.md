<!-- keywords: software engineering, clean code, naming, functions, modules, error handling, exceptions, logging, testing, unit tests, integration tests, edge cases, input validation, boundary conditions, idempotency, concurrency, race condition, locking, atomic write, temp file rename, configuration, environment variables, secrets, dependency management, versioning, semantic versioning, cli design, argument parsing, exit codes, portability, cross-platform, refactoring, code review, documentation, readme, maintainability, defensive programming, fail loud, fail safe, observability -->

# General Software Engineering — Build-Agent Cheatsheet

Language-agnostic rules for any project that isn't specifically iOS, web, or backend (CLIs, scripts, data tools, libraries, desktop utilities). Bias toward correctness, clarity, and things that bite in production.

## Structure & naming

- **One responsibility per function/module.** If you can't name it without "and", split it. Names describe *what/why*, not *how*; a good name removes the need for a comment.
- **Make the common case obvious and the dangerous case loud.** Public APIs should be hard to misuse: validate inputs at the boundary, not deep inside.
- **Prefer pure functions** (inputs → output, no hidden state) for logic you want to test; push I/O and side effects to the edges.

## Error handling

- **Never swallow an error silently.** Either handle it, log it with context, or propagate it. A bare `except: pass` / empty catch is a future 3am debugging session.
- **Fail loud on programmer errors** (bad config, missing required arg) and **fail safe on expected runtime errors** (network blip, missing optional file → degrade).
- **Include actionable context** in messages: what was attempted, with which inputs, and what to do next — not just "error".
- **Clean up on the failure path.** Temp files, locks, sockets, and subprocesses must be released even when the happy path throws (`finally`/`defer`/context manager).

## Durability & concurrency

- **Atomic writes:** write to a temp file then rename over the target, so a crash mid-write never leaves a half-written file. Use a **per-writer temp name** so concurrent writers don't clobber one shared `.tmp`.
- **Guard shared mutable state** with a lock; a read-modify-write across threads/processes without synchronization loses updates. For cross-process, use a file lock (`flock`) or a real DB, not just an in-memory lock.
- **Make operations idempotent** where you can — re-running a step should be safe. This is what makes retries and resumes possible.

## CLI & config

- **Exit codes matter:** `0` success, non-zero failure. Scripts and CI branch on them.
- **Config precedence, documented:** explicit flag > environment variable > config file > built-in default. Never hardcode a user-specific absolute path as the default — derive from `$HOME`, the tool's own location, or an env override.
- **Keep secrets out of argv** (visible in `ps`/`/proc`) and out of the repo; read them from a file or env var, and refuse to commit secret-shaped filenames.

## Portability

- Don't assume an OS, shell, path separator, or that a tool is on `PATH`. Probe with `command -v` / `shutil.which`; degrade with a clear message when a dependency is absent rather than crashing.
- Prefer relative or configurable paths; expand `~` explicitly if you accept it.

## Testing

- **Test behavior, not implementation.** A test that breaks on every refactor is a liability.
- **Cover the edges:** empty input, `None`/null, huge input, unicode, concurrent access, the failure path, and the boundary values (0, 1, max, off-by-one).
- **One assertion of intent per test**, named for the scenario. A failing test name should tell you what broke without reading the body.
- Make tests deterministic: no reliance on wall-clock time, network, or ordering of a hash set.

## Before you call it done

- Re-read the diff as a reviewer: would a stranger understand *why*, not just *what*?
- Run the thing end-to-end, not just the unit tests — exercise the real entry point once.
- Check what happens on the unhappy path: kill it mid-run, feed it garbage, remove a file it expects.
