# Orchestrator V2 — One-Shot Build Specification for Fable

This document is the single handoff spec for building Orchestrator V2. It replaces the earlier architecture-only master spec with a buildable, scoped, one-shot implementation brief.

The goal is not to implement every future idea. The goal is to build a complete native macOS V2 MVP that is useful immediately, proves the architecture, and creates a safe foundation for later modules.

## 0. Non-negotiable product promise

Build a native macOS app that lets a single user enter one prompt, choose which AI agents can participate, and run a structured multi-agent workflow that produces a verified app artifact, documentation, and an honest readiness report.

The system must never claim something is done just because an agent said so. It must distinguish:

- `VERIFIED`: an actual tool ran and passed.
- `FAILED`: an actual tool ran and failed.
- `UNVERIFIED`: the toolchain was missing, blocked, skipped, unavailable, or no valid verification result exists.

The V2 MVP is local-first, single-user, and macOS-native. It should use existing subscription/local CLI sessions for cloud agents and Ollama for local models. No multi-user SaaS, no billing system, no automatic GitHub repo creation, and no cloud deployment automation in the MVP.

## 1. Source of truth and starting point

Use the current committed source snapshot as the starting implementation reference if present in the repo:

```text
./
  orchestrator.py
  workflows.py
  roles.py
  roles.json
  knowledge.py
  verify.py
  config.yaml
  README.md
  run.sh
  workflows/*.json
  knowledge/**/*.md
  gui/Sources/OrchestratorGUI/*.swift
  gui/Package.swift
  sample-run/sprint-demo/...
```

If the working repository contains the live `.orchestrator/` folder instead, modify that folder directly. Preserve current working behavior unless this spec explicitly replaces it.

Do not fabricate source files from memory if equivalent files already exist. Inspect the current source first, then apply this spec as an implementation overlay.

## 2. MVP scope

Build exactly these capabilities in V2 MVP:

1. Native macOS SwiftUI control app.
2. Local Python orchestration engine, launched by the Mac app but able to continue independently of the UI process.
3. Provider/agent settings for Codex, Claude, Gemini/Antigravity, and local Ollama models.
4. Enable/disable agents per run.
5. One prompt creates or opens a project.
6. Workflow profiles: `Prototype`, `MVP`, `V1`, and `Production Draft`.
7. Autonomy modes: `Fully Autonomous`, `Semi-Autonomous`, and `Manual`.
8. Structured phase pipeline for app creation.
9. Structured verification gate with persisted `verify_results.json`.
10. Secret redaction before persisted logs/transcripts/verification errors.
11. Persistent git repo inside each project `app_build/`.
12. Worktree-isolated build lanes with deterministic patch integration.
13. `tasks.json` backlog and `interfaces.json` interface contract before build.
14. Deterministic documentation rendering into `docs/PROJECT_DOCUMENTATION.md` and supporting docs.
15. Native GUI views for projects, phases, transcripts, artifacts, verification state, settings, and local models.
16. Ollama detection and guided installation/pull flow for a small curated local-model list.
17. Sample demo project visible on first launch.
18. Test harnesses for verification status, redaction, schema parsing, and worktree patch conflicts.

Everything else is out of scope unless listed as an extension point.

## 3. Explicitly out of scope for MVP

Do not implement these in the first build:

- Multi-user accounts or workspaces.
- SaaS backend.
- Billing/subscriptions.
- Automatic GitHub repo creation.
- Automatic PR creation or merging.
- Notion, Google Drive, Slack, Linear, Jira, or email integrations.
- App Store Connect upload.
- TestFlight upload.
- Real Firebase/Supabase/cloud deployment.
- Browser computer-use design tool automation.
- Web app target generation.
- Portfolio/library mining workflow.
- Full SQLite event-log migration, except the global worker cap may use stdlib SQLite if implemented.
- Full container/chroot sandboxing.
- A marketplace of workflows.

Design extension points for these, but do not build them.

## 4. Architecture overview

Implement as one native macOS app plus a local engine.

```text
Orchestrator.app (SwiftUI macOS)
  ├── Project sidebar
  ├── Run/phase detail UI
  ├── Transcript/artifact viewer
  ├── Settings
  ├── Local model manager
  └── Engine launcher/status monitor

Local Python engine
  ├── Workflow engine
  ├── Agent adapter layer
  ├── Knowledge injection
  ├── Structured artifact parser
  ├── Build/worktree manager
  ├── Verification/repair loop
  ├── Secret redaction
  ├── Documentation renderer
  └── File-based project state
```

The MVP may remain file-state-first. SQLite is optional only for cross-process worker-slot caps. Do not require a database for normal project state in the first implementation.

## 5. Data directory and project layout

Default root:

```text
~/Documents/autonomous_apps
```

Allow this to be changed in Settings.

Each project lives at:

```text
<root>/<project_slug>/
  initial_prompt/initial_prompt.md
  workflow.txt
  run_state.json
  agent_state.json                 # backward-compatible if existing code expects it
  verify_results.json
  tasks.json
  interfaces.json
  docs/
    phase_outputs.json
    PROJECT_DOCUMENTATION.md
    PRD.md
    TECHNICAL_ARCHITECTURE.md
    QA_REPORT.md
    LAUNCH_READINESS.md
    KNOWN_LIMITATIONS.md
  transcripts/
    <phase>.md
  logs/
    *.json
  review/
    code_review_findings.json
  app_build/                       # persistent git repo
  .orchestrator_runtime/
    worktrees/
    patches/
    live_log.jsonl
    locks/
    temp/
```

