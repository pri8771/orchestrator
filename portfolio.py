#!/usr/bin/env python3
"""
portfolio.py — deterministic portfolio-to-project expansion.

Some prompts ask for a *portfolio* of app ideas, not one app. The agents can
debate the ideas, but the engine owns the filesystem contract:

    <root>/<project-a>/
    <root>/<project-b>/
    ...

Never a wrapper folder that secretly contains many projects. This module parses
an agent-emitted ```portfolio-json``` manifest and materializes each selected
app as a sibling folder under the workspace root.
"""

import json
import os
import re
import textwrap

import schemas as schemalib

FENCE = "portfolio-json"
MANIFEST_NAME = "portfolio_manifest.json"
CHILD_META_NAME = "portfolio_child.json"
AUTORUN_DISABLED_NAME = ".orchestrator_autorun_disabled"


PORTFOLIO_JSON_INSTRUCTION = (
    "PORTFOLIO CONTRACT (required for multi-app prompts): if this run selects "
    "more than one app/project, your wrap-up MUST emit one fenced "
    "```portfolio-json``` block with JSON shaped like "
    '{"apps":[{"name":"...","slug":"...","category":"...",'
    '"one_sentence_promise":"...","target_user":"...",'
    '"painful_moment":"...","why_different":"...",'
    '"why_users_pay":"...","subscription_model":"...",'
    '"local_first_mvp_scope":"...","future_backend_roadmap":"...",'
    '"core_workflows":["..."],"ios_native_features":["..."],'
    '"ar_ml_llm_fit":"...","design_direction":"...",'
    '"claude_design_prompt":"...","app_store_positioning":"...",'
    '"key_risks":["..."],"invalidation_criteria":"...",'
    '"v1_build_scope":"...","build":true,"build_priority":1}]}.\n'
    "Include EVERY selected app in this manifest, not only the MVP build picks. "
    "If the original prompt asks for multiple apps to be built, production-ready "
    "apps, 0-to-production apps, or says not to produce only specs, EVERY app in "
    "the selected build set MUST use build=true. Only use build=false when the "
    "human explicitly asks for spec-only/documentation apps. The orchestrator "
    "will then create one sibling child project per app: build=true children run "
    "the full app_build workflow, build=false children run an app_spec workflow "
    "with product/design/tech/planning/review phases but no code build. Use "
    "stable, lowercase slugs because those become folder names.\n"
)


# Signals that a prompt is intentionally a multi-app portfolio. Hoisted to module
# scope (from inside the function) so the boundaries are inspectable and unit
# tested — this classifier gates whether the engine fans a prompt out into
# sibling child projects, so a misclassification is expensive.
PORTFOLIO_SIGNALS = (
    "one folder per selected app",
    "one folder per app",
    "for every selected app",
    "each selected app",
    "selected app list",
    "apps 1-8",
    "required app map",
    "do not build 63 apps",
    "multiple apps",
    "more than one app",
    "one unique app for each",
    "1 unique app for each",
    "one app for each",
    "out of these categories",
    "these categories",
    "each app should",
    "all these apps",
)
APP_CATEGORY_NAMES = (
    "business", "finance", "food & drink", "graphics & design",
    "health & fitness", "medical", "navigation", "news",
    "photo & video", "productivity", "reference", "social networking",
    "travel", "utilities", "weather", "sports", "shopping",
)
# The "distributive" phrases that, alongside several category names, imply
# "one app per category".
_PORTFOLIO_DISTRIBUTIVE = ("one app", "1 app", "unique app", "each app", "for each")


def is_portfolio_parent_prompt(prompt):
    """Heuristic guard for prompts that are intentionally multi-app portfolios."""
    text = (prompt or "").lower()
    if "portfolio_child_project:" in text:
        return False
    signal_hits = sum(1 for s in PORTFOLIO_SIGNALS if s in text)
    category_hits = sum(1 for name in APP_CATEGORY_NAMES if name in text)
    return ("portfolio" in text and signal_hits >= 1) \
        or signal_hits >= 2 \
        or (category_hits >= 4 and any(s in text for s in _PORTFOLIO_DISTRIBUTIVE))


