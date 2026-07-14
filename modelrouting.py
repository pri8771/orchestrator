#!/usr/bin/env python3
"""
Per-phase model routing + cloud->local fallback (model_routing.json).

Routes expensive frontier models to the phases that need them (build,
verification, architecture) and cheap or fully-local models to the chatty
discussion phases, so subscription tokens are spent only where they buy
quality. One editable JSON file next to the engine — same pattern as
phase_rules.json / local_models.json — read here, edited by the GUI's
Settings -> Routing tab.

Shape (schema_version 1):

{
  "schema_version": 1,
  "enabled": true,
  "fallback": {"cloud_to_local": true, "local_model": ""},
  "phases": {
    "initial_discussion": {
      "claude": "claude-haiku-4-5",        # claude -p --model override
      "codex": "gpt-5.3-codex-spark",      # codex exec --model override
      "codex_reasoning": "low",            # codex -c model_reasoning_effort=...
      "claude_reasoning": "high",          # claude -p --effort ...
      "gemini_reasoning": "low",           # ACCEPTED BUT IGNORED (see below)
      "ollama_reasoning": "medium",        # HTTP path only (see below)
      "ollama": "qwen2.5-coder:7b",        # local model override
      "agents": "cloud",                   # participant filter, see below
      "roles": {                           # per-ROLE effort within one phase
        "worker": {"codex_reasoning": "low"},
        "integrator": {"claude_reasoning": "high"}
      }
    }
  }
}

"agents" restricts who speaks in a phase: a comma list of agent ids
("claude,codex"), local model tags, or the groups "cloud" / "local".
A filter that would empty the roster is ignored (fail-open).

"roles" applies extra overrides per build role within the phase: "worker"
patches every parallel build lane's per-lane cfg, "integrator" patches only
the integrator's turn — the cheap-workers/expensive-integrator split. Inner
keys are validated against PHASE_FIELDS; unknown role names are ignored with
a warning.

Reasoning-effort parity note (evidence-based): the codex CLI
(`-c model_reasoning_effort=...`) and claude CLI (`--effort ...`) expose an
effort control in how this engine invokes them; so does Ollama's
/api/generate HTTP endpoint, via its documented boolean "think" field for
reasoning-capable models (deepseek-r1, qwen3, ...). "ollama_reasoning" maps
onto that field (medium/high/max -> think=true; unset/low -> omitted) but
ONLY on the HTTP path — orchestrator.run_local, used by dynamic
local:<model> agents. The gemini CLI (`gemini -p ...`) and the roster
"ollama" agent's plain `ollama run <model>` CLI invocation (orchestrator.
run_ollama) carry no effort/thinking flag as invoked here, so
"gemini_reasoning" is ACCEPTED (valid schema, GUI can round-trip it) but
IGNORED at runtime with a logged warning rather than inventing a flag the
CLI may reject, and "ollama_reasoning" is honored on the HTTP path but still
noop (also warned) on the CLI roster path.

Every reader is best-effort: a missing/corrupt file or unknown phase key
degrades to "no overrides" and can never take down a run.
"""

import copy as _copy
import json
import os
import re

ROUTING_FILENAME = "model_routing.json"

# Per-phase override fields the engine honors (anything else is dropped).
# gemini_reasoning is accepted-but-noop (the gemini CLI exposes no effort
# control as invoked here). ollama_reasoning is honored on the HTTP
# local:<model> path (run_local's "think" field) and noop on the CLI roster
# path (run_ollama) — see module docstring.
PHASE_FIELDS = ("claude", "claude_reasoning", "codex", "codex_reasoning",
                "gemini", "gemini_reasoning", "ollama", "ollama_reasoning",
                "agents")

# Role names honored inside a phase's "roles" sub-dict.
ROLE_NAMES = ("worker", "integrator")

