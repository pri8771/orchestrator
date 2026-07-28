#!/usr/bin/env python3
"""Minimal YAML reader for config.yaml (stdlib only).

Extracted from orchestrator.py to begin decomposing that monolith: this is a
self-contained, dependency-free unit (nested 2-space maps, scalar coercion, and
inline `#` comments) that the engine imports back. Kept intentionally small —
config.yaml uses only nested maps and scalars; anything richer (lists) lives in
JSON handled by `json`.
"""


def coerce_scalar(raw):
    """Coerce a raw scalar string to bool/int/float/None/str, stripping matched
    surrounding quotes. An unmatched leading quote is dropped (rather than kept
    as a corrupting prefix) and the remainder coerced."""
    s = raw.strip()
    if (len(s) >= 2) and ((s[0] == s[-1] == '"') or (s[0] == s[-1] == "'")):
        return s[1:-1]
    if s[:1] in ('"', "'"):
        s = s[1:].strip()   # unmatched leading quote -> don't keep it verbatim
    low = s.lower()
    if low == "true":
        return True
    if low == "false":
        return False
    if low in ("null", "~", ""):
        return None
    try:
        return int(s)
    except ValueError:
        pass
    try:
        return float(s)
    except ValueError:
        pass
    return s


def strip_inline_comment(val):
    """Drop a trailing ` # comment` from a scalar value (config.yaml documents
    `# comments` as supported), while respecting quotes so a `#` inside a quoted
    string stays data."""
    if not val:
        return val
    if val[0] == "#":
        return ""                       # value is only a comment -> null
    if val[0] in ('"', "'"):
        q = val[0]
        end = val.find(q, 1)
        if end == -1:
            return val                  # unterminated quote — leave for coerce_scalar
        rest = val[end + 1:]
        cut = rest.find("#")
        if cut != -1 and (cut == 0 or rest[cut - 1] == " "):
            return (val[:end + 1] + rest[:cut]).rstrip()
        return val
    i = val.find(" #")                   # YAML: a comment needs whitespace before #
    return val[:i].rstrip() if i != -1 else val


def parse_min_yaml(text):
    """Parse the restricted YAML subset used by config.yaml (nested maps only)."""
    root = {}
    # stack of (indent, container)
    stack = [(-1, root)]
    # `key:  # comment` is ambiguous mid-parse: a commented-out scalar (null,
    # like real YAML) or a comment-annotated section header (map). Open a
    # child map either way, remember the triple, and decide at the end —
    # only a triple whose map stayed empty (no deeper lines) demotes to None.
    comment_only = []
    for rawline in text.splitlines():
        if not rawline.strip() or rawline.lstrip().startswith("#"):
            continue
        indent = len(rawline) - len(rawline.lstrip(" "))
        line = rawline.strip()
        if ":" not in line:
            continue
        key, _, val = line.partition(":")
        key = key.strip()
        raw_val = val.strip()
        val = strip_inline_comment(raw_val)
        # pop to correct parent
        while stack and indent <= stack[-1][0]:
            stack.pop()
        parent = stack[-1][1]
        if val == "":
            child = {}
            parent[key] = child
            stack.append((indent, child))
            if raw_val.startswith("#"):
                comment_only.append((parent, key, child))
        else:
            parent[key] = coerce_scalar(val)
    for parent, key, child in comment_only:
        # `is child` guards against a later duplicate key having replaced it.
        if parent.get(key) is child and not child:
            parent[key] = None
    return root
