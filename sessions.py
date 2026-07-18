#!/usr/bin/env python3
"""
sessions.py — the conductor-less delegation machinery for the V3 artifact
bus (board 4.4): mint a nested chat session for a delegated request, spawn
an engine run in it, and deliver the resulting artifact back to the
originating session's inbox exactly once.

WHY a separate module, and why it never imports orchestrator:
Every module orchestrator.py imports (artifacts, events, sections,
portfolio, schemas, ...) is forbidden from importing orchestrator back —
the cycle would also double-load the engine when it runs as __main__
(two copies of every global, including the lock registries). orchestrator
imports THIS module (for the reply edge in _hook_artifact_publish), so
sessions.py must stay a leaf: stdlib + events + schemas only.

That is why mint_delegation_session takes 3.0's nested-minting helper
(orchestrator.create_session) as an INJECTED parameter rather than
importing it — the card's "go through 3.0's helper, do not re-derive
paths" is honored by passing it in, and the delegation logic (idempotent
re-mint, delegation.json, the reply ledger) stays testable in isolation.

Durability + exactly-once (§6.1, §12.4, R2):
  * delegation.json is written tmp+fsync+os.replace, so the reply ledger
    survives a crash.
  * A re-mint of the same request returns the SAME dir (idempotency key =
    a hash of the request), never a duplicate.
  * A reply is delivered exactly once even across a crash between publish
    and inbox-append: dedupe is layered — the durable `delivered` ledger
    in delegation.json (authoritative, written AFTER the append) plus a
    scan of the target inbox for the artifact marker (closes the
    post-append/pre-ledger window).
"""

import copy
import datetime
import hashlib
import json
import os
import re
import subprocess
import sys

import events as evlib
import procutil
import schemas

DELEGATION_BASENAME = "delegation.json"

# Explicit lifecycle (R4 — never boolean soup): pending at mint, running
# once spawned, replied once a reply is durably delivered, failed on a
# launch error or a dead run that never replied.
STATUS = ("pending", "running", "replied", "failed")

# Key material never rides argv or a spawned run's env — auth is by the
# logged-in CLIs' own config dirs. The env scrub uses procutil.is_secret_env
# (the same predicate verify.py's npm scrub uses — one source, no drift), so
# it catches AWS_*/GITHUB_*/*_TOKEN too, not just this readable floor list.
SECRET_ENV = (
    "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY",
    "GOOGLE_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL",
    "OPENAI_BASE_URL", "GOOGLE_APPLICATION_CREDENTIALS",
)

_SLUG_STRIP = re.compile(r"[^a-z0-9]+")


def _slug(text):
    # The artifacts.mint_id discipline: lowercase-ASCII so the dir name is
    # filesystem-safe on APFS without any encoding layer.
    s = _SLUG_STRIP.sub("-", (text or "").lower()).strip("-")
    return s[:48].rstrip("-")


def _now_iso():
    return (datetime.datetime.now(datetime.timezone.utc)
            .astimezone().isoformat(timespec="seconds"))


# ---------------------------------------------------------------------------
# delegation.json — the durable record.
# ---------------------------------------------------------------------------
def delegation_path(session_dir):
    return os.path.join(session_dir, DELEGATION_BASENAME)


def delegation_id(reply_to, project, section, request_meta):
    """A stable 12-hex key for "the same delegated request". Embedding it
    in the chat slug makes an idempotent re-mint return the same dir and
    makes cross-delegation slug collisions structurally impossible. The
    CALLER (4.6) is responsible for putting a stable per-send token into
    request_meta so a UI retry collapses here while a genuine re-send
    differs — 4.4 only hashes what it is given."""
    payload = json.dumps(
        {"reply_to": reply_to or "", "project": project, "section": section,
         "request": request_meta if isinstance(request_meta, dict) else {}},
        sort_keys=True, ensure_ascii=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]


def read_delegation(session_dir, on_error=None):
    """The parsed delegation.json, or None (reported) — tolerant of a
    missing file (a non-delegation session) and a corrupt one."""
    if on_error is None:
        on_error = lambda _m: None
    path = delegation_path(session_dir)
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
    except (OSError, ValueError) as exc:
        on_error("delegation.json unreadable at %r: %s" % (session_dir, exc))
        return None
    if not isinstance(doc, dict):
        on_error("delegation.json is not an object at %r" % (session_dir,))
        return None
    return doc


