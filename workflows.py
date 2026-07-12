#!/usr/bin/env python3
"""
Pluggable workflows for the orchestrator.

A *workflow* is an ordered list of phases. The engine used to hard-code a single
10-phase app-building pipeline; now that pipeline is just the built-in
``app_build`` workflow, and other workflows (answer a question, do research,
productionize a prototype) reuse the exact same debate -> consensus -> vote ->
(optional) build machinery.

Design constraints (unchanged from the rest of the project):
  * Standard library only. Workflow files are JSON (the built-in mini-YAML
    reader can't do lists, and phases are a list) so ``json`` handles them.
  * Backward compatible. A ``Phase`` still unpacks as the legacy 4-tuple
    ``(key, folder, file, purpose)`` and indexes as ``phase[0..3]``, so existing
    engine code keeps working untouched — while ``phase["rounds"]`` /
    ``phase.get("roles")`` expose the new fields.
  * Zero-config. Built-in workflows are defined in Python and *seeded* to
    ``workflows/*.json`` on first run so the GUI has something to edit; if a
    JSON file is deleted we fall back to the built-in. On-disk JSON always wins
    when present (that's how GUI edits to rounds/roles persist).
"""

import copy
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
WORKFLOWS_DIR = os.path.join(HERE, "workflows")

DEFAULT_WORKFLOW = "app_build"


def phase_key(phase):
    """The canonical way to read a phase's key from any of its shapes: a Phase
    object (``.key``), a dict (``["key"]``), or a legacy ``(key, ...)`` tuple.
    Centralizes the three ad-hoc accessors that had drifted across modules."""
    if hasattr(phase, "key"):
        return phase.key
    if hasattr(phase, "get"):
        return phase.get("key")
    try:
        return phase[0]
    except (TypeError, IndexError, KeyError):
        return None


class Phase:
    """One phase of a workflow.

    Behaves like the legacy ``(key, folder, file, purpose)`` tuple for the old
    engine code (iteration + integer indexing) and like a dict for new fields
    (``rounds``, ``roles``, ``writes``, ``verify``, ``title``).
    """

    __slots__ = ("key", "folder", "file", "title", "purpose",
                 "rounds", "roles", "writes", "reads_target", "verify",
                 # V2 spec §8 additions (all default so legacy JSON still loads):
                 "checkpoint", "structurally_required", "requires_verification",
                 "doc_sections", "test_deliverable")

    def __init__(self, key, folder, file, purpose, title=None, rounds=6,
                 roles=None, writes=False, reads_target=False, verify=None,
                 checkpoint=False, structurally_required=False,
                 requires_verification=False, doc_sections=None,
                 test_deliverable=None):
        self.key = key
        self.folder = folder
        self.file = file
        self.purpose = purpose
        self.title = title or key.replace("_", " ").title()
        self.rounds = int(rounds)
        self.roles = list(roles) if roles else []
        self.writes = bool(writes)
        # When true, this phase reads a pre-existing TARGET codebase (audit mode) —
        # read-only, never written to. Mutually exclusive with writes in practice.
        self.reads_target = bool(reads_target)
        self.verify = verify or None
        # V2 fields (§8): checkpoint = pause in Semi-Autonomous; structurally_required
        # = turns floor of 1 when included; requires_verification = gate final output
        # on a structured verification label; doc_sections = named blueprint subsection
        # keys this phase emits; test_deliverable = per-stack test descriptor.
        self.checkpoint = bool(checkpoint)
        self.structurally_required = bool(structurally_required)
        self.requires_verification = bool(requires_verification)
        self.doc_sections = list(doc_sections) if doc_sections else []
        self.test_deliverable = test_deliverable or None

    # --- legacy 4-tuple compatibility: key, folder, file, purpose ---
    def _tuple(self):
        return (self.key, self.folder, self.file, self.purpose)

    def __iter__(self):
        return iter(self._tuple())

    def __len__(self):
        return 4

    def __getitem__(self, item):
        if isinstance(item, int):
            return self._tuple()[item]
        # dict-style access for new fields
        return getattr(self, item)

    def get(self, item, default=None):
        return getattr(self, item, default)

    def to_json(self):
        return {
            "key": self.key, "folder": self.folder, "file": self.file,
            "title": self.title, "purpose": self.purpose, "rounds": self.rounds,
            "roles": self.roles, "writes": self.writes,
            "reads_target": self.reads_target, "verify": self.verify,
            "checkpoint": self.checkpoint,
            "structurally_required": self.structurally_required,
            "requires_verification": self.requires_verification,
            "doc_sections": self.doc_sections,
            "test_deliverable": self.test_deliverable,
        }

    @staticmethod
    def from_json(d):
        return Phase(
            key=d["key"], folder=d.get("folder", d["key"]),
            file=d.get("file", d["key"] + ".md"), purpose=d.get("purpose", ""),
            title=d.get("title"), rounds=d.get("rounds", 6),
            roles=d.get("roles"), writes=d.get("writes", False),
            reads_target=d.get("reads_target", False),
            verify=d.get("verify"),
            checkpoint=d.get("checkpoint", False),
            structurally_required=d.get("structurally_required", False),
            requires_verification=d.get("requires_verification", False),
            doc_sections=d.get("doc_sections"),
            test_deliverable=d.get("test_deliverable"),
        )

    def __repr__(self):
        return "Phase(%s)" % self.key


