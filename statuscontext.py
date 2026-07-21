#!/usr/bin/env python3
"""Truthful persisted run context for chat and answer workflows."""

import json
import os
from collections import deque

import costs


def _state(app_dir):
    try:
        with open(os.path.join(app_dir, "agent_state.json"), encoding="utf-8") as fh:
            value = json.load(fh)
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def _field(value):
    return value.strip() if isinstance(value, str) and value.strip() else "unknown"


def _cost(app_dir):
    records = costs.read_records(app_dir)
    if not records:
        return "unknown"
    totals = costs.rollup(app_dir)
    known = int(totals.get("cost_micro_usd") or 0)
    unknown = int(totals.get("unmetered_turns") or 0) \
        + int(totals.get("unpriced_turns") or 0)
    metered = int(totals.get("metered_turns") or 0)
    if metered:
        label = "$%.2f" % (known / 1000000.0)
        return ("at least %s; %d unmetered/unpriced turn(s)" % (label, unknown)
                if unknown else label)
    return "unknown (%d unmetered/unpriced turn(s))" % unknown


def _headline(event):
    kind = _field(event.get("kind"))
    details = []
    for key in ("phase", "agent", "status", "target", "detail", "reason"):
        value = event.get(key)
        if isinstance(value, str) and value.strip():
            details.append("%s=%s" % (key, value.strip().replace("\n", " ")[:240]))
    ts = event.get("ts")
    prefix = "[%s] " % ts.strip() if isinstance(ts, str) and ts.strip() else ""
    return prefix + kind + ((" — " + "; ".join(details)) if details else "")


def _recent_events(app_dir, limit=10):
    rows = deque(maxlen=max(1, int(limit)))
    try:
        with open(os.path.join(app_dir, "events.jsonl"), encoding="utf-8") as fh:
            for line in fh:
                try:
                    value = json.loads(line)
                except ValueError:
                    continue
                if isinstance(value, dict):
                    rows.append(value)
    except OSError:
        return []
    return list(rows)


def render(app_dir, event_limit=10):
    """Render observed state only; absent data is always labeled unknown."""
    state = _state(app_dir)
    completed = state.get("completed_phases")
    completed_label = str(len(completed)) if isinstance(completed, list) else "unknown"
    events = _recent_events(app_dir, limit=event_limit)
    lines = [
        "===== CURRENT PROJECT STATUS (persisted engine evidence) =====",
        "Status: %s" % _field(state.get("status")),
        "Current phase: %s" % _field(state.get("current_phase")),
        "Completed phases: %s" % completed_label,
        "Recorded cost: %s" % _cost(app_dir),
        "Recent event headlines (oldest to newest; at most %d):" % event_limit,
    ]
    if events:
        lines.extend("- " + _headline(event) for event in events)
    else:
        lines.append("- none observed")
    return "\n".join(lines)