def _write_delegation(session_dir, record):
    """Atomic + durable (tmp+fsync+os.replace): the reply ledger must
    survive a crash, so this is stronger than a plain atomic write."""
    path = delegation_path(session_dir)
    tmp = "%s.%d.tmp" % (path, os.getpid())
    # default=str mirrors delegation_id's tolerance, so a request_meta with a
    # non-JSON value degrades to a stringified record instead of raising out
    # of mint and leaving a poison dir with no delegation.json.
    data = json.dumps(record, indent=2, sort_keys=True,
                      default=str).encode("utf-8")
    with open(tmp, "wb") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def _new_record(did, request_meta, reply_to, created_at):
    rm = request_meta if isinstance(request_meta, dict) else {}
    return {
        "schema_version": schemas.SCHEMA_VERSION,
        "delegation_id": did,
        "request": copy.deepcopy(rm),
        "origin_session": rm.get("origin_session", "") or "",
        "reply_to": reply_to,
        "created_at": created_at or _now_iso(),
        "status": "pending",
        # delivering = per-artifact INTENT (written+fsynced BEFORE the inbox
        # append); delivered = DONE (written after). The pair is the 7.3
        # intent->effect->done ledger that makes delivery exactly-once even
        # across a crash between the append and the done-write.
        "delivering": [],
        "delivered": [],
    }


def set_status(session_dir, status, on_error=None, **extra):
    """Transition delegation.json's status (R4), preserving the record.
    A missing/corrupt record is reported and left untouched."""
    if on_error is None:
        on_error = lambda _m: None
    rec = read_delegation(session_dir, on_error=on_error)
    if rec is None:
        return False
    rec["status"] = status
    rec.update(extra)
    try:
        _write_delegation(session_dir, rec)
    except OSError as exc:
        on_error("could not update delegation status at %r: %s"
                 % (session_dir, exc))
        return False
    return True


# ---------------------------------------------------------------------------
# Mint — nested, idempotent.
# ---------------------------------------------------------------------------
def delegation_slug(section, request_meta, did):
    title = (request_meta or {}).get("title") if isinstance(
        request_meta, dict) else None
    base = _slug(title) or _slug(section) or "chat"
    return "%s-%s" % (base, did[:8])


def mint_delegation_session(root, project, section, request_meta,
                            reply_to=None, *, create_session,
                            created_at=None, on_error=None):
    """Mint (or return the existing) nested session for a delegated
    request. Returns the session dir, or None with the reason reported.

    Idempotent: the same request twice returns the SAME dir and never a
    duplicate (the request hash is baked into the slug, so the second
    call's derived path already exists). ``create_session`` is 3.0's
    nested-minting helper, injected to keep this module free of an
    orchestrator import."""
    if on_error is None:
        on_error = lambda _m: None
    if not isinstance(request_meta, dict):
        on_error("mint rejected: request_meta must be an object")
        return None
    did = delegation_id(reply_to, project, section, request_meta)
    slug = delegation_slug(section, request_meta, did)
    sid = "%s/%s/%s" % (project, section, slug)
    app_dir = os.path.join(root, sid)
    # A self-reply loop is structurally impossible: the slug hashes
    # reply_to, so the freshly-minted app_dir can never equal an existing
    # reply_to (that would require a hash fixed point). No runtime guard
    # needed — the delegation always replies to a DIFFERENT session.

    # Idempotent hit / crashed-mint resume.
    if os.path.isdir(app_dir):
        rec = read_delegation(app_dir, on_error=on_error)
        if rec is not None and rec.get("delegation_id") == did:
            return app_dir
        if rec is None:
            # A prior mint created the dir but died before (or while)
            # writing delegation.json — finish the job and return.
            try:
                _write_delegation(app_dir, _new_record(
                    did, request_meta, reply_to, created_at))
            except (OSError, TypeError, ValueError) as exc:
                on_error("mint resume failed at %r: %s" % (sid, exc))
                return None
            return app_dir
        on_error("mint rejected: %r already hosts a different delegation"
                 % (sid,))
        return None

    prompt = (request_meta.get("prompt") or request_meta.get("title") or "")
    workflow = request_meta.get("workflow")
    try:
        create_session(root, sid, prompt, workflow)
    except Exception as exc:  # noqa: BLE001 — injected helper's type is opaque
        # A race (another caller minted the same id between our isdir
        # check and here) is the benign case: re-check and adopt it.
        if os.path.isdir(app_dir):
            rec = read_delegation(app_dir, on_error=on_error)
            if rec is not None and rec.get("delegation_id") == did:
                return app_dir
        on_error("mint failed for %r: %s" % (sid, exc))
        return None
    try:
        _write_delegation(app_dir, _new_record(
            did, request_meta, reply_to, created_at))
    except (OSError, TypeError, ValueError) as exc:
        on_error("mint wrote the session dir but not delegation.json at "
                 "%r: %s" % (sid, exc))
        return None
    return app_dir


