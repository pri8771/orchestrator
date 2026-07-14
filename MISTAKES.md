# MISTAKES.md — recurring failure taxonomy + the mistakes ledger

A short, curated record of the mistake *classes* the four audit-fix rounds
(TASKS.md … TASKS_ROUND4.md) actually found in this codebase, kept next to the
code so new work checks itself against the known failure modes.

## Taxonomy (what the audits kept finding)

| Class | One-line description | Found in |
|---|---|---|
| String-truthiness coercion | `bool("false")` is `True` — hand-edited JSON/YAML string booleans ("false", "0") silently flipped features back on (e.g. model_routing `enabled`). | Round 2 |
| TOCTOU races | Check-then-act gaps on shared files/state: shared `.tmp` names clobbered by concurrent writers, stale-lock checks racing the lock owner, read-modify-write on agent_state.json losing updates across threads. | Rounds 1–3 |
| Stale mtime caches | Change detection keyed on cheap mtime signatures went stale (or over-triggered) when directories were renamed/pruned; cached probe verdicts outlived the condition they measured. | Round 3 |
| Silent fallbacks / self-graded gates | Failures degraded quietly into success: fallback rescues visible only as transcript prose, an integrator's own "CONSENSUS: YES" accepted with no external check, quality gates that the same model both took and graded. | Rounds 1–4 |
| Tail-truncation context loss | `text[-limit:]` context budgeting silently dropped the OLDEST (most foundational) decisions first, with no marker that anything was missing. | Round 4 |

## The runtime ledger: `<app>/mistakes.jsonl`

The engine now appends one JSON line per recorded mistake to each project's
`mistakes.jsonl` (same never-raise, atomic-append, secret-redacted contract as
`events.jsonl`). Record fields: `ts`, `app`, `workflow`, `phase`, `agent`,
`cls`, `summary`, optional `detail`. Classes:

- `verify_failure` — a build compiled and FAILED (initial or repair attempt).
- `repair_queued` — the release gate refused the done flag and queued a repair.
- `quality_gate_fail` — a phase closed despite a failing quality evaluation.
- `agent_fallback` — a cloud turn was rescued (or lost) by the fallback ladder.
- `contract_error` — tasks.json/interfaces.json parse errors or dependency cycles.
- `consensus_unverified` — the integrator declared consensus while the real
  verifier said the build does not compile.

## The report: `--mistakes`

```
python3 orchestrator.py --mistakes [--app NAME] [--json]
```

Aggregates every project's ledger under the workspace root: total counts
per class, per phase, and per agent, plus a per-app **verification rollup**
(`verified` | `failed` | `unverified`) derived from the latest persisted
verification record — so a run where every build went unverified (no
toolchain) is no longer indistinguishable from a genuinely verified one.
`--json` prints a machine-readable report (stdout is pure JSON).

## The report: `--postmortem`

```
python3 orchestrator.py --postmortem --app NAME [--json]
```

One correlated failure report for a single project (postmortem.py): run
status + verification rollup, workflow and the last phase/round reached,
phase-by-phase completion with consensus status, the failure chain from
`events.jsonl`, every persisted verification attempt, this app's
mistakes-ledger aggregation, and measured per-phase/per-agent turn telemetry
(turn counts, durations, output chars, fallback counts). Text mode is a
timeline; `--json` prints one structured object (stdout is pure JSON).
