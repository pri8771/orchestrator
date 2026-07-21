"""V3 board 9.0: the Situation data format — a completeness profile
generalized to doc slots + pipeline + casts (sections plan §14.1). Where
completeness.py's PROFILES pick a phase-key allow-list for ONE project's
workflow, a Situation picks which of the 40 documentation slots (5.1/5.3's
doc_map.json) a run needs, which 7.11 pipeline preset (if any) seeds it, and
per-phase/section/cast overrides layered on top.

Data only — no engine behavior changes in this card (9.1 wires resolution
into phase filtering; 9.2/9.3 are the GUI). This module owns the format,
the loader, the lint validator, and the six seeded defaults; no other
module builds a situations/<name>/situation.json path by hand.

Schema (schema_version 1)::

    {
      "schema_version": 1,
      "name": "prototype_sprint",
      "description": "...",
      "doc_slots": ["problem_statement", ...],       # ordered, doc_map.json slot ids
      "pipeline_ref": "",                             # a 7.11 preset name, or ''
      "overrides": {
        "sections": {"<section>": {"enabled": true|false}},
        "phases": {"<phase key>": {<modelrouting.PHASE_FIELDS subset>,
                                   "rounds": <int>}},
        "casts": {"<phase key>": ["<agent or persona ref>", ...]}
      }
    }

overrides.phases keys are validated against modelrouting.PHASE_FIELDS plus
the single additional "rounds" field (not a PHASE_FIELDS entry, but the one
per-phase knob every situation-override needs and model_routing.json does
not itself carry) — deliberately the SAME vocabulary 9.1/9.3 already know,
so applying a Situation needs no translation layer.

Seed materialization follows workflows.ensure_seeded's contract exactly:
never clobber an existing file, disk wins, best-effort. A corrupt
situation.json is reported through on_error and skipped — never silently
defaulted, never blinding the rest of the library (§6.2).

Standard library only.
"""
import json
import os

import modelrouting as mrlib

SCHEMA_VERSION = 1
SITUATIONS_DIRNAME = "situations"
SITUATION_FILENAME = "situation.json"

# The one extra per-phase override field beyond modelrouting.PHASE_FIELDS —
# every situation override needs a round-count knob, and model_routing.json
# doesn't carry one (rounds live in workflow phase defs, not routing).
_EXTRA_PHASE_FIELDS = ("rounds",)


def _warn(on_error, msg):
    if on_error:
        on_error(msg)


def situations_root(orch_dir):
    return os.path.join(orch_dir, SITUATIONS_DIRNAME)


def _situation_path(name, orch_dir):
    return os.path.join(situations_root(orch_dir), name, SITUATION_FILENAME)


def _default_overrides():
    return {"sections": {}, "phases": {}, "casts": {}}


def _normalize(raw, name_hint=None):
    """Raw JSON -> a well-shaped situation dict, or None if the top-level
    shape is unusable (not an object). Individual bad fields degrade to
    empty/defaults here; load_situation's caller decides whether that's
    acceptable via lint_situation — normalization itself never refuses."""
    if not isinstance(raw, dict):
        return None
    name = raw.get("name") if isinstance(raw.get("name"), str) and raw.get("name").strip() \
        else (name_hint or "")
    doc_slots = raw.get("doc_slots")
    doc_slots = [s for s in doc_slots if isinstance(s, str) and s.strip()] \
        if isinstance(doc_slots, list) else []
    pipeline_ref = raw.get("pipeline_ref")
    pipeline_ref = pipeline_ref.strip() if isinstance(pipeline_ref, str) else ""
    overrides_raw = raw.get("overrides")
    overrides = _default_overrides()
    if isinstance(overrides_raw, dict):
        sec = overrides_raw.get("sections")
        if isinstance(sec, dict):
            overrides["sections"] = {str(k): v for k, v in sec.items()
                                     if isinstance(v, dict)}
        ph = overrides_raw.get("phases")
        if isinstance(ph, dict):
            overrides["phases"] = {str(k): v for k, v in ph.items()
                                   if isinstance(v, dict)}
        ca = overrides_raw.get("casts")
        if isinstance(ca, dict):
            overrides["casts"] = {str(k): list(v) for k, v in ca.items()
                                  if isinstance(v, list)}
    return {"schema_version": SCHEMA_VERSION, "name": name,
           "description": str(raw.get("description") or ""),
           "doc_slots": doc_slots, "pipeline_ref": pipeline_ref,
           "overrides": overrides}


