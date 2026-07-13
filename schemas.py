#!/usr/bin/env python3
"""
schemas.py — shared structured-artifact schemas + parsing for Orchestrator V2.

One module every phase that emits or consumes a structured artifact imports, so
the field lists used in prompts and the parser used to read them can never drift
(V2 spec §14, and the completeness-audit gap "no shared output-contract/parsing
layer for the new structured phase artifacts").

Standard library only. Defines lightweight schemas (required-field lists) for the
V2 artifacts — VerifyResult, TaskItem, InterfaceItem, LiveLogEntry, Finding,
PhaseStructuredOutput, AgentIdentity, AgentHealth — plus:

    extract_structured_blocks(text, fence_name, required_fields, on_error=None)
    validate_required_fields(obj, required_fields)

Hard rule (gap 27 / §6.5): a fenced block that fails json.loads, isn't a dict, or
is missing a required field is COUNTED and reported via on_error — never silently
dropped the way the legacy finding parser did.
"""

import json
import re

SCHEMA_VERSION = 1


# ---------------------------------------------------------------------------
# Required-field lists per artifact (the single source of truth referenced by
# both prompt-generation and parsing).
# ---------------------------------------------------------------------------
REQUIRED_FIELDS = {
    "verify_result": ["ran", "ok", "status", "tool"],
    "task_item": ["id", "title", "owner_lane", "files", "status"],
    "interface_item": ["name", "kind", "language", "owning_lane"],
    "live_log_entry": ["ts", "lane", "agent", "kind", "summary"],
    "finding": ["source", "category", "severity", "title"],
    "phase_structured_output": ["phase", "doc_sections"],
    # decisions-json entries (the cross-phase DECISIONS LOG contract).
    "decision_item": ["id", "decision"],
    "agent_identity": ["id", "provider", "kind"],
    "agent_health": ["status"],
}

# Controlled vocabularies used for normalization/validation.
VERIFY_STATUS = ("verified", "failed", "unverified")
TASK_STATUS = ("pending", "claimed", "in_progress", "done", "failed", "blocked")
FINDING_SOURCE = ("audit", "code_review", "secret_scan")
FINDING_STATUS = ("open", "fixed", "wont_fix", "deferred")
SEVERITY = ("Critical", "High", "Med", "Low")
AGENT_STATUS = ("up", "down")


def validate_required_fields(obj, required_fields):
    """Return (ok, missing_fields). ``ok`` is False if ``obj`` is not a dict or
    any required key is absent (or present-but-None)."""
    if not isinstance(obj, dict):
        return False, list(required_fields)
    missing = [f for f in required_fields if obj.get(f) is None]
    return (len(missing) == 0), missing


_FENCE_CACHE = {}


def _fence_re(fence_name):
    """Regex matching the OPENING fence ```<fence_name>. Cached per fence.

    Only the opener is matched; extract_structured_blocks bounds the body at
    the next ``` itself. A lazy ```name (\\{.*?\\}) ``` regex had two failure
    modes: an array body never matched (silently dropped, no on_error), and an
    unclosed block's lazy match swallowed the next valid block of the same
    fence. The lookahead keeps prefix fences (`task` vs `task-json`) and inline
    prose mentions like ```task-json``` from matching as openers."""
    if fence_name not in _FENCE_CACHE:
        _FENCE_CACHE[fence_name] = re.compile(
            r"```" + re.escape(fence_name) + r"(?![\w\-`])")
    return _FENCE_CACHE[fence_name]


_LABELED_JSON_CACHE = {}


def _labeled_json_re(fence_name):
    """Fallback opener: a label line naming the block type directly above a
    plain ```json (or bare ```) fence. Matches the drifted shape
    `**portfolio-json:**` + newline + ````json` that the strict info-string
    fence misses (models move the name into prose surprisingly often)."""
    if fence_name not in _LABELED_JSON_CACHE:
        _LABELED_JSON_CACHE[fence_name] = re.compile(
            r"[^\n]*\b" + re.escape(fence_name) +
            r"\b[^\n]*\n+[ \t]*```(?:json)?[ \t]*\n")
    return _LABELED_JSON_CACHE[fence_name]


