# JIRA_BACKLOG.md — epics, tasks, subtasks for the bot portfolio (sync source)

Sync agent: create everything below in Jira exactly as specified, top to bottom. Read `PORTFOLIO_PLAN.md` (same folder) first for governance context.

## Site + field map

- Site `priyanshchordia-1779372280524.atlassian.net`, cloudId `e0c4564f-4721-4dc1-b829-fe284a36b763`.
- Projects: ORCH8, PT, DOCS8 (company-managed; shared Task type 10012 — discover Epic/Subtask type ids via createmeta), LIPI (team-managed; Task issueTypeId 10353).
- Select fields take `{"value": "<option>"}`. Company-managed (ORCH8/PT): Role `customfield_10294`, Model `customfield_10295`, Effort `customfield_10296`, Interaction `customfield_10297`. LIPI: Role `customfield_10290`, Model `customfield_10291`, Effort `customfield_10292`, Interaction `customfield_10293`.
- All projects: Risk Level `customfield_10204` (Low/Medium/High/Critical), Primary Agent `customfield_10206`, Acceptance Criteria `customfield_10183` (ADF), estimate via `timetracking.originalEstimate`. Textarea custom fields require ADF documents.
- Description skeleton: `# Objective / # Why / # Scope / # Out of scope / # Acceptance criteria / # Dependencies / # Estimate` — expand each task's `body` into it.
- **Dependencies must be created as issue links** (`Blocks` / `is blocked by`), including cross-project. The `deps:` line names the blocking items by their heading; resolve to keys after creation and link.
- Custom fields go on tasks. Subtasks need only summary + description + labels (inherit context from parent); set fields on subtasks too only if trivial to do.
- Dedup rule: before creating anything, search the target project for an issue with the same summary; if found, reuse it (record its key) instead of creating.
- Every issue gets its epic's bot label + a priority label. `kind: workable` → also `agent-task`; `kind: owner` → `owner-action`, never `agent-task`; `kind: tracking` → `repo-tracked`, never `agent-task`.

## AFTER creating everything

In `pri8771/lipi-standard-store`, `ops/JIRA_SYNC_QUEUE.md` has 15 blocks under PENDING SYNC staged 2026-08-31 for these same ventures. They are SUPERSEDED by this file. Move all 15 to the SYNCED section with annotation `→ superseded by portfolio backlog (orchestrator repo), created as <matching new key>` (match by topic; a few have no 1:1 match — annotate `→ superseded, restructured`). Use the lease protocol (`ops/LOCK.json` acquire → edit → WORKLOG entry → release in final push).

---

## EPIC E1: "CommerceLint Ops" — ORCH8, label `bot:commercelint`

Epic description: Operate and monitor the CommerceLint 90-day $0 experiment (ends 2026-11-21). Primary score: verified net cash received. Current bottleneck: qualified traffic + market validation. Framework lane: LangGraph.

### E1-T1: Build LangGraph monitor for CommerceLint operator loop
kind: workable | priority: P1 | role: engineering | model: sonnet | effort: high | interaction: proposal | risk: Medium | estimate: 4h
deps: none
body: LangGraph check→classify→escalate watcher on the always-on Windows box polling GitHub Actions on pri8771/autonomous_apps: hourly-operator freshness vs the 75-min watchdog threshold, watchdog.yml, production-smoke.yml. On breach follow the recovery runbook (dispatch watchdog → confirm recovery → rerun to close → production smoke → verify state+site). Alerts to Slack via Kai; silent when green. Read-only + Actions-dispatch only.
Subtasks:
- E1-T1a: Scaffold LangGraph project + config on Windows box (venv, repo location, gh auth reuse)
- E1-T1b: Freshness + smoke checks via GitHub API with 75-min threshold logic
- E1-T1c: Watchdog recovery runbook automation (dispatch→confirm→close→smoke), no duplicate dispatch when current
- E1-T1d: Slack alert wiring via Kai, delta-style (silent when green)
- E1-T1e: 48h soak test + runbook doc