# Model ids / filters reach subprocess argv lists directly (never a shell),
# but keep the charset tidy anyway so a corrupt file can't smuggle newlines
# or control characters into commands and logs.
_SAFE_VALUE = re.compile(r"^[A-Za-z0-9 ,;._:/-]+$")

_DEFAULT = {
    "schema_version": 1,
    "enabled": True,
    # chains: per-agent ordered rescue steps, tried before the local net.
    # Each step is either a same-provider model id ("gpt-5.3-codex-spark" —
    # rate/5-hour caps are usually per model tier, so a sibling model often
    # still works) or a local tag ("local:qwen3-coder:30b").
    "fallback": {"cloud_to_local": True, "local_model": "", "chains": {}},
    "phases": {},
}

_CHAIN_AGENTS = ("codex", "claude", "gemini")
_MAX_CHAIN_STEPS = 5


def _as_bool(value, default):
    """bool() coercion that doesn't treat the string "false" as truthy — hand-
    edited model_routing.json commonly has JSON-string booleans."""
    if isinstance(value, str):
        v = value.strip().lower()
        if v in ("false", "no", "0", ""):
            return False
        if v in ("true", "yes", "1"):
            return True
        return default
    return bool(value)


def default_routing():
    return json.loads(json.dumps(_DEFAULT))   # deep copy


def load_routing(here, on_warn=None):
    """model_routing.json as a validated dict. A missing, unreadable, or
    malformed file NEVER raises — it yields the default (routing on, fallback
    on, no per-phase overrides) so callers need no special-casing.
    ``on_warn(msg)`` (optional) surfaces dropped-but-intentional-looking
    content, e.g. an unknown role name in a phase's "roles" sub-dict."""
    out = default_routing()
    path = os.path.join(here, ROUTING_FILENAME)
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return out
    if not isinstance(data, dict):
        return out
    out["enabled"] = _as_bool(data.get("enabled", True), True)
    fb = data.get("fallback")
    if isinstance(fb, dict):
        out["fallback"]["cloud_to_local"] = _as_bool(fb.get("cloud_to_local", True), True)
        model = fb.get("local_model", "")
        if isinstance(model, str) and _SAFE_VALUE.match(model.strip() or "x"):
            out["fallback"]["local_model"] = model.strip()
        chains = fb.get("chains")
        if isinstance(chains, dict):
            for agent, steps in chains.items():
                if agent not in _CHAIN_AGENTS or not isinstance(steps, list):
                    continue
                clean_steps = []
                for s in steps:
                    if isinstance(s, str) and s.strip() and _SAFE_VALUE.match(s.strip()):
                        clean_steps.append(s.strip())
                if clean_steps:
                    out["fallback"]["chains"][agent] = clean_steps[:_MAX_CHAIN_STEPS]
    phases = data.get("phases")
    if isinstance(phases, dict):
        for key, ov in phases.items():
            if not isinstance(key, str) or not isinstance(ov, dict):
                continue
            clean = {}
            for field in PHASE_FIELDS:
                val = ov.get(field, "")
                if isinstance(val, str):
                    val = val.strip()
                    if val and _SAFE_VALUE.match(val):
                        clean[field] = val
            # Per-phase turn timeout (seconds) — lets slow/strong models think
            # longer on the phases that deserve it.
            try:
                t = int(ov.get("timeout", 0) or 0)
            except (TypeError, ValueError):
                t = 0
            if 0 < t <= 7200:
                clean["timeout"] = t
            # Per-role overrides within the phase (worker lanes vs integrator).
            roles = ov.get("roles")
            if isinstance(roles, dict):
                clean_roles = {}
                for role, rov in roles.items():
                    if role not in ROLE_NAMES:
                        if on_warn:
                            on_warn("model_routing phase %r: unknown role %r in "
                                    "\"roles\" ignored (expected one of: %s)"
                                    % (key, role, ", ".join(ROLE_NAMES)))
                        continue
                    if not isinstance(rov, dict):
                        continue
                    rclean = {}
                    for field in PHASE_FIELDS:
                        val = rov.get(field, "")
                        if isinstance(val, str):
                            val = val.strip()
                            if val and _SAFE_VALUE.match(val):
                                rclean[field] = val
                    if rclean:
                        clean_roles[role] = rclean
                if clean_roles:
                    clean["roles"] = clean_roles
            # Per-phase rounds override: how long this phase may debate.
            # 0 = unlimited (run until natural consensus, no forced vote) —
            # the same contract as a workflow phase's rounds <= 0.
            if "rounds" in ov:
                try:
                    r = int(ov.get("rounds"))
                except (TypeError, ValueError):
                    r = None
                if r is not None and 0 <= r <= 99:
                    clean["rounds"] = r
            # Per-phase free-text instructions, spliced into every turn's
            # context for the phase. Control characters (except newline/tab)
            # are stripped and length is capped so a runaway paste can't
            # balloon every prompt.
            instr = ov.get("instructions")
            if isinstance(instr, str) and instr.strip():
                instr = "".join(ch for ch in instr
                                if ch in "\n\t" or ord(ch) >= 32)
                clean["instructions"] = instr.strip()[:4000]
            if clean:
                out["phases"][key.strip()] = clean
    return out