If the current implementation already stores phase transcripts under `<phase_folder>/<phase>.md`, keep backward compatibility by either continuing to write those files or symlinking/copying them into `transcripts/`. Do not break existing sample runs.

## 6. Project lifecycle

A project goes through this lifecycle:

```text
created
configured
queued
running
awaiting_approval
blocked_no_agents
blocked_conflict
failed
complete_at_target
complete
```

Minimum state fields:

```json
{
  "schema_version": 1,
  "project_id": "uuid-or-stable-slug",
  "project_slug": "string",
  "workflow": "app_build",
  "run_id": "run-0001",
  "status": "running",
  "created_at": "ISO-8601",
  "updated_at": "ISO-8601",
  "prompt_hash": "sha256",
  "current_phase": "scope_and_prd",
  "current_round": 1,
  "next_agent": "claude",
  "completed_phases": [],
  "phase_status": {},
  "phase_outputs": {},
  "consensus_status": {},
  "agent_health": {},
  "active_seconds_consumed": 0,
  "autonomy": "fully_autonomous|semi_autonomous|manual",
  "completeness": "prototype|mvp|v1|production_draft",
  "stop_after_phase": null,
  "error": null
}
```

Use atomic temp-file + `os.replace` for all state writes.

## 7. Run settings

### 7.1 Autonomy

- `Fully Autonomous`: checkpoints ignored except hard checkpoints that touch external accounts or conflicts.
- `Semi-Autonomous`: pause at phases marked `checkpoint=true`.
- `Manual`: pause after every phase.

Autonomy is fixed for a run. To change autonomy midstream, fork or start a new run.

### 7.2 Completeness

Implement four profiles:

```json
{
  "prototype": {
    "description": "Fast verified prototype",
    "phase_keys": ["problem_and_vision", "scope_and_prd", "feature_specs", "user_flows", "frontend_architecture", "task_assignments", "interface_contract", "build_coordination", "launch_readiness_review", "v2_handoff_and_decision_log"],
    "round_multiplier": 0.75
  },
  "mvp": {
    "description": "Useful MVP with tests and docs",
    "phase_keys": ["problem_and_vision", "scope_and_prd", "feature_specs", "user_flows", "screen_specs", "design_system", "frontend_architecture", "storage_and_persistence", "security_and_privacy", "accessibility_and_localization", "task_assignments", "interface_contract", "build_coordination", "qa_test_plan", "code_review", "launch_readiness_review", "v2_handoff_and_decision_log"],
    "round_multiplier": 1.0
  },
  "v1": {
    "description": "Launch candidate draft",
    "phase_keys": ["problem_and_vision", "scope_and_prd", "feature_specs", "user_flows", "information_architecture", "screen_specs", "design_system", "ux_copy", "frontend_architecture", "storage_and_persistence", "networking_and_api", "auth_and_payments", "analytics_and_notifications", "security_and_privacy", "accessibility_and_localization", "project_plan", "task_assignments", "interface_contract", "build_coordination", "qa_test_plan", "code_review", "legal_and_compliance", "deployment_and_release", "launch_readiness_review", "v2_handoff_and_decision_log"],
    "round_multiplier": 1.25
  },
  "production_draft": {
    "description": "Most complete local-first draft, still human-reviewed before real launch",
    "phase_keys": ["problem_and_vision", "scope_and_prd", "feature_specs", "user_flows", "information_architecture", "screen_specs", "design_system", "ux_copy", "visual_assets", "motion_and_animation", "frontend_architecture", "storage_and_persistence", "backend_and_hosting", "networking_and_api", "api_design", "auth_and_payments", "analytics_and_notifications", "security_and_privacy", "accessibility_and_localization", "project_plan", "task_assignments", "interface_contract", "build_coordination", "qa_test_plan", "code_review", "legal_and_compliance", "deployment_and_release", "launch_readiness_review", "v2_handoff_and_decision_log"],
    "round_multiplier": 1.5
  }
}
```

`backend_and_hosting`, `api_design`, and backend deployment must be decision phases only in MVP. Do not create real external cloud resources.

### 7.3 Turns

Each phase has a `rounds` cap. The GUI may expose `0`, `N`, or `∞` later, but for the MVP use profile defaults and per-phase workflow JSON values.

Structurally required phases cannot be skipped if included by the selected profile:

- `task_assignments`
- `interface_contract`
- `qa_test_plan` when the selected workflow supports tests
- `launch_readiness_review`

### 7.4 Stop target

Support optional `stop_after_phase`. Friendly labels:

- Idea Validated → `scope_and_prd`
- Docs Complete → last included pre-build documentation/design phase
- Design Complete → `design_system`
- Tech Spec Complete → `interface_contract`
- MVP Built → `build_coordination`
- Production Ready → `launch_readiness_review`

If set, the run stops at that phase and marks `complete_at_target`.

## 8. Phase model

Extend the current `Phase` model to include:

```python
@dataclass
class Phase:
    key: str
    folder: str
    file: str
    title: str
    purpose: str
    rounds: int
    roles: list[str]
    writes: bool = False
    reads_target: bool = False
    verify: bool = False
    checkpoint: bool = False
    structurally_required: bool = False
    requires_verification: bool = False
    doc_sections: list[str] = field(default_factory=list)
    test_deliverable: str | None = None
```

Backward compatibility: existing workflow JSON without new fields must still load with defaults.

## 9. Required phase menu

Implement or preserve these phase keys. Lower completeness profiles include subsets.

### Discovery

- `problem_and_vision`
- `scope_and_prd`
- `feature_specs`
- `user_flows`

### Design

- `information_architecture`
- `screen_specs`
- `design_system`
- `ux_copy`
- `visual_assets`
- `motion_and_animation`

### Technical

- `frontend_architecture`
- `storage_and_persistence`
- `backend_and_hosting`
- `networking_and_api`
- `api_design`
- `auth_and_payments`
- `analytics_and_notifications`
- `security_and_privacy`
- `accessibility_and_localization`

### Plan

- `project_plan`
- `task_assignments`
- `interface_contract`

### Build/review

- `build_coordination`
- `qa_test_plan`
- `code_review`
- `deploy_backend` placeholder only, checkpointed and disabled in MVP

### Launch/handoff

- `legal_and_compliance`
- `deployment_and_release`
- `launch_readiness_review`
- `v2_handoff_and_decision_log`

## 10. Agent/provider layer

Replace hardcoded agent lists with runtime-registered identities.

```python
@dataclass
class AgentIdentity:
    id: str                       # codex, claude, gemini, local:qwen2.5-coder:7b
    display_name: str
    provider: str                 # codex|claude|gemini|ollama
    kind: str                     # cloud_cli|local_llm
    command: list[str] | None
    enabled: bool
    can_coordinate: bool
    can_write: bool
    default_roles: list[str]
    timeout_multiplier: float = 1.0
```

Minimum built-in providers:

- Codex CLI
- Claude Code CLI
- Gemini CLI / Antigravity fallback
- Ollama local model runtime

Agent settings must allow users to:

- enable/disable each cloud provider
- test login/availability
- add/remove local Ollama models from active roster
- exclude local models from time-limited runs by default
- choose agents per run

Local models can participate as debate/review/build workers. They should not be default coordinator if any cloud agent is available.

## 11. Agent health and circuit breaker

Persist per-agent health in run state:

```json
{
  "status": "up|down",
  "failure_signature": "rate_limit|timeout|crash|malformed_output|null",
  "consecutive_failures": 0,
  "retry_after": "ISO-8601|null",
  "last_error_excerpt": "string|null"
}
```

Rules:

1. Nonzero exit, timeout, or malformed required structured output counts as a failure.
2. After 2 consecutive failures, mark agent `down`.
3. Rate-limit fingerprints get a longer cooldown, default 5 hours.
4. Other failures get a default 30 minute cooldown.
5. Cooldown doubles on repeated failures up to 4 hours, except known rate limits may keep the provider-specific longer value.
6. Check recovery once per phase boundary, not every turn.
7. If zero agents remain available, set project status `blocked_no_agents` and notify the user.

## 12. Local model manager

Use Ollama first.

### 12.1 Detection

Run:

```bash
ollama --version
```

If unavailable, show guided install.

### 12.2 Install flow

If Homebrew exists:

```bash
brew install ollama
```

If Homebrew is missing, do not run the Homebrew bootstrap script inside a non-interactive `Process()`. Open Terminal.app with the official Homebrew install command prefilled or displayed, then poll for `brew` to appear.

### 12.3 Model registry

Add `local_models.json`:

```json
{
  "schema_version": 1,
  "models": [
    {
      "id": "qwen2.5-coder:7b",
      "label": "Fast Local Coding Assistant",
      "runtime": "ollama",
      "pull_command": ["ollama", "pull", "qwen2.5-coder:7b"],
      "min_ram_gb": 16,
      "recommended_ram_gb": 24,
      "roles": ["implementation", "review", "summarization"]
    },
    {
      "id": "llama3.1:8b",
      "label": "General Local Assistant",
      "runtime": "ollama",
      "pull_command": ["ollama", "pull", "llama3.1:8b"],
      "min_ram_gb": 16,
      "recommended_ram_gb": 24,
      "roles": ["discussion", "summarization"]
    },
    {
      "id": "mistral:7b",
      "label": "Small Local Reviewer",
      "runtime": "ollama",
      "pull_command": ["ollama", "pull", "mistral:7b"],
      "min_ram_gb": 16,
      "recommended_ram_gb": 24,
      "roles": ["review", "summarization"]
    }
  ]
}
```

The UI may show these as recommendations. Do not allow arbitrary shell input to become a pull command in MVP. Advanced custom model entry can be added later.

### 12.4 Privacy boundary

State plainly in UI:

```text
Local-first does not mean every call stays on-device. Codex, Claude, and Gemini calls leave this Mac through their respective CLIs. Ollama-routed local model calls stay on this Mac.
```

## 13. Global worker cap

Implement if time permits; otherwise leave as a documented TODO with no partial broken behavior.

If implemented, use stdlib SQLite at:

```text
~/.orchestrator_global/workers.db
```

Table:

