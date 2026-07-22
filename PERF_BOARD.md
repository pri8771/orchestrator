# Perf board: speed, token economy, live visibility (11 cards)

Status: PLAN, approved by Priyansh 2026-07-21. Author: Claude (planning);
implementer: Codex. **Queue behind ENROLL_BOARD.md — finish E3–E8 first.**
The delivery rhythm, test-adequacy-trap check, and git safety notes in
`CODEX_HANDOFF.md` apply verbatim to every card here.

## Goals, in the user's words

Speed and token minimization first; better visibility into what's going on;
a dashboard with real stats; and the ability to watch a conversation live
and "chime in at the right moment."

## Ground truth from recon (2026-07-21, don't rediscover)

- Token counting WORKS: `costs.py` records per-turn tokens via the traces
  side-channel (`record_turn`, `rollup`) — streak shows 858k in / 17k out
  over 34 turns. But turns are UNPRICED by design for CLI subscription
  agents (`cost_micro_usd null -> UNPRICED. We never guess a price.` —
  costs.py:13, ruling R3). What's missing is per-PHASE/per-AGENT grouping,
  durations, and any surfacing. There is no per-phase stats file.
- Cloud failure → straight to local: `_fallback_steps` orchestrator.py:2121,
  the "retrying locally on %s" emit ~2284. No cloud retry exists. This
  session, codex empty-output failures repeatedly dropped turns onto local
  qwen-14b (600s timeout) — the single biggest observed wall-clock loss.
- Probe verdict caches are a flat 4h TTL regardless of WHY the probe
  failed: `_probe_cache_path` orchestrator.py:556; codex model probe :593,
  api probe :1341, gemini probe :1555. A 90s network timeout gets the same
  4h ban as an invalid API key. Observed today: a healthy gemini key benched
  4h by one transient timeout.
- `logs/` holds 8,871 per-turn JSONs, 214MB, no GC of any kind. Schema:
  {timestamp, app, phase, round, agent, command, exit_code, stdout, stderr}.
- Rounds are discrete: the engine has natural seams between turns and
  rounds. CLI runners are blocking subprocesses (no token streaming), but
  `claude` supports `--output-format stream-json` and `codex` has a JSON
  event stream — streaming is an upgrade, not a rewrite.
- GUI already has `BuildActivityLog` (EngineEvents.swift
  `filterBuildActivity`, TranscriptView) fed by `events.jsonl`.

## Invariants

1. **Honest numbers only.** Preserve costs.py ruling R3: never a guessed
   price, unpriced renders as `>= $X · N unpriced`, never a bare total.
   Same for durations/ETAs: derived from recorded timestamps or absent.
2. **No mid-turn injection.** User interjections and pauses take effect at
   turn/round boundaries only — never splice into an in-flight subprocess.
3. **Everything additive & config-gated.** Existing runs, state files, and
   events must parse unchanged. New behavior defaults conservative
   (retry=1, adaptive rounds opt-in, GC dry-run first).
4. **Never GC a live app's logs.** Any log sweep skips apps whose
   agent_state status is running/paused, plus a configurable recency floor.

## Cards (implement in this order)

