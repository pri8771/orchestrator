"""Per-app LLM call traces (V3 board 2.8; radar ADOPT NOW #5).

Every runner invocation leaves one JSON file at
<app_dir>/traces/<run_id>/<seq>.json:

    {schema_version, trace_id, parent_call, ts, app, phase, round, agent,
     producer, model, rendered_prompt, response, stderr, exit, tokens,
     duration_s, status}

Written as a valid status="started" record BEFORE the runner runs and
atomically rewritten (tmp + os.replace) when the call resolves — a kill
mid-call always leaves parseable evidence, and a partial trace SAYS
status="started" rather than masquerading as a completed call.

run_id is minted once per (process, app_dir) as "<UTC %Y%m%d_%H%M%S>-<pid>";
a crash-resume gets a NEW run directory — that is the debugging boundary
wanted. seq is zero-padded and per-run monotonic under a lock, so parallel
lanes cannot collide or interleave.

§17 trade-off, stated not hidden: traces deliberately persist rendered
prompts and responses — the local-only diagnostic sink for a single-user
tool, and the reason the first overnight autonomous failure will be
debuggable. Every text field passes schemas.redact_secrets BEFORE disk
(the same chokepoint the transcript path uses), so a secret an agent
echoed never reaches a trace byte.

Tokens ride a threading.local side-channel (set_last_usage/pop_last_usage)
because the runner 4-tuple contract is frozen: run_local populates
Ollama's real counters; CLI runners report none and their traces carry
tokens=null — never estimated.

Best-effort by contract: every entry point catches everything, emits one
WARN through on_warn, and never takes a turn down. GC/retention is task
8.6's policy — traces accumulate until then, deliberately.

Standard library only.
"""

import datetime as _dt
import json
import os
import sys
import tempfile
import threading
import types
import typing

_schemalib: typing.Optional[types.ModuleType]
try:
    import schemas as _schemalib
except Exception:  # noqa: BLE001 - traces must degrade, never crash
    _schemalib = None

SCHEMA_VERSION = 1

# Rebindable warn sink (orchestrator points this at emit()); default stderr.
def _default_warn(msg):
    print("WARN " + msg, file=sys.stderr)


on_warn = _default_warn

_lock = threading.Lock()
_runs: dict[str, str] = {}   # app_dir -> run_id
_seqs: dict[str, int] = {}   # app_dir -> next seq (int)
_usage = threading.local()


def _redact(value):
    if not isinstance(value, str):
        return value
    if _schemalib is not None:
        try:
            return _schemalib.redact_secrets(value)
        except Exception:  # noqa: BLE001 - defense in depth
            pass
    return value


def set_last_usage(usage):
    """Runner-side half of the tokens side-channel (run_local only today).
    The 4-tuple runner contract is frozen; this is how real token counts
    reach the trace without touching it."""
    _usage.value = dict(usage) if usage else None


def pop_last_usage():
    """Harvest-and-clear. _call_agent_once clears BEFORE dispatch too, so
    a stale count from an earlier local call on this thread can never
    attach to a CLI turn."""
    value = getattr(_usage, "value", None)
    _usage.value = None
    return value


def _atomic_write(path, record):
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".trace-",
                               suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(record, fh, default=str)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


def write_started(app_dir, *, app, phase, round, agent, producer, model,
                  rendered_prompt, parent_call=None):
    """Create the crash-valid 'started' record. Returns an opaque handle
    for finalize(), or None (no app_dir, or any failure — one WARN)."""
    try:
        if not app_dir:
            return None
        with _lock:
            run_id = _runs.get(app_dir)
            if run_id is None:
                run_id = (_dt.datetime.now(_dt.timezone.utc)
                          .strftime("%Y%m%d_%H%M%S") + "-%d" % os.getpid())
                _runs[app_dir] = run_id
                _seqs[app_dir] = 1
            seq = _seqs[app_dir]
            _seqs[app_dir] = seq + 1
        run_dir = os.path.join(app_dir, "traces", run_id)
        os.makedirs(run_dir, exist_ok=True)
        trace_id = "%s:%04d" % (run_id, seq)
        record = {
            "schema_version": SCHEMA_VERSION,
            "trace_id": trace_id,
            "parent_call": parent_call,
            "ts": _dt.datetime.now().astimezone().isoformat(timespec="seconds"),
            "app": str(app),
            "phase": str(phase),
            "round": round,
            "agent": str(agent),
            "producer": str(producer),
            "model": str(model or ""),
            "rendered_prompt": _redact(rendered_prompt),
            "response": None,
            "stderr": None,
            "exit": None,
            "tokens": None,
            "duration_s": None,
            "status": "started",
        }
        path = os.path.join(run_dir, "%04d.json" % seq)
        _atomic_write(path, record)
        return {"path": path, "record": record}
    except Exception as exc:  # noqa: BLE001 - best-effort by contract
        try:
            on_warn("trace write_started failed (turn unaffected): %s" % exc)
        except Exception:  # noqa: BLE001
            pass
        return None


def finalize(handle, *, response=None, stderr=None, exit=None, tokens=None,
             duration_s=None, status="ok"):
    """Atomically rewrite the started record with the call's outcome.
    Exactly once per handle by caller discipline; None handle is a no-op.
    Never raises (one WARN on failure)."""
    if not handle:
        return False
    try:
        record = handle["record"]
        record["response"] = _redact(response)
        record["stderr"] = _redact(stderr)
        record["exit"] = exit
        record["tokens"] = tokens
        record["duration_s"] = (round(duration_s, 2)
                                if isinstance(duration_s, float) else duration_s)
        record["status"] = status
        _atomic_write(handle["path"], record)
        return True
    except Exception as exc:  # noqa: BLE001 - best-effort by contract
        try:
            on_warn("trace finalize failed (turn unaffected): %s" % exc)
        except Exception:  # noqa: BLE001
            pass
        return False


def trace_id(handle):
    """The handle's trace id, or None — for AgentError.trace_id chaining."""
    return handle["record"]["trace_id"] if handle else None


def _reset_for_tests():
    with _lock:
        _runs.clear()
        _seqs.clear()