def requires_built_children(prompt):
    """True when a portfolio prompt says selected apps must become built apps.

    This prevents agents from silently downgrading a multi-app build request into
    one built app plus many spec dossiers. They may still recommend reducing
    scope in prose, but the filesystem contract stays with the user's prompt.
    """
    text = (prompt or "").lower()
    if "portfolio_child_project:" in text:
        return False
    explicit_spec_only = (
        "spec only", "spec-only", "documentation only", "docs only",
        "do not build code", "no code build",
    )
    if any(s in text for s in explicit_spec_only):
        return False
    build_signals = (
        "production ready", "production-ready", "0 -> production",
        "zero to production", "launch-ready", "launch ready",
        "not just specs", "not only specs", "not specs",
        "whole production ready app", "whole app", "complete app",
        "multiple apps built", "apps built", "build all",
        "all these apps should go", "from 0 -> production",
    )
    return is_portfolio_parent_prompt(prompt) and any(s in text for s in build_signals)


def slugify(value, fallback="app"):
    """Folder-safe lowercase slug."""
    s = re.sub(r"[^a-zA-Z0-9]+", "-", str(value or "").strip().lower())
    s = re.sub(r"-+", "-", s).strip("-")
    return s or fallback


def _as_list(value):
    if value is None:
        return []
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    if isinstance(value, str):
        parts = [p.strip(" -\t") for p in re.split(r"\n|;", value)]
        return [p for p in parts if p]
    return [str(value)]


def _clean_app(raw, index):
    if not isinstance(raw, dict):
        return None, "app #%d is not an object" % index
    name = str(raw.get("name") or raw.get("app_name") or "").strip()
    if not name:
        return None, "app #%d missing name" % index
    app = dict(raw)
    app["name"] = name
    app["slug"] = slugify(app.get("slug") or name, "app-%02d" % index)
    # Normalize common field aliases.
    aliases = {
        "promise": "one_sentence_promise",
        "target": "target_user",
        "mvp_scope": "local_first_mvp_scope",
        "backend_roadmap": "future_backend_roadmap",
        "design_prompt": "claude_design_prompt",
        "risks": "key_risks",
        "invalidates_if": "invalidation_criteria",
    }
    for src, dst in aliases.items():
        if dst not in app and src in app:
            app[dst] = app[src]
    app["core_workflows"] = _as_list(app.get("core_workflows"))
    app["ios_native_features"] = _as_list(app.get("ios_native_features"))
    app["key_risks"] = _as_list(app.get("key_risks"))
    build = app.get("build")
    if isinstance(build, str) and build.strip().lower() in (
            "false", "no", "0", "spec", "spec-only", ""):
        # Agents sometimes emit build as a string; "false"/"no"/"spec" must not
        # coerce to True via bool().
        app["build"] = False
    else:
        app["build"] = bool(build or str(app.get("status", "")).lower() in (
            "build", "mvp", "built", "build_now"))
    try:
        app["build_priority"] = int(app.get("build_priority") or 0)
    except (TypeError, ValueError):
        app["build_priority"] = 0
    return app, None


def parse_portfolio_manifest(text):
    """Return (manifest, errors) from ```portfolio-json``` blocks.

    Last valid app slug wins so a coordinator's final block can refine an
    earlier draft without duplicating folders.
    """
    errors = []
    blocks = schemalib.extract_structured_blocks(text or "", FENCE,
                                                 on_error=errors.append)
    by_slug = {}
    for block in blocks:
        apps = block.get("apps")
        if not isinstance(apps, list):
            errors.append("portfolio-json block missing apps list")
            continue
        for i, raw in enumerate(apps, 1):
            app, err = _clean_app(raw, i)
            if err:
                errors.append(err)
                continue
            by_slug[app["slug"]] = app
    apps = list(by_slug.values())
    apps.sort(key=lambda a: (
        0 if a.get("build") else 1,
        a.get("build_priority") or 9999,
        a.get("slug", "")))
    return {"schema_version": schemalib.SCHEMA_VERSION, "apps": apps}, errors


def manifest_path(parent_dir):
    return os.path.join(parent_dir, MANIFEST_NAME)


def load_manifest(parent_dir):
    try:
        with open(manifest_path(parent_dir), encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict) and isinstance(data.get("apps"), list):
            return data
    except (OSError, ValueError):
        pass
    return None


