# AUDIT_BOARD.md — full defect inventory (2026-07-28)

## Progress — COMPLETE (2026-07-28)

**All 88 cards are fixed.** CI blockers CI-1…4 ✅ · HIGH A-01…A-13 ✅
· MEDIUM A-14…A-40 ✅ · LOW A-41…A-88 ✅ (A-82 resolved by materializing the
six situations/ seeds as repo files).

**A-49 closed 2026-07-28 (82c9945)** — the operator decision went to
*opt-in*: `gemini_enabled`/`ollama_enabled`/`visual_qa_enabled` are committed
as `false` with the three comment blocks rewritten to describe the opt-in
honestly. Rationale: gemini's free tier is 20 req/day/model, and
ollama/visual-QA only help once the local models are actually pulled.

**Final gate:** 2289 engine tests OK (strict; +147 net new regression tests
since the audit), 414 GUI tests OK (+20), ruff clean, mypy clean, shell
syntax clean, doctor green, suite leaves the working tree clean.

Nothing was committed — review and stage explicitly (never `git add -A`;
this checkout is shared).

Produced by a 26-agent audit (13 subsystem finders + 13 adversarial verifiers) over
branch `dev` @ 37483a3, plus a full local gate run (strict unittest suite, ruff, mypy,
doctor, GUI build + 394 GUI tests). **98 defect claims were raised; 97 were CONFIRMED
by an adversarial verifier that tried to refute each one; 1 was refuted and dropped.**
After merging cross-lens duplicates: **88 cards + 4 CI-blocker cards below.**

## How to use this board (instructions for the fixing model)

- Work **one card at a time**. Fix exactly what the card says, run the verification
  command the card names, then run the module's tests before moving on.
- Cards are already adversarially verified — trust the evidence, but re-read the cited
  lines before editing; line numbers drift as fixes land.
- NEVER `git add -A` (shared checkout — stage files explicitly).
- Full gate when a batch lands: `python3 -W error::ResourceWarning -m unittest discover -s tests`
  then `ruff check .` and `mypy . --config-file pyproject.toml`.
- Severity: **HIGH** = a feature/gate is broken or lies, or data is lost.
  **MEDIUM** = wrong under realistic conditions. **LOW** = cruft, drift, doc contradictions, edge cases.

---

## Section 0 — CI blockers (fix these first, in this order; CI has been red since 2026-07-21)

### CI-1 [BLOCKER] Stale test double: 2 committed test failures in tests/test_xcodebuild_tests.py

**Where:** `tests/test_xcodebuild_tests.py:73` (the `fake_run` stub in `TestVerifyXcodeRunTests._stub_run`)

**Evidence:** Commit 45e289a added `_concrete_sim_destination()` (verify.py:314 → 176) which calls
`xcrun simctl list devices available -j` before the test action. The stub raises
`AssertionError("unexpected command: ...")` on any unrecognized argv, so
`test_run_tests_true_runs_after_successful_build` and
`test_failing_test_run_is_recorded_but_ok_reflects_build_only` both FAIL — locally and in CI, every push since 2026-07-21.

**Fix:** In `_stub_run`'s `fake_run`, add a branch BEFORE the final `raise`:
```python
if cmd[:4] == ["xcrun", "simctl", "list", "devices"]:
    return (0, json.dumps({"devices": {"iOS 18.0": [
        {"udid": "TEST-UDID-1234", "state": "Booted",
         "name": "iPhone 15", "isAvailable": True}]}}), "")
```
Add `import json` to the file's imports if missing. Then assert the test action received the
concrete destination (the cmd containing `test` should include `platform=iOS Simulator,id=TEST-UDID-1234`) —
optional but keeps the new behavior pinned. Verify: `python3 -m unittest tests.test_xcodebuild_tests -v` → all 21 pass.

### CI-2 [BLOCKER] mypy (an ENFORCING CI job) fails on two unannotated module globals

**Where:** `buildpolicy.py:26-27`

**Fix:** Change to:
```python
_CACHE: dict[str, dict] = {}
_WARNED: set[str] = set()
```
(PEP 585 builtin generics work at runtime on the 3.9 floor.) Verify: `mypy . --config-file pyproject.toml` → 0 errors.
(If the value types are wrong, mypy will say so — adjust the value type to what `_read_policy` stores.)

### CI-3 [BLOCKER] ruff F811 + 3 permanently-skipped security regression tests → see card A-35

Delete the weaker duplicate `TestLibraryMiningBothGates` class and both mid-file
`if __name__` blocks per A-35. The shadowed (stronger) tests were resurrected during this
audit and all 3 pass today — this is dormant coverage, not a hidden engine bug.
Verify: `ruff check .` → 0 errors; `python3 -m unittest tests.test_section_capability_engine` → 13 tests.

### CI-4 [BLOCKER] Working-tree model_routing.json was destroyed by the GUI → restore, then fix the GUI (A-06)

**Evidence:** `test_migrate_v3` seed-hash FAILs against the working tree only — HEAD still matches the
pin (sha256 a84bbac…). The GUI's `ModelRouting.save` rewrote the file, deleting `_examples`,
the long `_docs`, `fallback.chains: {}`, and the trailing newline. Semantics (the two phase
entries) are unchanged, so restoring is lossless.

**Fix:** `git restore model_routing.json` (do NOT hand-edit). Verify:
`python3 -m unittest tests.test_migrate_v3` → all pass. The root cause is card **A-06** —
until it lands, any Settings→Routing save in the GUI re-breaks this file.

Also note for CI: fixing CI-1..4 turns the test/lint/typecheck jobs green, but the clean-tree
guard still cannot see the `situations/` pollution (A-14 ci.yml guard + A-34 test seeding, plus A-82 for the engine-side design) — land those two together to make the guard honest.

---

## Confirmed findings (88 cards, severity-sorted)

### A-01 [HIGH] Per-route hop-budget 'budget_exhausted' ledger line is replayed as a permanent WORKSPACE halt on restart/rollback

**Where:** `conductor.py:713`  ·  lens: conductor-stack  ·  verified: CONFIRMED

**Evidence:** Two different decisions share one ledger name. guard_route's per-route hop-budget verdict is BUDGET_EXHAUSTED = "budget_exhausted" (conductor_routing.py:36) and execute_intents ledgers it verbatim: `ledger({**base, "decision": intent.verdict})` (conductor_routing.py:398) — a record WITH session and route_id. The workspace budget halt uses the same name with "session": None and no route_id (conductor.py:1026). reconcile_on_start's replay only checks the name: `if rec.get("decision") == "budget_exhausted" and not state.get("halted"): state["halted"] = {...}` (conductor.py:713-718). Reproduced: wrote a single per-route record {decision: budget_exhausted, session: 'proj/planning', route_id: 'abc123'} into a fresh ledger and ran reconcile_on_start(root, default_state()) -> `halted after replaying a per-route hop-budget line: {'reason': 'budget_exhausted', 'ts': 1.0}`. This fires (a) on a crash between that route's ledger append and the state save, and (b) on EVERY rollback/repair_pending_rollback, which replays the entire retained ledger from default_state() cursor 0 (conductor.py:1580, …[trimmed]

**Fix (verifier-corrected):** The proposed fix is correct as written: require `not rec.get("route_id")` at conductor.py:713 (workspace records from _record_workspace_termination carry no route_id; per-route records always do, and reconcile already relies on that distinction at lines 791-795 via _ROUTED_DECISIONS). The suggested regression test's assertion that state['routed']['r1'] becomes True already holds via the _ROUTED_DECISIONS replay, so the test is coherent. tests/test_conductor_termination.py:793 test_budget_halt_survives_restart_via_reconcile exists as claimed and must stay green (workspace record has no route_id, so it will).

### A-02 [HIGH] A workspace budget halt can never be lifted — the documented 'until the cap is lifted (a new manifest ...)' path does not exist in code

**Where:** `conductor.py:1759`  ·  lens: conductor-stack  ·  verified: CONFIRMED

**Evidence:** route_engine's own comment (conductor.py:1757-1758): 'a workspace budget halt stops ALL routing — the acting stage is a no-op until the cap is lifted (a new manifest / a new day for wall-clock)'. But `state["halted"]` is only ever assigned at three sites — default None (line 315), the reconcile replay (line 716), and _record_workspace_termination (line 1029) — never cleared. evaluate_terminations only checks `if bc["exhausted"] and not state.get("halted")` (line 1127) and returns early once halted (line 1140). Reproduced: set state['halted']={'reason':'turns_exhausted'}, then ran evaluate_terminations with a manifest whose caps are 999999 (usage 0) -> `halted after caps raised well above usage: {'reason': 'turns_exhausted', 'ts': 1.0}` and route_engine returned immediately without acting. So after any hard-cap hit, raising or removing the budget in goal_manifest.json (or a new day for wall-clock) does nothing; routing is dead until someone hand-edits conductor_state.json — and finding #1 can re-instate the halt from the ledger even then.

