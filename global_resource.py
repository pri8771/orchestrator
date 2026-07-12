#!/usr/bin/env python3
"""
global_resource.py — machine-wide worker cap across concurrent projects (V2 spec §4.5).

Each project runs as its own orchestrator.py process, so a shared counter must be
cross-process-safe. This uses a single stdlib-sqlite3 DB with WAL + BEGIN IMMEDIATE
so claim/release are serialized across processes. Two independently-capped resource
classes: `cli_remote` (network CLI turns) and `local_model` (resident Ollama
generations, which are far heavier per-slot).

Failure handling: if the DB can't be opened or a query errors unexpectedly,
claims fail OPEN (succeed) and releases are no-ops, so the broker can never crash
an agent turn. The one exception is lock contention — `BEGIN IMMEDIATE` timing
out because other processes hold the write lock is EXACTLY when the cap matters,
so a busy DB is retried and, if still contended, the claim fails CLOSED rather
than silently letting the cap be exceeded. Dead-PID rows are reaped on each
claim, so a crashed process never leaks slots.
"""

import os
import sqlite3
import time

DEFAULT_DB = os.path.expanduser("~/.orchestrator_global/workers.db")

# A single claimed slot maps to one agent turn (minutes). A row older than this
# is a leak — a crashed process whose PID was recycled (so os.kill(pid,0) sees
# the NEW process and reports it "alive"), or one owned by another user after
# reuse. Reaping by age bounds that damage portably, without needing per-OS
# process start-times.
_MAX_SLOT_AGE_SECONDS = 6 * 3600

# Lock-contention retry policy for try_claim. A short per-attempt busy timeout
# plus a few retries bounds worst-case latency while still riding out the normal
# write-lock contention of many projects claiming at once.
_CLAIM_BUSY_TIMEOUT_MS = 2000
_CLAIM_RETRIES = 5
_CLAIM_BACKOFF = 0.1


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
    # Age-based reap first: bounds PID-reuse leaks regardless of liveness.
    conn.execute("DELETE FROM worker_slots WHERE claimed_at < ?",
                 (time.time() - _MAX_SLOT_AGE_SECONDS,))
    for (pid,) in conn.execute("SELECT DISTINCT pid FROM worker_slots").fetchall():
        if not _pid_alive(pid):
            conn.execute("DELETE FROM worker_slots WHERE pid=?", (pid,))


def _claim_once(conn, project_id, resource_class, cap, pid):
    """One claim transaction. Returns True/False; may raise on lock contention."""
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


def try_claim(project_id, resource_class, cap, pid=None, db_path=DEFAULT_DB):
    """Try to take one slot. Returns True if taken, False if the cap is full.

    Fails OPEN (True) only when the broker is genuinely unavailable (can't open
    the DB, unexpected error). Lock contention is NOT treated as unavailability:
    the whole point of the cap is to hold under concurrency, so a busy DB is
    retried and, if still contended, the claim fails CLOSED (False) — the caller
    (claim_slot) then polls, rather than the cap being silently exceeded.

    A non-positive `cap` (0, negative — a bad config value, since nothing here
    validates runtime.global_worker_cap.* before it reaches this call) is
    treated as a misconfiguration, not an intentional "block everything": it
    fails OPEN so a typo'd/broken cap degrades to uncapped instead of silently
    wedging every single claim for the resource class shut."""
    try:
        cap = int(cap)
    except (TypeError, ValueError):
        cap = 0
    if cap <= 0:
        return True
    pid = pid or os.getpid()
    try:
        conn = _conn(db_path)
    except Exception:
        return True   # broker unavailable — fail open
    try:
        # Shorter busy timeout than the default connection so each attempt is
        # responsive; retries cover the rest.
        try:
            conn.execute("PRAGMA busy_timeout=%d" % _CLAIM_BUSY_TIMEOUT_MS)
        except Exception:
            pass
        for attempt in range(_CLAIM_RETRIES):
            try:
                return _claim_once(conn, project_id, resource_class, cap, pid)
            except sqlite3.OperationalError:
                # "database is locked/busy": other claimers hold the write lock.
                # Roll back and retry so the cap is still enforced.
                try:
                    conn.rollback()
                except Exception:
                    pass
                if attempt < _CLAIM_RETRIES - 1:
                    time.sleep(_CLAIM_BACKOFF * (attempt + 1))
        # Still contended after retries: fail closed so the cap holds.
        return False
    except Exception:
        return True   # unexpected broker error — fail open
    finally:
        try:
            conn.close()
        except Exception:
            pass


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
