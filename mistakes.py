"""Per-project mistakes ledger — one consolidated, cross-run record of what
went wrong (mistakes.jsonl next to events.jsonl).

The engine already leaves failure signals scattered across verify_results.json,
.shepherd_retries, and events.jsonl; this file is the single machine-readable
place a human (or the `--mistakes` report) can ask "what keeps going wrong,
where, and with whom?" across every run of a project. Surfacing, not new logic.

Mistake classes appended by orchestrator.py:

    verify_failure        (a build compiled and FAILED)
    repair_queued         (release gate refused done; repair pass queued)
    quality_gate_fail     (phase closed despite a failing quality evaluation)
    agent_fallback        (a cloud turn was rescued/lost to the fallback ladder)
    contract_error        (tasks.json/interfaces.json parse or cycle errors)
    consensus_unverified  (integrator declared consensus while the verifier said NO)
    escalation_triggered  (adaptive escalation bumped effort/model after N
                           repeated failures on the SAME repair/quality-gate loop)

Contract (mirrors events.py exactly):
  * Best-effort by design — writing a ledger line must NEVER take a run down.
    ``append_mistake`` swallows every exception and just returns False.
  * Appends are atomic per line: one write() of one "\\n"-terminated line on an
    O_APPEND handle kept under PIPE_BUF, so concurrent writers (parallel build
    lanes) cannot interleave partial lines.
  * String fields are secret-redacted (schemas.redact_secrets, §17) and
    length-capped so an echoed stderr can never leak a key or bloat the file.

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
except Exception:  # noqa: BLE001 - the ledger must work even if schemas breaks
    _schemalib = None

MISTAKES_FILENAME = "mistakes.jsonl"

# The class vocabulary above, exported so tests/tooling can validate against it.
CLASSES = (
    "verify_failure", "repair_queued", "quality_gate_fail",
    "agent_fallback", "contract_error", "consensus_unverified",
    "escalation_triggered",
)

_MAX_FIELD_CHARS = 500
# Keep a serialized line under Linux's PIPE_BUF so concurrent appenders don't
# interleave mid-line (same bound as events.py).
_MAX_LINE_BYTES = 3500


def mistakes_path(app_dir):
    return os.path.join(app_dir, MISTAKES_FILENAME)


def _clean(value):
    """Redact + cap string field values; pass everything else through."""
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
    if isinstance(value, list):
        return [_clean(v) for v in value]
    return value


def append_mistake(app_dir, record):
    """Append one mistake line to <app_dir>/mistakes.jsonl.

    ``record`` is a dict with (all optional except cls/summary in practice):
    app, workflow, phase, agent, cls (one of CLASSES), summary (short string),
    detail (optional dict). A tz-aware ``ts`` is stamped here. Returns True
    when the line was written, False otherwise. Never raises — a broken disk
    or an unserializable field must never crash a run.
    """
    try:
        if not app_dir or not isinstance(record, dict):
            return False
        cls = str(record.get("cls") or "")
        if cls not in CLASSES:
            # Same stance as events.emit_event on an unknown kind: still write
            # it (best-effort), but surface the typo immediately.
            try:
                warnings.warn("append_mistake: unknown cls %r (not in mistakes.CLASSES)"
                              % cls, stacklevel=2)
            except Warning:
                pass
        # tz-aware (local + offset) so ledger ordering is unambiguous across
        # DST boundaries, matching events.py.
        rec = {"ts": _dt.datetime.now().astimezone().isoformat(timespec="seconds"),
               "cls": cls}
        for k in ("app", "workflow", "phase", "agent", "summary", "detail"):
            v = record.get(k)
            if v is None:
                continue
            rec[k] = _clean(v)
        line = json.dumps(rec, default=str)
        # Shrink the largest string field until the whole line fits under
        # PIPE_BUF, keeping it valid JSON (same loop as events.emit_event).
        while len(line.encode("utf-8", "replace")) > _MAX_LINE_BYTES:
            longest = max((k for k, v in rec.items() if isinstance(v, str)),
                          key=lambda k: len(rec[k]), default=None)
            if longest is None or len(rec[longest]) <= 16:
                if isinstance(rec.get("detail"), (dict, list)):
                    rec.pop("detail", None)  # non-string bloat — drop the detail
                    line = json.dumps(rec, default=str)
                    if len(line.encode("utf-8", "replace")) <= _MAX_LINE_BYTES:
                        break
                break  # accept best-effort
            rec[longest] = rec[longest][:len(rec[longest]) // 2] + "…"
            line = json.dumps(rec, default=str)
        with open(mistakes_path(app_dir), "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
        return True
    except Exception:  # noqa: BLE001 - the ledger is best-effort by contract
        return False


def load_mistakes(app_dir):
    """Every parseable ledger record, oldest first; corrupt/partial lines are
    skipped. Never raises (missing file -> [])."""
    out = []
    try:
        with open(mistakes_path(app_dir), encoding="utf-8") as fh:
            for raw in fh:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    rec = json.loads(raw)
                except ValueError:
                    continue
                if isinstance(rec, dict):
                    out.append(rec)
    except OSError:
        pass
    return out


def _bump(counts, key):
    key = str(key or "(unknown)")
    counts[key] = counts.get(key, 0) + 1


def aggregate_mistakes(root):
    """Aggregate every mistakes.jsonl under the direct child dirs of ``root``.

    Returns {"total": n, "by_class": {...}, "by_phase": {...}, "by_agent": {...},
    "apps": {app: {"total", "by_class", "by_phase", "by_agent"}}}. Records with
    no agent/phase are counted under "(unknown)" per-axis only when the field is
    present-but-empty is not distinguished — absent fields are simply skipped
    for that axis. Never raises (bad root -> empty aggregation)."""
    agg = {"total": 0, "by_class": {}, "by_phase": {}, "by_agent": {}, "apps": {}}
    try:
        names = sorted(os.listdir(root)) if root and os.path.isdir(root) else []
    except OSError:
        names = []
    for name in names:
        app_dir = os.path.join(root, name)
        if not os.path.isdir(app_dir) or not os.path.exists(mistakes_path(app_dir)):
            continue
        per = {"total": 0, "by_class": {}, "by_phase": {}, "by_agent": {}}
        for rec in load_mistakes(app_dir):
            per["total"] += 1
            agg["total"] += 1
            _bump(per["by_class"], rec.get("cls"))
            _bump(agg["by_class"], rec.get("cls"))
            if rec.get("phase"):
                _bump(per["by_phase"], rec.get("phase"))
                _bump(agg["by_phase"], rec.get("phase"))
            if rec.get("agent"):
                _bump(per["by_agent"], rec.get("agent"))
                _bump(agg["by_agent"], rec.get("agent"))
        agg["apps"][name] = per
    return agg