### E1-T2 (PT): Reauthenticate CommerceLint Gmail OAuth
kind: owner | priority: P1 | role: ops | model: sonnet | effort: low | interaction: execution | risk: Medium | estimate: 15m
deps: none | blocks: E1-T3
body: Owner gate: Gmail OAuth for the CommerceLint sender needs reauthentication; blocks outreach-evidence reconciliation. Details in owner handoff (out of band); the private sender address must not be documented anywhere.

### E1-T3: Reconcile CommerceLint outreach evidence
kind: workable | priority: P1 | role: ops | model: sonnet | effort: low | interaction: inspection | risk: Medium | estimate: 1h
deps: E1-T2
body: Private CRM shows 0 sent outreach; coordination/agency-outreach-experiment-2026-08-24.md records 2 Gmail sends verified in Sent on Aug 24. After reauth, narrowly verify only the tracked threads (never bulk-read), then record supported reconciliation in CRM + audit diary. Never change counts from the historical note alone.

### E1-T4: CommerceLint traffic + market-validation push
kind: tracking | priority: P1 | role: marketing | model: opus | effort: high | interaction: proposal | risk: Medium | estimate: n/a
body: Mirror for visibility: the bottleneck work runs in CommerceLint's own GitHub-Actions operator lane under its $0/no-spam constraints, not from this board.

## EPIC E2: "Wait How Big Launch" — ORCH8, label `bot:waithowbig`

Epic description: Take the Wait, How Big? social operator from broken-cron to verified launch (public post IDs on X/Instagram/TikTok). $0, no card/trial, kill switch preserved. Framework lane: CrewAI (post-launch).

### E2-T1: Fix WHB operator module-shadowing bug
kind: workable | priority: P0 | role: engineering | model: sonnet | effort: medium | interaction: proposal | risk: High | estimate: 2h
deps: none | blocks: E2-T2, E2-T4
body: In pri8771/orchestrator wait-how-big-social: packaged entrypoint operator.py shadows Python stdlib operator (ImportError, Actions run 33342049065). PR flow; owner merges.
Subtasks:
- E2-T1a: Rename entrypoint to whb_operator.py + update workflow command
- E2-T1b: Deterministic ZIP rebuild + updated verified SHA-256
- E2-T1c: py3.12 regression test proving packaged startup; missing-key path exits WAIT_HOW_BIG_NOT_CONFIGURED

### E2-T2: Add safe bootstrap gate to WHB workflow
kind: workable | priority: P0 | role: engineering | model: sonnet | effort: medium | interaction: proposal | risk: High | estimate: 1h
deps: E2-T1 | blocks: E2-T4
body: The 3-hourly cron runs dry_run=false, so installing WHB_BUFFER_API_KEY today could publish. Gate so a newly installed key cannot trigger a recurring normal run before a verified dry run; preserve WHB_KILL_SWITCH; document the protection.

### E2-T3: Live re-verify WHB Buffer channels and brand assets
kind: workable | priority: P1 | role: marketing | model: sonnet | effort: low | interaction: inspection | risk: Low | estimate: 1h
deps: none
body: Re-check the three connected Buffer channels + public profiles live (old zero-post baseline is stale): X avatar/banner/display-name/bio vs canonical, Instagram Creator account state, TikTok avatar/bio. Recover assets from MANUAL_BOOTSTRAP.md / ACCOUNT_LAUNCH_PACKET.md in the project Drive if tmp copies are gone.

### E2-T4 (PT): Generate WHB Buffer API key + install GitHub secret
kind: owner | priority: P0 | role: ops | model: sonnet | effort: low | interaction: execution | risk: High | estimate: 20m
deps: E2-T1, E2-T2 | blocks: E2-T5
body: Owner gate: Generate creates persistent posting access. Name Wait How Big Operator, 1-year expiry, scopes account:read/posts:read/posts:write only; value straight into Actions secret WHB_BUFFER_API_KEY, never printed/stored/committed.

