"""TurnContext — the typed view over the engine's per-phase cfg state.

V3 board 2.3(b). The engine threads run/phase/turn state through underscore
keys on a shared cfg dict; every `dict(cfg)` shallow copy, every band-D
identity-shared map (_agent_health, the session maps), and every frozen
fixture depends on that dict staying the transport. So this module does NOT
replace the dict — it is a zero-cost VIEW constructed on demand
(`ctx = TurnContext(cfg)`) whose properties read and write the same
underscore keys in the same dict. What it adds is structure: one module
documents each key's lifecycle band, lifecycle methods make set-or-clear
discipline structural (`end_phase()`), and the inventory gate
(tests/test_cfg_key_inventory.py) ratchets raw `cfg["_…"]` writes out of the
engine files as tranches land here.

Rules this module must never break:

- Getters use `.get` semantics — a missing key reads as None, never
  KeyError (the read-modify-write idioms like
  `ctx.target_digest or build_target_digest(...)` depend on it).
- Constructing or reading a view must not seed keys into the dict —
  `"_base_models" not in cfg` style containment latches would re-trigger.
- No getter may return a copy of a mapping. Band-D maps are shared BY
  IDENTITY across thread copies; a copying getter silently breaks cooldown
  and session sharing. (Band D is out of scope for tranche 1 for exactly
  this reason.)

Tranche 1 covered band B: the eleven per-phase keys process_phase sets on
entry and clears on exit. Tranche c1 adds band C (per-thread-copy state —
thread_copy() and the session/model-override protocols) and band B′ (the
per-phase-copy keys _apply_phase_routing writes). Band D
(shared-by-identity maps) comes in tranche c2 with its own review.
"""


def _prop(key, doc):
    def fget(self):
        return self._cfg.get(key)

    def fset(self, value):
        self._cfg[key] = value

    return property(fget, fset, None, doc)


