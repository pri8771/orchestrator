# Enroll board: adopt an external codebase (8 cards)

Status: PLAN, approved by Priyansh 2026-07-21. Author: Claude (planning);
implementer: Codex. Follow the delivery rhythm, test-adequacy-trap check,
and git safety notes in `CODEX_HANDOFF.md` §"The delivery rhythm…" through
§"The git safety notes" — they apply verbatim to every card here.

## What "enroll" is

A new intake path: the user points the orchestrator at an EXISTING codebase
they did not build here. The system adopts it as a first-class project:
understands it, checks it against `knowledge/ios/` rules (reporting *why*
anything is non-compliant), reads their docs if any, and rebuilds docs in
our house format — **without ever writing a single byte into their tree**.
It ends at a human checkpoint; only after approval can build-type workflows
(iterate/sprint) run, and those run on a clone, never the original.

## Hard invariants (check every card against these)

1. **The enrolled origin directory is read-only forever.** Not "we try not
   to write" — structurally read-only, same mechanism as the audit target
   (see `workflows.py:779`+ `read_target_path` and the comment about audit
   targets never resolving inside app_build). Their docs are never edited,
   moved, or deleted. Everything we produce lives in OUR project dir.
2. **No invention in rebuilt docs.** Every claim in a rebuilt doc carries a
   provenance tag (card E3). A claim with no tag is a lint violation. When
   the model is not sure, it must write `[UNVERIFIED]`, not guess.
3. **Enroll ends at a gate.** The workflow's last phase is a report for a
   human. Nothing build-shaped runs until the user promotes the project.

## Existing machinery to reuse (verified in code, do not rebuild)

- Read-only external target: `audit` workflow (`target=audit`),
  `target_path.txt` / `target:` line, `wflib.read_target_path`.
- Project contract & discovery: `<root>/<app>/initial_prompt/initial_prompt.md`
  (+ optional `workflow.txt`), see `discover_apps` and the minting helpers
  around `orchestrator.py:9971`.
- Docs in house format: `documentation` workflow (`doc_gap_fill`,
  `doc_coherence`), `docs.py` slot system (`render_handoff_blueprint`,
  `recompute_gap_report`, SUBSCRIBE artifact→slot ingestion,
  `sections/documentation/doc_map.json` ownership).
- iOS rules: `knowledge/ios/*.md`, injected automatically per phase by
  `knowledge.py` — the compliance check *reads* these, it does not copy them.
- Promotion into build flows: `--promote` / `--continue-with` (rewrite
  `workflow.txt`, selectively clear phases — `orchestrator.py:11823`+).
- Live status: `agent_state.json`, `events.jsonl`, GUI `BuildActivityLog`.

## Cards (implement in this order)

### E1. `--enroll <dir>` intake command
CLI: `--enroll /path/to/repo [--name slug]`. Validates the dir exists and is
readable; warns (does not fail) if not a git repo. Scaffolds
`<root>/<slug>/` with:
- `target_path.txt` → absolute path to their repo
- `workflow.txt` → `enroll`
- `initial_prompt/initial_prompt.md` — auto-drafted from OBSERVED facts
  only: top-level file inventory, README first ~50 lines quoted (marked as
  quoted), detected project type (xcodeproj/Package.swift/etc.), LOC counts.
  No summarization-by-guess; if there's no README, say so.
Tests: scaffold shape; slug collision refuses (no silent `-2` suffixing —
that already confused a real user once, see streak/streak-2); a dir that is
itself inside `<root>` is rejected (would self-enroll).

### E2. `enroll` workflow definition + engine target plumbing
New `workflows/enroll.json`: `recon_understand → docs_inventory →
compliance_check → doc_rebuild → enroll_report`, `target="enroll"`.
Engine: `target=="enroll"` gets the exact same read-only target treatment as
`target=="audit"` (same guard: missing target_path is a hard, recorded
error — mirror the audit branch at `orchestrator.py:11034`). iOS knowledge
injection must fire for these phases (project type detected in E1 decides
domain). Tests: enroll app with no target_path errors cleanly; phases see
`tctx.target_path`; a write attempt into the target dir path from any
enroll phase is the sabotage test — prove the plumbing never passes a
writable target.

