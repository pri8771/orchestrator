#!/usr/bin/env python3
"""Docs backfill: seed pending workflow phases from user-supplied docs.

When <app_dir>/.backfill_requested exists AND <app_dir>/docs/ contains usable
files, the engine calls run_backfill() right after the workflow/phases are
loaded for a run (see orchestrator._run_app_pipeline). Each not-yet-completed
phase gets ONE claude call asking it to distill the docs into that phase's
expected output. The backfill loop STOPS (and the rest of the pipeline runs
live) at the first phase that writes code (writes:true), carries a verify
spec, or is a QA/review/verification gate — those must always run for real —
and stops entirely the first time the docs don't cover a phase (NOT_COVERED)
or the agent call fails.

Standard library only, matching the rest of the engine.
"""

import os
import shutil
import subprocess

MARKER = ".backfill_requested"
MAX_DOC_CHARS = 50000

_INSTRUCTIONS = (
    "If these docs substantially cover this phase's purpose, distill them into "
    "the phase's expected output (markdown, expand details the docs clearly "
    "imply). If they do not, reply with exactly NOT_COVERED."
)


def _engine():
    # Lazy import: orchestrator imports this module at startup, so import it
    # back only at call time (it is fully initialized by then). This gives us
    # the engine's emit() and save_state() without a circular module import.
    import orchestrator
    return orchestrator


def _emit(msg):
    _engine().emit(msg)


def _pget(phase, key, default=None):
    """Phase objects may be workflows.Phase instances OR plain dicts (both
    expose .get); anything else falls back to the default, mirroring the
    hasattr-guard style used across orchestrator.py."""
    if hasattr(phase, "get"):
        return phase.get(key, default)
    return default


def _phase_key(phase):
    import workflows
    return workflows.phase_key(phase)  # one shared accessor across modules


def _remove_marker(app_dir):
    try:
        os.remove(os.path.join(app_dir, MARKER))
    except OSError:
        pass


def gather_docs(app_dir, emit=_emit):
    """Concatenate <app_dir>/docs/*.md and *.txt (and *.pdf via `pdftotext`
    when installed — otherwise the PDF is skipped with a warning). The result
    is capped at MAX_DOC_CHARS (truncated with a warning). Returns '' when
    there is nothing usable."""
    docs_dir = os.path.join(app_dir, "docs")
    parts = []
    try:
        names = sorted(os.listdir(docs_dir))
    except OSError:
        return ""
    for fn in names:
        path = os.path.join(docs_dir, fn)
        if not os.path.isfile(path):
            continue
        low = fn.lower()
        if low.endswith((".md", ".txt")):
            try:
                with open(path, encoding="utf-8", errors="replace") as fh:
                    parts.append("### %s\n\n%s" % (fn, fh.read().strip()))
            except OSError as exc:
                emit("BACKFILL WARN: could not read docs/%s (%s) — skipped."
                     % (fn, exc))
        elif low.endswith(".pdf"):
            if not shutil.which("pdftotext"):
                emit("BACKFILL WARN: docs/%s skipped — pdftotext not installed."
                     % fn)
                continue
            try:
                res = subprocess.run(["pdftotext", path, "-"],
                                     capture_output=True, text=True, timeout=120)
                if res.returncode == 0 and res.stdout.strip():
                    parts.append("### %s\n\n%s" % (fn, res.stdout.strip()))
                else:
                    emit("BACKFILL WARN: pdftotext failed on docs/%s — skipped."
                         % fn)
            except (OSError, subprocess.SubprocessError):
                emit("BACKFILL WARN: pdftotext failed on docs/%s — skipped." % fn)
    text = "\n\n".join(parts)
    if len(text) > MAX_DOC_CHARS:
        emit("BACKFILL WARN: docs truncated to %d chars (had %d)."
             % (MAX_DOC_CHARS, len(text)))
        text = text[:MAX_DOC_CHARS]
    return text