**Fix (verifier-corrected):** The proposed lift branch is right but INCOMPLETE: placed inside `if budgets:` it never runs on the documented 'a new manifest' path where the operator DELETES the budgets key (block skipped) or empties the manifest entirely (evaluate_terminations not called at all per full_poll's gate at 1454-1456). Extend it: (a) inside evaluate_terminations, before `if budgets:`, add `if not budgets and state.get("halted"):` -> same budget_lifted ledger+clear (evidence: {'budgets': None}); (b) keep the proposed in-block lift for the caps-raised/new-day case; (c) the reconcile_on_start budget_lifted replay as proposed — and note it must be combined with claim 1's route_id guard, or a later per-route budget_exhausted line in the replayed tail would re-halt after the lift. Also beware _last_good_budgets (conductor.py:1443-1451): a lift should only fire on an OK-status manifest, which the existing structure already guarantees since the corrupt-manifest path substitutes the old budgets.

### A-03 [HIGH] Private artifacts lose their privacy stamp when routed through the approval gate — approved mint skips persist_private_session and drops sensitivity

**Where:** `conductor.py:1887`  ·  lens: conductor-stack  ·  verified: CONFIRMED

**Evidence:** The direct mint path enforces V3 8.5: `if session_dir and request.get("sensitivity") == "private" and not orchlib.persist_private_session(session_dir): ... session_dir = None` (conductor.py:2590-2594, and 2123-2127 for plans), with request_extra injecting sensitivity from the artifact meta (conductor.py:2729-2732). The approval path loses it at every layer: (1) cplib.pending_action's payload is only {artifact_id, content_hash, source_section, rule_id, strategy} — no sensitivity (conductor_permissions.py:120-132); (2) _mint_request rebuilds the request from that payload, so the minted delegation.json request has no sensitivity field (conductor.py:1814-1821); (3) _drain_pending's approved branch mints via seslib_local.mint_delegation_session with no persist_private_session call at all (conductor.py:1882-1913). The engine's privacy detection (orchestrator._effective_sensitivity, lines 1913-1923) relies on exactly the two sources this path omits: the child's run_config.json stamp and the delegation request's sensitivity. Consequence: a private artifact whose route is gated (dial …[trimmed]

**Fix (verifier-corrected):** The three edits are correct and complete for the route path (the plan path already carries sensitivity end-to-end, verified). Two refinements: in edit (3), keep the persist_private_session call strictly on the non-notification branch — for target == 'notification', sdir is the REQUESTING session's own dir (conductor.py:1884-1885) and stamping it would wrongly flip the parent private; and compute the sensitivity once from action['payload'].get('sensitivity') rather than calling _mint_request(action) a second time. orchlib is in scope inside _drain_pending (imported at route_engine top, conductor.py:1750).

### A-04 [HIGH] recompute_gap_report bypasses docsync: clobbers human-overridden docs and poisons the ownership ledger

**Where:** `docs.py:1366`  ·  lens: v3-stack  ·  verified: CONFIRMED

**Evidence:** recompute_gap_report (docs.py:1345-1378) writes docs/HANDOFF_BLUEPRINT.md and docs/GAP_REPORT.md via _write() unconditionally: `_write(os.path.join(docs_dir, "HANDOFF_BLUEPRINT.md"), blueprint)` and the GAP_REPORT write right below. It takes no human_overrides parameter and never calls docsync.prepare_render/finish_render. Both files are in docsync._ALL_SLOT_FILES (docsync.py:41-46), i.e. exactly the files the 5.5 human-override contract protects, and the normal render path (orchestrator.py:11579-11609) does gate them: write_project_docs(..., human_overrides=overrides) skips overridden paths in put(). The caller is conductor.py:1368 (Situation-switch recompute), which runs against live session dirs. Two failure modes: (1) a human-edited HANDOFF_BLUEPRINT.md/GAP_REPORT.md is silently overwritten — the exact data loss 5.5 exists to prevent; (2) recompute writes into app_dir/docs WITHOUT a milestone commit (docsync._commit is never called), so the next prepare_render's `git diff HEAD` (docsync.py:237) sees the engine-written bytes as divergence, marks both files status=human-overridden …[trimmed]

**Fix (verifier-corrected):** The proposed fix is essentially correct; two refinements. (1) finish_render(app_dir, context, written, ...) needs the list of project-relative paths actually written this pass ("docs/HANDOFF_BLUEPRINT.md", "docs/GAP_REPORT.md", minus any skipped for override) so cleared records are popped correctly — recompute_gap_report currently returns only coverage, so either have it also return the written list or have the conductor reconstruct it from which writes were not skipped. (2) In conductor.sync_situations there is no cfg in scope; either load the root config the same way the conductor does elsewhere to honor runtime.docs_git_sync_enabled, or simply call prepare_render unconditionally — it degrades to {enabled: False} when no docs repo exists, and a session that rendered with sync disabled has no docs/.git, so the unconditional call is safe. Otherwise implement exactly as described: human_overrides param gating both _write calls in docs.py, prepare_render before / finish_render after in conductor.py ~1368, plus the suggested test in tests/test_conductor_situations.py.

### A-05 [HIGH] Fleet anti-pattern ledger is never injected into any prompt — fleet-learning read half is dead

**Where:** `fleetlearn.py:317`  ·  lens: seeds-config  ·  verified: CONFIRMED

**Evidence:** fleetlearn.py's module contract (lines 18-21) says: 'build_ledger() clusters incidents + bad ratings into knowledge/anti_patterns.md ... The existing knowledge splicer scores and injects it into build/design phases like any other cheatsheet, so every recorded failure teaches future runs.' The write half works (build_ledger writes os.path.join(here, 'knowledge', 'anti_patterns.md') — fleetlearn.py:317 — and the working tree shows the file actively growing: git diff shows new visual_qa/design_lint clusters from real runs). But the read half can never fire: the only injection path is orchestrator.py:8945 `tctx.knowledge = knowlib.retrieve(HERE, domain, ...)`, and knowledge.retrieve (knowledge.py:167-177) scans ONLY os.path.join(orch_dir, 'knowledge', domain) for *.md. domain_for (knowledge.py:85-100) always returns a non-empty domain ('ios'/'backend'/'web'/'general' or the config value), so the root-level knowledge/anti_patterns.md is never in any scanned directory. Repo-wide grep confirms no other reader: the only mentions of anti_patterns outside fleetlearn.py are a comment at …[trimmed]

**Fix (verifier-corrected):** The proposed fix is directionally right but needs two refinements. (1) Gate the append with the existing knowlib.should_inject(key) condition (same block at orchestrator.py:8941) rather than inventing a new notion of 'injectable phases'. (2) Do NOT read os.path.join(HERE, 'knowledge', 'anti_patterns.md') directly: build_ledger honors ORCH_LEDGER_DIR (fleetlearn.py:270-272), and tests/__init__.py:16 exports it precisely so no test touches the repo's tracked ledger. Add a small fleetlearn.read_ledger(here) helper that applies the same ORCH_LEDGER_DIR redirection, and have process_phase call it best-effort (try/except OSError), cap ~2000 chars, and append with its own banner to tctx.knowledge. Otherwise the new regression test would read/pin this repo's own tracked knowledge/anti_patterns.md. Then run: python3 -m unittest tests.test_knowledge tests.test_fleet_quality

### A-06 [HIGH] ModelRouting.save rebuilds model_routing.json from typed fields, destroying engine-honored and documented keys (fleet AND per-project files)

**Where:** `gui/Sources/OrchestratorGUI/ModelLibrary.swift:246`  ·  lens: gui-swift+seeds-config  ·  verified: CONFIRMED

**Evidence:** Round-trip verified by code trace of load (lines 205-244) vs save (246-285) plus the live destruction in the working tree (git diff model_routing.json: the whole _examples block, the full _docs field-reference text, the explicit "chains": {} key, and the trailing newline are gone; save also emits '\ No newline at end of file'). SURVIVES a load->save: enabled; fallback.cloud_to_local; fallback.local_model; fallback.chains entries that are non-empty [String]; phases.<key>.{claude, codex, codex_reasoning, claude_reasoning, gemini, ollama, agents, composition, cast_size, timeout>0, rounds, instructions}; phases.<key>.roles.worker/integrator with only the six RoleFields keys. DROPPED/DAMAGED: (a) any top-level key other than schema_version/_docs/enabled/fallback/phases — _examples confirmed destroyed; (b) _docs is REPLACED by the GUI's shorter string (line 276); (c) schema_version is hardcoded to 1 (line 275) — a future engine bump gets silently downgraded; (d) phases.<key>.gemini_reasoning and phases.<key>.ollama_reasoning — both in the engine's PHASE_FIELDS (modelrouting.py:77-79); …[trimmed]

**Fix (verifier-corrected):** The proposed raw-preserving merge is right but incomplete in one spot: the phase-level residual merge (remove modeled keys, re-add typed values) still loses unknown keys INSIDE roles.worker/roles.integrator, because RoleFields.jsonObject rebuilds those sub-dicts from six fields — and per-role ollama_reasoning is engine-honored (orchestrator.py _apply_role_routing). So either (a) apply the same residual-merge inside each role sub-dict (start from the raw role dict, remove only the six modeled keys, re-add non-empty typed values), or (b) promote gemini_reasoning/ollama_reasoning to first-class RoleFields/PhaseRoute fields — the claim lists (b) as optional hardening, but one of the two is REQUIRED for a complete fix. Rest of the fix (rawRoot carry, fresh read-then-merge in save, keep phases whose residual dict is non-empty, don't overwrite _docs/_examples/schema_version when present, trailing newline, fixture-based swift test) is correct as written.

### A-07 [HIGH] Section-scope routing grid writes model routing to sections/<name>/routing.json — the CONDUCTOR routing file — so section model edits never take effect and Apply destroys seeded artifact_routes/rules

**Where:** `gui/Sources/OrchestratorGUI/RoutingGridView.swift:262`  ·  lens: gui-swift  ·  verified: CONFIRMED

**Evidence:** routingURL for .section returns 'sections/\(name)/routing.json' (RoutingGridView.swift:262-265) and applyChanges saves a ModelRouting there (line 321). But the engine's section model-routing overlay reads sections/<name>/model_routing.json ONLY: modelrouting.py ROUTING_FILENAME="model_routing.json" (line 70), _overlay_routing joins layer_dir+ROUTING_FILENAME, and orchestrator.py:7137 load_routing_for_session(HERE, app_dir, section_dir=_section_dir(cfg)) with _section_dir = HERE/sections/<section> (orchestrator.py:3239-3243). So every model/effort/participant edit made in Section Settings -> Models is silently ignored by runs. Worse, sections/<name>/routing.json is conductor_routing.py's config (ROUTING_FILENAME="routing.json", line 31) and is SEEDED with real content: sections/_template/routing.json, sections/design/routing.json, sections/execution/routing.json etc. all contain {artifact_routes, rules} (verified by parsing them). ModelRouting.save overwrites that file with {schema_version,_docs,enabled,fallback,phases}, erasing artifact_routes and rules; conductor_routing.py's …[trimmed]

**Fix (verifier-corrected):** The fix (change the .section case to sections/<name>/model_routing.json; grep for other section-scope writers; regression test; restore clobbered installs from seed) is correct. Add one item: the repo's own seeds are inconsistent the same way — sections/documentation, ideas, planning, qa, research ship routing.json containing {"phases": {}} (model-routing shape under the conductor filename, seeded by commit aec7830). Review/rename those seeds to model_routing.json (or empty conductor shape) as part of the fix so the two contracts stop sharing content shapes under one name.

### A-08 [HIGH] build_app.sh bundles the builder's private run logs, probe/state files, and 7MB of caches into the redistributable .app/DMG

**Where:** `gui/build_app.sh:53`  ·  lens: scripts-ci  ·  verified: CONFIRMED

**Evidence:** The engine-copy `find` (lines 53-67) excludes only ./gui, ./.git, ./logs, ./locks, __pycache__, ./tests, ./sample-run, ./.orchestrator plus secret-shaped names. I dry-ran the exact find against the repo: it includes ./driftwords-run.log, ./driftwords-2-run.log, ./gloam-run.log, ./tether-run.log, ./tipjar-run.log (~600KB of the builder's actual orchestrator run transcripts, written to the repo root by shepherd.sh's launch(): `nohup bash run.sh --app "$1" >> "$logf-run.log"`), ./.gemini_probe.json, ./.codex_model_probe.json, ./runtime/loaded_models.json and .lock (local model state), ./.mypy_cache (5.1MB, 18 files), ./.ruff_cache, ./build (1.8MB — a stale duplicate copy of every engine module from pip's in-tree build, dated Jul 15), and ./orchestrator.egg-info. make_dmg.sh (line 22) wraps this app verbatim, so a distributed DMG ships the builder's AI-session transcripts and local state. The script's own header (lines 41-44) says 'CRITICAL: exclude secret-shaped files. This bundle is redistributed by make_dmg.sh' — the intent exists, but the exclusion list predates the run logs/caches …[trimmed]

**Fix (verifier-corrected):** The proposed find exclusions are correct but incomplete: also add `-not -path './.claude/*'` — otherwise the DMG still ships .claude/settings.local.json and the full .claude/worktrees/m5-docs-manifest engine-copy worktree (which itself contains shepherd.sh/orchestrator.py duplicates). The git-archive alternative is the more robust fix and subsumes the config.yaml (and model_routing.json) HEAD special-cases, but note it would newly bundle tracked-but-currently-excluded content (tests/, sample-run/, gui/ sources) unless the tar extract is followed by removing those dirs or `git archive HEAD -- <paths>` is scoped; harmless but ~larger bundle. The verification command in the fix is good; extend its -o list with `-path '*.claude*'`.

### A-09 [HIGH] Fallback ladder hijacks failed resumed session calls with the delta-only prompt, defeating the documented stateless full-prompt safety net

**Where:** `orchestrator.py:2241`  ·  lens: orch-core-a  ·  verified: CONFIRMED

**Evidence:** call_agent_sessioned's docstring (lines 2696-2699) promises: "ANY failure of a resumed call falls back to one stateless full-prompt call, so a lost/expired session can never produce a worse result than before." But the resumed call goes through call_agent (line 2724: `return call_agent(cfg, app, phase, rnd, agent, delta_prompt)`), and call_agent's own AgentError handler (line 2241: `steps = _fallback_steps(cfg, agent)`) engages the fallback ladder FIRST, never checking `cfg["_session"]["resume"]`. Every ladder step retries with the prompt call_agent received — the DELTA prompt ("NEW MESSAGES SINCE YOUR LAST TURN...", built by _delta_discuss_prompt line 2748, which assumes the agent already holds the phase context) — on a stateless copy (thread_copy(stateless=True) sets _session=None, turncontext.py:172-173). A context-free model answering the delta prompt has no phase context at all. If any step succeeds, call_agent returns normally, so call_agent_sessioned's except-arm (2735-2740) never fires: the dead session id stays in the map and EVERY later turn of the phase repeats …[trimmed]

**Fix (verifier-corrected):** The proposed two-part fix is correct and I verified its preconditions: (1) `if (cfg.get("_session") or {}).get("resume"): raise` before line 2241 is safe — only call_agent_sessioned ever sets _session with resume=True, and its except-arm sets tctx.session=None (cfg["_session"]=None) before the full-prompt retry, so the ladder stays available for that retry. (2) The marker guard `if new_sid and not out.startswith("_[Fallback: ")` works — both rescue returns (lines 2297, 2310) start with that exact literal. One wording correction for the proposed tests: stub `_call_agent_once` (not call_agent) and note macOS quirk none; run with `python3 -m unittest tests.test_session_reuse`. Also note the first-call manifestation needs only edit (2), and the resumed-call manifestation needs only edit (1), but both edits are required for full coverage.

### A-10 [HIGH] Conversational barrier commands (/vote, /consensus, /cast) are queued then silently wiped by the next save_state — they never fire

**Where:** `orchestrator.py:7672`  ·  lens: orch-core-c  ·  verified: CONFIRMED

**Evidence:** _queue_barrier_command (line 4324) does its own load_state/save_state round-trip to append the row to disk, but the conversational loop keeps saving its STALE in-memory `state` dict: _take_barrier_commands refreshed `state` at round open (before the command arrived), then after _dispatch_command returns, lines 7671-7672 run `state["next_agent"] = "+".join(round_agents); save_state(app_dir, state)` — save_state (line 4697) dumps the whole dict with no merge, so the just-queued row is erased before the next round's barrier can see it. Reproduced with the real functions: after the queue, disk shows [{'name': 'vote', 'args': '', 'requested_round': 1}]; after the loop's save it shows []; `_take_barrier_commands(app_dir, state, 2)` returns [] — the vote never fires, even though the user was shown '**/vote queued** — will take effect at the next round barrier'. The V3 9.8 contract at line 7577 ('room-mutating commands are applied only after the round in which they were requested. The persisted queue makes crash/resume deterministic') is therefore dead code on the live path; …[trimmed]

**Fix (verifier-corrected):** The proposed fix is correct and complete: add `state=None` to _queue_barrier_command (mutate the caller's dict with the same name-dedupe filter, then save_state; fall back to load_state when None), thread `state` from _run_conversational_phase through _dispatch_command into the `barrier` closure at 4502-4503, and add the interleaving regression test. Equivalent minimal alternative if less plumbing is wanted: immediately after _dispatch_command returns at 7658, resync with `state.clear(); state.update(load_state(app_dir))` before the 7671-7672 save — but the write-through-state version is the cleaner contract. Run: python3 -m unittest tests.test_commands tests.test_conversational

### A-11 [HIGH] Release-gate repair budget is wiped every pass by the prompt-hash reset — the 'capped' repair loop is actually unbounded

**Where:** `orchestrator.py:11194`  ·  lens: orch-core-d+verify-stack  ·  verified: CONFIRMED

**Evidence:** _queue_release_gate_repair (line 10874-10875) rewrites initial_prompt.md with a '## Change requested' tail containing the CURRENT gate-failure reason, and increments state['release_gate_repairs'] (line 10832). On the next pass, _run_app_pipeline recomputes phash over the rewritten prompt (line 11192) and, because the text changed, takes `if state.get("prompt_hash") != phash: reset_state_for_new_prompt(state, phash)` (lines 11194-11196) — and reset_state_for_new_prompt sets `"release_gate_repairs": 0` (line 4690). Gate reasons embed dynamic text ('last verification failed (%s)' % summary, visual-QA grader verdicts, ui_crawl flow failures, adherence unmet lists), so the tail differs pass-to-pass and the counter is zeroed before it is ever read. Reproduced with the real functions: 5 consecutive gate failures with varying reasons each ended with release_gate_repairs=0 — every pass logs 'queued repair 1/2', 'repair budget exhausted' is unreachable, and the emit_failure_artifact('release_gate_budget_exhausted') path (lines 11450-11455) is dead code for varying reasons. Under --watch or …[trimmed]

**Fix (verifier-corrected):** The proposed fix (hash only the text before '\n\n## Change requested\n') is WRONG in a way the auditor missed: its premise '(a) the engine is the only writer of that marker' is false. shepherd.sh:214-215 also appends '\n\n## Change requested\n' to a DONE app's prompt (auto-repair of hollow builds) and explicitly depends on the hash moving — shepherd.sh:116: 'the prompt change resets their pipeline into the iterate flow'. The shepherd never sets done=False, so with the tail stripped from the hash the relaunched orchestrator would hit the 'unchanged and already done — skipping' branch (line 11198-11200) and the shepherd repair becomes a permanent no-op (.repair_attempted blocks requeue). Corrected fix — keep today's hash/reset semantics and instead carry the budget across body-identical resets: in _run_app_pipeline next to line 11192, also compute body = prompt.split("\n\n## Change requested\n")[0].rstrip("\n") (rstrip is required: the first append does base = existing.rstrip("\n") at line 10874) and bhash = sha256_text(body + "\n#target:" + _tgt + "\n#tsig:" + sha256_text(_tsig)). On phash mismatch: if state.get("prompt_body_hash") == bhash (only the engine/shepherd tail changed), preserve _keep = state.get("release_gate_repairs") across reset_state_for_new_prompt and restore it; otherwise reset normally. Always store state["prompt_body_hash"] = bhash. This keeps shepherd's flow (full reset still happens, done cleared) while a real human body edit still zeroes the budget per reset_state_for_new_prompt's documented intent. Add the regression test in tests/test_release_gate.py: repair with reason A, recompute phash+bhash the pipeline's way, simulate the reset path, repair with different reason B, assert release_gate_repairs == 2. Run: python3 -m unittest tests.test_release_gate tests.test_shepherd

### A-12 [HIGH] shepherd.sh lane counter counts stale (dead-pid) lock files — crashed/rebooted fleet permanently loses build lanes and can deadlock forever

**Where:** `shepherd.sh:107`  ·  lens: scripts-ci  ·  verified: CONFIRMED

**Evidence:** The per-iteration capacity count (lines 107-111) is:
  for L in "$ROOT"/.orch-locks/*.lock; do
    [ -f "$L" ] || continue
    b=$(basename "$L" .lock)
    if is_parent "$b"; then parents_running=$((parents_running+1)); else builds_running=$((builds_running+1)); fi
  done
It counts every lock FILE. The launch guard locked() (lines 55-60) was deliberately fixed (per its own comment, lines 46-54: 'A stale lock (its pid is dead ...) must NOT count as running') to grep the pid and kill -0 it — but the counter was not given the same fix. After a crash/SIGKILL/reboot the engine leaves the lock file behind (engine reclaim happens ONLY inside acquire_app_lock on a new launch of that same app; grep shows no other sweep of .orch-locks in orchestrator.py). So N stale locks permanently consume N of MAX_BUILDS lanes; when N >= MAX_BUILDS (e.g. a reboot while MAX_BUILDS builds were live), every launch loop hits `[ "$builds_running" -ge "$MAX_BUILDS" ] && break` before launching anything, no launch ever runs to reclaim the locks, and the fleet loops 'N child app(s) still pending' forever — the …[trimmed]

**Fix (verifier-corrected):** The fix is correct (skip locks whose pid is missing/dead, mirroring locked(); extracting a shared lock_live() helper is the right shape). One correction to the test guidance: `bash shepherd.sh --check-lock dead` only exercises locked(), which is ALREADY correct — it proves nothing about the counter. To test the counter you need to drive a loop iteration (e.g. add a `--check-capacity` diagnostic hook like the existing --check-lock/--check-disabled hooks that prints parents_running/builds_running and exits), then assert a dead-pid lock yields builds_running=0. The lock_encoding.json parity check via --lock-name is unaffected by this change and fine to keep.

### A-13 [HIGH] HTTP-boot verification hands the LLM-written server the full operator env, including provider API keys

**Where:** `verify.py:553`  ·  lens: verify-stack+security  ·  verified: CONFIRMED

**Evidence:** `_verify_http` builds the child env as `env = dict(os.environ)` (line 553) plus `env["PORT"]`, then `subprocess.Popen(argv, cwd=cwd, env=env, ...)` (line 570), where `argv` runs `npm start` / `npm run dev` / `uvicorn <mod>:app` / `python3 <file>` from `_detect_start` — all agent-authored code (`npm start` executes whatever the generated package.json points at).

The npm path deliberately does the opposite: `_npm_env` (line 686) builds `{k: v for k, v in os.environ.items() if not _is_secret_env(k)}`, with the comment "npm install runs arbitrary LLM-chosen dependency code; scrubbing these from the child env means a malicious/curious postinstall can't read our provider keys straight out of the environment." The identical threat applies to the boot command, but the scrub is missing there. `procutil.is_secret_env` covers AWS_*, GITHUB_*, GH_*, NPM_*, STRIPE_*, *_TOKEN, *_SECRET, *_PASSWORD — none of which run.sh (which unsets only the four provider vars, run.sh:29-31) or the GUI launcher (RunController.swift:56-59, same 8-name floor list) removes. So a generated server boots with the …[trimmed]

**Fix (verifier-corrected):** Fix as written is correct and one line. Add: keep PORT (already set on 554) and be aware the scrub also drops *_SESSION/*_CREDENTIAL-shaped vars, so if a generated server legitimately needs a token the boot will now fail with a clearer 'did not respond' — acceptable, and identical to the precedent _npm_env already sets. Severity lowered to low for a local single-user dev tool: the leaked values are the operator's own env, and the same env is already inherited by the agent CLIs the engine spawns.

### A-14 [MEDIUM] CI clean-tree guard uses `git diff --exit-code`, which is blind to untracked files — the gate cannot catch the mutation tests actually make

**Where:** `.github/workflows/ci.yml:25`  ·  lens: tests-hygiene+scripts-ci  ·  verified: CONFIRMED

**Evidence:** The guard step is `run: git diff --exit-code`, which only detects modifications to TRACKED files. tests/test_conductor_situations.py::test_unknown_ref_is_visible_and_fails_open (line ~129) calls the real conductor._read_project_situation, which calls sitlib.load_situation(ref, os.path.dirname(os.path.abspath(__file__))) (conductor.py:1219-1221) — i.e. the repo checkout dir. situations.load_situation calls ensure_seeded first (situations.py:115), and ensure_seeded (situations.py:335-353) writes six situations/<name>/situation.json files under the repo root when absent. `git check-ignore situations/brainstorm/situation.json` exits 1 (not ignored; verified, and .git/info/exclude has no entry), so in CI these appear as UNTRACKED files — `git diff --exit-code` exits 0 regardless. The step is literally named 'Tests must not mutate the repo' but cannot see this mutation, and it will stay silent even after the 3 known test failures are fixed. (Local evidence of the same class: the repo's locks/ dir contains test residue like app-a.lock.guard, myapp.lock.guard, …[trimmed]

**Fix (verifier-corrected):** The proposed fix is correct and I verified its preconditions: (1) the `git status --porcelain` guard is safe because the repo .gitignore already covers the other artifacts a CI test run creates (__pycache__/, *.pyc, logs/, locks/ — .gitignore lines 2-6), so situations/ is the only thing it will newly flag; keep the suggestion to add the same step after the doctor smoke test. (2) Patching `situations.ensure_seeded` (or situations.load_situation) in test_unknown_ref_is_visible_and_fails_open is sufficient today — I checked every other test touching seeding (test_commands seeds orch.HERE but commands.json is TRACKED so ensure_seeded no-ops on a fresh checkout; test_situations/test_workflows_schema/test_snippets/test_sections all seed temp dirs). A third, house-pattern-consistent option worth offering: commit the six seed situations/<name>/situation.json files (exactly what commands.json already does for cmdlib.ensure_seeded — tracked seed makes first-read seeding a no-op and the 'disk wins' contract is preserved); that fixes CI without touching the test. Severity downgraded to medium: it is a guard blind spot plus benign untracked pollution in a local single-user tool, not a runtime defect.

### A-15 [MEDIUM] state['over_quota'] goes stale forever when budgets are removed from the manifest — routes to that provider defer indefinitely

**Where:** `conductor.py:1126`  ·  lens: conductor-stack  ·  verified: CONFIRMED

**Evidence:** state['over_quota'] is recomputed ONLY inside `if budgets:` in evaluate_terminations (conductor.py:1119-1126), and evaluate_terminations itself only runs when the manifest enables at least one layer (full_poll line 1454-1456: `if (manifest.get("goal") or manifest.get("quiescence_cycles") or manifest.get("budgets") or manifest.get("stall"))`). If an operator deletes budgets (or the whole goal_manifest.json — 'missing' status loads SAFE_DEFAULT with budgets None), the previously persisted over_quota list survives in conductor_state.json forever. route_engine keeps reading it (line 1766: `over_quota = set(state.get("over_quota") or [])`) and defers every route whose target section last ran on that provider (lines 2634-2643, 1871-1881) — with no daily budget_check ever running again to clear it. Result: removing budgets, which should un-cap routing, instead permanently defers routes to the flagged provider, re-ledgering route_deferred each poll.

**Fix (verifier-corrected):** Proposed fix is correct and minimal: clear over_quota in evaluate_terminations when budgets is falsy, plus an else-branch in full_poll for the no-layer-enabled case (guard the save with `if state.get("over_quota")` to avoid a per-poll state write). Note the symmetry with claim 3's corrected fix — both need the 'budgets removed' branch; implement them together in the same block.

### A-16 [MEDIUM] config.yaml `rounds:` block (22 keys) is dead — the legacy fallback at orchestrator.py:8868 is unreachable

**Where:** `config.yaml:132`  ·  lens: seeds-config  ·  verified: CONFIRMED

**Evidence:** orchestrator.py:8865-8868: `raw_rounds = (phasedef.get('rounds') if hasattr(phasedef, 'get') else None); ... max_rounds = int(raw_rounds if raw_rounds is not None else cget(cfg, 'rounds.%s' % key, 3))` with the comment 'fall back to the legacy config rounds: block'. But phasedef is always a workflows.Phase (sole call site orchestrator.py:11401 iterates workflow.phases; grep shows no Phase construction outside workflows.py/tests), and Phase.from_json (workflows.py:176) defaults missing rounds to 6 while Phase.__init__ stores `self.rounds = int(rounds)` — so phasedef.get('rounds') is ALWAYS an int, never None, and the cget fallback can never execute. Probe confirmed the values actively disagree: 67 phase entries across workflows/*.json carry explicit rounds (3-6) while config.yaml sets 9 for the same keys (e.g. app_build.json initial_discussion rounds=3 vs config rounds.initial_discussion=9). config.yaml:129-131's claim 'Max rounds per phase ... a phase caps at 27 bot messages (9 rounds x 3)' is false — editing this block does nothing; the live knob is workflows/<name>.json (which the …[trimmed]

**Fix (verifier-corrected):** The fix is right but INCOMPLETE: deleting the config.yaml `rounds:` block breaks tests/test_miniyaml.py::TestRealConfig::test_top_level_rounds_map, which parses the repo's real config.yaml (setUpClass, line 63-65) and asserts cfg['rounds'] is a non-empty str->int map containing 'prompt_contract' (lines 100-112). Either repoint that regression test at another nested int map already in config.yaml (e.g. runtime.global_worker_cap is asserted elsewhere — pick a different same-shape block or add a tiny synthetic fixture) or keep a minimal 2-3 key rounds-shaped block in config.yaml clearly labeled as a parser fixture, not a live knob. Then: (1) orchestrator.py:8868 -> max_rounds = int(raw_rounds if raw_rounds is not None else 3) and rewrite the comment at 8862-8865; (2) remove/replace the config block. Run: python3 -m unittest tests.test_miniyaml tests.test_workflows_schema tests.test_workflow_overrides

### A-17 [MEDIUM] Design lint hard gate misses common hardcoded color/font shapes (Font.system, CGFloat(), Color(hue:/white:/.sRGB))

**Where:** `designlint.py:37`  ·  lens: verify-stack  ·  verified: CONFIRMED

**Evidence:** Probe against the shipped regexes: `.font(Font.system(size: 24, weight: .bold))` -> not flagged; `let f = Font.system(size: 24)` -> not flagged; `.font(.system(size: CGFloat(16)))` -> not flagged; `Color(hue: 0.6, saturation: 0.8, brightness: 0.9)`, `Color(white: 0.95)`, `UIColor(white: 0.2, alpha: 1)`, `Color(.sRGB, red: 0.1, ...)` -> all not flagged. QUALITY_RULES.md line 70 claims '§3.5 shared design tokens, no scattered color/font literals' is 'GATED (designlint, hard error)'. _RAW_FONT requires the literal `.font(` prefix followed directly by `.system(` and a digit right after `size:`; _INLINE_COLOR only matches `red:` as the first argument. (The recently committed token fix c9f217f does work: `size: DS.IconSize.tab` is correctly not flagged, pinned by tests/test_quality_rules.py.)

**Fix (verifier-corrected):** The claim's replacement regexes are correct — I validated both against all the flag cases above AND the must-not-flag cases (`size: DS.IconSize.tab`, `Font.system(size: DS.Type.body)`, `Color(.systemBackground)`, `Color("AccentColor")`, `Color(DS.accent)`): every case behaves as intended. Adopt as written, plus the test cases in tests/test_quality_rules.py.

### A-18 [MEDIUM] designlint's 'test'/'preview' substring exemption matches inside ordinary words — an app named e.g. 'Contest' gets ALL product source exempted and can false-fail the launch-screen check

**Where:** `designlint.py:186`  ·  lens: verify-stack  ·  verified: CONFIRMED

**Evidence:** `test_file = "test" in rel.lower() or "preview" in rel.lower()` matches the whole build-relative PATH as a substring: 'contest', 'latest', 'protest', 'testament' all contain 'test', so an app whose project/group folder is named e.g. ContestTracker (rel = 'ContestTracker/Sources/HomeView.swift') exempts every source file from inline_color/raw_font_size/empty_action — the hard gate is silently vacuous for that app. The inverse also bites: the launch-screen scan excludes Info.plist files via `"test" not in low` (line 241), so such an app's ONLY Info.plist is never read; if its UILaunchScreen key lives only in the plist (not pbxproj/project.yml), the deterministic hard error missing_launch_screen fires on a correctly configured app and burns repair rounds.

**Fix (verifier-corrected):** The proposed component/token predicate is directionally correct (case-sensitive 'Test'/'Preview' tokens don't match 'ContestTracker'/'LatestNews'; whole-component lowercase 'test(s)'/'preview(s)' still exempt conventional dirs; basename check catches FooTests.swift/Foo_Previews.swift). One note: lowercase names like 'test_helpers.swift' would no longer be exempt under the proposal — acceptable for Swift (the convention is FooTests.swift), but add it as an explicit test case so the behavior change is pinned. Apply the same predicate at both line 186 and the Info.plist exclusion at line 241, as proposed.

### A-19 [MEDIUM] Loaded-once routing snapshots in ModelsAgentsView and AppShellView clobber newer edits: any single-field change writes the entire stale ModelRouting back to disk

**Where:** `gui/Sources/OrchestratorGUI/ModelsAgentsView.swift:59`  ·  lens: gui-swift  ·  verified: CONFIRMED

**Evidence:** ModelsAgentsView loads routing ONCE into @State ('.onAppear { if !loaded { routing = store.readModelRouting(); loaded = true } }', lines 50-56) and persistRouting() (59-61) then writes that whole object via store.writeModelRouting on every chain edit. The 'loaded' guard means the snapshot is never refreshed for the lifetime of the view identity, including reappearances. Identical pattern in AppShellView: the project tuning editor binding writes the entire snapshot on any per-phase change ('routing.phases[key] = ...; store.writeProjectRouting(routing, for: project)', lines 1731-1734) with the same 'guard !loaded' onAppear (1762-1767), and the per-project fallback-chain editor does the same (1870-1885). Failure scenario: open Library > Models & Agents (snapshot taken) -> go to Settings > Defaults grid, edit phase routing, Apply -> return to Models & Agents (onAppear does nothing: loaded==true) -> add one fallback step -> writeModelRouting persists the pre-grid-edit phases, silently reverting the grid's Apply. Same clobber against Inspector cell edits, applyProfile output, hand edits, …[trimmed]

**Fix:** In both views, before persisting, re-read the file and merge only the fields the view owns (the pattern already used by ModelRoutingSections in ModelLibrary.swift:450-459): for ModelsAgentsView.persistRouting, 'var current = store.readModelRouting(); current.chains = routing.chains; store.writeModelRouting(current)' (plus whatever other sections that view edits). For AppShellView's tuning binding, re-read readProjectRouting(project), replace only phases[key] (and only chains[agent] in the chain editor), then write. Alternatively/additionally drop the 'loaded' guard and reload the snapshot on every onAppear. Verify with a unit test that simulates two editors interleaving writes and asserts no field regression.

### A-20 [MEDIUM] saveProfile / applyProfile / rateProject log success even when their try? writes failed — silent data loss presented as 'Saved'

**Where:** `gui/Sources/OrchestratorGUI/OrchestratorStore.swift:2946`  ·  lens: gui-swift  ·  verified: CONFIRMED

**Evidence:** saveProfile (2927-2950): 'try? data.write(to: url, options: .atomic); runLog += "Saved profile ..."' — the success line is unconditional on the write result. applyProfile (2953-2972): both writes are try? ('try? out.write(to: routingURL...)' at 2963, 'try? (wf...).write(...)' at 2967) yet it always logs 'Applied profile ... to ...' at 2970 — a user creating a project from a profile gets default routing with no warning. rateProject (2981-3002): 'try? data.write(to: url, options: .atomic)' then unconditional 'runLog += "Rated ..."' and, for verdict==good, launches --save-exemplar even if rating.json never landed, so fleet learning (presort/anti-pattern ledger reads rating.json) silently disagrees with what the user was told. This contradicts the store's own documented contract two screens down: writeJSON (4791-4795) exists precisely because 'a lost setting looked saved and silently reverted' and 'callers that mutate state on the strength of a save MUST check the result'.

**Fix (verifier-corrected):** Direction is right but one correction: the suggested writeJSON(_:to:) helper writes with `try data.write(to: url)` — NO .atomic — while all three current sites use .atomic. Routing them through writeJSON as-is would trade silent failure for lost atomicity. Either add `.atomic` to writeJSON's write (strictly safer for its existing callers too) or keep per-site do/catch with `.write(to:options:.atomic)` plus surfaceError, gating the runLog success line and the --save-exemplar launch / workflow.txt write on success. The read-only-directory unit test suggestion stands.

### A-21 [MEDIUM] setRuntimeInt/setRuntimeBool missing-key insert fallback is dead code: searches for literal 'runtime:\n' (backslash-n) which never occurs, so settings silently vanish when config.yaml lacks the key

**Where:** `gui/Sources/OrchestratorGUI/OrchestratorStore.swift:3397`  ·  lens: gui-swift  ·  verified: CONFIRMED

**Evidence:** Lines 3397 and 3412 both read: '} else if let runtimeRange = text.range(of: "runtime:\\n") {'. In Swift source, "runtime:\\n" is the ten characters 'runtime:' + backslash + 'n' — String.range(of:) does a LITERAL substring search, and a YAML file never contains 'runtime:\n' as literal backslash-n. Verified against the sibling correct sites: lines 2708 and 4060 use "models:\n" (real newline) — sed -l output shows the raw bytes differ ('models:\n' vs 'runtime:\\n'). Consequence: when the regex at 3391/3406 finds no existing key, the insert branch never fires, yet writeConfig(text) still runs with UNCHANGED text and the caller's UI state updates — the toggle looks saved but nothing reached disk, with no error surfaced (unlike setAgentEnabled, which surfaces a missing key at 2278-2280). Realistic trigger: config.yaml files predating recently added keys — phase_independent_first_round_enabled / phase_quality_gates_enabled / phase_quality_repair_rounds (Configuration.swift:964-994) were added in recent commits, and the sibling comment at 4061 ('Older engine copies predate the key ... …[trimmed]

**Fix (verifier-corrected):** Fix as stated is correct: delete one backslash in each literal (lines 3397 and 3412) so it reads "runtime:\n", matching the models: pattern. The suggested unit test is apt. Optional hardening: anchor the search to a line start ((?m)^runtime:\n via regex) so a hypothetical '*_runtime:' key can't match, mirroring how the models: sites are equally vulnerable-but-unproblematic today.

### A-22 [MEDIUM] build_app.sh ships the builder's working-tree model_routing.json while explicitly shielding config.yaml for the identical reason

**Where:** `gui/build_app.sh:68`  ·  lens: scripts-ci  ·  verified: CONFIRMED

**Evidence:** Lines 68-80 special-case config.yaml: 'it's the file the GUI's Settings panel writes into ... Ship the clean, git-tracked version instead so a distributed .app/DMG never bakes in whoever built it's local settings' — and bundle `git show HEAD:config.yaml`. But model_routing.json, which the GUI equally rewrites (the working tree copy is currently GUI-rewritten and lost its `_examples` block — `git status` shows ` M model_routing.json`, and the known test_migrate_v3 seed-hash failure exists precisely because the working-tree file diverged from the HEAD pin), is copied verbatim from the working tree by the generic find loop. A distributed DMG therefore bakes in the builder's personal model roster/routing, and — because the seed-hash pin tracks HEAD — ships a file the engine's own migration check considers drifted. Any situations/ dirs the builder's GUI created at the engine root would ship too.

**Fix (verifier-corrected):** Fix is correct as written (mirror the config.yaml HEAD-restore block for model_routing.json, or adopt git-archive bundling which fixes this class wholesale). The diff-based verification command is right. Minor note: `-not -name 'model_routing.json'` matches by basename anywhere in the tree, consistent with the script's existing documented over-exclusion policy (lines 45-52), so it needs no path scoping.

### A-23 [MEDIUM] migrate_v3._baseline trusts the engine working tree unverified — pristine external workspaces get phantom tuned deltas today

**Where:** `migrate_v3.py:108`  ·  lens: v3-stack  ·  verified: CONFIRMED

**Evidence:** _baseline's docstring promises 'Immutable shipped JSON', but for workspace != HERE it returns _read_json(os.path.join(HERE, rel)) with NO SEED_HASHES verification (migrate_v3.py:106-111) — the hash check exists only on the git-show path for workspace == HERE (line 115). The engine's working-tree model_routing.json is currently GUI-rewritten (git diff: 15 insertions, 35 deletions; lost _examples and fallback.chains; sha 2537fcb0... vs pinned a84bbacd...). Reproduced: built a scratch workspace whose 20 files are byte-identical to the pinned HEAD seeds, ran migrate_v3.build_plan(ws) — 19 files correctly report seed_identical, but model_routing.json reports THREE phantom tuned deltas ('/fallback', '/_docs', '/_examples') and is queued for backup, because the deltas were computed against the drifted engine file. Any real engine-file drift that touched `phases` would go further and migrate wrong per-phase entries into sections/<sec>/model_routing.json.

**Fix (verifier-corrected):** The proposed fix (hash-verify the engine copy in the workspace != HERE branch; return None on mismatch, degrading to whole-doc carry / seed-identical shortcut) is correct and safe. One improvement worth adding: on hash mismatch, before returning None, try `git show HEAD:rel` with cwd=HERE (hash-verified against SEED_HASHES, same as the existing workspace==HERE path) — as this session proves, the common failure is a dirty engine working tree over a pristine HEAD, and the git fallback preserves precise per-delta migration for external workspaces instead of degrading tuned files to whole-document carry. Keep the proposed test.

### A-24 [MEDIUM] migrate_v3.apply_plan validates AFTER writing all targets — a lint failure leaves a half-migrated workspace while printing APPLY REFUSED

**Where:** `migrate_v3.py:681`  ·  lens: v3-stack  ·  verified: CONFIRMED

**Evidence:** apply_plan writes every target file first (`for rel, value in sorted(plan.get("targets", {}).items()): _atomic_json(...)`, lines 667-671), and only THEN runs the loader/lint validation (lines 674-682), raising MigrationError('migrated section %r failed lint: ...') on failure. main() catches it and prints 'APPLY REFUSED' (lines 710-712) — but at that point all target files, including overwritten pre-existing sections/<sec>/section.json manifests, are already on disk with no receipt and no rollback. Backups cover only the SOURCE rels (plan['backups'] = tuned workflows + phase_rules + model_routing, lines 663-665) — pre-existing TARGET files that were merged-into and rewritten are never backed up, so their prior bytes are unrecoverable. Reachable in practice: a custom (unmapped-name) tuned workflow is embedded inline via _section_stub, and lint_section errors on it (e.g. an inline workflow whose phases fail Workflow.from_json, or a dangling cast ref) after the write.

**Fix (verifier-corrected):** The two proposed changes (back up every existing target rel into backup_manifest before the write loop; on validation failure restore each written rel from its .v2.bak / remove targets that had none, then re-raise) are correct. Add a third: wrap the validation loop's load_section/lint_section calls in `except (ValueError, MigrationError)` and convert to MigrationError after rollback — load_section raises bare ValueError (sections.py:281,284,300,304), which today bypasses main()'s handler entirely. Keep the proposed tree-hash-unchanged test.

### A-25 [MEDIUM] Keyless gemini fallback path omits --skip-trust, so the startup probe validates a different invocation than the runtime uses

**Where:** `orchestrator.py:1674`  ·  lens: orch-core-a  ·  verified: CONFIRMED

**Evidence:** The keyed path builds `cmd = ["gemini", "--skip-trust"]` (line 1623) with an explicit comment that agents run in temp/app_build dirs the CLI treats as untrusted. The availability probe detect_gemini_available also uses `["gemini", "--skip-trust", "-p", ...]` (line 1569) — including in the KEYLESS case (env has no key when none is configured). But the keyless runtime path builds `cmd = ["gemini"]` (line 1674) without the flag. Result: the probe can pass keyless (with trust skipped) and enable gemini for the run, while every actual keyless turn launches the CLI in an untrusted ephemeral tempdir (from _agent_cwd's mkdtemp, line 1403) where it can emit a trust prompt headless — and since the prompt text is piped on stdin, the trust dialog may consume prompt lines as its answer or block until the no-output heartbeat kills the turn. The existing test test_keyless_fallback_passes_prompt_on_stdin (tests/test_sweep_fixes.py:198) asserts cmd[0]=="gemini" but never asserts the flag, so the drift is uncovered.

**Fix (verifier-corrected):** Fix as claimed is correct and complete: change line 1674 to `cmd = ["gemini", "--skip-trust"]`, and add `self.assertIn("--skip-trust", captured["cmd"])` to both test_keyless_fallback_passes_prompt_on_stdin and test_api_key_path_passes_prompt_on_stdin in tests/test_sweep_fixes.py. Verify: python3 -m unittest tests.test_sweep_fixes

### A-26 [MEDIUM] Tasks claimed by a worker that fails every iteration are never reverted or redistributed — the build 'proceeds' while its tasks are silently never built

**Where:** `orchestrator.py:5951`  ·  lens: orch-core-c  ·  verified: CONFIRMED

**Evidence:** _claim_tasks_for_iteration reverts a claim only when `cb not in roster_slugs` (line 5951), and the roster is computed exactly once before the iteration loop (line 6616, `roster = build_worker_roster(cfg, active)`), with availability checked only at that moment (_agent_available checks CLI-installed, not logged-in/capped). A worker whose CLI is installed but whose every call_agent_sessioned raises AgentError (logged out, 5-hour cap with fallback ladder exhausted) stays in roster_slugs forever, so its sticky claims are never cleared — each iteration it is 'skipped: CLI unavailable' (line 6784) while the tasks it claimed are excluded from open_tasks (line 5965, claimed => not redistributable) and every other worker is told '(nothing claimed for you this iteration — support the other lanes)' (line 6047). The docstring's promise that 'a dropped-out worker's tasks don't sit permanently orphaned' (lines 5930-5934) only covers roster-membership changes, which it itself admits never happen today ('today's roster is fixed for the whole build phase'). Net effect: one dead CLI orphans its whole …[trimmed]

**Fix (verifier-corrected):** The proposed fix works but has two gaps. (1) Thrash: reverting on any single failed iteration breaks the sticky-claim/session-continuity design for transient errors (one timeout would reshuffle a worker's whole slice). Track consecutive failures per slug (e.g. `_fail_streak[slug]`) and revert only when a worker has failed >= 2 consecutive iterations, keeping the `produced > 0` guard. (2) Vertical slices: the next iteration's `_claim_tasks_for_iteration(roster, _wave_backlog, rnd)` only sees the CURRENT wave, so reverted tasks from an earlier wave would never be re-handed out — after reverting, also extend that iteration's claim pool with reverted-but-unfinished tasks from earlier waves (or run the revert-redistribution over the full backlog). Emit the CLAIM: reverted banner as proposed. Test in tests/test_fleet_quality.py as proposed.

### A-27 [MEDIUM] Vertical-slice waves beyond the build iteration budget are never scheduled — contradicts the in-code contract that they 'collapse into the final iteration'

**Where:** `orchestrator.py:6705`  ·  lens: orch-core-c  ·  verified: CONFIRMED

**Evidence:** Comment at lines 6632-6634: 'iteration k works wave k ... Waves beyond the iteration budget collapse into the final iteration.' Code at 6705-6706: `_widx = min(rnd - 1, len(waves) - 1); _wave_backlog = waves[_widx]` — the min() only clamps ITERATIONS beyond the wave count; it never merges remaining waves into the final budgeted iteration. `_wave_backlog` is the sole source for both _claim_tasks_for_iteration (line 6718) and _worker_contract_block (line 6731), and _wave_note explicitly tells workers 'iteration %d works WAVE %d/%d ONLY'. Reproduced: with a 6-task dependency chain (6 waves, backlog >= 4 so vertical slices activate — default-on via runtime.build_vertical_slices=True) and the vslice workflow's build_coordination rounds=2 (workflows/vslice.json), tasks t2..t5 are never claimed and never appear in any worker prompt: worked={t0,t1}, never-scheduled={t2,t3,t4,t5}. iterate.json (rounds=4) and any _round_multiplier-shrunk budget hit the same hole. The tasks stay claimed_by=None in tasks.json and no banner is emitted.

**Fix (verifier-corrected):** The proposed fix is sound: keep `_widx = min(rnd - 1, len(waves) - 1)`, and when `not unlimited_rounds and rnd == max_rounds and _widx < len(waves) - 1`, set `_wave_backlog = [t for wv in waves[_widx:] for t in wv]` with a 'final iteration works ALL remaining waves W%d-W%d' note (_done_n formula `sum(len(wv) for wv in waves[:_widx])` is already correct for both branches). Two additions: emit an observability banner when the merge fires (e.g. 'Vertical slices: budget %d < %d waves — final iteration takes waves W%d-W%d'), and note the sprint-deadline break (6665-6667) can still exit before rnd == max_rounds, losing waves — acceptable for a time-budget stop but worth a WARN listing never-claimed task ids at loop exit. Test as proposed in tests/test_fleet_quality.py.

### A-28 [MEDIUM] Crash-resume after a recorded consensus re-runs the phase decision as a forced vote (no consensus reconciliation, unlike the vote path)

**Where:** `orchestrator.py:9145`  ·  lens: orch-core-b  ·  verified: CONFIRMED

**Evidence:** The V3 2.4 resume gate (orchestrator.py:9134-9153) reconciles only a crash-left FORCED VOTE section; nothing re-checks the recovered transcript for a coordinator 'CONSENSUS: YES'. Reproduced with the real functions: for a transcript whose final round (rnd == max_rounds) ends with '**Coordinator (Claude) — decision after round 2**\n\nCONSENSUS: YES', `_resume_round_state` returns resume_round=3, so rounds_iter = range(3,3) is empty, `consensus` seeds False (line 9174), `_recovered_vote` is None, and the gate at line 9244 (`elif not consensus and not unlimited_rounds and not is_build and len(available_active) >= 2:`) re-runs `_run_forced_vote` — probe output: 'case1 resume_round: 3 … case1 would force vote: True'. The crash window is real and long: consensus break → `_PHASE_CLOSE_HOOKS` (verify can run xcodebuild for minutes; `_record_phase_contracts` makes additional agent repair calls) → footer → completed_phases save at line 9303. Consequences on resume: (a) a vote is cast that can pick a different winner than the recorded consensus; (b) `last_substantive` is empty (fresh dict at …[trimmed]

**Fix (verifier-corrected):** The proposed fix is sound for the case it targets but note two refinements. (1) As written it only covers consensus on the FINAL budgeted round (guard: resumed rounds_iter empty / resume_round > max_rounds); a crash after a consensus on a NON-final round still silently re-debates the remaining rounds. That is only duplicate spend, not a wrong decision, so it is acceptable to leave — but say so in the test/comment rather than implying full coverage. (2) Adopting a recovered CONSENSUS: YES skips the quality gate the original run may have been mid-way through (run_phase_quality_gate can veto consensus at 8054-8056 only when rnd < max_rounds, so on the final round consensus survives gate failure anyway — the adoption is faithful there; document that this mirrors the _recover_forced_vote precedent 'a decision already on disk stands'). Scope the CONSENSUS_RE search to the text from the LAST _COORD_DECISION_RE match to end of _kept as proposed, so an agent merely quoting 'CONSENSUS: YES' earlier in the round cannot false-positive. The suggested tests (no '### Forced Vote' appended, footer marker CONSENSUS: YES) are the right assertions; run python3 -m unittest tests.test_round_resume tests.test_transcript_golden.

### A-29 [MEDIUM] Run-start crash: raw target-policy tech_stack reaches render_tech_stack unsanitized (validator admits entry shapes the renderer cannot handle)

**Where:** `orchestrator.py:11261`  ·  lens: seeds-config  ·  verified: CONFIRMED

**Evidence:** orchestrator.py:11259-11262 passes the manifest dict straight through: `tctx.tech_stack_block = dlintlib.render_tech_stack(policy['tech_stack'] if policy is not None else dlintlib.load_tech_stack(HERE))`. buildpolicy._validate_policy (buildpolicy.py:73-77) only checks tech_stack.allowed/banned are lists and notes is a str — entry SHAPES are never validated. designlint.load_tech_stack sanitizes entries (`[e for e in v if isinstance(e, dict) and e.get('name')]`, designlint.py:75-79) but this call site bypasses it when a Sections tree exists (which it does on this checkout). render_tech_stack does `e['name']` unconditionally (designlint.py:93,98). Reproduced: designlint.render_tech_stack({'allowed': ['GRDB'], 'banned': [], 'notes': ''}) -> TypeError: string indices must be integers — and {'allowed': [{}], ...} raises KeyError. So a hand/GUI edit of sections/build/target_policy.json that writes string entries (a natural shorthand) passes buildpolicy validation, is cached as VALID (so the seed fallback never engages), and then crashes every app-workflow run at startup.

**Fix (verifier-corrected):** The proposed validator extension is correct but incomplete: checking only that entries are dicts with a non-empty 'name' still admits {'name': 'GRDB', 'for': 123}, which crashes render_tech_stack the same way (designlint.py:92-93 does ' — ' + e['for'] when e.get('for') is truthy; TypeError on non-str). Extend the loop to also require the optional 'for'/'why' (and 'url_contains') fields to be strings when present: for e in stack[k]: if not isinstance(e, dict) or not str(e.get('name', '')).strip() or any(f in e and not isinstance(e[f], str) for f in ('for', 'why', 'url_contains')): raise ValueError(...). Add the string-entry test asserting the seed fallback engages, then run: python3 -m unittest tests.test_build_policy

### A-30 [MEDIUM] Fleet ledger rebuilt from the wrong root for nested sessions — clobbers knowledge/anti_patterns.md with section-scoped data

**Where:** `orchestrator.py:11631`  ·  lens: orch-core-d  ·  verified: CONFIRMED

**Evidence:** After a run is marked done, _run_app_pipeline refreshes the fleet anti-pattern ledger with `_lpath, _lclusters = fllib.build_ledger(os.path.dirname(os.path.abspath(app_dir)), HERE)` (lines 11630-11631). But the same function establishes 130 lines earlier that dirname is wrong for nested sessions: `# Nested sessions live two levels down — dirname(app_dir) is NOT the workspace root for them; cfg["root"] is authoritative.` with `root = cfg.get("root") or os.path.dirname(app_dir)` (lines 11076-11078). For a nested session <root>/<project>/<section>/<chat>, dirname(app_dir) is <root>/<project>/<section>. fleetlearn.build_ledger (fleetlearn.py) lists only the DIRECT children of the root it is given and aggregates their incidents/ratings, then unconditionally rewrites knowledge/anti_patterns.md in the engine checkout and returns the cluster count — so a finished nested-session run regenerates the fleet-wide ledger from only that one section's chats, silently discarding every other project's incident clusters (the ledger is the repo's own tracked knowledge/anti_patterns.md, injected into …[trimmed]

**Fix (verifier-corrected):** The proposed fix is correct as written: line 11631 is inside _run_app_pipeline (starts 11057, no intervening def), so the `root` variable bound at 11078 is in scope — replace the dirname expression with `fllib.build_ledger(root, HERE)`. The follow-up observation is also accurate (build_ledger's os.listdir scan is one level deep, so nested chats' own incidents stay invisible even with the correct root — a separate fleetlearn.py issue). Run: python3 -m unittest tests.test_fleet_quality

### A-31 [MEDIUM] One project's unexpected crash kills the entire --watch loop and aborts the rest of a sequential pass

**Where:** `orchestrator.py:12329`  ·  lens: orch-core-d  ·  verified: CONFIRMED

**Evidence:** process_app deliberately re-raises unexpected exceptions after recording the crash: `except Exception as exc:  # noqa: BLE001 - preserve worker propagation ... raise` (lines 10437-10450). That propagation is only caught in run_once's PARALLEL branch (`fut.result()` wrapped in try/except at lines 12335-12340, which emits 'unexpected worker failure' and continues). The sequential branch — the DEFAULT, since _project_parallel_workers defaults to runtime.project_parallel_workers=1 (line 10518) — has no containment: `if workers <= 1 or len(apps) <= 1: for app in apps: process_app(dict(cfg), root, app)` (lines 12328-12331). And main()'s watch loop has no try/except either: `while True: ... run_once(cfg) ... time.sleep(args.watch)` inside a try/FINALLY only (lines 12772-12791). So with the default single-worker config, any unexpected exception in one app (state-save OSError, a bug in any phase helper, etc.) (a) aborts processing of every remaining app in the pass and (b) terminates the whole --watch daemon with a traceback — contradicting the CLI contract '--watch SECONDS: loop forever, …[trimmed]

**Fix (verifier-corrected):** The proposed fix is correct and complete as written (both edits are needed: the run_once sequential wrap alone would not cover the --watch --app branch, and `except Exception` correctly lets _cleanup's SystemExit through). Only refinement: severity is medium, not high, for a local single-user tool — the failure is loud (traceback), state is preserved, and restart is trivial; and with this repo's config (project_parallel_workers: 3) multi-app passes are already contained. Place the new test (stub process_app to raise for app 1 of 2, assert app 2 still processed) in its own module or tests/test_commands.py; tests.test_release_gate/test_shepherd_lock are unrelated smoke.

### A-32 [MEDIUM] run.sh secret-commit guard misses *_api_key, *.gemini_api_key and *.secret files — they would be auto-committed and pushed

**Where:** `run.sh:66`  ·  lens: scripts-ci  ·  verified: CONFIRMED

**Evidence:** The guard regex is `(^|/)(gemini_api_key|config\.json|\.env[^/]*)$|\.(pem|key|p12)$`. I piped candidate basenames through the exact regex: `openai_api_key`, `my_api_key`, `foo.gemini_api_key`, and `client.secret` are NOT matched (grep output verified), so after `git -C "$ROOT" add -A` they stay staged and the script commits and pushes them to origin. This contradicts the repo's own definitions of secret-shaped names: .gitignore line 35 pins `*.gemini_api_key`, and gui/build_app.sh (which claims at line 44 to 'Mirror run.sh's commit block-list') excludes `*_api_key`, `.env.*` AND `*.secret`. A stray `openai_api_key` file dropped into the workspace by a tool or user would reach the remote.

**Fix (verifier-corrected):** The proposed regex is correct — I validated it: all of openai_api_key, my_api_key, foo.gemini_api_key, client.secret, gemini_api_key, .env.local, config.json, cert.pem (and sub/dir/openai_api_key) match; notes.md does not. Note `[^/]*\.gemini_api_key` is redundant (subsumed by `[^/]*_api_key` since the name ends in _api_key) but harmless. No changes needed.

### A-33 [MEDIUM] search.py never prunes a vanished session's artifact rows — deleted sessions return ghost search hits forever

**Where:** `search.py:322`  ·  lens: support-modules  ·  verified: CONFIRMED

**Evidence:** _prune_vanished derives the stale set only from the messages table: `stale = [p for (p,) in conn.execute("SELECT DISTINCT project FROM messages") if p not in live]`. A session whose messages.jsonl is empty (or whose only indexed content is artifacts) has zero messages rows, so after the session dir is deleted it is never considered stale and its artifacts/artifacts_fts rows persist. Reproduced in scratchpad: created <root>/myapp with an empty messages.jsonl plus one published artifact (body 'The zanzibar protocol design notes'), ran index_incremental (hits while live: 1), then shutil.rmtree'd the session and re-indexed — output: `tick2: {'projects': 0, ..., 'pruned': 0}` and `query(root,'zanzibar')` still returned `GHOST HIT: {'project': 'myapp', 'kind': 'artifact', 'turn_id': 'spec-001', ...}`. This directly contradicts _prune_vanished's own docstring: 'Migrated/removed sessions must leave the index (R2: a hit that cannot jump is a lie).' Additionally, even for projects that DO have messages rows, the prune loop deletes messages, messages_fts, artifacts, and cursors but never …[trimmed]

**Fix (verifier-corrected):** The proposed fix is correct and sufficient for the user-visible defect: in _prune_vanished (search.py:318-329) build stale as the union `{p for (p,) in conn.execute("SELECT DISTINCT project FROM messages")} | {p for (p,) in conn.execute("SELECT DISTINCT project FROM artifacts")}` minus `live`, and inside the loop add `if _has_artifact_fts(conn): conn.execute("DELETE FROM artifacts_fts WHERE project=?", (p,))` after the artifacts delete. One optional completeness addition: a project whose messages.jsonl contained only corrupt/non-turn lines stores a cursor row (_store_cursor fires on any non-blank line) but zero messages/artifacts rows, so its cursors row would still never be pruned; if desired, also union `{p.split("|", 1)[-1] if p.startswith("ev|") else p for (p,) in conn.execute("SELECT project FROM cursors")}` — harmless bytes either way. Verify with python3 -m unittest tests.test_search (currently 15 tests, all green) plus the artifact-only-session repro.

### A-34 [MEDIUM] test_unknown_ref_is_visible_and_fails_open seeds situations/ into the engine repo root (working-tree mutation)

**Where:** `tests/test_conductor_situations.py:129`  ·  lens: tests-hygiene  ·  verified: CONFIRMED

**Evidence:** The test calls conductor._read_project_situation(self.root, "project", shown.append) UNMOCKED. conductor.py:1219-1221 hard-codes the engine dir: sitlib.load_situation(ref, os.path.dirname(os.path.abspath(__file__)), ...), and situations.load_situation (situations.py:115) calls ensure_seeded(orch_dir) first, which materializes the six fleet seeds. Reproduced: `ls situations` -> exit 1 (absent); `python3 -m unittest tests.test_conductor_situations.LiveSituationSwitchTests.test_unknown_ref_is_visible_and_fails_open` -> OK; `ls situations` -> compliance_pass, full_production_app, launch_push, prototype_sprint, research_spike, v2_iteration. `git status --porcelain` showed `?? situations/` (untracked, NOT in .gitignore — verified with git check-ignore, exit 1). The two other tests touching this path are mocked (line 62 mocks _read_project_situation; line 157 mocks situations.load_situation), so this is the single seeding trigger. The seeded dir is live engine state that subsequent real runs read, and with the shared Codex checkout it is one `git add -A` away from being committed. I …[trimmed]

**Fix (verifier-corrected):** The claimed fix is correct as written; keep it. Severity note only: the seeded content is byte-identical to what any legitimate engine run materializes on first use by design (ensure_seeded is never-clobber/disk-wins, and conductor always passes the repo root as orch_dir), so the harm is untracked-tree pollution and accidental-commit risk (real given the shared Codex checkout and git add -A hazard), not state corruption — medium, not high, for a local single-user dev tool.

### A-35 [MEDIUM] test_section_capability_engine.py: 3 hook-driven regression tests execute in NO mode (new detail on known F811 — dual mid-file unittest.main() blocks)

**Where:** `tests/test_section_capability_engine.py:209`  ·  lens: tests-hygiene  ·  verified: CONFIRMED

**Evidence:** New detail beyond the known F811: the file has TWO mid-file `if __name__ == "__main__": unittest.main()` blocks (lines 143-144 and 205-206) before a third class definition. Measured: `python3 -m tests.test_section_capability_engine` runs only 7 tests (unittest.main() at line 143 sys.exits during module execution, before EITHER TestLibraryMiningBothGates class is defined — all 6 of those tests vanish in direct mode); `python3 -m unittest tests.test_section_capability_engine` runs 10 tests (the weaker helper-only class at 209 shadows the stronger class at 147 that drives the real orch._hook_library_mining). Net: the 3 hook-driven tests at lines 147-202 never run in ANY execution mode. I resurrected the shadowed class by exec'ing lines 1-202 and running it: all 3 tests PASS today, so no engine regression is being hidden and the cleanup is safe.

**Fix (verifier-corrected):** The fix's verification count is wrong: after deleting the weaker duplicate class at 209-246 the module has 3+2+2+3 = 10 tests, not 13 — `python3 -m unittest tests.test_section_capability_engine` would report 'Ran 10 tests'. Two correct options: (a) as claimed but expect 10 tests; or (b) preferable — RENAME the second class (e.g. TestLibraryMiningHelperGates) instead of deleting it, since its helper-level assertions on orch._section_writes_allowed are not covered by any other class (TestCapabilityHelpers only exercises _section_exec_allowed/_section_external_allowed), then delete the two mid-file main blocks (143-144, 205-206), add one `if __name__ == "__main__": unittest.main()` at end of file, and expect 'Ran 13 tests'. Either way also confirm `python3 -m tests.test_section_capability_engine` reports the same count as the unittest form.

### A-36 [MEDIUM] urlfetch's DNS-rebinding pin is inert: the pinned connection classes are never instantiated

**Where:** `urlfetch.py:345`  ·  lens: security  ·  verified: CONFIRMED

**Evidence:** `_PinnedHTTPHandler.http_open` (line 345) calls `self.do_open(_pinned_conn_factory(http.client.HTTPConnection, ip), req)` and `_PinnedHTTPSHandler.https_open` (line 356) calls `_pinned_conn_factory(http.client.HTTPSConnection, ip)` — both pass the STOCK http.client classes, not `_PinnedHTTPConnection`/`_PinnedHTTPSConnection` (defined lines 283/296). `grep -n "_PinnedHTTPConnection\|_PinnedHTTPSConnection" urlfetch.py tests/*.py` shows the two classes are referenced nowhere but their own definitions — dead code. `_pinned_conn_factory` sets `conn.pinned_ip = ip` on a stock object whose `connect()` ignores it.

Proved twice, offline:
(1) `python3 -c "import http.client,urlfetch; c=urlfetch._pinned_conn_factory(http.client.HTTPConnection,'203.0.113.9')('example.com',timeout=1); print(type(c).__name__, type(c).connect is http.client.HTTPConnection.connect, isinstance(c,urlfetch._PinnedHTTPConnection))"` -> `HTTPConnection True False`.
(2) end-to-end with getaddrinfo/create_connection stubbed so the host resolves to a public IP: `res = urlfetch.fetch_url('http://rebind.example/x')` -> …[trimmed]

**Fix (verifier-corrected):** Fix as written (swap the stock classes for _PinnedHTTPConnection/_PinnedHTTPSConnection at lines 345 and 356) — I verified do_open's `context=` kwarg still flows correctly into _PinnedHTTPSConnection and that TLS SNI stays on self.host. For the test, the recorded-address assertion in the claim is right, but also assert `calls.count('rebind.example') == 1` (one getaddrinfo per fetch) since that, not the address alone, is what the docstring promises. Severity lowered to medium: this is a local single-user dev tool, the static pre-check still blocks hostnames that resolve to private/loopback/metadata addresses, and exploitation needs an attacker-controlled short-TTL domain to reach fetch_url; only the rebinding TOCTOU window is open.

### A-37 [MEDIUM] _concrete_sim_destination returns a booted watchOS/tvOS simulator as the concrete 'iOS Simulator' test destination

**Where:** `verify.py:189`  ·  lens: verify-stack  ·  verified: CONFIRMED

**Evidence:** The booted-device scan iterates `runtimes.values()` with no runtime filter: `if d.get("state") == "Booted": booted = booted or d["udid"]` — `simctl list devices available -j` lists watchOS/tvOS runtimes too. Probe (monkeypatched _run with a Booted 'Apple Watch Series 10' under watchOS-11-0 plus a Shutdown available iPhone 16): returns `platform=iOS Simulator,id=WATCH-UDID`; a booted Apple TV likewise wins. A paired watch sim commonly boots alongside an iPhone, and dict order decides which UDID wins. xcodebuild test then rejects the destination as a harness refusal, so _verify_xcode records "(tests could not run: ...)" and tests_ran=False — with runtime.tests_gate_release=True the release gate passes without tests ever running, even though an iPhone simulator was available. Contrast visualqa.pick_simulator (line 145), which correctly skips `if "iOS" not in runtime`. This is distinct from the known stale fake_run stub failure in test_xcodebuild_tests.

**Fix (verifier-corrected):** Fix as proposed is correct: iterate `runtimes.items()`, `continue` unless 'iOS' in the runtime key (case-sensitive 'iOS' does not substring-match 'watchOS'/'tvOS'/'visionOS' — verified). tests/test_verify_tests_action.py exists for the new case.

### A-38 [MEDIUM] Auto-detected Python verification executes LLM-written test code unsandboxed with the full operator env

**Where:** `verify.py:385`  ·  lens: verify-stack+security  ·  verified: CONFIRMED

**Evidence:** `_verify_shell` ends with `code, out, err = _run(["/bin/sh", "-lc", command], build_dir, timeout)` (line 393) — no `_sandbox_wrap`, and `env` is left None so `procutil.run_capture` inherits the engine's whole environment. Its sibling verifiers both harden: `_verify_http` wraps the boot command (line 567 `argv, sandbox_profile_path = _sandbox_wrap(start, write_root=build_dir)`) and `_verify_web` runs everything through `_run_sandboxed` + `_npm_env` (lines 759, 779).

And this path does execute LLM-authored code: for `detect_project(...) == "python"` with a discoverable suite it builds `command = "python3 -m compileall -q . && python3 -m unittest discover -q"` (lines 384-388), i.e. it imports and runs the agents' test modules at module scope. So the one verifier that runs generated code as code is the one with neither the Seatbelt deny on ~/.ssh / ~/.aws / ~/.orchestrator / the engine source, nor the secret-env scrub. A generated `tests/test_x.py` can overwrite the engine's own .py files or ~/.orchestrator/*_api_key and read any non-scrubbed token out of os.environ. …[trimmed]

**Fix (verifier-corrected):** Fix as written is correct — `_run_sandboxed(command, build_dir, timeout, env=None, write_root=build_dir)` matches the signature `_run_sandboxed(cmd_str, cwd, timeout, env, write_root)`, _sandbox_wrap already re-wraps in /bin/sh -lc (so behavior is identical minus the sandbox), and forward reference at call time is fine. I verified the profile blocks writes to denied subpaths and that build_dir stays writable, so __pycache__/compileall output is unaffected. Two additions: (1) apply the escaping fix from the SBPL-interpolation claim FIRST, otherwise this newly routes another verifier through a profile that a quoted path can neuter; (2) also update KNOWN_LIMITATIONS.md's 'untested / unsandboxed paths' section, which currently implies the only code-executing verifiers are http and web. Severity lowered to low: this is a local single-user tool whose agent CLIs already run unsandboxed as the operator (the doc itself calls that 'the same trust boundary the engine already crosses'), so the incremental exposure is real but small.

### A-39 [MEDIUM] Seatbelt deny-list omits the agent CLIs' own credential/config dirs and shell rc files — sandboxed code can tamper with ~/.claude, ~/.npmrc, ~/.zshrc

**Where:** `verify.py:473`  ·  lens: verify-stack+security  ·  verified: CONFIRMED

**Evidence:** `_SANDBOX_DENY_WRITE_SUBPATHS = ("~/.ssh", "~/.aws", "~/.orchestrator", "~/.gnupg", "~/.netrc", "~/Library/Keychains")` plus the engine dir. Generated profile (printed by calling `verify._sandbox_wrap('echo hi', write_root='/tmp/proj')`):
```
(version 1)
(allow default)
(deny file-write*
  (subpath "/Users/pchordia/.ssh")
  (subpath "/Users/pchordia/.aws")
  (subpath "/Users/pchordia/.orchestrator")
  (subpath "/Users/pchordia/.gnupg")
  (subpath "/Users/pchordia/.netrc")
  (subpath "/Users/pchordia/Library/Keychains")
  (subpath "/Users/pchordia/Documents/core_apps/orchestrator")
)
(allow file-write* (subpath "/tmp/proj"))
```
Because the base rule is `(allow default)`, EVERYTHING else under $HOME stays writable. An `npm install` postinstall (verify.py:759, running arbitrary transitive-dependency code) or a booted generated server can therefore append to `~/.zshrc` / `~/.zprofile`, drop a plist in `~/Library/LaunchAgents` (the repo itself ships install_launch_agent.sh, so that dir is an established execution path), or write `~/.claude/settings.json` hooks / `~/.gitconfig` aliases — …[trimmed]

**Fix (verifier-corrected):** Partially wrong as written — do NOT add "~/.config", "~/.local/bin" or "~/bin". Those are ordinary write targets for legitimate tooling (configstore-based npm packages write ~/.config/configstore, `pip install --user` writes ~/.local/bin), and denying them turns real installs into fabricated verification failures, which the module's own comment says is worse than unsandboxed. Add only the persistence paths with no legitimate build use: "~/Library/LaunchAgents", "~/Library/LaunchDaemons", "~/.claude", "~/.codex", "~/.zshrc", "~/.zprofile", "~/.zshenv", "~/.bashrc", "~/.bash_profile", "~/.profile", "~/.gitconfig". I verified both mechanics the fix depends on: `(subpath "<plain file>")` does deny writes to a regular file ('Operation not permitted', file unchanged), and a path in the profile that does not exist does not break sandbox-exec. Apply the SBPL escaping fix first, since these entries are interpolated raw through the same `'  (subpath "%s")' % p` line. Severity lowered to low: local single-user tool, deny-list incompleteness is a documented design choice, and the doc update is the highest-value part of this.

### A-40 [MEDIUM] Visual QA silently degrades to light-only grading when the dark-mode launch or screenshot fails

**Where:** `visualqa.py:384`  ·  lens: verify-stack  ·  verified: CONFIRMED

**Evidence:** capture_screens returns early with the partial shot list when the dark-mode relaunch fails (`return shots, "launch failed (dark): ..."`, line 231) and silently drops a failed screenshot (`if code == 0 and os.path.exists(path)`, line 236 — note stays ""). run_visual_qa only inspects the note when shots is EMPTY (`if not shots:` line 390); with one light shot present, the note is discarded, the panel grades light only, and the gate can PASS. The module's own contract says screenshots are captured 'in light AND dark mode (the design rules demand both palettes)'. An app that crashes when relaunched in dark appearance therefore passes visual QA with zero trace (and uicrawl resets appearance to light, so no later gate sees dark mode either).

**Fix (verifier-corrected):** Fix as proposed is right (emit the note even when shots is non-empty; persist a capture_note when len(shots) < 2; optionally treat 'launch failed (dark)' as a FAIL reason since crash-on-dark is a real defect). tests/test_visual_qa.py exists.

### A-41 [LOW] .gitignore misses the .api_probe_<provider>.json cache family written into the repo root

**Where:** `.gitignore:9`  ·  lens: orch-core-a  ·  verified: CONFIRMED

**Evidence:** detect_api_available writes its 4h verdict cache to `_probe_cache_path(".api_probe_%s.json" % provider)` (orchestrator.py line 1341), which resolves to the engine dir HERE — the repo root — whenever it is writable (lines 560-576). .gitignore pins the two older sibling caches from the same _probe_cache_path family (.codex_model_probe.json line 9, .gemini_probe.json line 10) but not the api ones. Verified: `git check-ignore .api_probe_anthropic.json` reports NOT IGNORED. Once any api: agent is probed, up to three untracked files appear in git status; with the documented fleet shared-checkout workflow (agents staging files in this tree), they are one careless `git add -A` away from being committed. The file contents include the resolved key-file path and failure reasons (no secret values), so this is repo hygiene, not a leak.

**Fix (verifier-corrected):** Fix as claimed is correct: add `.api_probe_*.json` to .gitignore after line 10. Verify with `git check-ignore -v .api_probe_anthropic.json` (should match the new pattern).

### A-42 [LOW] make clean leaves the root build/ dir (pip in-tree build) behind — the stale engine copy that pollutes mypy and the app bundle

**Where:** `Makefile:64`  ·  lens: scripts-ci  ·  verified: CONFIRMED

**Evidence:** clean runs `rm -rf gui/.build gui/dist .mypy_cache .ruff_cache *.egg-info dist` plus a __pycache__ sweep — but not `build`. pyproject.toml:72-74 documents that `pip install .` (pip >= 21.3 in-tree builds; CI's typecheck job does exactly this) creates build/ at the repo root and that mypy must exclude it to avoid duplicate-module errors. The dir exists locally (build/lib contains a full Jul-15 copy of every engine module, 1.8MB) and there is no target that removes it, so it persists indefinitely, ships in the app bundle via build_app.sh's find (verified by dry-run), and presents stale duplicates of every module to any tool not carrying the mypy exclude.

**Fix (verifier-corrected):** Fix is correct as written: adding `build` (and optionally `.pytest_cache`) to the rm line only removes the literal root-level ./build; the tracked sections/build/ path is untouched (also protected by .gitignore's `!sections/build/` negations, lines 21-23, which are irrelevant to rm anyway). The proposed verification (make clean; test ! -d build; porcelain shows no tracked deletions) is apt.

### A-43 [LOW] "No API keys" contract in README and module docstring contradicted by the V3 direct-API runners

**Where:** `README.md:23`  ·  lens: orch-core-a  ·  verified: CONFIRMED

**Evidence:** README.md lines 23-29: "No API keys — run.sh and the GUI strip ... One exception: the gemini CLI can run headless with a key read from ~/.orchestrator/gemini_api_key". The module docstring (orchestrator.py line 10) likewise claims "No API keys. Uses your normal subscription CLI sessions only." But orchestrator.py implements three more key-consuming paths: api:anthropic / api:openai / api:google runners (lines 1167-1305) that bill provider accounts per token, reading keys from ~/.orchestrator/anthropic_api_key, openai_api_key, google_api_key (_API_KEY_FILES, lines 693-699), enabled per project via run_config.json {"api_agents": true} (lines 963-985). grep confirms neither README.md nor config.yaml mentions api_agents, anthropic_api_key, or openai_api_key anywhere. A user relying on the documented "only exception is the gemini CLI key" guarantee has no documentation that three other key files exist, are honored, and cost real money when a project opts in.

**Fix (verifier-corrected):** Fix as claimed is correct and appropriately scoped: add a sentence after the gemini exception in README.md documenting the per-project opt-in api:<provider>:<model> agents (keys file-only from ~/.orchestrator/<provider>_api_key, enabled via "api_agents": true in run_config.json, billed per token; note google also falls back to gemini_api_key per _API_KEY_FILES), and soften the module docstring line 10. Run python3 -m py_compile orchestrator.py after the docstring edit.

### A-44 [LOW] is_admissible docstring contradicts its own code: claims status=='published' (a retired status) and describes the shipped 'final' check as future work

**Where:** `artifacts.py:1437`  ·  lens: v3-stack  ·  verified: CONFIRMED

**Evidence:** The docstring bullet reads "* status == 'published' — not draft, not superseded" and closes with "4.8 will tighten THIS body to a policy-driven status=='final' check without touching route_push" (artifacts.py:1432-1441), but the body already enforces `meta.get("status") != "final"` (line 1451) and the module header at line 82-83 states 'published' is RETIRED (V3 4.8) — split into pending_review/final. retrieve()'s docstring (line 2000-2001) repeats the same stale claim ('is_admissible: published, not stale, ...'). Anyone implementing against the documented contract (e.g. a GUI badging admissibility) would test for a status value no publish path can ever assign.

**Fix (verifier-corrected):** Fix is correct as written (two docstring edits, no behavior change). Both suggested test modules exist (tests/test_artifact_retrieval.py, tests/test_artifacts.py); running them after the edit is a fine smoke check.

### A-45 [LOW] Corrupt pipeline_request.json is never drained and never surfaced — contradicts the documented peek-and-clear contract

**Where:** `conductor.py:949`  ·  lens: conductor-stack  ·  verified: CONFIRMED

**Evidence:** _consume_pipeline_request's docstring (conductor.py:936-939): 'if present, drain it immediately regardless of outcome, so a broken request can't retry-loop forever', and the module constant's comment (line 57): 'Consumed (deleted) the moment it's read'. The code merges 'file missing' and 'file corrupt' into one silent path: `except (OSError, ValueError): return state` (lines 946-950) — os.remove only runs after a SUCCESSFUL json.load. Reproduced: wrote '{this is not json' to .conductor/pipeline_request.json, called _consume_pipeline_request -> 'corrupt request still on disk: True', no ledger line, no banner. A GUI 'Run pipeline' click that lands as a torn/invalid write is silently ignored on every poll forever (the operator sees nothing — a §6.2 visible-fallback violation by the file's own standards), and the stale marker lingers to be misread later.

**Fix (verifier-corrected):** The proposed fix is correct (catch FileNotFoundError separately -> return; on ValueError/other OSError best-effort remove, ledger pipeline_load_failed with preset_path None, advance cursor, save, emit banner) and tests/test_conductor_pipeline.py already asserts pipeline_load_failed for the invalid-preset path (line 79), so the new test fits the file's pattern. Severity downgraded to low for a local single-user tool: it requires a torn/hand-mangled write of a tiny JSON marker, and the failure mode is a silently ignored GUI click plus a stale file — annoying and contract-violating, but narrow and easily recovered by re-clicking after any fix.

### A-46 [LOW] full_poll's routing-fault handler comment claims the fault 'is ledgered' but the handler only prints — the audit trail silently misses routing errors

**Where:** `conductor.py:1466`  ·  lens: conductor-stack  ·  verified: CONFIRMED

**Evidence:** `except Exception as exc:  # noqa: BLE001 - a routing fault must not / # wedge the observation loop; it's ledgered and the poll ends.` followed only by `emit("conductor: routing error (loop continues): %s" % exc)` (conductor.py:1461-1467). Nothing appends to conductor_ledger.jsonl, so a crash-looping route_engine (e.g. a corrupt artifact store raising every poll) leaves zero durable record in the file the module header calls 'the append-only decision record' — an auditor replaying the ledger sees an idle, healthy conductor.

**Fix (verifier-corrected):** Fix as proposed (guarded ledger_append of a routing_error decision before emit). Keep the emit — tests/test_conductor.py:356 asserts a warning containing 'routing error', so the print must survive. One caution: a NEW decision kind 'routing_error' passes reconcile_on_start harmlessly (no replay branch matches it), so no reconcile change is needed; if the team prefers zero new kinds, the comment-only correction is the honest minimum.

### A-47 [LOW] Stale --route startup warning claims 'no termination/permission dials yet' — contradicting the same flag's help text and the implemented 7.4/7.5/7.6 enforcement

**Where:** `conductor.py:1703`  ·  lens: conductor-stack  ·  verified: CONFIRMED

**Evidence:** main() prints at startup: `"conductor: --route ENABLED — autonomous session minting is on (no termination/permission dials yet; supervise this run)."` (lines 1703-1705), while the --route argparse help added 20 lines earlier says the opposite: 'Off by default — 7.4 permissions, 7.5 termination, and 7.6 oversight dials remain enforced on every routed session' (lines 1641-1644) — and the enforcement genuinely exists (capability gate at 2655-2668, termination stack at 1454-1456, dials via classify_route). The runtime banner is a leftover from the 7.1 skeleton and falsely tells an operator that enabling --route runs unsupervised-unguarded.

**Fix (verifier-corrected):** Fix as proposed; suggested replacement text is accurate. python3 -m unittest tests.test_conductor is a sufficient check (no test references the old string).

### A-48 [LOW] route_deferred is re-appended (and fsynced) to conductor_ledger.jsonl on every poll for every deferred route — unbounded duplicate-decision growth

**Where:** `conductor.py:2640`  ·  lens: conductor-stack  ·  verified: CONFIRMED

**Evidence:** Over-quota deferral intentionally leaves the route un-recorded so it retries ('don't mark routed, so the still-admissible source re-plans next cycle', line 2635-2637) — but each retry appends a fresh route_deferred ledger line via _ledger_route (lines 2638-2643), and _drain_pending does the same for approved-but-deferred actions every poll (lines 1871-1881). full_poll re-runs on any session events.jsonl growth (wake tick is 1s, interval cap 30s), so one deferred route over one quota'd day can add tens of thousands of identical lines to the header-declared 'append-only decision record ... never a lost or doubled decision' ledger — each individually fsynced, and each append then re-counts the whole file (ledger_append -> ledger_length, line 579) while route_engine re-parses the full ledger per poll for proposed_route_ids (lines 1776-1780), so growth compounds into quadratic I/O. If finding #6's stale over_quota occurs, this growth never stops at the day boundary.

**Fix (verifier-corrected):** The per-(route_id, day) dedupe map is sound; two refinements: the map must be persisted in state (state.setdefault does this — it will ride along in conductor_state.json; keep pruning to today's entries at route_engine start so it stays bounded), and the _drain_pending site must dedupe on the same map so an approved-deferred action doesn't spam either. Existing deferral test lives at tests/test_conductor_termination.py:682 — extend there rather than a new file.

### A-49 [LOW] Uncommitted config.yaml flag flips contradict their own committed comments (gemini/ollama/visual_qa)

**Where:** `config.yaml:127`  ·  lens: seeds-config  ·  verified: CONFIRMED  ·  **CLOSED 82c9945 (opt-in)**

**Evidence:** Working-tree config.yaml (uncommitted, per git diff) flips three flags to false while the comment block directly above each still asserts the opposite: line 113 'Gemini stays enabled, but a STARTUP PROBE decides whether it actually joins each run' above `gemini_enabled: false` (line 121); lines 122-123 'Local model via Ollama — OFF by default in docs; enabled here by default for richer multi-LLM discussion' above `ollama_enabled: false` (line 127); and the visual-QA gate description above `visual_qa_enabled: false` (line 422) still reads as an active gate. Anyone (human or agent) reading the file infers the opposite of actual behavior. Probe caches corroborate the runtime reality: .gemini_probe.json (written today, 0.6h old) records a 90s gemini CLI timeout, so the flips look deliberate.

**Fix:** When committing the flips, update the comments in config.yaml: at line 122-123 change 'enabled here by default for richer multi-LLM discussion' to reflect that ollama is currently disabled; at line 113 rephrase 'Gemini stays enabled' to describe the flag as the master switch with the probe as a second gate; optionally add a dated note for why visual_qa_enabled is off. No test needed — comment-only edit.

### A-50 [LOW] Prompt header and README name tech_stack.json as the binding registry, but the live source is sections/build/target_policy.json (fleet file is an empty tombstone)

**Where:** `designlint.py:88`  ·  lens: seeds-config  ·  verified: CONFIRMED

**Evidence:** render_tech_stack emits '===== APPROVED TECH STACK (tech_stack.json — binding) =====' (designlint.py:88), and README.md:376 says 'banned packages (tech_stack.json)'. On this checkout the Sections tree exists, so load_tech_stack/orchestrator resolve the stack from sections/build/target_policy.json (verified: policy tech_stack has 3 banned entries — Alamofire, SwiftyJSON, realm — while fleet tech_stack.json has allowed=[] banned=[] and a notes tombstone saying policy resolves from the Build section manifest). An agent or user told by the prompt/README to edit tech_stack.json changes nothing; the designlint.py module docstring (line 22, 'tech_stack.json (next to the engine) is the approved-library registry') carries the same stale claim.

**Fix (verifier-corrected):** The fix is right, with one point made definite: the hash IS pinned — tests/test_build_policy.py:41-43 asserts _sha(designlint.render_tech_stack(designlint.load_tech_stack(HERE))) == '310b0900485738...', and that output contains the header string, so changing the header WILL fail the test until the pinned sha is recomputed (the test's own comment says the hashes pin 'actual rendered bytes'). So: add the optional source_label parameter to render_tech_stack defaulting to the legacy label, pass the sections path from orchestrator.py:11259 when policy is not None, update the designlint.py docstring (line 22) and README.md:376, recompute the pinned sha in tests/test_build_policy.py, then run: python3 -m unittest tests.test_build_policy

### A-51 [LOW] docsync 'cleared' override state silently discards subsequent human edits until the file is next rewritten

**Where:** `docsync.py:322`  ·  lens: v3-stack  ·  verified: CONFIRMED

**Evidence:** clear_override marks a record status='cleared' (docsync.py:268); prepare_render then skips every detected edit on a cleared path outright: `if path in cleared: continue` (docsync.py:322-324), and finish_render only pops the cleared record when the renderer actually rewrote that file this pass (`if path in written_set: state["files"].pop(path, ...)`, docsync.py:432-434). But several protected files are NOT written on every render — HANDOFF_BLUEPRINT.md/GAP_REPORT.md only when artifact_reader is not None and the blueprint renders (docs.py:1316-1331), PRD.md/TECHNICAL_ARCHITECTURE.md only when their source phases exist in the workflow (docs.py:1305 skips md=None). So a record can stay 'cleared' across renders, and during that window a NEW human edit to the file is neither recorded as human-overridden nor reconciled — the next render that does write the file silently destroys the human's new bytes with no reconcile request, violating the module's own contract ('overwriting on uncertainty would discard the exact datum 5.5 exists to protect').

**Fix (verifier-corrected):** The claimed fix is correct as written (compare the detected edit's content_hash to the stored record's; fall through on mismatch, continue on match; update the clear_override docstring; add the re-edit test to tests/test_docsync). Severity downgraded to low for this local single-user tool: the loss window requires the user to explicitly clear_override a file AND re-edit it before the next render that writes that file — a narrow, self-initiated sequence — unlike claim 1's automatic path.

### A-52 [LOW] Enroll promotion clones git HEAD, silently dropping the uncommitted origin state the intake and compliance audit actually observed

**Where:** `enroll.py:370`  ·  lens: v3-stack  ·  verified: CONFIRMED

**Evidence:** prepare_writable_clone uses `git clone --no-hardlinks -- origin staging` when the origin is a git root (enroll.py:369-370) — the clone materializes committed HEAD only, while inspect_origin (line counts, README excerpt, markers — enroll.py:174-193) and the whole enroll audit read the origin's WORKING TREE. An origin with uncommitted or untracked changes therefore promotes to an app_build that does not match the code the compliance_report evidence was validated against (compliance evidence paths are checked against target_root = the origin working tree via docslint.existing_target_file, compliance.py:60-65). The rsync path (non-git origins) copies the working tree, and scaffold's only warning covers the opposite case ('target is not a Git repository root; promotion will snapshot-copy it', enroll.py:290-291) — nothing warns about the git-clone divergence.

**Fix (verifier-corrected):** Direction is right, two corrections. (1) Use `git --no-optional-locks -C <origin> status --porcelain` (subprocess with check=False; treat nonzero/exception as unknown and only warn): plain `git status` opportunistically refreshes .git/index, i.e. WRITES into the origin, violating enroll's read-only-origin contract ('Materialize app_build without ever writing into source'). (2) Prefer the warn variant over hard refusal for this local tool — enrolling WIP code is a legitimate use, and an EnrollError would block it; append the divergence note to prepare_writable_clone's returned dict (or scaffold's warnings list) and have the promotion path emit it. Test in tests/test_enroll_clone.py as proposed (dirty a file in the git-origin fixture, assert the warning surfaces).

### A-53 [LOW] evalharness crashes with an uncaught TypeError on events.jsonl files mixing naive and tz-aware timestamps

**Where:** `evalharness.py:63`  ·  lens: verify-stack  ·  verified: CONFIRMED

**Evidence:** Probe: events.jsonl with first ts '2026-07-20T10:00:00' (naive, pre-tz-change era) and last ts '2026-07-28T12:00:00-04:00' (aware, current events.py format) -> `ESCAPED: TypeError: can't subtract offset-naive and offset-aware datetimes`. The handler is `except ValueError:` only, so the TypeError propagates out of _events_span, crashing score_project and the whole --eval-report run. Any long-lived project whose events.jsonl spans the tz-awareness change (verify.py:940 documents that events.py switched to aware local+offset timestamps) hits this.

**Fix (verifier-corrected):** Fix is correct: change to `except (ValueError, TypeError):` (line 62, not 63), optionally normalizing single-naive pairs by stripping tzinfo to still return a span. No evalharness test file exists, as the claim says.

### A-54 [LOW] emit_event's PIPE_BUF line cap is defeated by nested dict/list fields — conductor termination/budget events write oversized lines into concurrently-appended events.jsonl

**Where:** `events.py:226`  ·  lens: conductor-stack  ·  verified: CONFIRMED

**Evidence:** The contract (events.py:63-65) promises 'Appends are atomic per line ... so concurrent writers (parallel build lanes) cannot interleave partial lines', enforced by shrinking 'the largest string field until the whole line fits' (lines 222-227). The shrink loop only sees TOP-LEVEL string fields; nested dicts/lists hit `break  # non-string bloat we can't trim` and the oversized line is written anyway. Reproduced: emit_event(root, 'budget_exhausted', evidence={20 keys of 500-char strings}) wrote one 10281-byte line (cap 3500). Real emitters pass exactly such payloads: conductor.py:906 `eventslib.emit_event(os.path.join(root, sid), event_kind, ..., evidence=detail)` — where a converged report's evidence embeds `"open_gaps": _open_gaps(app_dir, on_warn)` with NO cap (conductor_termination.py:357; contrast _check_gap_empty's ids[:20]) — and conductor.py:1049-1053 (spend=spend, evidence=evidence). These land in the SESSION's events.jsonl, which the live engine process appends to concurrently (a terminated/stalled session's runner is not killed), so a >PIPE_BUF write can interleave and …[trimmed]

**Fix (verifier-corrected):** The events.py fix is right: when no top-level string can shrink, JSON-flatten the largest dict/list field to a truncated string (which the existing string-shrink loop can then further trim on the next iteration), and break only when neither exists. One correction to the second part: capping open_gaps to [:20] inside converged_report also truncates the durable termination REPORT file and the ledger detail (the same dict is passed to _record_termination as evidence), slightly weakening the report's 'honest about remaining work' contract — prefer keeping the report intact and capping only at the emit site (conductor.py:906, pass a copy with open_gaps[:20] + an open_gap_count field, mirroring _check_gap_empty), or accept the [:20] but add open_gap_count. Severity downgraded: for a local single-user tool the corruption needs two concurrent >PIPE_BUF-crossing writes to the same session events.jsonl in the same instant; the practical consequence is occasional dropped GUI/notification event lines, not decision-record damage (the conductor ledger has its own independent cap).

### A-55 [LOW] setChatModelOverride writes model_routing.json non-atomically while the engine polls it, and swallows write failures

**Where:** `gui/Sources/OrchestratorGUI/OrchestratorStore.swift:1738`  ·  lens: gui-swift  ·  verified: CONFIRMED

**Evidence:** setChatModelOverride (1723-1740) is the mid-chat model-swap path whose own comment says 'the engine's conversational loop re-reads it at the next round barrier'. The write is 'try? data.write(to: url)' — no .atomic option, unlike every other JSON writer in this file (e.g. 2946, 2963, 2988 all pass .atomic). A round-barrier read that lands mid-write sees truncated JSON; modelrouting.load_routing fails open to defaults (modelrouting.py:129-133), silently dropping not just the pending swap but every previously-set per-chat override for that read. The try? also hides a failed write entirely — the UI chip stays 'pending' forever with no error. (requestChatRetry at 1712-1715 has the same try?-swallow but the engine's rename-then-run contract limits the damage to a silently-lost retry.)

**Fix (verifier-corrected):** As stated: `try data.write(to: url, options: .atomic)` in a do/catch with surfaceError for setChatModelOverride, same treatment for requestChatRetry. Correct and complete.

### A-56 [LOW] forkChatSession reads the child's pipe only after waitUntilExit — deadlocks the fork (and leaks the process) if output exceeds the 64KB pipe buffer

**Where:** `gui/Sources/OrchestratorGUI/OrchestratorStore.swift:1773`  ·  lens: gui-swift  ·  verified: CONFIRMED

**Evidence:** forkChatSession (1765-1797): 'try? proc.run(); proc.waitUntilExit(); let out = ... readDataToEndOfFile()' with BOTH stdout and stderr merged into one pipe (1770-1771). If the fork errors verbosely (python traceback + engine banners over 64KB), the child blocks writing to the full pipe while the GUI blocks in waitUntilExit — permanent mutual wait; the detached task never resumes, no error ever surfaces, and the child is leaked. The codebase itself documents the safe order 200 lines away: conciergeAsk reads 'to EOF BEFORE waitUntilExit — the safe order when the reply could exceed the 64KB pipe buffer' (3075-3078), and other sites comply (1860-1862, 3693-3695). PipelineBuilderView.swift:398-401 and ArtifactRouting.swift:261-262/298-299 repeat the unsafe order.

**Fix (verifier-corrected):** The read-then-wait swap is correct for forkChatSession and both ArtifactRouting.swift sites. One correction: the PipelineBuilderView.swift site (processCommand, ~390-402) runs `/bin/ps -p <pid> -o command=` whose output is a single command line and cannot approach 64KB — swapping there is harmless-but-unnecessary; don't count it as part of the defect. Everything else as stated.

### A-57 [LOW] API-key env-strip lists have drifted into three inconsistent sets (run.sh=10 vars, RunController=8, enrollment path=5)

**Where:** `gui/Sources/OrchestratorGUI/OrchestratorStore.swift:1842`  ·  lens: scripts-ci  ·  verified: CONFIRMED

**Evidence:** run.sh:29-31 unsets 10 vars (OPENAI_API_KEY, ANTHROPIC_API_KEY, GEMINI_API_KEY, GOOGLE_API_KEY, OPENAI_API_BASE, OPENAI_BASE_URL, ANTHROPIC_AUTH_TOKEN, ANTHROPIC_BASE_URL, GOOGLE_APPLICATION_CREDENTIALS, GOOGLE_GENAI_API_KEY). RunController.swift:57-60 and OrchestratorStore.swift:3057-3060 strip 8 (missing GOOGLE_GENAI_API_KEY and OPENAI_API_BASE). The enrollment launch at OrchestratorStore.swift:1842-1844 strips only 5: ["OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY", "ANTHROPIC_AUTH_TOKEN"] — missing GOOGLE_APPLICATION_CREDENTIALS, ANTHROPIC_BASE_URL, OPENAI_BASE_URL, GOOGLE_GENAI_API_KEY, OPENAI_API_BASE. README.md:24-25 claims run.sh AND the GUI strip these keys 'and related vars' so 'every call counts' against the logged-in CLIs; the enrollment-launched engine process (and anything it spawns) retains Vertex credentials (GOOGLE_APPLICATION_CREDENTIALS) and custom base-URL overrides, so a CLI configured for pay-as-you-go via those vars would bill despite the no-cost promise.

**Fix (verifier-corrected):** Fix is sound: one canonical `static let strippedAPIKeyVars` (RunController and OrchestratorStore are in the same module, so a shared constant works) used at all three Swift sites, containing run.sh's 10 names, with cross-referencing comments in run.sh and the Swift file. The grep verification is fine; note `make gui-test` only compiles/tests — consider a small unit test asserting the constant contains all 10 names so future drift is caught.

### A-58 [LOW] writeRunConfig overwrites <project>/run_config.json wholesale, contradicting the file's other writers which carefully preserve keys (sensitivity, situation)

**Where:** `gui/Sources/OrchestratorGUI/OrchestratorStore.swift:3970`  ·  lens: gui-swift  ·  verified: CONFIRMED

**Evidence:** writeRunConfig (3968-3977) builds cfg from an EMPTY dict (only autonomy/completeness/stop_after_phase) and writeJSON's it to <project>/run_config.json — no read-merge. The same file is a multi-writer contract: ProjectSensitivityFile.write (Configuration.swift:36-58) and SituationApplyService.confirm (SituationEditorView.swift:18-37) both load-existing-then-overlay precisely because run_config.json also carries "sensitivity" (read by the engine at orchestrator.py:1915 as the privacy floor) and "situation". Today the only caller is NewAppIntake.swift:562 on a freshly created dir, so exposure is latent — but the method is public, takes any project name, and any future caller on an existing project (the API shape invites it: 'the same store calls the classic New-chat sheet made') would strip a user's private flag, silently sending a private project's turns to cloud models. Secondary wart: 'guard !cfg.isEmpty else { return }' means reverting all three fields to defaults never clears a previously written file.

**Fix (verifier-corrected):** As stated (read-merge like siblings, removeValue for defaults, drop the early-return guard, unit test pre-seeding sensitivity+situation). Correct.

### A-59 [LOW] RoutingGridView.applyChanges bypasses the store's modelRoutingCache invalidation — up to 2s of readers get pre-Apply routing, and a concurrent merge-writer can revert the Apply

**Where:** `gui/Sources/OrchestratorGUI/RoutingGridView.swift:321`  ·  lens: gui-swift  ·  verified: CONFIRMED

**Evidence:** applyChanges calls 'm.draft.save(to: routingURL)' directly (321) instead of store.writeModelRouting/writeProjectRouting, so the TTL cache entry for that URL is NOT cleared (writeModelRouting at OrchestratorStore.swift:2725-2728 exists precisely to pair save with 'modelRoutingCache[modelRoutingURL] = nil'). cachedModelRouting (3097-3105) serves the stale entry for up to modelRoutingTTL=2.0s. Concrete revert path: fleet grid Apply -> within 2s the user toggles anything in ModelRoutingSections, whose onChange does 'var current = store.readModelRouting()' (ModelLibrary.swift:454) — returning the PRE-Apply cached object — then writes it back (458), silently undoing the grid's Apply. The store's own cache comment (3086-3094) assumes 'routing edits ... invalidate the cache explicitly below', which this path violates.

**Fix (verifier-corrected):** As stated: route applyChanges through store.writeModelRouting/.writeProjectRouting per scope (and a new writeSectionRouting after the claim-2 file fix), or expose an invalidateRoutingCache(at:) and call it after the direct save. Correct.

### A-60 [LOW] SectionRulesLogic.save drops unknown top-level keys of rules.json and writes non-atomically

**Where:** `gui/Sources/OrchestratorGUI/SectionSettings.swift:87`  ·  lens: gui-swift  ·  verified: CONFIRMED

**Evidence:** save (71-100) preserves per-phase non-rules fields (good) but rebuilds the document as exactly '["schema_version": 1, "phases": phases]' (line 87), discarding any other top-level key — sections/_template/rules.json ships with a top-level "_comment" (verified by parsing), and a hand-added note or global_app_rules key in a real section would be silently destroyed on the first Save Rules. The write at line 95 is 'try out.write(to: rulesURL)' with no .atomic option — a crash mid-write leaves a torn rules.json, which phase_rules._load_layer then treats as corrupt (None + banner), disabling the section's rules overlay until manually fixed. The sheet's own header (lines 4-6) promises R2 discipline; the read-back check at line 99 does not protect against the torn-file-on-crash case.

**Fix (verifier-corrected):** As stated: load the full existing root, overlay edited phases, keep other top-level keys, default schema_version only when absent, write with .atomic, fixture test with _comment. Correct. Note the trailing read-back check (`return (try? Data(contentsOf: rulesURL)) == out`) already exists and still works after the change.

### A-61 [LOW] SnippetLibrary.save swallows all write errors — snippet edits appear saved but can silently vanish

**Where:** `gui/Sources/OrchestratorGUI/SnippetLibrary.swift:157`  ·  lens: gui-swift  ·  verified: CONFIRMED

**Evidence:** save(_:to:) ends with 'if let data = try? JSONSerialization.data(...) { try? data.write(to: url, options: .atomic) }' (155-158) — a Void return with both encode and write failures swallowed. OrchestratorStore.saveSnippets (2896-2908) calls it and unconditionally fires objectWillChange.send(); AppShellView's snippet editor (1405, 1439) treats the call as success. A failed write (read-only volume, permissions, disk full) leaves the user's snippet edits alive only in memory; they disappear on relaunch with no error ever shown. Contrast with the store's own writeJSON contract (OrchestratorStore.swift:4791-4795) that exists to prevent exactly this.

**Fix (verifier-corrected):** As stated: return Bool (or throw) from SnippetLibrary.save, check it in saveSnippets, surfaceError on failure. Correct.

### A-62 [LOW] install_launch_agent.sh embeds unescaped paths in the plist XML (breaks on '&' or '<' in the path) and its header lies about the log location

**Where:** `install_launch_agent.sh:39`  ·  lens: scripts-ci  ·  verified: CONFIRMED

**Evidence:** The heredoc (lines 39-70) interpolates $ORCH_DIR, $ROOT, $LABEL and $INTERVAL directly into XML <string>/<integer> elements with no escaping. Paths with spaces are fine (each ProgramArguments element is its own <string>), but a repo path containing '&' (e.g. ~/Documents/Apps & Tools/orchestrator) or a label/INTERVAL containing '<' produces malformed XML, and `launchctl load` fails on an unparseable plist — the script then prints 'Installed and loaded' anyway only if launchctl happened to exit 0, and there is no plutil validation. Separately, the header comment (line 10) says 'Logs go to .orchestrator/logs/launchagent.out/.err' while the plist writes them to $ORCH_DIR/logs/launchagent.out/.err (lines 60-62) — stale path from an older layout.

**Fix (verifier-corrected):** Keep the plutil -lint validation and the XML-escape helper (correct as written — &, <, > cover plist <string> needs). Add the piece the claim missed: check launchctl's own exit status, e.g. `launchctl load "$PLIST" || { echo "launchctl load failed for $PLIST" >&2; exit 1; }` — today failure is silently reported as success regardless of XML validity. For the comment, rewording line 10 to 'Logs go to <engine dir>/logs/launchagent.out/.err' resolves the ambiguity; it is a clarity fix, not correcting a wrong path.

### A-63 [LOW] SEED_HASHES has no documented re-pin protocol for legitimate seed changes

**Where:** `migrate_v3.py:30`  ·  lens: v3-stack  ·  verified: CONFIRMED

**Evidence:** grep across all .py/.md in the repo finds no re-pin instructions for SEED_HASHES anywhere (only migrate_v3.py itself, the task board's unrelated GUI card, and tests/test_migrate_v3.py). The only guard is the test at tests/test_migrate_v3.py:85-88, which asserts each pinned rel's WORKING-TREE hash equals the pin — exactly the assertion now failing for model_routing.json (working tree 2537fcb0... vs pin a84bbacd...), with no message telling a maintainer that the fix is to either revert the file or update the pin in the same commit as the intended seed change. Because _baseline's HEAD path (line 115) and the seed-identical shortcuts key off these pins, an un-re-pinned seed change silently degrades every affected file to 'complete document; pristine baseline unavailable' whole-doc carry for users — safe but lossy-of-precision, and invisible without the doc.

**Fix (verifier-corrected):** Fix is correct: comment block above SEED_HASHES (migrate_v3.py ~line 27-29) documenting the re-pin protocol, plus a self-explaining assertion message in tests/test_migrate_v3.py:86-88 (class is MigratorTest, method test_pinned_no_git_seed_hashes_match_every_shipped_source). As stated, that one test case stays red until the separate model_routing.json working-tree drift is resolved — a known, independent issue.

### A-64 [LOW] miniyaml parses `key:  # comment` as an empty nested map ({}) instead of null, so a commented-out scalar silently becomes a truthy dict

**Where:** `miniyaml.py:46`  ·  lens: support-modules  ·  verified: CONFIRMED

**Evidence:** strip_inline_comment returns "" when the value is only a comment (`if val[0] == "#": return ""`, line 45-46), and parse_min_yaml treats an empty value as the opener of a nested map (`if val == "": child = {}; parent[key] = child; stack.append(...)`, lines 80-83). Probe: `miniyaml.parse_min_yaml("a: # note\nb: 2")` returns `{'a': {}, 'b': 2}` — real YAML yields `{'a': None, 'b': 2}`. A plausible hand edit to config.yaml like `development_team:  # fill in later` therefore yields a truthy `{}` where every consumer expects a string/None (e.g. `cget(cfg, "ios.development_team", "")` returns {} which passes `if team:` checks and str()s to "{}"), and any following line indented deeper is silently swallowed into that accidental map. Note coerce_scalar already maps a bare empty/`~`/null scalar to None — the divergence exists only on the comment-only-value path, and no line in the shipped config.yaml currently triggers it (verified with grep ':[[:space:]]+#').

**Fix (verifier-corrected):** In parse_min_yaml, keep opening a child map on a comment-only value but defer the null decision until the parse ends: before stripping, capture `raw_val = val` (the already-stripped value); when `strip_inline_comment(raw_val) == ''`, push the child map exactly as today, and if `raw_val.startswith('#')` also record `(parent, key, child)` in a `comment_only` list. After the main loop, for each recorded triple: `if parent.get(key) is child and not child: parent[key] = None`. This makes a commented-out scalar (`team:  # fill in later` with no deeper lines) parse as None while a comment-annotated section header (`ios:  # apple stuff` + nested block) keeps parsing as its map. Then run python3 -m unittest tests.test_miniyaml (currently 18 tests OK) and re-parse the shipped config.yaml asserting an identical dict.

### A-65 [LOW] modelrouting._overlay_routing deep-copies the wrong key, injecting a spurious top-level "chains": None into every layered routing dict

**Where:** `modelrouting.py:281`  ·  lens: support-modules+seeds-config  ·  verified: CONFIRMED

**Evidence:** Line 281 reads `out["chains"] = _copy.deepcopy(base.get("chains"))`, but chains live at routing["fallback"]["chains"], never at the top level — `base.get("chains")` is always None, so the line only adds a bogus `"chains": None` key to the merged dict (the real anti-aliasing work is already done by line 280's `out["fallback"] = _copy.deepcopy(base.get("fallback"))`, whose deepcopy includes the nested chains). Probe confirmed: loading a fleet+project overlay via load_routing_for_app yields top-level keys `['chains', 'enabled', 'fallback', 'phases', 'schema_version']` with `chains value: None` — a shape no other code path (load_routing, default_routing, summary, fallback_chain) produces or reads, so it exists only in the three-layer overlay outputs and drifts the routing dict shape between flat and layered runs. The comment at lines 277-279 ("out[\"fallback\"]/out[\"chains\"] alias the base's nested dicts") documents an aliasing hazard for a key that does not exist.

**Fix (verifier-corrected):** As proposed: delete line 281 and reword the comment at lines 277-279 to mention only out["fallback"] (whose deepcopy already covers the nested chains). Fix instruction's test preconditions verified: tests.test_model_routing (49 tests OK), tests.test_phase_controls (17 tests OK), tests.test_migrate_v3 currently FAILED (failures=1) — exactly the one known pre-existing failure the claim describes, so treat that single failure as the baseline.

### A-66 [LOW] run_anthropic_api/run_openai_api silently accept max-length-truncated replies as complete turns

**Where:** `orchestrator.py:1202`  ·  lens: orch-core-a  ·  verified: CONFIRMED

**Evidence:** run_anthropic_api hardcodes `"max_tokens": 8192` (line 1202) and treats any stream that reaches message_stop as complete (line 1194-1195 sets complete=True; line 1207 only fails when message_stop never arrived). Per Anthropic SSE semantics, a reply cut off at max_tokens still emits message_delta with stop_reason "max_tokens" followed by message_stop — so a mid-sentence truncated reply is returned with code 0 and no warning. run_openai_api has the same gap: it checks only for the [DONE] sentinel (line 1250-1251) and never inspects choices[].finish_reason=="length". This contradicts the module's own doctrine that partial text must never be silently promoted (_api_sse docstring line 1047-1048: "partial text is never promoted into the authoritative transcript") — a truncated final answer in a decision phase is exactly such partial text, and 8192 tokens is realistically exceeded by long phase outputs.

**Fix (verifier-corrected):** Fix as claimed is correct: capture `data.get("delta", {}).get("stop_reason")` in the message_delta arm into a nonlocal; after the stream, if it equals "max_tokens", fail the turn with a distinct message (preferred — matches the module's never-promote-partial doctrine) or append an explicit truncation marker plus emit() a warning. Mirror for openai via choices[].finish_reason == "length" (note: capture it in _event, since delta content and finish_reason arrive on choice objects). Make the limit configurable via cget(cfg, "models.api_max_tokens", 8192). Add an SSE fixture test to tests/test_api_runners.py. Verify: python3 -m unittest tests.test_api_runners

### A-67 [LOW] agy (Antigravity) invocations pass the full prompt on argv, violating the file's own stdin-only secret-hygiene invariant

**Where:** `orchestrator.py:1650`  ·  lens: orch-core-a+security  ·  verified: CONFIRMED

**Evidence:** Every other runner is explicit that argv is forbidden for prompt text — run_gemini's own key path (line 1618-1622): "Prompt goes over stdin, not argv: on argv the full prompt (and any secret spliced into it) is visible to every local user via `ps`/`/proc/<pid>/cmdline`", and run_claude repeats it verbatim at line 1461-1464. But the agy fallback does exactly that:
```python
for tmpl in ([["agy"] + writeflag + pt + ["-p", prompt],
              ["agy", "exec", "-p", prompt],
              ["agy", "run", "-p", prompt]]):
```
The prompt at that point carries the full phase context — prior transcripts, fetched URL content, and anything the operator put in initial_prompt.md — and is passed straight to `_run_subprocess`, so it is readable via `ps auxww` by any local process for the life of the turn. A second consequence: a very large prompt makes execve fail with E2BIG, which is swallowed as a generic "agy attempt failed" note (line 1656-1658) rather than surfacing as an argv-size problem. This path is on by default (`runtime.gemini_use_agy` defaults True, line 1644) whenever `agy` is on …[trimmed]

**Fix (verifier-corrected):** The fix is right in substance but one detail is off: it says "the first template already routes through _run_subprocess at line 1655" — in fact ALL THREE templates go through the single `_run_subprocess(tmpl, cwd, timeout, heartbeat=...)` call inside the loop (line 1655). So: verify the installed agy reads stdin when -p's positional is omitted; if yes, drop the `"-p", prompt` pair from all three templates, add `input_text=prompt` to that one _run_subprocess call, and change line 1664's success return to `_display_cmd(tmpl + ["<prompt on stdin>"])` (this also fixes the prompt leaking into the display string). Add TestAgyInvocationUsesStdin mirroring the gemini test. If agy cannot read stdin, document the accepted exposure in run_gemini's docstring. Verify: python3 -m unittest tests.test_sweep_fixes

### A-68 [LOW] Phase transcript path joins workflow-supplied folder/file with no traversal guard, unlike the identical join in backfill.py

**Where:** `orchestrator.py:2830`  ·  lens: security  ·  verified: CONFIRMED

**Evidence:** `_phase_file_path` (orchestrator.py:2820-2830) ends with `return key, os.path.join(app_dir, folder, fname)` where `folder`/`fname` come straight from the workflow phase definition. backfill.py performs the very same join for the very same values and explicitly guards it (backfill.py:168-179): "folder/fname come from workflow/phase config; refuse a value that joins to a path outside app_dir (a '../' or absolute escape)" — realpath + `os.path.commonpath([app_real, dest]) == app_real`, aborting otherwise. The main writer has no such check, so the two paths disagree about whether that input is trusted.

Workflow JSON is not purely engine-authored: `workflows.load_workflow` (workflows.py:711-726) reads `<orch_dir>/workflows/<name>.json` — the GUI's workflow editor writes there — and `Phase.from_json` (workflows.py:174) takes `folder`/`file` verbatim with no validation. A workflow whose phase carries `"folder": "../../.."` or an absolute `"file"` therefore writes phase transcripts outside the session dir. `load_workflow` additionally does not sanitize `name`, which is read from …[trimmed]

**Fix (verifier-corrected):** Mostly right, with two gaps. (1) The proposed fallback `os.path.join(app_dir, key, key + ".md")` is itself unguarded — `key` comes from the same untrusted dict, so validate/sanitize `key` too (or reject the phase outright) before using it as the escape hatch. (2) Better to normalize once in workflows.Phase.from_json rather than only in _phase_file_path: _phase_file_path is the shared reader AND writer, but other call sites also consume phase.folder/phase.file, and a chokepoint keeps them from disagreeing the way orchestrator.py and backfill.py already do. Test command is wrong: there is no tests/test_workflows.py — run `python3 -m unittest tests.test_workflows_schema tests.test_workflow_overrides tests.test_backfill`.

### A-69 [LOW] A completed forced vote is silently discarded (and re-run) on resume when the round preceding it is incomplete — the V3 2.4 reconciliation's own hazard

**Where:** `orchestrator.py:3884`  ·  lens: orch-core-b  ·  verified: CONFIRMED

**Evidence:** The comment at orchestrator.py:3883-3886 claims '_resume_round_state folds a crash-left vote section into the last complete round's segment (it runs to EOF)'. That assumption fails whenever the final round before the vote never got a coordinator block — reachable via the `if cresp is None: … continue` path (line 7985-7988, coordinator unavailable on the last budgeted round) or an empty round (line 7908-7916), after which the loop exhausts and `_run_forced_vote` still runs and can write a complete deterministic tally. Probe with the real functions on such a transcript: '_resume_round_state' returned resume_round=2 and the kept text ends at round 1's coordinator block — 'case2 vote header survives in kept: False'. Because `last_complete_end` (line 3879) precedes the incomplete round's header, `_kept` excludes the entire finished vote; `write_md(md_path, _kept)` (line 9157) then physically deletes the tally from disk, and the round AND the vote are re-run — precisely what _recover_forced_vote's docstring says must not happen ('re-running a finished vote loses last_substantive and can …[trimmed]

**Fix (verifier-corrected):** The proposed direction (search _existing instead of _kept) is right, with three precisions. (1) Only take the _existing-based path when _recover_forced_vote returns non-None (complete tally): then _kept = _existing[:_vote_end] with _vote_end bounded by _PHASE_FOOTER_MARK as today, and resume_round must be advanced past the last on-disk round header so rounds_iter is empty. (2) When the vote is PARTIAL (recover returns None), fall back to the CURRENT _kept-based logic unchanged — do not use the _existing offsets there, or the incomplete round's partial posts would survive while resume_round points at that round, duplicating them. (3) Keep msglib.reconcile_messages(keep_below_round=..., drop_post_round=...) consistent with whichever kept text was chosen (in the adopt case, keep_below_round must be past the incomplete round whose posts you preserved). Offsets align because _kept is a prefix of _existing, so _header_end is valid for both. Add the proposed test (coordinator-less final round + deterministic tally → tally adopted, not truncated); run python3 -m unittest tests.test_round_resume tests.test_vote_tally.

### A-70 [LOW] human_inbox.txt read-then-truncate race can silently destroy a concurrently written human message

**Where:** `orchestrator.py:3950`  ·  lens: orch-core-b  ·  verified: CONFIRMED

**Evidence:** _drain_inbox_message (orchestrator.py:3942-3952) reads the inbox (`msg = fh.read().strip()`) and then truncates it with a separate `open(inbox, "w").close()`; _peek_command_from_inbox (4272-4283) has the identical window. The docstring itself calls this an 'append-mode inbox' (3939): if the GUI or a CLI helper writes/appends a second message between the read and the truncate, that message is destroyed unseen — no transcript fold, no event, violating the file's own §6.2/'never silently lost' discipline cited elsewhere (e.g. 9226-9232 goes out of its way to preserve pending inbox content). The window is small but the engine drains at every round open/coord barrier while the human is actively typing into the same file, and rounds are minutes long, so drains and writes do interleave over a session.

**Fix (verifier-corrected):** The os.replace claim (rename inbox → inbox+'.draining', OSError → treat as empty, then read and delete) is the right engine-side fix and should be applied to both _drain_inbox_message and _peek_command_from_inbox. But it is incomplete on its own: the GUI writer is itself read-modify-write-rename, so after the engine claims the file the GUI can re-materialize the just-claimed message A inside its A+B write — the race then produces DUPLICATION of A instead of loss of B. Full fix needs the writer side too: the GUI (and any CLI helper) should append with O_APPEND semantics (FileHandle seekToEnd/write) instead of read-rewrite, or both sides should serialize on a lock file; alternatively accept rare duplication as the strictly-better failure mode and document it. Also preserve _peek's contract that ordinary chat is left untouched: with the rename approach, a non-command message must be renamed BACK (or the peek must read without claiming first and only claim when parse_command matches, accepting the same tiny window for commands only). Run: python3 -m unittest tests.test_conversational tests.test_messages_jsonl.

### A-71 [LOW] Stale command_barrier rows survive phase end and reset_state_for_new_prompt, and can fire a spurious forced vote in a later unrelated chat

**Where:** `orchestrator.py:4682`  ·  lens: orch-core-b  ·  verified: CONFIRMED

**Evidence:** Barrier rows are stored phase-unscoped as {name, args, requested_round} (orchestrator.py:4324-4334) and consumed only by `requested_round < rnd` (4337-4349). If a /vote is queued at round N of a chat and the user ends the chat in that same round (decision=='end' at 7726 breaks before the next barrier), the row persists in agent_state.json forever: the conversational finalize (7756-7769) never clears state['command_barrier'], and reset_state_for_new_prompt (4672-4694) omits it too — so a later conversational phase (same or NEW prompt) silently fires the stale vote the first time it reaches round N+1, injecting an unrequested '### Forced Vote' + tally into an unrelated conversation. Related hygiene gap in the same function: load_state documents fallback_counts as 'Per-run fallback rescues' (4618-4621) yet reset_state_for_new_prompt does not clear it (nor conversation_end), so the GUI's degraded-operation badge reflects lifetime counts, not the current run — contradicting both the field comment and reset_state_for_new_prompt's own docstring promise to clear 'per-run FAILURE …[trimmed]

**Fix (verifier-corrected):** The proposed fix is correct and minimal: (1) add '"command_barrier": [], "fallback_counts": {}, "conversation_end": {}' to reset_state_for_new_prompt's state.update (4682-4693); (2) clear state['command_barrier'] = [] in the conversational finalize before the save_state at 7769. One caution on (2): _take_barrier_commands does state.clear(); state.update(latest) from disk (4345-4346), so place the clear immediately before the final save_state to avoid a reloaded copy resurrecting rows. The deeper alternative (store the phase key in _queue_barrier_command rows and filter in _take_barrier_commands) is the better long-term shape if the non-conversational command dispatch fix lands, since that fix must not queue barrier rows no phase will ever take. Run: python3 -m unittest tests.test_commands tests.test_conversational.

### A-72 [LOW] First build iteration rewrites tasks.json with errors=[] — destroys the contract-error record users were told to review

**Where:** `orchestrator.py:6721`  ·  lens: orch-core-c  ·  verified: CONFIRMED

**Evidence:** _record_phase_contracts persists parse/cycle errors into tasks.json ('errors' field) and WARNs 'review tasks.json \'errors\' and the mistakes ledger' (lines 9530-9533). But _run_parallel_build's claim persistence does `persist_tasks(app_dir, backlog, [])` (line 6721) every iteration — persist_tasks (line 5025) writes the full file {schema_version, tasks, errors}, so the recorded errors list is blanked the moment the build starts, breaking the pointer the WARN gave the user (the mistakes ledger keeps a 10-item truncated copy only).

**Fix (verifier-corrected):** The proposed fix is correct and complete: add a load_task_errors(app_dir) helper next to load_tasks returning data.get('errors', []), read it once near line 6623, and pass it at 6721 (`persist_tasks(app_dir, backlog, _task_errors)`). Test as proposed. Run: python3 -m unittest tests.test_fleet_quality

### A-73 [LOW] _verify_and_repair reports 'still not compiling after N repair attempt(s)' with N=max_repairs even when the loop bailed out after fewer attempts

**Where:** `orchestrator.py:7127`  ·  lens: orch-core-c  ·  verified: CONFIRMED

**Evidence:** The repair loop breaks early on the sprint deadline (line 7086-7088) or when the repair agent is unavailable (lines 7108-7112), but the fall-through return is `return transcript, "still not compiling after %d repair attempt(s)" % max_repairs` — the phase's Final Output ('**Build verification:** still not compiling after 3 repair attempt(s)') then overstates the repair effort when 0 or 1 attempts actually ran, misleading anyone triaging the failed build (and the GUI, which surfaces the verify note verbatim).

**Fix (verifier-corrected):** The proposed fix is correct: initialize `attempts_run = 0` before the loop, increment right after call_agent at 7107 returns without raising (before the re-verify), and use it at 7127, optionally with '(budget %d)' when attempts_run < max_repairs. Note the fix's suggested test module tests/test_verify_repair does not exist; the discover fallback pattern works, but the closest existing targeted suites are tests.test_iteration_verify and tests.test_build_verification_phase. Run: python3 -m unittest tests.test_iteration_verify tests.test_build_verification_phase

### A-74 [LOW] Sequential roster-turn path catches only AgentError — one unexpected exception kills the whole phase, unlike the parallel path

**Where:** `orchestrator.py:7429`  ·  lens: orch-core-c  ·  verified: CONFIRMED

**Evidence:** In _run_roster_turns the parallel branch has a belt-and-suspenders `except Exception ... 'one turn must not kill the phase'` (lines 7424-7425), but the sequential fallback (taken when runtime.parallel_discussion_rounds=False or the round has a single agent) catches only AgentError (lines 7427-7431). Any non-AgentError raised inside _roster_turn — e.g. an OSError from build_context/session file IO, or a bug in prompt_discuss — propagates and aborts process_phase entirely, contradicting the resilience contract the same function states one branch above. Single-agent rounds always take this path, so every single-agent phase is exposed.

**Fix (verifier-corrected):** The proposed fix is correct and complete: add `except Exception as exc:  # noqa: BLE001 - one turn must not kill the phase` setting `results_by_agent[agent] = (None, "unexpected turn error: %s" % exc)` after the AgentError handler in the sequential loop, mirroring 7424-7425. tests/test_roster_turns.py exists. Run: python3 -m unittest tests.test_roster_turns

### A-75 [LOW] Mid-chat routing refresh layers new overrides onto the already-routed cfg — removing an override (model, timeout, rounds, instructions) mid-conversation has no effect

**Where:** `orchestrator.py:7573`  ·  lens: orch-core-c  ·  verified: CONFIRMED

**Evidence:** V3 board 1.11 (lines 7548-7551) promises 'a mid-chat model swap edits <chat>/model_routing.json' and conversational phases 're-resolve routing at each round open when the file's stat changes'. But the refresh does `base = dict(cfg)` where cfg is the ALREADY-ROUTED phase copy (process_phase applied _apply_phase_routing before calling _run_conversational_phase), then `cfg = _apply_phase_routing(base, key)`. _apply_phase_routing only ADDS overrides onto `models`/`_resolved` (lines 7157-7233) and returns early with the stale patched copy when `ov` is empty (line 7153-7154). So deleting an override from the file — reverting claude to its default model, clearing a routed timeout/rounds/instructions — leaves the stale `models['claude']`, `_routed_turn_timeout`, `_routed_rounds`, `_phase_instructions` values in force for the rest of the chat, while the engine still emits 'Chat routing updated — new model assignments apply from round %d', which is then false.

**Fix (verifier-corrected):** The proposed fix (stash pristine pre-route `models`/`_resolved` in process_phase before line 8848; on refresh restore them and pop `_routed_turn_timeout`, `_routed_rounds`, `_phase_instructions`, `_role_routing` before re-applying) is correct — TurnContext maps those exact keys (turncontext.py:141-152). One addition: process_phase folds the routed timeout into `_turn_timeout` once at 8879-8882, so for a timeout change (add OR remove) to actually apply mid-chat the refresh block must also recompute `_turn_timeout` from the restored/re-applied `_routed_turn_timeout` — without that, only the model/instructions restoration is observable. `_routed_rounds` is cosmetic for conversational phases (the chat loop is itertools.count, not rounds-bounded). Test as proposed in tests/test_conversational.py.

### A-76 [LOW] /cast add accepts any binary on PATH as an agent; the bogus id later raises a raw KeyError that AgentError-only handlers do not catch

**Where:** `orchestrator.py:7594`  ·  lens: orch-core-b  ·  verified: CONFIRMED

**Evidence:** The conversational /cast barrier (orchestrator.py:7593-7595) validates only `_agent_available(_agent, cfg)`, which for any non-ollama/gemini name is just `bool(which(agent))` (line 5832) — probe: `_agent_available('git') -> True`, `_agent_available('python3') -> True`. An added bogus id then reaches `resolve_runner`, whose final line is `return RUNNERS[agent]` (line 2027) with RUNNERS keyed only by ['codex','claude','gemini','ollama'] — probe: `resolve_runner('python3') -> KeyError 'python3'`. KeyError is not AgentError, and several call sites catch only AgentError: the sequential roster-turn path (lines 7427-7431, active when runtime.parallel_discussion_rounds is false), the conversational retry loop (lines 7707-7721), and the LLM tally failover loop (lines 8178-8193, reachable because voters come from available_active which includes the bogus id). In those paths the KeyError propagates and aborts the phase with state['error']; in the default parallel path it is swallowed each round as a perpetual '(skipped: CLI unavailable) … unexpected turn error' block, so a typo'd /cast add …[trimmed]

**Fix (verifier-corrected):** The proposed fix is correct and complete as stated: (1) tighten the /cast add branch to require a real agent identity (`_agent in RUNNERS or _agent.startswith('local:') or _agent.startswith('api:')`) before _agent_available, with the clearer announcement text; (2) belt-and-suspenders in resolve_runner — replace `return RUNNERS[agent]` with a lookup that raises AgentError('unknown agent id %r' % agent) so every dispatch path degrades to the existing skip/failover handling instead of a raw KeyError. Part (2) also fixes the parallel path's cryptic "unexpected turn error: 'git'" note into a diagnosable message. One nit: bare 'api:...' ids currently fail _agent_available anyway (which('api:x') is falsy), so admitting the prefix in the validity check changes nothing today but keeps the check future-proof. Run: python3 -m unittest tests.test_conversational tests.test_commands.

### A-77 [LOW] /commands typed during non-conversational phases are folded into the transcript as 'You (human)' chat lines, contradicting the module's own §13.5 contract

**Where:** `orchestrator.py:7861`  ·  lens: orch-core-b  ·  verified: CONFIRMED

**Evidence:** The V3 9.5 contract comments state a command 'must NEVER be folded into the transcript as a "You (human)" message' (orchestrator.py:4260-4262) and that 'execution rides this same drain boundary so CLI (human_inbox.txt) and GUI composer get identical behavior' (4264-4265); commands.py's header repeats 'the caller must show a visible banner and NOT forward the raw text as a chat message'. But `_peek_command_from_inbox` has exactly one call site — line 7653, inside _run_conversational_phase. Every debate-phase drain (`drain_human_inbox` at 7861 slot='open' and 7921 slot='coord') and both parallel-build drains (6674, 6887) fold ANY inbox content verbatim, so '/status' or '/vote' written to human_inbox.txt mid-debate becomes a '**You (human)**' block that the agents read and react to, and the command never executes. Since nearly every workflow phase is non-conversational, the documented CLI command surface silently does the wrong thing for most of a run.

**Fix (verifier-corrected):** The proposed fix works but one parenthetical must be promoted to a requirement: at the non-conversational drain sites, barrier-kind builtins (/vote /consensus /cast) must NOT be queued via _queue_barrier_command — nothing in the debate or build loops ever calls _take_barrier_commands, so a queued row would sit in agent_state.json until an unrelated later chat fires it (exactly the stale-barrier defect in the companion claim). Render an 'unavailable in this phase' command card for those instead; non-barrier builtins (/status /cost /help), templates, and meta commands can dispatch normally since _dispatch_command already renders them as fenced cards, never chat lines (personas/active/prior_outputs are all in scope at 7861/7921). Keep the peek a strict no-op when the inbox holds ordinary chat so tests.test_transcript_golden stays byte-identical; run python3 -m unittest tests.test_commands tests.test_transcript_golden.

### A-78 [LOW] _await_approval ignores _SHUTDOWN — a stop signal during a checkpoint pause can hang the process for up to 2 hours

**Where:** `orchestrator.py:9821`  ·  lens: orch-core-d  ·  verified: CONFIRMED

**Evidence:** Its sibling wait loops both check the shutdown flag every tick: _await_step_in (`if _SHUTDOWN.is_set(): return "shutdown"`, line 9687) and _await_inbox (line 9746, returning ("shutdown", None)). _await_approval's loop (lines 9821-9840) checks ONLY the three decision files and the deadline — no _SHUTDOWN check, and (unlike its siblings) no cfg/_phase_deadline check either. _await_approval is called from _run_app_pipeline (checkpoint pause at line 11411 and the re-arm path at line 11234) with timeout=_approval_timeout(cfg), default 7200s. In parallel-project mode process_app runs in ThreadPoolExecutor worker threads (line 12334); on SIGTERM/SIGINT the _cleanup handler (lines 12758-12768) sets _SHUTDOWN, kills live agent process groups, removes held locks, and sys.exit(0)s — but SystemExit only unwinds the MAIN thread; the `with ThreadPoolExecutor` exit then blocks in shutdown(wait=True) joining the non-daemon worker that is still sleeping inside _await_approval. Net effect: stopping a semi-autonomous/manual run that is paused at an approval checkpoint leaves the orchestrator process …[trimmed]

**Fix (verifier-corrected):** The proposed fix has a defect: it clears state['awaiting_approval'] on shutdown 'exactly like the timeout path'. That is wrong — the resume path (orchestrator.py:11229-11236) re-arms an interrupted approval precisely BECAUSE awaiting_approval stays set when a run dies mid-pause ('The run died while paused at this checkpoint... re-arm the approval instead of silently skipping'); the checkpoint phase is already in completed_phases, so clearing the marker makes the next run sail past the checkpoint without any human decision. Corrected: add `if _SHUTDOWN.is_set(): return "shutdown", None` at the top of the while body with NO state mutation (mimicking how a crash/sys.exit leaves state today), and at both call sites (11234 re-arm, 11411 checkpoint pause) `if decision == "shutdown": return` from _run_app_pipeline immediately so process_app's finally releases the lock. There are exactly two production call sites (verified by grep). Put the unit test in tests/test_checkpoint.py — its existing harness (line 30) already runs _await_approval in a thread; set orchestrator._SHUTDOWN, assert ("shutdown", None) well under the timeout AND that state['awaiting_approval'] is still set, clear the flag in tearDown. Run: python3 -m unittest tests.test_checkpoint

### A-79 [LOW] --finalize-in is missing from main()'s session-id validation, so invalid input crashes _do_finalize with an AttributeError traceback

**Where:** `orchestrator.py:10186`  ·  lens: orch-core-d  ·  verified: CONFIRMED

**Evidence:** main() validates operator-supplied session ids for `args.app, args.project, args.resume, args.fork, args.promote, args.route_from, args.route_to, args.archive_project, args.unarchive_project` (lines 12542-12544) and issues a clean ap.error for anything parse_session_id rejects — but `args.finalize_in` is absent from that tuple. _do_finalize then does `project = parse_session_id(args.finalize_in).split("/")[0]` (line 10186). parse_session_id returns None for any invalid id — confirmed by probe: parse_session_id('..') / ('a/b') / ('../x') / ('') all return None, and None.split raises `AttributeError: 'NoneType' object has no attribute 'split'`. So `orchestrator.py --finalize-artifact X --finalize-in 'a/b'` (a natural mistake — two-segment ids look plausible) dies with an unhandled traceback and exit code 1 instead of the documented clean exit-2 CLI error. No traversal is possible (None short-circuits any path use), so this is a crash/UX defect, not a security hole. Contrast _do_route_push (line 10126), whose route_from/route_to inputs ARE pre-validated by the main() loop.

**Fix (verifier-corrected):** The proposed fix is correct: add `args.finalize_in` to the tuple at 12542-12544 and optionally guard the parse in _do_finalize (`sid = parse_session_id(args.finalize_in); if sid is None: emit(...); return 2`). One refinement: the existing validation behavior is pinned in tests/test_nested_layout.py (the module containing 'invalid project name' assertions), so add the regression case there rather than pointing at tests.test_release_gate. Verify: python3 orchestrator.py --finalize-artifact x --finalize-in 'a/b' should exit 2 via ap.error with no traceback.

### A-80 [LOW] search.py module docstring describes an events cursor ("ev|<project>") that the code no longer implements — events.jsonl is fully re-read and artifacts fully re-indexed every tick

**Where:** `search.py:29`  ·  lens: support-modules  ·  verified: CONFIRMED

**Evidence:** The module docstring (lines 29-32) states: 'artifact_published events ... are indexed from events.jsonl through the same cursor machinery under the "ev|<project>" cursor key.' No code writes or reads an "ev|"-prefixed cursor: index_incremental only stores per-project message cursors (line 478), and _index_artifacts (lines 377-434) unconditionally does `DELETE FROM artifacts WHERE project=?` then re-reads the ENTIRE events.jsonl (`old_events = list(fh)`) and re-inserts every artifact row (and its FTS row) on every single index tick. The only surviving reference to "ev|" is the delete in _prune_vanished (line 327-328), which removes a cursor that is never created. The docstring's contract (incremental event indexing) contradicts the implementation (full rescan per tick) — a correctness-of-contract drift, plus O(events + artifacts bodies) disk work on every poll for every project.

**Fix (verifier-corrected):** As proposed — docstring-only edit to lines 29-32 describing the full-rescan reality and noting the ev|<project> key survives only as a defensive delete in _prune_vanished for older DBs; no behavior change. tests.test_search confirmed currently green (15 tests OK).

### A-81 [LOW] shepherd.sh queue-lanes override is sticky: deleting .orch-queue-order.json never restores the configured MAX_BUILDS

**Where:** `shepherd.sh:105`  ·  lens: scripts-ci  ·  verified: CONFIRMED

**Evidence:** Line 18 sets MAX_BUILDS once from ORCH_MAX_BUILDS (default 3). Each loop iteration runs `_l=$(queue_lanes); [ -n "$_l" ] && MAX_BUILDS=$_l` (line 105) — it only ever overwrites MAX_BUILDS when the GUI queue file exists and has a valid positive int. If the GUI writes {"lanes":1} and the user later deletes the queue file (or the GUI removes the lanes key) to go back to defaults, queue_lanes emits nothing and MAX_BUILDS stays pinned at the last override (1) until shepherd is restarted. The comment contract at line 19-20 ('lanes overrides MAX_BUILDS') implies no-file means no override, but the code makes the override permanent for the process lifetime.

**Fix (verifier-corrected):** Fix is correct and minimal (capture DEFAULT_MAX_BUILDS near line 18, restore it when queue_lanes emits nothing). The suggested test needs the same caveat as the lane-counter finding: there is currently no hook to drive one loop iteration, so either factor the assignment into a function exercised by a new --check-style hook (matching the existing --check-lock/--check-disabled pattern) or accept a code-review-only fix.

### A-82 [LOW] situations.ensure_seeded writes six untracked dirs into the engine repo — situations/ is neither committed nor gitignored

**Where:** `situations.py:335`  ·  lens: v3-stack  ·  verified: CONFIRMED

**Evidence:** orchestrator.py:939 calls sitlib.load_situation(ref, HERE, ...) and conductor.py:1219 calls sitlib.load_situation(ref, os.path.dirname(os.path.abspath(__file__)), ...) — both pass the ENGINE checkout as orch_dir. load_situation/list_situations unconditionally call ensure_seeded(orch_dir) (situations.py:115, 139), which creates situations/<name>/situation.json for all six seeds. Verified in the repo: `ls situations` -> no such dir; `git check-ignore situations` -> not ignored; .gitignore has no situations entry. So the first run that references a Situation dirties the engine working tree with six untracked directories. This is inconsistent with the sibling seed mechanisms: workflows.ensure_seeded's seeds (workflows/*.json) and the section seeds (sections/*/section.json) are all COMMITTED, so their ensure_seeded never creates untracked files in the engine checkout. Untracked engine-dir dirt is also a hazard given the shared checkout used by other agents (accidental `git add -A` scoops them up).

**Fix (verifier-corrected):** Fix is correct and consistent with the workflows/sections precedent: materialize via situations.ensure_seeded(engine_dir), verify the six files' parsed content matches _SEEDS-derived docs (note ensure_seeded writes json.dump(indent=2) with no trailing newline — compare parsed JSON, not bytes), git add exactly those six situation.json files (never git add -A), one dedicated commit, plus an optional byte-parity test mirroring the sections one. Do not gitignore.

### A-83 [LOW] Wake-latency assertions in test_conversational.py leave ~200ms slack — flake risk on loaded shared CI runners

**Where:** `tests/test_conversational.py:260`  ·  lens: tests-hygiene  ·  verified: CONFIRMED

**Evidence:** test_message_wakes_in_under_750ms (line 260) and test_missing_inbox_never_crashes_and_end_still_wakes (line 300) assert elapsed < 0.75s where legitimate worst-case latency is ~0.55s: a writer thread sleeps 0.3s before writing, and _await_inbox (orchestrator.py, poll=0.25 default) can miss the write by up to one full 250ms tick. Only ~200ms of slack absorbs thread-start latency, file I/O, and scheduler delay — on contended ubuntu-latest shared runners (2 vCPU) that margin is occasionally exceeded. Same pattern, milder: tests/test_checkpoint.py:37 sleeps a fixed 0.3s then asserts the worker thread has already set state['awaiting_approval'], assuming the thread got scheduled and reached the flag-set within 0.3s. These are deliberate latency gates and pass comfortably on an idle machine, hence low severity; I did not observe an actual failure.

**Fix (verifier-corrected):** The proposed fix is correct and the 1.5s bound still discriminates: with the legacy ~2s poll, the earliest wake after the 0.3s write would be the ~2.0s tick, well above 1.5s, so the property (sub-second tick) is still pinned. One addition for the test_checkpoint.py change: after the bounded poll, keep the existing assertEqual so a 5s-deadline expiry still fails with the same message, i.e. `deadline = time.time() + 5\nwhile time.time() < deadline and state.get("awaiting_approval") != phase: time.sleep(0.02)\nself.assertEqual(state.get("awaiting_approval"), phase)`. Verify with `python3 -m unittest tests.test_conversational tests.test_checkpoint`.

### A-84 [LOW] TestSandboxWrapFunctional (real sandbox-exec deny check) never runs in CI — only test-running jobs are ubuntu, where it is skipped

**Where:** `tests/test_verify_http_sandbox.py:69`  ·  lens: tests-hygiene  ·  verified: CONFIRMED

**Evidence:** Skip audit result. `@unittest.skipUnless(shutil.which("sandbox-exec"), "sandbox-exec is macOS-only")` gates the 2 functional tests (sandboxed write inside build dir works; write to ~/.ssh is blocked). CI's only test job runs on ubuntu-latest (ci.yml line 12) where sandbox-exec is absent -> both skip; the sole macos-latest job (gui-build, ci.yml line 60) runs only `swift build`/`swift test`, never the Python suite. So the one end-to-end proof that the verify.py http-boot sandbox actually denies sensitive writes relies entirely on the developer's local `make verify`. For completeness on the skip audit: the other platform-conditional pair, TestNonDarwinGuard in tests/test_install_launch_agent.py:31 (the 2 tests skipped locally on macOS), IS covered — it runs on every ubuntu CI job — and all remaining skipUnless conditions (bash, git, npm, swift, FTS5) are satisfied both locally and on CI, so no other skips hide gaps.

**Fix (verifier-corrected):** The proposed fix is sound with two refinements: the new step in the gui-build job must NOT inherit `working-directory: gui` (run it at the repo checkout root, where the tests/ package lives), and `actions/setup-python@v5` is optional since macos-latest runners ship python3 — but pinning 3.12 is harmless and more reproducible. Keep the run scoped to `python3 -m unittest tests.test_verify_http_sandbox -v` rather than the full suite (other modules' behavior on macOS is unvetted in CI, e.g. TestNonDarwinGuard skips there). Verify in the job log that the 2 TestSandboxWrapFunctional tests show 'ok' not 'skipped'.

### A-85 [LOW] uicrawl/visualqa run xcodebuild through plain subprocess.run — the exact reap deadlock procutil exists to prevent

**Where:** `uicrawl.py:39`  ·  lens: security  ·  verified: CONFIRMED

**Evidence:** `uicrawl._run` (line 37-43) is `p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout, env=env)`, and it is the wrapper used for `["xcodebuild", "build-for-testing", ...]` (line 98-104) and `["xcodebuild", "test-without-building", ...]` with `timeout=int(max_seconds)+420` (line 146-152). visualqa.py:53-59 is the same wrapper for every `xcrun simctl` call.

This is precisely what procutil.py's module docstring forbids: "subprocess.run(cmd, timeout=T, capture_output=True) does NOT reliably honor its timeout. On TimeoutExpired it kills only the direct child and then calls communicate() a second time (with no timeout) to reap it. If that child had spawned a helper/grandchild that inherited our stdout/stderr pipe — which ... xcodebuild does via its build-service daemons — the pipe never sees EOF, so the reaping communicate() blocks forever and the whole build deadlocks." verify.py:46-49 repeats the same warning for its own xcodebuild calls and routes them through procutil.run_capture.

Consequences: (a) a hung xcodebuild in the ui_crawl/visual_qa release gate …[trimmed]

**Fix (verifier-corrected):** The rewrite is correct and worth doing (it buys process-group kill + kill_live_groups tracking + optional heartbeat), but the rationale must be leak/consistency, not deadlock — do not repeat the 'run hangs forever' claim in a comment, it is false on Python >= 3.9. Test-file names in the claim are wrong: the suites are `python3 -m unittest tests.test_ui_crawl tests.test_visual_qa` (there is no tests/test_uicrawl.py). Also note the behavioral delta the fix introduces: timeouts start returning code 124 instead of 1, so check uicrawl's `"no crawl report (exit %d)"` message and any test asserting on it.

### A-86 [LOW] Seatbelt profile is malformed (and rule-injectable) when a denied or write_root path contains a double-quote

**Where:** `verify.py:504`  ·  lens: verify-stack+security  ·  verified: CONFIRMED

**Evidence:** The profile is assembled with raw `%s` interpolation of filesystem paths: line 503-505 `['  (subpath "%s")' % p for p in deny_paths]` and line 509 `lines.append('(allow file-write* (subpath "%s"))' % os.path.abspath(write_root))`. Nothing escapes `"` or parentheses, and `write_root` is the build_dir, i.e. `<workspace>/<project>/app_build` — `orchestrator.valid_app_slug` (orchestrator.py:10017-10024) rejects only '..', separators and a leading '.', so a project name containing `"` (or a workspace/HOME path containing one) reaches this line verbatim.

Reproduced in the scratchpad with `_SANDBOX_DENY_WRITE_SUBPATHS` monkeypatched to a scratch dir:
- benign write_root: write into the denied subpath -> `rc=1 stderr='...: Operation not permitted' wrote=False` (sandbox works).
- `write_root = .../proj"weird`: `rc=65 stderr='sandbox-exec: unbound variable: weird" at ...verify_sandbox_*.sb, line 7'` — the command never runs. In `_verify_web` that non-zero exit does not match `_NPM_ENV_FAILURE_RE`, so verification reports `ran=True, ok=False, "npm install FAILED (dependency/manifest error)"` …[trimmed]

**Fix (verifier-corrected):** Fix as written is correct — I verified that a backslash-escaped quote inside an SBPL string parses and the deny still matches (a path containing a literal `"` was correctly denied once escaped as `\"`). Two additions: apply `_sb_path` to `engine_dir` too (it is appended to deny_paths on line 501 and is equally interpolated), and prefer the fail-closed variant the claim mentions as an option — if any path contains a character outside a conservative allow-set, return `plain, None` — because a mis-escaped profile that still parses is strictly worse than the documented unsandboxed fallback. Tightening valid_app_slug is good hygiene but is not the load-bearing fix (HOME/workspace paths bypass it entirely). Severity lowered to low: on a local single-user tool the only party who can plant a quoted directory name is the operator or an agent already holding workspace write access.

### A-87 [LOW] _verify_http leaks its Seatbelt profile temp file on every successful verification — contradicting _run_sandboxed's docstring

**Where:** `verify.py:639`  ·  lens: verify-stack  ·  verified: CONFIRMED

**Evidence:** grep shows sandbox_profile_path is removed only in the Popen-failure except branch (lines 586-588); the normal path (server booted or timed out) reads the log tail, removes out_path (line 641) and returns — the mkstemp'd verify_sandbox_*.sb file is never deleted, accumulating one temp file per HTTP verification. _run_sandboxed's docstring (line 713) claims it cleans up "like _verify_http does for the boot command", which is only true for the failure branch, evidencing the leak is unintended.

**Fix (verifier-corrected):** Fix as proposed is correct: remove the profile in the same finally that removes out_path (sandbox-exec parses the profile at process start, and the process is dead by then, so removal is safe). Assert non-existence after a successful boot-verify in tests/test_verify_http_sandbox.py.

### A-88 [LOW] Visual QA skip paths leave a stale docs/visual_qa.json from an earlier pass in place — GUI and eval harness read outdated verdicts

**Where:** `visualqa.py:364`  ·  lens: verify-stack  ·  verified: CONFIRMED

**Evidence:** Every early skip (Ollama down line 357, zero vision models installed line 364, simulator build failure line 371, no simulator line 380, no screenshots line 392) returns None BEFORE the docs/visual_qa.json write at line 417, so a verdict file persisted by a previous pass survives untouched. Sequence: pass 1 fails visual QA (verdict FAIL persisted) -> repair rewrites the UI -> pass 2 skips (e.g. Ollama died) -> app is marked done while docs/visual_qa.json still says FAIL; the inverse (stale PASS blessing an unseen post-repair UI) also holds. evalharness.score_project (line 82) and the GUI read this file as current-run evidence. Answering the panel-quorum question directly: with 0 vision models the gate skips (returns None, gate green) and records that only in the run log, never in the persisted verdict file.

**Fix (verifier-corrected):** Fix as proposed is right (factor a _persist(app_dir, result) helper; write {"verdict": "SKIPPED", "reason": <skip message>, "models": [...]} in each skip branch — six branches, including the no-bundle-id one, not five). Consider adding a timestamp field so consumers can distinguish runs. Test in tests/test_visual_qa.py as suggested.