class Workflow:
    def __init__(self, name, title, description, phases, target="app",
                 build_phase=None, budget=None, overrides=None):
        self.name = name
        self.title = title
        self.description = description
        self.phases = phases
        self.target = target
        # The phase (if any) where agents actually write code/files.
        self.build_phase = build_phase or next(
            (p.key for p in phases if p.writes), None)
        # Optional time-budget parameters (dict). When present, the engine enforces
        # a hard wall-clock ceiling for the whole run (see Sprint mode). None means
        # unbounded — the normal behavior for every other workflow. Deep-copied (like
        # modelrouting's per-phase overrides) so a caller mutating the Workflow's copy
        # can never bleed back into the caller's own budget dict.
        self.budget = copy.deepcopy(dict(budget)) if budget else None
        # Optional per-run preset (dict): claude_model / codex_model / effort
        # ("fast" | "standard" | "max") / rounds_scale (0.5-2.0). Applied by the
        # engine where the workflow is loaded for a run; None (the default, and
        # anything that isn't a dict) leaves every existing workflow untouched.
        self.overrides = copy.deepcopy(dict(overrides)) \
            if isinstance(overrides, dict) and overrides else None

    def phase(self, key):
        return next((p for p in self.phases if p.key == key), None)

    def to_json(self):
        # Every field is emitted (budget/overrides as null when unset) so the
        # on-disk shape is uniform and consumers can rely on each key's presence.
        return {
            "name": self.name, "title": self.title,
            "description": self.description, "target": self.target,
            "build_phase": self.build_phase,
            "budget": self.budget,
            "overrides": self.overrides,
            "phases": [p.to_json() for p in self.phases],
        }

    @staticmethod
    def from_json(d):
        phases = [Phase.from_json(p) for p in d.get("phases", [])]
        return Workflow(
            name=d["name"], title=d.get("title", d["name"]),
            description=d.get("description", ""), phases=phases,
            target=d.get("target", "app"), build_phase=d.get("build_phase"),
            budget=d.get("budget"), overrides=d.get("overrides"),
        )


# ---------------------------------------------------------------------------
# Built-in workflows (canonical source; seeded to JSON on first run)
# ---------------------------------------------------------------------------
_IOS_SIGNING_VERIFY = {"type": "xcodebuild", "repair_iterations": 3}

