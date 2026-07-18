"""Per-project structured event log (Native Pro spec §6, milestone M4).

The engine appends one JSON object per line to ``<app_dir>/events.jsonl`` at
its existing decision points (run/phase/turn transitions, fallback rescues,
verify results, agent auto-disable). Fallbacks used to be visible only as
transcript prose the GUI had to parse — this file is the machine-readable
surface any UI can tail instead. Surfacing, not new logic.

Event kinds emitted by orchestrator.py:

    run_started / run_finished
    phase_started / phase_completed
    turn_started / turn_completed
    agent_fallback      (from_model, to_model, reason, status)
    agent_disabled      (agent, reason — e.g. the gemini startup probe)
    verify_result       (status, detail)
    url_fetched         (url, title, chars — prompt-URL ground truth, urlfetch.py)
    url_fetch_failed    (url, reason)
    release_gate_timing (gate, seconds — wall-clock inside each release gate)
    conversation_round  (project, phase, round — a chat round opened)
    awaiting_human      (project, phase — engine blocked on the human's turn)
    step_in_requested / step_in_joined / step_in_missed
                        (project, phase, detail — live-debate join lifecycle)
    message_produced    (agent, model_used, status — one per DELIVERED reply)
    message_appended    (turn_id, content_path — one per messages.jsonl index
                         line; ids+paths only, the body lives in the phase .md)
    artifact_published  (artifact_id, type, version, path)
    artifact_routed     (route_id, artifact_id, target)
    artifact_consumed   (artifact_id, consumer, phase)
    route_proposed      (route_id, artifact_id, target)
    route_approved      (route_id, artifact_id, target)

Contract:
  * Best-effort by design — emitting an event must NEVER take a run down.
    ``emit_event`` swallows every exception and just returns False.
  * Appends are atomic per line: one write() of one "\\n"-terminated line on
    an O_APPEND handle, so concurrent writers (parallel build lanes) cannot
    interleave partial lines.
  * String fields are secret-redacted (schemas.redact_secrets, §17) and
    length-capped so an agent's stderr echoed into a reason can never leak a
    key or bloat the log.

Standard library only, matching the rest of the engine.
"""

import datetime as _dt
import json
import os
import types
import typing
import warnings

_schemalib: typing.Optional[types.ModuleType]
try:
    import schemas as _schemalib
except Exception:  # noqa: BLE001 - events must work even if schemas breaks
    _schemalib = None

EVENTS_FILENAME = "events.jsonl"

# The vocabulary above, exported so tests/GUI code can validate against it.
KINDS = (
    "run_started", "phase_started", "turn_started", "turn_completed",
    "agent_fallback", "agent_disabled", "verify_result",
    "url_fetched", "url_fetch_failed",
    "phase_completed", "run_finished",
    # Wall-clock spent inside each release gate (design_lint / visual_qa /
    # ui_crawl / adherence): the measurement that decides whether gate
    # parallelization is worth its complexity.
    "release_gate_timing",
    # V3 board 1.1 (conversational phases): a chat round opened, and the
    # engine is blocked waiting for the human's next message / end command.
    "conversation_round", "awaiting_human",
    # V3 board 1.7 ("Step in"): a human asked to join a live debate at the
    # next round barrier; joined = their message was folded into the round;
    # missed = the marker could not be honored (stale, or the phase ended
    # first — detail says whether a message is still queued).
    "step_in_requested", "step_in_joined", "step_in_missed",
    # V3 board 1.11: exactly one per DELIVERED agent reply (direct or
    # fallback-rescued) — agent is the roster id, model_used the model that
    # actually answered, status "direct" | "fallback". The GUI's per-message
    # attribution captions resolve from these instead of parsing transcript
    # prose (a fallback rescue re-enters the turn under a DIFFERENT id, so a
    # (phase, round, agent) join on turn_completed fails exactly when
    # attribution matters most).
    "message_produced",
    # V3 board 2.5 — the artifact-bus vocabulary, registered ahead of its
    # emitters so nothing trips the unknown-kind warning later. Payloads
    # are ids+paths ONLY (never message/artifact content — bodies live in
    # messages.jsonl / artifact files, and the 3500B line cap depends on
    # it). Consumers: message_appended = 2.4's messages.jsonl dual-write;
    # artifact_published/_routed/_consumed = the M4 artifact bus;
    # route_proposed/_approved = the M7 Conductor.
    "message_appended",
    "artifact_published", "artifact_routed", "artifact_consumed",
    "route_proposed", "route_approved",
)

