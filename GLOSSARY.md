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

## Layout (M1 interim) — binding decision, task 0.2

The target nested layout `workspace/<project>/<section>/<chat-slug>/`
(plus `workspace/<project>/artifacts/` and `…/conductor/`) is IMPLEMENTED
in task 3.0 — not before. Until 3.0 lands, discovery is flat by design on
both sides: the engine's `find_apps` lists direct children of the
workspace root and requires `<dir>/initial_prompt/initial_prompt.md`,
ignoring nested dirs (orchestrator.py:6400–6421); GUI discovery is flat
too (`BackgroundProjectLoader.discoverApps`, used at
OrchestratorStore.swift:866) and locks are keyed by top-level dir name.

**M1 flat chat naming.** A chat is a flat project dir named
`<project>--<section>--<chat-slug>`. Each component is INDEPENDENTLY
slugified (OrchestratorStore.slugify semantics, OrchestratorStore.swift:
2980–2993: lowercase ASCII alphanumerics, single dashes, ≤40 chars,
trailing dashes stripped — consecutive dashes can never survive), then the
three components are joined with a literal `--`. Never slugify the joined
string: slugify collapses consecutive dashes, which would destroy the
separator. Parsing splits on `--` and is unambiguous precisely because
components cannot contain `--`.

**Lock-collision rule (derived, not asserted).** Locks are
`<root>/.orch-locks/<dir-name>.lock` (OrchestratorStore.swift:3312; the
engine-local `locks/<name>.lock` is the legacy location, :3098). Lock name
equals dir name, and dir names are filesystem-unique at the flat root —
so lock uniqueness follows from dir uniqueness. Chat minting MUST reject
an existing name by suffixing `-2`, `-3`, … (the ChatHome.createRun
precedent, ChatHome.swift:236–240).

**Back-compat rule (binding on task 3.0).** Any directory with
`agent_state.json` at its root is a legacy single-chat project — forever.
3.0's migration must never rewrite one into the nest without an explicit
dry-run-diffed migration step.

**Minted-chat checklist (what task 1.5 creates, no engine-source reading
required).** A freshly minted chat dir must contain:
1. `initial_prompt/initial_prompt.md` — the seed prompt (discovery
   contract, find_apps).
2. `workflow.txt` — naming the chat workflow (e.g. `chat_ideas`).
Name it per the flat convention above with mint-time `-2`/`-3` suffixing
on collision.
