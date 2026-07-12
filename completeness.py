#!/usr/bin/env python3
"""
completeness.py — completeness profiles + stop-target selection (V2 spec §7.2 / §7.4 / §14).

Pure functions over a phase list so they unit-test without the engine. A run may
carry a per-project run_config.json:

    {"completeness": "mvp", "autonomy": "fully_autonomous", "stop_after_phase": "tech spec complete"}

- completeness picks which phases run (a named subset + a round multiplier),
- stop_after_phase ends the run at a chosen phase boundary.

Profile phase_keys and stop-target keys reference the ACTUAL phase keys of the
shipped workflows (workflows/app_build.json etc.), not the spec's aspirational
names — tests/test_completeness.py asserts they resolve against every shipped
workflow. Both selectors are defensive: a profile that would gut a workflow
falls back to all phases, and an unresolvable stop target leaves the run
unchanged; both report through the optional on_warn callback instead of
silently mis-running (the pre-fix behavior stripped a run to 1-2 phases).

Standard library only.
"""

import json
import os

# Spec §7.2 profiles, keyed to the real workflow phase keys. phase_keys is the
# inclusion allow-list; a phase not listed is skipped for that profile.
# round_multiplier scales each phase's round budget. The build phase and the
# final (verification/review) phase are always retained by filter_phases.
PROFILES = {
    "prototype": {
        "round_multiplier": 0.75,
        "phase_keys": ["prompt_contract", "product_research",
                       "portfolio_selection", "initial_discussion",
                       "per_app_product_brief", "app_features",
                       "design_handoff", "ios_architecture_review",
                       "tech_specs", "task_assignments",
                       "implementation_readiness_gate", "build_coordination",
                       "build_verification", "human_qa_checklist",
                       "app_store_readiness", "final_review"],
    },
    "mvp": {
        "round_multiplier": 1.0,
        "phase_keys": ["prompt_contract", "product_research",
                       "portfolio_selection", "initial_discussion",
                       "per_app_product_brief", "next_steps_small",
                       "app_features", "design_discussion", "design_handoff",
                       "ios_architecture_review", "tech_specs",
                       "task_assignments", "implementation_readiness_gate",
                       "build_coordination", "build_verification",
                       "human_qa_checklist", "app_store_readiness",
                       "final_review"],
    },
    "v1": {"round_multiplier": 1.25, "phase_keys": None},           # None = all phases
    "production_draft": {"round_multiplier": 1.5, "phase_keys": None},
}

# Friendly stop-target labels -> the phase key to stop after (spec §7.4 / §14),
# mapped to keys that exist in the shipped app_build workflow.
STOP_TARGETS = {
    "idea validated": "next_steps_small",
    "docs complete": "implementation_readiness_gate",
    "design complete": "design_handoff",
    "tech spec complete": "tech_specs",
    "mvp built": "build_verification",
    "production ready": "final_review",
}

# Backward-compatible fallbacks for older/custom workflows that do not yet have
# the newer higher-quality gates.
STOP_TARGET_FALLBACKS = {
    "implementation_readiness_gate": "project_plan",
    "design_handoff": "design_discussion",
    "build_verification": "build_coordination",
}

# A profile filter that keeps fewer than this many phases is treated as a
# key-mismatch against the workflow and falls back to all phases.
_MIN_KEPT_PHASES = 3


def profile(name):
    return PROFILES.get((name or "").strip().lower())


def round_multiplier(name):
    p = profile(name)
    return float(p["round_multiplier"]) if p else 1.0


def filter_phases(phases, completeness_name, on_warn=None):
    """Return the phases included by the named completeness profile, preserving
    order. Unknown/blank profile or a profile with phase_keys=None => all phases.

    Defensive guarantees (a broken allow-list must never gut a run):
    - the workflow's FINAL phase (the verification/review gate) is always kept;
    - if the allow-list intersects the workflow so poorly that fewer than
      _MIN_KEPT_PHASES phases survive, ALL phases are returned and on_warn is
      called — a silent 1-2 phase run is worse than an unfiltered one."""
    on_warn = on_warn or (lambda _msg: None)
    phases = list(phases)
    p = profile(completeness_name)
    if not p or p.get("phase_keys") is None:
        return phases
    allow = set(p["phase_keys"])
    kept = [ph for ph in phases if _phase_key(ph) in allow]
    # Always retain the final phase — it computes the verification gate.
    if phases and phases[-1] not in kept:
        kept.append(phases[-1])
    if len(kept) < min(_MIN_KEPT_PHASES, len(phases)):
        on_warn("completeness '%s' matched only %d of %d workflow phases — "
                "profile keys don't fit this workflow; running ALL phases instead."
                % (completeness_name, len(kept), len(phases)))
        return phases
    return kept


