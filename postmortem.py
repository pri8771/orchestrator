"""Postmortem assembly for one project (`--postmortem --app <name>`).

When a run aborts or produces garbage, the evidence is scattered across five
files in the app dir — agent_state.json, events.jsonl, verify_results.json,
mistakes.jsonl, and logs/ pointers inside them. This module correlates those
into ONE report so the operator stops hand-joining timestamps:

    * run status (state["status"] / derive_run_status rules) + the §15
      verification rollup (state["verification"])
    * workflow + the last phase/round actually reached
    * phase-by-phase completion with each phase's consensus_status
    * the failure chain from events.jsonl (agent_fallback outcomes,
      agent_disabled, failed turns incl. timeouts, failed verify_result,
      url_fetch_failed, non-done run_finished)
    * every persisted verification attempt (verify_results.json)
    * the mistakes-ledger aggregation for this app (mistakes.jsonl)
    * per-phase / per-agent turn telemetry measured from turn_completed
      events (turn count, total/mean dur, total output chars, fallback
      count) — measured quantities only, no token/cost estimation
    * the final state error, if any

Pure disk reads, surfacing only — never invokes an agent turn, never mutates
the app dir, and never raises on missing/corrupt inputs (each section simply
degrades to empty). Standard library only, matching the rest of the engine.
"""

import datetime as _dt
import json
import os

import events as evlib
import mistakes as mistklib
import verify as verifylib

# turn_completed reasons that mean the turn FAILED (call_agent emits exactly
# one turn_completed per turn; ok=False carries one of these).
_TIMEOUT_REASONS = ("timeout", "no_output_timeout")

# agent_fallback statuses that are outcomes (the ledger's convention): the
# per-step "attempt" transitions are progress noise, not failures.
_FALLBACK_OUTCOMES = ("rescued", "failed", "skipped", "exhausted")