_MAX_FIELD_CHARS = 500
# Keep a serialized event line under Linux's PIPE_BUF so concurrent appenders
# from parallel projects don't interleave mid-line in events.jsonl.
_MAX_EVENT_LINE_BYTES = 3500


def events_path(app_dir):
    return os.path.join(app_dir, EVENTS_FILENAME)


def _clean(value):
    """Redact + cap string field values, recursing into dicts/lists so a
    secret nested inside a structured field can't skip redaction; pass
    everything else through."""
    if isinstance(value, str):
        v = value
        if _schemalib is not None:
            try:
                v = _schemalib.redact_secrets(v)
            except Exception:  # noqa: BLE001 - redaction is defense in depth
                pass
        return v[:_MAX_FIELD_CHARS]
    if isinstance(value, dict):
        return {str(k): _clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_clean(v) for v in value]
    return value


def emit_event(app_dir, kind, **fields):
    """Append one structured event line to <app_dir>/events.jsonl.

    Returns True when the line was written, False otherwise. Never raises:
    a broken disk, a missing project dir, or an unserializable field must
    never crash a run (fields that json can't encode fall back to str()).
    None-valued fields are dropped so call sites can pass optionals freely.
    """
    try:
        if not app_dir or not kind:
            return False
        if kind not in KINDS:
            # A typo'd kind would still write successfully but stay invisible to
            # any UI/tooling that filters read_events(kinds=...) against KINDS —
            # surface it immediately instead of silently shipping an
            # unfilterable event. Still written below (best-effort, not blocked);
            # a warnings-as-errors test filter must not swallow the event itself.
            try:
                warnings.warn("emit_event: unknown kind %r (not in events.KINDS)" % kind,
                              stacklevel=2)
            except Warning:
                pass
        # tz-aware (local + offset) so event ordering is unambiguous across DST
        # boundaries, unlike a naive datetime.now().
        evt = {"ts": _dt.datetime.now().astimezone().isoformat(timespec="seconds"),
               "kind": str(kind)}
        for k, v in fields.items():
            if v is None:
                continue
            evt[str(k)] = _clean(v)
        line = json.dumps(evt, default=str)
        # POSIX guarantees an append() write is atomic only up to PIPE_BUF
        # (>=512; 4096 on Linux). A longer line can interleave with a concurrent
        # writer's line and corrupt events.jsonl. Per-field caps aren't enough
        # when there are many fields, so shrink the largest string field until
        # the whole line fits — keeping it valid JSON.
        while len(line.encode("utf-8", "replace")) > _MAX_EVENT_LINE_BYTES:
            longest = max((k for k, v in evt.items() if isinstance(v, str)),
                          key=lambda k: len(evt[k]), default=None)
            if longest is None or len(evt[longest]) <= 16:
                break  # non-string bloat we can't trim — accept best-effort
            evt[longest] = evt[longest][:len(evt[longest]) // 2] + "…"
            line = json.dumps(evt, default=str)
        with open(events_path(app_dir), "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
        return True
    except Exception:  # noqa: BLE001 - events are best-effort by contract
        return False


def read_events(app_dir, kinds=None):
    """Every parseable event, oldest first; corrupt/partial lines are skipped.

    ``kinds`` optionally filters to an iterable of kind strings. Never raises
    (missing file -> []). Handy for tests and any UI that doesn't tail."""
    out = []
    want = set(kinds) if kinds is not None else None
    try:
        with open(events_path(app_dir), encoding="utf-8") as fh:
            for raw in fh:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    evt = json.loads(raw)
                except ValueError:
                    continue
                if isinstance(evt, dict) and (want is None or evt.get("kind") in want):
                    out.append(evt)
    except OSError:
        pass
    return out