### E2-T5: WHB dry run + verification
kind: workable | priority: P0 | role: ops | model: sonnet | effort: low | interaction: inspection | risk: Medium | estimate: 45m
deps: E2-T4 | blocks: E2-T6
body: Dispatch the operator once with dry_run=true. Verify: success; exactly the three intended channels; media validation passes; NO Buffer/public post created; honest dry-run receipt in state.json; no duplicate/partial scheduling. Failure → diagnose+fix, never proceed to normal publishing.

### E2-T6 (PT): Owner decision — clear WHB kill switch / first live publish
kind: owner | priority: P0 | role: ops | model: opus | effort: low | interaction: execution | risk: Critical | estimate: 15m
deps: E2-T5 | blocks: E2-T7
body: Separate explicit owner decision on the dry-run evidence. Launch = verified public post IDs, URLs, timestamps on all three platforms; nothing less counts.

### E2-T7: Stand up CrewAI content crew for WHB
kind: workable | priority: P2 | role: marketing | model: opus | effort: high | interaction: proposal | risk: Medium | estimate: 4h
deps: E2-T6
body: Post-launch CrewAI research→draft→schedule pipeline feeding the Buffer queue as DRAFTS only; publishing stays behind operator gates. CrewAI app already present in the Slack workspace.

## EPIC E3: "One Person Ops" — ORCH8, label `bot:onepersonops`

Epic description: X growth campaign (@OnePerson0ops): 1,000 followers/30 days, $0, controller-driven, deliberately manual. Mac-resident (files outside git) — tracking-only from Windows. Framework lane: Hermes/Ollama (support).

### E3-T1: OPO controller run + verification-first publication
kind: tracking | priority: P1 | role: marketing | model: opus | effort: medium | interaction: execution | risk: High | estimate: n/a
deps: E3-T2
body: Mac lane: fresh campaign_control.py + unittest run; fix Buffer workspace binding to @OnePerson0ops (org switcher; stop at manual sign-in gate; never touch the waithowbig workspace); publish the exact controller-selected verification-first original with privacy preflight passed, selection <5 min old, fresh action-time authorization. Not executable from Windows.

### E3-T2 (PT): OPO owner gates — Buffer sign-in + publish authorization
kind: owner | priority: P1 | role: ops | model: sonnet | effort: low | interaction: execution | risk: High | estimate: 20m
deps: none | blocks: E3-T1
body: Manual Buffer sign-in to the workspace holding @OnePerson0ops if absent from the org switcher, plus fresh action-time authorization for the verification-first publication (prior authorization never replays).

### E3-T3: Evaluate Hermes agent via Ollama for portfolio monitoring
kind: workable | priority: P2 | role: engineering | model: sonnet | effort: medium | interaction: advisory | risk: Low | estimate: 2h
deps: none
body: Install Ollama on the Windows box, pull a Hermes model, prototype as the shared local low-cost lane: portfolio watcher/drafter + the planned local Jira-backfill agent. Advisory: recommendation + working proof-of-concept, no standing automation without owner approval.

## EPIC E4: "Agent Infrastructure" — ORCH8, label `bot:kai`

Epic description: Kai/OpenClaw platform work: support-loop completion, delegation lanes, monitoring plumbing, Jira hygiene enforcement.

### E4-T1: Fix Slack app event subscriptions for #lipi-support
kind: workable | priority: P0 | role: ops | model: sonnet | effort: low | interaction: proposal | risk: Medium | estimate: 45m
deps: none | blocks: E4-T2
body: The Slack app only subscribes to app_mention; unmentioned messages in the private support channel never reach the gateway (verified in logs 2026-08-31). Add message.groups (+ message.channels/message.im as appropriate) to the app's Event Subscriptions at api.slack.com, reinstall app if scopes change, verify an unmentioned message produces an inbound event.