_APP_BUILD = Workflow(
    name="app_build", title="Build an App", target="app",
    description="The full production pipeline: lock the prompt contract, select "
                "portfolio children when needed, brief each app, design it, review "
                "iOS architecture, gate implementation readiness, build, verify and "
                "repair, QA it, prepare App Store material, and audit the portfolio.",
    build_phase="build_coordination",
    phases=[
        Phase("prompt_contract", "prompt_contract", "prompt_contract.md",
              "Preserve the user's original prompt before any discussion, convert it "
              "into a clear execution contract, list hard requirements, non-goals, "
              "success metrics, production-readiness expectations, and decision rules "
              "for every later phase.", rounds=9,
              roles=["product", "qa", "delivery"], checkpoint=True),
        Phase("product_research", "product_research", "product_research.md",
              "Before product debate begins, infer the likely market, audience, "
              "competitors/patterns, platform expectations, risks, and native iOS "
              "opportunities from the prompt. Label assumptions clearly and turn "
              "the research into implications for the first build.", rounds=9,
              roles=["product", "design", "qa"]),
        Phase("portfolio_selection", "portfolio_selection", "portfolio_selection.md",
              "If the prompt requests multiple apps, force a real independent selection "
              "process: one app per requested category or requested count, with no "
              "collapsed wrapper project. Produce the required portfolio-json manifest "
              "for sibling child projects. If the prompt is a single app, explicitly "
              "record that no portfolio split is needed.", rounds=9,
              roles=["product", "design", "qa"], structurally_required=True),
        Phase("initial_discussion", "initial_discussion", "initial_discussion.md",
              "Establish a shared, precise understanding of what this app is, who it is "
              "for, the core problem it solves, hard scope boundaries, and measurable "
              "success criteria.", rounds=9, roles=["product", "design", "qa"]),
        Phase("per_app_product_brief", "per_app_product_brief",
              "per_app_product_brief.md",
              "Create a production-grade product brief for this specific app: target "
              "user, paid value, core loop, subscription value, competitive wedge, "
              "viral or niche growth mechanism, local-first behavior, cloud-ready "
              "extension path, and the real reason the app deserves to exist.",
              rounds=9, roles=["product", "design", "qa"], checkpoint=True),
        Phase("next_steps_small", "next_steps_small", "next_steps_small.md",
              "Define the smallest valuable slice / immediate next steps that de-risk and "
              "validate the concept with minimal effort.", rounds=9,
              roles=["product", "delivery", "qa"]),
        Phase("detailed_discussion", "detailed_discussion", "detailed_discussion.md",
              "Deeply analyze requirements, edge cases, constraints, assumptions, risks, "
              "and open questions, resolving ambiguity left from earlier phases.",
              rounds=9, roles=["product", "qa", "backend"]),
        Phase("app_features", "app_features", "app_features.md",
              "Enumerate and prioritize the full feature set with an explicit scoring / "
              "MoSCoW model and clear in-scope vs out-of-scope lines.", rounds=9,
              roles=["product", "design", "qa"]),
        Phase("design_discussion", "design_discussion", "design_discussion.md",
              "Decide UX flows, information architecture, key screens/states, and the "
              "design principles that govern them.", rounds=9,
              roles=["design", "frontend", "product"]),
        Phase("design_handoff", "design_handoff", "design_handoff.md",
              "Produce a complete design handoff: screen-by-screen specs, interaction "
              "states, motion notes, design tokens, accessibility expectations, and a "
              "professional Claude Design prompt or import plan if the run is configured "
              "to pause for external design.", rounds=9,
              roles=["design", "frontend", "product"], checkpoint=True),
        Phase("ios_architecture_review", "ios_architecture_review",
              "ios_architecture_review.md",
              "Review the app as an iOS product before final tech specs: SwiftUI "
              "architecture, Apple framework choices, local persistence, privacy, "
              "permissions, offline behavior, StoreKit/subscription architecture, "
              "permissive dependency policy, and testability.", rounds=9,
              roles=["frontend", "backend", "qa"]),
        Phase("tech_specs", "tech_specs", "tech_specs.md",
              "Decide architecture, technology stack, data model, interfaces/APIs, and "
              "non-functional requirements with justified trade-offs.", rounds=9,
              roles=["frontend", "backend", "qa"]),
        Phase("project_plan", "project_plan", "project_plan.md",
              "Produce milestones, sequencing, estimates, dependencies, and a risk-managed "
              "delivery plan.", rounds=9, roles=["delivery", "backend", "qa"]),
        Phase("task_assignments", "task_assignments", "task_assignments.md",
              "Produce concrete workstreams, file ownership, forbidden edit zones, branch "
              "names, merge strategy, testing responsibilities, communication protocol, "
              "and a conflict-prevention plan.", rounds=9,
              roles=["delivery", "frontend", "backend"]),
        Phase("implementation_readiness_gate", "implementation_readiness_gate",
              "implementation_readiness_gate.md",
              "Before any code is written, audit whether the product brief, design "
              "handoff, iOS architecture, tech specs, project plan, and task contracts "
              "are coherent enough for a one-shot production build. Resolve blockers "
              "or explicitly downgrade scope before build starts.", rounds=9,
              roles=["delivery", "qa", "frontend"], checkpoint=True),
        Phase("build_coordination", "build_coordination", "agent_messages.md",
              "Iterative build coordination via agent messages. Planning and task "
              "assignment only unless build_code_changes_enabled is true.", rounds=9,
              roles=["frontend", "backend", "design", "qa"], writes=True),
        Phase("build_verification", "build_verification", "build_verification.md",
              "Compile and verify the generated app, then run bounded repair iterations "
              "against real compiler/test failures until it passes or records honest "
              "remaining blockers.", rounds=9, roles=["qa", "frontend", "backend"],
              verify=_IOS_SIGNING_VERIFY, requires_verification=True,
              test_deliverable="xcodebuild/test verification plus repair log"),
        Phase("human_qa_checklist", "human_qa_checklist", "human_qa_checklist.md",
              "Create a manual simulator/device QA script for every important workflow, "
              "including onboarding, empty states, error states, persistence, "
              "accessibility, offline behavior, paywall/subscription behavior when "
              "relevant, and regression checks.", rounds=9,
              roles=["qa", "design", "product"], requires_verification=True),
        Phase("app_store_readiness", "app_store_readiness", "app_store_readiness.md",
              "Prepare launch readiness: App Store positioning, screenshots/storyboard "
              "needs, privacy nutrition labels, permission copy, subscription/paywall "
              "review risks, support/contact requirements, and release blockers.",
              rounds=9, roles=["product", "qa", "delivery"],
              requires_verification=True, checkpoint=True),
        Phase("final_review", "final_review", "final_review.md",
              "Final review of completeness, consistency, risks, quality gates, and a "
              "clear go / no-go decision.", rounds=9, roles=["qa", "product", "delivery"],
              requires_verification=True),
        Phase("portfolio_audit", "portfolio_audit", "portfolio_audit.md",
              "For multi-app prompts, audit the portfolio shape: every requested app is "
              "a sibling folder, every selected app has the right workflow, child prompts "
              "preserve the parent requirements, and no category was silently collapsed. "
              "For single-app prompts, record that portfolio audit is not applicable.",
              rounds=9, roles=["qa", "delivery", "product"],
              requires_verification=True),
    ],
)

