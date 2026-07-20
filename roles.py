#!/usr/bin/env python3
"""
Roles + rotating personalities for the orchestrator.

Two orthogonal ideas:

  * ROLE  = the discipline / lens an agent argues from (Product, Design,
    Frontend, Backend, QA, Delivery, ...). Roles map onto the documentation
    disciplines the workspace already uses. Fully user-editable.

  * PERSONALITY = the temperament an agent argues WITH (Visionary, Skeptic,
    Pragmatist, ...). A pool that ROTATES every phase so no single agent is
    ever stuck to one personality — the whole point of the "each phase better,
    no AI sticks to one personality" requirement.

Each phase, every active agent is handed a (role, personality) pair. The pair
is deterministic (so a resumed run reproduces it) but rotates across phases and
differs between agents within a phase, which manufactures the productive
friction the debate needs.

Both pools live in ``roles.json`` next to this file so the GUI can add/remove/
edit them. If that file is missing we fall back to the built-in defaults below,
so the engine keeps working with zero config.
"""

import copy as _copy
import hashlib
import json
import math
import os

HERE = os.path.dirname(os.path.abspath(__file__))
ROLES_PATH = os.path.join(HERE, "roles.json")
AGENT_LIBRARY_FILENAME = "agent_library.json"
PRESETS_DIRNAME = "presets"
_SAMPLING_KEYS = ("temperature", "top_p", "top_k", "min_p",
                  "repeat_penalty", "seed")


def _warn(on_warn, message):
    if on_warn is not None:
        on_warn(message)


def _section_path(orch_dir, section):
    if not section:
        return None
    raw = os.fspath(section)
    if os.path.isabs(raw) or os.path.isdir(raw):
        return raw
    return os.path.join(orch_dir, "sections", raw)


def _backend_capabilities(backend):
    """Dependency-free mirror of the runner controls used for validation."""
    backend = str(backend or "").strip().lower()
    if backend in ("claude", "codex"):
        return {"known": True, "effort": True, "sampling": False}
    if backend in ("gemini", "ollama"):
        return {"known": True, "effort": False, "sampling": False}
    if backend.startswith("local:") and len(backend) > len("local:"):
        return {"known": True, "effort": True, "sampling": True}
    if backend.startswith("api:") and len(backend) > len("api:"):
        return {"known": True, "effort": False, "sampling": True}
    return {"known": False, "effort": False, "sampling": False}


def load_sampling_presets(orch_dir=None, section=None, on_warn=None):
    """Load fleet presets then overlay section presets by id."""
    orch_dir = os.path.abspath(orch_dir or HERE)
    layers = [os.path.join(orch_dir, PRESETS_DIRNAME)]
    section_dir = _section_path(orch_dir, section)
    if section_dir:
        layers.append(os.path.join(section_dir, PRESETS_DIRNAME))
    merged = {}
    for directory in layers:
        if not os.path.isdir(directory):
            continue
        try:
            names = sorted(n for n in os.listdir(directory) if n.endswith(".json"))
        except OSError as exc:
            _warn(on_warn, "preset library %s is unreadable (%s)" % (directory, exc))
            continue
        for name in names:
            path = os.path.join(directory, name)
            try:
                with open(path, encoding="utf-8") as fh:
                    raw = json.load(fh)
                if not isinstance(raw, dict):
                    raise ValueError("root must be an object")
                pid, label, params = raw.get("id"), raw.get("label"), raw.get("params")
                if not isinstance(pid, str) or not pid.strip():
                    raise ValueError("id must be a non-empty string")
                if not isinstance(label, str) or not label.strip():
                    raise ValueError("label must be a non-empty string")
                if not isinstance(params, dict):
                    raise ValueError("params must be an object")
                clean = {}
                for key, value in params.items():
                    if key not in _SAMPLING_KEYS:
                        _warn(on_warn, "%s: unknown sampling param %r ignored" % (path, key))
                        continue
                    if key in ("top_k", "seed"):
                        valid = isinstance(value, int) and not isinstance(value, bool)
                    else:
                        valid = (isinstance(value, (int, float))
                                 and not isinstance(value, bool)
                                 and math.isfinite(value))
                    if not valid:
                        _warn(on_warn, "%s: sampling param %s has the wrong type" % (path, key))
                        continue
                    clean[key] = value
                digest = hashlib.sha256(json.dumps(
                    clean, sort_keys=True, separators=(",", ":")
                ).encode("utf-8")).hexdigest()
                merged[pid.strip()] = {
                    "schema_version": raw.get("schema_version", 1),
                    "id": pid.strip(), "label": label.strip(),
                    "params": clean, "notes": str(raw.get("notes") or ""),
                    "params_hash": digest, "source_path": path,
                }
            except (OSError, ValueError) as exc:
                _warn(on_warn, "preset %s is invalid (%s)" % (path, exc))
    return _copy.deepcopy(merged)