### E4-T2: Support draft-loop end-to-end verification + ingestion wiring
kind: workable | priority: P0 | role: ops | model: sonnet | effort: medium | interaction: proposal | risk: Medium | estimate: 2h
deps: E4-T1
body: Prove the loop: message lands in #lipi-support → Kai classifies + drafts per SUPPORT.md → DMs owner for approval, no in-channel reply. Then wire ingestion with the owner: Gmail auto-forward of support@ to the channel; Shopify admin notifications to the channel. SUPPORT.md/AGENTS.md already deployed.

### E4-T3 (PT): Unblock gemini lane — AI Studio API key
kind: owner | priority: P2 | role: ops | model: sonnet | effort: low | interaction: execution | risk: Low | estimate: 10m
deps: none
body: Google retired the free-tier Gemini CLI (UNSUPPORTED_CLIENT). Owner creates a free AI Studio API key and places it in ~/.gemini/.env as GEMINI_API_KEY=<key>; then auth type switches to gemini-api-key. Until then the gemini lane falls back to Kai.

### E4-T4: Wire Option A native backends (codex/gemini) in OpenClaw
kind: workable | priority: P2 | role: engineering | model: opus | effort: medium | interaction: proposal | risk: Medium | estimate: 2h
deps: E4-T3
body: Flip delegation from shell workers to native OpenClaw backends per workspace DELEGATION.md Option A: enable codex plugin + gemini backend, map openai/* and google/* model refs, per-backend MCP config, validate + isolated-cron smoke per lane. Only on explicit owner go.

### E4-T5: Portfolio monitor Slack routing (#portfolio-ops)
kind: workable | priority: P1 | role: ops | model: sonnet | effort: low | interaction: proposal | risk: Low | estimate: 1h
deps: none
body: Create/designate the alert channel, add to Kai's allowlist, route all portfolio monitors through Kai delta-style: message only on breach/change, silent when green.

### E4-T6: Jira hygiene enforcement in Kai's sweep
kind: workable | priority: P1 | role: ops | model: sonnet | effort: low | interaction: proposal | risk: Low | estimate: 1h
deps: none
body: Implement PORTFOLIO_PLAN.md hygiene rules: sweep refuses malformed tickets (comment missing fields + label needs-triage + skip); open is-blocked-by links make a ticket non-workable; weekly hygiene metrics in the Monday digest.

### E4-T7: Cron loop verification checkpoint
kind: workable | priority: P2 | role: ops | model: haiku | effort: low | interaction: inspection | risk: Low | estimate: 30m
deps: none
body: After a full day of scheduled firings, read sweep/poll cron run history; confirm silent-on-empty, no spurious pickups, delta-only DMs; record in workspace SYSTEM.md.

## LIPI board additions (no epic; label `lipi`)

### L-T1: Draft soft-launch product-subset proposal
kind: workable | priority: P0 | role: store | model: opus | effort: medium | interaction: proposal | risk: Medium | estimate: 2h
deps: LIPI-21, LIPI-22 (existing issues — link them)
body: The biggest launch-schedule lever: launch with a small fully-verified subset instead of all 7-17 products. Proposal: which products, what each still needs (20% all-in floor per BUSINESS_RULES §1, imagery, fulfillment mapping, QA), recommended launch set. Advisory/proposal only; owner picks.

### L-T2: Executor v1 — scoped write path for approved actions
kind: workable | priority: P1 | role: engineering | model: opus | effort: high | interaction: proposal | risk: Critical | estimate: 8h
deps: LIPI-49 (redaction fix), LIPI-26 (margin-floor wiring) — link existing issues
body: Design + build the deterministic executor: scoped Shopify/Printful credentials behind the ActionEnvelope so approved actions (send support reply, product edit) execute without exposing credentials to any model. Owner-approval gate on every action class; starts with support-reply send. PR flow, cold adversarial review, owner merges. No write capability ships before LIPI-49 is fixed.
