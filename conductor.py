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

CONDUCTOR_DIRNAME = ".conductor"
# R4 explicit lifecycle (precedent: sessions.STATUS): the stage is persisted
# on every transition so a kill at ANY point resumes into a known state.
STAGES = ("idle", "scanning", "evaluating", "acting")
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
            "ledger_cursor": 0, "sessions": {}, "routed": {}}


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
    if not isinstance(data["sessions"], dict) \
            or not isinstance(data["routed"], dict) \
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
    state["ledger_cursor"] = length
    save_conductor_state(root, state)
    emit("conductor: resumed — reconciled %d un-cursored ledger entr%s "
         "(%d applied)." % (len(tail), "y" if len(tail) == 1 else "ies",
                            applied))
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
    from orchestrator import create_session
    import artifacts as artlib
    import sessions as seslib_local

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

    for sid in sessions:
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

            by_rid = {i.route_id: i for i in fresh}
            outcomes = crlib.execute_intents(
                fresh, sid, root, _mint,
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