```sql
CREATE TABLE IF NOT EXISTS worker_slots (
  pid INTEGER NOT NULL,
  project_id TEXT NOT NULL,
  resource_class TEXT NOT NULL,
  claimed_at TEXT NOT NULL
);
```

Resource classes:

- `cli_remote`, default cap 12
- `local_model`, default cap 1

Use WAL mode and `BEGIN IMMEDIATE` for claim/release. Reap dead PID rows on every claim attempt.

## 14. Structured artifacts

Create `schemas.py` in the engine.

It must define dataclasses or TypedDict-style schemas and validation helpers for:

- `VerifyResult`
- `TaskItem`
- `InterfaceItem`
- `LiveLogEntry`
- `Finding`
- `PhaseStructuredOutput`
- `AgentIdentity`
- `AgentHealth`

Also create:

```python
extract_structured_blocks(text: str, fence_name: str, required_fields: list[str]) -> list[dict]
validate_required_fields(obj: dict, required_fields: list[str]) -> tuple[bool, list[str]]
```

Rule: invalid structured blocks must be logged visibly. Do not silently drop invalid JSON.

## 15. Verification results

Create or update `verify.py` with these helpers:

```python
verification_status(result: dict) -> str
persist_verify_result(app_dir: Path, phase_key: str, result: dict, attempt: int = 0, prompt_hash: str | None = None, workflow: str | None = None) -> dict
load_verify_results(app_dir: Path) -> list[dict]
latest_verify_result(app_dir: Path, prompt_hash: str | None = None, phase_key: str | None = None) -> dict | None
summarize_verify_results(results: list[dict], latest: dict | None) -> str
```

Persist to:

```text
<project>/verify_results.json
```

Schema:

```json
{
  "schema_version": 1,
  "timestamp": "ISO-8601",
  "app": "project slug",
  "workflow": "app_build",
  "phase": "build_coordination",
  "attempt": 0,
  "repair_attempt": false,
  "prompt_hash": "sha256-or-null",
  "ran": true,
  "ok": false,
  "status": "verified|failed|unverified",
  "tool": "xcodebuild|swift-build|node|python|http|shell|none",
  "scheme": "optional string or null",
  "summary": "short summary",
  "errors": "redacted/truncated errors",
  "tests": null
}
```

Status mapping:

- `verified`: `ran == true and ok == true`
- `failed`: `ran == true and ok == false`
- `unverified`: `ran == false`

Persist every verification attempt, including repair-loop attempts. Attempt `0` is initial verification; repair attempts are `1..N`.

## 16. Final verification gate

Phases with `requires_verification=true` must receive structured verification context.

If no matching latest result exists for current `prompt_hash`, inject:

```text
===== VERIFICATION RESULTS =====
Latest status: unverified
Reason: No current verification result exists for this prompt hash.
```

The final review coordinator must end with exactly one of:

```text
VERIFICATION: VERIFIED
VERIFICATION: FAILED
VERIFICATION: UNVERIFIED
```

The required label must be derived by the orchestrator, not the agent. The agent may explain it but cannot choose a different status.

If the label is missing, append a visible warning to the transcript and final output:

```text
⚠️ no VERIFICATION: label found; treating output as VERIFICATION: UNVERIFIED
```

For MVP, warn and continue. Do not crash the whole run.

## 17. Secret redaction

Create one shared function:

```python
redact_secrets(text: str) -> str
```

Apply before any persisted write involving agent/tool text:

- call logs
- transcripts
- verification errors
- repair prompt excerpts if persisted
- live log summaries
- code review findings sourced from agent output

Minimum redaction patterns:

- `Authorization:` headers
- `Bearer <token>`
- `sk-...` style tokens
- `ghp_...` GitHub tokens
- `AIza...` Google API keys
- `AKIA...` AWS access keys
- JWT-shaped tokens
- PEM private key blocks
- `KEY=`, `TOKEN=`, `SECRET=`, `PASSWORD=` assignments
- long high-entropy strings over a conservative threshold

Replace with:

```text
[REDACTED:<type>]
```

Also scan freshly written source files during `code_review` for hardcoded secrets. A hardcoded secret finding is `Critical` and must block launch readiness unless fixed or explicitly marked as test/mock placeholder by deterministic allowlist logic.

## 18. Build execution model

### 18.1 Persistent repo

`app_build/` is a persistent git repo for the lifetime of the project. Initialize it once if missing.

Branch/tag conventions:

```text
main integration branch: main
run branch: run/<run_id>
lane branch: run/<run_id>/lane/<lane_id>
worktree path: <project>/.orchestrator_runtime/worktrees/<run_id>/<lane_id>
patch path: <project>/.orchestrator_runtime/patches/<run_id>/<lane_id>/<task_id>.patch
run tag: run-0001
phase tag: run-0001-phase-<phase_key>
```

If an existing repo uses `master`, do not destructively rename; detect default branch and store it in state as `integration_branch`.

### 18.2 Build lanes

Default lanes:

```json
[
  {"id": "data_domain", "label": "Data / Domain"},
  {"id": "primary_ui", "label": "Primary UI"},
  {"id": "services_utilities", "label": "Services / Utilities"},
  {"id": "polish_resilience", "label": "Polish / Resilience"}
]
```

Each lane gets a worktree. Workers must not write directly into the integrated `app_build/` repo.

