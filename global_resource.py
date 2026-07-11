#!/usr/bin/env python3
"""
global_resource.py — machine-wide worker cap across concurrent projects (V2 spec §4.5).

Each project runs as its own orchestrator.py process, so a shared counter must be
cross-process-safe. This uses a single stdlib-sqlite3 DB with WAL + BEGIN IMMEDIATE
so claim/release are serialized across processes. Two independently-capped resource
classes: `cli_remote` (network CLI turns) and `local_model` (resident Ollama
generations, which are far heavier per-slot).

EVERY operation is fail-open: if the DB can't be opened or a query errors, claims
succeed and releases are no-ops, so the broker can never block or crash an agent
turn — it only throttles when it's healthy. Dead-PID rows are reaped on each claim,
so a crashed process never leaks slots.
"""

import os
import sqlite3
import time

DEFAULT_DB = os.path.expanduser("~/.orchestrator_global/workers.db")


def _conn(db_path):
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE IF NOT EXISTS worker_slots "
                 "(pid INTEGER, project_id TEXT, resource_class TEXT, claimed_at REAL)")
    return conn


def _pid_alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True   # exists, owned by another user
    except OSError:
        return False


def _reap(conn):
    for (pid,) in conn.execute("SELECT DISTINCT pid FROM worker_slots").fetchall():
        if not _pid_alive(pid):
            conn.execute("DELETE FROM worker_slots WHERE pid=?", (pid,))


def try_claim(project_id, resource_class, cap, pid=None, db_path=DEFAULT_DB):
    """Try to take one slot. Returns True if taken (or if the broker is
    unavailable — fail open), False only if the cap is genuinely full."""
    pid = pid or os.getpid()
    try:
        conn = _conn(db_path)
        try:
            conn.execute("BEGIN IMMEDIATE")
            _reap(conn)
            n = conn.execute("SELECT COUNT(*) FROM worker_slots WHERE resource_class=?",
                             (resource_class,)).fetchone()[0]
            if n >= cap:
                conn.rollback()
                return False
            conn.execute("INSERT INTO worker_slots VALUES (?,?,?,?)",
                         (pid, project_id, resource_class, time.time()))
            conn.commit()
            return True
        finally:
            conn.close()
    except Exception:
        return True   # fail open


def release(resource_class, pid=None, db_path=DEFAULT_DB):
    """Release one slot held by this pid in this class. Best-effort; never raises."""
    pid = pid or os.getpid()
    try:
        conn = _conn(db_path)
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT rowid FROM worker_slots WHERE pid=? AND resource_class=? "
                               "LIMIT 1", (pid, resource_class)).fetchone()
            if row:
                conn.execute("DELETE FROM worker_slots WHERE rowid=?", (row[0],))
            conn.commit()
        finally:
            conn.close()
    except Exception:
        pass


def claim_slot(project_id, resource_class, cap, max_wait=300, poll=3.0, db_path=DEFAULT_DB):
    """Claim a slot, waiting up to ``max_wait`` seconds for one to free. Returns
    True if a slot was taken, False if it gave up after the timeout — in which case
    the caller proceeds anyway (better mild oversubscription than a hung run)."""
    deadline = time.time() + max_wait
    while True:
        if try_claim(project_id, resource_class, cap, db_path=db_path):
            return True
        if time.time() >= deadline:
            return False
        time.sleep(poll)


def active_count(resource_class, db_path=DEFAULT_DB):
    """Current live slot count for a class (after reaping). 0 on any error."""
    try:
        conn = _conn(db_path)
        try:
            conn.execute("BEGIN IMMEDIATE")
            _reap(conn)
            n = conn.execute("SELECT COUNT(*) FROM worker_slots WHERE resource_class=?",
                             (resource_class,)).fetchone()[0]
            conn.commit()
            return n
        finally:
            conn.close()
    except Exception:
        return 0
