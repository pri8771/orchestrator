#!/usr/bin/env python3
"""
Hardened subprocess execution — one chokepoint for every external command.

Why this module exists
----------------------
``subprocess.run(cmd, timeout=T, capture_output=True)`` does NOT reliably honor
its timeout. On ``TimeoutExpired`` it kills only the *direct* child and then
calls ``communicate()`` a second time (with no timeout) to reap it. If that child
had spawned a helper/grandchild that inherited our stdout/stderr pipe — which
``agy``/gemini do, and which ``xcodebuild`` does via its build-service daemons —
the pipe never sees EOF, so the reaping ``communicate()`` blocks forever and the
whole build deadlocks. A 30-minute per-agent timeout then never fires.

``run_capture`` fixes this everywhere:

* it launches the command in its OWN session/process group
  (``start_new_session=True``) and, on timeout, kills the WHOLE group with
  ``os.killpg`` — closing those inherited pipes so the reap can't block; and
* it optionally enforces a *no-output heartbeat*: if the process emits zero bytes
  on stdout+stderr for ``heartbeat`` seconds it is killed as a suspected hang,
  catching stalls long before the (much larger) overall timeout would.

Both the overall-timeout and the heartbeat raise ``subprocess.TimeoutExpired``
(the heartbeat raises the ``NoOutputTimeout`` subclass), so a single
``except subprocess.TimeoutExpired`` handles either.

Platform: POSIX only. ``start_new_session=True`` plus process-group signalling
(``os.killpg`` / ``os.getpgid``) are Unix APIs; the engine targets macOS/Linux.
On Windows these calls are unavailable and this module would need a Job-object
equivalent — out of scope for the current CLI-on-your-Mac deployment.
"""

import os
import signal
import subprocess
import threading
import time


class NoOutputTimeout(subprocess.TimeoutExpired):
    """Raised when a process produces no output for ``heartbeat`` seconds.

    Subclasses ``TimeoutExpired`` on purpose: existing
    ``except subprocess.TimeoutExpired`` handlers treat a silent hang exactly
    like an overall timeout, while callers that care can special-case it.
    """


# Process groups run_capture is currently executing. The orchestrator's
# SIGTERM/SIGINT handler drains this via kill_live_groups() so stopping a run
# also stops its in-flight agent CLIs — they run in their own sessions and
# would otherwise survive as orphans, still writing to the workspace.
_LIVE_PGIDS: "set[int]" = set()
# RLock, not Lock: kill_live_groups runs inside the SIGTERM/SIGINT handler,
# which can interrupt this module's own _track/_untrack on the main thread —
# a non-reentrant lock would deadlock the handler against its own thread.
_LIVE_PGIDS_LOCK = threading.RLock()


def _track_pgid(pgid):
    with _LIVE_PGIDS_LOCK:
        _LIVE_PGIDS.add(pgid)


def _untrack_pgid(pgid):
    with _LIVE_PGIDS_LOCK:
        _LIVE_PGIDS.discard(pgid)


def track_pgid(pgid):
    """Register an externally-spawned process group so kill_live_groups() (the
    orchestrator's SIGTERM/SIGINT drain) also stops it. For processes started
    directly with Popen(start_new_session=True) rather than via run_capture —
    e.g. verify.py booting a server. Pair with untrack_pgid in a finally."""
    _track_pgid(pgid)


def untrack_pgid(pgid):
    """Stop tracking a process group registered with track_pgid."""
    _untrack_pgid(pgid)


def kill_live_groups(sig=signal.SIGTERM):
    """Signal every process group currently running under run_capture."""
    with _LIVE_PGIDS_LOCK:
        pgids = list(_LIVE_PGIDS)
    for pgid in pgids:
        try:
            os.killpg(pgid, sig)
        except (ProcessLookupError, PermissionError, OSError):
            pass


def _dec(b):
    """Decode captured bytes to text, tolerating malformed output rather than
    raising (a build's stderr is not guaranteed to be valid UTF-8)."""
    if b is None:
        return ""
    if isinstance(b, (bytes, bytearray)):
        return bytes(b).decode("utf-8", "replace")
    return b


