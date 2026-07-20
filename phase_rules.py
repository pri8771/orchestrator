#!/usr/bin/env python3
"""
Editable phase playbooks.

The workflow JSON decides *which* phases run. phase_rules.json decides how each
phase should behave: required sections, acceptance checks, and quality bars.
This keeps the one-shot-app "taste" editable without changing Python code.

Best-effort by design: missing/malformed files return no playbook instead of
breaking a run.
"""

import copy
import json
import os
import typing

import buildpolicy as buildpolicylib

RULES_FILENAME = "phase_rules.json"

# Per-phase list fields. A GUI edit that turns one of these into a string (e.g.
# "rules": "be careful") must not reach _bullets(), which would silently
# iterate the string's characters instead of mis-rendering loudly.
_PHASE_LIST_FIELDS = ("rules", "required_output", "acceptance_checks")

# Keyed on (st_mtime_ns, st_size) so a run only re-parses phase_rules.json
# once per phase pair of calls (render_phase_playbook +
# render_phase_quality_rubric) instead of hitting disk every time, while an
# on-disk edit still busts it — including a same-second replacement, which an
# mtime-alone key served stale. A same-second, same-length rewrite is still
# theoretically stale; mtime_ns+size is the accepted stdlib-only tradeoff
# (no content hashing on this hot path).
_CACHE: typing.Dict[str, typing.Tuple[typing.Tuple[int, int],
                                      typing.Dict[str, typing.Any]]] = {}


def _clean_phase(phase):
    if not isinstance(phase, dict):
        return {}
    out = dict(phase)
    for field in _PHASE_LIST_FIELDS:
        if field in out and not isinstance(out[field], list):
            out[field] = []
    return out


def load_rules(orch_dir):
    empty = {"schema_version": 1, "global_app_rules": [], "phases": {}}
    path = os.path.join(orch_dir, RULES_FILENAME)
    try:
        st = os.stat(path)
    except OSError:
        _CACHE.pop(path, None)
        return empty
    key = (st.st_mtime_ns, st.st_size)
    cached = _CACHE.get(path)
    if cached is not None and cached[0] == key:
        return copy.deepcopy(cached[1])
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return empty
    if not isinstance(data, dict):
        return empty
    if not isinstance(data.get("global_app_rules"), list):
        data["global_app_rules"] = []
    phases = data.get("phases")
    data["phases"] = {k: _clean_phase(v) for k, v in phases.items()} \
        if isinstance(phases, dict) else {}
    _CACHE[path] = (key, data)
    return copy.deepcopy(data)


def _load_layer(path, on_fallback=None):
    """One OVERLAY rules file (V3 board 3.2): a section's rules.json or a
    project's phase_rules.json. ABSENT is silent None (absence is normal,
    not a fallback); PRESENT-but-corrupt is None + one on_fallback(path,
    error) call per fresh parse — the failure is cached on (mtime_ns,
    size) like a success, so per-turn renders neither re-parse nor
    re-banner until the file changes. global_app_rules in an overlay is
    deliberately never read (global-only; the 3.7 linter warns on it)."""
    try:
        st = os.stat(path)
    except OSError:
        _CACHE.pop(path, None)
        return None
    key = (st.st_mtime_ns, st.st_size)
    cached = _CACHE.get(path)
    if cached is not None and cached[0] == key:
        return copy.deepcopy(cached[1]) if cached[1] is not None else None
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict) or \
                not isinstance(data.get("phases"), dict):
            raise ValueError("rules file must be an object with a "
                             "'phases' object")
    except (OSError, ValueError) as exc:
        _CACHE[path] = (key, None)
        if on_fallback is not None:
            on_fallback(path, exc)
        return None
    layer = {"phases": {k: _clean_phase(v)
                        for k, v in data["phases"].items()}}
    _CACHE[path] = (key, layer)
    return copy.deepcopy(layer)


def load_rules_layered(orch_dir, layers=None, on_fallback=None,
                       target_policy=None):
    """The layered rules view: section -> project-override -> global, with
    WHOLE-phase-entry precedence — the most specific layer containing a
    phase key supplies the entire entry, no cross-layer field merging.
    `layers` is ordered most-specific-first; global_app_rules always comes
    from the global file alone. With no layers this IS load_rules —
    flat/legacy runs behave byte-identically."""
    rules = load_rules(orch_dir)
    phases = dict(rules.get("phases", {}))
    if target_policy:
        # Build-target policy sits above fleet-neutral rules but below the
        # ordinary section/project overlays.  This preserves the established
        # specificity order while removing platform assumptions from fleet
        # config.
        phases.update(target_policy.get("phase_rules", {}))
        rules["phases"] = phases
    live = [p for p in (layers or []) if p]
    if not live:
        return rules
    for path in reversed(live):          # least specific first; later wins
        layer = _load_layer(path, on_fallback)
        if layer:
            phases.update(layer["phases"])
    rules["phases"] = phases
    return rules


def _bullets(items):
    return "\n".join("- %s" % str(i).strip() for i in items if str(i).strip())


def render_phase_playbook(orch_dir, workflow_target, phase_key,
                          layers=None, on_fallback=None):
    """Markdown snippet injected into every turn for ``phase_key``."""
    target_policy = buildpolicylib.load_target_policy(
        orch_dir, workflow_target, on_fallback=on_fallback)
    rules = load_rules_layered(orch_dir, layers, on_fallback,
                               target_policy=target_policy)
    phase = rules.get("phases", {}).get(phase_key, {})
    if not isinstance(phase, dict):
        phase = {}

    parts = []
    global_rules = (target_policy.get("app_rules", []) if target_policy
                    else (rules.get("global_app_rules")
                          if workflow_target == "app" else []))
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


def render_phase_quality_rubric(orch_dir, workflow_target, phase_key,
                                layers=None, on_fallback=None):
    """Markdown rubric used by the phase quality gate.

    This intentionally mirrors the editable playbook, but phrases it as an
    evaluator checklist. Missing rule files stay best-effort: the fixed gate can
    still check for a useful, concrete phase artifact.
    """
    target_policy = buildpolicylib.load_target_policy(
        orch_dir, workflow_target, on_fallback=on_fallback)
    rules = load_rules_layered(orch_dir, layers, on_fallback,
                               target_policy=target_policy)
    phase = rules.get("phases", {}).get(phase_key, {})
    if not isinstance(phase, dict):
        phase = {}

    parts = []
    global_rules = (target_policy.get("app_rules", []) if target_policy
                    else (rules.get("global_app_rules")
                          if workflow_target == "app" else []))
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