_APP_SPEC = Workflow(
    name="app_spec", title="Spec an App", target="app_spec",
    description="A per-app specification pipeline for portfolio child projects. "
                "Runs the product, design, technical, planning, and review phases "
                "for one selected app concept without generating code.",
    build_phase=None,
    phases=[
        Phase("prompt_contract", "prompt_contract", "prompt_contract.md",
              "Preserve the user's original prompt and convert it into a clear spec "
              "contract for this one selected app: requirements, non-goals, success "
              "metrics, production expectations, and decision rules.", rounds=9,
              roles=["product", "qa", "delivery"], checkpoint=True),
        Phase("product_research", "product_research", "product_research.md",
              "For this one app concept, infer the likely market, audience, "
              "competitors/patterns, platform expectations, risks, and native iOS "
              "opportunities. Label assumptions clearly and turn the research into "
              "implications for this app's v1.", rounds=9,
              roles=["product", "design", "qa"]),
        Phase("portfolio_selection", "portfolio_selection", "portfolio_selection.md",
              "Confirm this spec is a single portfolio child and not a parent wrapper. "
              "Record the parent category/selection rationale and why this child should "
              "stay as its own app.", rounds=9,
              roles=["product", "design", "qa"], structurally_required=True),
        Phase("initial_discussion", "initial_discussion", "initial_discussion.md",
              "For this one app concept, establish a precise understanding of what "
              "it is, who it is for, the core problem it solves, hard scope "
              "boundaries, and measurable success criteria.", rounds=9,
              roles=["product", "design", "qa"]),
        Phase("per_app_product_brief", "per_app_product_brief",
              "per_app_product_brief.md",
              "Create a production-grade product brief for this specific app: paid "
              "value, core loop, competitive wedge, local-first behavior, cloud-ready "
              "path, growth angle, and why the app deserves to exist.", rounds=9,
              roles=["product", "design", "qa"], checkpoint=True),
        Phase("next_steps_small", "next_steps_small", "next_steps_small.md",
              "Define this app's smallest valuable local-first slice / immediate "
              "next steps that de-risk and validate the concept with minimal effort.",
              rounds=9, roles=["product", "delivery", "qa"]),
        Phase("detailed_discussion", "detailed_discussion", "detailed_discussion.md",
              "Deeply analyze this app's requirements, edge cases, constraints, "
              "assumptions, risks, and open questions, resolving ambiguity left from "
              "earlier phases.", rounds=9, roles=["product", "qa", "backend"]),
        Phase("app_features", "app_features", "app_features.md",
              "Enumerate and prioritize this app's feature set with explicit "
              "in-scope vs out-of-scope lines.", rounds=9,
              roles=["product", "design", "qa"]),
        Phase("design_discussion", "design_discussion", "design_discussion.md",
              "Decide this app's UX flows, information architecture, key screens/"
              "states, design direction, and Claude Design handoff requirements.",
              rounds=9, roles=["design", "frontend", "product"]),
        Phase("design_handoff", "design_handoff", "design_handoff.md",
              "Produce a complete design handoff and Claude Design prompt/import plan: "
              "screens, states, tokens, motion notes, accessibility, and responsive "
              "behavior.", rounds=9, roles=["design", "frontend", "product"],
              checkpoint=True),
        Phase("ios_architecture_review", "ios_architecture_review",
              "ios_architecture_review.md",
              "Review the spec as an iOS product: SwiftUI architecture, local "
              "persistence, Apple frameworks, privacy, permissions, permissive "
              "dependencies, StoreKit when relevant, and testability.", rounds=9,
              roles=["frontend", "backend", "qa"]),
        Phase("tech_specs", "tech_specs", "tech_specs.md",
              "Decide this app's architecture, technology stack, data model, "
              "interfaces/APIs, and non-functional requirements with justified "
              "trade-offs. This is a spec-only project, so do not emit build tasks.",
              rounds=9, roles=["frontend", "backend", "qa"]),
        Phase("project_plan", "project_plan", "project_plan.md",
              "Produce this app's milestones, sequencing, estimates, dependencies, "
              "risks, and a future implementation plan.", rounds=9,
              roles=["delivery", "backend", "qa"]),
        Phase("implementation_readiness_gate", "implementation_readiness_gate",
              "implementation_readiness_gate.md",
              "Audit whether the spec is coherent enough for a future one-shot build. "
              "Resolve contradictions, downgrade impossible scope, and name any "
              "information the builder must still collect.", rounds=9,
              roles=["delivery", "qa", "frontend"], checkpoint=True),
        Phase("human_qa_checklist", "human_qa_checklist", "human_qa_checklist.md",
              "Draft the manual QA script that a future build must satisfy for every "
              "important workflow, state, persistence case, and accessibility check.",
              rounds=9, roles=["qa", "design", "product"]),
        Phase("app_store_readiness", "app_store_readiness", "app_store_readiness.md",
              "Prepare launch-readiness material for a future build: positioning, "
              "screenshots, privacy labels, subscription/paywall risks, support needs, "
              "and likely review blockers.", rounds=9,
              roles=["product", "qa", "delivery"], checkpoint=True),
        Phase("final_review", "final_review", "final_review.md",
              "Final review of this app specification's completeness, consistency, "
              "risks, and go / no-go recommendation for a future build.",
              rounds=9, roles=["qa", "product", "delivery"]),
        Phase("portfolio_audit", "portfolio_audit", "portfolio_audit.md",
              "Confirm this spec child preserves the parent portfolio requirements and "
              "has the right sibling-folder/workflow metadata for later Jira/Notion "
              "backfill.", rounds=9, roles=["qa", "delivery", "product"]),
    ],
)