def _load_state(app_dir):
    """agent_state.json as a dict; {} when missing/corrupt. Reads the file
    directly (same shape orchestrator.load_state returns) so this module has
    no import edge back into the engine monolith."""
    try:
        with open(os.path.join(app_dir, "agent_state.json"), encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _run_status(state):
    """The persisted one-word rollup when present (save_state stamps it on
    every write), else the same derivation orchestrator.derive_run_status
    applies — mirrored here (8 lines of pure dict logic) rather than imported
    so a fixture/partial state file still gets a status without pulling in
    the whole engine."""
    got = state.get("status")
    if got:
        return str(got)
    if state.get("blocked_conflict"):
        return "blocked_conflict"
    if state.get("error"):
        return "aborted"
    if state.get("awaiting_approval"):
        return "awaiting_approval"
    if state.get("done"):
        return "done"
    return "running"


def _phase_rows(state):
    """Phase-by-phase completion joined with consensus_status. Ordered by
    completed_phases (the run's actual order), then any consensus-tracked
    phase that never completed (e.g. the one it died in)."""
    completed = [str(p) for p in (state.get("completed_phases") or [])]
    consensus = state.get("consensus_status") or {}
    rows = []
    seen = set()
    for key in completed:
        rows.append({"phase": key, "completed": True,
                     "consensus": consensus.get(key)})
        seen.add(key)
    for key in consensus:
        if key not in seen:
            rows.append({"phase": key, "completed": False,
                         "consensus": consensus.get(key)})
    current = state.get("current_phase")
    if current and current not in seen and current not in {r["phase"] for r in rows}:
        rows.append({"phase": current, "completed": False, "consensus": None})
    return rows


def _last_position(events, state):
    """The last phase/round a turn actually reached, from the event stream
    (turn events carry phase+round); falls back to state["current_phase"]."""
    phase, rnd = None, None
    for evt in events:
        if evt.get("kind") in ("turn_started", "turn_completed") and evt.get("phase"):
            phase = evt.get("phase")
            rnd = evt.get("round", rnd)
    if phase is None:
        phase = state.get("current_phase")
    return {"phase": phase, "round": rnd}


def _failure_entry(evt):
    """One failure-chain row for an error-ish event, or None. Error-ish =
    fallback outcomes, agent disables, failed turns (timeouts, empty output,
    provider banners, missing CLIs), failed verifications, URL fetch
    failures, and a run_finished that isn't 'done'."""
    kind = evt.get("kind")
    row = None
    if kind == "agent_fallback" and evt.get("status") in _FALLBACK_OUTCOMES:
        row = {"summary": "%s: %s -> %s (%s)" % (
            evt.get("agent", "?"), evt.get("from_model", "?"),
            evt.get("to_model") or "(none)", evt.get("status"))}
        if evt.get("reason"):
            row["detail"] = evt["reason"]
    elif kind == "agent_disabled":
        row = {"summary": "agent %s disabled" % evt.get("agent", "?"),
               "detail": evt.get("reason")}
    elif kind == "turn_completed" and evt.get("ok") is False:
        reason = evt.get("reason") or "failed"
        label = "timed out" if reason in _TIMEOUT_REASONS else reason
        row = {"summary": "%s turn %s" % (evt.get("agent", "?"), label),
               "detail": evt.get("detail")}
    elif kind == "verify_result" and evt.get("status") == "failed":
        row = {"summary": "verification FAILED", "detail": evt.get("detail")}
    elif kind == "url_fetch_failed":
        row = {"summary": "url fetch failed: %s" % evt.get("url", "?"),
               "detail": evt.get("reason")}
    elif kind == "run_finished" and evt.get("status") not in (None, "done"):
        row = {"summary": "run finished: %s" % evt.get("status"),
               "detail": evt.get("detail")}
    if row is None:
        return None
    entry = {"ts": evt.get("ts"), "kind": kind, "phase": evt.get("phase"),
             "round": evt.get("round"), "agent": evt.get("agent"),
             "summary": row["summary"]}
    if row.get("detail"):
        entry["detail"] = row["detail"]
    return entry


def _telemetry(events):
    """Per-phase and per-agent rollup of measured turn quantities from
    turn_completed events: turn count, total/mean dur (seconds), total output
    chars, plus fallback outcomes from agent_fallback events. Missing
    dur/output_len fields count as 0 toward totals (failed turns often carry
    no output_len). Measured only — no token or cost estimation."""
    def _fresh():
        return {"turns": 0, "failed_turns": 0, "total_dur": 0.0,
                "mean_dur": 0.0, "total_output_chars": 0, "fallbacks": 0}

    by_phase, by_agent = {}, {}

    def _buckets(evt):
        out = []
        phase = evt.get("phase")
        agent = evt.get("agent")
        if phase:
            out.append(by_phase.setdefault(str(phase), _fresh()))
        if agent:
            out.append(by_agent.setdefault(str(agent), _fresh()))
        return out

    for evt in events:
        kind = evt.get("kind")
        if kind == "turn_completed":
            try:
                dur = float(evt.get("dur") or 0)
            except (TypeError, ValueError):
                dur = 0.0
            try:
                chars = int(evt.get("output_len") or 0)
            except (TypeError, ValueError):
                chars = 0
            for b in _buckets(evt):
                b["turns"] += 1
                if evt.get("ok") is False:
                    b["failed_turns"] += 1
                b["total_dur"] += dur
                b["total_output_chars"] += chars
        elif kind == "agent_fallback" and evt.get("status") in _FALLBACK_OUTCOMES:
            for b in _buckets(evt):
                b["fallbacks"] += 1
    for buckets in (by_phase, by_agent):
        for b in buckets.values():
            b["total_dur"] = round(b["total_dur"], 1)
            b["mean_dur"] = round(b["total_dur"] / b["turns"], 1) if b["turns"] else 0.0
    return {"by_phase": by_phase, "by_agent": by_agent}


def _mistakes_rollup(app_dir):
    """This app's ledger aggregated by class/phase/agent, plus the most
    recent few records verbatim (they're already redacted + capped)."""
    recs = mistklib.load_mistakes(app_dir)
    agg = {"total": len(recs), "by_class": {}, "by_phase": {}, "by_agent": {},
           "recent": recs[-5:]}
    for rec in recs:
        for axis, key in (("by_class", "cls"), ("by_phase", "phase"),
                          ("by_agent", "agent")):
            v = rec.get(key)
            if v or axis == "by_class":
                v = str(v or "(unknown)")
                agg[axis][v] = agg[axis].get(v, 0) + 1
    return agg


def _verify_attempts(app_dir):
    return [{"ts": r.get("timestamp"), "phase": r.get("phase"),
             "attempt": r.get("attempt"), "repair": bool(r.get("repair_attempt")),
             "status": r.get("status"), "tool": r.get("tool"),
             "summary": r.get("summary")}
            for r in verifylib.load_verify_results(app_dir)
            if isinstance(r, dict)]


def build_postmortem(app_dir, app=None):
    """Assemble the full postmortem dict for one app dir. Pure reads; every
    section degrades to empty on missing/corrupt input; JSON-serializable
    (the `--postmortem --json` payload)."""
    state = _load_state(app_dir)
    events = evlib.read_events(app_dir)
    chain = []
    for evt in events:
        entry = _failure_entry(evt)
        if entry is not None:
            chain.append(entry)
    return {
        "schema_version": 1,
        "generated": _dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "app": app or os.path.basename(os.path.abspath(app_dir)),
        "app_dir": app_dir,
        "run": {
            "status": _run_status(state),
            "verification": state.get("verification", "unverified"),
            "workflow": state.get("workflow"),
            "done": bool(state.get("done")),
            "error": state.get("error"),
            "blocked_conflict": state.get("blocked_conflict"),
            "awaiting_approval": bool(state.get("awaiting_approval")),
            "last_processed": state.get("last_processed"),
            "last_position": _last_position(events, state),
            "fallback_counts": state.get("fallback_counts") or {},
        },
        "phases": _phase_rows(state),
        "failure_chain": chain,
        "verify_attempts": _verify_attempts(app_dir),
        "mistakes": _mistakes_rollup(app_dir),
        "telemetry": _telemetry(events),
        "events_total": len(events),
    }


def _fmt_counts(counts):
    return ", ".join("%s=%d" % (k, n) for k, n in
                     sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))


