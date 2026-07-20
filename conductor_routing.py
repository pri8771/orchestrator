"""Conductor routing rules (V3 7.2, sub-PR a): the pure, side-effect-free
half of artifact routing — load the data-driven rules, merge the two config
layers, resolve a deterministic target, and guard a candidate route.

Nothing here mints, spawns, or writes: strategy EXECUTION and minting live in
the conductor's acting stage (sub-PR b), which calls these functions and then
routes exclusively through sessions.mint_delegation_session (conductor.py owns
no dir-minting code — grep-checkable). Keeping load/merge/guard pure is what
makes the oscillation, hop-budget, and merge-precedence cases unit-testable
without a filesystem full of live sessions.

Config lives in the section layer (sections/<name>/routing.json, the shared
default outbound routes) merged under the project layer
(<root>/<project>/routing.json), same non-empty-wins per-field precedence as
modelrouting.load_routing_for_app. Two keys coexist in that file:
  "artifact_routes": {<type>: <target-section>}  — the flat map the GUI's
     route-preview chip reads (RoutePreviewResolver); route-to-one defaults.
  "rules": [ {match, strategy, targets, hop_budget, ...}, ... ]  — the full
     rule objects the engine evaluates; the GUI ignores this key.
A model_routing.json-shaped file (phases/enabled/fallback) carries NO artifact
routes and yields an empty, disabled-free routing set — never a guessed route.

Stdlib only; imports nothing from the engine. Corrupt config disables routing
with a visible banner rather than falling back to stale rules (R2/§6.2).
"""
import hashlib
import json
import os

STRATEGIES = ("one", "every", "chain")
ROUTING_FILENAME = "routing.json"
# The guard verdicts — an explicit enum, never a bare bool (R4). "allow" is
# the ONLY value the caller may act on; the rest are ledgered refusals.
ALLOW = "allow"
CONVERGED = "converged"
BUDGET_EXHAUSTED = "budget_exhausted"
DEFAULT_HOP_BUDGET = 4


class RouteConfig:
    """The merged, validated routing set for one (section, project) pair.
    `ok` is False when a config file was present but corrupt — the caller
    disables routing and shows `banner`, never routes on partial data."""

    __slots__ = ("routes", "rules", "ok", "banner")

    def __init__(self, routes=None, rules=None, ok=True, banner=""):
        self.routes = routes or {}
        self.rules = rules or []
        self.ok = ok
        self.banner = banner


def _read_layer(path):
    """(dict, error) for one routing.json layer. Missing file -> ({}, None):
    absence is a valid empty layer. Corrupt/non-object -> ({}, message):
    a present-but-broken file must disable routing, not be treated as empty
    (silently ignoring a config the user wrote is the §6.2 violation)."""
    if not os.path.exists(path):
        return {}, None
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as exc:
        return {}, "unreadable %s: %s" % (path, exc)
    if not isinstance(data, dict):
        return {}, "%s is not a JSON object" % path
    return data, None


def _normalize_routes(raw):
    """The GUI-shared artifact_routes map, tolerant of both shapes the GUI
    resolver accepts: a flat {type: target} object, or a list of
    {match:{artifact_type}, target} rule-lite objects. Returns {type:
    target} with blank targets dropped."""
    out = {}
    if isinstance(raw, dict):
        for atype, target in raw.items():
            if isinstance(target, str) and target.strip():
                out[str(atype)] = target.strip()
    elif isinstance(raw, list):
        for item in raw:
            if not isinstance(item, dict):
                continue
            atype = (item.get("match") or {}).get("artifact_type") \
                if isinstance(item.get("match"), dict) else item.get("artifact_type")
            target = item.get("target") or item.get("target_section")
            if isinstance(atype, str) and isinstance(target, str) \
                    and atype.strip() and target.strip():
                out[atype.strip()] = target.strip()
    return out


