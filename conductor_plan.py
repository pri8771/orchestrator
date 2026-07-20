"""Plan artifacts and per-step activity records for Conductor (V3 7.12).

This module is deliberately a small leaf around the existing routing engine:
plans enumerate RouteIntent effects, while conductor_routing remains the only
executor.  activity.jsonl is a distinct, best-effort trace (never events.jsonl)
with the same single-O_APPEND, redaction, and 3500-byte discipline as the
mistakes ledger.
"""
import datetime
import hashlib
import json
import os

import conductor_routing as crlib
import schemas


PLAN_FENCE = "plan-json"
ACTIVITY_FILENAME = "activity.jsonl"
ACTIVITY_STATUSES = ("started", "completed", "failed")
_MAX_LINE_BYTES = 3500
_MAX_FIELD_CHARS = 1000


def parse_plan_body(body, on_error=None):
    """Return the one validated step list, or None with a visible reason."""
    if on_error is None:
        on_error = lambda _m: None
    errors = []
    blocks = schemas.extract_structured_blocks(
        body or "", PLAN_FENCE, required_fields=["steps"],
        on_error=errors.append)
    if errors:
        for error in errors:
            on_error("plan refused: %s" % error)
        return None
    if len(blocks) != 1:
        on_error("plan refused: body must contain exactly one fenced "
                 "```plan-json``` block")
        return None
    steps = blocks[0].get("steps")
    if not isinstance(steps, list) or not steps:
        on_error("plan refused: plan-json.steps must be a non-empty list")
        return None
    out, seen = [], set()
    for index, step in enumerate(steps):
        ok, missing = schemas.validate_required_fields(
            step, ["id", "title"])
        if not ok:
            on_error("plan refused: steps[%d] missing required field(s): %s"
                     % (index, ", ".join(missing)))
            return None
        if not isinstance(step.get("id"), str) or not step["id"].strip():
            on_error("plan refused: steps[%d].id must be a non-empty string"
                     % index)
            return None
        sid = step["id"].strip()
        if sid in seen:
            on_error("plan refused: duplicate step id %r" % sid)
            return None
        seen.add(sid)
        title = step.get("title")
        target = step.get("target_section")
        expected = step.get("expected_artifact_type")
        if not isinstance(title, str) or not title.strip():
            on_error("plan refused: steps[%d].title must be a non-empty string"
                     % index)
            return None
        if not isinstance(target, str) or not target.strip():
            on_error("plan refused: steps[%d].target_section must be a "
                     "non-empty string" % index)
            return None
        if not isinstance(expected, str) or not expected.strip():
            on_error("plan refused: steps[%d].expected_artifact_type must be "
                     "a non-empty string" % index)
            return None
        clean = dict(step)
        clean.update(id=sid, title=title.strip(),
                     target_section=target.strip(),
                     expected_artifact_type=expected.strip())
        out.append(clean)
    return out


def render_plan_body(steps, title="Conductor execution plan"):
    payload = {"steps": [dict(step) for step in steps]}
    return ("# %s\n\n```plan-json\n%s\n```\n" %
            (title, json.dumps(payload, indent=2, sort_keys=True)))


def plan_key(intents):
    route_ids = sorted(str(intent.route_id) for intent in intents)
    blob = json.dumps(route_ids, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def steps_from_intents(intents, artifact_type):
    return [{"id": "step-%d" % (index + 1),
             "title": "Route %s to %s" % (intent.artifact_id,
                                            intent.target),
             "target_section": intent.target,
             "expected_artifact_type": artifact_type,
             "route_id": intent.route_id,
             "rule_id": intent.rule_id}
            for index, intent in enumerate(intents)]


def intents_from_steps(steps, context, plan_id):
    """Compile the body on disk, including human target edits, to intents."""
    out = []
    for step in steps:
        rule_id = step.get("rule_id")
        if not isinstance(rule_id, str) or not rule_id:
            rule_id = "plan:%s:%s" % (plan_id, step["id"])
        out.append(crlib.RouteIntent(
            context["artifact_id"], context["content_hash"],
            context["source_section"], step["target_section"], "chain",
            rule_id, crlib.ALLOW))
    return out


def _clean(value):
    if isinstance(value, str):
        try:
            value = schemas.redact_secrets(value)
        except Exception:
            pass
        return value[:_MAX_FIELD_CHARS]
    if isinstance(value, dict):
        return {str(key): _clean(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_clean(item) for item in value]
    return value


def _activity_key(record):
    return tuple(record.get(key) for key in
                 ("plan_id", "plan_version", "step_id", "status"))


def read_activity(directory):
    out = []
    try:
        with open(os.path.join(directory, ACTIVITY_FILENAME),
                  encoding="utf-8") as fh:
            for line in fh:
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                if isinstance(record, dict):
                    out.append(record)
    except OSError:
        pass
    return out


def append_activity(directory, record):
    """Append one deduplicated transition. Best effort and never raises."""
    try:
        if not directory or not isinstance(record, dict):
            return False
        status = record.get("status")
        if status not in ACTIVITY_STATUSES:
            return False
        required = ("plan_id", "plan_version", "step_id", "actor",
                    "action", "artifact_ids")
        if any(record.get(key) is None for key in required):
            return False
        clean = {"ts": datetime.datetime.now().astimezone().isoformat(
                    timespec="seconds")}
        clean.update({str(k): _clean(v) for k, v in record.items()
                      if k != "ts"})
        key = _activity_key(clean)
        if any(_activity_key(existing) == key
               for existing in read_activity(directory)):
            return True
        line = json.dumps(clean, ensure_ascii=False, default=str,
                          separators=(",", ":"))
        while len((line + "\n").encode("utf-8", "replace")) >= \
                _MAX_LINE_BYTES:
            longest = max((k for k, value in clean.items()
                           if isinstance(value, str)),
                          key=lambda k: len(clean[k]), default=None)
            if longest is not None and len(clean[longest]) > 16:
                clean[longest] = clean[longest][:len(clean[longest]) // 2] + "…"
            elif clean.get("artifact_ids"):
                clean["artifact_ids"] = clean["artifact_ids"][:-1]
            else:
                return False
            line = json.dumps(clean, ensure_ascii=False, default=str,
                              separators=(",", ":"))
        os.makedirs(directory, exist_ok=True)
        fd = os.open(os.path.join(directory, ACTIVITY_FILENAME),
                     os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
        try:
            os.write(fd, (line + "\n").encode("utf-8"))
        finally:
            os.close(fd)
        return True
    except Exception:
        return False


def append_activity_pair(root, session_dir, record):
    """The workspace trace and executing-session trace share one record."""
    workspace_ok = append_activity(os.path.join(root, ".conductor"), record)
    session_ok = append_activity(session_dir, record)
    return workspace_ok and session_ok