def apply_stop_target(phases, stop_after_phase, on_warn=None):
    """Truncate the phase list after the given phase key (inclusive). If the key
    is a friendly label, it is resolved first. Unknown/blank => unchanged.

    If the resolved key matches NO phase in the workflow, the list is returned
    unchanged and on_warn is called — never silently run everything while the
    user believes an early stop is in effect."""
    on_warn = on_warn or (lambda _msg: None)
    phases = list(phases)
    if not stop_after_phase:
        return phases
    key = STOP_TARGETS.get(stop_after_phase.strip().lower(), stop_after_phase)
    phase_keys = [_phase_key(ph) for ph in phases]
    if key not in phase_keys and STOP_TARGET_FALLBACKS.get(key) in phase_keys:
        key = STOP_TARGET_FALLBACKS[key]
    if key not in phase_keys:
        on_warn("stop target '%s' (resolved to phase key '%s') does not exist in "
                "this workflow — ignoring it; the FULL workflow will run."
                % (stop_after_phase, key))
        return phases
    out = []
    for ph in phases:
        out.append(ph)
        if _phase_key(ph) == key:
            break
    return out


def load_run_config(app_dir):
    """Read <app>/run_config.json if present. Returns a dict (empty if none)."""
    try:
        with open(os.path.join(app_dir, "run_config.json"), encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _phase_key(ph):
    return ph.key if hasattr(ph, "key") else (ph[0] if isinstance(ph, (list, tuple)) else ph)


# ---------------------------------------------------------------------------
# Definition of Done (task #8): an editable per-tier checklist of what
# "complete" MEANS, consumed by the adherence gate (grading) and design lint
# (machine-checkable items). Tiers inherit upward: prototype < mvp < v1 <
# production_draft. definition_of_done.json overrides the defaults per tier.
# ---------------------------------------------------------------------------
DOD_FILENAME = "definition_of_done.json"
DOD_ORDER = ("prototype", "mvp", "v1", "production_draft")
DEFAULT_DOD = {
    "prototype": [
        "Every core feature is FUNCTIONALLY implemented, not just visually "
        "present: real logic runs, real data is produced/stored, state "
        "persists where the feature implies it should. A screen that looks "
        "finished but whose controls don't actually work (taps that do "
        "nothing, hardcoded placeholder data, unperformed calculations) is "
        "UNMET regardless of visual polish.",
        "The primary user workflow works end-to-end using REAL user-entered "
        "data, not hardcoded content standing in for it.",
        "The app launches to a real screen, not a placeholder.",
        "No crash on the primary workflow.",
    ],
    "mvp": [
        "Every screen has empty, loading, and error states.",
        "Dark mode is styled and readable.",
        "Onboarding or a self-explanatory first screen exists.",
        "All views style through DesignSystem tokens.",
        "Every interactive element has an accessibilityIdentifier.",
    ],
    "v1": [
        "All declared user flows pass against the built app.",
        "No TODO/FIXME markers left in source.",
        "App icon and display name are set.",
    ],
    "production_draft": [
        "Unit or UI test target exists and runs.",
        "Accessibility audit issues addressed.",
        "No dead buttons.",
    ],
}


def load_dod(orch_dir):
    """definition_of_done.json as {tier: [items]}; missing/corrupt -> defaults."""
    out = {k: list(v) for k, v in DEFAULT_DOD.items()}
    try:
        with open(os.path.join(orch_dir, DOD_FILENAME), encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return out
    if isinstance(data, dict):
        for tier in DOD_ORDER:
            v = data.get(tier)
            if isinstance(v, list) and v:
                out[tier] = [str(i) for i in v if str(i).strip()]
    return out


def dod_items(orch_dir, tier):
    """The ACTIVE checklist for a tier: its items plus every lower tier's
    (inheritance). Unknown tier -> v1."""
    tier = tier if tier in DOD_ORDER else "v1"
    dod = load_dod(orch_dir)
    items = []
    for t in DOD_ORDER:
        items.extend(dod.get(t) or [])
        if t == tier:
            break
    return items


def render_dod(orch_dir, tier):
    """Prompt block for the adherence gate ('' never happens — defaults exist)."""
    items = dod_items(orch_dir, tier)
    return ("===== DEFINITION OF DONE (tier: %s — grade against EVERY item) =====\n"
            % (tier if tier in DOD_ORDER else "v1")
            + "\n".join("%d. %s" % (i + 1, it) for i, it in enumerate(items)))
