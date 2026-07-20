"""V3 7.11: pipeline presets — a pipeline is NOT a second execution engine.
It is a saved preset of Conductor routing rules + a goal manifest (the
architectural insight in orchestrator-v3-sections-plan.md:240-256), so a
preset run inherits Mission Control, budgets, oversight dials, replay, and
termination for free. This module is the pure schema + validator both the
GUI canvas (compiler/decompiler) and conductor.py (the loader) share — it
mints nothing, mutates nothing, imports nothing from the engine.

One JSON file per preset, anywhere on disk (the GUI owns WHERE presets are
persisted, mirroring the RunProfile idiom — this module only cares about the
file's CONTENT):

    {
      "schema_version": 1,
      "preset_name": "Brainstorm to Plan",
      "routing": {"artifact_routes": {...}, "rules": [...]},   # 7.2 shape
      "goal_manifest": {"goal": {...}, "quiescence_cycles": N,
                        "budgets": {...}, "stall": {...}},      # 7.5 shape
      "seed": {"section": "ideas", "prompt_template": "..."},
      "ui": {...}   # GUI-only layout (node positions etc) — opaque, NEVER
                     # read or interpreted here or by conductor.py; carried
                     # through validation byte-for-byte so round-trips and
                     # unknown-key preservation both hold (§6.2).
    }

routing/goal_manifest reuse conductor_routing.validate_rule and
conductor_termination.normalize_manifest respectively — ONE validation path,
so a preset rule/goal is accepted iff the exact same rule/goal would be
accepted in a normal routing.json/goal_manifest.json (no parallel schema to
drift out of sync).

CANVAS COMPILER NOTE (edges vs. type-wide routes): a preset's `routing.rules`
apply across EVERY section in the run (conductor.py's route_engine uses ONE
RouteConfig, built from the whole preset, for every session's section loop —
that is what "zero pipeline-specific execution code" means: the preset
supplies the SAME config object load_route_config would otherwise build
per-section). rules_for (conductor_routing.py) already filters a rule by
`match.source_section` when the field is set, and treats an ABSENT
source_section as "any producing section" (a wildcard). So a canvas edge
A -> B for artifact type X compiles to a rule with
`"match": {"artifact_type": "X", "source_section": "A"}` — the compiler MUST
set source_section per edge, or the rule becomes "any section producing X
routes to B," not the specific edge the author drew on the canvas.
"""
import copy
import json
import os

import conductor_routing as crlib
import conductor_termination as ctlib

SCHEMA_VERSION = 1


def _err(field, detail):
    return "%s: %s" % (field, detail)


def validate_preset(data, known_sections, on_warn=None):
    """(normalized_preset, error) — error is None iff the preset is fully
    valid. NEVER partially applies: any single invalid field/edge fails the
    WHOLE preset with a message naming exactly that field/edge (§5.2), so the
    Conductor never starts on partially-applied config. `known_sections` is
    the set of section names that actually exist in this workspace (fleet
    sections dir) — a preset naming an unknown section is refused, not
    silently dropped.

    `ui` is preserved verbatim (never validated, never interpreted) so the
    canvas's layout round-trips losslessly and unknown extra top-level keys
    in `ui` survive untouched (§6.2)."""
    if not isinstance(data, dict):
        return None, _err("preset", "must be a JSON object")
    name = data.get("preset_name")
    if not isinstance(name, str) or not name.strip():
        return None, _err("preset_name", "missing or empty")

    routing_raw = data.get("routing")
    if not isinstance(routing_raw, dict):
        return None, _err("routing", "missing or not an object")
    routes = crlib.normalize_routes(routing_raw.get("artifact_routes"))
    rules = []
    for i, raw_rule in enumerate(routing_raw.get("rules") or []):
        norm, rule_err = crlib.validate_rule(raw_rule)
        if rule_err:
            return None, _err("routing.rules[%d]" % i, rule_err)
        rules.append(norm)
    for target in list(routes.values()) + [t for r in rules
                                           for t in r["targets"]]:
        if target not in known_sections:
            return None, _err("routing", "targets unknown section %r"
                              % (target,))

    goal_raw = data.get("goal_manifest")
    if not isinstance(goal_raw, dict):
        return None, _err("goal_manifest", "missing or not an object")
    warnings = []
    goal_manifest = ctlib.normalize_manifest(goal_raw, warnings.append)
    if warnings:
        # normalize_manifest degrades individual bad sub-fields to safe
        # defaults rather than failing — but a PRESET must be explicit: an
        # author who wrote an invalid goal/budget field gets told, not
        # silently downgraded to "never terminate."
        return None, _err("goal_manifest", "; ".join(warnings))

    seed = data.get("seed")
    if not isinstance(seed, dict) or not isinstance(seed.get("section"), str) \
            or not seed["section"].strip():
        return None, _err("seed.section", "missing or empty")
    if seed["section"] not in known_sections:
        return None, _err("seed.section", "unknown section %r"
                          % (seed["section"],))
    if not isinstance(seed.get("prompt_template"), str):
        return None, _err("seed.prompt_template", "missing or not a string")

    normalized = {
        "schema_version": SCHEMA_VERSION,
        "preset_name": name.strip(),
        "routing": {"artifact_routes": routes, "rules": rules},
        "goal_manifest": goal_manifest,
        "seed": {"section": seed["section"].strip(),
                "prompt_template": seed["prompt_template"]},
        # opaque passthrough — copied, never inspected
        "ui": copy.deepcopy(data.get("ui")) if "ui" in data else {},
    }
    return normalized, None


def load_preset_file(path, known_sections, on_warn=None):
    """(normalized_preset, error). A missing/unreadable/non-JSON file is
    itself a validation failure (there is no 'safe default' for a preset the
    caller explicitly asked to run — refuse, don't guess)."""
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except OSError as exc:
        return None, _err("preset file", "unreadable (%s)" % exc)
    except ValueError as exc:
        return None, _err("preset file", "invalid JSON (%s)" % exc)
    return validate_preset(data, known_sections, on_warn)


def as_route_config(normalized_preset):
    """A conductor_routing.RouteConfig built from an already-validated
    preset's routing block — the exact object route_engine's section loop
    would otherwise get from load_route_config, so a preset run needs no
    parallel routing code path."""
    r = normalized_preset["routing"]
    return crlib.RouteConfig(routes=dict(r["artifact_routes"]),
                             rules=list(r["rules"]), ok=True)


def known_sections_in_workspace(sections_dir):
    """Every section name that has a manifest dir under sections_dir (the
    fleet sections/ tree) — the set a preset's routing targets and seed
    section are checked against."""
    try:
        return {name for name in os.listdir(sections_dir)
               if os.path.isdir(os.path.join(sections_dir, name))
               and not name.startswith(("_", "."))}
    except OSError:
        return set()