_ANSWER_QUESTION = Workflow(
    name="answer_question", title="Answer a Question", target="answer",
    description="Point the three agents at a question instead of an app. They "
                "reason from different angles, debate to the strongest answer, then "
                "write it up. No build phase.",
    phases=[
        Phase("deliberation", "deliberation", "deliberation.md",
              "Reason through the question from genuinely different angles, surface the "
              "strongest candidate answers, and pressure-test them against each other.",
              rounds=5, roles=["product", "qa", "design"]),
        Phase("answer", "answer", "answer.md",
              "Commit to the single best-supported answer and write it clearly, noting "
              "the key reasoning and any honest uncertainty.", rounds=2,
              roles=["product", "qa"]),
    ],
)

_RESEARCH = Workflow(
    name="research", title="Research a Topic", target="research",
    description="A deeper research pipeline: gather what's known, weigh the "
                "evidence and resolve contradictions, then write a synthesized, "
                "honest report.",
    phases=[
        Phase("gather", "gather", "gather.md",
              "Surface what's known about the topic: the key facts, the sub-questions "
              "that matter, competing viewpoints, and where the uncertainty lives.",
              rounds=4, roles=["product", "backend", "qa"]),
        Phase("analyze", "analyze", "analyze.md",
              "Weigh the evidence, resolve contradictions, separate strong claims from "
              "weak ones, and decide what the balance of reasoning actually supports.",
              rounds=5, roles=["qa", "backend", "product"]),
        Phase("report", "report", "report.md",
              "Write a clear synthesis with a bottom-line answer, the reasoning behind "
              "it, and an explicit note on what remains uncertain.", rounds=2,
              roles=["product", "qa"]),
    ],
)

_PRODUCTIONIZE = Workflow(
    name="productionize", title="Productionize a Prototype", target="productionize",
    description="Take a working prototype and turn it into something deployable: "
                "backend, data model, APIs, infra, and a real integration + "
                "production-readiness pass.",
    build_phase="build_backend",
    phases=[
        Phase("assess_prototype", "assess_prototype", "assess_prototype.md",
              "Review the existing prototype and define exactly what 'production-ready' "
              "means for it: what's missing, what must be real, and the acceptance bar.",
              rounds=4, roles=["product", "backend", "qa"]),
        Phase("backend_design", "backend_design", "backend_design.md",
              "Design the server: data model, API contracts, service boundaries, and how "
              "the client will talk to it.", rounds=5, roles=["backend", "frontend", "qa"]),
        Phase("infra_and_security", "infra_and_security", "infra_and_security.md",
              "Decide hosting, deployment, auth, security, observability, and a rough "
              "cost model, with justified trade-offs.", rounds=5,
              roles=["backend", "delivery", "qa"]),
        Phase("integration_plan", "integration_plan", "integration_plan.md",
              "Plan how the prototype consumes the backend, the migration/cutover, and "
              "the division of build work with conflict-prevention.", rounds=4,
              roles=["delivery", "frontend", "backend"]),
        Phase("build_backend", "build_backend", "agent_messages.md",
              "Iteratively build the backend and wire the prototype to it. Expose a "
              "GET /health endpoint returning 200 so the build can be verified by "
              "actually booting the server.", rounds=30,
              roles=["backend", "frontend", "qa"], writes=True,
              verify={"type": "http", "repair_iterations": 3, "ready_timeout": 45}),
        Phase("production_review", "production_review", "production_review.md",
              "Assess production readiness: reliability, rollback, security, and a clear "
              "go / no-go decision.", rounds=5, roles=["qa", "backend", "delivery"]),
    ],
)