class TurnContext(object):
    """View over one cfg dict. Cheap to construct; holds no state of its
    own. Two views over the same dict see each other's writes — that is
    the point, not an accident."""

    __slots__ = ("_cfg",)

    def __init__(self, cfg):
        self._cfg = cfg

    # ---- band B: per-phase, set by process_phase on entry, cleared by
    # end_phase() on exit (or the deliberate conversational subset). ----
    turn_timeout = _prop(
        "_turn_timeout",
        "Per-turn wall-clock cap (seconds) or None; sprint budgets tighten "
        "it, routing can override it. Also reset once per run.")
    verify_context = _prop(
        "_verify_context",
        "Structured verification-results block injected into turns of a "
        "requires_verification phase; '' otherwise. Set-or-CLEAR each phase.")
    phase_exemplar = _prop(
        "_phase_exemplar",
        "Fleet-learning few-shot exemplar for this phase key, if exported.")
    phase_playbook = _prop(
        "_phase_playbook",
        "Rendered per-phase playbook (phase_rules.json) or ''.")
    knowledge = _prop(
        "_knowledge",
        "Retrieved domain-knowledge block for this phase or ''.")
    allow_writes = _prop(
        "_allow_writes",
        "True only during a build/verify-repair phase with code changes "
        "enabled — the single switch the write-guard trusts.")
    build_dir = _prop(
        "_build_dir",
        "Persistent build folder when allow_writes, else None. (The build "
        "lane's per-thread override at its call site is band C, not here.)")
    session_cwd = _prop(
        "_session_cwd",
        "Stable per-app cwd for resumed claude sessions in read-only "
        "phases; None during builds.")
    prior_disc_cap = _prop(
        "_prior_disc_cap",
        "Char cap for the planning-transcript payload in build turns; "
        "None outside builds.")
    target_digest = _prop(
        "_target_digest",
        "Read-only digest of the audit target (or portfolio); '' when the "
        "phase does not read a target. Memoized via `ctx.target_digest or "
        "build_…` — getter must return None/'' falsy on absence.")
    read_dir = _prop(
        "_read_dir",
        "Live read-only cwd for audit phases when enabled, else None.")

    # ---- band C: per-thread-copy state. Written only onto dict(cfg)
    # copies; a write that lands on the shared original is a bug the
    # thread_copy contract test exists to catch. ----
    session = _prop(
        "_session",
        "Per-call session directive for the runners: {'id':…,'resume':…}, "
        "or None for a stateless call. The set/clear state machine lives in "
        "call_agent_sessioned — its finally-clear is exception-safety and "
        "must stay there.")
    claude_model_override = _prop(
        "_claude_model_override",
        "Per-call claude model override (integrator/escalation/retry); "
        "written only on per-call copies so it can never leak to siblings.")
    resolved = _prop(
        "_resolved",
        "Resolved model map. Band A at run scope; per-call PATCHES must "
        "copy-before-mutate (dict(... or {})) and assign back — the getter "
        "returns the live dict, never a copy.")

    # ---- band B′: per-phase-copy keys written by _apply_phase_routing /
    # _apply_role_routing onto the routed copy; they die with it. ----
    phase_key = _prop(
        "_phase_key",
        "The routed phase's key, set on the phase-scoped copy so "
        "enabled_agents can apply the phase's participant filter.")
    routed_turn_timeout = _prop(
        "_routed_turn_timeout",
        "Per-phase routed turn timeout (seconds); process_phase folds it "
        "into turn_timeout.")
    routed_rounds = _prop(
        "_routed_rounds",
        "Per-phase rounds override from project routing; 0 = unlimited.")
    phase_instructions = _prop(
        "_phase_instructions",
        "Per-phase operator instructions spliced into every turn's context.")
    role_routing = _prop(
        "_role_routing",
        "Validated per-ROLE routing sub-dict (roles.worker/integrator), "
        "applied per lane/turn by _apply_role_routing.")
    noted_indep_grader = _prop(
        "_noted_indep_grader",
        "One-shot latch: the independent-grader note has been emitted for "
        "this phase copy.")

    def thread_copy(self, health_key=None, stateless=False,
                    drop_prior_discussions=False):
        """dict(cfg) per-thread copy plus the band-C writes that accompany
        it. health_key=None means DO NOT WRITE the key (absence, not a
        None value); stateless=True writes _session = None (an explicit
        clear — the runners check `is not None`, so absence and None must
        stay distinguishable from a live session dict). Band-D maps alias
        by identity — that is the shallow copy's contract, not an
        accident."""
        c = dict(self._cfg)
        if health_key is not None:
            c["_health_key"] = health_key
        if stateless:
            c["_session"] = None
        if drop_prior_discussions:
            c["_drop_prior_discussions"] = True
        return c

    def patch_agent_model(self, agent, model):
        """Point one provider at a different model for a single retry
        call. Must be called on a per-call copy. _resolved is
        copy-before-mutate so the patch can never reach the parent's
        (or any sibling copy's) resolved map — they alias it."""
        resolved = dict(self._cfg.get("_resolved") or {})
        if agent == "claude":
            self._cfg["_claude_model_override"] = model
        elif agent == "codex":
            resolved["codex_model"] = model
        elif agent == "gemini":
            resolved["gemini_model"] = model
        self._cfg["_resolved"] = resolved

    def stash_new_session_id(self, sid):
        """Runner-side half of the codex session-id return channel: codex
        assigns its own session id and it must be scraped from output; the
        runner stashes it on the per-call cfg for call_agent_sessioned to
        harvest. Write-once per call."""
        self._cfg["_new_session_id"] = sid

    def take_new_session_id(self):
        """Caller-side half: harvest exactly once (pop, never
        read-and-leave). The finally-block take is exception-safety — a
        failed call must not leak a stale id into the next turn's copy."""
        return self._cfg.pop("_new_session_id", None)

    def end_phase(self):
        """Clear the per-phase channels a completed phase must not leak
        into the next one. Exactly these nine keys, in the order the
        historical clear block wrote them (byte-compat keel: the golden
        fixtures pin the surrounding behavior, this pins the discipline).

        Deliberately NOT used by the conversational close, which clears
        only its three-key subset (_phase_playbook/_knowledge/
        _verify_context) — the other six are never set on that path, and
        a "helpful" full clear there would be silent semantic drift.
        """
        c = self._cfg
        c["_allow_writes"] = False
        c["_build_dir"] = None
        c["_session_cwd"] = None
        c["_prior_disc_cap"] = None
        c["_phase_playbook"] = ""
        c["_knowledge"] = ""
        c["_read_dir"] = None
        c["_target_digest"] = ""
        c["_verify_context"] = ""
