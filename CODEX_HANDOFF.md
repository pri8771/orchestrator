# Codex handoff: remaining V3 Sections board (18 cards)

Claude was doing the conductor/autonomy track; ownership is transferring to
Codex for ALL remaining work, including the cards that were reserved for
full-adversarial-rigor review. This doc is the accumulated context so the
safety bar doesn't drop just because the reviewer changed. Read it once,
fully, before starting card 1.

## Board state (2026-07-20)

71/89 landed. Dev HEAD: `955b1f3` (V3 7.11 engine half). 18 remaining, in
dependency order:

1. **8.4** De-iOS-ify — already claimed/in-progress (uncommitted). Finish it.
2. **5.5** GitHub sync contract (docsync.py)
3. **5.6** Notion exporter (depends on 5.5)
4. **7.6** Oversight dials (full-auto/suggest-only/gated/loops-gated)
5. **7.7** Workspace snapshots at quiescence points
6. **7.8** Notifications (stall/converged/budget-exhausted)
7. **7.9** Routing eval suite (recorded-stream replay)
8. **7.10** Mission Control budget-meter UI
9. **7.11 (GUI half)** Pipeline builder canvas — engine half (`pipeline_presets.py`
   + conductor.py loader) already landed at `955b1f3`. Read that module's
   docstring in full before building the compiler.
10. **7.12** Plan gate + activity trace
11. **7.13** Terminal-state honesty audit
12. **8.5** Privacy tiering (sensitivity enforcement)
13. **8.6** Lifecycle/GC (artifact tombstoning, `--gc`)
14. **8.8** Config migration (depends on 8.3 done, 8.4)
15. **9.0** Situation manifest + seeds
16. **9.1** Apply-situation resolution
17. **9.2** Document Builder UI (depends on 9.0, 9.1)
18. **9.3** Situation editor (depends on 9.2, 7.11)

Read each card's FULL text in `orchestrator-v3-task-board.md` before
implementing — this doc is context, not a substitute for the card.

## Why this matters more than usual

7.6–7.13, 5.5, 8.5, 8.6, 9.0, 9.1 are **autonomy/safety-critical**: they
control when the Conductor stops, what it's allowed to do without a human,
what leaves the machine, and what gets permanently deleted. A subtle bug here
doesn't just misbehave — it can mean an unattended overnight run does
something irreversible, or never stops, or silently claims success. Treat
these with more rigor than a typical feature card, even without someone
else reviewing your diff.

## The delivery rhythm that caught real bugs, every single time

Every one of the last ~8 cards (7.4c, 7.5a, 7.5b, 9.5, 7.11 engine half) went
through this exact loop, and every single time it caught 2–7 real, confirmed
defects that shipped-looking code had. Don't skip steps to save time — the
steps are where the bugs get found.

1. **Recon first.** Read the exact seams you're wiring into — real
   file:line, real signatures, real return shapes. Don't assume a helper
   works the way its name suggests; read it. Card citations in
   `orchestrator-v3-task-board.md` go stale (line numbers drift) — verify
   against the actual tree.
2. **Implement.**
3. **Write tests that assert POSITIVE evidence, not absence of failure.**
   The single most common bug class this session: a check that returns
   "pass" when there's simply no data yet, not when the thing is actually
   verified. Concretely: a "no open gaps" check that can't tell "genuinely
   zero gaps" from "no scan ever ran"; an eval-score check that accepts
   `composite=0` from a project that was never built. If your check can
   return true/pass on a completely untouched/never-run input, it's wrong.
   Require positive proof of the state you're claiming, not merely absence
   of evidence against it.
4. **Adversarially self-review before calling it done.** After implementing,
   re-read your own diff as if you're trying to break it. Ask specifically:
   - Can this terminate/stop/succeed FALSELY (before real work happened)?
   - Can this FAIL to terminate/stop (an unreachable condition runs forever)?
   - What happens if the process crashes at each `ledger_append` /
     `save_*state` boundary — is the ledger written before the state is
     saved, and does resume/reconcile replay correctly rebuild state from an
     un-cursored ledger tail?
   - Does a mock/stub in your OWN test hide the exact bug you're trying to
     catch? (See "the test-adequacy trap" below — this bit us twice.)
5. **Sabotage-validate the tests that matter.** For the 2-4 highest-stakes
   behaviors: copy the file to a scratch dir, revert your fix to the
   plausible buggy version, run the specific test, confirm it actually FAILS.
   Then restore FROM YOUR SCRATCH COPY (never `git checkout` — see the git
   safety section). If a test doesn't fail when the bug is reintroduced, the
   test is not doing its job — fix the test, not just the code.
6. **Full gate, isolated.** `make -C <repo root> verify` from the repo root
   (never from `gui/` — `make` silently resolves to the wrong target and a
   piped `| tail` can swallow a nonzero exit). Read the `verify: all gates
   passed` marker in its own step before committing — don't chain gate-read
   and commit in one command (a race there once caused a red push).
