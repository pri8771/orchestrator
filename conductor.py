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
import fcntl
import hashlib
import json
import os
import signal
import socket
import sys
import time

import sessions as seslib
import conductor_termination as ctlib

CONDUCTOR_DIRNAME = ".conductor"
# R4 explicit lifecycle (precedent: sessions.STATUS): the stage is persisted
# on every transition so a kill at ANY point resumes into a known state.
STAGES = ("idle", "scanning", "evaluating", "acting")
# 7.5 terminal ledger decisions — replayed by reconcile_on_start to rebuild
# state['terminated'] after a crash between the termination append and its save.
_TERMINAL_DECISIONS = ("goal_met", "converged_open_items", "stalled")
SCHEMA_VERSION = 1
_MAX_LEDGER_LINE_BYTES = 3500   # same PIPE_BUF discipline as events.py

# The authoritative agent_state.json fields a conductor decision may read.
# Everything else (transcripts, event payloads) is either hint or content.
_DECISION_FIELDS = ("current_phase", "done", "error", "awaiting_approval",
                    "status", "consensus_status", "last_processed")


def conductor_dir(root):
    return os.path.join(root, CONDUCTOR_DIRNAME)


def lock_path(root):
    return os.path.join(conductor_dir(root), "conductor.lock")


def state_path(root):
    return os.path.join(conductor_dir(root), "conductor_state.json")


def ledger_path(root):
    return os.path.join(conductor_dir(root), "conductor_ledger.jsonl")


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
            "halted": None, "over_quota": []}


def load_conductor_state(root):
    """Defensive: missing/corrupt state file resumes from defaults — the
    ledger (not this cache) is the record of what happened (§6.2)."""
    try:
        with open(state_path(root), encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return default_state()
    if not isinstance(data, dict) or data.get("stage") not in STAGES:
        return default_state()
    data.setdefault("schema_version", SCHEMA_VERSION)
    data.setdefault("ledger_cursor", 0)
    data.setdefault("sessions", {})
    data.setdefault("routed", {})
    data.setdefault("terminated", {})   # 7.5: symmetry + defense-in-depth
    data.setdefault("quiescence", {})
    data.setdefault("halted", None)     # 7.5b: symmetry + defense-in-depth
    data.setdefault("over_quota", [])
    if not isinstance(data["sessions"], dict) \
            or not isinstance(data["routed"], dict) \
            or not isinstance(data["terminated"], dict) \
            or not isinstance(data["quiescence"], dict) \
            or not isinstance(data["ledger_cursor"], int) \
            or data["ledger_cursor"] < 0:
        return default_state()
    return data


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
    state["ledger_cursor"] = length
    save_conductor_state(root, state)
    emit("conductor: resumed — reconciled %d un-cursored ledger entr%s "
         "(%d applied)." % (len(tail), "y" if len(tail) == 1 else "ies",
                            applied))
    return state


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
    save_conductor_state(root, state)
    emit("conductor: session %s terminated (%s) -> %s" % (sid, reason, report))


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


def _record_workspace_termination(root, state, reason, evidence, emit=print):
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
            _record_workspace_termination(root, state, bc["reason"],
                                          bc["evidence"], emit)
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
    save_conductor_state(root, state)
    return state


def full_poll(root, state, emit=print, route_engine=None):
    """One authoritative pass: scan -> evaluate (ledger observations for
    every changed session, APPEND-THEN-CURSOR each) -> idle. The skeleton
    records observations; when route_engine is injected, the acting
    stage routes this poll's admissible artifacts (7.2)."""
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
    # V3 7.5: termination stack (goal -> quiescence; 7.5b inserts stall +
    # budget). Runs inside the evaluating stage, BEFORE acting, so a session
    # that terminated this poll is not routed into. Zero cost + zero ledger
    # noise when no manifest enables any layer.
    manifest, mstatus = ctlib.load_goal_manifest_ex(root, on_warn=emit)
    if mstatus == "ok" and isinstance(manifest.get("budgets"), dict):
        state["_last_good_budgets"] = manifest["budgets"]
    elif mstatus == "corrupt" and state.get("_last_good_budgets"):
        # A torn/corrupt write must not silently drop budgets that were
        # active a moment ago (deny-safe: never WIDEN by losing a cap).
        # goal/quiescence/stall stay disabled — that's the conservative
        # direction (never a false termination), only budgets need the
        # fallback (an uncapped run is the unsafe direction for THIS layer).
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
                         "the safety layers (7.4 permissions, 7.5 "
                         "termination, 7.6 oversight dials) are not built "
                         "yet, so live routing stays opt-in until they are.")
    args = ap.parse_args(argv)
    root = os.path.abspath(args.root)
    try:
        lock_fd = acquire_singleton(root)
    except ConductorLocked as exc:
        print("conductor: %s" % exc, file=sys.stderr)
        return 2

    state = reconcile_on_start(root, load_conductor_state(root))

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