def load_routing_for_app(here, app_dir, on_warn=None):
    """Fleet routing (model_routing.json next to the engine) with the app's
    own model_routing.json phase overrides layered on top — the engine side
    of the GUI's per-project Plan tab (DESIGN-NATIVE-PRO.md #5.7).

    Mirrors the GUI's RoutingMatrix.cell resolution exactly: per phase, per
    field, a non-empty value in the app's file wins over the fleet's; unset
    fields fall through to the fleet value.

    Fallback overrides (the Plan tab's per-project ladders): a NON-EMPTY
    project chain for an agent wins over the fleet chain, and a non-empty
    project local_model wins — empty/absent entries inherit. "enabled" and
    "cloud_to_local" stay fleet-owned: the project file round-trips harmless
    defaults for those (ModelRouting.save always writes enabled=true,
    cloud_to_local=true) that must not shadow a deliberately different fleet
    setting.

    ``on_warn(msg)`` (optional) is called when the project's file has no
    per-phase overrides AND no fallback-chain overrides but DOES carry a
    non-default enabled/fallback value — those are silently ignored by
    design (see above), which used to be indistinguishable from "the file
    is a no-op"; surfaced instead of hidden."""
    fleet = load_routing(here, on_warn=on_warn)
    if not app_dir or os.path.abspath(app_dir) == os.path.abspath(here):
        return fleet
    if not os.path.exists(os.path.join(app_dir, ROUTING_FILENAME)):
        return fleet
    project = load_routing(app_dir, on_warn=on_warn)
    proj_fb = project.get("fallback") or {}
    if not project["phases"] and not proj_fb.get("chains") \
            and not proj_fb.get("local_model"):
        if on_warn and (not project.get("enabled", True)
                        or project.get("fallback") != _DEFAULT["fallback"]):
            on_warn("Project routing file %s has no phase overrides — its "
                    "enabled/fallback settings are ignored (those are fleet-scoped "
                    "only)." % os.path.join(app_dir, ROUTING_FILENAME))
        return fleet
    out = dict(fleet)
    # dict(fleet) is shallow: without this, out["fallback"]/out["chains"] alias
    # the fleet's nested dicts, so a downstream mutation of the per-app view would
    # bleed into the fleet view.
    out["fallback"] = _copy.deepcopy(fleet.get("fallback"))
    out["chains"] = _copy.deepcopy(fleet.get("chains"))
    merged_phases = {key: dict(ov) for key, ov in fleet["phases"].items()}
    for key, ov in project["phases"].items():
        merged_phases[key] = dict(merged_phases.get(key, {}), **ov)
    out["phases"] = merged_phases
    if proj_fb.get("chains") or proj_fb.get("local_model"):
        fb = dict(out.get("fallback") or {})
        chains = dict(fb.get("chains") or {})
        for agent, steps in (proj_fb.get("chains") or {}).items():
            if steps:
                chains[agent] = list(steps)
        fb["chains"] = chains
        if proj_fb.get("local_model"):
            fb["local_model"] = proj_fb["local_model"]
        out["fallback"] = fb
    return out