_SPRINT = Workflow(
    name="sprint", title="Sprint Build (under 1 hour)", target="app",
    description="A time-boxed app build with a HARD wall-clock ceiling. Collapses "
                "planning to the essentials, caps build iterations, tightens per-turn "
                "timeouts, and enforces a run deadline so a working, compiling app "
                "lands in under an hour. Same parallel build + verify + iOS-signing "
                "machinery as app_build — just disciplined on time.",
    build_phase="build_coordination",
    budget={
        # Hard ceiling for the whole run (minutes). The engine caps every single
        # turn to the time remaining, so nothing can run past this — the ceiling is
        # a structural guarantee, not dependent on any turn finishing early. Set to
        # 57 so that signing + git commit (a few deterministic seconds after the
        # last agent turn) still land the whole run under an hour.
        "time_budget_minutes": 57,
        # Planning (all pre-build phases, collectively) must finish by
        # (budget - build_reserve) = +10 min. Whatever hasn't converged is skipped
        # so the build always gets to start.
        "build_reserve_minutes": 47,
        # The build must finish by (deadline - verify_reserve) = +45 min, leaving a
        # ~12-min tail for compile + repair + a fast review, which is never skipped.
        "verify_reserve_minutes": 12,
        # Per-turn timeout caps (seconds). Derived from measured tails: a 100s chat
        # cap kills claude's 1017s chat tail while letting a normal turn finish; a
        # 300s build cap covers a real code-generating/integration turn and force-
        # cuts the 17-min integrator outlier. The deadline cap above always wins
        # when it is tighter.
        "chat_turn_timeout": 100,
        "build_turn_timeout": 300,
        # Per compile attempt (seconds).
        "verify_timeout": 240,
    },
    phases=[
        Phase("initial_discussion", "initial_discussion", "initial_discussion.md",
              "Rapidly establish what this app is, who it's for, the core problem, hard "
              "scope boundaries for a <1-hour build, and the 3-5 must-have features. Be "
              "decisive and converge fast — this is a time-boxed sprint, not an "
              "open-ended debate.", rounds=2, roles=["product", "design", "qa"]),
        Phase("design_discussion", "design_discussion", "design_discussion.md",
              "Decide the key screens, the primary user flow, and the handful of design "
              "principles that govern them. Only what's buildable within the time box.",
              rounds=1, roles=["design", "frontend", "product"]),
        Phase("tech_specs", "tech_specs", "tech_specs.md",
              "Decide architecture, stack, data model, and how the build splits into "
              "parallel lanes. One clear plan; no bikeshedding.", rounds=1,
              roles=["frontend", "backend", "qa"]),
        Phase("build_coordination", "build_coordination", "agent_messages.md",
              "Build the working app in parallel lanes with an integrator turn between "
              "iterations. Ship a functional, compiling app within the time box; prefer "
              "a smaller app that works over a bigger one that doesn't.", rounds=6,
              roles=["frontend", "backend", "design", "qa"], writes=True,
              verify={"type": "xcodebuild", "repair_iterations": 2}),
        Phase("final_review", "final_review", "final_review.md",
              "Fast go/no-go: what works, what's stubbed, and the top risks. One round.",
              rounds=1, roles=["qa", "product", "delivery"]),
    ],
)

_AUDIT = Workflow(
    name="audit", title="Audit an Existing Codebase", target="audit",
    build_phase=None,
    description="Point the agents at a PRE-EXISTING codebase (READ-ONLY) and "
                "produce a prioritized findings report covering security "
                "vulnerabilities, correctness bugs, and modernization/update "
                "opportunities. The target is resolved from <app>/target_path.txt "
                "(or a 'target:' line in initial_prompt.md), is never written to, "
                "and is injected as a read-only digest. No build phase.",
    phases=[
        Phase("recon", "recon", "recon.md",
              "Map the target READ-ONLY before hunting: identify the language(s)/"
              "framework versions, dependency manifests (Package.swift/Podfile.lock, "
              "requirements.txt/poetry.lock, package.json/lockfile), entry points and "
              "external inputs (routes/handlers/URL schemes/CLI args), trust "
              "boundaries, where secrets and persistence live, and the highest-risk "
              "surfaces. Produce a concise shared architecture + attack-surface "
              "summary later phases build on. NO findings yet.",
              rounds=3, roles=["backend", "qa", "product"], reads_target=True),
        Phase("security", "security", "security.md",
              "Hunt SECURITY vulnerabilities: secrets in code, injection "
              "(SQL/command/path/template), broken authn/authz, insecure storage "
              "(iOS Keychain vs UserDefaults; plaintext creds), network/TLS (disabled "
              "cert validation, ATS exceptions, verify=False, rejectUnauthorized:false), "
              "over-broad permissions/entitlements and missing usage strings, "
              "vulnerable/unpinned dependencies, and PII/privacy leaks. For each "
              "finding give severity, category, file:line, a concrete exploit "
              "scenario, and a safe fix. Emit each finding as a finding-json block.",
              rounds=4, roles=["backend", "qa", "security"], reads_target=True),
        Phase("bugs", "bugs", "bugs.md",
              "Find correctness/reliability BUGS: concurrency/data races (Swift "
              "main-actor/Sendable, unawaited JS promises, unlocked Python globals), "
              "swallowed/incorrect error handling, unhandled edge cases "
              "(nil/None/empty/off-by-one/overflow/timezone), memory and lifecycle "
              "issues (retain cycles/[weak self], leaked handles/listeners), and logic "
              "errors. Each finding: file:line, the input/state that triggers wrong "
              "behavior, and a minimal fix. Emit each as a finding-json block.",
              rounds=4, roles=["backend", "qa", "frontend"], reads_target=True),
        Phase("modernization", "modernization", "modernization.md",
              "Identify UPDATES/modernization: outdated or abandoned dependencies "
              "(versions behind, EOL), deprecated/removed APIs, worthwhile modern-idiom "
              "refactors (async/await, Codable, type hints, ESM), dead code, and "
              "performance issues (N+1, sync I/O on hot paths, main-thread blocking). "
              "Cite file:line with current-vs-recommended and the migration "
              "effort/risk. Emit each as a finding-json block.",
              rounds=3, roles=["backend", "frontend", "delivery"], reads_target=True),
        Phase("report", "report", "report.md",
              "Synthesize (do NOT vote away) ALL findings from the security, bugs, and "
              "modernization phases into ONE prioritized, de-duplicated report grouped "
              "Critical/High/Medium/Low. Open with a 3-line severity-count summary and "
              "end with the top 5 fixes to do first. The ranked list itself is "
              "assembled deterministically by the tool.",
              rounds=2, roles=["product", "qa", "delivery"], reads_target=True),
    ],
)