def _live_only(phase):
    """True for phases that must NEVER be backfilled: anything that writes
    code, carries a verify spec, must be verified, reads the built target, or
    is a QA/review/verification gate. requires_verification/reads_target phases
    have a real gate that a distilled-from-docs stand-in would skip."""
    if (_pget(phase, "writes", False) or _pget(phase, "verify")
            or _pget(phase, "requires_verification", False)
            or _pget(phase, "reads_target", False)):
        return True
    low = str(_phase_key(phase) or "").lower()
    return "qa" in low or "review" in low or "verification" in low


def run_backfill(cfg, app, app_dir, phases, state, call_agent):
    """Backfill pending phases from <app_dir>/docs/ (see module docstring).

    Marker semantics: absent marker (or no usable docs) = no-op. Otherwise the
    marker is one-shot — it is deleted when the loop finishes, when it stops at
    a live-only phase, and when the docs don't cover a phase (NOT_COVERED) or
    the agent call fails, so a partial backfill is never retried blindly."""
    if not os.path.exists(os.path.join(app_dir, MARKER)):
        return
    docs = gather_docs(app_dir)
    if not docs.strip():
        return
    completed = state.setdefault("completed_phases", [])
    outputs = state.setdefault("phase_outputs", {})
    for phase in phases:
        key = _phase_key(phase)
        if key in completed:
            continue
        if _live_only(phase):
            # Build/QA always runs live — stop the whole backfill loop here.
            _emit("App '%s': backfill stopping at phase '%s' — build/QA phases "
                  "always run live." % (app, key))
            break
        prompt = ("## User-supplied docs\n\n%s\n\n## Phase purpose\n\n%s\n\n"
                  "## Instructions\n\n%s"
                  % (docs, str(_pget(phase, "purpose") or ""), _INSTRUCTIONS))
        try:
            reply = call_agent(cfg, app, key, "backfill", "claude", prompt)
        except Exception as exc:
            _emit("App '%s': backfill agent call failed on phase '%s' (%s) — "
                  "stopping backfill; remaining phases run live." % (app, key, exc))
            _remove_marker(app_dir)
            return
        # An empty/blank reply is NOT coverage — treat it like NOT_COVERED so a
        # phase is never marked complete against an empty backfilled doc.
        reply_text = (reply or "").strip()
        if reply_text == "NOT_COVERED" or not reply_text:
            _emit("App '%s': docs do not cover phase '%s' (or the reply was "
                  "empty) — stopping backfill; remaining phases run live."
                  % (app, key))
            _remove_marker(app_dir)
            return
        folder = str(_pget(phase, "folder") or key)
        fname = str(_pget(phase, "file") or (key + ".md"))
        title = str(_pget(phase, "title") or key.replace("_", " ").title())
        # folder/fname come from workflow/phase config; refuse a value that
        # joins to a path outside app_dir (a "../" or absolute escape).
        app_real = os.path.realpath(app_dir)
        dest = os.path.realpath(os.path.join(app_dir, folder, fname))
        try:
            within = os.path.commonpath([app_real, dest]) == app_real
        except ValueError:
            within = False
        if not within:
            _emit("App '%s': backfill target '%s/%s' escapes the app directory "
                  "— stopping backfill." % (app, folder, fname))
            _remove_marker(app_dir)
            return
        # Atomic write (per-writer temp name + os.replace): a crash/kill mid-
        # write must never leave a truncated file that completed/outputs/
        # save_state below then treat as a legitimately backfilled phase.
        body = ("# %s — %s\n\n_Backfilled from user-supplied docs._\n\n%s"
               % (app, title, reply))
        tmp = "%s.%d.tmp" % (dest, os.getpid())
        try:
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as fh:
                fh.write(body)
            os.replace(tmp, dest)
        except OSError as exc:
            try:
                os.remove(tmp)
            except OSError:
                pass
            _emit("App '%s': backfill could not write %s/%s (%s) — stopping "
                  "backfill." % (app, folder, fname, exc))
            _remove_marker(app_dir)
            return
        if key not in completed:
            completed.append(key)
        outputs[key] = reply
        _engine().save_state(app_dir, state)
        _emit("App '%s': phase '%s' backfilled from user-supplied docs."
              % (app, key))
    _remove_marker(app_dir)
