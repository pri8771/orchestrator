#!/usr/bin/env python3
"""
memory.py — factory memory (V3 board 4.13): a directory of founder-pinned
markdown facts ("SwiftUI only", "RevenueCat for IAP") injected at the SAME
phase-start retrieval slot the knowledge/artifact-context precursors feed,
so knowledge, pulled artifacts (4.7), and pinned facts ride ONE seam.

A fact is <orch_dir>/memory/<name>.md with optional leading frontmatter:

    ---
    scope: global | project | section
    project: <name>     # required when scope=project
    section: <name>     # required when scope=section
    triggers: auth, refresh token, iap
    ---
    The fact body (markdown).

Scope filter: global always eligible; project only in that project; section
only in sessions of that section. Trigger filter: NO triggers = pinned
(always inject when in scope); with triggers, inject only when a trigger
term appears in the tokenized phase query. Budget precedence when over the
char cap: section survives before project before global (most specific
wins), deterministic file-name order within a tier. Every injected fact
carries a file+scope provenance header (R3 — no anonymous context).

Stdlib + miniyaml only (no PyYAML — frontmatter stays in the vendored
subset). A malformed fact is skipped and reported, never a crashed phase.
"""

import os
import re

import miniyaml

# The knowledge.py tokenizer, copied so a query term matches a trigger term
# exactly as it matches a knowledge keyword (no drift, no cross-module dep).
_WORD_RE = re.compile(r"[a-z0-9@._+]+")
MEMORY_HEADER = "\n\n===== FACTORY MEMORY (founder-pinned — binding) ====="
_SCOPES = ("global", "project", "section")
# path -> ((mtime_ns, size), fact-or-None): a stat-scan, not a re-read.
_FACT_CACHE: dict = {}


def _terms(text):
    return set(_WORD_RE.findall((text or "").lower()))


def memory_dir(orch_dir):
    return os.path.join(orch_dir, "memory")


def parse_fact(path, on_error=None):
    """Parse one memory fact: optional leading '---' frontmatter + body.
    Returns the fact dict, or None (malformed/invalid → reported, skipped —
    never a crash). scope defaults to 'global'; project/section are required
    for their scopes; triggers is a comma-separated scalar tokenized the way
    knowledge keywords are (miniyaml has no lists)."""
    if on_error is None:
        on_error = lambda _m: None
    name = os.path.basename(path)
    try:
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
    except OSError as exc:
        on_error("memory fact %r unreadable: %s" % (name, exc))
        return None
    fm, body = {}, text
    if text.startswith("---"):
        rest = text[3:]
        end = rest.find("\n---")
        if end < 0:
            on_error("memory fact %r: frontmatter has no closing '---'"
                     % (name,))
            return None
        try:
            parsed = miniyaml.parse_min_yaml(rest[:end])
        except Exception as exc:  # noqa: BLE001 — never crash a phase
            on_error("memory fact %r frontmatter unparseable: %s"
                     % (name, exc))
            return None
        fm = parsed if isinstance(parsed, dict) else {}
        body = rest[end + 4:].lstrip("\n")
    scope = str(fm.get("scope", "global")).strip().lower() or "global"
    if scope not in _SCOPES:
        on_error("memory fact %r: invalid scope %r (global|project|section)"
                 % (name, scope))
        return None
    project = str(fm.get("project", "")).strip()
    section = str(fm.get("section", "")).strip()
    if scope == "project" and not project:
        on_error("memory fact %r: scope=project requires a 'project'" % (name,))
        return None
    if scope == "section" and not section:
        on_error("memory fact %r: scope=section requires a 'section'" % (name,))
        return None
    triggers = set()
    for part in str(fm.get("triggers", "")).split(","):
        triggers |= _terms(part)
    return {"file": name, "scope": scope, "project": project,
            "section": section, "triggers": triggers,
            "body": body.rstrip("\n")}


def _cached_fact(path, on_error):
    try:
        st = os.stat(path)
        key = (st.st_mtime_ns, st.st_size)
    except OSError:
        key = None
    cached = _FACT_CACHE.get(path)
    if key is not None and cached is not None and cached[0] == key:
        return cached[1]
    fact = parse_fact(path, on_error=on_error)
    if key is not None:
        _FACT_CACHE[path] = (key, fact)
    return fact


def retrieve(orch_dir, project, section, query_text, max_chars=3000,
             on_error=None):
    """The FACTORY MEMORY block of in-scope, triggered founder facts, or ''
    when nothing applies (so a memory-free run injects nothing and stays
    byte-identical). Facts are placed most-specific first (section, then
    project, then global) and dropped once the char budget is exhausted —
    the least specific loses the budget."""
    if on_error is None:
        on_error = lambda _m: None
    d = memory_dir(orch_dir)
    try:
        names = sorted(os.listdir(d))
    except OSError:
        return ""
    query_terms = _terms(query_text)
    tiers = {"section": [], "project": [], "global": []}
    for name in names:
        if not name.endswith(".md") or name.startswith("."):
            continue
        fact = _cached_fact(os.path.join(d, name), on_error)
        if fact is None:
            continue
        if fact["scope"] == "project" and fact["project"] != project:
            continue
        if fact["scope"] == "section" and (
                not section or fact["section"] != section):
            continue
        # No triggers = pinned (always inject in scope); else a trigger term
        # must appear in the tokenized query.
        if fact["triggers"] and not (fact["triggers"] & query_terms):
            continue
        tiers[fact["scope"]].append(fact)

    ordered = tiers["section"] + tiers["project"] + tiers["global"]
    if not ordered:
        return ""
    parts = [MEMORY_HEADER]
    used = len(MEMORY_HEADER)
    for fact in ordered:
        chunk = ("\n\n----- %s (%s) -----\n%s"
                 % (fact["file"], fact["scope"], fact["body"]))
        if used + len(chunk) > max_chars:
            # A fact is atomic — never inject a partial one. Placed
            # most-specific first, so this drops the least specific.
            break
        parts.append(chunk)
        used += len(chunk)
    if len(parts) == 1:
        return ""
    return "".join(parts)