_BUILTINS = {w.name: w for w in (_APP_BUILD, _APP_SPEC, _ANSWER_QUESTION, _RESEARCH,
                                 _PRODUCTIONIZE, _SPRINT, _AUDIT)}


def _load_shipped_fallbacks():
    """Give every workflow name that ships as JSON next to this module (not a
    project's orch_dir — the packaged copy under WORKFLOWS_DIR) an in-memory
    fallback in _BUILTINS, for the names that only exist as JSON
    (app_build_child, brainstorm, full_max, iterate, library_mining, prototype,
    vslice — everything not hand-authored above as a Python Workflow).

    Without this, load_workflow() falls back to `if name in _BUILTINS: ...
    else return _BUILTINS[DEFAULT_WORKFLOW]` for those 7 names, meaning a
    missing/corrupt on-disk iterate.json silently turns an intended surgical
    `iterate` run into a full `app_build` rebuild — a correctness bug with real
    consequences, not just a degraded fallback. Sourcing the fallback from the
    same shipped JSON (rather than hand-duplicating it as Python prose) means
    it can never drift from the on-disk version. This also fixes
    ensure_seeded()'s self-healing for these 7 files, which previously only
    covered the 7 hand-authored ones. Best-effort at import time; a bad shipped
    file is skipped rather than breaking import."""
    try:
        names = [fn[:-5] for fn in os.listdir(WORKFLOWS_DIR) if fn.endswith(".json")]
    except OSError:
        return
    for name in names:
        if name in _BUILTINS:
            continue  # hand-authored Python version already covers it
        try:
            with open(os.path.join(WORKFLOWS_DIR, name + ".json"), encoding="utf-8") as fh:
                _BUILTINS[name] = Workflow.from_json(json.load(fh))
        except (OSError, ValueError, KeyError, TypeError):
            continue


_load_shipped_fallbacks()


# ---------------------------------------------------------------------------
# Loading / seeding
# ---------------------------------------------------------------------------
def _workflows_dir(orch_dir):
    return os.path.join(orch_dir, "workflows") if orch_dir else WORKFLOWS_DIR


def ensure_seeded(orch_dir=None):
    """Materialize built-in workflows to JSON on first run (never clobbering an
    existing file), so the GUI has editable files. Best-effort."""
    d = _workflows_dir(orch_dir)
    try:
        os.makedirs(d, exist_ok=True)
        for name, wf in _BUILTINS.items():
            path = os.path.join(d, name + ".json")
            if not os.path.exists(path):
                with open(path, "w", encoding="utf-8") as fh:
                    json.dump(wf.to_json(), fh, indent=2)
    except OSError:
        pass


def load_workflow(name, orch_dir=None):
    """On-disk JSON wins (that's where GUI edits live); fall back to built-in;
    fall back to app_build. Never raises."""
    name = (name or DEFAULT_WORKFLOW).strip()
    path = os.path.join(_workflows_dir(orch_dir), name + ".json")
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as fh:
                return Workflow.from_json(json.load(fh))
        # TypeError covers GUI/user edits like "rounds": null, a non-dict
        # "budget", or a non-iterable "roles" — fall back like other bad JSON.
        except (OSError, ValueError, KeyError, TypeError):
            pass
    if name in _BUILTINS:
        return _BUILTINS[name]
    return _BUILTINS[DEFAULT_WORKFLOW]