def child_autorun_disabled(child_dir):
    return os.path.exists(os.path.join(child_dir, AUTORUN_DISABLED_NAME))


def _write_json(path, payload):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2)
    os.replace(tmp, path)


def _write_text(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text.rstrip() + "\n")


def _unique_slug(root, preferred, reserved):
    base = slugify(preferred)
    slug = base
    n = 2
    while slug in reserved or os.path.exists(os.path.join(root, slug)):
        slug = "%s-%d" % (base, n)
        n += 1
    reserved.add(slug)
    return slug


def _is_minted_child(child_dir, parent_app, app_name):
    """True when child_dir holds a portfolio_child.json this parent minted for
    the app named app_name (identity check for reusing suffixed slugs)."""
    if not app_name:
        return False
    try:
        with open(os.path.join(child_dir, CHILD_META_NAME), encoding="utf-8") as fh:
            meta = json.load(fh)
        return meta.get("parent") == parent_app and \
            (meta.get("app") or {}).get("name") == app_name
    except (OSError, ValueError, AttributeError):
        return False


def _reuse_or_mint_slug(root, preferred, parent_app, app_name, reserved):
    """When ``preferred`` is squatted by an unrelated folder, reuse a suffixed
    variant (foo-2, foo-3, ...) this parent already minted for this app on an
    earlier call instead of minting a fresh one every time."""
    n = 2
    while True:
        cand = "%s-%d" % (preferred, n)
        if not os.path.exists(os.path.join(root, cand)):
            break
        if cand not in reserved and _is_minted_child(
                os.path.join(root, cand), parent_app, app_name):
            reserved.add(cand)
            return cand
        n += 1
    return _unique_slug(root, preferred, reserved)


def _spec_markdown(app):
    fields = [
        ("App Name", app.get("name")),
        ("Category", app.get("category")),
        ("One-Sentence Promise", app.get("one_sentence_promise")),
        ("Target User", app.get("target_user")),
        ("Painful Moment Solved", app.get("painful_moment")),
        ("Why It Is Different", app.get("why_different")),
        ("Why Users Would Pay", app.get("why_users_pay")),
        ("Subscription Model", app.get("subscription_model")),
        ("Local-First MVP Scope", app.get("local_first_mvp_scope")),
        ("Future Backend Roadmap", app.get("future_backend_roadmap")),
        ("AR / Local ML / Local LLM Fit", app.get("ar_ml_llm_fit")),
        ("Design Direction", app.get("design_direction")),
        ("App Store Positioning", app.get("app_store_positioning")),
        ("Invalidation Criteria", app.get("invalidation_criteria")),
        ("v1 Build Scope", app.get("v1_build_scope")),
    ]
    lines = ["# %s" % app.get("name", "Selected App"), ""]
    lines.append("Build mode: **%s**." % ("MVP build" if app.get("build") else "Spec/documentation"))
    if app.get("build_priority"):
        lines.append("Build priority: **%s**." % app["build_priority"])
    lines.append("")
    for title, value in fields:
        if value:
            lines += ["## %s" % title, "", str(value).strip(), ""]
    for title, values in (("Core Workflows", app.get("core_workflows") or []),
                          ("iOS-Native Features", app.get("ios_native_features") or []),
                          ("Key Risks", app.get("key_risks") or [])):
        if values:
            lines += ["## %s" % title, ""]
            lines += ["- %s" % v for v in values]
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _child_prompt(parent_app, app, parent_prompt):
    spec = _spec_markdown(app)
    mode = "Build this app as a working local-first SwiftUI MVP." if app.get("build") else (
        "Run this app through the full product, design, technical, planning, and "
        "review phases as a separate app concept, producing stranger-buildable "
        "documentation and a Claude Design handoff. Do not build code in this "
        "workflow; this is the per-app spec pipeline.")
    return textwrap.dedent("""\
        PORTFOLIO_CHILD_PROJECT: true
        Parent portfolio: {parent}
        Selected app slug: {slug}

        {mode}

        ## Selected App Spec

        {spec}

        ## Claude Design Handoff Prompt

        {design}

        ## Parent Portfolio Prompt

        {prompt}
        """).format(
            parent=parent_app,
            slug=app.get("slug"),
            mode=mode,
            spec=spec.strip(),
            design=str(app.get("claude_design_prompt") or "").strip() or "(not supplied yet)",
            prompt=(parent_prompt or "").strip(),
        )


