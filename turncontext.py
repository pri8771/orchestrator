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

Tranche 1 (this file today) covers band B: the eleven per-phase keys that
process_phase sets on entry and clears on exit. Bands C (per-thread-copy)
and D (shared-by-identity) come with their own review and want methods
(e.g. thread_copy(health_key=...)), not bare properties.
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