def list_workflows(orch_dir=None):
    """Names available: union of on-disk JSON files and built-ins."""
    names = set(_BUILTINS)
    d = _workflows_dir(orch_dir)
    try:
        for fn in os.listdir(d):
            if fn.endswith(".json"):
                names.add(fn[:-5])
    except OSError:
        pass
    # app_build first, then the rest alphabetically
    ordered = [DEFAULT_WORKFLOW] if DEFAULT_WORKFLOW in names else []
    ordered += sorted(n for n in names if n != DEFAULT_WORKFLOW)
    return ordered


def resolve_workflow_for_app(app_dir, default_name=None, orch_dir=None):
    """Which workflow does this app use? Priority:
      1. <app>/workflow.txt  (single workflow name — written by the GUI picker)
      2. a 'workflow: <name>' line in the first ~15 lines of initial_prompt.md
      3. the provided default (config runtime.default_workflow)
      4. app_build
    Returns a Workflow.
    """
    # 1. explicit workflow.txt
    wt = os.path.join(app_dir, "workflow.txt")
    try:
        with open(wt, encoding="utf-8") as fh:
            name = fh.read().strip().splitlines()[0].strip()
            if name:
                return load_workflow(name, orch_dir)
    except (OSError, IndexError):
        pass
    # 2. frontmatter in the initial prompt
    ip = os.path.join(app_dir, "initial_prompt", "initial_prompt.md")
    try:
        with open(ip, encoding="utf-8") as fh:
            for line in fh.read().splitlines()[:15]:
                s = line.strip().lstrip("#").strip()
                low = s.lower()
                if low.startswith("workflow:"):
                    name = s.split(":", 1)[1].strip()
                    if name:
                        return load_workflow(name, orch_dir)
    except OSError:
        pass
    return load_workflow(default_name or DEFAULT_WORKFLOW, orch_dir)


def read_target_path(app_dir, orch_dir=None):
    """Resolve the pre-existing codebase an `audit` app points at. Precedence:
      1. <app>/target_path.txt — first non-empty line.
      2. a 'target: <path>' line in the first ~15 lines of initial_prompt.md.
    The path is realpath'd and must be an existing directory. It is REJECTED if it
    resolves inside the app's own app_build (which is writable) — an audit target
    must be a separate, read-only tree. Returns an absolute dir path or None.
    """
    def _guard(p):
        rp = os.path.realpath(os.path.expanduser((p or "").strip()))
        if not rp or not os.path.isdir(rp):
            return None
        ab = os.path.realpath(os.path.join(app_dir, "app_build"))
        if rp == ab or rp.startswith(ab + os.sep):
            return None  # never alias the writable build dir
        return rp

    tp = os.path.join(app_dir, "target_path.txt")
    try:
        with open(tp, encoding="utf-8") as fh:
            # Skip blank/comment lines and CONTINUE past invalid entries — a
            # commented or stale first line must not abort the audit when a
            # valid path follows (mirrors the read_target_paths loop below).
            for line in fh.read().splitlines():
                s = line.strip()
                if not s or s.startswith("#"):
                    continue
                g = _guard(s)
                if g:
                    return g
    except OSError:
        pass
    ip = os.path.join(app_dir, "initial_prompt", "initial_prompt.md")
    try:
        with open(ip, encoding="utf-8") as fh:
            for line in fh.read().splitlines()[:15]:
                s = line.strip().lstrip("#").strip()
                if s.lower().startswith("target:"):
                    g = _guard(s.split(":", 1)[1])
                    if g:
                        return g
    except OSError:
        pass
    return None


def read_target_paths(app_dir, orch_dir=None):
    """Like read_target_path but returns ALL valid target dirs (one per non-empty
    line of target_path.txt, plus any `target:` lines in the prompt) — for the
    library_mining workflow, which analyzes a whole portfolio at once. Each path is
    guarded the same way (realpath, is-a-dir, not inside app_build). De-duplicated,
    order preserved. Returns a possibly-empty list."""
    def _guard(p):
        rp = os.path.realpath(os.path.expanduser((p or "").strip()))
        if not rp or not os.path.isdir(rp):
            return None
        ab = os.path.realpath(os.path.join(app_dir, "app_build"))
        if rp == ab or rp.startswith(ab + os.sep):
            return None
        return rp

    out, seen = [], set()
    def _add(p):
        g = _guard(p)
        if g and g not in seen:
            seen.add(g)
            out.append(g)

    tp = os.path.join(app_dir, "target_path.txt")
    try:
        with open(tp, encoding="utf-8") as fh:
            for line in fh.read().splitlines():
                if line.strip() and not line.strip().startswith("#"):
                    _add(line)
    except OSError:
        pass
    ip = os.path.join(app_dir, "initial_prompt", "initial_prompt.md")
    try:
        with open(ip, encoding="utf-8") as fh:
            for line in fh.read().splitlines()[:20]:
                s = line.strip().lstrip("#").strip()
                if s.lower().startswith("target:"):
                    _add(s.split(":", 1)[1])
    except OSError:
        pass
    return out