def load_agent_library(orch_dir=None, section=None, on_warn=None):
    """Resolve fleet -> section personas and recommended-cast hints by id."""
    orch_dir = os.path.abspath(orch_dir or HERE)
    section_dir = _section_path(orch_dir, section)
    paths = [os.path.join(orch_dir, AGENT_LIBRARY_FILENAME)]
    if section_dir:
        paths.append(os.path.join(section_dir, AGENT_LIBRARY_FILENAME))
    personas, recommended = {}, {}
    for path in paths:
        if not os.path.exists(path):
            continue
        try:
            with open(path, encoding="utf-8") as fh:
                root = json.load(fh)
            if not isinstance(root, dict):
                raise ValueError("root must be an object")
        except (OSError, ValueError) as exc:
            _warn(on_warn, "agent library %s is invalid (%s)" % (path, exc))
            continue
        casts = root.get("recommended_casts")
        if isinstance(casts, dict):
            for key, value in casts.items():
                if isinstance(key, str) and isinstance(value, dict):
                    recommended[key] = _copy.deepcopy(value)
        raw_personas = root.get("personas")
        if not isinstance(raw_personas, list):
            _warn(on_warn, "%s: personas must be an array" % path)
            continue
        for index, raw in enumerate(raw_personas):
            where = "%s personas[%d]" % (path, index)
            if not isinstance(raw, dict):
                _warn(on_warn, "%s must be an object" % where)
                continue
            if any(not isinstance(raw.get(k), str) or not raw.get(k).strip()
                   for k in ("id", "name", "preamble")):
                _warn(on_warn, "%s requires non-empty id, name, and preamble" % where)
                continue
            persona = _copy.deepcopy(raw)
            persona["id"] = persona["id"].strip()
            backend = str(persona.get("backend") or "").strip()
            caps = _backend_capabilities(backend)
            if not caps["known"]:
                _warn(on_warn, "%s: unknown backend %r ignored" % (where, backend))
                for key in ("backend", "model", "default_effort", "preset"):
                    persona.pop(key, None)
            else:
                persona["backend"] = backend
                effort = persona.get("default_effort")
                if effort and not caps["effort"]:
                    _warn(on_warn, "%s: backend %r has no effort control; default_effort ignored"
                          % (where, backend))
                    persona.pop("default_effort", None)
                elif effort and effort not in ("low", "medium", "high", "xhigh", "max"):
                    _warn(on_warn, "%s: default_effort %r is unknown; ignored"
                          % (where, effort))
                    persona.pop("default_effort", None)
            persona["source_path"] = path
            personas[persona["id"]] = persona
    presets = load_sampling_presets(orch_dir, section_dir, on_warn)
    for pid, persona in personas.items():
        preset_id = persona.get("preset")
        if not preset_id:
            continue
        preset = presets.get(preset_id)
        caps = _backend_capabilities(persona.get("backend"))
        if preset is None:
            _warn(on_warn, "persona %r references missing preset %r; running preset-less"
                  % (pid, preset_id))
            persona.pop("preset", None)
        elif not caps["sampling"]:
            _warn(on_warn, "persona %r backend %r lacks sampling_params; preset %r ignored"
                  % (pid, persona.get("backend"), preset_id))
            persona.pop("preset", None)
        else:
            persona["preset_params"] = _copy.deepcopy(preset["params"])
            persona["preset_params_hash"] = preset["params_hash"]
    return {"schema_version": 1,
            "personas": [_copy.deepcopy(personas[k]) for k in personas],
            "personas_by_id": _copy.deepcopy(personas),
            "recommended_casts": _copy.deepcopy(recommended),
            "presets": presets}