7. **Commit + push, staging explicitly.** See git safety below.
8. **Ledger record.** Append a done-record to
   `memory/orchestrator-v3-sections-direction.md`-equivalent (or wherever
   your own progress ledger lives) — what landed, any deviations from the
   card, one lesson if something surprised you. Future-you (or whoever picks
   this up next) needs this more than a clean commit message.

## The test-adequacy trap (hit twice this session — check for it explicitly)

A regression test that mocks a dependency with a lambda that **ignores its
arguments** (`lambda *a, **k: [...]`) will pass even if the code under test
stops passing the right filter/type/status to that dependency. Concretely:
a test mocking `artifacts.list_artifacts` with `lambda *a, **k: []` doesn't
notice if the real code stops filtering by `status="final"` — the mock
returns the same thing regardless. This let a real capability-gate bypass
and a real quiescence-churn bug both ship with "passing" tests.

**The check:** for any test that mocks a store/query function, ask "if the
production code passed the WRONG filter arguments to this mock, would the
test still pass?" If yes, either use a real on-disk store (publish real
fixtures via `artifacts.publish`, not a mock) or make the mock assert on its
call arguments.

## Crash-safety pattern (ledger-before-state, reconcile replay)

The conductor's `.conductor/conductor_ledger.jsonl` is the authoritative,
append-only decision record; `.conductor/conductor_state.json` is a
rebuildable cache. Every state-mutating decision (termination, budget halt,
pipeline activation, and — coming — oversight-dial decisions, notifications
sent, snapshots taken) must:

1. `ledger_append(root, rec)` FIRST (durable, fsynced).
2. THEN mutate `state` in memory.
3. THEN `save_conductor_state(root, state)`.