def validate_rule(rule):
    """(normalized_rule, error). A rule needs a match.artifact_type, a valid
    strategy, and at least one target. Per-field messages, never a silent
    drop — an author who typo'd a strategy must be told which one."""
    if not isinstance(rule, dict):
        return None, "rule is not an object"
    match = rule.get("match")
    if not isinstance(match, dict) or not match.get("artifact_type"):
        return None, "rule missing match.artifact_type"
    strategy = rule.get("strategy", "one")
    if strategy not in STRATEGIES:
        return None, ("rule for %r has unknown strategy %r (valid: %s)"
                      % (match.get("artifact_type"), strategy,
                         ", ".join(STRATEGIES)))
    targets = rule.get("targets")
    if targets is None and rule.get("target"):
        targets = [rule["target"]]
    targets = [t for t in (targets or []) if isinstance(t, str) and t.strip()]
    if not targets:
        return None, ("rule for %r has no targets"
                      % (match.get("artifact_type"),))
    if strategy in ("one", "chain") and len(targets) > 1:
        return None, ("rule for %r uses strategy %r but names %d targets — "
                      "one/chain route to a single destination (use 'every' "
                      "to fan out)" % (match.get("artifact_type"), strategy,
                                       len(targets)))
    budget = rule.get("hop_budget", DEFAULT_HOP_BUDGET)
    if not isinstance(budget, int) or isinstance(budget, bool) or budget < 1:
        return None, ("rule for %r has invalid hop_budget %r (need int >= 1)"
                      % (match.get("artifact_type"), budget))
    return {"artifact_type": match["artifact_type"],
            "source_section": match.get("source_section"),
            "strategy": strategy, "targets": [t.strip() for t in targets],
            "hop_budget": budget,
            "rule_id": rule.get("rule_id")
            or _derive_rule_id(match["artifact_type"], strategy, targets)}, None


def _derive_rule_id(artifact_type, strategy, targets):
    # Stable identity so 7.3 can hash (artifact, target, RULE) and two rules
    # routing the same type to the same target stay distinguishable.
    return "%s:%s:%s" % (artifact_type, strategy, ",".join(sorted(targets)))


def _merge_layer(base, overlay):
    """Per-key non-empty-wins, mirroring load_routing_for_app: an overlay
    entry replaces the base entry for that key; absent/blank inherits."""
    merged = dict(base)
    for key, val in overlay.items():
        if val not in ("", None):
            merged[key] = val
    return merged


def load_route_config(sections_dir, section, project_dir, on_warn=None):
    """Merge the section-default and project-override routing.json layers
    into one validated RouteConfig. Either layer corrupt -> ok=False +
    banner (routing disabled). Rule-level validation errors warn per rule
    and drop only that rule, keeping the rest (a single bad rule shouldn't
    silence the whole file — distinct from a corrupt FILE, which must)."""
    warn = on_warn or (lambda _m: None)
    section_path = os.path.join(sections_dir, section, ROUTING_FILENAME) \
        if section else None
    project_path = os.path.join(project_dir, ROUTING_FILENAME) \
        if project_dir else None
    banners = []
    layers = []
    for path in (section_path, project_path):
        if path is None:
            layers.append({})
            continue
        data, err = _read_layer(path)
        if err:
            banners.append(err)
        layers.append(data)
    if banners:
        banner = "routing disabled — " + "; ".join(banners)
        warn(banner)
        return RouteConfig(ok=False, banner=banner)

    routes = _merge_layer(_normalize_routes(layers[0].get("artifact_routes")),
                          _normalize_routes(layers[1].get("artifact_routes")))
    # Rules override by (artifact_type, source_section): a project rule for
    # the same match REPLACES the section default, matching the routes map's
    # non-empty-wins precedence (blind concatenation would let a project only
    # ADD, never override, a section rule — a silent surprise).
    by_key = {}
    order = []
    for layer in layers:   # section first, then project (project wins)
        for raw in layer.get("rules") or []:
            rule, err = validate_rule(raw)
            if err:
                warn("routing rule skipped: %s" % err)
                continue
            key = (rule["artifact_type"], rule["source_section"])
            if key not in by_key:
                order.append(key)
            by_key[key] = rule
    return RouteConfig(routes=routes, rules=[by_key[k] for k in order])


def deterministic_target(artifact_type, config):
    """The route-to-one deterministic target for a type, or None when no
    mapping exists (the ONLY case that may invoke the classifier). Checked
    before any model call — a mapped type never costs an inference turn."""
    return config.routes.get(artifact_type)


def rules_for(artifact_type, source_section, config):
    """Every rule whose match applies to this artifact, in file order. A
    rule with no source_section matches any source; a set one must equal
    the producing section."""
    out = []
    for rule in config.rules:
        if rule["artifact_type"] != artifact_type:
            continue
        if rule["source_section"] and rule["source_section"] != source_section:
            continue
        out.append(rule)
    return out


