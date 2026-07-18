"""<app_dir>/messages.jsonl — the machine-readable transcript index (V3 2.4).

The regex-parsed markdown transcript stays AUTHORITATIVE (resume and the
GUI's TranscriptParser depend on its bytes); this file is an index over it
for machine consumers (2.6 search, jump-to-turn, future SessionModel). One
line per AUTHORED transcript block — a block gets a line iff it has an
author (an agent, the human, or the orchestrator-as-tally); system notes
(worker-stall, iteration-verify failures, deferral footnotes) and
round/iteration headers get none.

Line shape (ids and paths only — the body lives in the phase .md that
content_path points at; a line carrying a message body is a
review-blocking defect):

    {turn_id, phase, kind, agent, role, persona, round, ts, content_path}

turn_id is deterministic: "<phase>:<token>:<author>:<kind>[.<slot>][:<seq>]"

  * round-scoped kinds (turn, pass, skip, human, quality, review,
    integrator, coordinator, retry): token = str(round); repair uses the
    attempt number as its token/round.
  * the human kind carries a structural slot — a debate round drains the
    inbox TWICE (round-open and pre-coordinator), so the ids are
    "<phase>:<N>:human:human.open" / "…:human.coord" / "…:human.closing".
    Slots are positional, not counted, so they are resume-deterministic.
  * parallel-build lanes author by WORKER SLUG, never agent id — a roster
    can field two lanes of the same agent; the JSONL agent field still
    carries the agent id and role carries the lane label.
  * conversational retries append ":<seq>" from a per-(round, agent)
    in-run counter; safe because conversational resume is append-only, so
    a restarted chat can never regenerate an old (round, seq) pair.
  * phase-final kinds (vote, tally, contract): token = "final" (contract:
    "final.<contract-kind>"), round = 0.
  * POST-ROUND kinds (repair, vote, tally, contract) get an automatic
    dedupe suffix (":2", ":3", …) when the base id already exists in the
    file. This is how a crash-resume stays honest: the engine's resume
    truncation KEEPS post-round blocks (the last complete round's segment
    extends to end-of-file), and the resumed run re-appends a second
    Repair/Forced-Vote section to the .md — so the index must carry BOTH
    sets under distinct ids, exactly mirroring the transcript.

Contract (mirrors events.py): appends are best-effort and NEVER raise —
the markdown transcript must complete regardless of this index; one
write() of one "\n"-terminated line on an append handle (atomic under
PIPE_BUF, so parallel build lanes cannot interleave partial lines); string
fields are secret-redacted and capped. Each successful append emits one
message_appended event (ids+paths only).

reconcile_messages() is the resume-consistency half, and its drop
predicate MIRRORS _resume_round_state's truncation semantics — no more,
no less. A resume-truncate cuts ONLY a trailing incomplete round (an
incomplete round means the crash was mid-rounds, so no post-round blocks
exist yet; when post-round blocks DO exist, all rounds completed and the
truncation keeps the whole file): reconcile therefore drops only
round-scoped lines at or above the resume round, never post-round kinds.
A fresh-start rewrite (write_md of a new header, incl. the skipped-phase
path) truncates EVERYTHING: reconcile drops every line of the phase
(drop_post_round=True). Atomic via temp file + os.replace; crashing
between the .md truncate and the rewrite is safe because both steps are
idempotent re-entered in md-first order.

Standard library only. This module imports schemas/events only — never
turncontext and never cfg: it is an I/O sink, peer of events.py.
"""

import datetime as _dt
import json
import os
import tempfile
import types
import typing

import events as evlib

_schemalib: typing.Optional[types.ModuleType]
try:
    import schemas as _schemalib
except Exception:  # noqa: BLE001 - the index must work even if schemas breaks
    _schemalib = None

MESSAGES_FILENAME = "messages.jsonl"
# Kinds whose blocks live AFTER the round headers. Resume truncation keeps
# them (see the module docstring), so resume reconciliation must never
# drop them — and re-appended sections dedupe-suffix instead of colliding.
POST_ROUND_KINDS = ("repair", "vote", "tally", "contract")
FINAL_STAGE_KINDS = ("vote", "tally", "contract")   # token="final" ids
_MAX_FIELD_CHARS = 300
_MAX_LINE_BYTES = 3500   # same PIPE_BUF reasoning as events.py


def messages_path(app_dir):
    return os.path.join(app_dir, MESSAGES_FILENAME)


def turn_id(phase, token, author, kind, slot="", seq=None):
    tid = "%s:%s:%s:%s" % (phase, token, author,
                           kind + (("." + slot) if slot else ""))
    if seq:
        tid += ":%d" % seq
    return tid


