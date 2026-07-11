#!/usr/bin/env python3
"""
resilience.py — per-agent circuit breaker (V2 spec §4.2 / §11).

Pure functions over a small health dict so they unit-test without any subprocess
or clock dependency (the caller passes ``now`` as an epoch float). One state
machine reconciles "down for this run" with "auto-recover after cooldown": an
agent trips ``down`` after N consecutive failures with a ``retry_after`` deadline,
and a single half-open probe at each phase boundary clears or re-trips it.

Standard library only.
"""

FAILURE_THRESHOLD = 2          # consecutive failures before tripping
DEFAULT_COOLDOWN = 30 * 60     # generic failure cooldown (seconds)
RATE_LIMIT_COOLDOWN = 5 * 3600 # recognized rate-limit cooldown
MAX_COOLDOWN = 4 * 3600        # cap for the doubling backoff on generic failures

# Substrings (lowercased) that identify a provider rate-limit / quota exhaustion,
# so those fail fast with a long cooldown instead of being retried immediately.
_RATE_LIMIT_MARKERS = (
    "rate limit", "rate-limit", "ratelimit", "usage limit", "quota",
    "too many requests", "429", "5-hour limit", "5 hour limit",
    "resource_exhausted", "overloaded",
)


def new_health():
    """A fresh, healthy record for one agent."""
    return {
        "status": "up",
        "failure_signature": None,
        "consecutive_failures": 0,
        "retry_after": None,
        "last_error_excerpt": None,
    }


def classify_failure(exit_code, text):
    """Map a failing turn to a signature: rate_limit | timeout | malformed_output
    | crash. ``text`` is the combined stderr/stdout (or error message)."""
    t = (text or "").lower()
    if exit_code == 124 or "timed out" in t or "timeout" in t:
        return "timeout"
    if any(m in t for m in _RATE_LIMIT_MARKERS):
        return "rate_limit"
    if "malformed" in t or "schema" in t or "invalid json" in t:
        return "malformed_output"
    return "crash"


def record_failure(health, signature, now, err_excerpt=None):
    """Register a failure. Trips ``down`` with a cooldown once consecutive
    failures reach the threshold; the cooldown doubles per trip (capped), except a
    recognized rate-limit gets the fixed long cooldown. Mutates and returns
    ``health``."""
    health["consecutive_failures"] = int(health.get("consecutive_failures", 0)) + 1
    health["failure_signature"] = signature
    if err_excerpt is not None:
        health["last_error_excerpt"] = str(err_excerpt)[:300]
    if health["consecutive_failures"] >= FAILURE_THRESHOLD:
        if signature == "rate_limit":
            cooldown = RATE_LIMIT_COOLDOWN
        else:
            trips = health["consecutive_failures"] - FAILURE_THRESHOLD
            cooldown = min(MAX_COOLDOWN, DEFAULT_COOLDOWN * (2 ** trips))
        health["status"] = "down"
        health["retry_after"] = now + cooldown
    return health


def record_success(health):
    """Clear failure state back to healthy. Mutates and returns ``health``."""
    health["status"] = "up"
    health["failure_signature"] = None
    health["consecutive_failures"] = 0
    health["retry_after"] = None
    return health


def in_cooldown(health, now):
    """True if the agent is currently ``down`` and its cooldown hasn't elapsed."""
    if not health or health.get("status") != "down":
        return False
    ra = health.get("retry_after")
    return ra is not None and now < ra


def due_for_probe(health, now):
    """True if the agent is ``down`` but its cooldown has elapsed — eligible for a
    single half-open recovery probe at the next phase boundary."""
    if not health or health.get("status") != "down":
        return False
    ra = health.get("retry_after")
    return ra is not None and now >= ra


def health_map_for(agents):
    """A fresh health map for a list of agent ids."""
    return {a: new_health() for a in agents}