# ---------------------------------------------------------------------------
# Spawn — launch the engine, write run.pid, no secrets on argv.
# ---------------------------------------------------------------------------
def _engine_path():
    return os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "orchestrator.py")


def spawn_argv(root, sid):
    """The engine launch argv — only --root/--app, never key material.
    Pure so a test can assert the no-secrets contract without launching."""
    return [sys.executable, _engine_path(), "--root", root, "--app", sid]


def run_pid_path(session_dir):
    return os.path.join(session_dir, "run.pid")


def _write_pidfile(session_dir, pid):
    # A single-value liveness file (bare int) that 7.0's stop coverage can
    # int(open(...).read()); atomic overwrite reaps a stale prior pid.
    path = run_pid_path(session_dir)
    tmp = "%s.%d.tmp" % (path, os.getpid())
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write("%d\n" % pid)
    os.replace(tmp, path)


def read_pidfile(session_dir):
    try:
        with open(run_pid_path(session_dir), encoding="utf-8") as fh:
            return int(fh.read().strip())
    except (OSError, ValueError):
        return None


def spawn_run(session_dir, workflow=None, *, root, on_error=None,
              _popen=None):
    """Launch a detached engine run in the delegated session and return
    its pid, or None with the failure recorded as status='failed' (never a
    silent dead dir; §12.1). The workflow is already persisted by the mint
    (create_session writes workflow.txt); it is accepted here for API
    symmetry. argv carries no secrets and the child's env is scrubbed of
    key material."""
    if on_error is None:
        on_error = lambda _m: None
    sid = os.path.relpath(session_dir, root)
    argv = spawn_argv(root, sid)
    env = {k: v for k, v in os.environ.items()
           if not procutil.is_secret_env(k)}
    popen = _popen or subprocess.Popen
    try:
        proc = popen(argv, cwd=root, env=env, start_new_session=True)
    except (OSError, ValueError) as exc:
        set_status(session_dir, "failed", on_error=on_error,
                   failed_reason=str(exc)[:300])
        on_error("spawn failed for %r: %s" % (sid, exc))
        return None
    _write_pidfile(session_dir, proc.pid)
    set_status(session_dir, "running", on_error=on_error)
    return proc.pid


def _pid_alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def reconcile_delegation(session_dir, on_error=None):
    """Flip a 'running' delegation whose engine process is gone to
    'failed', so the originating session never waits forever on a reply
    that can never come. A library predicate — WIRING it into a watcher
    is 4.6/7.0; exit-code-aware refinement (clean exit that simply
    published nothing) is theirs too. Returns the resolved status."""
    if on_error is None:
        on_error = lambda _m: None
    rec = read_delegation(session_dir, on_error=on_error)
    if rec is None:
        return None
    if rec.get("status") != "running":
        return rec.get("status")
    pid = read_pidfile(session_dir)
    if pid is not None and _pid_alive(pid):
        return "running"
    set_status(session_dir, "failed", on_error=on_error,
               failed_reason="engine process is no longer alive")
    return "failed"


# ---------------------------------------------------------------------------
# Reply edge — deliver an artifact card to the origin inbox, exactly once.
# ---------------------------------------------------------------------------
def _reply_marker(artifact_id):
    # A distinctive, id-bearing substring so the inbox dedupe scan cannot
    # false-match on a title that merely contains the id.
    return "[artifact:%s]" % artifact_id


def _reply_line(rec, card):
    section = ""
    req = rec.get("request")
    if isinstance(req, dict):
        section = req.get("section") or ""
    return ("\n[reply from %s] %s %s v%s — %s\n"
            % (section or "delegation", _reply_marker(card["id"]),
               card.get("type", ""), card.get("version", 1),
               card.get("path", "")))