def _clean(value):
    if not isinstance(value, str):
        return value
    v = value
    if _schemalib is not None:
        try:
            v = _schemalib.redact_secrets(v)
        except Exception:  # noqa: BLE001 - redaction is defense in depth
            pass
    return v[:_MAX_FIELD_CHARS]


def append_message(app_dir, phase, kind, author, md_path, *, agent=None,
                   role="", persona="", rnd=0, token=None, slot="", seq=None):
    """Append one index line for an authored block just written to md_path.

    The ONE way lines are produced — call sites never hand-roll ids or
    fields. Returns True when the line was written; False otherwise, and
    never raises (a broken disk must not delay or abort the turn)."""
    try:
        if not app_dir or not phase or not kind or not author:
            return False
        tid = turn_id(phase, str(rnd) if token is None else str(token),
                      author, kind, slot=slot, seq=seq)
        if kind in POST_ROUND_KINDS:
            # Crash-resume re-runs these stages while their old blocks
            # survive in the .md — suffix instead of colliding (docstring).
            taken = {m.get("turn_id") for m in read_messages(app_dir)}
            if tid in taken:
                n = 2
                while "%s:%d" % (tid, n) in taken:
                    n += 1
                tid = "%s:%d" % (tid, n)
        line_obj = {
            "turn_id": _clean(tid),
            "phase": _clean(str(phase)),
            "kind": _clean(str(kind)),
            "agent": _clean(str(agent if agent is not None else author)),
            "role": _clean(str(role or "")),
            "persona": _clean(str(persona or "")),
            "round": int(rnd or 0),
            "ts": _dt.datetime.now().astimezone().isoformat(timespec="seconds"),
            "content_path": _clean(os.path.relpath(md_path, app_dir)),
        }
        line = json.dumps(line_obj, default=str)
        if len(line.encode("utf-8", "replace")) > _MAX_LINE_BYTES:
            return False   # ids+paths only — a line this big is a defect
        with open(messages_path(app_dir), "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
        evlib.emit_event(app_dir, "message_appended",
                         turn_id=line_obj["turn_id"],
                         content_path=line_obj["content_path"])
        return True
    except Exception:  # noqa: BLE001 - best-effort by contract
        return False


def read_messages(app_dir):
    """Every parseable line, oldest first; corrupt/partial lines skipped.
    Never raises (missing file -> [])."""
    out = []
    try:
        # errors="replace": a crash-torn non-UTF-8 byte must cost one line
        # (its json.loads fails), never the whole read.
        with open(messages_path(app_dir), encoding="utf-8",
                  errors="replace") as fh:
            for raw in fh:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    obj = json.loads(raw)
                except ValueError:
                    continue
                if isinstance(obj, dict):
                    out.append(obj)
    except OSError:
        pass
    return out


def reconcile_messages(app_dir, phase, keep_below_round,
                       drop_post_round=False):
    """Drop this phase's lines whose blocks just left the .md, mirroring
    the engine's truncation semantics (module docstring): round-scoped
    lines with round >= keep_below_round; post-round kinds only when
    drop_post_round (the fresh-start/skip rewrites, which truncate
    everything). Other phases' lines are untouched. Atomic (temp +
    os.replace); never raises; returns True when the file is consistent
    (including the nothing-to-do cases)."""
    try:
        path = messages_path(app_dir)
        if not os.path.exists(path):
            return True
        kept, dropped = [], 0
        with open(path, encoding="utf-8", errors="replace") as fh:
            for raw in fh:
                line = raw.rstrip("\n")
                if not line.strip():
                    continue
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue   # corrupt lines do not survive a rewrite
                if isinstance(obj, dict) and obj.get("phase") == phase:
                    if obj.get("kind") in POST_ROUND_KINDS:
                        drop = drop_post_round
                    else:
                        # A malformed round costs ITS line a keep-decision,
                        # never the whole reconciliation.
                        try:
                            drop = int(obj.get("round") or 0) >= keep_below_round
                        except (TypeError, ValueError):
                            drop = False
                    if drop:
                        dropped += 1
                        continue
                kept.append(line)
        if not dropped:
            return True
        fd, tmp = tempfile.mkstemp(dir=app_dir, prefix=".messages-",
                                   suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                for line in kept:
                    fh.write(line + "\n")
            os.replace(tmp, path)
        finally:
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass
        return True
    except Exception:  # noqa: BLE001 - best-effort by contract
        return False
