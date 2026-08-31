# PORTFOLIO_PLAN.md — bot portfolio: governance, labels, and operating rules

Owner directive 2026-08-31. This directory is the authoritative planning home for the three bot ventures (CommerceLint, Wait How Big, One Person Ops) plus shared agent infrastructure. It supersedes the 15 PENDING SYNC blocks staged earlier today in `pri8771/lipi-standard-store ops/JIRA_SYNC_QUEUE.md` — same tasks, now structured as epics/tasks/subtasks in `JIRA_BACKLOG.md` (this folder). The Lipi *store* keeps its own board (LIPI) and repo docs unchanged.

**Sensitive-data rule (binding):** owner handoffs for these ventures contain private identifiers (CRM sheet ID, Buffer channel IDs, analytics IDs, private sender addresses). They stay out of this repo and out of Jira — referenced only as "owner handoff, out of band." Each venture's own constraints ($0 budget, no unauthorized sends/publishes, action-time authorization, no credential handling) bind every agent that touches it.

## What Kai monitors (current census)

- **Jira boards swept:** 4 — LIPI, PT, DOCS8, ORCH8 (hourly pickup sweep + 15-min owner-action poll + daily digest).
- **Work streams:** 5 — Lipi Standard store (operating), CommerceLint, Wait How Big, One Person Ops (all three: adopted, backlog defined, runtime monitors not yet built), and its own OpenClaw/agent infrastructure.
- One Person Ops is **tracking-only** from this machine (its files live outside git on the owner's Mac).

## Per-bot labels (filterable)

Every ticket belonging to a venture carries exactly one bot label:

| Label | Venture |
|---|---|
| `bot:commercelint` | CommerceLint |
| `bot:waithowbig` | Wait, How Big? |
| `bot:onepersonops` | One Person Ops |
| `bot:kai` | OpenClaw/agent infrastructure (Kai itself) |
| `lipi` (existing) | Lipi Standard store (kept for continuity) |

Filter examples: `labels = bot:commercelint AND statusCategory != Done` · all bot work: `labels in (bot:commercelint, bot:waithowbig, bot:onepersonops, bot:kai)`.

Lane labels stay orthogonal: `operator:openai` / `operator:gemini` route execution per DELEGATION.md; `agent-task` / `owner-action` / `repo-tracked` keep their existing meanings.

## Ticket hygiene — ENFORCED at pickup

A ticket is **workable only if fully specified**: Role, Model, Effort, Interaction, Risk Level set; a priority label (P0/P1/P2); exactly one bot/venture label; an `# Acceptance criteria` section in the description.

- Kai's sweep MUST NOT pick up a malformed ticket. Instead: comment listing the missing fields, add label `needs-triage`, move on.
- **Dependencies are Jira issue links** (`Blocks` / `is blocked by`), never prose-only — including cross-project links (ORCH8 ↔ PT ↔ LIPI). A ticket with an open `is blocked by` link is not workable regardless of status.
- Weekly hygiene metric in the Monday digest: % of open tickets fully specified; % of stated dependencies that exist as links. No new process beyond these checks.

## Framework lanes (recap — detail in lipi-standard-store ops/PORTFOLIO_OPS_PLAN.md)

LangGraph → CommerceLint monitoring · CrewAI → Wait How Big content (post-launch) · Hermes-via-Ollama → shared local model layer (cheap nodes for all lanes + Jira backfill). One primary orchestrator per venture; Hermes cross-cuts; mixing orchestrators only when a venture demonstrably needs both shapes.

## Delegation lanes status (2026-08-31)

- **anthropic (Kai):** live.
- **openai (codex CLI):** installed + ChatGPT-authenticated, verified `READY`. Kai must invoke by absolute path (its exec shell PATH lacks the npm global dir) — see workspace DELEGATION.md.
- **gemini (gemini CLI):** BLOCKED — Google retired the free individual tier (`UNSUPPORTED_CLIENT`). Unblock = owner adds a free AI Studio API key to `~/.gemini/.env`.
- **cursor:** human-driven lane (IDE + Jira backfill). `cursor-agent` headless CLI not installed; optional future worker lane.
- **Option A (native OpenClaw backends for codex/gemini):** documented in workspace DELEGATION.md, deliberately dormant until owner go.

## Board mapping

- **ORCH8** — all bot epics, agent-executable tasks, and subtasks.
- **PT** — owner gates (linked cross-project as blockers).
- **LIPI** — Lipi store items only (existing LIPI-1..50 untouched; two new tasks in the backlog).
- **DOCS8** — reserved for the docs-generation lane; nothing new this pass.
