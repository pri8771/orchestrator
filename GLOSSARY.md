# Glossary — V3 "Sections" vocabulary

The frozen nouns for Orchestrator V3 (see `orchestrator-v3-sections-plan.md`
§1). PRs are checked against this file: code, UI copy, comments, and docs
use these words with exactly these meanings.

**Project** — one product/effort (e.g. "CupDeck"). The top-level container
a user thinks in. On disk today: one flat directory under the workspace
root (see Layout below).

**Section** — one specialized studio (Ideas, Research, Planning, Design,
Build, QA & Red Team, Documentation, Go-to-Market, Legal & Compliance,
Execution & Operations, Library & Knowledge). Sections are data — a
manifest directory, not code.

**Chat** (user-facing) / **session** (on disk) — one conversation inside a
section. A "session" is always a *directory* (transcripts, state, inbox);
it is never a backend continuity handle. UI copy says "chat"; engine paths
and docs may say "session dir".

**Run** — one engine process executing a session
(`python3 orchestrator.py --app <session>`).

**Agent thread** — a backend continuity key used by `call_agent_sessioned`
(orchestrator.py:1531) to resume a CLI/API conversation with delta prompts.
Never called a "session" in UI or code comments. Safety rule: **forking a
chat must NOT clone agent-thread ids** — `codex exec resume` has no
`--sandbox` flag and always runs workspace-write regardless of what the
original session used (orchestrator.py:1543–1547), so a cloned thread id
can silently upgrade a read-only discussion's sandbox. Codex thread reuse
is therefore scoped to write-enabled phases only.

**Artifact** — a typed, versioned output published to the bus
(`workspace/<project>/artifacts/<id>/` = `body.md` + `meta.json` with
lineage/supersedes/status). Only artifacts route between sections.

**Conductor** — the global autonomous router, one per workspace: moves
finished artifacts between sections and mints/continues sessions until a
goal manifest is satisfied. Per-section auto mode is a conversation; the
Conductor is an economy.

**Pipeline vs workflow** — two layers, never one format:
- a **workflow** is *within-section* phase JSON executed inside one session
  (`workflows.py` `Workflow`/`Phase`, workflows/*.json);
- a **pipeline** is a *cross-section* Conductor routing preset (routing
  rules + goal manifest) compiled by the pipeline builder.
A single format must never span both layers.

**Situation** — a named, applyable bundle of {document flow (required doc
slots) + pipeline preset + section/phase/cast overrides}. Applying a
Situation to a project re-scopes what the Conductor considers "done"
(sections-plan §14).