def load_situation(name, orch_dir, on_error=None):
    """The named situation, normalized, or None on any read/parse/shape
    failure (reported via on_error with the file path and the problem —
    §6.2, never a silent fallback). Ensures the six seeds exist first (a
    fresh workspace's first read materializes them, workflows.ensure_seeded's
    contract)."""
    ensure_seeded(orch_dir, on_error=on_error)
    path = _situation_path(name, orch_dir)
    try:
        with open(path, encoding="utf-8") as fh:
            raw = json.load(fh)
    except OSError as exc:
        _warn(on_error, "situation %r: unreadable (%s)" % (name, exc))
        return None
    except ValueError as exc:
        _warn(on_error, "situation %r (%s): invalid JSON (%s)"
             % (name, path, exc))
        return None
    normalized = _normalize(raw, name_hint=name)
    if normalized is None:
        _warn(on_error, "situation %r (%s): root must be a JSON object"
             % (name, path))
        return None
    return normalized


def list_situations(orch_dir, on_error=None):
    """Every situation name with a LOADABLE situation.json, sorted. A corrupt
    entry is reported via on_error and excluded — it never blinds the whole
    library (§6.2: healthy situations still load)."""
    ensure_seeded(orch_dir, on_error=on_error)
    root = situations_root(orch_dir)
    try:
        names = sorted(n for n in os.listdir(root)
                       if os.path.isdir(os.path.join(root, n))
                       and not n.startswith(("_", ".")))
    except OSError:
        return []
    out = []
    for name in names:
        if load_situation(name, orch_dir, on_error=on_error) is not None:
            out.append(name)
    return out


# --------------------------------------------------------------------------- #
# V3 board 9.1: apply-situation resolution — pure, unit-testable without the
# engine. Deliberately DEFENSIVE, not validating: an unknown slot id here is
# silently dropped (never raises, never propagates into phase selection) —
# lint_situation is where an author gets TOLD about it; resolution's job is
# to never crash or gut a run on a situation that has drifted from doc_map.
# --------------------------------------------------------------------------- #
def resolve_required_slots(situation, doc_map):
    """(ordered_slots, owning_sections) for an already-loaded situation dict.
    ordered_slots preserves situation['doc_slots']' order, deduplicated, and
    dropping any id that doesn't exist in doc_map (a stale/renamed slot must
    not propagate into filtering — it just contributes nothing).
    owning_sections is the SET of doc_map owner_section values those slots
    map to (5.3's ownership), used to decide which sections' phases stay
    eligible. situation=None or a non-dict yields ([], set()) — the same
    'nothing required' shape callers already treat as no-op."""
    if not isinstance(situation, dict):
        return [], set()
    by_id = {s.get("slot_id"): s.get("owner_section")
            for s in (doc_map or {}).get("slots", [])
            if isinstance(s, dict) and isinstance(s.get("slot_id"), str)}
    ordered, owners, seen = [], set(), set()
    for slot in situation.get("doc_slots") or []:
        if slot in by_id and slot not in seen:
            ordered.append(slot)
            seen.add(slot)
            owner = by_id[slot]
            if owner:
                owners.add(owner)
    return ordered, owners


# --------------------------------------------------------------------------- #
# Lint — every problem names the offending field + value (§5.2)
# --------------------------------------------------------------------------- #
def lint_situation(situation, doc_map, presets=None, on_error=None):
    """[problem strings]. `doc_map` is doc_map.json's PARSED dict (the
    {"slots": [...]} shape 5.1/5.3 ship); `presets` is an iterable of known
    7.11 pipeline preset names (an empty/None presets means 'no presets
    exist yet' — a non-empty pipeline_ref is then reported as dangling,
    since it cannot resolve against nothing). Every problem names the exact
    field and value — never a generic 'invalid situation' (§5.2)."""
    problems = []
    if not isinstance(situation, dict):
        return ["situation is not an object"]
    known_slots = {s.get("slot_id") for s in (doc_map or {}).get("slots", [])
                   if isinstance(s, dict) and isinstance(s.get("slot_id"), str)}
    seen_slots = set()
    for slot in situation.get("doc_slots") or []:
        if slot not in known_slots:
            problems.append("doc_slots: unknown slot id %r" % (slot,))
        elif slot in seen_slots:
            problems.append("doc_slots: duplicate slot id %r" % (slot,))
        seen_slots.add(slot)

    ref = situation.get("pipeline_ref") or ""
    if ref:
        known_presets = set(presets or ())
        if ref not in known_presets:
            problems.append("pipeline_ref: dangling reference %r "
                            "(no such pipeline preset)" % (ref,))

    overrides = situation.get("overrides") or {}
    known_sections = {s.get("slot_id"): s.get("owner_section")
                      for s in (doc_map or {}).get("slots", [])
                      if isinstance(s, dict)}
    known_section_names = set(known_sections.values())
    for sec_name in (overrides.get("sections") or {}):
        if sec_name not in known_section_names:
            problems.append("overrides.sections: unknown section %r"
                            % (sec_name,))

    allowed_phase_fields = set(mrlib.PHASE_FIELDS) | set(_EXTRA_PHASE_FIELDS)
    for phase_key, fields in (overrides.get("phases") or {}).items():
        if not isinstance(fields, dict):
            problems.append("overrides.phases[%r]: must be an object"
                            % (phase_key,))
            continue
        for field in fields:
            if field not in allowed_phase_fields:
                problems.append("overrides.phases[%r]: unknown field %r"
                                % (phase_key, field))
    return problems


