#!/usr/bin/env python3
"""
Editable phase playbooks.

The workflow JSON decides *which* phases run. phase_rules.json decides how each
phase should behave: required sections, acceptance checks, and quality bars.
This keeps the one-shot-app "taste" editable without changing Python code.

Best-effort by design: missing/malformed files return no playbook instead of
breaking a run.
"""

import json
import os

RULES_FILENAME = "phase_rules.json"


def load_rules(orch_dir):
    empty = {"schema_version": 1, "global_app_rules": [], "phases": {}}
    path = os.path.join(orch_dir, RULES_FILENAME)
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return empty
    if not isinstance(data, dict):
        return empty
    if not isinstance(data.get("global_app_rules"), list):
        data["global_app_rules"] = []
    if not isinstance(data.get("phases"), dict):
        data["phases"] = {}
    return data


def _bullets(items):
    return "\n".join("- %s" % str(i).strip() for i in items if str(i).strip())


def render_phase_playbook(orch_dir, workflow_target, phase_key):
    """Markdown snippet injected into every turn for ``phase_key``."""
    rules = load_rules(orch_dir)
    phase = rules.get("phases", {}).get(phase_key, {})
    if not isinstance(phase, dict):
        phase = {}

    parts = []
    global_rules = rules.get("global_app_rules") if workflow_target == "app" else []
    if global_rules:
        parts.append("Global app-build rules:\n" + _bullets(global_rules))
    if phase.get("rules"):
        parts.append("Phase rules:\n" + _bullets(phase.get("rules", [])))
    if phase.get("required_output"):
        parts.append("Required output:\n" + _bullets(phase.get("required_output", [])))
    if phase.get("acceptance_checks"):
        parts.append("Acceptance checks before consensus:\n"
                     + _bullets(phase.get("acceptance_checks", [])))
    if not parts:
        return ""
    return ("\n\n===== PHASE PLAYBOOK (editable phase_rules.json) =====\n"
            + "\n\n".join(parts))


def render_phase_quality_rubric(orch_dir, workflow_target, phase_key):
    """Markdown rubric used by the phase quality gate.

    This intentionally mirrors the editable playbook, but phrases it as an
    evaluator checklist. Missing rule files stay best-effort: the fixed gate can
    still check for a useful, concrete phase artifact.
    """
    rules = load_rules(orch_dir)
    phase = rules.get("phases", {}).get(phase_key, {})
    if not isinstance(phase, dict):
        phase = {}

    parts = []
    global_rules = rules.get("global_app_rules") if workflow_target == "app" else []
    if global_rules:
        parts.append("Global quality bar:\n" + _bullets(global_rules))
    if phase.get("rules"):
        parts.append("Phase intent:\n" + _bullets(phase.get("rules", [])))
    if phase.get("required_output"):
        parts.append("Required output coverage:\n"
                     + _bullets(phase.get("required_output", [])))
    if phase.get("acceptance_checks"):
        parts.append("Acceptance checks:\n" + _bullets(phase.get("acceptance_checks", [])))
    return "\n\n".join(parts)