### E3. Provenance contract + lint
Define exactly four tags, spelled exactly:
`[VERIFIED: <repo-relative-path>]`, `[FROM-THEIR-DOCS: <file>]`,
`[UNVERIFIED]`, `[RESEARCH: <source-url-or-name>]`.
The doc_rebuild phase prompt requires a tag on every factual claim
(per-paragraph granularity is acceptable; per-sentence not required). Add a
lint pass (natural home: alongside designlint or a new `docslint`) that
scans rebuilt docs and reports untagged paragraphs as violations feeding
the gap report — untagged ≠ delete, untagged = flagged for the human.
Tests: tagged doc passes; untagged paragraph flagged; tag with a path that
doesn't exist in the target repo flagged (fabricated citation detector —
this is the card's real point, sabotage-validate it).

### E4. compliance_check phase prompt + artifact
Prompt: audit the target against each `knowledge/ios/*.md` rule area;
per finding emit rule, verdict (compliant / non-compliant / not-applicable
/ cannot-determine), evidence file path(s), and *why*. "cannot-determine"
is a first-class verdict — never coerced to a pass or fail. Output is a
typed `compliance_report` artifact on the bus (SUBSCRIBE-ingestible into a
doc slot). Tests: artifact schema; a finding without evidence paths is
rejected by the phase's quality gate.

### E5. Writable clone for post-enroll builds
On promote (not during enroll), clone the target repo into
`<app>/app_build/` on branch `enroll/<slug>`. The clone step: `git clone
--no-hardlinks <target> app_build` then branch; if the target is not a git
repo, `rsync -a` copy + `git init` + initial commit labeled "enrolled
snapshot of <path> at <sha-or-mtime>". Invariant 1 test: after a full
promote+iterate cycle on a fixture repo, the fixture's mtime/sha is
untouched (assert on content hash, not just mtime).

### E6. Enroll gate + promotion path
`enroll_report` is terminal: `agent_state.json` ends `status:
"enrolled_awaiting_approval"` (new status, additive — check GUI status
switch handles unknown statuses gracefully first). Promotion reuses
`--promote` to rewrite `workflow.txt` to `iterate` (default) and triggers
E5's clone. GUI shows the report + an "Approve & promote" button gated on
the compliance report existing (positive evidence, not absence-of-error —
same principle as the 7.5a gap-report guarantee). Tests: promote before
enroll_report completes is refused; promote after works and clones.

### E7. Status-aware chat
Chat phases (chat/answer workflows) on ANY project get a context block:
current `agent_state.json` summary (status, phase, completed count, cost)
plus the last ~10 `events.jsonl` headlines. So "how long? what's going on?
why is it slow?" is answerable from real state. Keep it a plain context
injection (like knowledge injection), not a tool. Tests: block present and
truthful for a fixture state; absent fields degrade to "unknown", never
invented values (test asserts the words, this is the invention trap again).

### E8. GUI enroll entry + report view
"Enroll existing codebase…" in the projects view → NSOpenPanel directory
picker → runs E1 intake → project appears with an "Enrolled" badge.
Report view renders compliance_report + doc-lint gaps, with `[UNVERIFIED]`
and `[RESEARCH]` items visually distinct (this is the whole trust story —
the user must be able to see at a glance what the machine actually knows
vs. flagged vs. researched). Tests: XCTest on the view-model mapping
(verdict → badge), not on AppKit chrome.

## Dependency graph

E1 → E2 → {E3, E4} → E6 → E5; E7 independent (can go first or last);
E8 depends on E1+E4+E6. Suggested order: E1, E2, E3, E4, E6, E5, E7, E8.

## Ambiguities already decided (don't re-ask)

- Non-git target dirs: allowed, warned, snapshot-committed on clone (E5).
- Non-iOS repos: enroll runs anyway; compliance_check reports
  "not-applicable" per rule area rather than refusing. Knowledge domain
  falls back from `ios` by detection.
- Their docs are inputs to `docs_inventory` (quoted with
  `[FROM-THEIR-DOCS:]` tags) — never templates to rewrite in place.
- Cost: enroll uses the same agent roster/config as any run; no special
  budget carve-out in v1.

## Open questions (ask Priyansh only if blocking)

- Should `--enroll` be exposed in the GUI in the same PR as E1, or is CLI
  first acceptable? (Plan assumes CLI first, GUI in E8.)
- Default post-promote workflow `iterate` vs `sprint` — plan says
  `iterate`; flip only if the first real enrollment shows scoping pain.