def overrides_for(routing, phase_key):
    """The validated override dict for one phase ({} when routing is disabled
    or the phase has no entry)."""
    if not isinstance(routing, dict) or not routing.get("enabled", True):
        return {}
    ov = (routing.get("phases") or {}).get(str(phase_key or ""))
    return dict(ov) if isinstance(ov, dict) else {}


def _is_local(agent):
    return agent == "ollama" or str(agent).startswith("local:")


def filter_agents(routing, phase_key, active):
    """Apply a phase's "agents" participant filter to the active roster.

    Returns (kept_roster, note). The note is a human line for the run log
    ("" when nothing changed). Fail-open: a filter that matches nobody is
    ignored so a typo can never strand a phase with zero speakers."""
    spec = overrides_for(routing, phase_key).get("agents", "")
    active = list(active)
    if not spec:
        return active, ""
    wanted = [t.strip().lower() for t in re.split(r"[;,]", spec) if t.strip()]

    def keep(agent):
        a = str(agent).lower()
        local = _is_local(agent)
        for t in wanted:
            if t == "cloud" and not local:
                return True
            if t == "local" and local:
                return True
            if t == a or (local and a == "local:" + t):
                return True
        return False

    kept = [a for a in active if keep(a)]
    if not kept:
        return active, ("Phase '%s': routing agent filter '%s' matched no enabled "
                        "agent — ignoring it." % (phase_key, spec))
    if kept != active:
        return kept, ("Phase '%s': routing restricts participants to %s."
                      % (phase_key, ", ".join(kept)))
    return kept, ""


def fallback_chain(routing, agent):
    """The configured ordered rescue steps for one cloud agent ([] when none)."""
    if not isinstance(routing, dict) or not routing.get("enabled", True):
        return []
    chains = (routing.get("fallback") or {}).get("chains") or {}
    steps = chains.get(agent)
    return list(steps) if isinstance(steps, list) else []


def fallback_enabled(routing):
    if not isinstance(routing, dict) or not routing.get("enabled", True):
        return False
    fb = routing.get("fallback") or {}
    return bool(fb.get("cloud_to_local", True))


def fallback_model(routing, configured, roster, installed):
    """Resolve which INSTALLED local model rescues a failed cloud turn.

    Priority: the explicit fallback.local_model -> the configured default
    (models.ollama) -> the first installed roster entry -> the first installed
    model at all (any open model beats a dead turn). "" = no fallback possible."""
    installed = list(installed or [])
    if not installed:
        return ""
    inst = set(installed)
    explicit = str((routing.get("fallback") or {}).get("local_model", "") or "").strip() \
        if isinstance(routing, dict) else ""
    for candidate in [explicit, str(configured or "").strip()]:
        if candidate and candidate in inst:
            return candidate
    for candidate in roster or []:
        if candidate in inst:
            return candidate
    return installed[0]


def summary(routing):
    """Compact block for --doctor --json / the GUI: what routing is active."""
    routing = routing if isinstance(routing, dict) else default_routing()
    fb = routing.get("fallback") or {}
    return {
        "enabled": bool(routing.get("enabled", True)),
        "fallback_cloud_to_local": bool(fb.get("cloud_to_local", True)),
        "fallback_local_model": str(fb.get("local_model", "") or ""),
        "fallback_chains": dict(fb.get("chains") or {}),
        "phase_overrides": {k: sorted(v) for k, v in
                            (routing.get("phases") or {}).items()},
    }