### 18.3 Worktree flow

For each build round:

1. Ensure `app_build/` is a git repo.
2. Ensure a clean integration branch.
3. Create or update lane branches from integration HEAD.
4. Create worktrees for each active lane.
5. Run each worker in its lane worktree.
6. After each worker completes, collect `git diff` as a patch.
7. Validate patch touched paths against claimed task files.
8. Apply patches to integration branch in deterministic lane order.
9. If patch fails, retry once after rebasing lane branch onto integration HEAD.
10. If still failing, run one bounded integrator auto-fix with both diffs in context.
11. If still unresolved, set `blocked_conflict` and pause even in Fully Autonomous mode.
12. Commit the integrated state after successful patch application.

### 18.4 Integrator-owned files

These are owned by the integrator, not lanes:

- Xcode project files
- generated package lock files
- dependency lock files
- project manifests
- shared app entry point if multiple lanes need it
- files marked `integrator_owned` in `interfaces.json`

When possible, regenerate these deterministically from directives rather than applying worker edits.

## 19. Task backlog

`task_assignments` emits `tasks.json`.

Schema:

```json
{
  "schema_version": 1,
  "tasks": [
    {
      "id": "T-001",
      "title": "Implement habit model",
      "owner_lane": "data_domain",
      "priority": 1,
      "files": [{"path": "App/Models/Habit.swift", "action": "create|edit|delete"}],
      "depends_on": [],
      "acceptance_criteria": ["Model compiles", "Unit tests cover recurrence"],
      "status": "pending|claimed|in_progress|done|failed|blocked",
      "claimed_by": null,
      "claimed_at": null,
      "completed_at": null,
      "attempt_count": 0,
      "produced_by": null,
      "is_fallback_substitution": false
    }
  ]
}
```

Rules:

- Validate dependency graph for cycles before build.
- Claiming a task must be atomic.
- Use OS advisory locking around `tasks.json` read-modify-write.
- A stale claimed/in-progress task reverts to pending and increments `attempt_count`.
- After max attempts, mark failed/blocked.

## 20. Interface contract

`interface_contract` emits `interfaces.json`.

Schema:

```json
{
  "schema_version": 1,
  "interfaces": [
    {
      "name": "HabitStore",
      "kind": "struct|protocol|function|enum|endpoint",
      "language": "swift|typescript|python|other",
      "signature_or_shape": "string",
      "owning_lane": "data_domain",
      "consumed_by_lanes": ["primary_ui"],
      "integrator_owned": false
    }
  ]
}
```

Inject `interfaces.json` into every build worker prompt. If a worker needs to change it, it emits a contract-change request to the integrator.

## 21. Live log

Write:

```text
<project>/.orchestrator_runtime/live_log.jsonl
```

Schema per line:

```json
{
  "schema_version": 1,
  "ts": "ISO-8601",
  "lane": "primary_ui",
  "agent": "claude",
  "task_id": "T-001",
  "kind": "task_done|blocked|contract_change_request|file_change|lane_authorship_change",
  "summary": "<=280 chars",
  "files_touched": [],
  "new_public_symbols": []
}
```

Only the Python coordinator writes this file, not agents directly.

## 22. Verification and tests

### 22.1 iOS verification

Detect project type:

- `.xcodeproj` or `.xcworkspace` → Xcode project
- `Package.swift` only → SwiftPM
- `package.json` → Node/web/backend
- `pyproject.toml`, `requirements.txt`, or `.py` entrypoint → Python

For Xcode:

1. Prefer `.xcworkspace` over `.xcodeproj` if present.
2. Detect schemes via `xcodebuild -list -json`.
3. Prefer app scheme matching project/app name.
4. Build for simulator first.
5. Use a generic modern simulator destination when possible:

```bash
xcodebuild -scheme <scheme> -destination 'platform=iOS Simulator,name=iPhone 16' build
```

If that simulator is unavailable, list available simulators and choose the newest available iPhone simulator.

### 22.2 Tests

For iOS app workflows that include `qa_test_plan`, run:

```bash
xcodebuild -scheme <scheme> -destination '<sim destination>' test
```

A test suite with zero tests is not green. Report it as `unverified` or `failed` depending on whether test execution ran and confirmed zero tests.

### 22.3 Flaky tests

On test failure, rerun failing targets up to 2 times if time remains. Classify tests as:

- `pass`
- `failing`
- `flaky`
- `unconfirmed`

Only consistently failing tests enter the repair prompt. Flaky/unconfirmed tests do not hard-block launch readiness but must appear as caveats.

## 23. Code review findings

Use `finding-json` with extensions:

```json
{
  "schema_version": 1,
  "source": "audit|code_review|secret_scan",
  "category": "security|bug|update|secret_hardcoded|accessibility|test|architecture",
  "severity": "Critical|High|Med|Low",
  "confidence": "high|medium|low",
  "title": "string",
  "file": "string|null",
  "line": 0,
  "why": "string",
  "fix": "string",
  "status": "open|fixed|wont_fix|deferred",
  "fix_attempt_count": 0,
  "reconciled_by": []
}
```

Persist code review findings to:

```text
<project>/review/code_review_findings.json
```

Launch readiness requires:

- no open Critical/High findings
- no unresolved `secret_hardcoded` finding
- verification status not upgraded beyond actual structured result

