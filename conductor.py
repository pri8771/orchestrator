"""Conductor skeleton (V3 7.1): the workspace-level autonomous loop.

Exactly ONE conductor runs per workspace (flock singleton, lock dies with
the process). It wakes on events.jsonl growth and polls per-session
agent_state.json — and that split is the load-bearing contract of the whole
design (plan §5): agent_state.json is AUTHORITATIVE; events.jsonl is a
WAKE-UP HINT ONLY. Event lines are size-capped and truncatable (events.py
shrinks them to stay under PIPE_BUF), so no decision may ever be made from
an event payload — the wake sweep touches only stat() metadata, never file
contents.

Persistence is two files under <root>/.conductor/ (NOT the card's literal
".orchestrator/": three downstream cards (7.6/7.7/7.9) and GLOSSARY.md
converge on .conductor/, it can't be confused with the ~/.orchestrator key
dir, and one gitignore rule covers the whole surface — recorded deviation):

  conductor_state.json   — loop stage (explicit enum, R4: never boolean
                           soup), ledger cursor, per-session digests.
                           Atomic tmp+os.replace writes.
  conductor_ledger.jsonl — append-only decision record. The ledger line is
                           appended (and fsynced) BEFORE the cursor in
                           conductor_state.json advances; a crash between
                           the two leaves an un-cursored tail that resume
                           reconciles by replay — never a lost or doubled
                           decision (§12 write ordering; 7.3 builds route
                           idempotency on exactly this discipline).

The skeleton only OBSERVES (ledger entries record authoritative-state
changes). Routing/minting is 7.2; idempotent route ids 7.3; permissions
7.4; termination 7.5; dials 7.6. Stdlib + leaf modules only — the one
orchestrator.py import (find_apps) is deferred into the function that needs
it, so orchestrator may some day import conductor without a cycle.
"""
import argparse
import datetime
import fcntl
import hashlib
import json
import os
import re
import shlex
import signal
import socket
import subprocess
import sys
import time

import sessions as seslib
import conductor_termination as ctlib
import pipeline_presets as pplib

CONDUCTOR_DIRNAME = ".conductor"
# V3 7.11: a marker the GUI (or any external caller) drops to request the
# active preset for the NEXT poll, without restarting the conductor process
# — the "equivalent state field for GUI launches" the card calls for
# alongside --pipeline. Consumed (deleted) the moment it's read, same as
# human_inbox.txt's peek-and-clear discipline.
PIPELINE_REQUEST_FILENAME = "pipeline_request.json"
OVERSIGHT_REQUEST_FILENAME = "oversight_request.json"
# R4 explicit lifecycle (precedent: sessions.STATUS): the stage is persisted
# on every transition so a kill at ANY point resumes into a known state.
STAGES = ("idle", "scanning", "evaluating", "acting")
# 7.5 terminal ledger decisions — replayed by reconcile_on_start to rebuild
# state['terminated'] after a crash between the termination append and its save.
_TERMINAL_DECISIONS = ("goal_met", "converged_open_items", "stalled")
# V3 7.11: replayed by reconcile_on_start to rebuild state['pipeline'] after a
# crash between the pipeline_loaded append and its state save (same class of
# gap 7.5a's termination replay fixed).
_PIPELINE_DECISIONS = ("pipeline_loaded",)
_PLAN_DECISIONS = ("plan_published", "plan_edited", "plan_executed",
                   "plan_rejected")
# Terminal route decisions rebuild the exactly-once cache from the ledger.
# Proposed/deferred/enqueue_failed/mint_failed are intentionally absent: they
# did not complete an effect and must remain eligible after rollback/restart.
_ROUTED_DECISIONS = (
    "route_approved", "route_recovered", "route_denied", "denied",
    "route_suppressed", "do_not_route_added", "kill_session",
    "converged", "budget_exhausted", "unroutable",
)
_SNAPSHOT_TAG_RE = re.compile(r"^conductor/(\d{8}T\d{6}Z)-(\d+)$")
_SNAPSHOT_SUBJECT_RE = re.compile(
    r"^conductor-snapshot: (.+) cursor=(\d+) ts=(\d{8}T\d{6}Z)$")
OVERSIGHT_DIALS = ("full_auto", "suggest_only", "gated", "loops_gated")
DEFAULT_OVERSIGHT_DIAL = "loops_gated"
SCHEMA_VERSION = 1
_MAX_LEDGER_LINE_BYTES = 3500   # same PIPE_BUF discipline as events.py


def _eval_crash(stage):
    """SIGKILL at a named routing boundary for the offline 7.9 eval suite.

    This is deliberately an environment-only, private seam: without the
    variable it is one comparison and has no observable production effect.
    SIGKILL (rather than an exception) is required to exercise the actual
    durability boundaries -- no finally block or broad routing catch may
    make the test gentler than a process crash.
    """
    if os.environ.get("ORCH_CONDUCTOR_EVAL_CRASH") == stage:
        os.kill(os.getpid(), signal.SIGKILL)

# The authoritative agent_state.json fields a conductor decision may read.
# Everything else (transcripts, event payloads) is either hint or content.
_DECISION_FIELDS = ("current_phase", "done", "error", "awaiting_approval",
                    "status", "consensus_status", "last_processed")


def conductor_dir(root):
    return os.path.join(root, CONDUCTOR_DIRNAME)


def _sections_dir():
    return os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "sections")


def lock_path(root):
    return os.path.join(conductor_dir(root), "conductor.lock")


def state_path(root):
    return os.path.join(conductor_dir(root), "conductor_state.json")


def ledger_path(root):
    return os.path.join(conductor_dir(root), "conductor_ledger.jsonl")


def _git(root, *args):
    """The only Git seam: delegate to orchestrator's timeout-safe helper."""
    from orchestrator import _git as engine_git
    return engine_git(root, *args)


def ensure_workspace_repo(root):
    """Idempotently make root snapshot-capable without rewriting history."""
    from orchestrator import _ensure_workspace_gitignore
    try:
        os.makedirs(root, exist_ok=True)
        _ensure_workspace_gitignore(root, snapshot=True)
        if os.path.isdir(os.path.join(root, ".git")):
            return True, ""
        code, _out, err = _git(root, "init", "-q")
        if code:
            return False, err or "git init failed"
        _ensure_workspace_gitignore(root, snapshot=True)
        code, _out, err = _git(
            root, "-c", "user.name=Orchestrator Conductor",
            "-c", "user.email=conductor@local", "commit", "-q",
            "--allow-empty", "-m", "conductor: workspace initialized")
        return code == 0, err if code else ""
    except Exception as exc:  # safety net must never take the run down
        return False, str(exc)


def _snapshot_failure(root, reason, error, emit=print):
    message = str(error or "unknown git failure")[:500]
    try:
        ledger_append(root, {
            "v": SCHEMA_VERSION, "ts": time.time(), "stage": "acting",
            "decision": "snapshot_failed",
            "detail": {"reason": str(reason)[:200], "error": message}})
    except Exception:
        pass
    try:
        import events as eventslib
        eventslib.emit_event(conductor_dir(root), "snapshot_failed",
                            reason=str(reason), error=message)
    except Exception:
        pass
    emit("conductor: snapshot failed (routing continues): %s" % message)


def _latest_snapshot_tag(root):
    code, out, _err = _git(root, "tag", "--list", "conductor/*",
                           "--sort=-creatordate")
    return out.splitlines()[0].strip() if code == 0 and out.strip() else None


def snapshot(root, reason, ledger_cursor, emit=print):
    """Commit+tag one dirty checkpoint; clean/failure paths never raise."""
    ok, err = ensure_workspace_repo(root)
    if not ok:
        _snapshot_failure(root, reason, err, emit)
        return None
    try:
        # Repair the only commit->tag crash gap before considering new work.
        code, subject, _err = _git(root, "log", "-1", "--format=%s")
        match = _SNAPSHOT_SUBJECT_RE.match(subject.strip()) if code == 0 else None
        if match:
            repair_tag = "conductor/%s-%s" % (match.group(3), match.group(2))
            if _git(root, "rev-parse", "-q", "--verify",
                    "refs/tags/%s" % repair_tag)[0] != 0:
                code, _out, err = _git(
                    root, "-c", "user.name=Orchestrator Conductor",
                    "-c", "user.email=conductor@local", "tag", "-a",
                    repair_tag, "-m", subject.strip())
                if code:
                    _snapshot_failure(root, "repair-tag", err, emit)
                    return None

        code, _out, err = _git(root, "add", "-A")
        if code:
            _snapshot_failure(root, reason, err, emit)
            return None
        code, _out, err = _git(root, "diff", "--cached", "--quiet")
        if code == 0:
            return _latest_snapshot_tag(root)
        if code != 1:
            _snapshot_failure(root, reason, err, emit)
            return None

        stamp = datetime.datetime.now(datetime.timezone.utc).strftime(
            "%Y%m%dT%H%M%SZ")
        cursor = int(ledger_cursor)
        stamp_dt = datetime.datetime.strptime(stamp, "%Y%m%dT%H%M%SZ").replace(
            tzinfo=datetime.timezone.utc)
        tag = "conductor/%s-%d" % (stamp, cursor)
        # Multiple dirty checkpoints can legitimately share a cursor (for
        # example two quiescent sessions before another route decision).
        # Preserve the required timestamp-cursor shape without overwriting.
        while _git(root, "rev-parse", "-q", "--verify",
                   "refs/tags/%s" % tag)[0] == 0:
            stamp_dt += datetime.timedelta(seconds=1)
            stamp = stamp_dt.strftime("%Y%m%dT%H%M%SZ")
            tag = "conductor/%s-%d" % (stamp, cursor)
        subject = "conductor-snapshot: %s cursor=%d ts=%s" % (
            str(reason).replace("\n", " ")[:120], cursor, stamp)
        code, _out, err = _git(
            root, "-c", "user.name=Orchestrator Conductor",
            "-c", "user.email=conductor@local", "commit", "-q", "-m", subject)
        if code:
            _snapshot_failure(root, reason, err, emit)
            return None
        code, _out, err = _git(
            root, "-c", "user.name=Orchestrator Conductor",
            "-c", "user.email=conductor@local", "tag", "-a", tag,
            "-m", subject)
        if code:
            _snapshot_failure(root, reason, err, emit)
            return None
        return tag
    except Exception as exc:
        _snapshot_failure(root, reason, exc, emit)
        return None


class ConductorLocked(Exception):
    """Another live conductor holds this workspace's flock."""


def _pid_alive(pid):
    # Local 5-liner rather than importing the 11k-line orchestrator module
    # for one helper (EPERM = alive-but-not-ours, same as everywhere else).
    try:
        os.kill(int(pid), 0)
        return True
    except PermissionError:
        return True
    except (OSError, TypeError, ValueError):
        return False


