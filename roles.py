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
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
ROLES_PATH = os.path.join(HERE, "roles.json")


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
    if not section_dir:
        return base_p, base_r
    path = os.path.join(section_dir, "roles.json")
    if not os.path.exists(path):
        return base_p, base_r
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            raise ValueError("roles.json must be a JSON object")
    except (OSError, ValueError) as exc:
        if on_fallback is not None:
            on_fallback(path, exc)
        return base_p, base_r
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
    return (_copy.deepcopy(p) if p is not None else base_p,
            _copy.deepcopy(r) if r is not None else base_r)


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
    order); otherwise use the whole roster."""
    if phase_role_ids:
        by_id = {r.get("id"): r for r in roles}
        picked = [by_id[i] for i in phase_role_ids if i in by_id]
        if picked:
            return picked
    return roles


def assign_personas(phase_index, agents, personalities, roles, phase_role_ids=None,
                    agent_role_overrides=None, role_by_id=None):
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
        out[agent] = {"personality": personalities[pi], "role": role}
    return out


def persona_preamble(persona):
    """One natural sentence telling an agent which hat to wear this phase. Kept
    conversational so it fits the 'talk like a person, no headings' house style."""
    if not persona:
        return ""
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