def _library_role(persona, role_id=None):
    return {"id": role_id or persona["id"], "name": persona["name"],
            "focus": persona["preamble"],
            "library_persona": _copy.deepcopy(persona)}


# ---------------------------------------------------------------------------
# Built-in defaults (used when roles.json is absent). These mirror the eleven
# documentation disciplines used across the workspace, collapsed to the ones
# that actually change how an agent argues in a discussion.
# ---------------------------------------------------------------------------
DEFAULT_PERSONALITIES = [
    {"id": "visionary", "name": "the Visionary",
     "style": "push the boldest version of the idea and ask what would make this "
              "genuinely remarkable, not just adequate"},
    {"id": "skeptic", "name": "the Skeptic",
     "style": "stress-test every claim, hunt for what quietly breaks, and play "
              "devil's advocate even against a popular idea"},
    {"id": "pragmatist", "name": "the Pragmatist",
     "style": "fixate on the smallest thing that actually ships and on the real "
              "constraints (time, effort, platform limits)"},
    {"id": "user_advocate", "name": "the User Advocate",
     "style": "keep a specific real user in the room and reject anything "
              "confusing, slow, or joyless for them"},
    {"id": "systems_thinker", "name": "the Systems Thinker",
     "style": "care about how the pieces fit, the edge cases, and the "
              "second-order consequences nobody mentioned"},
    {"id": "closer", "name": "the Closer",
     "style": "drive hard toward one concrete decision and refuse to leave "
              "loops open or punt to 'later'"},
]

DEFAULT_ROLES = [
    {"id": "product", "name": "Product Strategist",
     "focus": "the core problem, who it's for, user value, scope boundaries, and "
              "measurable success"},
    {"id": "design", "name": "Design Lead",
     "focus": "UX flows, information architecture, the key screens and states, and "
              "how the thing feels to use"},
    {"id": "frontend", "name": "Frontend Engineer",
     "focus": "client architecture, state management, navigation, offline behavior, "
              "and performance"},
    {"id": "backend", "name": "Backend / Systems Engineer",
     "focus": "data model, APIs, storage, auth, and the non-functional requirements"},
    {"id": "qa", "name": "QA & Risk",
     "focus": "edge cases, failure modes, what could go wrong, and how you'd know "
              "it works"},
    {"id": "delivery", "name": "Delivery Lead",
     "focus": "sequencing, dependencies, who owns what, and a realistic path to done"},
    {"id": "security", "name": "Security Engineer",
     "focus": "vulnerabilities and abuse: secrets in code, injection, authn/authz, "
              "insecure storage and transport, unsafe deserialization, over-broad "
              "permissions, dependency CVEs, and the concrete exploit path for each — "
              "always tied to a real file:line"},
]


def _valid_pool(obj, keys):
    return (isinstance(obj, list) and obj
            and all(isinstance(x, dict) and all(k in x for k in keys) for x in obj))