def extract_structured_blocks(text, fence_name, required_fields=None, on_error=None):
    """Extract every ```<fence_name>``` JSON object from ``text``.

    Returns the list of valid dicts. Every block that fails to parse, isn't a
    dict, is missing a required field, or lacks a closing fence is reported
    through ``on_error(message)`` (defaults to a no-op collector) rather than
    being silently skipped — the caller decides whether to warn, repair, or
    abort. Never raises.
    """
    if on_error is None:
        on_error = lambda _msg: None
    required_fields = required_fields or []
    text = text or ""
    open_re = _fence_re(fence_name)
    out = []
    counter = [0]

    def _parse_block(raw):
        counter[0] += 1
        i = counter[0]
        try:
            obj = json.loads(raw)
        except (ValueError, TypeError) as exc:
            on_error("block #%d in `%s` failed JSON parse: %s" % (i, fence_name, exc))
            return
        if not isinstance(obj, dict):
            on_error("block #%d in `%s` is not a JSON object" % (i, fence_name))
            return
        ok, missing = validate_required_fields(obj, required_fields)
        if not ok:
            on_error("block #%d in `%s` missing required field(s): %s"
                     % (i, fence_name, ", ".join(missing)))
            return
        out.append(obj)

    pos = 0
    while True:
        m = open_re.search(text, pos)
        if m is None:
            break
        close = text.find("```", m.end())
        if close < 0:
            counter[0] += 1
            on_error("block #%d in `%s` has no closing fence" % (counter[0], fence_name))
            break
        if open_re.match(text, close):
            # The next ``` opens another block of this fence: the current block
            # is truncated. Report it and re-scan from the new opener so the
            # following block still parses.
            counter[0] += 1
            on_error("block #%d in `%s` has no closing fence" % (counter[0], fence_name))
            pos = close
            continue
        _parse_block(text[m.end():close].strip())
        pos = close + 3
    if not out:
        # Format-drift fallback: accept a label line + generic json fence — but
        # ONLY when the strict fence matched nothing, so a correct block can
        # never be double-parsed.
        for m in _labeled_json_re(fence_name).finditer(text):
            close = text.find("```", m.end())
            if close < 0:
                on_error("labeled `%s` json block has no closing fence" % fence_name)
                break
            _parse_block(text[m.end():close].strip())
    return out


# ---------------------------------------------------------------------------
# Normalizers — coerce loose agent output into the controlled vocabularies so a
# downstream gate never sees an unexpected value. All best-effort, never raise.
# ---------------------------------------------------------------------------
def normalize_severity(value):
    v = str(value or "Med").strip().title()
    alias = {"Medium": "Med", "Moderate": "Med", "Info": "Low", "Informational": "Low"}
    v = alias.get(v, v)
    return v if v in SEVERITY else "Med"


def normalize_verify_status(result):
    """Map a raw verify dict to verified|failed|unverified (spec §15)."""
    if not isinstance(result, dict):
        return "unverified"
    if not result.get("ran"):
        return "unverified"
    return "verified" if result.get("ok") else "failed"


# ---------------------------------------------------------------------------
# Secret redaction (V2 spec §17). One shared function applied at the single
# call_agent() chokepoint so no persisted sink ever sees a raw secret an agent
# echoed. Best-effort pattern + entropy detection; never raises.
# ---------------------------------------------------------------------------
import math as _math

_SECRET_PATTERNS = [
    ("aws_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("google_key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b")),
    ("github_token", re.compile(r"\bgh[pousr]_[0-9A-Za-z]{36,}\b")),
    ("openai_key", re.compile(r"\bsk-[0-9A-Za-z_\-]{20,}\b")),
    ("slack_token", re.compile(r"\bxox[baprs]-[0-9A-Za-z\-]{10,}\b")),
    ("jwt", re.compile(r"\beyJ[0-9A-Za-z_\-]{10,}\.[0-9A-Za-z_\-]{10,}\.[0-9A-Za-z_\-]{10,}\b")),
    ("bearer", re.compile(r"(?i)\bBearer\s+[0-9A-Za-z._\-]{16,}")),
    ("auth_header", re.compile(r"(?i)\bAuthorization\s*:\s*\S+")),
    ("pem", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.DOTALL)),
    # Connection-string / URL userinfo credentials: scheme://user:pass@host.
    # Group 1 (scheme://user:) and group 2 (@) survive so the URL stays readable;
    # redact_secrets special-cases this name to redact only the password.
    ("url_userinfo", re.compile(r"(\b[a-z][a-z0-9+.\-]*://[^\s:/@'\"]+:)[^\s@'\"]+(@)")),
    # No leading \b before the alternation so embedded forms like client_secret
    # / access_token / refresh_token match too (the old \b required the key to
    # START the word, missing `client_secret=`).
    ("assignment", re.compile(r"(?i)(?:api[_-]?key|client[_-]?secret|access[_-]?token|refresh[_-]?token|secret|token|password|passwd)\b\s*[:=]\s*['\"]?([^\s'\"]{8,})['\"]?")),
]