def guard_route(meta, lineage_metas, hop_budget=DEFAULT_HOP_BUDGET):
    """The pre-route safety verdict for one candidate artifact — pure, the
    single gate every strategy passes through before any side effect.

      CONVERGED         — this body's content_hash already appears ANYWHERE
                          in its own lineage (oscillation A->B->A': routing
                          it would loop forever). This scans the WHOLE
                          ancestry, not just the direct parent — artifacts.py
                          only converges on the immediate parent at publish,
                          so the two-hops-back collision reaches here live.
      BUDGET_EXHAUSTED  — hop_count has reached the rule's hop_budget: the
                          chain has gone as far as the author allowed.
      ALLOW             — neither guard tripped.

    `lineage_metas` maps ancestor id -> its meta (or carries content_hash
    directly); a missing ancestor hash can't prove convergence, so it's
    treated as non-colliding (fail-open on missing DATA, never on a real
    collision — the collision case has the hash by construction)."""
    if not isinstance(meta, dict):
        return CONVERGED   # a nonsense candidate is never routed
    my_hash = meta.get("content_hash")
    ancestors = meta.get("lineage") or []
    if my_hash:
        for anc_id in ancestors:
            anc = lineage_metas.get(anc_id) if lineage_metas else None
            anc_hash = anc.get("content_hash") if isinstance(anc, dict) else None
            if anc_hash and anc_hash == my_hash:
                return CONVERGED
    hop_count = meta.get("hop_count")
    if isinstance(hop_count, int) and not isinstance(hop_count, bool) \
            and hop_count >= hop_budget:
        return BUDGET_EXHAUSTED
    return ALLOW


# --- sub-PR (b): planning + execution -------------------------------------
# Planning is pure (no mint, no model call unless a deterministic target is
# missing); execution takes injected side-effect callables so the conductor
# owns no dir-minting code and tests need no real subprocess.

class RouteIntent:
    """One resolved (artifact -> target-section, strategy) decision, ready
    to execute. `verdict` is a guard/resolution outcome; only ALLOW intents
    with a target actually mint. `route_key` carries the 7.3 hash inputs."""

    __slots__ = ("artifact_id", "content_hash", "source_section", "target",
                 "strategy", "rule_id", "verdict", "reason")

    def __init__(self, artifact_id, content_hash, source_section, target,
                 strategy, rule_id, verdict, reason=""):
        self.artifact_id = artifact_id
        self.content_hash = content_hash
        self.source_section = source_section
        self.target = target
        self.strategy = strategy
        self.rule_id = rule_id
        self.verdict = verdict
        self.reason = reason

    @property
    def route_key(self):
        # The stable identity 7.3 hashes for exactly-once routing: artifact
        # content + destination + which rule decided it (two rules to the
        # same target must not collapse; a NEW content_hash is a NEW route).
        return {"artifact_id": self.artifact_id,
                "content_hash": self.content_hash,
                "target": self.target, "rule_id": self.rule_id}

    @property
    def route_id(self):
        return route_id_for(self.route_key)

    def as_ledger_detail(self):
        return {"artifact_id": self.artifact_id, "target": self.target,
                "strategy": self.strategy, "rule_id": self.rule_id,
                "verdict": self.verdict, "reason": self.reason}