def load_roles(orch_dir=None):
    """Return (personalities, roles). Reads roles.json if present and valid;
    otherwise the built-in defaults. Never raises.

    Always returns fresh copies, never the module-level DEFAULT_* lists by
    reference: a caller that mutates its result in place (e.g. filtering,
    `assign_personas`'s empty-pool fallback) would otherwise corrupt the
    built-in defaults for the rest of the process, including every other
    concurrent project that later falls back to them."""
    path = os.path.join(orch_dir, "roles.json") if orch_dir else ROLES_PATH
    personalities, roles = DEFAULT_PERSONALITIES, DEFAULT_ROLES
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        p = data.get("personalities")
        r = data.get("roles")
        # 'id' is required: role ids are referenced by workflow phases and by the
        # GUI, so a pool missing them is treated as invalid (falls back to default).
        if _valid_pool(p, ("id", "name", "style")):
            personalities = p
        if _valid_pool(r, ("id", "name", "focus")):
            roles = r
    except (OSError, ValueError):
        pass
    return _copy.deepcopy(personalities), _copy.deepcopy(roles)


def load_roles_layered(orch_dir=None, section_dir=None, on_fallback=None):
    """Section-first role resolution (V3 board 3.4): a PRESENT section
    roles.json supplies pools with WHOLE-POOL precedence — a valid section
    pool replaces (never merges with) the next layer's; an invalid pool in
    a present file falls through WITH one on_fallback(path, error) call;
    an absent section file is silent. Deep-copy semantics preserved: the
    result is always fresh, never a module default by reference."""
    base_p, base_r = load_roles(orch_dir)

    def with_library(personalities, role_pool):
        warnings = []
        library = load_agent_library(orch_dir or HERE, section_dir,
                                     on_warn=warnings.append)
        for warning in warnings:
            if on_fallback is not None:
                on_fallback(os.path.join(section_dir or orch_dir or HERE,
                                         AGENT_LIBRARY_FILENAME), warning)
        personas = library["personas"]
        if section_dir:
            section_library_path = os.path.join(section_dir,
                                                AGENT_LIBRARY_FILENAME)
            # Preserve 3.4's whole-pool contract: fleet personas remain
            # resolvable at fleet scope but are not appended to every section's
            # explicit roles pool. A section opts in by defining/shadowing the
            # persona in its own library file.
            personas = [persona for persona in personas
                        if persona.get("source_path") == section_library_path]
        if not personas:
            return personalities, role_pool
        by_id = {role.get("id"): role for role in role_pool}
        for persona in personas:
            by_id[persona["id"]] = _library_role(persona)
        return personalities, list(by_id.values())

    if not section_dir:
        return with_library(base_p, base_r)
    path = os.path.join(section_dir, "roles.json")
    if not os.path.exists(path):
        return with_library(base_p, base_r)
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            raise ValueError("roles.json must be a JSON object")
    except (OSError, ValueError) as exc:
        if on_fallback is not None:
            on_fallback(path, exc)
        return with_library(base_p, base_r)
    p = data.get("personalities")
    r = data.get("roles")
    bad = []
    if p is not None and not _valid_pool(p, ("id", "name", "style")):
        bad.append("personalities")
        p = None
    if r is not None and not _valid_pool(r, ("id", "name", "focus")):
        bad.append("roles")
        r = None
    if bad and on_fallback is not None:
        on_fallback(path, "invalid pool(s): %s — falling through per pool"
                    % ", ".join(bad))
    return with_library(_copy.deepcopy(p) if p is not None else base_p,
                        _copy.deepcopy(r) if r is not None else base_r)