# Shapes that look high-entropy but are legitimate and must NOT be redacted.
_ENTROPY_ALLOW = re.compile(
    r"^(?:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"  # uuid
    r"|[0-9a-f]{40}|[0-9a-f]{64})$", re.IGNORECASE)                       # sha1/sha256 hex
_TOKEN_RE = re.compile(r"[A-Za-z0-9+/=_\-]{24,}")

# Fenced code blocks — ALL of them, regardless of info string. The entropy
# fallback below is a heuristic and will false-positive on legitimate long
# identifiers (a git SHA fragment, a localization key, a base64 asset id) —
# fine to lose in ordinary transcript prose, but a false-positive redaction
# inside a code block corrupts content downstream parsing (or a human reading
# a code sample) depends on. Skipping every fence, not just ```*-json ones, is
# the deliberate design: (a) the strict labeled _SECRET_PATTERNS above still
# run over the ENTIRE text first, so a genuinely leaked key inside a code
# block is still caught; (b) extract_structured_blocks itself accepts a
# "labeled fallback" shape (a `**task-json:**` label line above a plain
# ```json / bare ``` fence) whose info string carries no -json marker, so any
# info-string filter here inevitably misses parseable JSON; (c) code blocks
# are exactly where long high-entropy identifiers are densest, making the
# heuristic most false-positive-prone there. Only the entropy guess is
# skipped inside them.
#
# Fence delimiters may be indented up to 3 spaces (markdown-legal, and models
# produce this when nesting a fence inside a list item). The close fence must
# be a LINE that is *only* the (optionally indented) ``` — a naive non-greedy
# ``` ... ``` DOTALL match instead stops at the FIRST ``` substring anywhere,
# including one embedded inside a JSON string value (e.g. a "snippet" field
# quoting a fenced ```python block) — that ends the recognized span early and
# runs the rest of the real block through the entropy fallback, corrupting
# it. Anchoring both ends to full lines side-steps that: an embedded fence
# inside a validly-escaped JSON string (newlines escaped as \n, not literal)
# never stands alone on its own line.
_CODE_FENCE_SPAN_RE = re.compile(
    r"^ {0,3}```[^\n]*\n.*?^ {0,3}```[ \t]*$", re.DOTALL | re.MULTILINE)


def _shannon_entropy(s):
    if not s:
        return 0.0
    counts = {}
    for ch in s:
        counts[ch] = counts.get(ch, 0) + 1
    n = len(s)
    return -sum((c / n) * _math.log2(c / n) for c in counts.values())


def redact_secrets(text):
    """Redact recognizable secrets in ``text``, replacing each with
    ``[REDACTED:<type>]``. Applies known key-shape patterns everywhere, then an
    entropy fallback for unlabeled high-entropy tokens (skipping UUIDs/hashes,
    and skipping the body of ALL fenced code blocks entirely — see
    _CODE_FENCE_SPAN_RE — since a false-positive entropy redaction there
    corrupts content downstream parsing depends on, not just prose).
    Returns the redacted string; never raises."""
    if not text:
        return text
    try:
        out = text
        for name, pat in _SECRET_PATTERNS:
            if name == "url_userinfo":
                out = pat.sub(r"\1[REDACTED:url_userinfo]\2", out)
            else:
                out = pat.sub("[REDACTED:%s]" % name, out)

        def _maybe(m):
            tok = m.group(0)
            if _ENTROPY_ALLOW.match(tok):
                return tok
            # Only redact genuinely high-entropy, mixed-class tokens.
            if _shannon_entropy(tok) >= 4.0 and any(c.isdigit() for c in tok) \
                    and any(c.isalpha() for c in tok):
                return "[REDACTED:high_entropy]"
            return tok

        fence_spans = [m.span() for m in _CODE_FENCE_SPAN_RE.finditer(out)]
        if not fence_spans:
            return _TOKEN_RE.sub(_maybe, out)
        # Run the entropy pass only on the gaps BETWEEN fenced spans, and
        # pass each fenced span through untouched (after the strict patterns
        # above already ran over the whole string, including these spans).
        pieces = []
        pos = 0
        for start, end in fence_spans:
            pieces.append(_TOKEN_RE.sub(_maybe, out[pos:start]))
            pieces.append(out[start:end])
            pos = end
        pieces.append(_TOKEN_RE.sub(_maybe, out[pos:]))
        return "".join(pieces)
    except Exception:
        return text
