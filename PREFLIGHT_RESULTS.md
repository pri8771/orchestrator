# PREFLIGHT_RESULTS.md — Orchestrator V2

_Local run on the owner's Mac from
`/Users/pchordia/Documents/core_apps/ios-app-factory`. Re-verified 2026-07-05
via `cd orchestrator-v2-source && python3 orchestrator.py --doctor --json`._

## Doctor check (2026-07-05)

| Tool | Status | Detail |
|------|--------|--------|
| python3 | ✅ | Python 3.14.4 (`/opt/homebrew/bin/python3`) |
| git | ✅ | 2.53.0 |
| swift | ✅ | Apple Swift 6.3.3 |
| xcodebuild | ✅ | Xcode 26.6 |
| xcrun / simctl | ✅ | available |
| codex CLI | ✅ | codex-cli 0.142.4 (`~/.local/bin/codex`) |
| claude CLI | ✅ | 2.1.197 Claude Code (`~/.local/bin/claude`) |
| gemini CLI | ✅ | 0.49.0 (`~/.local/bin/gemini`) |
| agy (Antigravity) | ✅ | 1.0.16 (`/opt/homebrew/bin/agy`) |
| ollama | ✅ binary | `/opt/homebrew/bin/ollama` — **server not running** at probe time; no registry model pulled (fine unless local models are enabled) |

`build_capable: true` in the JSON report. No pay-as-you-go API-key env vars
detected in the environment.

## Repo

- Working directory: `/Users/pchordia/Documents/core_apps/ios-app-factory` ✅
- `orchestrator-v2-master-spec.md` present ✅
- `orchestrator-v2-source/` present ✅ (orchestrator.py, workflows.py,
  verify.py, docs.py, schemas.py, completeness.py, localmodels.py, GUI,
  workflows/*.json, knowledge/**, tests/)
- Workspace: `/Users/pchordia/Documents/iOS-App-Factory` ✅ (doctor:
  `exists=True`; not a git repo, so
  `run.sh` skips its git steps)

## Verdict

**PASS.** This machine (macOS, Xcode + Swift + simulators present, all agent
CLIs installed, spec + source in the working repo) can build and run the
native V2 app and the engine test suite.

Note on agent CLIs: presence is confirmed here; login/session validity is
verified lazily at run time, not during preflight. A local-model run
additionally requires the Ollama server (`ollama serve`) and a pulled model.