def _kill_group(pgid, p):
    """SIGKILL the whole process group; fall back to killing just the child.

    ``pgid`` must be captured right after Popen (while the group leader is still
    alive). Looking it up later via ``os.getpgid(p.pid)`` fails once the leader
    has exited and been reaped — which is exactly the fast-exiting-child case
    that leaves a grandchild holding our pipe — so the caller passes the cached
    value. The group persists as long as the grandchild is in it, so killpg still
    reaches it even after the leader is gone."""
    try:
        os.killpg(pgid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            p.kill()
        except OSError:
            pass


def run_capture(cmd, cwd=None, timeout=None, env=None, heartbeat=None,
                input_text=None):
    """Run ``cmd``, capturing stdout/stderr; return ``(out, err, returncode)``.

    The command runs in a fresh process group so a timeout kills the entire tree
    (not just the direct child, which otherwise deadlocks the reap on a pipe a
    surviving grandchild still holds open).

    ``input_text`` (str/bytes) is written to the child's stdin, which is then
    closed — how the local `ollama run` model gets its prompt without hitting
    ARG_MAX. ``None`` leaves stdin untouched (inherited), exactly as before.

    Raises ``subprocess.TimeoutExpired`` if the run exceeds ``timeout`` seconds,
    or ``NoOutputTimeout`` (a subclass) if ``heartbeat`` is set and the process
    emits zero bytes for that many seconds. ``timeout=None`` disables the overall
    limit; ``heartbeat`` falsy/<=0 disables the idle guard.
    """
    stdin = subprocess.PIPE if input_text is not None else None
    data = (input_text.encode("utf-8", "replace")
            if isinstance(input_text, str) else input_text)
    p = subprocess.Popen(cmd, cwd=cwd, env=env, stdin=stdin,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         start_new_session=True)
    # Capture the group id NOW, while the leader is guaranteed alive. If we defer
    # this to timeout-handling time, communicate() may have already reaped a
    # fast-exiting child, and os.getpgid(p.pid) would then raise — leaving the
    # grandchild that holds our pipe alive and the drain hung.
    try:
        pgid = os.getpgid(p.pid)
    except OSError:
        pgid = p.pid   # start_new_session makes pgid == pid; safe fallback
    _track_pgid(pgid)

    # Fast path — no heartbeat: a single communicate(timeout) reads the pipes in
    # C. Killing the group on timeout is the whole fix (vs. subprocess.run, which
    # would block re-reaping a pipe held open by an inherited grandchild).
    if not heartbeat or heartbeat <= 0:
        try:
            out, err = p.communicate(input=data, timeout=timeout)
            return _dec(out), _dec(err), p.returncode
        except subprocess.TimeoutExpired:
            _kill_group(pgid, p)
            try:
                p.communicate(timeout=10)   # drain closed pipes; output discarded
            except Exception:
                pass
            finally:
                for stream in (p.stdin, p.stdout, p.stderr):
                    try:
                        if stream is not None:
                            stream.close()
                    except OSError:
                        pass
            raise
        finally:
            _untrack_pgid(pgid)

    # Heartbeat path — drain both pipes in reader threads so we can watch for a
    # no-output window. Reading raw bytes off the fds (rather than readline)
    # counts partial lines / spinner updates as activity, and continuous draining
    # also prevents a full-pipe deadlock. The threads see EOF the instant the
    # group is killed.
    out_buf, err_buf = [], []
    lock = threading.Lock()
    last = [time.monotonic()]

    # Feed stdin from a thread so a child that reads it lazily can't deadlock
    # against a full OS pipe buffer (large prompts exceed 64KiB). Errors are
    # expected if the child exits/dies before reading — just stop writing.
    if data is not None:
        def _feed(stdin_pipe, payload):
            try:
                stdin_pipe.write(payload)
                stdin_pipe.flush()
            except (BrokenPipeError, ValueError, OSError):
                pass
            finally:
                try:
                    stdin_pipe.close()
                except (ValueError, OSError):
                    pass
        threading.Thread(target=_feed, args=(p.stdin, data), daemon=True).start()

    def _pump(fd, buf):
        while True:
            try:
                chunk = os.read(fd, 65536)
            except OSError:
                break
            if not chunk:
                break
            buf.append(chunk)
            with lock:
                last[0] = time.monotonic()

    threads = [threading.Thread(target=_pump, args=(p.stdout.fileno(), out_buf), daemon=True),
               threading.Thread(target=_pump, args=(p.stderr.fileno(), err_buf), daemon=True)]
    for t in threads:
        t.start()

    start = time.monotonic()
    reason = None   # None | ("timeout", secs) | ("heartbeat", secs)
    try:
        while True:
            if p.poll() is not None:
                break
            now = time.monotonic()
            if timeout and now - start >= timeout:
                reason = ("timeout", timeout)
                break
            with lock:
                idle = now - last[0]
            if idle >= heartbeat:
                reason = ("heartbeat", heartbeat)
                break
            time.sleep(0.2)
        if reason:
            _kill_group(pgid, p)
        try:
            p.wait(timeout=10)
        except subprocess.TimeoutExpired:
            _kill_group(pgid, p)
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
        # Threads exit once the pipes hit EOF (i.e. once the group is dead). Bound
        # the join so a lingering grandchild that outlived a clean child exit
        # can't wedge us here either — the threads are daemons and the fds are
        # closed below, which unblocks any still-pending os.read.
        for t in threads:
            t.join(timeout=10)
    finally:
        _untrack_pgid(pgid)
        for stream in (p.stdin, p.stdout, p.stderr):
            try:
                if stream is not None:
                    stream.close()
            except OSError:
                pass

    out, err = _dec(b"".join(out_buf)), _dec(b"".join(err_buf))
    if reason and reason[0] == "timeout":
        raise subprocess.TimeoutExpired(cmd, reason[1], output=out, stderr=err)
    if reason and reason[0] == "heartbeat":
        raise NoOutputTimeout(cmd, reason[1], output=out, stderr=err)
    return out, err, p.returncode
