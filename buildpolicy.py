#!/usr/bin/env python3
"""Build-section target policy loader (V3 8.4).

Platform assumptions live under ``sections/build/`` rather than in fleet
configuration.  ``section.json`` names the editable target-policy file; a
separate shipped seed is the last-known-safe fallback when that file is
missing or malformed.  Consumers receive one validated policy shape, so
phase prompts, dependency lint, review guidance, and Definition of Done
cannot drift onto parallel resolution paths.

Only ``workflow_target == "app"`` selects the current ``ios`` policy.  A
second platform is deliberately out of scope; non-app workflows resolve no
build policy at all.
"""

import copy
import json
import os


BUILD_SECTION_DIR = os.path.join("sections", "build")
SECTION_FILENAME = "section.json"
DEFAULT_POLICY_FILENAME = "target_policy.json"
SEED_POLICY_FILENAME = "target_policy.seed.json"

# (path, target) -> ((mtime_ns, size), policy dict | Exception)
_CACHE: "dict[tuple, tuple]" = {}
# (path, (mtime_ns, size)) keys for fallback warnings already emitted
_WARNED: "set[tuple]" = set()


def _read_json(path):
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        raise ValueError("build policy file must contain a JSON object")
    return data


def _target_name(workflow_target):
    return "ios" if workflow_target in ("app", "ios") else None


def _validate_policy(data, target):
    targets = data.get("targets")
    if not isinstance(targets, dict) or not isinstance(targets.get(target), dict):
        raise ValueError("build policy missing targets.%s object" % target)
    policy = copy.deepcopy(targets[target])
    required = {
        "app_rules": list,
        "phase_rules": dict,
        "tech_stack": dict,
        "dod": dict,
        "review_prompts": dict,
        "contract_snippets": dict,
    }
    for field, want in required.items():
        if not isinstance(policy.get(field), want):
            raise ValueError("targets.%s.%s must be a %s"
                             % (target, field, want.__name__))
    if not policy["app_rules"] or not all(
            isinstance(item, str) and item.strip()
            for item in policy["app_rules"]):
        raise ValueError("targets.%s.app_rules must contain non-empty strings"
                         % target)
    for phase, entry in policy["phase_rules"].items():
        if not isinstance(phase, str) or not isinstance(entry, dict):
            raise ValueError("targets.%s.phase_rules must map names to objects"
                             % target)
        for field in ("rules", "required_output", "acceptance_checks"):
            if field in entry and (not isinstance(entry[field], list) or not all(
                    isinstance(item, str) for item in entry[field])):
                raise ValueError("targets.%s.phase_rules.%s.%s must be a "
                                 "string list" % (target, phase, field))
    stack = policy["tech_stack"]
    if not isinstance(stack.get("allowed"), list) or not isinstance(
            stack.get("banned"), list) or not isinstance(
                stack.get("notes"), str):
        raise ValueError("targets.%s.tech_stack has an invalid shape" % target)
    # Entry SHAPES matter too: designlint.render_tech_stack does e["name"] and
    # " — " + e["for"]/e["why"] unconditionally, so a bare-string entry (a
    # natural hand-edit shorthand) or a non-str for/why would pass the list
    # checks above, be cached VALID here, and then crash every app-workflow
    # run at startup.  Reject them so the seed fallback engages instead.
    for key in ("allowed", "banned"):
        for entry in stack[key]:
            if not isinstance(entry, dict) or \
                    not str(entry.get("name", "")).strip() or any(
                        f in entry and not isinstance(entry[f], str)
                        for f in ("for", "why", "url_contains")):
                raise ValueError(
                    "targets.%s.tech_stack.%s entries must be objects with a "
                    "non-empty name (for/why/url_contains strings when present)"
                    % (target, key))
    for tier in ("prototype", "mvp", "v1", "production_draft"):
        items = policy["dod"].get(tier)
        if not isinstance(tier, str) or not isinstance(items, list) or not all(
                isinstance(item, str) and item.strip() for item in items):
            raise ValueError("targets.%s.dod tiers must be string lists" % target)
    if not all(isinstance(key, str) and isinstance(value, str)
               for key, value in policy["review_prompts"].items()):
        raise ValueError("targets.%s.review_prompts must map names to text"
                         % target)
    if not all(isinstance(key, str) and isinstance(value, str)
               for key, value in policy["contract_snippets"].items()):
        raise ValueError("targets.%s.contract_snippets must map names to text"
                         % target)
    if not policy["phase_rules"] or not policy["review_prompts"]:
        raise ValueError("targets.%s must include phase and review guidance"
                         % target)
    return policy


def _cached_policy(path, target):
    st = os.stat(path)
    key = (st.st_mtime_ns, st.st_size)
    cached = _CACHE.get((path, target))
    if cached is not None and cached[0] == key:
        if isinstance(cached[1], Exception):
            raise cached[1]
        return copy.deepcopy(cached[1])
    try:
        policy = _validate_policy(_read_json(path), target)
    except (OSError, ValueError) as exc:
        _CACHE[(path, target)] = (key, exc)
        raise
    _CACHE[(path, target)] = (key, policy)
    return copy.deepcopy(policy)


def load_target_policy(orch_dir, workflow_target, on_fallback=None):
    """Return the selected Build policy or ``None`` for non-app workflows.

    A missing/corrupt editable policy emits one visible-fallback callback per
    on-disk version and resolves the shipped seed.  If the seed itself is
    broken, fail loudly: silently returning an empty build policy would remove
    binding quality and dependency constraints.
    """
    target = _target_name(workflow_target)
    if target is None:
        return None
    build_dir = os.path.join(orch_dir, BUILD_SECTION_DIR)
    # Unit fixtures and older installed engines may have no Sections tree at
    # all.  Preserve their fleet-only behavior; once a Build section exists,
    # however, its target contract is mandatory and visibly fallback-safe.
    if not os.path.isdir(build_dir):
        return None
    policy_name = DEFAULT_POLICY_FILENAME
    section_path = os.path.join(build_dir, SECTION_FILENAME)
    section_error = None
    try:
        section = _read_json(section_path)
        named = section.get("target_policy")
        if named is not None:
            if not isinstance(named, str) or not named.strip() or \
                    os.path.basename(named) != named:
                raise ValueError("section.json target_policy must be a file name")
            policy_name = named
    except (OSError, ValueError) as exc:
        section_error = exc

    policy_path = os.path.join(build_dir, policy_name)
    error = section_error
    if error is None:
        try:
            return _cached_policy(policy_path, target)
        except (OSError, ValueError) as exc:
            error = exc

    try:
        stat = os.stat(policy_path if section_error is None else section_path)
        version = (stat.st_mtime_ns, stat.st_size)
    except OSError:
        version = (None, None)
    warning_key = (section_path if section_error is not None else policy_path,
                   version)
    # A headless consumer (for example design lint) must not consume the one
    # visible warning before an event-capable engine path sees the same bad
    # file.  Suppress repeats only after a callback actually emitted it.
    if on_fallback is not None and warning_key not in _WARNED:
        _WARNED.add(warning_key)
        on_fallback(warning_key[0], error)

    seed_path = os.path.join(build_dir, SEED_POLICY_FILENAME)
    try:
        return _cached_policy(seed_path, target)
    except (OSError, ValueError) as seed_error:
        raise RuntimeError("Build target policy and seeded fallback are invalid: "
                           "%s; seed: %s" % (error, seed_error))


def clear_cache():
    """Test/migration hook: forget parsed policies and fallback suppression."""
    _CACHE.clear()
    _WARNED.clear()