## 24. Documentation renderer

Each phase coordinator output must include prose plus a structured block with doc sections.

Example:

```markdown
```phase-output-json
{
  "schema_version": 1,
  "phase": "scope_and_prd",
  "doc_sections": {
    "prd.summary": "...",
    "prd.mvp_scope": "...",
    "prd.non_goals": "..."
  },
  "decisions": ["..."],
  "open_questions": [],
  "risks": []
}
```
```

Store rolling phase outputs in:

```text
<project>/docs/phase_outputs.json
```

Docs renderer outputs:

```text
<project>/docs/PROJECT_DOCUMENTATION.md
<project>/docs/PRD.md
<project>/docs/TECHNICAL_ARCHITECTURE.md
<project>/docs/QA_REPORT.md
<project>/docs/LAUNCH_READINESS.md
<project>/docs/KNOWN_LIMITATIONS.md
```

Do not parse raw prose to fill docs. Use structured `doc_sections` fields. If a required included phase lacks a section, render a warning instead of fabricating content.

## 25. Knowledge vault

Keep existing keyword-overlap knowledge injection for MVP. Add GUI upload/copy support later if easy, but the engine should support these folders now:

```text
.orchestrator/knowledge/ios/*.md
.orchestrator/knowledge/web/*.md
.orchestrator/knowledge/backend/*.md
.orchestrator/knowledge/general/*.md
.orchestrator/knowledge/user/*.md
```

Knowledge files may include:

```html
<!-- keywords: swiftui, xcode, firebase, testflight -->
```

Do not implement embeddings in MVP.

## 26. Native macOS GUI

Use SwiftUI.

### 26.1 Main layout

Three-pane layout:

1. Projects sidebar
2. Phase/run list
3. Detail pane

Detail pane tabs:

- Overview
- Prompt
- Phases
- Transcript
- Artifacts
- Verification
- Docs
- Files
- Settings

### 26.2 Required screens

#### Project sidebar

Shows:

- project name
- status pill
- latest verification status
- current phase
- running/paused/failed badge

#### New project screen

Inputs:

- prompt
- project name/slug
- workflow profile
- completeness
- autonomy
- stop target
- time limit optional
- agent selection
- local model inclusion toggle

#### Phase detail

Shows:

- phase title/purpose
- status
- assigned agents
- transcript
- structured final output
- approval controls when paused

Approval actions:

- Approve
- Edit & Approve
- Request Changes

#### Verification view

Shows:

- latest status: verified/failed/unverified
- tool used
- attempt count
- errors excerpt
- tests summary
- link to `verify_results.json`

#### Settings

Sections:

- General
- Agents
- Local Models
- Resources
- Notifications
- Advanced

### 26.3 App lifecycle

The engine must not die just because the main window closes.

Implement at least one of:

- detached Python engine process per run
- LaunchAgent wrapper
- robust resume after GUI relaunch

For MVP, acceptable fallback: if fully detached LaunchAgent is too risky, ensure the GUI can detect interrupted runs on relaunch and resume them safely from state. But do not pretend resume-across-quit works if the engine is actually killed.

`applicationShouldTerminateAfterLastWindowClosed` should be false while any run is active, paused, or resumable.

## 27. Engine CLI

Support these commands:

```bash
python orchestrator.py --root <path> --once
python orchestrator.py --root <path> --project <slug>
python orchestrator.py --root <path> --seed
python orchestrator.py --root <path> --doctor
python orchestrator.py --root <path> --resume <project_slug>
```

`--doctor` must check:

- Python version
- root path exists/writable
- Codex CLI availability
- Claude CLI availability
- Gemini/Antigravity availability
- Ollama availability
- Xcode command-line tools
- available simulator list if Xcode exists
- git availability

Return machine-readable JSON when called with `--json`.

## 28. Implementation order for Fable

**Ordering principle (read this first): build a runnable, visible app FIRST, then harden it.** The owner has one build pass and wants it spent on working, usable features — not on engine internals with no face, and not on document generation. Therefore the order below is *vertical-slice-first*: at every milestone boundary there must be an app the user can open, run, and see doing something real. Infrastructure is layered in *underneath* a working slice, not built ahead of it. Documentation rendering is explicitly the LAST, OPTIONAL milestone and is never a completion blocker.

Implement in this order. At each milestone the app must still launch and run. If the full build is too large for one pass, stop at the end of the latest fully-working milestone (§31).

### Milestone 0 — Orient, preflight, and preserve

- Inspect the current `.orchestrator` source before writing anything; do not fabricate files that already exist. The real agent runners already exist — find and reuse them (see Milestone 1); do not build a fake-agent-first system.
- **Run a preflight/doctor check before any architecture work**, and write the results to `PREFLIGHT_RESULTS.md` (which tools are available, missing, installed-but-not-logged-in, and which smoke prompts succeeded). Check:

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

  Where a CLI exists, attempt a harmless smoke prompt to confirm it is actually logged in and responds:

```bash
codex exec --sandbox read-only --skip-git-repo-check "Say READY"
claude -p "Say READY"
gemini -p "Say READY"
agy -p "Say READY"
```

- Confirm existing workflows load and the current sample run stays readable.
- Confirm the current SwiftUI GUI builds, or record the exact build blockers.
- Add no new architecture yet.