# --------------------------------------------------------------------------- #
# The six seeded defaults, generalizing completeness.PROFILES to doc slots.
# Every doc_slots entry below is a REAL slot id from
# sections/documentation/doc_map.json (verified by
# tests/test_situations.py's mechanical check against the shipped file, not
# by review — §23). pipeline_ref is '' for every seed: no 7.11 pipeline
# preset files exist in the repo yet (its GUI half, which creates them, is a
# separate card) — the schema explicitly allows '' (schema comment above),
# and a seed shipping a non-'' ref would dangle against nothing.
# --------------------------------------------------------------------------- #
_ALL_SLOT_IDS = (
    "problem_statement", "target_user", "value_proposition",
    "market_landscape", "competitor_analysis", "user_research",
    "technical_feasibility",
    "product_requirements", "scope_tiers", "feature_list", "task_graph",
    "success_metrics",
    "user_flows", "information_architecture", "design_language", "copy_voice",
    "technical_architecture", "data_model", "api_contracts", "build_plan",
    "dependency_map",
    "test_plan", "qa_report", "known_limitations", "security_review",
    "overview", "changelog", "glossary",
    "positioning", "pricing", "launch_plan", "marketing_channels",
    "privacy_policy", "terms_of_service", "app_store_compliance",
    "release_checklist", "runbook", "postmortem_template",
    "reusable_components", "lessons_learned",
)

_SEEDS = {
    "full_production_app": {
        "description": "Every documentation slot — the complete 40-slot "
                       "blueprint, no scope cut.",
        "doc_slots": list(_ALL_SLOT_IDS),
        "pipeline_ref": "",
        "overrides": _default_overrides(),
    },
    "prototype_sprint": {
        "description": "The smallest slice that proves the idea: what it "
                       "is, the core screens, and that it builds — mirrors "
                       "completeness.py's 'prototype' profile intent "
                       "generalized to doc slots.",
        "doc_slots": [
            "problem_statement", "target_user", "value_proposition",
            "product_requirements", "feature_list",
            "user_flows", "design_language",
            "technical_architecture", "build_plan",
            "test_plan",
        ],
        "pipeline_ref": "",
        "overrides": {"sections": {"gtm": {"enabled": False},
                                   "legal_compliance": {"enabled": False}},
                      "phases": {}, "casts": {}},
    },
    "research_spike": {
        "description": "Research-only: is this worth building, and what do "
                       "we not yet know.",
        "doc_slots": [
            "problem_statement",
            "market_landscape", "competitor_analysis", "user_research",
            "technical_feasibility",
        ],
        "pipeline_ref": "",
        "overrides": _default_overrides(),
    },
    "launch_push": {
        "description": "Everything a real ship needs beyond the build: "
                       "legal, marketing, and launch readiness.",
        "doc_slots": [
            "privacy_policy", "terms_of_service", "app_store_compliance",
            "positioning", "pricing", "launch_plan", "marketing_channels",
            "release_checklist",
        ],
        "pipeline_ref": "",
        "overrides": _default_overrides(),
    },
    "v2_iteration": {
        "description": "A second pass on a shipped app: what changed, "
                       "what's still open, and where to pick up.",
        "doc_slots": [
            "overview", "changelog", "known_limitations", "task_graph",
        ],
        "pipeline_ref": "",
        "overrides": _default_overrides(),
    },
    "compliance_pass": {
        "description": "A focused legal + adversarial security review of "
                       "an existing app.",
        "doc_slots": [
            "privacy_policy", "terms_of_service", "app_store_compliance",
            "security_review", "known_limitations", "qa_report",
        ],
        "pipeline_ref": "",
        "overrides": _default_overrides(),
    },
}


def ensure_seeded(orch_dir, on_error=None):
    """Materialize the six default situations on first use — never clobber
    an existing file, even an invalid one (disk wins, workflows.ensure_seeded's
    exact contract). Best-effort: an OSError here is reported, not raised."""
    root = situations_root(orch_dir)
    try:
        for name, body in _SEEDS.items():
            d = os.path.join(root, name)
            path = os.path.join(d, SITUATION_FILENAME)
            if os.path.exists(path):
                continue
            os.makedirs(d, exist_ok=True)
            doc = {"schema_version": SCHEMA_VERSION, "name": name,
                  "description": body["description"],
                  "doc_slots": body["doc_slots"],
                  "pipeline_ref": body["pipeline_ref"],
                  "overrides": body["overrides"]}
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(doc, fh, indent=2)
    except OSError as exc:
        _warn(on_error, "could not seed situations/ (%s)" % exc)