def resolve_phase_role_refs(phase_role_ids, sections_root, on_missing=None):
    """Cross-section cast references (V3 board 3.5): a phase's roles list
    may name "section:id", resolved against THAT section's own roles.json
    — its roles pool first, then its personalities pool (a personality hit
    joins as a role-shaped guest {id, name, focus:=style}; deterministic
    lookup order). Returns (kept_ids, guest_dicts): kept_ids is the input
    minus dangling refs (plain ids pass through byte-identically); guests
    carry the FULL ref as their id so a local id can never collide.

    A dangling ref — unknown section, no roles.json, or unknown id —
    calls on_missing(ref, reason) exactly once and is DROPPED: the run
    continues, and no placeholder cast member is ever invented (the
    fail-open silent drop this milestone exists to kill). Guests are
    deepcopied; resolving one never mutates the source section's pools."""
    if not phase_role_ids:
        return phase_role_ids, []
    kept, guests = [], []
    for rid in phase_role_ids:
        if not isinstance(rid, str) or ":" not in rid:
            kept.append(rid)
            continue
        section, _sep, target = rid.partition(":")
        path = os.path.join(sections_root, section, "roles.json")
        library_path = os.path.join(sections_root, section,
                                    AGENT_LIBRARY_FILENAME)
        reason = None
        if not section or not target:
            reason = "malformed reference"
        elif not os.path.isdir(os.path.join(sections_root, section)):
            reason = "unknown section %r" % section
        elif not os.path.exists(path) and not os.path.exists(library_path):
            reason = "section %r has no roles.json or agent_library.json" % section
        if reason is None and os.path.exists(library_path):
            warnings = []
            library = load_agent_library(os.path.dirname(sections_root),
                                         os.path.join(sections_root, section),
                                         on_warn=warnings.append)
            persona = library["personas_by_id"].get(target)
            if persona is not None:
                guests.append(_library_role(persona, rid))
                kept.append(rid)
                continue
            if warnings and not os.path.exists(path):
                reason = "; ".join(warnings)
        if reason is None:
            try:
                with open(path, encoding="utf-8") as fh:
                    data = json.load(fh)
                if not isinstance(data, dict):
                    raise ValueError("roles.json must be a JSON object")
            except (OSError, ValueError) as exc:
                reason = "unreadable roles.json (%s)" % exc
        if reason is None:
            r = data.get("roles")
            p = data.get("personalities")
            hit = None
            if _valid_pool(r, ("id", "name", "focus")):
                hit = next((e for e in r if e.get("id") == target), None)
            if hit is None and _valid_pool(p, ("id", "name", "style")):
                pe = next((e for e in p if e.get("id") == target), None)
                if pe is not None:
                    hit = {"id": target, "name": pe["name"],
                           "focus": pe.get("style", "")}
            if hit is None:
                reason = "no role or personality %r in section %r" \
                    % (target, section)
            else:
                guest = _copy.deepcopy(hit)
                guest["id"] = rid
                guests.append(guest)
                kept.append(rid)
                continue
        if on_missing is not None:
            on_missing(rid, reason)
    return kept, guests


def load_agent_role_overrides_layered(orch_dir=None, section_dir=None):
    """agent_role_overrides with the section file winning when IT is the
    source of any override — per-agent precedence, section over engine."""
    base = load_agent_role_overrides(orch_dir)
    if not section_dir:
        return base
    path = os.path.join(section_dir, "roles.json")
    if not os.path.exists(path):
        return base
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        raw = data.get("agent_role_overrides") or {}
        if not isinstance(raw, dict):
            return base
        merged = dict(base)
        for agent, role in raw.items():
            if isinstance(agent, str) and isinstance(role, str) and role:
                merged[agent] = role
        return merged
    except (OSError, ValueError):
        return base


def load_agent_role_overrides(orch_dir=None):
    """Return {agent_id: role_id} from roles.json. Invalid/missing values simply
    disappear so callers can treat the result as optional user preference."""
    path = os.path.join(orch_dir, "roles.json") if orch_dir else ROLES_PATH
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        raw = data.get("agent_role_overrides") or {}
        if not isinstance(raw, dict):
            return {}
        return {str(k): str(v) for k, v in raw.items()
                if str(k).strip() and str(v).strip()}
    except (OSError, ValueError):
        return {}


def _role_pool_for_phase(roles, phase_role_ids):
    """If a phase names a subset of role ids, restrict to those (in the phase's
    order); otherwise use the legacy role roster. Library personas are opt-in
    cast data and must never silently join every phase merely because the
    shipped fleet library exists."""
    if phase_role_ids:
        by_id = {r.get("id"): r for r in roles}
        picked = [by_id[i] for i in phase_role_ids if i in by_id]
        if picked:
            return picked
    legacy = [r for r in roles if not r.get("library_persona")]
    return legacy or roles


