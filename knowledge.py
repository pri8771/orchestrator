#!/usr/bin/env python3
"""
Lightweight retrieval-augmented knowledge injection (the "Tier-1 specialist"
from the design discussion) — stdlib only, no embeddings, no dependencies.

A knowledge base is a folder of curated markdown cheatsheets:

    .orchestrator/knowledge/<domain>/*.md

Each file may start with a keyword hint the retriever weights heavily:

    <!-- keywords: swiftui, navigationstack, @observable, state -->

At phase time we score every doc in the relevant domain against the phase's
text (purpose + original prompt + recent transcript) by keyword/term overlap and
splice the top few into the agent's context. This is what makes the agents
"know iOS" without training or fine-tuning a model: it's the existing models
plus curated, retrieved expertise.

Everything here is best-effort: a missing knowledge dir just yields "".
"""

import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))

# Per-file (kws, body_terms, body) cache keyed by (path, mtime). The query
# changes every call so scoring itself can't be cached, but the read+parse of
# each cheatsheet (unchanged between phases within a run) can be — this file
# set is small and rarely edited, so an mtime check is cheap insurance against
# ever serving a stale parse after an on-disk edit.
_DOC_CACHE = {}

_KW_RE = re.compile(r"<!--\s*keywords:\s*(.*?)\s*-->", re.IGNORECASE | re.DOTALL)
_WORD_RE = re.compile(r"[a-z0-9@._+]+")

# iOS/Swift markers that route a build to the iOS knowledge domain.
_IOS_MARKERS = (
    "ios", "iphone", "ipad", "swift", "swiftui", "xcode", "app store",
    "testflight", "swiftdata", "uikit", "spritekit", "storekit", "apple",
)

# Web-frontend markers.
_WEB_MARKERS = (
    "web app", "website", "react", "next.js", "nextjs", "vue", "svelte",
    "frontend", "browser", "tailwind", "typescript web", "spa ",
)

# Backend/server markers.
_BACKEND_MARKERS = (
    "backend", "server", "api", "rest", "endpoint", "database", "postgres",
    "auth", "express", "fastapi", "node.js", "microservice", "deploy",
)

# Phases that don't benefit from technical cheatsheets (pure product/planning
# talk). Everything else gets a retrieval attempt; irrelevant docs simply score
# 0 and nothing is injected.
_SKIP_PHASES = {"initial_discussion", "next_steps_small"}


def _knowledge_dir(orch_dir, domain):
    return os.path.join(orch_dir or HERE, "knowledge", domain)


def _has_marker(blob, markers):
    """Phrase markers (with a space) match as substrings; single-word markers
    match as whole tokens, so 'api' doesn't fire on 'capital' and 'ios' doesn't
    fire on 'radios'."""
    toks = _terms(blob)
    for m in markers:
        if " " in m:
            if m in blob:
                return True
        elif m in toks:
            return True
    return False


def domain_for(cfg_domain, workflow_target, *texts):
    """Pick a knowledge domain. Explicit config wins; else infer from the
    workflow target + prompt; else 'general'."""
    if cfg_domain:
        return str(cfg_domain).strip()
    blob = " ".join(t for t in texts if t).lower()
    # Productionize is always about a backend/server.
    if workflow_target == "productionize":
        return "backend"
    if _has_marker(blob, _IOS_MARKERS):
        return "ios"
    if _has_marker(blob, _BACKEND_MARKERS) and not _has_marker(blob, _WEB_MARKERS):
        return "backend"
    if _has_marker(blob, _WEB_MARKERS):
        return "web"
    return "general"


def _terms(text):
    return set(_WORD_RE.findall((text or "").lower()))


def _doc_keywords(body):
    m = _KW_RE.search(body)
    kws = set()
    if m:
        for k in m.group(1).replace("\n", ",").split(","):
            k = k.strip().lower()
            if k:
                kws.add(k)
    # Also weight the headings (## ...) as soft keywords.
    for h in re.findall(r"^#{1,3}\s+(.+)$", body, re.MULTILINE):
        kws |= _terms(h)
    return kws


def _score(query_terms, kws, body_terms):
    """Keyword hits count triple; body-term overlap is a tie-breaker."""
    if not query_terms:
        return 0
    strong = len(query_terms & kws) * 3
    weak = len(query_terms & body_terms)
    return strong + weak


def should_inject(phase_key):
    return phase_key not in _SKIP_PHASES


def _load_doc(path):
    """(kws, body_terms, body) for one cheatsheet, cached by (path, mtime) so
    repeated retrieve() calls within a run don't re-read/re-parse every file
    from disk every time. Returns None on any read error."""
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        _DOC_CACHE.pop(path, None)
        return None
    cached = _DOC_CACHE.get(path)
    if cached is not None and cached[0] == mtime:
        return cached[1]
    try:
        with open(path, encoding="utf-8") as fh:
            body = fh.read()
    except OSError:
        return None
    parsed = (_doc_keywords(body), _terms(body), body)
    _DOC_CACHE[path] = (mtime, parsed)
    return parsed


def retrieve(orch_dir, domain, query_text, max_chars=6000, top_k=3):
    """Return concatenated markdown from the top-scoring docs in ``domain``
    (or "" if none are relevant / the dir is missing)."""
    d = _knowledge_dir(orch_dir, domain)
    if not os.path.isdir(d):
        return ""
    query_terms = _terms(query_text)
    scored = []
    for fn in sorted(os.listdir(d)):
        if not fn.endswith(".md"):
            continue
        parsed = _load_doc(os.path.join(d, fn))
        if parsed is None:
            continue
        kws, body_terms, body = parsed
        s = _score(query_terms, kws, body_terms)
        if s > 0:
            scored.append((s, fn, body))
    if not scored:
        return ""
    scored.sort(key=lambda x: (-x[0], x[1]))
    parts, used = [], 0
    for _s, fn, body in scored[:top_k]:
        # Strip the keyword hint comment from what the agent sees.
        clean = _KW_RE.sub("", body).strip()
        title = fn[:-3].replace("-", " ").replace("_", " ").title()
        chunk = "\n\n----- %s -----\n%s" % (title, clean)
        if used + len(chunk) > max_chars:
            chunk = chunk[: max(0, max_chars - used)]
        parts.append(chunk)
        used += len(chunk)
        if used >= max_chars:
            break
    if not parts:
        return ""
    header = ("\n\n===== RELEVANT KNOWLEDGE (curated %s reference — use it, "
              "don't restate it) =====" % domain.upper())
    return header + "".join(parts)


def available_domains(orch_dir):
    base = os.path.join(orch_dir or HERE, "knowledge")
    try:
        return sorted(n for n in os.listdir(base)
                      if os.path.isdir(os.path.join(base, n)))
    except OSError:
        return []