Acceptance: `PREFLIGHT_RESULTS.md` is written and honestly reflects tool/CLI availability; existing workflows still load; sample-run remains readable; current GUI build status is known and written down.

### Milestone 1 — Runnable vertical slice (THIS IS THE PRIORITY)

Goal: from the native app, a user types one prompt, picks agents, starts a run, and watches a real workflow execute to a visible result — before any deep infrastructure exists. Reuse everything that already works; do not gold-plate.

- Minimal but real SwiftUI three-pane shell (section 26.1): projects sidebar, phase/run list, and a detail pane with at least Overview / Prompt / Phases / Transcript / Verification tabs. Artifacts / Docs / Files / Settings tabs may be stubs this milestone.
- New-project screen (section 26.2) that writes `initial_prompt/initial_prompt.md` + `run_state.json` (section 6 schema) and launches the engine.
- The engine runs a workflow end-to-end using the EXISTING generic prompt builders (`prompt_discuss` / `prompt_coordinate`) driven by each phase's `purpose` string. Do NOT block this milestone on authoring 25 bespoke phase prompts — those are refined later and are not required for a working run.
- **Use the existing real cloud-agent runners first — do not replace them with a fake/demo implementation.** In the current snapshot these are in `./orchestrator.py` (or the live `.orchestrator/orchestrator.py`): `run_codex(...)`, `run_claude(...)`, `run_gemini(...)` (which already fans out gemini-CLI → `agy` → keyless-gemini), and `RUNNERS = {"codex": run_codex, "claude": run_claude, "gemini": run_gemini}`. Inspect and wire these real paths into the GUI/engine vertical slice.
- **Scope Milestone 1 to exactly these three existing agents.** They are already keys in every hardcoded structure (`AGENT_ORDER`, `DISPLAY`, `SIGNATURE`, `RUNNERS`, `COORDINATOR_PREFERENCE`), so using them here is precisely what avoids the known silent-drop bug — that bug only affects *dynamically-named* identities (e.g. `local:<model>`) absent from those tuples. Local models and the agent-identity normalization that makes dynamic identities safe both arrive in Milestone 2; do not let Milestone 1 depend on them.
- **Demo/fake agent is a dev-only fallback, never the product path.** Only if the build environment cannot run the real logged-in CLIs (per `PREFLIGHT_RESULTS.md`) may Fable add a deterministic `demo`/`fake_agent` adapter — solely to validate that the app can create a project, launch the engine, stream phase output, update state, render transcripts, and reach a final-review-like result. Rules: disabled by default in normal runs; not shown as a real provider in the product UI; must not replace or delay the Codex/Claude/Gemini path; documented in `KNOWN_LIMITATIONS.md` if used. **If a demo agent is built, it must be registered in ALL five hardcoded structures above (`AGENT_ORDER`, `DISPLAY`, `SIGNATURE`, `RUNNERS`, `COORDINATOR_PREFERENCE`)** — otherwise the demo identity itself trips the same KeyError/drop bug, since it is a new key not yet in those tuples. If the real CLIs are available, do not build or use the demo adapter.
- Engine writes transcripts + `run_state.json` per turn; the GUI polls and renders phase status, transcripts, and the current speaking agent live.
- Use the EXISTING `verify.py` to produce a verification result for a build workflow and show it in the Verification tab. (The hardened, persisted `verify_results.json` gate comes in Milestone 3 — a plain in-memory result is fine here.)
- Approval controls (Approve / Edit & Approve / Request Changes) wired for **Manual mode** at minimum (pause after each phase). The full per-phase `checkpoint` model arrives with Milestone 2.

Acceptance: from the app, one prompt creates a project, a workflow runs to final review, transcripts + phase status render live, approval works in Manual mode, and a verification result (even `unverified`) appears. **This is the first thing that must work; nothing below ships before this does.**

### Milestone 2 — Real agent roster + workflow/profile model

Implement sections 10, then 8, 9, and 7 (identity normalization first so `local:<model>` and unknown IDs never crash; then the extended `Phase` fields, the full phase menu, completeness profiles, autonomy modes, and stop targets).

Acceptance: dynamic `local:<model>` identities are never silently dropped; no hardcoded display/signature lookup crashes on an unknown agent ID; completeness profiles select the correct phase set; new `Phase` fields load from JSON with defaults; structurally-required phases can't be skipped by an including profile; stop targets work; agent enable/disable per run works from the GUI.

### Milestone 3 — Trust: structured verification gate + redaction

Implement sections 15, 16, and 17.

Acceptance: `verify_results.json` is created after every attempt (including repairs); verified/failed/unverified mapping is tested; final review receives structured verification context; a missing result yields `VERIFICATION: UNVERIFIED`; fake secrets are redacted before any persisted write.

### Milestone 4 — Structured artifacts foundation

Implement section 14 and retrofit audit/finding parsing to use it.

Acceptance: invalid structured JSON is logged visibly; required-field failures trigger one repair attempt or a warning; no silent `continue` on invalid structured output.

### Milestone 5 — Persistent git repo + worktree build lanes

Implement sections 18, 19, 20, and 21.