def materialize_children(root, parent_app, parent_dir, parent_prompt, manifest,
                         force_build_all=False):
    """Create/update sibling folders for manifest apps.

    Returns {"created": [...], "updated": [...], "skipped": [...]}.
    """
    result = {"created": [], "updated": [], "skipped": []}
    apps = list((manifest or {}).get("apps") or [])
    reserved = {parent_app}
    materialized = []
    for app in apps:
        app = dict(app)
        if force_build_all:
            app["build"] = True
        preferred = slugify(app.get("slug") or app.get("name"))
        child_dir = os.path.join(root, preferred)
        meta_path = os.path.join(child_dir, CHILD_META_NAME)
        same_parent = False
        try:
            with open(meta_path, encoding="utf-8") as fh:
                same_parent = json.load(fh).get("parent") == parent_app
        except (OSError, ValueError, AttributeError):
            same_parent = False
        if os.path.exists(child_dir) and not same_parent:
            slug = _reuse_or_mint_slug(root, preferred, parent_app,
                                       app.get("name"), reserved)
        else:
            slug = preferred
            reserved.add(slug)
        app["slug"] = slug
        child_dir = os.path.join(root, slug)
        meta_path = os.path.join(child_dir, CHILD_META_NAME)
        if os.path.exists(child_dir) and not os.path.exists(meta_path):
            result["skipped"].append(slug)
            continue
        existed = os.path.exists(child_dir)
        os.makedirs(child_dir, exist_ok=True)
        _write_json(meta_path, {
            "schema_version": schemalib.SCHEMA_VERSION,
            "parent": parent_app,
            "build": bool(app.get("build")),
            "build_priority": app.get("build_priority") or 0,
            "app": app,
        })
        _write_text(os.path.join(child_dir, "SPEC.md"), _spec_markdown(app))
        _write_text(os.path.join(child_dir, "CLAUDE_DESIGN_PROMPT.md"),
                    str(app.get("claude_design_prompt") or "").strip() or
                    "Claude Design prompt was not supplied in the portfolio manifest.")
        _write_text(os.path.join(child_dir, "initial_prompt", "initial_prompt.md"),
                    _child_prompt(parent_app, app, parent_prompt))
        workflow = "app_build" if app.get("build") else "app_spec"
        _write_text(os.path.join(child_dir, "workflow.txt"), workflow)
        # Older versions parked spec-only children with this marker. New portfolio
        # children should run their per-app spec phases automatically.
        try:
            os.remove(os.path.join(child_dir, AUTORUN_DISABLED_NAME))
        except OSError:
            pass
        materialized.append(app)
        result["updated" if existed else "created"].append(slug)
    manifest = dict(manifest or {})
    manifest["parent"] = parent_app
    manifest["apps"] = materialized
    _write_json(manifest_path(parent_dir), manifest)
    return result


def apply_build_plan(manifest, build_limit=None, force_build_all=False):
    """Normalize which portfolio children become full builds.

    - `force_build_all=True` upgrades every selected child to build=true first.
    - `build_limit` then caps the number of build=true children deterministically,
      keeping the lowest build_priority values first and preserving input order as
      the secondary tiebreaker.

    Returns a new manifest dict.
    """
    out = dict(manifest or {})
    apps = [dict(app) for app in (out.get("apps") or [])]

    if force_build_all:
        for app in apps:
            app["build"] = True

    try:
        limit = None if build_limit is None else int(build_limit)
    except (TypeError, ValueError):
        limit = None

    if limit is not None and limit >= 0:
        build_indexes = [i for i, app in enumerate(apps) if app.get("build")]
        if len(build_indexes) > limit:
            ranked = sorted(
                build_indexes,
                key=lambda i: (
                    apps[i].get("build_priority") or 9999,
                    i,
                    apps[i].get("slug", ""),
                ),
            )
            keep = set(ranked[:limit])
            for i in build_indexes:
                apps[i]["build"] = i in keep

    apps.sort(key=lambda a: (
        0 if a.get("build") else 1,
        a.get("build_priority") or 9999,
        a.get("slug", "")))
    out["apps"] = apps
    return out
