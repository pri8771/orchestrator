# Fable Preflight Amendment — Orchestrator V2

> **STATUS: MERGED into `orchestrator-v2-master-spec.md` (Claude, sign-off pass).** The binding content of this amendment — the Milestone 0 preflight/doctor check and the Milestone 1 "use the real `run_codex`/`run_claude`/`run_gemini` runners first, demo agent as dev-only fallback" rules — now lives directly in Section 28 of the master spec, so Fable gets it by reading the single authoritative document. This file is retained as the rationale/governance record. If the two ever disagree, the master spec wins. Claude also added one clarification during the merge: if a demo/fake agent is built, it must be registered in all five hardcoded structures (`AGENT_ORDER`, `DISPLAY`, `SIGNATURE`, `RUNNERS`, `COORDINATOR_PREFERENCE`) or the demo identity itself trips the same silent-drop bug.

This amendment governs `orchestrator-v2-master-spec.md` before the repo is handed to Fable. It does not replace the master spec. It clarifies Milestone 0 and Milestone 1 so Fable uses the existing real agent runners first and only falls back to a dev/demo path if Fable's environment cannot run the real CLIs.

## Why this amendment exists

The current orchestrator source already contains real runner implementations for Codex, Claude, and Gemini/Antigravity. Fable should not waste the build pass creating a fake-agent-first system if the real current runners can be reused.

The only reason to add a demo/fake agent is to validate the Mac app + engine vertical slice when Fable's build environment lacks Priyansh's logged-in CLI sessions. A demo adapter is a development fallback, not a product feature and not a replacement for the real agent path.

## Binding change to Section 28 / Milestone 0

Milestone 0 must include a preflight/doctor check before architecture work begins.

Fable must run or implement a doctor flow that checks:

```bash
which codex && codex --version
which claude && claude --version
which gemini && gemini --version
which agy && agy --version
which ollama && ollama --version
xcodebuild -version
xcrun simctl list devices available
git --version
python3 --version
```

If a CLI exists, Fable should also attempt a harmless smoke prompt where supported:

```bash
codex exec --sandbox read-only --skip-git-repo-check "Say READY"
claude -p "Say READY"
gemini -p "Say READY"
agy -p "Say READY"
```

The output of this check must be written to:

```text
PREFLIGHT_RESULTS.md
```

`PREFLIGHT_RESULTS.md` must state which tools were available, which were missing, which were installed but not logged in, and which smoke prompts succeeded.

## Binding change to Section 28 / Milestone 1

Milestone 1 should use the existing real cloud-agent runners first:

- Codex CLI via the current `run_codex` path.
- Claude Code CLI via the current `run_claude` path.
- Gemini/Antigravity via the current `run_gemini` fallback chain.

Fable must inspect the committed current source before rewriting these paths. In the current snapshot these are in:

```text
./orchestrator.py
  run_codex(...)
  run_claude(...)
  run_gemini(...)
  RUNNERS = {"codex": run_codex, "claude": run_claude, "gemini": run_gemini}
```

Do not replace these with a fake implementation. Preserve and wire the real runner path into the GUI/engine vertical slice.

## Dev/demo fallback rule

A dev/demo agent may be added only if needed to validate the vertical slice in an environment where no real AI CLI can run.

If added, it must follow these rules:

1. It is clearly named `demo` or `fake_agent`.
2. It is disabled by default in normal user runs.
3. It is available only in development/test mode or when all real agents fail preflight.
4. It writes deterministic canned responses only for verifying GUI/engine plumbing.
5. It must not be counted as a real supported provider in the product UI.
6. It must not replace or delay Codex/Claude/Gemini integration.
7. It must be documented in `KNOWN_LIMITATIONS.md` if used to validate Milestone 1.

The purpose of a demo adapter is only this:

```text
Verify that Orchestrator.app can create a project, launch the engine, stream phase output, update state, show transcripts, and reach a final-review-like result even when Fable's build environment cannot access Priyansh's logged-in cloud CLIs.
```

If the real CLIs are available, Fable should not build or use the demo adapter for Milestone 1.

## Preferred Milestone 1 behavior

Preferred path:

1. Run preflight.
2. Use the existing real Codex/Claude/Gemini runners.
3. Create a project from the native app.
4. Run a real workflow through the existing prompt builders.
5. Show live transcript/phase state in the GUI.
6. Show the real verification result, even if it is `UNVERIFIED` because Xcode or a simulator is missing.

Fallback path only if real CLIs are unavailable:

1. Run preflight and record why real agents are unavailable.
2. Add a dev-only deterministic demo agent.
3. Validate GUI/engine flow with the demo agent.
4. Keep real runner integration intact and mark real-agent execution as blocked by environment, not unimplemented.

## Sign-off position requested from Claude

Claude should review this amendment and answer:

1. Do you agree that Fable should use the existing real runners first?
2. Do you agree the demo/fake agent should be a dev-only fallback, not a core milestone?
3. Do you see any dependency violation in using real runners for Milestone 1 before agent-identity normalization lands in Milestone 2?
4. Should this amendment be merged into `orchestrator-v2-master-spec.md`, or is it acceptable as a companion governance file for the Fable handoff?

## ChatGPT sign-off

ChatGPT signs off on Claude's vertical-slice-first reorder with this amendment.

The corrected instruction to Fable is:

> Use the current real orchestrator runners first. Run a preflight to check Codex, Claude, Gemini/Antigravity, Ollama, Xcode, and Git. Only add a dev-only demo agent if the build environment lacks real CLI access, and never let that replace the real-agent path.