def route_id_for(route_key):
    """The deterministic route_id = sha256(canonical route_key)[:16] — the
    single hash inputs 7.3 promises: {artifact_id, content_hash, target,
    rule_id}. Same route -> same id (dedup); new artifact version (new
    content_hash) -> new id (re-routes). conductor.route_digest delegates
    here so there is ONE definition."""
    blob = json.dumps(route_key, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def plan_routes(meta, source_section, config, lineage_metas, classify=None):
    """Pure route planning for ONE admissible artifact — the caller has
    already filtered to admissible, non-branched tips. Returns a list of
    RouteIntent (one per target for broadcast; one for one/chain; a single
    non-ALLOW intent when guarded off or unroutable). No side effects except
    an optional `classify(atype, candidates)` call, invoked ONLY when a type
    has no deterministic target AND no explicit rule — the single-inference
    unmapped case.

    `classify` returns a target section from the given closed candidate list,
    or None (failure -> an `unroutable` intent, never a guessed route)."""
    atype = meta.get("artifact_type") or meta.get("type")
    rules = rules_for(atype, source_section, config)
    aid, chash = meta.get("id"), meta.get("content_hash")

    def guarded(target, strategy, rule_id, hop_budget):
        verdict = guard_route(meta, lineage_metas, hop_budget)
        reason = "" if verdict == ALLOW else verdict
        return RouteIntent(aid, chash, source_section, target, strategy,
                           rule_id, verdict, reason)

    if rules:
        intents = []
        for rule in rules:
            if rule["strategy"] == "every":
                for tgt in rule["targets"]:
                    intents.append(guarded(tgt, "every", rule["rule_id"],
                                           rule["hop_budget"]))
            else:  # one | chain -> the rule's first target
                intents.append(guarded(rule["targets"][0], rule["strategy"],
                                        rule["rule_id"], rule["hop_budget"]))
        return intents

    target = deterministic_target(atype, config)
    if target is None and classify is not None:
        # The ONLY inference: a closed candidate list, output selects a
        # label — the model can never emit a free-form target. Candidates
        # come from every configured destination (routes map AND rule
        # targets), not just the flat map.
        candidates = sorted(set(config.routes.values())
                            | {t for r in config.rules for t in r["targets"]})
        chosen = classify(atype, candidates) if candidates else None
        if chosen in candidates:
            return [guarded(chosen, "one", "classifier:%s" % atype,
                            DEFAULT_HOP_BUDGET)]
        return [RouteIntent(aid, chash, source_section, None, "one",
                            "classifier:%s" % atype, "unroutable",
                            "no deterministic route and classifier declined")]
    if target is None:
        return [RouteIntent(aid, chash, source_section, None, "one", None,
                            "unroutable", "no route for type %r" % atype)]
    return [guarded(target, "one", "route:%s" % atype, DEFAULT_HOP_BUDGET)]


def route_marker(route_id):
    """The exactly-once marker embedded in a routed session's seed prompt and
    delegation metadata. Restart recovery probes for it to prove a route
    already effected before re-acting (§4.3)."""
    return "[route:%s]" % route_id


def execute_intents(intents, session_id, root, mint, ledger, permit=None,
                    probe=None):
    """Execute planned intents, ledger-before-act with the 7.3 intent->done
    protocol. For each intent: a `route_proposed` line (carrying route_id) is
    durable FIRST; a guarded/denied intent gets its terminal line and no
    effect; an ALLOW+permitted intent then either RECOVERS (probe proves the
    route already effected on a prior, crashed run — ledger `route_recovered`,
    no second mint) or MINTS and ledgers `route_approved`.

    `mint(target_section, request_meta) -> session_dir|None` wraps
    sessions.mint_delegation_session; request_meta carries content_hash AND
    route_id so a NEW artifact version mints a DISTINCT session (the delegation
    id hashes the request) while the SAME version is idempotent. The seed
    title embeds the route marker so it survives into the minted session.
    `permit` is the 7.4 seam. `probe(route_id, target_section) -> bool` is the
    restart-recovery check (default: none — rely on mint idempotency)."""
    permit = permit or (lambda _intent: True)
    probe = probe or (lambda _rid, _tgt: False)
    outcomes = []
    for intent in intents:
        rid = intent.route_id
        base = {"session": session_id, "route_id": rid,
                "route_key": intent.route_key,
                "detail": intent.as_ledger_detail()}
        ledger({**base, "decision": "route_proposed"})
        if intent.verdict != ALLOW or not intent.target:
            outcomes.append({"outcome": intent.verdict, "target": intent.target,
                             "route_id": rid, "route_key": intent.route_key})
            ledger({**base, "decision": intent.verdict})
            continue
        if not permit(intent):
            outcomes.append({"outcome": "denied", "target": intent.target,
                             "route_id": rid, "route_key": intent.route_key})
            ledger({**base, "decision": "denied"})
            continue
        if probe(rid, intent.target):
            # The effect already landed on a prior run that crashed before
            # recording done — recover, don't mint twice (§4.3, §23).
            outcomes.append({"outcome": "recovered", "target": intent.target,
                             "route_id": rid, "route_key": intent.route_key})
            ledger({**base, "decision": "route_recovered"})
            continue
        request = {"title": "%s route:%s -> %s"
                            % (route_marker(rid), intent.artifact_id,
                               intent.target),
                   "artifact_id": intent.artifact_id,
                   "content_hash": intent.content_hash,   # 7.3: version-
                   "route_id": rid,                       # distinct minting
                   "source_section": intent.source_section,
                   "rule_id": intent.rule_id}
        session_dir = mint(intent.target, request)
        outcome = "routed" if session_dir else "mint_failed"
        outcomes.append({"outcome": outcome, "target": intent.target,
                         "route_id": rid, "session_dir": session_dir,
                         "route_key": intent.route_key})
        ledger({**base, "decision": "route_approved" if session_dir
                else "mint_failed",
                "detail": {**intent.as_ledger_detail(),
                           "session_dir": session_dir}})
    return outcomes