def render_postmortem(rep):
    """Human-readable text mode: a headline block, then timeline-ordered
    sections (failure chain and verify attempts keep their timestamps)."""
    run = rep.get("run") or {}
    pos = run.get("last_position") or {}
    lines = ["=== POSTMORTEM: %s ===" % rep.get("app"),
             "Generated: %s   (%d events)" % (rep.get("generated"),
                                              rep.get("events_total", 0)),
             "Run status  : %s   verification=%s" % (run.get("status"),
                                                     run.get("verification")),
             "Workflow    : %s" % (run.get("workflow") or "(unknown)"),
             "Last reached: phase=%s round=%s   (last processed %s)"
             % (pos.get("phase") or "(none)", pos.get("round") if pos.get("round")
                is not None else "?", run.get("last_processed") or "?")]
    if run.get("error"):
        lines.append("Final error : %s" % run["error"])
    if run.get("blocked_conflict"):
        lines.append("Blocked     : %s" % run["blocked_conflict"])

    lines.append("")
    lines.append("--- Phases ---")
    phases = rep.get("phases") or []
    if not phases:
        lines.append("(no phase records)")
    for row in phases:
        lines.append("%-28s %-12s consensus=%s"
                     % (row.get("phase"),
                        "completed" if row.get("completed") else "NOT completed",
                        row.get("consensus") or "-"))

    lines.append("")
    lines.append("--- Failure chain (events.jsonl, oldest first) ---")
    chain = rep.get("failure_chain") or []
    if not chain:
        lines.append("(no failure events recorded)")
    for e in chain:
        where = "/".join(str(x) for x in (e.get("phase"), e.get("round"))
                         if x is not None)
        lines.append("%s  [%s]%s %s" % (e.get("ts"), e.get("kind"),
                                        " " + where if where else "",
                                        e.get("summary")))
        if e.get("detail"):
            lines.append("    %s" % e["detail"])

    lines.append("")
    lines.append("--- Verification attempts (verify_results.json) ---")
    attempts = rep.get("verify_attempts") or []
    if not attempts:
        lines.append("(none persisted)")
    for a in attempts:
        lines.append("%s  %-10s attempt=%s%s tool=%s phase=%s — %s"
                     % (a.get("ts"), (a.get("status") or "?").upper(),
                        a.get("attempt"), " (repair)" if a.get("repair") else "",
                        a.get("tool"), a.get("phase"), a.get("summary") or ""))

    lines.append("")
    lines.append("--- Mistakes ledger (mistakes.jsonl) ---")
    mk = rep.get("mistakes") or {}
    lines.append("Total: %d" % mk.get("total", 0))
    for label, key in (("By class", "by_class"), ("By phase", "by_phase"),
                       ("By agent", "by_agent")):
        if mk.get(key):
            lines.append("%s: %s" % (label, _fmt_counts(mk[key])))
    for rec in mk.get("recent") or []:
        lines.append("  %s  [%s] %s" % (rec.get("ts"), rec.get("cls"),
                                        rec.get("summary", "")))

    lines.append("")
    lines.append("--- Turn telemetry (measured from turn_completed events) ---")
    tel = rep.get("telemetry") or {}
    for label, key in (("Per phase", "by_phase"), ("Per agent", "by_agent")):
        rows = tel.get(key) or {}
        lines.append("%s:" % label)
        if not rows:
            lines.append("  (no turns recorded)")
        for name, b in sorted(rows.items()):
            lines.append("  %-26s turns=%d (failed=%d)  dur total=%.1fs "
                         "mean=%.1fs  output=%d chars  fallbacks=%d"
                         % (name, b["turns"], b["failed_turns"], b["total_dur"],
                            b["mean_dur"], b["total_output_chars"], b["fallbacks"]))
    lines.append("=== POSTMORTEM complete ===")
    return "\n".join(lines)