def _origin_saw(reply_to, marker):
    """Whether this reply already reached the origin durably — checked
    only on the rare crash-resume path (an artifact in `delivering` but
    not `delivered`). The live human_inbox.txt holds the marker BEFORE
    the origin drains it; drain_human_inbox folds the drained message
    (marker and all) into the origin's phase transcript, so a `.md` under
    the origin dir holds it AFTER. Checking both closes the
    crash-then-drain window that a live-inbox-only scan misses."""
    inbox = os.path.join(reply_to, "human_inbox.txt")
    try:
        if os.path.exists(inbox):
            with open(inbox, encoding="utf-8", errors="replace") as fh:
                if marker in fh.read():
                    return True
    except OSError:
        pass
    try:
        for base, _dirs, files in os.walk(reply_to):
            for name in files:
                if not name.endswith(".md"):
                    continue
                try:
                    with open(os.path.join(base, name), encoding="utf-8",
                              errors="replace") as fh:
                        if marker in fh.read():
                            return True
                except OSError:
                    continue
    except OSError:
        pass
    return False


def _emit_routed(session_dir, rec, aid, target):
    evlib.emit_event(
        session_dir, "artifact_routed",
        route_id="reply-%s-%s" % (rec.get("delegation_id", ""), aid),
        artifact_id=aid, target=target)


def deliver_reply(session_dir, card, on_error=None):
    """Append ONE artifact-card reference (id/type/version/path — never
    the body) to the delegation's reply_to inbox, exactly once, and mark
    the delegation replied. Driven by delegation STATE, not by the run's
    publish list, so a crash between publish and append still delivers on
    resume (the republish would return no new ids).

    Exactly-once uses the 7.3 intent->effect->done protocol: the artifact
    id is fsync'd into `delivering` BEFORE the append, then moved to
    `delivered` after. An id already in `delivered` is a no-op; an id in
    `delivering` (a crashed mid-flight attempt) is resolved by probing the
    origin's DURABLE record (_origin_saw) — deliver only if the append
    truly never landed, so neither a live inbox nor a drained-to-transcript
    reply is ever duplicated. Returns True if delivered (or reconciled as
    already-delivered), False if there was nothing to do."""
    if on_error is None:
        on_error = lambda _m: None
    rec = read_delegation(session_dir, on_error=on_error)
    if rec is None:
        return False
    reply_to = rec.get("reply_to")
    if not reply_to or rec.get("status") == "failed":
        return False
    if not isinstance(reply_to, str):
        on_error("deliver_reply: reply_to at %r is not a string"
                 % (session_dir,))
        return False
    aid = card.get("id")
    if not isinstance(aid, str):
        on_error("deliver_reply: card has no id")
        return False
    delivered = rec.get("delivered")
    delivered = delivered if isinstance(delivered, list) else []
    if aid in delivered:                       # DONE — no-op.
        return False
    delivering = rec.get("delivering")
    delivering = delivering if isinstance(delivering, list) else []
    marker = _reply_marker(aid)

    if aid in delivering:
        # A prior attempt crashed after recording intent. Did its append
        # land durably (live inbox pre-drain, or the origin transcript
        # post-drain)? If so, finish without re-appending; else re-attempt.
        if _origin_saw(reply_to, marker):
            _mark_delivered(session_dir, rec, aid, on_error)
            _emit_routed(session_dir, rec, aid, reply_to)
            return True
    else:
        # Record the INTENT durably before the effect.
        rec["delivering"] = list(delivering) + [aid]
        try:
            _write_delegation(session_dir, rec)
        except (OSError, TypeError, ValueError) as exc:
            on_error("could not record delivery intent at %r: %s"
                     % (session_dir, exc))
            return False

    inbox = os.path.join(reply_to, "human_inbox.txt")
    try:
        with open(inbox, "a", encoding="utf-8") as fh:
            fh.write(_reply_line(rec, card))
    except OSError as exc:
        # A transient/gone origin: report and leave the delegation
        # 'running' (intent recorded) so a later retry reconciles — never
        # crash, never claim a reply that did not land (R2).
        on_error("reply delivery to %r failed: %s" % (reply_to, exc))
        return False

    _mark_delivered(session_dir, rec, aid, on_error)
    _emit_routed(session_dir, rec, aid, reply_to)
    return True


def _mark_delivered(session_dir, rec, artifact_id, on_error):
    delivered = rec.get("delivered")
    delivered = list(delivered) if isinstance(delivered, list) else []
    if artifact_id not in delivered:
        delivered.append(artifact_id)
    delivering = rec.get("delivering")
    delivering = [x for x in delivering if x != artifact_id] \
        if isinstance(delivering, list) else []
    rec["delivered"] = delivered
    rec["delivering"] = delivering
    rec["status"] = "replied"
    try:
        _write_delegation(session_dir, rec)
    except (OSError, TypeError, ValueError) as exc:
        on_error("could not record reply delivery at %r: %s"
                 % (session_dir, exc))