def assign_personas(phase_index, agents, personalities, roles, phase_role_ids=None,
                    agent_role_overrides=None, role_by_id=None,
                    agent_library=None):
    """Deterministically hand each agent a (role, personality) for this phase.

    Rotation guarantees:
      * within a phase, agents get different personalities (agent index shifts),
      * across phases, a given agent's personality changes (phase index shifts),
    so nobody is ever locked to one personality.

    ``role_by_id`` lets a caller that invokes this once per phase (every phase
    of a run, same ``roles`` list) precompute the full-roster id lookup once
    per run instead of rebuilding it on every call; omitted, it's built here
    same as before.

    Returns {agent: {"role": <role dict>, "personality": <personality dict>}}.
    """
    role_pool = _role_pool_for_phase(roles, phase_role_ids)
    # Guard the modulo below against empty pools (a caller passing gutted
    # roles/personalities would otherwise raise ZeroDivisionError). Fall back to
    # the built-in defaults so every agent still gets a (role, personality).
    if not personalities:
        personalities = DEFAULT_PERSONALITIES
    if not role_pool:
        role_pool = DEFAULT_ROLES
    # agent_role_overrides is looked up against the FULL roster, not the
    # phase-restricted role_pool: an override is a deliberate per-agent admin
    # choice (roles.json / GUI "Configure -> Sub-agents") and is meant to win
    # over a phase's generic role restriction — see
    # test_agent_role_overrides_drive_personas, which pins this intentionally.
    by_role_id = role_by_id if role_by_id is not None else {r.get("id"): r for r in roles}
    overrides = agent_role_overrides or {}
    out = {}
    for j, agent in enumerate(agents):
        pi = (phase_index + j) % len(personalities)
        ri = (phase_index + j) % len(role_pool)
        role = by_role_id.get(overrides.get(agent)) or role_pool[ri]
        assigned = {"personality": personalities[pi], "role": role}
        library_persona = role.get("library_persona") if isinstance(role, dict) else None
        if library_persona is None and isinstance(agent_library, dict):
            library_persona = (agent_library.get("personas_by_id") or {}).get(
                role.get("id") if isinstance(role, dict) else None)
        if isinstance(library_persona, dict):
            assigned["library_persona"] = _copy.deepcopy(library_persona)
            assigned["binding"] = {k: _copy.deepcopy(library_persona[k])
                                   for k in ("backend", "model", "default_effort",
                                             "preset", "preset_params",
                                             "preset_params_hash")
                                   if k in library_persona}
        out[agent] = assigned
    return out


def persona_preamble(persona):
    """One natural sentence telling an agent which hat to wear this phase. Kept
    conversational so it fits the 'talk like a person, no headings' house style."""
    if not persona:
        return ""
    library = persona.get("library_persona") or {}
    if library:
        return ("Come at this as %s. %s This is your stance for THIS phase "
                "only — lean into it and don't be a yes-man."
                % (library.get("name", "your assigned persona"),
                   library.get("preamble", ""))).strip()
    role = persona.get("role") or {}
    pers = persona.get("personality") or {}
    bits = []
    if role:
        bits.append("Come at this as a %s — your lens is %s."
                    % (role.get("name", "generalist"), role.get("focus", "the whole picture")))
    if pers:
        bits.append("And bring the temperament of %s: %s."
                    % (pers.get("name", "yourself"), pers.get("style", "argue honestly")))
    if bits:
        bits.append("This is your stance for THIS phase only — lean into it and "
                    "don't be a yes-man.")
    return " ".join(bits)


def persona_label(persona):
    """Short 'Role · Personality' tag for transcripts/logs."""
    if not persona:
        return ""
    role = (persona.get("role") or {}).get("name", "")
    pers = (persona.get("personality") or {}).get("name", "")
    pers = pers.replace("the ", "").strip().title()
    if role and pers:
        return "%s · %s" % (role, pers)
    return role or pers