A crash between steps 1 and 3 leaves the ledger holding a decision the state
file doesn't reflect yet. `reconcile_on_start` must replay that decision from
the un-cursored ledger tail to rebuild the in-memory effect — see
`_TERMINAL_DECISIONS` / `_PIPELINE_DECISIONS` in `conductor.py` for the
existing pattern to extend. **Don't put large/unbounded data in the ledger
detail field** — `ledger_append` truncates oversized lines to 500 chars,
which will silently corrupt a naive JSON-based replay. If you need to replay
something big (like 7.11's presets), ledger a reference (path/id/hash) and
re-derive the full object on replay, not the object itself.

## The git safety notes (this is now YOUR checkout, but still applies to concurrent runs)

- **Stage explicitly.** `git add <exact files for this card>` — never
  `git add -A` / `git add .` / `git commit -am`. If another session (yours
  or a reviewer's) has uncommitted work in the same checkout, a broad add
  sweeps it into your commit.
- **Add and commit in ONE bash call**, chained with `&&`, not two separate
  tool calls. A concurrent commit from another session can land in the gap
  between a separate `add` and `commit`, silently resetting your staged
  index (`git commit` then reports "nothing to commit" — no data is lost,
  but the commit doesn't happen). This actually occurred this session.
- **Verify immediately** after every commit: `git log --oneline -1` and
  confirm the message and file list are actually yours. Don't assume a quiet
  `git commit -q` succeeded.
- **Never `git checkout <file>` to "clean up"** — it destroys uncommitted
  work (possibly someone else's). If you need to revert a bad edit during
  sabotage-testing, restore from your own scratch copy of the file, made
  before you mutated it.
- If you want to gate a change against a clean baseline without disturbing
  the live checkout's uncommitted state, use `git worktree add --detach
  <tmp-dir> origin/<branch>`, copy just your changed files into it, gate
  there, then `git worktree remove --force` when done.

## Card-specific notes

### 7.6 Oversight dials
Wraps the EXISTING `conductor_permissions.py` approval/pending-queue
mechanism (built for 7.4b) — reuse the file contract
(`pending_actions.jsonl` + `approvals/<route_id>.{ok,changes}`), don't build
a parallel one. The four dial positions gate which routes auto-execute vs.
queue; "loops-gated" (the default) needs a definition of what counts as a
loop — check 4.3's lineage/supersedes machinery and 7.5's oscillation
detection (`conductor_termination.has_oscillation`) before inventing a new
loop-detection heuristic. Per-decision undo (mark artifact do-not-route, kill
a spawned session) needs its own audit trail — ledger it.

### 7.7 Workspace snapshots at quiescence
Triggers off 7.5's quiescence layer (`conductor_termination.quiescence_step`
returning `converged=True`). Don't snapshot on EVERY poll — only at the
actual convergence point, or you'll thrash. Check what "snapshot" means here
(git commit? tar? artifact-store copy?) against the card before assuming.

### 7.8 Notifications
Consumes the events 7.5/7.6 already emit (`budget_exhausted`, `stalled`,
`converged_open_items`, approval-related decisions) — this card is a
consumer, not a new emitter. Don't duplicate termination-detection logic;
subscribe to what already exists in the ledger/events.jsonl.

### 7.9 Routing eval suite
Recorded-stream replay against the REAL conductor (`full_poll`/`route_engine`),
not a simulation. Check `simulate_stream.py` and `evalharness.py` for
existing replay-harness precedent before building a new one.

### 7.10 Mission Control budget-meter UI
Pure GUI, reads `costs.py`'s real rollup (`rollup_workspace`) — the acceptance
bar elsewhere in this project has been "never estimate, never show $0.00 for
an unpriced model, always read real accounting." Don't invent a parallel cost
computation in Swift.

### 7.11 GUI half (pipeline builder canvas)
Read `pipeline_presets.py`'s module docstring in full — specifically the
"CANVAS COMPILER NOTE": a canvas edge A→B for artifact type X must compile to
a rule with `"match": {"artifact_type": "X", "source_section": "A"}`. If you
omit `source_section`, the rule silently becomes "any section producing X
routes to B" — not the specific edge drawn. `pipeline_presets.py`,
`conductor.py`, `conductor_routing.py`, `conductor_termination.py` are a
frozen contract from this card's engine half — don't edit them; if the
schema is missing something the canvas needs, that's a signal to extend the
schema deliberately (new card/follow-up), not to route around it.

### 7.12 Plan gate + activity trace
"Plan" as a first-class artifact type — check `artifact_types.json` /
`artifacts.SEED_TYPES` for the registration pattern (a new type needs a
`required` field list + `finalization` policy, following the existing 7
types). Per-step `activity.jsonl` is a NEW file, not `events.jsonl` — don't
conflate the two logs.

### 7.13 Terminal-state honesty audit
This is explicitly about NEVER fabricating completion (§13.2, referenced
throughout this board) — re-verify against `docs/adherence.json` and real
artifact/DoD state, never trust a session's own `done: true` claim. This is
the same "positive evidence" principle from 7.5's goal predicate — read
`conductor_termination.goal_predicate`/`_check_gap_empty`/`_check_eval` for
the established pattern of requiring proof-of-work before accepting a
completion claim.

### 8.5 Privacy tiering
A sensitivity field enforced at `resolve_runner` + `build_context` —
"sensitive fallback terminates at local" means: if a sensitive-tagged
artifact/context would otherwise route to a cloud provider, it must instead
either stay local or the run must refuse, never silently send it to a cloud
API. This is a real data-exfiltration boundary — treat it like the 7.4
capability gates (default-deny, engine-enforced at the actual effect seam,
not just a config flag nobody checks).

### 8.6 Lifecycle/GC
Tombstoning superseded artifacts + `--gc dry-run` + `archive-project`. GC is
inherently destructive-adjacent — the card explicitly wants a dry-run mode;
build and test the dry-run path FIRST, and make the real deletion path
require an explicit non-dry-run flag. Exclude killed lineages correctly —
check 4.3's lineage/supersedes semantics (a superseded artifact in THIS
codebase stays `status="final"` forever — nothing currently marks anything
`status="superseded"` — verify this is still true before assuming GC can key
off that status value, or you may need to introduce the actual superseded
transition as part of this card).

### 8.8 Config migration
One-shot migrator, needs a dry-run diff. Depends on 8.3 (done) and 8.4 — wait
for 8.4 to actually land (check `git log` for its commit, not just the
ledger claim) before starting, since it migrates `tuned workflows,
phase_rules, model_routing` which 8.4 also touches.

### 9.0 Situation manifest + seeds
Pure data, `situations.py` is new. Study `completeness.py`'s `PROFILES`/
`DOD_ORDER`/`load_dod` as the precedent for the tier-ladder shape, and
`workflows.py`'s `ensure_seeded` (never clobber, disk wins) for how the six
default situations get materialized. `doc_slots[]` entries must reference
real slot ids from `sections/documentation/doc_map.json` — read that file's
actual current slot ids, don't guess them from the card text (it may have
drifted).

### 9.1 Apply-situation resolution
State-mutating (a live mid-run switch changes what's running) — depends on
9.0, 5.4, 7.5, 7.1. This is the one non-9.0/9.2/9.3 card in the 9.x cluster
that's genuinely autonomy-adjacent (it changes phase filtering/required
slots for an IN-PROGRESS run) — give it the same rigor as the 7.x conductor
cards, not the light-path treatment.

## Questions / ambiguity

If a card's dependencies claim something isn't landed but you find it in the
code (or vice versa), trust the code — `git log --oneline` and direct file
inspection are ground truth; the task-board's own dependency/line citations
have drifted before (documented in multiple `orchestrator-v3-sections-direction.md`
entries this session). If something is genuinely ambiguous after reading the
card and the actual code, make the most conservative/safe choice (deny by
default, never terminate falsely, never auto-execute) and leave a note in
your ledger record explaining the call — don't block waiting for
clarification on a judgment call you're equipped to make.
