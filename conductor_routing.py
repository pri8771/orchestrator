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
    rules = []
    for layer in layers:
        for raw in layer.get("rules") or []:
            rule, err = validate_rule(raw)
            if err:
                warn("routing rule skipped: %s" % err)
                continue
            rules.append(rule)
    return RouteConfig(routes=routes, rules=rules)


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