### P1. Per-phase / per-agent stats layer
Extend `costs.py` turn records with `duration_s` and `prompt_chars` (input
size — P9's context diet needs this baseline). New `stats.py`:
`phase_stats(app_dir)` → per-phase {per-agent tokens in/out, turns,
retries, fallbacks, duration, rounds used}; `fleet_stats(root)` rollup.
Written to `<app>/stats.json` at each phase end (atomic tmp+rename) and on
demand via `--stats [app]` CLI (table output). Failure artifacts already
embed `costslib.rollup` output (orchestrator.py:10205) — reuse, don't fork.
Tests: fixture turn records → exact expected aggregates; unpriced stays
unpriced in every rendering (sabotage: introduce a fake price guess, test
must fail); running-app stats.json parses while mid-phase.

### P2. One cloud retry before local fallback
In `_fallback_steps` / the failure path at orchestrator.py:~2284: on cloud
CLI failure (empty output, nonzero exit, usage-limit NOT included — that
one should still fall through immediately), retry the SAME cloud agent once
(fresh subprocess) before dropping to local. Config
`runtime.cloud_retry_count` default 1, max 2. Emit an `agent_retry` event
(additive kind; GUI headline for it). Tests: fail-once-succeed-twice fixture
uses cloud result; fail-twice falls back exactly as today; usage-limit
bypasses retry.

### P3. Tiered probe-cache TTL by failure class
Probe caches (orchestrator.py:556/593/1341/1555) store a `failure_class`
with the verdict: `timeout`/`network` → 15-minute TTL; `auth`/`invalid_key`
→ 4h (unchanged); success → 4h (unchanged). Classify from the probe's
error text/exception type; unknown failures → 1h. The startup banner
states the class and when the next re-probe is due. Tests: each class →
its TTL honored across a simulated clock; cache files from the OLD format
(no failure_class) are treated as unknown, not crashed on.

### P4. logs/ retention
`--gc-logs [--apply]` (dry-run default, prints what WOULD go) + automatic
sweep at orchestrator start when `runtime.log_retention_days` set (default
unset = no auto-GC). Policy: delete per-turn JSONs older than N days,
EXCEPT apps currently running/paused (invariant 4) and always keep the
newest M=50 per app regardless of age. Deletion is `os.remove` of matched
files only — never rmtree, never touch non-matching names. Tests: dry-run
deletes nothing; apply deletes exactly the fixture's expected set; running
app fully skipped (sabotage: mark fixture app running, assert zero of its
files eligible even when ancient).

### P5. Interjection at round boundaries ("chime in")
CORRECTION (2026-07-22 recon): most of this EXISTS. The GUI inputBar
(TranscriptView.swift `inputBar`/`send()`) already appends to
`<app>/human_inbox.txt` (OrchestratorStore `sendHumanMessage`/`inboxURL`)
and live runs already drain it at the next round barrier via
`requestStepIn`. Do NOT build a parallel mechanism. This card is now:
(a) verify the drain path end-to-end with a real-engine test (note
queued mid-phase → injected exactly once next round → consumed, crash
between inject and consume must not double-inject); (b) make consumption
durable in the ledger if it is not already; (c) emit a
`user_interjection` event so the activity log shows the note landed;
(d) surface queued-but-undrained notes in the GUI (badge on the input
bar) so the user knows their note is waiting.

### P6. Pause / resume at round boundaries
`<app>/.pause_requested` (GUI: pause button next to the activity log).
Engine checks at the same seams as P5: finishes the in-flight turn, sets
state status `paused`, emits `run_paused`, exits the phase loop cleanly
(resumable — same contract as any interrupted run). Resume = delete the
flag and re-run the app (existing resume path). GUI shows paused state
distinctly. Tests: pause lands at boundary not mid-turn; paused state
resumes into the same phase/round; stale flag from a crashed run doesn't
wedge the next run (consumed on startup with an event).

### P7. Stats dashboard (GUI)
New dashboard view on P1's data: per-project — cost/tokens per phase (bar),
phase durations, rounds used vs allowed, fallback + retry counts; fleet —
totals across apps, `>= $X · N unpriced` rendering (invariant 1), top-5
most expensive phases. SwiftUI per the design system; view-model unit
tests on the mapping (stats.json fixture → rows/labels), not AppKit
chrome. Unpriced sabotage test again at the view-model layer: a
fabricated total must fail.

### P8. Round adaptivity (early consensus exit)
Opt-in `runtime.adaptive_rounds: true`. After each round's coordinator
verdict, if outputs materially converge (reuse the quality-gate judgment
path — do NOT invent a new similarity metric; the coordinator already
reads all outputs), skip remaining allowed rounds; emit `rounds_skipped`
event with the reason. Never skips round 1; never skips repair rounds the
quality gate demanded. Tests: convergent fixture → skip + event;
disagreement fixture → full rounds; quality-gate repair unaffected.

### P9. Context diet (needs P1's prompt_chars data)
First: a one-shot `--stats --context` report showing per-phase prompt
sizes from P1 — the evidence. Then: prior-phase outputs injected into
later phases become their CONSENSUS/summary sections (bounded by
`runtime.context_budget_chars`, default generous ~60k) instead of full
transcripts, oldest-first truncation, with an explicit
`[TRUNCATED: full text in <phase>/...]` marker so agents know what they
are not seeing (invariant-1 honesty applies to context too). Tests: budget
enforced exactly; marker present when truncated and ABSENT when not
(sabotage: always-on marker must fail); prompt_contract and the current
phase's own materials never truncated.

### P10. Per-phase model tiering
Config `phase_agent_overrides: {<phase_key>: [agents...]}`, default empty
(no behavior change). Ships with a commented-out suggested map (mechanical
phases — portfolio_audit, task_assignments, doc assembly — on local/cheap;
design/build phases untouched). Merge semantics: override replaces the
roster for that phase only; unknown phase keys warn, unknown agents error.
Tests: override honored in phase banner + turn dispatch; empty config is
byte-identical behavior on a fixture run.

### P11. Streaming runners → live conversation pane
Upgrade claude runner to `--output-format stream-json` and codex to its
JSON event stream; parse incrementally, emit throttled `turn_progress`
events (max ~1 per 2s per agent, cumulative char count + last text delta,
capped) into events.jsonl; GUI extends the activity log with an expandable
live text pane per in-flight turn. Gemini/local runners unchanged (graceful
absence — pane only appears for streaming-capable agents). This is the
biggest and riskiest card (subprocess I/O rework touches the crash-safety
path — re-read the BrokenPipeError history in the streak run before
starting); it is deliberately LAST. Fallback: any stream-parse error
degrades to today's blocking read, never fails the turn. Tests:
stream-json fixture transcript → assembled output identical to blocking
path; mid-stream garbage → degradation, turn still completes; throttling
enforced (event count bounded for a large fixture).

### P12. Seed demo data before visual-QA screenshots
Root cause of both streak release-gate failures: 3-4B vision judges
cannot grade EMPTY-STATE screenshots (dark mode especially — muted
secondary text reads as "low contrast" to them). Durable fix: before
capturing screenshots, launch the app with a `--seed-demo-data` launch
argument (the app-side toggle is already mandated in newer prompts;
detect support by grepping the build for the argument string, skip
seeding when absent). Judges then always see populated screens. Tests:
seeding argument passed when supported; absent → current behavior;
screenshots captured after seeding, not before.

### P13. iterate workflow must honor run_tests
The release-gate repair routes through `iterate`, whose build phase never
sets `run_tests` — so a repair run can "pass" without ever executing the
test suite the original failure may implicate. Add `run_tests: true` to
iterate's build_coordination verify spec (config-gated:
`runtime.repair_runs_tests`, default true). Tests: repair fixture runs
tests; explicit opt-out honored.

