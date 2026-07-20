"""Conductor route permissions (V3 7.4b): the capability gate + non-blocking
approval queue that sits between a planned route and its mint.

A route into a section that stays workspace-only fires immediately. A route
into a section that escalates beyond workspace-only (exec or external — see
sections.exceeds_workspace_only) NEVER fires directly: it enqueues a pending
action and waits, across polls, for an operator decision file — so the whole
poll loop is never blocked on one route (§13.5: no direct destructive AI
action; plan §11.1: escalated routes require approval regardless of dial).

The mechanism is deliberately file-and-state, not a sleep loop: a request is
enqueued to <root>/.conductor/pending_actions.jsonl and re-evaluated on each
later poll by probing <root>/.conductor/approvals/<route_id>.{ok,changes}.
Restart-safe: the queue is on disk, dedup by action_id, and an approval that
lands while the conductor is down is picked up on the next poll.

This is a leaf (stdlib only). The oversight dials (7.6) compose ON this — they
decide auto-approve vs. hold — so nothing here hardcodes a policy beyond the
capability floor.
"""
import json
import os

CONDUCTOR_DIRNAME = ".conductor"
PENDING_FILENAME = "pending_actions.jsonl"
APPROVALS_DIRNAME = "approvals"


def _cdir(root):
    return os.path.join(root, CONDUCTOR_DIRNAME)


def pending_path(root):
    return os.path.join(_cdir(root), PENDING_FILENAME)


def approvals_dir(root):
    return os.path.join(_cdir(root), APPROVALS_DIRNAME)


def approval_decision(root, route_id):
    """The operator's decision for a pending route, or None if still pending.
    Non-blocking: one stat pair per call, checked on each poll. `.ok` ->
    "approved" (execute the effect), `.changes` -> "rejected" (drop it). A
    stray both-files case resolves to "rejected" (deny wins — never act on an
    ambiguous approval)."""
    adir = approvals_dir(root)
    ok = os.path.exists(os.path.join(adir, "%s.ok" % route_id))
    changes = os.path.exists(os.path.join(adir, "%s.changes" % route_id))
    if changes:
        return "rejected"
    if ok:
        return "approved"
    return None


def pending_action(intent, session_id, kind="route"):
    """The restart-safe queue record for a route awaiting approval — carries
    everything needed to execute it later without re-deriving from live
    state, plus the route_id both the approval file and 7.3 dedup key on."""
    return {"action_id": intent.route_id, "route_id": intent.route_id,
            "kind": kind, "requested_by": session_id,
            "target": intent.target,
            "payload": {"artifact_id": intent.artifact_id,
                        "content_hash": intent.content_hash,
                        "source_section": intent.source_section,
                        "rule_id": intent.rule_id}}


def read_pending(root):
    """Every queued action, oldest first; malformed lines skipped (a corrupt
    line must not hide the rest — §6.2)."""
    out = []
    try:
        with open(pending_path(root), encoding="utf-8") as fh:
            for line in fh:
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if isinstance(rec, dict) and rec.get("action_id"):
                    out.append(rec)
    except OSError:
        return []
    return out


def is_pending(root, action_id):
    return any(a.get("action_id") == action_id for a in read_pending(root))


def enqueue_pending(root, action):
    """Append one action, idempotently (a re-request for a route already
    queued is a no-op — the approval is still outstanding). Best-effort like
    the ledger's siblings, but returns False on failure so the caller can
    ledger the miss rather than silently drop a gated effect."""
    if not isinstance(action, dict) or not action.get("action_id"):
        return False
    if is_pending(root, action["action_id"]):
        return True
    try:
        os.makedirs(_cdir(root), exist_ok=True)
        with open(pending_path(root), "a", encoding="utf-8") as fh:
            fh.write(json.dumps(action, ensure_ascii=False) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        return True
    except OSError:
        return False


def remove_pending(root, action_id):
    """Drop a resolved action (approved+executed, or rejected) by rewriting
    the queue without it. Atomic tmp+replace so a crash mid-rewrite never
    corrupts the queue. Missing file / absent id is a silent no-op."""
    actions = [a for a in read_pending(root) if a.get("action_id") != action_id]
    path = pending_path(root)
    if not os.path.exists(path):
        return
    tmp = "%s.%d.tmp" % (path, os.getpid())
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            for a in actions:
                fh.write(json.dumps(a, ensure_ascii=False) + "\n")
            fh.flush()
            os.fsync(fh.fileno())   # durability parity with enqueue_pending
        os.replace(tmp, path)
    except OSError:
        try:
            os.remove(tmp)
        except OSError:
            pass