if __name__ == "__main__":
    sys.exit(main())


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
    import sections as seclib
    from orchestrator import create_session
    import artifacts as artlib
    import sessions as seslib_local

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

    sections_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "sections")
    classify = _build_classifier(root)
    effected_cache = {}   # (project, section) -> set, scanned once per poll

    def _ledger_route(root_, base):
        rec = {"v": SCHEMA_VERSION, "ts": time.time(), "stage": "acting"}
        rec.update(base)
        new_len = ledger_append(root_, rec)
        state["ledger_cursor"] = new_len
        save_conductor_state(root_, state)

    def _caps_for(target_section):
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
                continue
            decision = cplib.approval_decision(root, rid)
            if decision is None:
                continue   # still awaiting a human — never blocks the poll
            base = {"session": action.get("requested_by"), "route_id": rid,
                    "detail": {"target": action["target"], **action["payload"]}}
            if decision == "rejected":
                _ledger_route(root, {**base, "decision": "route_denied"})
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
            else:  # approved -> execute the gated effect now
                sdir = seslib_local.mint_delegation_session(
                    root, action["requested_by"].split("/")[0],
                    action["target"], _mint_request(action),
                    create_session=create_session,
                    on_error=lambda m: emit("conductor mint: %s" % m))
                _ledger_route(root, {**base, "session_dir": sdir,
                                     "decision": "route_approved" if sdir
                                     else "mint_failed"})
                if not sdir:
                    continue   # retry on a later poll
            state["routed"][rid] = True
            cplib.remove_pending(root, rid)
            save_conductor_state(root, state)

    sections_dir_parent = os.path.dirname(sections_dir)
    _drain_pending()

    terminated = state.get("terminated", {})
    for sid in sessions:
        if sid in terminated:
            continue   # 7.5: the goal is met or the session converged — no
            # new routes are planned into it (a human-approved pending route
            # still drains above; termination gates only NEW planning).
        parts = sid.split("/")
        if len(parts) < 2:
            continue   # flat/legacy dir: no section to source-route from
        project, section = parts[0], parts[1]
        project_dir = os.path.join(root, project)
        app_dir = os.path.join(root, sid)
        config = crlib.load_route_config(sections_dir, section, project_dir,
                                         on_warn=emit)
        if not config.ok or (not config.routes and not config.rules):
            continue
        index = artlib.lineage_index(app_dir, on_error=lambda _m: None)
        # lineage_index already loaded every meta once — reuse its by_id map
        # for the whole session instead of re-scanning the store per artifact
        # (the O(finals * artifacts) trap the adversarial review caught).
        lineage_metas = index.get("by_id", {}) if isinstance(index, dict) else {}
        for meta in artlib.list_artifacts(app_dir, status="final",
                                          on_error=lambda _m: None):
            if not artlib.is_admissible(app_dir, meta, index=index,
                                        on_error=lambda _m: None):
                continue
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

            def _mint(target_section, request, _proj=project):
                return seslib_local.mint_delegation_session(
                    root, _proj, target_section, request,
                    create_session=create_session,
                    on_error=lambda m: emit("conductor mint: %s" % m))

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
                elif seclib.exceeds_workspace_only(_caps_for(i.target)):
                    gated.append(i)
                else:
                    allowed_now.append(i)
            for i in gated:
                base = {"session": sid, "route_id": i.route_id,
                        "route_key": i.route_key}
                if cplib.enqueue_pending(root, cplib.pending_action(i, sid)):
                    _ledger_route(root, {
                        **base, "decision": "approval_requested",
                        "detail": {**i.as_ledger_detail(),
                                   "capability": "exceeds workspace-only"}})
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
            outcomes = crlib.execute_intents(
                allowed_now, sid, root, _mint,
                lambda base: _ledger_route(root, base),
                probe=lambda rid, tgt: _probe(rid, tgt, _intent_by_rid=by_rid))
            # Record only routes that actually fired or were terminally
            # decided (converged/budget/unroutable/denied) — a mint_failed
            # stays un-recorded so the next poll retries it.
            for oc in outcomes:
                if oc["outcome"] not in ("mint_failed",):
                    state["routed"][oc["route_id"]] = True
            save_conductor_state(root, state)
    return state