Acceptance: `app_build/` is initialized once as a persistent git repo; each lane writes in an isolated worktree; patches are validated and applied deterministically; file conflicts produce `blocked_conflict`, never last-write-wins; `tasks.json` and `interfaces.json` are produced and consumed.

### Milestone 6 — Resilience + local models

Implement sections 11, 12, and 13.

Acceptance: agent failures update circuit-breaker state; `blocked_no_agents` is persisted and surfaced; Ollama detection works; the recommended model list displays and a pull works when Ollama is installed; a missing Homebrew path does not attempt a non-interactive bootstrap; the local/cloud privacy boundary is shown.

### Milestone 7 — App lifecycle + settings depth

Implement section 26.3 (engine survives window close / safe resume) and the full Settings sections from 26.2.

Acceptance: closing the main window does not destroy the ability to resume an active run; Settings exposes General / Agents / Local Models / Resources / Notifications / Advanced.

### Milestone 8 — OPTIONAL, LAST: documentation renderer

Implement section 24 **only if every prior milestone is fully working and build budget remains.** This is deliberately deprioritized: the owner wants this build pass spent on working features, not document generation. If skipped, record it in `NEXT_MILESTONES.md`. **It is not a completion blocker.**

Acceptance (only if attempted): phase outputs persist to `docs/phase_outputs.json`; docs render from structured `doc_sections`, not raw prose; a required-but-empty included section renders a warning rather than fabricated content.

### Milestone 9 — Tests + honest demo

Implement the section 29 harnesses and run a disposable demo project end-to-end.

Acceptance: one prompt creates a project; at least one workflow reaches final review; `verify_results.json` exists; the GUI shows status correctly; the final report is honest (verified / failed / unverified). **Documentation generation is NOT required for this to pass.**

## 29. Required test harnesses

Add stdlib-only tests/scripts if no test framework exists. They may live under:

```text
.orchestrator/tests/
```

Minimum tests:

1. Verification status mapping.
2. Verification append order.
3. Latest verification result by prompt hash.
4. Redaction of fake secrets.
5. Structured block parser valid/invalid cases.
6. Workflow JSON backward compatibility.
7. Phase field defaults.
8. Task dependency cycle detection.
9. Patch conflict simulation.
10. Agent identity registration with `local:test-model`.

If using Python `unittest`, run with:

```bash
python -m unittest discover .orchestrator/tests
```

## 30. Final demo acceptance checklist

The build is done only when all of this is true:

- Native Mac app launches.
- Settings opens.
- User can create a new project from one prompt.
- User can enable/disable Codex, Claude, Gemini, and local models.
- Ollama detection works.
- A demo project appears on first launch.
- Engine can run at least one project workflow.
- Transcripts are written.
- Logs are written with redaction.
- `verify_results.json` is written.
- Final review includes `VERIFICATION: VERIFIED/FAILED/UNVERIFIED`.
- `tasks.json` and `interfaces.json` are generated for build workflows (once Milestone 5 lands).
- `app_build/` is a persistent git repo (once Milestone 5 lands).
- Build workers do not directly write into integration repo when worktree mode is active.
- GUI shows project status, current phase, transcript, artifacts, and verification result.
- Closing the main window does not silently destroy the user's ability to resume a run.
- All test harnesses pass or failures are documented in `KNOWN_LIMITATIONS.md`.
- (Optional, Milestone 8) Documentation is generated into `docs/` — not required for a successful build; if not reached, it is recorded in `NEXT_MILESTONES.md`.

Because the build is milestone-ordered and vertical-slice-first, a partial build is still a success as long as the latest completed milestone leaves a launchable, runnable app. The minimum successful outcome is Milestone 1: a native app that runs one real workflow from a prompt and shows an honest verification result. Everything after that is depth added under a working slice.

## 31. Fable build instruction

Fable: implement the V2 MVP described in this document. Use the implementation order in section 28 exactly — it is **vertical-slice-first**: get Milestone 1 (a native app that runs one real workflow from a prompt and shows an honest verification result) fully working before anything else, then layer infrastructure underneath it. Spend the build pass on working, usable features. **Do not spend it on the documentation renderer (Milestone 8) — that is optional and last, and is never a completion blocker.** If the complete implementation is too large for one pass, stop at the end of the latest fully-working milestone (the app must still launch and run) and write exactly what remains in `NEXT_MILESTONES.md`. Prefer a smaller number of milestones that genuinely work over a broad, half-wired rewrite. Do not partially implement future-scope features. Do not invent cloud integrations. Do not auto-create GitHub repos. Do not upload to TestFlight. Do not introduce non-stdlib Python dependencies unless absolutely necessary; if you do, explain why in `DEPENDENCIES.md`.

When done, commit or output:

```text
CHANGELOG_V2.md
DEPENDENCIES.md
NEXT_MILESTONES.md
KNOWN_LIMITATIONS.md
TEST_RESULTS.md
```

`TEST_RESULTS.md` must include every command run, every pass/fail result, and any toolchain that was unavailable.

## 32. If ambiguity remains

Do not ask a clarification question unless blocked. Choose the safer local-first, single-user, no-cloud, no-auto-publish option. Preserve current behavior over a speculative rewrite. Prefer honest `UNVERIFIED` over optimistic claims. Prefer a working narrow milestone over a broad half-working rewrite.

This spec is intentionally scoped so Fable can build a real V2 MVP from one document.