### P14. Long-run survivability: caffeinate + dead-pid detection
recall-ios died silently at 03:22 when the Mac slept; state said
"running" with a dead pid until manually inspected. (a) CLI launcher
wraps long runs in `caffeinate -is` (config `runtime.prevent_sleep`,
default true). (b) Anywhere status is read (GUI store, statuscontext,
--stats), a `running` status with a dead `runner_pid` renders as
`stale (process gone)` — never plain "running". Tests: dead-pid fixture
renders stale in all three surfaces; live pid untouched.

### P15. Release-gate: full evaluation + cumulative repair budget
Two defects observed live on recall-ios (2026-07-22): (a) the gate
SHORT-CIRCUITS at the first failing check — a visual-QA false BAD
prevented the UI crawl from re-running, so the repair's actual fix was
never re-verified and the next repair prompt lacks current crawl truth.
Run EVERY gate check every pass and report all failures together.
(b) `release_gate_repairs` resets between passes (both streak and
recall showed "repair 1/2" on their SECOND failure) — the budget must
be cumulative per app until the gate passes, else a nondeterministic
judge drives unbounded repair loops. Tests: multi-failure pass reports
all checks; counter persists across passes; passing the gate resets it.

## Dependency graph

P1 → {P7, P9}; P2, P3, P4 independent; P5 → P6 (shared boundary-check
seam — build it once in P5); P8 independent; P10 independent; P11 last.
Suggested order: P1, P2, P3, P4, P5, P6, P7, P8, P9, P10, P11.

## Ambiguities already decided (don't re-ask)

- ETA display: out of scope. Durations are shown historically (invariant
  1); no predictive "time remaining" in v1.
- Interjection scope: injected into ALL agents' next-round context, not a
  private note to the coordinator.
- Adaptive rounds default OFF; retry default 1; auto log-GC default OFF.
- API-runner (pay-as-you-go) streaming already exists; P11 is CLI runners
  only.

## Open questions (write here if blocking, don't guess)

- (none yet)
