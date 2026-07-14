# Security Policy

## Reporting a vulnerability

Please report security issues privately rather than opening a public issue —
email priyansh.chordia@gmail.com with a description and, if possible, steps
to reproduce. We'll acknowledge within a few days.

## Scope

This is a local, single-user orchestration engine: it runs AI CLIs
(Codex/Claude/Gemini/Ollama) on your machine and reads/writes files under the
workspace root and `.orchestrator` engine dir. The main risk surface is:

- **Untrusted content reaching the shell or the filesystem.** Agent output,
  fetched URLs, and prompt text are treated as untrusted input; anything that
  ends up in a subprocess command or a file path should be validated/escaped,
  not interpolated directly (`verify.py`'s command construction and
  `urlfetch.py`'s URL handling are the historical trouble spots — see
  `TASKS.md` for fixes already made here).
- **SSRF via fetched URLs.** `urlfetch.py` fetches http(s) URLs found in
  prompts/output; it denies loopback/link-local/private-range targets
  (including on redirect) and only allows http/https schemes. Report a bypass
  if you find one.
- **Secrets.** API keys and credential-shaped files are gitignored and
  excluded from the GUI's app bundle (`gui/build_app.sh`); `run.sh` refuses to
  commit them. Report it if a code path could leak one.

## Supported versions

Only the latest commit on the default branch is supported; this project
doesn't maintain older release branches.
