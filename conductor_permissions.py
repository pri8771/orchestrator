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
DECISION_SUFFIXES = ("changes", "do_not_route", "kill_session", "edit", "ok")


def _cdir(root):
    return os.path.join(root, CONDUCTOR_DIRNAME)


def pending_path(root):
    return os.path.join(_cdir(root), PENDING_FILENAME)


def approvals_dir(root):
    return os.path.join(_cdir(root), APPROVALS_DIRNAME)


def pending_file(root, route_id):
    return os.path.join(approvals_dir(root), "%s.pending" % route_id)


def _atomic_json(path, value):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = "%s.%d.tmp" % (path, os.getpid())
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(value, fh, indent=1, sort_keys=True)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
        return True
    except OSError:
        try:
            os.remove(tmp)
        except OSError:
            pass
        return False


def _decision_body(root, route_id, suffix, max_chars=1000):
    try:
        with open(os.path.join(approvals_dir(root),
                               "%s.%s" % (route_id, suffix)),
                  encoding="utf-8") as fh:
            return fh.read(max_chars + 1)[:max_chars].strip()
    except OSError:
        return ""


def read_approval_decision(root, route_id):
    """Structured operator decision; deny/undo always wins over approval."""
    adir = approvals_dir(root)
    present = {suffix for suffix in DECISION_SUFFIXES
               if os.path.exists(os.path.join(
                   adir, "%s.%s" % (route_id, suffix)))}
    if "do_not_route" in present:
        choice = "do_not_route"
    elif "changes" in present:
        choice = "rejected"
    elif "kill_session" in present:
        choice = "kill_session"
    elif "edit" in present:
        choice = "edited"
    elif "ok" in present:
        choice = "approved"
    else:
        return None
    suffix = {"rejected": "changes", "approved": "ok",
              "edited": "edit"}.get(choice, choice)
    return {"decision": choice,
            "reason": _decision_body(
                root, route_id, suffix,
                max_chars=65536 if choice == "edited" else 1000)}


def approval_decision(root, route_id):
    """The operator's decision for a pending route, or None if still pending.
    Non-blocking: one stat pair per call, checked on each poll. `.ok` ->
    "approved" (execute the effect), `.changes` -> "rejected" (drop it). A
    stray both-files case resolves to "rejected" (deny wins — never act on an
    ambiguous approval)."""
    decision = read_approval_decision(root, route_id)
    return decision.get("decision") if decision else None


def consume_decision(root, route_id):
    """Best-effort cleanup after the ledger/state durably records a choice."""
    for suffix in DECISION_SUFFIXES:
        try:
            os.remove(os.path.join(approvals_dir(root),
                                   "%s.%s" % (route_id, suffix)))
        except OSError:
            pass


def pending_action(intent, session_id, kind="route", reason=""):
    """The restart-safe queue record for a route awaiting approval — carries
    everything needed to execute it later without re-deriving from live
    state, plus the route_id both the approval file and 7.3 dedup key on."""
    return {"action_id": intent.route_id, "route_id": intent.route_id,
            "kind": kind, "requested_by": session_id,
            "target": intent.target,
            "reason": str(reason or "route requires approval")[:500],
            "payload": {"artifact_id": intent.artifact_id,
                        "content_hash": intent.content_hash,
                        "source_section": intent.source_section,
                        "rule_id": intent.rule_id,
                        "strategy": intent.strategy}}


def read_pending(root):
    """Every queued action, oldest first; malformed lines skipped (a corrupt
    line must not hide the rest — §6.2)."""
    out = []
    positions = {}
    try:
        with open(pending_path(root), encoding="utf-8") as fh:
            for line in fh:
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if isinstance(rec, dict) and rec.get("action_id"):
                    action_id = rec["action_id"]
                    if action_id in positions:
                        out[positions[action_id]] = rec
                    else:
                        positions[action_id] = len(out)
                        out.append(rec)
    except OSError:
        pass
    # Crash recovery: enqueue writes the atomic per-action mirror BEFORE the
    # queue append. If the process dies in that narrow gap, the mirror is the
    # durable request and must be recovered rather than shown in the GUI as an
    # approval card the Conductor can never act on.
    try:
        names = sorted(os.listdir(approvals_dir(root)))
    except OSError:
        names = []
    for name in names:
        if not name.endswith(".pending"):
            continue
        try:
            with open(os.path.join(approvals_dir(root), name),
                      encoding="utf-8") as fh:
                rec = json.load(fh)
        except (OSError, ValueError):
            continue
        if isinstance(rec, dict) and rec.get("action_id"):
            action_id = rec["action_id"]
            if action_id in positions:
                # The atomic mirror is newer than the append-only queue line
                # (plan approval/edit updates use it as the durable current
                # action). It must override, not be hidden by, the old line.
                out[positions[action_id]] = rec
            else:
                positions[action_id] = len(out)
                out.append(rec)
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
        return _atomic_json(pending_file(root, action["action_id"]), action)
    try:
        if not _atomic_json(pending_file(root, action["action_id"]), action):
            return False
        os.makedirs(_cdir(root), exist_ok=True)
        with open(pending_path(root), "a", encoding="utf-8") as fh:
            fh.write(json.dumps(action, ensure_ascii=False) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        return True
    except OSError:
        try:
            os.remove(pending_file(root, action["action_id"]))
        except OSError:
            pass
        return False


def remove_pending(root, action_id):
    """Drop a resolved action (approved+executed, or rejected) by rewriting
    the queue without it. Atomic tmp+replace so a crash mid-rewrite never
    corrupts the queue. Missing file / absent id is a silent no-op."""
    actions = [a for a in read_pending(root) if a.get("action_id") != action_id]
    path = pending_path(root)
    tmp = "%s.%d.tmp" % (path, os.getpid())
    try:
        if os.path.exists(path):
            with open(tmp, "w", encoding="utf-8") as fh:
                for a in actions:
                    fh.write(json.dumps(a, ensure_ascii=False) + "\n")
                fh.flush()
                os.fsync(fh.fileno())   # durability parity with enqueue_pending
            os.replace(tmp, path)
        try:
            os.remove(pending_file(root, action_id))
        except OSError:
            pass
    except OSError:
        try:
            os.remove(tmp)
        except OSError:
            pass