def acquire_singleton(root):
    """flock(LOCK_EX|LOCK_NB) held for the PROCESS LIFETIME — the classic
    single-instance idiom, deliberately simpler than acquire_app_lock's
    reclaim dance: a dead conductor's flock is released by the OS the
    instant the process dies, so staleness reclaim is unnecessary. The
    payload is for humans (`cat` shows who holds it); the flock is the
    truth — a leftover file with no holder blocks nobody."""
    os.makedirs(conductor_dir(root), exist_ok=True)
    fd = os.open(lock_path(root), os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        try:
            payload = os.read(fd, 200).decode("utf-8", "replace").strip()
        except OSError:
            payload = ""
        os.close(fd)
        raise ConductorLocked(
            "another conductor holds %s%s" %
            (lock_path(root), (" (%s)" % payload) if payload else ""))
    os.ftruncate(fd, 0)
    os.write(fd, ("pid=%d host=%s started=%s\n" % (
        os.getpid(), socket.gethostname(),
        time.strftime("%Y-%m-%d %H:%M:%S"))).encode("utf-8"))
    return fd


def release_singleton(fd):
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
    except OSError:
        pass


def route_digest(route_key):
    """The route's deterministic id (7.3). Delegates to conductor_routing so
    there is ONE definition shared by the ledger route_id, the mint request,
    and the already-routed cache key."""
    import conductor_routing as _cr
    return _cr.route_id_for(route_key)


def default_state():
    return {"schema_version": SCHEMA_VERSION, "stage": "idle",
            "ledger_cursor": 0, "sessions": {}, "routed": {},
            # 7.5: terminal sessions (routing skips them) + per-session
            # quiescence idle-counter records. Defensive-loaded like the rest.
            "terminated": {}, "quiescence": {},
            # 7.5b: workspace budget halt (stops ALL routing when set) + the
            # providers currently over their daily request quota (route defer).
            "halted": None, "over_quota": [],
            # 7.6: one workspace-level policy. The capability floor remains
            # independent and can only make this stricter, never looser.
            "oversight": {"dial": DEFAULT_OVERSIGHT_DIAL},
            "do_not_route": [],
            # 7.11: the active pipeline preset (None = normal per-section
            # routing/goal-manifest). Loaded ONCE from the request marker or
            # --pipeline and held in-memory/state — never re-read from the
            # preset FILE afterward, so a live edit to the file cannot mutate
            # a running Conductor's rules (the "run holds its own copy" gate).
            "pipeline": None,
            # 7.12: deterministic plan-key -> published/executed lifecycle.
            "plans": {}}


def load_conductor_state(root):
    """Defensive: missing/corrupt state file resumes from defaults — the
    ledger (not this cache) is the record of what happened (§6.2)."""
    try:
        with open(state_path(root), encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return default_state()
    except (OSError, ValueError) as exc:
        data = default_state()
        data["_oversight_fallback_pending"] = (
            "conductor state unreadable: %s" % exc)
        return data
    if not isinstance(data, dict) or data.get("stage") not in STAGES:
        fallback = default_state()
        fallback["_oversight_fallback_pending"] = (
            "conductor state shape invalid")
        return fallback
    data.setdefault("schema_version", SCHEMA_VERSION)
    data.setdefault("ledger_cursor", 0)
    data.setdefault("sessions", {})
    data.setdefault("routed", {})
    data.setdefault("terminated", {})   # 7.5: symmetry + defense-in-depth
    data.setdefault("quiescence", {})
    data.setdefault("halted", None)     # 7.5b: symmetry + defense-in-depth
    data.setdefault("over_quota", [])
    data.setdefault("pipeline", None)   # 7.11: symmetry + defense-in-depth
    data.setdefault("plans", {})        # 7.12: plan-key lifecycle cache
    oversight = data.get("oversight")
    if not isinstance(oversight, dict) or oversight.get("dial") not in \
            OVERSIGHT_DIALS:
        data["oversight"] = {"dial": DEFAULT_OVERSIGHT_DIAL}
        data["_oversight_fallback_pending"] = (
            "missing or invalid oversight.dial")
    do_not_route = data.get("do_not_route")
    if not isinstance(do_not_route, list):
        data["do_not_route"] = []
        data["_oversight_fallback_pending"] = (
            "invalid do_not_route list")
    if not isinstance(data["sessions"], dict) \
            or not isinstance(data["routed"], dict) \
            or not isinstance(data["terminated"], dict) \
            or not isinstance(data["quiescence"], dict) \
            or not isinstance(data["plans"], dict) \
            or not isinstance(data["ledger_cursor"], int) \
            or data["ledger_cursor"] < 0:
        fallback = default_state()
        fallback["_oversight_fallback_pending"] = (
            "conductor state fields invalid")
        return fallback
    return data


def oversight_dial(state):
    value = state.get("oversight") if isinstance(state, dict) else None
    dial = value.get("dial") if isinstance(value, dict) else None
    return dial if dial in OVERSIGHT_DIALS else DEFAULT_OVERSIGHT_DIAL


def route_revisits_section(intent, meta, lineage_metas):
    """True when a candidate sends content back into its section ancestry."""
    seen = {str(getattr(intent, "source_section", "") or "")}
    for ancestor_id in (meta.get("lineage") or []) if isinstance(meta, dict) else []:
        ancestor = lineage_metas.get(ancestor_id) if lineage_metas else None
        source = ancestor.get("source") if isinstance(ancestor, dict) else None
        section = source.get("section") if isinstance(source, dict) else None
        if section:
            seen.add(str(section))
    return bool(getattr(intent, "target", None) in seen)


def classify_route(route, dial, feedback=False, capability_exceeds=False):
    """Pure 7.6 dial matrix; capability escalation is an immutable floor."""
    dial = dial if dial in OVERSIGHT_DIALS else DEFAULT_OVERSIGHT_DIAL
    if capability_exceeds:
        return "needs_approval"
    if dial in ("suggest_only", "gated"):
        return "needs_approval"
    if dial == "loops_gated" and feedback:
        return "needs_approval"
    return "auto"


def classify_plan(dial, feedback=False, capability_exceeds=False):
    """Plan-wide 7.12 matrix; the strictest step controls the whole plan."""
    return classify_route(None, dial, feedback=feedback,
                          capability_exceeds=capability_exceeds)


def _do_not_route_pair(artifact_id, rule_id):
    return {"artifact_id": str(artifact_id or ""),
            "rule_id": str(rule_id or "")}


def _is_do_not_route(state, artifact_id, rule_id):
    pair = _do_not_route_pair(artifact_id, rule_id)
    return any(isinstance(item, dict) and item == pair
               for item in state.get("do_not_route", []))


def _remember_do_not_route(state, artifact_id, rule_id):
    pair = _do_not_route_pair(artifact_id, rule_id)
    if pair not in state.setdefault("do_not_route", []):
        state["do_not_route"].append(pair)
    return pair


def _pid_command(pid):
    try:
        return subprocess.run(
            ["ps", "-p", str(pid), "-o", "command="], capture_output=True,
            text=True, timeout=5).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def _clear_session_lock_if_matches(root, session_id, pid):
    """Remove only the stale lock that still names the pid we inspected."""
    try:
        from orchestrator import encode_lock_name
        path = os.path.join(root, ".orch-locks",
                            "%s.lock" % encode_lock_name(session_id))
        with open(path, encoding="utf-8") as fh:
            payload = fh.read()
        named = None
        for token in payload.replace("\n", " ").split():
            if token.startswith("pid="):
                named = int(token[4:])
                break
        if named == pid:
            os.remove(path)
    except (OSError, TypeError, ValueError):
        pass


def kill_spawned_session(root, session_id, grace_seconds=5.0, *,
                         kill_fn=os.kill, command_fn=_pid_command,
                         sleep_fn=time.sleep, now_fn=time.monotonic):
    """Stop one proven Conductor-minted engine without trusting a stale pid."""
    session_dir = os.path.realpath(os.path.join(root, str(session_id or "")))
    root_real = os.path.realpath(root)
    if not session_dir.startswith(root_real + os.sep):
        return {"status": "refused", "reason": "session path escapes workspace"}
    delegation = seslib.read_delegation(session_dir, on_error=lambda _m: None)
    request = delegation.get("request") if isinstance(delegation, dict) else None
    if not isinstance(request, dict) or not request.get("route_id"):
        return {"status": "refused",
                "reason": "session is not proven Conductor-minted"}
    pid = seslib.read_pidfile(session_dir)
    if not isinstance(pid, int) or pid <= 0:
        return {"status": "not_running", "reason": "no valid run.pid"}

    def alive():
        try:
            kill_fn(pid, 0)
            return True
        except PermissionError:
            return True
        except OSError:
            return False

    def clear_if_same():
        if seslib.read_pidfile(session_dir) == pid:
            seslib.remove_pidfile(session_dir)
        _clear_session_lock_if_matches(root, session_id, pid)

    if not alive():
        clear_if_same()
        return {"status": "not_running", "pid": pid,
                "reason": "stale pid cleared"}
    command = command_fn(pid)
    try:
        argv = shlex.split(command or "")
    except ValueError:
        argv = []
    if "orchestrator.py" not in " ".join(argv) or \
            not any(arg == session_id for arg in argv):
        return {"status": "refused", "pid": pid,
                "reason": "live pid does not match this orchestrator session"}
    try:
        kill_fn(pid, signal.SIGTERM)
    except OSError as exc:
        return {"status": "failed", "pid": pid, "reason": str(exc)}
    deadline = now_fn() + max(0.0, grace_seconds)
    while alive() and now_fn() < deadline:
        sleep_fn(min(0.1, max(0.0, deadline - now_fn())))
    if alive():
        # Re-check identity immediately before the irreversible escalation.
        if command_fn(pid) != command:
            return {"status": "refused", "pid": pid,
                    "reason": "pid identity changed during shutdown"}
        try:
            kill_fn(pid, signal.SIGKILL)
        except OSError as exc:
            return {"status": "failed", "pid": pid, "reason": str(exc)}
    clear_if_same()
    return {"status": "killed", "pid": pid,
            "reason": "SIGTERM with liveness-checked escalation"}


def save_conductor_state(root, state):
    """Atomic tmp+os.replace (save_state's pattern): a reader never sees a
    torn file, a crash mid-write leaves the previous state intact."""
    os.makedirs(conductor_dir(root), exist_ok=True)
    path = state_path(root)
    tmp = "%s.%d.tmp" % (path, os.getpid())
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(state, fh, indent=1, sort_keys=True)
        os.replace(tmp, path)
    except OSError:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def set_stage(root, state, stage):
    """Persist every stage transition — the kill-at-any-point contract."""
    if stage not in STAGES:
        raise ValueError("unknown stage %r" % (stage,))
    state["stage"] = stage
    save_conductor_state(root, state)
    return state


def ledger_append(root, rec):
    """Append one decision record, fsynced, and return the ledger's new
    length. UNLIKE events.emit_event this raises on failure: a decision the
    ledger couldn't record must stop the loop, not vanish (the ledger is
    the correctness record 7.3's idempotency replays). Oversized detail is
    truncated, never the record dropped."""
    os.makedirs(conductor_dir(root), exist_ok=True)
    line = json.dumps(rec, ensure_ascii=False)
    if len(line.encode("utf-8")) > _MAX_LEDGER_LINE_BYTES:
        rec = dict(rec)
        detail = str(rec.get("detail", ""))
        rec["detail"] = detail[:500] + "…[truncated]"
        line = json.dumps(rec, ensure_ascii=False)
    with open(ledger_path(root), "a", encoding="utf-8") as fh:
        fh.write(line + "\n")
        fh.flush()
        os.fsync(fh.fileno())
    return ledger_length(root)


def read_ledger(root):
    """All parseable ledger records, oldest first; malformed lines are
    counted in position (the cursor is a LINE index) but returned as None
    so replay logic can see the hole instead of silently renumbering."""
    out = []
    try:
        with open(ledger_path(root), encoding="utf-8") as fh:
            for line in fh:
                try:
                    rec = json.loads(line)
                except ValueError:
                    out.append(None)
                    continue
                out.append(rec if isinstance(rec, dict) else None)
    except OSError:
        return []
    return out


def ledger_length(root):
    try:
        with open(ledger_path(root), "rb") as fh:
            return sum(1 for _ in fh)
    except OSError:
        return 0


def read_session_state(app_dir, warn=print):
    """One session's authoritative agent_state.json, defensively: absent
    file means a never-run session (None, silent); a CORRUPT file is
    skipped with a visible warning, never a crash and never a guess."""
    path = os.path.join(app_dir, "agent_state.json")
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as exc:
        warn("conductor: skipping %s — unreadable agent_state.json (%s)"
             % (app_dir, exc))
        return None
    return data if isinstance(data, dict) else None


def session_digest(state):
    """Stable digest over ONLY the decision-relevant authoritative fields —
    the change detector that drives ledger observations. Event payloads
    never enter this function by construction."""
    view = {k: state.get(k) for k in _DECISION_FIELDS}
    blob = json.dumps(view, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def discover_sessions(root):
    """Nested + flat session ids via the engine's own discovery — the one
    deliberate orchestrator.py import (deferred: keeps conductor importable
    without executing the engine module until a poll actually runs, and
    cycle-proof if orchestrator ever imports conductor)."""
    from orchestrator import find_apps
    return list(find_apps(root))


def wake_signal(root, session_ids, sizes):
    """STAT-ONLY growth sweep of each session's events.jsonl — the cheap
    tick. Mutates `sizes` (path -> last seen size) and returns True when
    any file grew or appeared. Never opens a file: idle cost is bounded by
    stat() calls, and unreadable contents can't matter because contents are
    never read here (they're a hint, not evidence)."""
    grew = False
    live = {os.path.join(root, sid, "events.jsonl") for sid in session_ids}
    for path in [p for p in sizes if p not in live]:
        del sizes[path]   # session disappeared: stop tracking its file
    for sid in session_ids:
        path = os.path.join(root, sid, "events.jsonl")
        try:
            size = os.path.getsize(path)
        except OSError:
            size = -1
        if size > sizes.get(path, -1):
            grew = True
        sizes[path] = size
    return grew


def reconcile_on_start(root, state, emit=print):
    """Resume-by-replay: a crash between ledger append and cursor advance
    leaves cursor < ledger length. The tail entries already happened —
    re-apply their session digests to the cache (dedupe: an entry for a
    session we already track at the same digest is a no-op) and advance the
    cursor. The ledger is never rewritten (§6.2)."""
    length = ledger_length(root)
    cursor = state["ledger_cursor"]
    if cursor > length:
        # A cursor ahead of the ledger means the ledger was truncated
        # externally — say so and re-anchor rather than replaying garbage.
        emit("conductor: cursor %d beyond ledger length %d — re-anchoring "
             "(ledger was externally modified). Clearing the digest cache "
             "so every session is re-observed and the ledger is rebuilt."
             % (cursor, length))
        state["ledger_cursor"] = length
        state["sessions"] = {}
        save_conductor_state(root, state)
        return state
    if cursor == length:
        return state
    tail = read_ledger(root)[cursor:length]
    applied = 0
    for rec in tail:
        if rec is None:
            continue   # malformed line: hole is visible, position preserved
        sid = rec.get("session")
        digest = rec.get("digest")
        if isinstance(sid, str) and isinstance(digest, str):
            if state["sessions"].get(sid) != digest:
                state["sessions"][sid] = digest
                applied += 1
        # 7.5: a termination decision in the un-cursored tail already happened
        # and is durable in the ledger; rebuild its effect (state['terminated'])
        # so a crash between the termination append and the state save does not
        # resurrect a terminated session into re-evaluation / a duplicate
        # terminal record. Idempotent: replaying it just re-sets the same entry.
        if isinstance(sid, str) and rec.get("decision") in _TERMINAL_DECISIONS:
            detail = rec.get("detail") if isinstance(rec.get("detail"),
                                                     dict) else {}
            state.setdefault("terminated", {})[sid] = {
                "reason": (detail.get("reason") or rec.get("decision")),
                "report": detail.get("report"), "ts": rec.get("ts")}
            applied += 1
        # 7.5b: a workspace budget halt in the tail also already happened —
        # rebuild state['halted'] so routing stays stopped after a crash
        # between the budget_exhausted append and its save.
        if rec.get("decision") == "budget_exhausted" and not state.get("halted"):
            detail = rec.get("detail") if isinstance(rec.get("detail"),
                                                     dict) else {}
            state["halted"] = {"reason": detail.get("reason"),
                               "ts": rec.get("ts")}
            applied += 1
        # 7.11: a pipeline_loaded decision in the tail already happened.
        # The ledger stores only the preset's PATH (the normalized preset
        # itself could exceed the line-size cap and get truncated, which
        # would corrupt a naive replay) — re-validate fresh from that path to
        # rebuild state['pipeline']. This is a RESTART, not a live mutation:
        # re-reading here doesn't violate "a live run holds its own copy"
        # (that guarantee is about NOT re-reading while a single process
        # keeps running, not about what a fresh restart re-derives).
        if rec.get("decision") in _PIPELINE_DECISIONS:
            detail = rec.get("detail") if isinstance(rec.get("detail"),
                                                     dict) else {}
            path = detail.get("preset_path")
            if isinstance(path, str) and path:
                preset, err = pplib.load_preset_file(
                    path, pplib.known_sections_in_workspace(_sections_dir()))
                if err:
                    emit("conductor: resume could not re-validate pipeline "
                         "preset %r (%s) — pipeline NOT reactivated" %
                         (path, err))
                else:
                    state["pipeline"] = preset
                    applied += 1
        if rec.get("decision") in _PLAN_DECISIONS:
            detail = rec.get("detail") if isinstance(rec.get("detail"), dict) \
                else {}
            plan_key = detail.get("plan_key")
            if isinstance(plan_key, str) and plan_key:
                status = {"plan_published": "published",
                          "plan_edited": "approved",
                          "plan_executed": "executed",
                          "plan_rejected": "rejected"}[rec["decision"]]
                state.setdefault("plans", {})[plan_key] = {
                    "plan_id": detail.get("plan_id"),
                    "plan_version": detail.get("plan_version"),
                    "status": status}
                applied += 1
        if rec.get("decision") in ("oversight_fallback", "oversight_changed"):
            detail = rec.get("detail") if isinstance(rec.get("detail"), dict) else {}
            dial = detail.get("dial")
            if dial in OVERSIGHT_DIALS:
                state["oversight"] = {"dial": dial}
                state.pop("_oversight_fallback_pending", None)
                applied += 1
        if rec.get("decision") == "do_not_route_added":
            detail = rec.get("detail") if isinstance(rec.get("detail"), dict) else {}
            _remember_do_not_route(state, detail.get("artifact_id"),
                                   detail.get("rule_id"))
            applied += 1
        rid = rec.get("route_id")
        if isinstance(rid, str) and rid and \
                rec.get("decision") in _ROUTED_DECISIONS:
            state.setdefault("routed", {})[rid] = True
            applied += 1
    state["ledger_cursor"] = length
    save_conductor_state(root, state)
    emit("conductor: resumed — reconciled %d un-cursored ledger entr%s "
         "(%d applied)." % (len(tail), "y" if len(tail) == 1 else "ies",
                            applied))
    return state


def _record_oversight_setting(root, state, dial, decision, reason, emit=print):
    dial = dial if dial in OVERSIGHT_DIALS else DEFAULT_OVERSIGHT_DIAL
    rec = {"v": SCHEMA_VERSION, "ts": time.time(), "stage": "evaluating",
           "decision": decision,
           "detail": {"dial": dial, "reason": str(reason)[:500]}}
    new_len = ledger_append(root, rec)
    state["oversight"] = {"dial": dial}
    state.pop("_oversight_fallback_pending", None)
    state["ledger_cursor"] = new_len
    save_conductor_state(root, state)
    emit("conductor: oversight=%s (%s)" % (dial, reason))


def sync_oversight_from_disk(root, state, emit=print):
    """Read back the GUI-edited dial at each wake; ledger before cache save."""
    pending = state.get("_oversight_fallback_pending")
    if pending:
        _record_oversight_setting(
            root, state, DEFAULT_OVERSIGHT_DIAL, "oversight_fallback",
            pending, emit)
        return oversight_dial(state)
    request_path = os.path.join(conductor_dir(root),
                                OVERSIGHT_REQUEST_FILENAME)
    if os.path.exists(request_path):
        try:
            with open(request_path, encoding="utf-8") as fh:
                request = json.load(fh)
            requested = request.get("dial") if isinstance(request, dict) else None
        except (OSError, ValueError):
            requested = None
        if requested not in OVERSIGHT_DIALS:
            _record_oversight_setting(
                root, state, DEFAULT_OVERSIGHT_DIAL, "oversight_fallback",
                "invalid oversight request", emit)
        elif requested != oversight_dial(state):
            _record_oversight_setting(
                root, state, requested, "oversight_changed",
                "workspace dial changed", emit)
        try:
            os.remove(request_path)
        except OSError:
            pass
        return oversight_dial(state)
    try:
        with open(state_path(root), encoding="utf-8") as fh:
            raw = json.load(fh)
    except FileNotFoundError:
        return oversight_dial(state)
    except (OSError, ValueError) as exc:
        _record_oversight_setting(
            root, state, DEFAULT_OVERSIGHT_DIAL, "oversight_fallback",
            "state unreadable while refreshing dial: %s" % exc, emit)
        return DEFAULT_OVERSIGHT_DIAL
    value = raw.get("oversight") if isinstance(raw, dict) else None
    dial = value.get("dial") if isinstance(value, dict) else None
    if dial not in OVERSIGHT_DIALS:
        _record_oversight_setting(
            root, state, DEFAULT_OVERSIGHT_DIAL, "oversight_fallback",
            "missing or invalid oversight.dial", emit)
        return DEFAULT_OVERSIGHT_DIAL
    if dial != oversight_dial(state):
        _record_oversight_setting(
            root, state, dial, "oversight_changed", "workspace dial changed",
            emit)
    return oversight_dial(state)


def _record_termination(root, state, sid, reason, evidence, emit=print):
    """Persist one terminal outcome: a durable report file, a ledger line
    carrying the full evidence, and a state['terminated'] entry that makes the
    acting stage skip this session. Ledger-before-cursor like every other
    conductor decision — the conductor NEVER stops a session silently."""
    report = ctlib.write_report(root, sid, reason, evidence)
    if report is None:
        # The ledger line below is the authoritative terminal record; the
        # report file is a supplementary artifact. If it could not be written,
        # still ledger (never terminate silently) but say so loudly.
        emit("conductor: WARNING session %s termination report could not be "
             "written — the ledger line remains authoritative" % sid)
    rec = {"v": SCHEMA_VERSION, "ts": time.time(), "stage": "evaluating",
           "decision": reason, "session": sid,
           "detail": {"reason": reason, "report": report, "evidence": evidence}}
    new_len = ledger_append(root, rec)
    state["terminated"][sid] = {"reason": reason, "report": report,
                                "ts": rec["ts"]}
    state["ledger_cursor"] = new_len
    event_kind = {"stalled": "stalled",
                  "converged_open_items": "converged"}.get(reason)
    if event_kind:
        detail = evidence if isinstance(evidence, dict) else {}
        measurement = detail.get("reason") or reason
        if event_kind == "converged":
            idle = detail.get("idle_cycles")
            measurement = ("no new final artifacts for %s idle cycles" % idle
                           if idle is not None else "converged with open items")
        try:
            import events as eventslib
            parts = sid.split("/")
            eventslib.emit_event(
                os.path.join(root, sid), event_kind,
                project=parts[0] if parts else sid,
                section=parts[1] if len(parts) > 1 else "",
                session=sid, reason=str(measurement), evidence=detail)
        except Exception:
            pass
    if reason == "stalled":
        try:
            import orchestrator as orchlib
            session_state = read_session_state(os.path.join(root, sid),
                                               warn=lambda _m: None) or {}
            measurement = (evidence.get("reason")
                           if isinstance(evidence, dict) else None) or "stalled"
            orchlib.emit_failure_artifact(
                os.path.join(root, sid), "stalled",
                "Conductor stopped this session after %s; inspect the "
                "termination report and routing ledger." % measurement,
                session_state, {"root": root}, notify=False)
        except Exception as exc:
            emit("conductor: WARNING stalled failure artifact could not be "
                 "published (%s)" % type(exc).__name__)
    # Event precedes the cache save: a crash after it is replay-safe because
    # the terminal ledger line already exists; save-before-event could lose
    # the only notification forever when restart skips the terminal session.
    save_conductor_state(root, state)
    emit("conductor: session %s terminated (%s) -> %s" % (sid, reason, report))


def pipeline_request_path(root):
    return os.path.join(conductor_dir(root), PIPELINE_REQUEST_FILENAME)


def _consume_pipeline_request(root, state, emit=print):
    """Peek-and-clear the pipeline-request marker (same discipline as
    orchestrator.py's _peek_command_from_inbox): if present, drain it
    immediately regardless of outcome, so a broken request can't retry-loop
    forever. On a VALID preset, ledger `pipeline_loaded` (path + name only —
    the full preset could exceed the ledger's line-size cap) BEFORE updating
    state (crash-safe: reconcile_on_start replays this to rebuild
    state['pipeline']), then activate it. On an invalid preset, ledger the
    specific refusal and leave any PRIOR active preset untouched — the
    Conductor never partially applies a broken request."""
    path = pipeline_request_path(root)
    try:
        with open(path, encoding="utf-8") as fh:
            req = json.load(fh)
    except (OSError, ValueError):
        return state
    try:
        os.remove(path)
    except OSError:
        pass
    preset_path = req.get("preset_path") if isinstance(req, dict) else None
    if not isinstance(preset_path, str) or not preset_path:
        return state
    preset, err = pplib.load_preset_file(
        preset_path, pplib.known_sections_in_workspace(_sections_dir()))
    if err:
        rec = {"v": SCHEMA_VERSION, "ts": time.time(), "stage": "evaluating",
              "session": None, "decision": "pipeline_load_failed",
              "detail": {"preset_path": preset_path, "error": err}}
        new_len = ledger_append(root, rec)
        state["ledger_cursor"] = new_len
        save_conductor_state(root, state)
        emit("conductor: pipeline request refused — %s" % err)
        return state
    return _activate_pipeline(root, state, preset, preset_path, emit)


def _activate_pipeline(root, state, preset, preset_path, emit=print):
    """Ledger + activate an already-validated preset — shared by the
    request-marker path and --pipeline so both dedup identically. A no-op
    (no ledger line, no save) when the SAME normalized preset is already
    active: a restart with an unchanged --pipeline, or a reconcile-replay
    racing a fresh request for the same content, must never double-ledger
    'pipeline_loaded' (the ledger's own 'never a lost or doubled decision'
    invariant)."""
    if state.get("pipeline") == preset:
        emit("conductor: pipeline preset %r already active — no-op"
             % preset["preset_name"])
        return state
    rec = {"v": SCHEMA_VERSION, "ts": time.time(), "stage": "evaluating",
          "session": None, "decision": "pipeline_loaded",
          "detail": {"preset_path": preset_path,
                    "preset_name": preset["preset_name"]}}
    new_len = ledger_append(root, rec)
    state["pipeline"] = preset
    state["ledger_cursor"] = new_len
    save_conductor_state(root, state)
    emit("conductor: pipeline preset %r activated (%s)"
         % (preset["preset_name"], preset_path))
    return state


def _recent_section_providers(root, sessions):
    """{section_shortname: provider} — the provider each section has MOST
    recently been running on, from its sessions' costs.jsonl. The honest basis
    for 'this route would run on provider X' without coupling to model-routing:
    a section with no cost history is simply absent (we never defer a route we
    cannot attribute to an over-quota provider). ts is 'YYYY-MM-DD HH:MM:SS' so
    string comparison orders it."""
    import costs as costslib
    latest = {}   # section -> (ts, provider)
    for sid in sessions:
        parts = sid.split("/")
        if len(parts) < 2:
            continue
        section = parts[1]
        for rec in costslib.read_records(os.path.join(root, sid)):
            p, ts = rec.get("provider"), rec.get("ts")
            if isinstance(p, str) and p and isinstance(ts, str) \
                    and ts > latest.get(section, ("", ""))[0]:
                latest[section] = (ts, p)
    return {s: v[1] for s, v in latest.items()}


def _record_workspace_termination(root, state, reason, evidence, emit=print,
                                  source_session=None):
    """A WORKSPACE-level halt (7.5b budget exhaustion): one report + one ledger
    line, and state['halted'] so route_engine stops ALL routing this and every
    later poll. Not per-session — the whole autonomous run is capped."""
    report = ctlib.write_report(root, "__workspace__", reason, evidence)
    rec = {"v": SCHEMA_VERSION, "ts": time.time(), "stage": "evaluating",
           "decision": "budget_exhausted", "session": None,
           "detail": {"reason": reason, "report": report, "evidence": evidence}}
    new_len = ledger_append(root, rec)
    state["halted"] = {"reason": reason, "report": report, "ts": rec["ts"]}
    state["ledger_cursor"] = new_len
    try:
        import events as eventslib
        spend = evidence.get("spend_usd") if isinstance(evidence, dict) else None
        provider = evidence.get("provider") if isinstance(evidence, dict) else None
        measurement = reason.replace("_", " ")
        if isinstance(spend, dict) and spend:
            measurement += ": " + ", ".join(
                "%s $%s / $%s" % (name, values.get("used"), values.get("cap"))
                for name, values in sorted(spend.items())
                if isinstance(values, dict))
        else:
            evidence_key = {"turns_exhausted": "turns",
                            "wall_clock_exhausted": "wall_clock_s"}.get(reason)
            values = evidence.get(evidence_key) \
                if isinstance(evidence, dict) and evidence_key else None
            if isinstance(values, dict):
                measurement += ": %s used / %s cap" % (
                    values.get("used"), values.get("cap"))
        eventslib.emit_event(
            conductor_dir(root), "budget_exhausted", project="Workspace",
            section="all", budget=reason, reason=reason,
            provider=provider, measurement=measurement,
            spend=spend, evidence=evidence)
    except Exception:
        pass
    if source_session:
        try:
            import orchestrator as orchlib
            app_dir = os.path.join(root, source_session)
            session_state = read_session_state(
                app_dir, warn=lambda _m: None) or {}
            orchlib.emit_failure_artifact(
                app_dir, "budget_exhausted",
                "Conductor halted the workspace because %s; inspect the "
                "budget evidence and routing ledger."
                % reason.replace("_", " "),
                session_state, {"root": root}, notify=False)
        except Exception as exc:
            emit("conductor: WARNING budget failure artifact could not be "
                 "published (%s)" % type(exc).__name__)
    save_conductor_state(root, state)
    emit("conductor: WORKSPACE HALTED — %s (%s)" % (reason, report))


def evaluate_terminations(root, state, sessions, manifest, emit=print):
    """The four-layer termination stack, evaluated in the card's specified
    order goal -> stall -> budget -> quiescence. Goal/stall run PER SESSION
    first (so a session that finishes or stalls this poll always gets its
    record, even in the same poll a global budget also exhausts — a global
    halt must never swallow an already-earned goal_met/stalled outcome). THEN
    the global budget gate: a hard cap (turns / wall-clock / per-provider
    spend) halts ALL routing for every later poll; providers over their daily
    request quota are stashed for route deferral (soft, not a halt). Finally
    quiescence runs for whatever sessions are still open — moot once halted,
    since nothing will progress anyway, so it's skipped in that case.
    Idempotent (an already-terminated session is skipped); a session with no
    agent_state.json has not started and never terminates."""
    import costs as costslib
    terminated = state.setdefault("terminated", {})
    quiescence = state.setdefault("quiescence", {})
    stall_cfg = manifest.get("stall")

    # --- per-session: goal -> stall (runs BEFORE the global budget gate) --- #
    verdicts = {}
    for sid in sessions:
        if sid in terminated:
            continue
        app_dir = os.path.join(root, sid)
        sstate = read_session_state(app_dir, warn=emit)
        if sstate is None:
            continue   # not started — nothing to terminate
        verdict = ctlib.goal_predicate(app_dir, manifest, on_warn=emit)
        if verdict["met"]:
            _record_termination(root, state, sid, "goal_met", verdict, emit)
            continue
        verdicts[sid] = (verdict, sstate)
        if stall_cfg is not None:
            st = ctlib.stall_check(app_dir, sstate, stall_cfg, on_warn=emit)
            if st["stalled"]:
                _record_termination(root, state, sid, "stalled", st, emit)
                del verdicts[sid]

    # --- global budgets (7.5b) --------------------------------------------- #
    budgets = manifest.get("budgets")
    if budgets:
        records = []
        for sid in sessions:
            records.extend(costslib.read_records(os.path.join(root, sid)))
        bc = ctlib.budget_check(budgets, read_ledger(root), records,
                                time.time(), time.strftime("%Y-%m-%d"))
        state["over_quota"] = sorted(bc.get("over_quota") or [])
        if bc["exhausted"] and not state.get("halted"):
            budget_evidence = dict(bc["evidence"])
            if bc.get("provider"):
                budget_evidence["provider"] = bc["provider"]
            ordered_sessions = sorted(sessions)
            source_session = next((sid for sid in ordered_sessions
                                   if os.path.isfile(os.path.join(
                                       root, sid, "agent_state.json"))),
                                  ordered_sessions[0]
                                  if ordered_sessions else None)
            _record_workspace_termination(
                root, state, bc["reason"], budget_evidence, emit,
                source_session=source_session)
    if state.get("halted"):
        save_conductor_state(root, state)
        return state   # capped: no routing, quiescence is moot

    # --- quiescence, for sessions still open after goal/stall -------------- #
    limit = manifest.get("quiescence_cycles")
    if limit is not None:
        for sid, (verdict, sstate) in verdicts.items():
            app_dir = os.path.join(root, sid)
            record, converged = ctlib.quiescence_step(
                quiescence.get(sid), app_dir, limit,
                ctlib.progress_digest(sstate), on_warn=emit)
            quiescence[sid] = record
            if converged:
                report = ctlib.converged_report(app_dir, verdict, record,
                                                on_warn=emit)
                _record_termination(root, state, sid, "converged_open_items",
                                    report, emit)
                snapshot(root, "quiescence:%s" % sid,
                         state["ledger_cursor"], emit)
                # A failed snapshot appends a visible failure decision.
                state["ledger_cursor"] = ledger_length(root)
    save_conductor_state(root, state)
    return state


def full_poll(root, state, emit=print, route_engine=None):
    """One authoritative pass: scan -> evaluate (ledger observations for
    every changed session, APPEND-THEN-CURSOR each) -> idle. The skeleton
    records observations; when route_engine is injected, the acting
    stage routes this poll's admissible artifacts (7.2)."""
    sync_oversight_from_disk(root, state, emit)
    set_stage(root, state, "scanning")
    sessions = discover_sessions(root)
    # Prune digests for sessions that no longer exist: a long-lived
    # conductor otherwise accretes ghosts forever and every state write
    # scales with historical rather than live session count.
    ghosts = set(state["sessions"]) - set(sessions)
    for sid in ghosts:
        del state["sessions"][sid]
    set_stage(root, state, "evaluating")
    for sid in sessions:
        app_dir = os.path.join(root, sid)
        sstate = read_session_state(app_dir, warn=emit)
        if sstate is None:
            continue
        digest = session_digest(sstate)
        if state["sessions"].get(sid) == digest:
            continue
        live = seslib.read_pidfile(app_dir)
        rec = {"v": SCHEMA_VERSION, "ts": time.time(), "stage": "evaluating",
               "decision": "observed", "session": sid, "digest": digest,
               "detail": {k: sstate.get(k) for k in _DECISION_FIELDS},
               "runner_alive": bool(live and _pid_alive(live))}
        # §12 ordering: the ledger line is durable BEFORE the cursor moves;
        # a crash here is exactly what reconcile_on_start replays.
        new_len = ledger_append(root, rec)
        state["sessions"][sid] = digest
        state["ledger_cursor"] = new_len
        save_conductor_state(root, state)
    # V3 7.11: consume a pending pipeline-request marker (the GUI's "Run
    # pipeline" verb, or any external caller) before this poll's termination
    # stack, so a preset activated this cycle takes effect immediately.
    state = _consume_pipeline_request(root, state, emit)
    active_preset = state.get("pipeline")
    # V3 7.5: termination stack (goal -> quiescence; 7.5b inserts stall +
    # budget). Runs inside the evaluating stage, BEFORE acting, so a session
    # that terminated this poll is not routed into. Zero cost + zero ledger
    # noise when no manifest enables any layer.
    if active_preset:
        # 7.11: a preset's goal_manifest was fully validated at load time and
        # is held in-memory (state) — never re-read from the preset FILE, so
        # a live edit can't mutate a running Conductor's rules.
        manifest = active_preset["goal_manifest"]
    else:
        manifest, mstatus = ctlib.load_goal_manifest_ex(root, on_warn=emit)
        if mstatus == "ok" and isinstance(manifest.get("budgets"), dict):
            state["_last_good_budgets"] = manifest["budgets"]
        elif mstatus == "corrupt" and state.get("_last_good_budgets"):
            # A torn/corrupt write must not silently drop budgets that were
            # active a moment ago (deny-safe: never WIDEN by losing a cap).
            # goal/quiescence/stall stay disabled — that's the conservative
            # direction (never a false termination), only budgets need the
            # fallback (an uncapped run is the unsafe direction here).
            manifest = dict(manifest, budgets=state["_last_good_budgets"])
            emit("conductor: falling back to last-known budgets while "
                 "goal_manifest.json is corrupt")
    if (manifest.get("goal") or manifest.get("quiescence_cycles")
            or manifest.get("budgets") or manifest.get("stall")):
        state = evaluate_terminations(root, state, sessions, manifest, emit)
    # V3 7.2: act on this poll's admissible newly-final artifacts. Routing
    # is off unless a .conductor/routing enable + rules exist; the acting
    # stage owns NO dir-minting — every route mints through sessions
    # (route_engine is injected so tests need no live bus/subprocess).
    if route_engine is not None:
        set_stage(root, state, "acting")
        try:
            state = route_engine(root, state, sessions, emit)
        except Exception as exc:  # noqa: BLE001 - a routing fault must not
            # wedge the observation loop; it's ledgered and the poll ends.
            emit("conductor: routing error (loop continues): %s" % exc)
    set_stage(root, state, "idle")
    return state


def _live_conductor_sessions(root):
    """Return proven Conductor sessions whose pid is currently alive."""
    live = []
    for dirpath, dirnames, filenames in os.walk(root):
        if ".git" in dirnames:
            dirnames.remove(".git")
        if seslib.DELEGATION_BASENAME not in filenames:
            continue
        delegation = seslib.read_delegation(dirpath, on_error=lambda _m: None)
        request = delegation.get("request") if isinstance(delegation, dict) else None
        if not isinstance(request, dict) or not request.get("route_id"):
            continue
        pid = seslib.read_pidfile(dirpath)
        if not isinstance(pid, int) or pid <= 0:
            continue
        try:
            os.kill(pid, 0)
        except PermissionError:
            pass
        except OSError:
            continue
        live.append({"session": os.path.relpath(dirpath, root), "pid": pid})
    return sorted(live, key=lambda item: item["session"])


def _parse_snapshot_tag(tag):
    match = _SNAPSHOT_TAG_RE.match(str(tag or ""))
    if not match:
        return None
    return {"tag": tag, "timestamp": match.group(1),
            "cursor": int(match.group(2))}


def snapshot_tag_before(root, timestamp):
    """Newest well-formed snapshot tag at/before an ISO-8601 timestamp."""
    try:
        wanted = datetime.datetime.fromisoformat(
            str(timestamp).replace("Z", "+00:00"))
        if wanted.tzinfo is None:
            wanted = wanted.replace(tzinfo=datetime.timezone.utc)
        wanted = wanted.astimezone(datetime.timezone.utc)
    except (TypeError, ValueError):
        return None
    code, out, _err = _git(root, "tag", "--list", "conductor/*")
    if code:
        return None
    candidates = []
    for tag in out.splitlines():
        parsed = _parse_snapshot_tag(tag.strip())
        if not parsed:
            continue
        stamp = datetime.datetime.strptime(
            parsed["timestamp"], "%Y%m%dT%H%M%SZ").replace(
                tzinfo=datetime.timezone.utc)
        if stamp <= wanted:
            candidates.append((stamp, tag.strip()))
    return max(candidates)[1] if candidates else None


def _atomic_json(path, value):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = "%s.%d.tmp" % (path, os.getpid())
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(value, fh, sort_keys=True)
        fh.write("\n")
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def _truncate_ledger(root, cursor):
    path = ledger_path(root)
    try:
        with open(path, "rb") as fh:
            lines = fh.readlines()
    except FileNotFoundError:
        lines = []
    if cursor > len(lines):
        raise ValueError("tag cursor %d exceeds ledger length %d" %
                         (cursor, len(lines)))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = "%s.%d.tmp" % (path, os.getpid())
    with open(tmp, "wb") as fh:
        fh.writelines(lines[:cursor])
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def _rollback_journal_path(root):
    return os.path.join(conductor_dir(root), "rollback_pending.json")


def repair_pending_rollback(root):
    """Finish reset->truncate crash gaps from the ignored rollback journal."""
    path = _rollback_journal_path(root)
    try:
        with open(path, encoding="utf-8") as fh:
            record = json.load(fh)
        tag, cursor = record["tag"], int(record["cursor"])
    except FileNotFoundError:
        return None
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if _git(root, "reset", "--hard", tag)[0] != 0:
        return None
    try:
        _truncate_ledger(root, cursor)
        state = reconcile_on_start(root, default_state(), emit=lambda _m: None)
        save_conductor_state(root, state)
        os.remove(path)
        return state
    except (OSError, ValueError):
        return None


def rollback(root, tag, dry_run=True):
    """Dry-run by default; apply is crash-resumable and refuses live runs."""
    parsed = _parse_snapshot_tag(tag)
    if not parsed or _git(root, "rev-parse", "-q", "--verify",
                          "refs/tags/%s" % tag)[0] != 0:
        return {"ok": False, "error": "unknown or malformed snapshot tag",
                "tag": tag}
    live = _live_conductor_sessions(root)
    code, stat, err = _git(root, "diff", "--stat", tag, "--")
    result = {"ok": code == 0, "dry_run": bool(dry_run), "tag": tag,
              "cursor": parsed["cursor"], "diffstat": stat.strip(),
              "live_sessions": live}
    if code:
        result.update(ok=False, error=err or "git diff failed")
        return result
    if dry_run:
        return result
    if live:
        result.update(ok=False,
                      error="rollback refused while Conductor sessions are live")
        return result
    if parsed["cursor"] > ledger_length(root):
        result.update(ok=False, error="tag cursor exceeds current ledger")
        return result
    journal = _rollback_journal_path(root)
    try:
        _atomic_json(journal, {"tag": tag, "cursor": parsed["cursor"],
                               "started_at": time.time()})
        if _git(root, "reset", "--hard", tag)[0] != 0:
            result.update(ok=False, error="git reset failed")
            return result
        _truncate_ledger(root, parsed["cursor"])
        state = reconcile_on_start(root, default_state(), emit=lambda _m: None)
        save_conductor_state(root, state)
        os.remove(journal)
        result["state"] = state
        return result
    except (OSError, ValueError) as exc:
        result.update(ok=False, error=str(exc))
        return result


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Workspace conductor (V3 7.1 skeleton): observes "
                    "authoritative session state; events are a wake hint.")
    ap.add_argument("--root", required=True)
    ap.add_argument("--once", action="store_true",
                    help="one full poll, then exit")
    ap.add_argument("--interval", type=int, default=30,
                    help="max seconds between full polls (wake may be "
                         "sooner on events.jsonl growth)")
    ap.add_argument("--route", action="store_true",
                    help="ENABLE the acting stage: autonomously mint routed "
                         "sessions per routing.json rules. Off by default — "
                         "7.4 permissions, 7.5 termination, and 7.6 oversight "
                         "dials remain enforced on every routed session.")
    ap.add_argument("--pipeline",
                    help="V3 7.11: activate this pipeline preset JSON for the "
                         "run's routing + goal manifest. Validated up front — "
                         "an invalid preset refuses to start rather than "
                         "running with partially-applied config.")
    rollback_group = ap.add_mutually_exclusive_group()
    rollback_group.add_argument("--rollback", metavar="TAG",
                                help="roll back to an exact conductor tag")
    rollback_group.add_argument(
        "--rollback-last-night", metavar="TIMESTAMP",
        help="roll back to the newest conductor tag at/before an ISO-8601 "
             "timestamp")
    ap.add_argument("--apply", action="store_true",
                    help="perform rollback (default is a non-mutating dry-run)")
    args = ap.parse_args(argv)
    root = os.path.abspath(args.root)
    try:
        lock_fd = acquire_singleton(root)
    except ConductorLocked as exc:
        print("conductor: %s" % exc, file=sys.stderr)
        return 2

    repair_pending_rollback(root)
    state = reconcile_on_start(root, load_conductor_state(root))
    if args.rollback or args.rollback_last_night:
        tag = args.rollback or snapshot_tag_before(
            root, args.rollback_last_night)
        if not tag:
            print("conductor: no snapshot tag matches the requested time",
                  file=sys.stderr)
            release_singleton(lock_fd)
            return 2
        result = rollback(root, tag, dry_run=not args.apply)
        print(json.dumps(result, indent=2, sort_keys=True))
        release_singleton(lock_fd)
        return 0 if result.get("ok") else 2
    if args.pipeline:
        preset_path = os.path.abspath(args.pipeline)
        preset, err = pplib.load_preset_file(
            preset_path, pplib.known_sections_in_workspace(_sections_dir()))
        if err:
            print("conductor: --pipeline refused — %s" % err, file=sys.stderr)
            release_singleton(lock_fd)
            return 2
        state = _activate_pipeline(root, state, preset, preset_path,
                                   emit=lambda m: print(m, file=sys.stderr))

    def _shutdown(*_a):
        # Persist honestly (whatever stage we were in), release, exit 0 —
        # sys.exit unwinds so the finally below also runs on SIGTERM.
        sys.exit(0)

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)
    # The acting engine runs ONLY under --route: reachable and wired, but not
    # silently autonomous before permissions/termination/dials land.
    engine = route_engine if args.route else None
    if args.route:
        print("conductor: --route ENABLED — autonomous session minting is on "
              "(no termination/permission dials yet; supervise this run).",
              file=sys.stderr)
    try:
        state = full_poll(root, state, route_engine=engine)
        if args.once:
            return 0
        sizes = {}
        session_ids = discover_sessions(root)
        wake_signal(root, session_ids, sizes)   # baseline sizes
        last_poll = time.time()
        while True:
            time.sleep(1)
            if wake_signal(root, session_ids, sizes) \
                    or time.time() - last_poll >= args.interval:
                state = full_poll(root, state, route_engine=engine)
                session_ids = discover_sessions(root)
                last_poll = time.time()
    finally:
        save_conductor_state(root, state)
        release_singleton(lock_fd)
    return 0


# --- V3 7.2: production routing adapter ------------------------------------
# Bound lazily (deferred imports keep conductor importable standalone and the
# module a leaf until a poll with routing actually runs). Passed as full_poll's
# route_engine; observe-only 7.1 behavior is preserved when it's absent.

def _build_classifier(root):
    """A closed-candidate section classifier backed by one local-model turn
    with 6.5 schema-constrained decoding. Returns None when no local model
    is available — plan_routes then ledgers `unroutable` rather than guessing
    or reaching for a cloud model."""
    return None   # wired to run_local in a follow-up; skeleton declines safely


def route_engine(root, state, sessions, emit=print):
    """The acting-stage engine: for each session's admissible newly-final
    artifact, plan routes and execute them, minting through sessions and
    ledgering every decision on the same append-before-cursor discipline as
    the observation loop. Reuses THIS poll's `sessions` list — never
    re-discovers (the O(N^2) trap)."""
    import conductor_routing as crlib
    import conductor_permissions as cplib
    import conductor_plan as cplan
    import sections as seclib
    from orchestrator import create_session
    import artifacts as artlib
    import sessions as seslib_local

    dial = oversight_dial(state)

    # 7.5b: a workspace budget halt stops ALL routing — the acting stage is a
    # no-op until the cap is lifted (a new manifest / a new day for wall-clock).
    if state.get("halted"):
        return state

    # 7.5b: providers over their daily request quota -> routes whose target
    # section has been running on that provider DEFER (retry next cycle). The
    # section->provider map is derived from real costs.jsonl activity and built
    # once per poll, and only when some provider is actually over quota.
    over_quota = set(state.get("over_quota") or [])
    section_providers = _recent_section_providers(root, sessions) \
        if over_quota else {}

    sections_dir = _sections_dir()
    active_preset = state.get("pipeline")
    classify = _build_classifier(root)
    # A durable proposal may outlive the process that wrote it.  Replaying
    # after that crash must continue the effect, not append a second proposal
    # for the same deterministic route id.
    proposed_route_ids = {
        rec.get("route_id") for rec in read_ledger(root)
        if isinstance(rec, dict) and rec.get("decision") == "route_proposed"
        and isinstance(rec.get("route_id"), str)
    }
    effected_cache = {}   # (project, section) -> set, scanned once per poll
    project_artifact_cache = {}  # project -> (lineage index, final metas)
    wave_snapshot_taken = False

    def _ensure_wave_snapshot():
        nonlocal wave_snapshot_taken
        if wave_snapshot_taken:
            return
        snapshot(root, "routing-wave", state["ledger_cursor"], emit)
        state["ledger_cursor"] = ledger_length(root)
        save_conductor_state(root, state)
        wave_snapshot_taken = True

    def _ledger_route(root_, base, after_append=None):
        rec = {"v": SCHEMA_VERSION, "ts": time.time(), "stage": "acting"}
        rec.update(base)
        new_len = ledger_append(root_, rec)
        if after_append is not None:
            try:
                after_append()
            except Exception:
                pass
        state["ledger_cursor"] = new_len
        save_conductor_state(root_, state)

    def _caps_for(target_section):
        if target_section == "notification":
            return {"writes": "workspace", "exec": False,
                    "external": False}
        return seclib.load_section(target_section, sections_dir_parent,
                                   app_dir=None).capabilities

    def _mint_request(action):
        rid = action["route_id"]
        p = action["payload"]
        return {"title": "%s route:%s -> %s"
                        % (crlib.route_marker(rid), p["artifact_id"],
                           action["target"]),
                "artifact_id": p["artifact_id"],
                "content_hash": p["content_hash"], "route_id": rid,
                "source_section": p["source_section"], "rule_id": p["rule_id"]}

    def _drain_pending():
        # 7.4b: approvals that landed (possibly while the conductor was down)
        # are the authoritative executor for gated routes — restart-safe, and
        # independent of whether the source artifact is still admissible.
        for action in cplib.read_pending(root):
            if action.get("kind") == "plan":
                continue   # the plan executor below owns plan decisions
            rid = action.get("action_id")
            target = action.get("target")
            payload = action.get("payload")
            requested_by = action.get("requested_by")
            if not (rid and target and isinstance(payload, dict)
                    and requested_by):
                if rid:
                    cplib.remove_pending(root, rid)   # drop the malformed rec
                continue
            if rid in state["routed"]:
                cplib.remove_pending(root, rid)
                cplib.consume_decision(root, rid)
                continue
            decision_record = cplib.read_approval_decision(root, rid)
            if decision_record is None:
                continue   # still awaiting a human — never blocks the poll
            decision = decision_record["decision"]
            base = {"session": action.get("requested_by"), "route_id": rid,
                    "detail": {"target": action["target"], **action["payload"],
                               "reason": decision_record.get("reason") or
                               action.get("reason")}}
            if decision == "rejected":
                # Same atomicity fix as the approved branch: routed must be
                # set before the one save this decision's ledger line uses,
                # or a crash in the gap re-ledgers a duplicate route_denied
                # on restart.
                state["routed"][rid] = True
                _ledger_route(root, {**base, "decision": "route_denied"})
            elif decision == "do_not_route":
                pair = _remember_do_not_route(
                    state, payload.get("artifact_id"), payload.get("rule_id"))
                state["routed"][rid] = True
                _ledger_route(root, {
                    **base, "decision": "do_not_route_added",
                    "detail": {**base["detail"], **pair}})
            elif decision == "kill_session":
                killed = kill_spawned_session(root, requested_by)
                state["routed"][rid] = True
                _ledger_route(root, {
                    **base, "decision": "kill_session",
                    "detail": {**base["detail"], **killed}})
            elif over_quota and section_providers.get(target) in over_quota:
                # 7.5b: an approval must not bypass the quota-defer a fresh
                # route would get — an approved-but-not-yet-executed action
                # stays pending and is retried once the provider's daily
                # quota resets (never removed, never marked routed).
                _ledger_route(root, {
                    **base, "decision": "route_deferred",
                    "detail": {**base["detail"],
                               "provider": section_providers.get(target),
                               "reason": "provider over daily request quota"}})
                continue
            elif decision == "approved":
                _ensure_wave_snapshot()
                if action["target"] == "notification":
                    sdir = os.path.join(root, action["requested_by"])
                else:
                    sdir = seslib_local.mint_delegation_session(
                        root, action["requested_by"].split("/")[0],
                        action["target"], _mint_request(action),
                        reply_to=os.path.join(root, action["requested_by"]),
                        create_session=create_session,
                        on_error=lambda m: emit("conductor mint: %s" % m))
                if sdir:
                    # CRASH-SAFETY: routed must land in the SAME save as the
                    # ledger cursor advance for this decision. The prior code
                    # ledgered+saved via _ledger_route, THEN set routed and
                    # saved AGAIN two lines later — a crash in that window
                    # left the ledger holding 'route_approved' (cursor
                    # already past it) while state['routed'] and
                    # pending_actions.jsonl both still said "not yet done."
                    # A restart's _drain_pending would then re-run this same
                    # branch: mint_delegation_session is itself idempotent
                    # (delegation_id is deterministic from the SAME action
                    # payload, so no second session is ever actually
                    # created — see sessions.py) but the ledger would still
                    # gain a DUPLICATE 'route_approved' line for the same
                    # rid, violating "never a doubled decision." Setting
                    # routed here, before the one save this decision uses,
                    # closes that window.
                    state["routed"][rid] = True
                _ledger_route(root, {**base, "session_dir": sdir,
                                     "decision": "route_approved" if sdir
                                     else "mint_failed"})
                if not sdir:
                    continue   # retry on a later poll; routed stays unset
            else:
                # A stray route `.edit` is not an approval. Only plan actions
                # have an edit-and-approve contract; fail closed here.
                state["routed"][rid] = True
                _ledger_route(root, {**base, "decision": "route_denied",
                                     "detail": {**base["detail"],
                                                "reason": "invalid route decision"}})
            cplib.remove_pending(root, rid)
            cplib.consume_decision(root, rid)

    sections_dir_parent = os.path.dirname(sections_dir)

    def _plan_record(session_id, decision, plan_meta, plan_key, **detail):
        payload = {"plan_id": plan_meta.get("id"),
                   "plan_version": plan_meta.get("version"),
                   "plan_key": plan_key, **detail}
        _ledger_route(root, {"session": session_id, "decision": decision,
                             "detail": payload})

    def _current_plan(project_dir, plan_key):
        candidates = []
        for candidate in artlib.list_artifacts(
                project_dir, type="plan", on_error=lambda m: emit(
                    "conductor plan: %s" % m)):
            fields = candidate.get("fields")
            if isinstance(fields, dict) and fields.get("plan_key") == plan_key:
                candidates.append(candidate)
        if not candidates:
            return None
        roots = {((item.get("lineage") or [item.get("id")])[0])
                 for item in candidates}
        if len(roots) != 1:
            emit("conductor: plan key %s has multiple lineages — refusing "
                 "to guess which intent is current" % plan_key)
            return None
        return artlib.latest_final(
            project_dir, next(iter(roots)),
            on_error=lambda m: emit("conductor plan: %s" % m))

    def _ensure_plan(project_dir, sid, section, meta, intents):
        plan_key = cplan.plan_key(intents)
        existing = _current_plan(project_dir, plan_key)
        if existing is None:
            steps = cplan.steps_from_intents(
                intents, meta.get("artifact_type") or meta.get("type") or
                "artifact")
            body = cplan.render_plan_body(steps)
            registry = artlib.load_registry(
                os.path.dirname(os.path.abspath(__file__)),
                on_error=lambda m: emit("conductor plan: %s" % m))
            aid = artlib.publish(
                project_dir, body,
                {"type": "plan", "title": "Execution plan %s" % plan_key,
                 "source": {"section": section,
                            "session": sid.split("/")[-1],
                            "phase": "conductor", "turn": ""},
                 "plan_key": plan_key,
                 "driving_artifact_id": meta.get("id"),
                 "driving_content_hash": meta.get("content_hash"),
                 "driving_artifact_type": (meta.get("artifact_type") or
                                             meta.get("type") or "artifact"),
                 "source_section": section,
                 "origin_route_ids": [intent.route_id for intent in intents]},
                registry, on_error=lambda m: emit("conductor plan: %s" % m),
                consensus=True)
            if aid is None:
                emit("conductor: multi-step intent refused — a valid plan "
                     "artifact could not be published")
                return None, None, plan_key
            existing = artlib.load_meta(
                project_dir, aid,
                on_error=lambda m: emit("conductor plan: %s" % m))
            if not existing:
                emit("conductor: multi-step intent refused — published plan "
                     "metadata is unreadable")
                return None, None, plan_key
        body = artlib.read_body(
            project_dir, existing["id"],
            on_error=lambda m: emit("conductor plan: %s" % m))
        if cplan.parse_plan_body(body, on_error=lambda m: emit(
                "conductor plan: %s" % m)) is None:
            emit("conductor: multi-step intent refused — plan body is not "
                 "enumerable")
            return None, None, plan_key
        prior = state.setdefault("plans", {}).get(plan_key)
        if not isinstance(prior, dict) or prior.get("plan_id") != existing["id"]:
            state["plans"][plan_key] = {
                "plan_id": existing["id"],
                "plan_version": existing.get("version"),
                "status": "published"}
            _plan_record(sid, "plan_published", existing, plan_key,
                         origin_route_ids=[intent.route_id for intent in intents])
        return existing, body, plan_key

    def _plan_action(plan_meta, body, plan_key, sid, project, section,
                     driving_meta, intents, reason, feedback,
                     capability_exceeds):
        steps = cplan.parse_plan_body(body, on_error=lambda _m: None) or []
        fields = plan_meta.get("fields") if isinstance(
            plan_meta.get("fields"), dict) else {}
        return {
            "action_id": plan_meta["id"], "kind": "plan",
            "requested_by": sid, "target": "multiple sections",
            "reason": reason,
            "payload": {
                "plan_id": plan_meta["id"],
                "plan_version": plan_meta.get("version", 1),
                "plan_key": plan_key, "project": project,
                "source_section": section,
                "artifact_id": driving_meta.get("id"),
                "content_hash": driving_meta.get("content_hash"),
                "artifact_type": (driving_meta.get("artifact_type") or
                                  driving_meta.get("type") or
                                  fields.get("driving_artifact_type") or
                                  "artifact"),
                "origin_route_ids": [intent.route_id for intent in intents],
                "feedback": bool(feedback),
                "capability_exceeds": bool(capability_exceeds),
                "step_summary": [
                    {"id": step["id"], "title": step["title"][:120],
                     "target_section": step["target_section"]}
                    for step in steps[:20]],
            }}

    def _execute_plan(action, plan_meta, body):
        payload = action.get("payload") if isinstance(
            action.get("payload"), dict) else {}
        plan_key = payload.get("plan_key")
        sid = action.get("requested_by")
        if not isinstance(plan_key, str) or not isinstance(sid, str):
            return False
        steps = cplan.parse_plan_body(
            body, on_error=lambda m: emit("conductor plan: %s" % m))
        if steps is None:
            return False
        try:
            body_hash = hashlib.sha256(body.encode("utf-8")).hexdigest()
        except (AttributeError, UnicodeEncodeError):
            body_hash = ""
        if body_hash != plan_meta.get("content_hash"):
            emit("conductor: plan %s refused — body.md no longer matches "
                 "its published content hash; submit an .edit decision so "
                 "the change becomes a new version" % plan_meta.get("id"))
            return False
        unknown = [step["target_section"] for step in steps
                   if step["target_section"] != "notification"
                   and not os.path.isdir(os.path.join(
                       sections_dir, step["target_section"]))]
        if unknown:
            emit("conductor: plan %s refused — unknown target section(s): %s"
                 % (plan_meta.get("id"), ", ".join(sorted(set(unknown)))))
            return False
        registry = artlib.load_registry(
            os.path.dirname(os.path.abspath(__file__)),
            on_error=lambda m: emit("conductor plan: %s" % m))
        known_types = registry.get("types") if isinstance(registry, dict) \
            else {}
        unknown_types = [step["expected_artifact_type"] for step in steps
                         if step["expected_artifact_type"] not in known_types]
        if unknown_types:
            emit("conductor: plan %s refused — unknown expected artifact "
                 "type(s): %s" % (plan_meta.get("id"), ", ".join(
                     sorted(set(unknown_types)))))
            return False
        context = {"artifact_id": payload.get("artifact_id"),
                   "content_hash": payload.get("content_hash"),
                   "source_section": payload.get("source_section")}
        if not all(isinstance(context[key], str) and context[key]
                   for key in context):
            emit("conductor: plan %s refused — driving artifact context is "
                 "incomplete" % plan_meta.get("id"))
            return False
        intents = cplan.intents_from_steps(
            steps, context, plan_meta.get("id"))
        _ensure_wave_snapshot()
        source_dir = os.path.join(root, sid)
        for step, intent in zip(steps, intents):
            ref = {"plan_id": plan_meta["id"],
                   "plan_version": plan_meta.get("version", 1),
                   "step_id": step["id"]}
            activity = {**ref, "actor": "conductor",
                        "action": "route:%s" % intent.target,
                        "artifact_ids": [context["artifact_id"]]}
            cplan.append_activity_pair(
                root, source_dir, {**activity, "status": "started"})
            if intent.route_id in state["routed"]:
                cplan.append_activity_pair(
                    root, source_dir, {**activity, "status": "completed"})
                continue

            def _mint_plan(target_section, request,
                           _project=payload.get("project")):
                _eval_crash("mid_inbox_injection")
                if target_section == "notification":
                    session_dir = source_dir
                else:
                    session_dir = seslib_local.mint_delegation_session(
                        root, _project, target_section, request,
                        reply_to=source_dir,
                        create_session=create_session,
                        on_error=lambda m: emit("conductor mint: %s" % m))
                _eval_crash("post_act_pre_record")
                return session_dir

            def _probe_plan(rid, target_section,
                            _project=payload.get("project")):
                key = (_project, target_section)
                if key not in effected_cache:
                    effected_cache[key] = seslib_local.scan_effected_routes(
                        root, _project, target_section)
                return rid in effected_cache[key]

            def _plan_route_ledger(base, _ref=ref):
                decorated = dict(base)
                decorated["plan_ref"] = dict(_ref)
                decorated["detail"] = {**(base.get("detail") or {}),
                                       "plan_ref": dict(_ref)}
                rid = decorated.get("route_id")
                if decorated.get("decision") == "route_proposed":
                    if rid in proposed_route_ids:
                        return
                    proposed_route_ids.add(rid)
                _ledger_route(root, decorated)

            outcomes = crlib.execute_intents(
                [intent], sid, root, _mint_plan, _plan_route_ledger,
                probe=_probe_plan,
                request_extra=lambda _intent, _ref=ref: {
                    "plan_ref": dict(_ref)})
            outcome = outcomes[0] if outcomes else {"outcome": "mint_failed"}
            if outcome.get("outcome") == "mint_failed":
                cplan.append_activity_pair(
                    root, source_dir, {**activity, "status": "failed"})
                return False
            state["routed"][intent.route_id] = True
            cplan.append_activity_pair(
                root, source_dir, {**activity, "status": "completed"})
        for route_id in payload.get("origin_route_ids") or []:
            if isinstance(route_id, str):
                state["routed"][route_id] = True
        state.setdefault("plans", {})[plan_key] = {
            "plan_id": plan_meta["id"],
            "plan_version": plan_meta.get("version", 1),
            "status": "executed"}
        _plan_record(sid, "plan_executed", plan_meta, plan_key,
                     step_ids=[step["id"] for step in steps])
        cplib.remove_pending(root, action["action_id"])
        cplib.consume_decision(root, action["action_id"])
        return True

    def _drain_plans():
        for action in cplib.read_pending(root):
            if action.get("kind") != "plan":
                continue
            action_id = action.get("action_id")
            payload = action.get("payload") if isinstance(
                action.get("payload"), dict) else {}
            plan_key = payload.get("plan_key")
            project = payload.get("project")
            plan_id = payload.get("plan_id")
            sid = action.get("requested_by")
            if not all(isinstance(value, str) and value
                       for value in (action_id, plan_key, project, plan_id, sid)):
                if action_id:
                    cplib.remove_pending(root, action_id)
                continue
            lifecycle = state.setdefault("plans", {}).get(plan_key)
            if isinstance(lifecycle, dict) and lifecycle.get("status") in \
                    ("executed", "rejected"):
                cplib.remove_pending(root, action_id)
                cplib.consume_decision(root, action_id)
                continue
            if isinstance(lifecycle, dict) and \
                    lifecycle.get("status") == "approved":
                current_id = lifecycle.get("plan_id")
                if isinstance(current_id, str) and current_id:
                    plan_id = current_id
                    payload["plan_id"] = current_id
                    payload["plan_version"] = lifecycle.get("plan_version")
                    payload["approved"] = True
                    action["payload"] = payload
            project_dir = os.path.join(root, project)
            plan_meta = artlib.load_meta(
                project_dir, plan_id,
                on_error=lambda m: emit("conductor plan: %s" % m))
            body = artlib.read_body(
                project_dir, plan_id,
                on_error=lambda m: emit("conductor plan: %s" % m))
            if not plan_meta or body is None:
                emit("conductor: pending plan %s is unreadable — left pending"
                     % plan_id)
                continue
            if payload.get("approved") is True:
                _execute_plan(action, plan_meta, body)
                continue
            decision_record = cplib.read_approval_decision(root, action_id)
            if decision_record is None:
                continue
            decision = decision_record["decision"]
            if decision == "rejected":
                for route_id in payload.get("origin_route_ids") or []:
                    if isinstance(route_id, str):
                        state["routed"][route_id] = True
                state["plans"][plan_key] = {
                    "plan_id": plan_id,
                    "plan_version": plan_meta.get("version", 1),
                    "status": "rejected"}
                _plan_record(
                    sid, "plan_rejected", plan_meta, plan_key,
                    reason=decision_record.get("reason") or
                    "rejected by human")
                cplib.remove_pending(root, action_id)
                cplib.consume_decision(root, action_id)
                continue
            if decision == "edited":
                edited_body = decision_record.get("reason") or ""
                if cplan.parse_plan_body(
                        edited_body, on_error=lambda m: emit(
                            "conductor plan: %s" % m)) is None:
                    _plan_record(sid, "plan_edit_refused", plan_meta,
                                 plan_key, reason="edited plan is invalid")
                    cplib.consume_decision(root, action_id)
                    continue
                fields = plan_meta.get("fields") if isinstance(
                    plan_meta.get("fields"), dict) else {}
                registry = artlib.load_registry(
                    os.path.dirname(os.path.abspath(__file__)),
                    on_error=lambda m: emit("conductor plan: %s" % m))
                edited_id = artlib.publish(
                    project_dir, edited_body,
                    {"type": "plan", "title": plan_meta.get("title") or
                     "Edited execution plan",
                     "source": {"section": payload.get("source_section", ""),
                                "session": "human", "phase": "conductor",
                                "turn": ""}, **fields},
                    registry, on_error=lambda m: emit(
                        "conductor plan: %s" % m),
                    supersedes=plan_id, consensus=True)
                edited_meta = artlib.load_meta(
                    project_dir, edited_id,
                    on_error=lambda m: emit("conductor plan: %s" % m)) \
                    if edited_id else None
                if not edited_meta:
                    _plan_record(sid, "plan_edit_refused", plan_meta,
                                 plan_key, reason="edited plan publish failed")
                    cplib.consume_decision(root, action_id)
                    continue
                state["plans"][plan_key] = {
                    "plan_id": edited_id,
                    "plan_version": edited_meta.get("version", 1),
                    "status": "approved"}
                _plan_record(sid, "plan_edited", edited_meta, plan_key,
                             supersedes=plan_id, by="human")
                payload["plan_id"] = edited_id
                payload["plan_version"] = edited_meta.get("version", 1)
                payload["approved"] = True
                action["payload"] = payload
                cplib.enqueue_pending(root, action)
                cplib.consume_decision(root, action_id)
                _execute_plan(action, edited_meta, edited_body)
                continue
            if decision == "approved":
                payload["approved"] = True
                action["payload"] = payload
                if not cplib.enqueue_pending(root, action):
                    _plan_record(sid, "enqueue_failed", plan_meta, plan_key,
                                 reason="could not persist approved plan")
                    continue
                cplib.consume_decision(root, action_id)
                _execute_plan(action, plan_meta, body)
                continue
            # do_not_route/kill_session are route-only verbs. A plan never
            # widens them into approval.
            _plan_record(sid, "plan_decision_refused", plan_meta, plan_key,
                         reason="decision is not valid for a plan")
            cplib.consume_decision(root, action_id)

    _drain_pending()
    _drain_plans()

    terminated = state.get("terminated", {})
    for sid in sessions:
        terminal_only = sid in terminated
        parts = sid.split("/")
        if len(parts) < 2:
            continue   # flat/legacy dir: no section to source-route from
        project, section = parts[0], parts[1]
        project_dir = os.path.join(root, project)
        app_dir = os.path.join(root, sid)
        # 7.11: an active pipeline preset supplies ITS routing config for
        # every section in the run — zero pipeline-specific execution code,
        # this is the same RouteConfig shape a normal routing.json produces.
        # No per-section routing.json is consulted while a preset is active.
        config = pplib.as_route_config(active_preset) if active_preset \
            else crlib.load_route_config(sections_dir, section, project_dir,
                                         on_warn=emit)
        if not config.ok or (not config.routes and not config.rules):
            continue
        # 4.2's production publisher writes to the PROJECT bus. Keep the
        # session-local scan as a legacy bridge, but source-filter project
        # artifacts so sibling sessions cannot each act on the same output.
        stores = []
        if project not in project_artifact_cache:
            project_artifact_cache[project] = (
                artlib.lineage_index(project_dir, on_error=lambda _m: None),
                artlib.list_artifacts(project_dir, status="final",
                                      on_error=lambda _m: None))
        project_index, all_project_finals = project_artifact_cache[project]
        project_finals = []
        for item in all_project_finals:
            source = item.get("source") if isinstance(
                item.get("source"), dict) else {}
            source_session = source.get("session")
            if source.get("section") == section and source_session in \
                    (sid, os.path.basename(app_dir)):
                project_finals.append(item)
        stores.append((project_dir, project_index, project_finals))
        if app_dir != project_dir:
            local_index = artlib.lineage_index(
                app_dir, on_error=lambda _m: None)
            stores.append((
                app_dir, local_index,
                artlib.list_artifacts(app_dir, status="final",
                                      on_error=lambda _m: None)))
        seen_artifacts = set()
        candidates = []
        for store_dir, index, finals in stores:
            for item in finals:
                key = (item.get("id"), item.get("content_hash"))
                if key in seen_artifacts:
                    continue
                seen_artifacts.add(key)
                candidates.append((store_dir, index, item))
        for artifact_store, index, meta in candidates:
            if not artlib.is_admissible(artifact_store, meta, index=index,
                                        on_error=lambda _m: None):
                continue
            configured_types = set(config.routes) | {
                rule["artifact_type"] for rule in config.rules}
            artifact_type = meta.get("artifact_type") or meta.get("type")
            if terminal_only and artifact_type != "failure":
                # A terminal session may route only the failure evidence that
                # explains its stop; goal/convergence outputs stay frozen.
                continue
            if configured_types == {"failure"} and artifact_type != "failure":
                # The fleet notification safety net must not turn an otherwise
                # routing-free section into an "unroutable" classifier pass
                # for every ordinary artifact.
                continue
            # lineage_index already loaded every meta once — reuse its by_id
            # map for this artifact's store (project bus or legacy session).
            lineage_metas = index.get("by_id", {}) \
                if isinstance(index, dict) else {}
            _eval_crash("pre_guard")
            intents = crlib.plan_routes(meta, section, config, lineage_metas,
                                        classify=classify)
            # Already-routed dedupe: a still-admissible artifact stays
            # admissible across polls, so without this the same route re-fires
            # (and re-ledgers) every tick forever. route_digest keys on
            # content_hash, so a NEW version re-routes correctly.
            fresh = [i for i in intents
                     if route_digest(i.route_key) not in state["routed"]]
            if not fresh:
                continue
            _eval_crash("post_route_id")

            # V3 7.12: two or more executable route effects are ONE
            # autonomous multi-step intent. Materialize and classify the
            # whole plan before any of those effects starts. Single routes
            # continue through the byte-for-byte 7.6 path below.
            plan_intents = [
                intent for intent in fresh
                if intent.verdict == crlib.ALLOW and intent.target
                and not _is_do_not_route(
                    state, intent.artifact_id, intent.rule_id)
                and not (over_quota and section_providers.get(intent.target)
                         in over_quota)
                and not cplib.is_pending(root, intent.route_id)]
            if len(plan_intents) > 1:
                # Terminal/guarded siblings are still ledgered exactly once;
                # they are not executable plan steps.
                for intent in fresh:
                    if intent in plan_intents:
                        continue
                    if _is_do_not_route(
                            state, intent.artifact_id, intent.rule_id):
                        state["routed"][intent.route_id] = True
                        _ledger_route(root, {
                            "session": sid, "route_id": intent.route_id,
                            "route_key": intent.route_key,
                            "decision": "route_suppressed",
                            "detail": {**intent.as_ledger_detail(),
                                       "reason": "do-not-route decision"}})
                    elif over_quota and section_providers.get(intent.target) \
                            in over_quota:
                        _ledger_route(root, {
                            "session": sid, "route_id": intent.route_id,
                            "route_key": intent.route_key,
                            "decision": "route_deferred",
                            "detail": {"target": intent.target,
                                       "provider": section_providers.get(
                                           intent.target),
                                       "reason": "provider over daily "
                                                 "request quota"}})
                    elif intent.verdict != crlib.ALLOW or not intent.target:
                        crlib.execute_intents(
                            [intent], sid, root,
                            lambda _target, _request: None,
                            lambda base: _ledger_route(root, base))
                        state["routed"][intent.route_id] = True

                plan_meta, plan_body, plan_key = _ensure_plan(
                    project_dir, sid, section, meta, plan_intents)
                if not plan_meta:
                    continue   # refusal is visible; NOTHING executes
                lifecycle = state.setdefault("plans", {}).get(plan_key)
                if isinstance(lifecycle, dict) and lifecycle.get("status") \
                        in ("executed", "rejected"):
                    for intent in plan_intents:
                        state["routed"][intent.route_id] = True
                    save_conductor_state(root, state)
                    continue
                if isinstance(lifecycle, dict) and \
                        lifecycle.get("status") == "approved":
                    approved_action = _plan_action(
                        plan_meta, plan_body, plan_key, sid, project, section,
                        meta, plan_intents, "edited plan already approved",
                        False, False)
                    approved_action["payload"]["approved"] = True
                    _execute_plan(approved_action, plan_meta, plan_body)
                    continue
                plan_feedback = any(route_revisits_section(
                    intent, meta, lineage_metas) for intent in plan_intents)
                plan_capability = any(seclib.exceeds_workspace_only(
                    _caps_for(intent.target)) for intent in plan_intents)
                classification = classify_plan(
                    dial, feedback=plan_feedback,
                    capability_exceeds=plan_capability)
                if plan_capability:
                    reason = "plan contains capability-exceeding steps"
                elif plan_feedback:
                    reason = "plan contains a feedback-loop step"
                else:
                    reason = "%s oversight requires plan approval" % dial
                action = _plan_action(
                    plan_meta, plan_body, plan_key, sid, project, section,
                    meta, plan_intents, reason, plan_feedback,
                    plan_capability)
                if classification == "needs_approval":
                    if not cplib.is_pending(root, action["action_id"]):
                        if cplib.enqueue_pending(root, action):
                            def _emit_plan_approval():
                                import events as eventslib
                                eventslib.emit_event(
                                    app_dir, "approval_needed",
                                    project=project, section=section,
                                    session=sid, route_id=plan_meta["id"],
                                    artifact_id=plan_meta["id"],
                                    target="multi-step plan", reason=reason)
                            _ledger_route(root, {
                                "session": sid,
                                "decision": "approval_requested",
                                "detail": {
                                    "kind": "plan",
                                    "plan_id": plan_meta["id"],
                                    "plan_version": plan_meta.get(
                                        "version", 1),
                                    "plan_key": plan_key, "dial": dial,
                                    "feedback": plan_feedback,
                                    "capability_exceeds": plan_capability,
                                    "reason": reason}},
                                after_append=_emit_plan_approval)
                        else:
                            _ledger_route(root, {
                                "session": sid,
                                "decision": "enqueue_failed",
                                "detail": {"kind": "plan",
                                           "plan_id": plan_meta["id"],
                                           "plan_key": plan_key,
                                           "reason": "pending plan write "
                                                     "failed"}})
                    continue
                _execute_plan(action, plan_meta, plan_body)
                continue

            def _mint(target_section, request, _proj=project):
                # "mid_inbox_injection" is the historical state-machine name
                # from 7.3; the landed delivery model mints a delegation
                # session directly, so this boundary is immediately before
                # that durable effect.
                _eval_crash("mid_inbox_injection")
                if target_section == "notification":
                    session_dir = app_dir
                else:
                    session_dir = seslib_local.mint_delegation_session(
                        root, _proj, target_section, request,
                        reply_to=app_dir,
                        create_session=create_session,
                        on_error=lambda m: emit("conductor mint: %s" % m))
                _eval_crash("post_act_pre_record")
                return session_dir

            def _probe(rid, target_section, _proj=project, _intent_by_rid=None):
                # 7.3 restart recovery, cost-bounded: scan each target
                # section's effected routes ONCE per poll (cached), then match
                # this route_id — or, for a session minted before 7.3 stamped
                # route_id, its legacy (artifact_id, rule_id) key. Skipping the
                # mint AND recording the recovery is what makes it exactly-once
                # across a crash-before-done or the 7.2b->7.3 boundary.
                key = (_proj, target_section)
                if key not in effected_cache:
                    effected_cache[key] = seslib_local.scan_effected_routes(
                        root, _proj, target_section)
                effected = effected_cache[key]
                if rid in effected:
                    return True
                intent = _intent_by_rid.get(rid) if _intent_by_rid else None
                if intent is not None:
                    return ("legacy", intent.artifact_id, intent.rule_id) \
                        in effected
                return False

            # 7.4b capability gate: a route into a section that escalates
            # beyond workspace-only NEVER mints directly — it enqueues a
            # pending action and waits (across polls) for an approval file.
            # Only ALLOW intents can be gated; guarded/unroutable ones fall
            # through to execute_intents' own terminal ledger lines.
            allowed_now, gated = [], []
            for i in fresh:
                if _is_do_not_route(state, i.artifact_id, i.rule_id):
                    state["routed"][i.route_id] = True
                    _ledger_route(root, {
                        "session": sid, "route_id": i.route_id,
                        "route_key": i.route_key,
                        "decision": "route_suppressed",
                        "detail": {**i.as_ledger_detail(),
                                   "reason": "do-not-route decision"}})
                    continue
                if over_quota and section_providers.get(i.target) in over_quota:
                    # DEFER (not drop, not execute over-quota): don't mint and
                    # don't mark routed, so the still-admissible source re-plans
                    # next cycle once the provider's daily quota resets.
                    _ledger_route(root, {
                        "session": sid, "route_id": i.route_id,
                        "route_key": i.route_key, "decision": "route_deferred",
                        "detail": {"target": i.target,
                                   "provider": section_providers.get(i.target),
                                   "reason": "provider over daily request quota"}})
                    continue
                if cplib.is_pending(root, i.route_id):
                    # CRITICAL: once a route is queued for approval, its
                    # disposition belongs to _drain_pending alone. A fresh
                    # capability read (a torn manifest read, a GUI edit
                    # lowering caps) must NEVER reclassify it into the direct
                    # mint path — that would bypass approval entirely.
                    continue
                if i.verdict != crlib.ALLOW or not i.target:
                    allowed_now.append(i)
                else:
                    capability_exceeds = seclib.exceeds_workspace_only(
                        _caps_for(i.target))
                    feedback = route_revisits_section(i, meta, lineage_metas)
                    classification = classify_route(
                        i, dial, feedback=feedback,
                        capability_exceeds=capability_exceeds)
                    if classification == "needs_approval":
                        reason = ("target capabilities exceed workspace-only"
                                  if capability_exceeds else
                                  ("feedback route revisits section ancestry"
                                   if feedback else
                                   "%s oversight requires approval" % dial))
                        gated.append((i, reason, feedback,
                                      capability_exceeds))
                    else:
                        allowed_now.append(i)
            for i, reason, feedback, capability_exceeds in gated:
                base = {"session": sid, "route_id": i.route_id,
                        "route_key": i.route_key}
                action = cplib.pending_action(i, sid, reason=reason)
                if cplib.enqueue_pending(root, action):
                    def _emit_approval_events():
                        import events as eventslib
                        eventslib.emit_event(
                            app_dir, "route_proposed", route_id=i.route_id,
                            artifact_id=i.artifact_id, target=i.target,
                            status="needs_approval", reason=reason)
                        parts = sid.split("/")
                        eventslib.emit_event(
                            app_dir, "approval_needed",
                            project=parts[0] if parts else sid,
                            section=parts[1] if len(parts) > 1 else "",
                            session=sid, route_id=i.route_id,
                            artifact_id=i.artifact_id, target=i.target,
                            reason=reason)
                    _ledger_route(root, {
                        **base, "decision": "approval_requested",
                        "detail": {**i.as_ledger_detail(),
                                   "dial": dial, "feedback": feedback,
                                   "capability_exceeds": capability_exceeds,
                                   "reason": reason}},
                        after_append=_emit_approval_events)
                else:
                    # FIX: a failed durable enqueue must NOT be ledgered as a
                    # successful approval request (§6.2, §23) — record the miss
                    # so the audit trail is honest; the route retries next poll.
                    _ledger_route(root, {
                        **base, "decision": "enqueue_failed",
                        "detail": {**i.as_ledger_detail(),
                                   "reason": "pending queue write failed"}})
            if not allowed_now:
                continue

            by_rid = {i.route_id: i for i in allowed_now}
            if any(i.verdict == crlib.ALLOW and i.target
                   for i in allowed_now):
                _ensure_wave_snapshot()
            def _route_ledger(base):
                rid = base.get("route_id")
                if base.get("decision") == "route_proposed":
                    if rid in proposed_route_ids:
                        return
                    _ledger_route(
                        root, base,
                        after_append=lambda: _eval_crash(
                            "post_ledger_append"))
                    proposed_route_ids.add(rid)
                    return
                _ledger_route(root, base)

            outcomes = crlib.execute_intents(
                allowed_now, sid, root, _mint,
                _route_ledger,
                probe=lambda rid, tgt: _probe(rid, tgt, _intent_by_rid=by_rid))
            # Record only routes that actually fired or were terminally
            # decided (converged/budget/unroutable/denied) — a mint_failed
            # stays un-recorded so the next poll retries it.
            for oc in outcomes:
                if oc["outcome"] not in ("mint_failed",):
                    state["routed"][oc["route_id"]] = True
            save_conductor_state(root, state)
    return state


if __name__ == "__main__":
    sys.exit(main())
