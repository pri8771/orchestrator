#!/usr/bin/env python3
"""Local Git contract for deterministic project documentation (V3 5.5).

Topology
--------
Each project owns an independent repository at ``<app_dir>/docs/.git``.  The
project's parent ``.gitignore`` contains ``docs/`` so a workspace-level commit
never records the nested repository as a gitlink and generated-app history
under ``app_build/`` remains unrelated.  This card is deliberately local-only:
no command here adds, contacts, or even inspects a remote.

The lifecycle is split around the deterministic renderer:

``prepare_render`` initializes/baselines the docs repository, diffs tracked
rendered files against HEAD, and atomically records human overrides.
``finish_render`` publishes idempotent reconciliation requests and makes one
bot-identity milestone commit.  Every function is best-effort and reports via
``on_warn``; documentation must still render when Git is absent or hung.
"""

import datetime
import hashlib
import json
import os
import subprocess

import artifacts as artifactslib
import procutil


STATE_FILENAME = ".docsync_state.json"
STATE_SCHEMA_VERSION = 1
BOT_NAME = "orchestrator-docs"
BOT_EMAIL = "docs@orchestrator.local"
GIT_TIMEOUT = 30
_PARENT_IGNORE_RULE = "docs/"

_ALL_SLOT_FILES = {
    "phase_outputs.json", "PROJECT_DOCUMENTATION.md", "LAUNCH_READINESS.md",
    "KNOWN_LIMITATIONS.md", "HANDOFF_BLUEPRINT.md", "GAP_REPORT.md",
    "phase_discussions.json", "COMPLETE_PROJECT_DOSSIER.md",
    "FULL_TRANSCRIPT.txt", "PROJECT_RECORD.json",
}
_DOC_SLOT_CATEGORIES = {
    "prd": {"ideas", "research", "planning_spec"},
    "technical_architecture": {"design", "build"},
    "qa_report": {"qa_redteam"},
}


def _warn(on_warn, message):
    if on_warn is not None:
        on_warn("docsync: " + str(message))


def _git(repo_dir, *args, timeout=GIT_TIMEOUT):
    """Return ``(code, out, err)``; timeout kills Git's whole process group."""
    try:
        out, err, code = procutil.run_capture(
            ["git", "-C", repo_dir] + list(args), timeout=timeout)
        return code, out, err
    except subprocess.TimeoutExpired:
        return 124, "", "git timed out after %ss" % timeout
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, "", str(exc)


def _atomic_json(path, value):
    tmp = "%s.%d.tmp" % (path, os.getpid())
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(value, fh, indent=2, sort_keys=True)
            fh.write("\n")
        os.replace(tmp, path)
    except OSError:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def _state_path(app_dir):
    return os.path.join(app_dir, "docs", STATE_FILENAME)


def _default_state():
    return {"schema_version": STATE_SCHEMA_VERSION, "files": {}, "slots": {}}


def _load_state_ex(app_dir, on_warn=None):
    path = _state_path(app_dir)
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict) or not isinstance(data.get("files"), dict) \
                or not isinstance(data.get("slots"), dict):
            raise ValueError("state must contain files and slots objects")
        data["schema_version"] = STATE_SCHEMA_VERSION
        return data, "ok"
    except FileNotFoundError:
        return _default_state(), "missing"
    except (OSError, ValueError) as exc:
        _warn(on_warn, "state unreadable (%s) — rebuilding from the Git diff" % exc)
        return _default_state(), "corrupt"


def load_state(app_dir, on_warn=None):
    return _load_state_ex(app_dir, on_warn)[0]


def save_state(app_dir, state):
    os.makedirs(os.path.join(app_dir, "docs"), exist_ok=True)
    _atomic_json(_state_path(app_dir), state)


def _ensure_parent_ignore(app_dir, on_warn=None):
    path = os.path.join(app_dir, ".gitignore")
    try:
        existing = ""
        if os.path.exists(path):
            with open(path, encoding="utf-8") as fh:
                existing = fh.read()
        if _PARENT_IGNORE_RULE in existing.splitlines():
            return
        with open(path, "a", encoding="utf-8") as fh:
            if existing and not existing.endswith("\n"):
                fh.write("\n")
            if not existing:
                fh.write("# Managed by the orchestrator — local docs history.\n")
            fh.write(_PARENT_IGNORE_RULE + "\n")
    except OSError as exc:
        _warn(on_warn, "could not exclude docs repo from parent history (%s)" % exc)


def _commit(repo_dir, message, allow_empty=False, on_warn=None):
    code, _out, err = _git(repo_dir, "add", "-A")
    if code != 0:
        _warn(on_warn, "git add failed (%s)" % (err.strip() or code))
        return False
    if not allow_empty:
        clean, _out, _err = _git(repo_dir, "diff", "--cached", "--quiet")
        if clean == 0:
            return True
    args = ["-c", "user.name=%s" % BOT_NAME,
            "-c", "user.email=%s" % BOT_EMAIL,
            "commit", "-m", message]
    if allow_empty:
        args.insert(-2, "--allow-empty")
    code, _out, err = _git(repo_dir, *args)
    if code != 0:
        _warn(on_warn, "git commit failed (%s)" % (err.strip() or code))
        return False
    return True


def init_docs_repo(app_dir, on_warn=None):
    """Initialize/baseline ``docs/`` idempotently; return repository path."""
    docs_dir = os.path.join(app_dir, "docs")
    try:
        os.makedirs(docs_dir, exist_ok=True)
    except OSError as exc:
        _warn(on_warn, "cannot create docs directory (%s)" % exc)
        return None
    _ensure_parent_ignore(app_dir, on_warn)
    code, _out, _err = _git(docs_dir, "rev-parse", "--git-dir")
    if code == 0:
        return docs_dir
    code, _out, err = _git(docs_dir, "init", "--quiet")
    if code != 0:
        _warn(on_warn, "local Git unavailable; rendering without history (%s)"
              % (err.strip() or code))
        return None
    # Baseline pre-existing deterministic docs on upgrade; from this commit
    # onward only an actual diff is considered a human override.
    if not _commit(docs_dir, "docs: establish local baseline", allow_empty=True,
                   on_warn=on_warn):
        return None
    return docs_dir


def _hash_file(path):
    try:
        with open(path, "rb") as fh:
            return hashlib.sha256(fh.read()).hexdigest()
    except OSError:
        return hashlib.sha256(b"<deleted>").hexdigest()


def _file_slot_map(doc_map):
    all_slots = [str(slot.get("slot_id")) for slot in (doc_map.get("slots") or [])
                 if isinstance(slot, dict) and slot.get("slot_id")]
    mapping = {name: list(all_slots) for name in _ALL_SLOT_FILES}
    for entry in (doc_map.get("docs") or []):
        if not isinstance(entry, dict) or not entry.get("filename"):
            continue
        slots = entry.get("slots")
        if not isinstance(slots, list) or not slots:
            categories = _DOC_SLOT_CATEGORIES.get(entry.get("doc_id"), set())
            slots = [str(slot.get("slot_id"))
                     for slot in (doc_map.get("slots") or [])
                     if isinstance(slot, dict) and slot.get("slot_id")
                     and slot.get("category") in categories]
        if not slots:
            slots = entry.get("sources") or [entry.get("doc_id")]
        mapping[str(entry["filename"])] = [str(s) for s in slots if s]
    return mapping


def _owner_map(doc_map):
    return {str(slot.get("slot_id")): str(slot.get("owner_section") or
                                          "documentation")
            for slot in (doc_map.get("slots") or []) if isinstance(slot, dict)
            and slot.get("slot_id")}


def detect_human_edits(app_dir, doc_map, on_warn=None):
    """Tracked rendered files changed vs HEAD, with slots and diff summaries."""
    repo = os.path.join(app_dir, "docs")
    code, out, err = _git(repo, "diff", "--name-only", "--diff-filter=ACDMRTUXB",
                          "HEAD", "--")
    if code != 0:
        _warn(on_warn, "pre-render diff failed (%s)" % (err.strip() or code))
        return None
    mapping = _file_slot_map(doc_map)
    edits = []
    for name in sorted(set(line.strip() for line in out.splitlines()
                           if line.strip())):
        if name not in mapping:
            continue
        _code, summary, _err = _git(repo, "diff", "--numstat", "HEAD", "--", name)
        edits.append({
            "path": "docs/" + name,
            "repo_path": name,
            "slots": mapping[name],
            "content_hash": _hash_file(os.path.join(repo, name)),
            "diff_summary": (summary.strip() or "changed")[:400],
        })
    return edits


def clear_override(app_dir, path, on_warn=None):
    """Explicitly hand one project-relative doc path back to the renderer."""
    rel = str(path or "")
    if rel and not rel.startswith("docs/"):
        rel = "docs/" + rel
    state = load_state(app_dir, on_warn)
    record = state["files"].get(rel)
    if not isinstance(record, dict):
        return False
    record["status"] = "cleared"
    for slot in record.get("slots") or []:
        state["slots"].pop(slot, None)
    save_state(app_dir, state)
    return True


def prepare_render(app_dir, doc_map, on_warn=None):
    repo = init_docs_repo(app_dir, on_warn)
    if repo is None:
        return {"enabled": False, "overrides": set(), "state": None,
                "edits": [], "cleared": set()}
    state, state_status = _load_state_ex(app_dir, on_warn)
    owners = _owner_map(doc_map)
    edits = []
    cleared = {path for path, rec in state["files"].items()
               if isinstance(rec, dict) and rec.get("status") == "cleared"}
    detected = detect_human_edits(app_dir, doc_map, on_warn)
    frozen = set()
    if detected is None:
        # Once a docs repository exists, a failed diff is an inability to
        # prove renderer ownership. Freeze every known rendered file for this
        # pass; overwriting on uncertainty would discard the exact datum 5.5
        # exists to protect. A fresh machine with no Git never reaches here —
        # init_docs_repo returns disabled and retains plain rendering.
        frozen = {"docs/" + name for name in _file_slot_map(doc_map)
                  if os.path.exists(os.path.join(app_dir, "docs", name))}
        detected = []
    if state_status != "ok":
        # Adoption/corrupt-state safety: HEAD may already contain docs while no
        # trustworthy ownership ledger exists. Their provenance is unknowable,
        # so treat them as human-owned instead of silently replacing them on
        # the first 5.5 render (or after state corruption/deletion).
        mapping = _file_slot_map(doc_map)
        code, tracked, err = _git(repo, "ls-files")
        if code != 0:
            _warn(on_warn, "could not enumerate adoption baseline (%s)"
                  % (err.strip() or code))
            frozen |= {"docs/" + name for name in mapping
                       if os.path.exists(os.path.join(repo, name))}
        else:
            already = {edit["repo_path"] for edit in detected}
            for name in sorted(set(tracked.splitlines()) & set(mapping)):
                if name in already:
                    continue
                detected.append({
                    "path": "docs/" + name, "repo_path": name,
                    "slots": mapping[name],
                    "content_hash": _hash_file(os.path.join(repo, name)),
                    "diff_summary": ("pre-existing rendered file at docs-sync "
                                     "adoption" if state_status == "missing"
                                     else "ownership state corrupt; preserving HEAD"),
                })
    for edit in detected:
        path = edit["path"]
        if path in cleared:
            continue
        previous = state["files"].get(path, {})
        changed = previous.get("content_hash") != edit["content_hash"]
        edit["owner_sections"] = sorted(set(
            owners.get(slot, "documentation") for slot in edit["slots"]))
        edit["dedupe_key"] = hashlib.sha256(
            (path + "\0" + edit["content_hash"]).encode("utf-8")).hexdigest()
        if changed or not previous.get("artifact_id"):
            edits.append(edit)
        state["files"][path] = {
            "status": "human-overridden", "slots": list(edit["slots"]),
            "content_hash": edit["content_hash"],
            "diff_summary": edit["diff_summary"],
            "dedupe_key": edit["dedupe_key"],
            "artifact_id": previous.get("artifact_id")
            if previous.get("content_hash") == edit["content_hash"] else None,
        }
        for slot in edit["slots"]:
            state["slots"][slot] = {"status": "human-overridden", "path": path}
    # Crash/failure replay: once the human bytes are milestone-committed, Git
    # is clean. A state record whose artifact_id is still absent must therefore
    # reconstruct the publish request from durable fields rather than waiting
    # for a diff that will never reappear.
    queued_paths = {edit["path"] for edit in edits}
    for path, record in state["files"].items():
        if not isinstance(record, dict) or \
                record.get("status") != "human-overridden" or \
                record.get("artifact_id") or path in queued_paths:
            continue
        if not record.get("content_hash") or not record.get("dedupe_key"):
            _warn(on_warn, "override record for %s lacks replay hashes; "
                  "preserving the file but not fabricating a reconcile request"
                  % path)
            continue
        slots = [str(slot) for slot in (record.get("slots") or [])]
        edits.append({
            "path": path,
            "repo_path": path[5:] if path.startswith("docs/") else path,
            "slots": slots,
            "owner_sections": sorted(set(
                owners.get(slot, "documentation") for slot in slots)),
            "content_hash": str(record.get("content_hash") or ""),
            "diff_summary": str(record.get("diff_summary") or "changed")[:400],
            "dedupe_key": str(record.get("dedupe_key") or ""),
        })
    try:
        save_state(app_dir, state)
    except OSError as exc:
        _warn(on_warn, "could not persist override state (%s)" % exc)
    overrides = {path for path, rec in state["files"].items()
                 if isinstance(rec, dict)
                 and rec.get("status") == "human-overridden"} | frozen
    return {"enabled": True, "overrides": overrides, "state": state,
            "edits": edits, "cleared": cleared}


def _existing_request(project_dir, dedupe_key, on_warn=None):
    for meta in artifactslib.list_artifacts(
            project_dir, type="reconcile",
            on_error=lambda message: _warn(on_warn, message)):
        fields = meta.get("fields") if isinstance(meta.get("fields"), dict) else {}
        if fields.get("request_kind") == "human_override" and \
                fields.get("dedupe_key") == dedupe_key:
            return meta.get("id")
    return None


def _publish_request(app_dir, edit, orch_dir, on_warn=None):
    existing = _existing_request(app_dir, edit["dedupe_key"], on_warn)
    if existing:
        return existing
    registry = artifactslib.load_registry(
        orch_dir, on_error=lambda message: _warn(on_warn, message))
    body = ("A human edited `%s`; deterministic rendering now preserves that "
            "file until the override is explicitly cleared.\n\n"
            "Affected slots: %s\nDiff summary: %s\nContent hash: `%s`\n"
            % (edit["path"], ", ".join(edit["slots"]) or "(none)",
               edit["diff_summary"], edit["content_hash"]))
    meta = {
        "type": "reconcile",
        "title": "Human documentation override: %s" % edit["path"],
        "source": {"section": "documentation", "session": "docsync",
                   "phase": "render", "turn": ""},
        "parents": [],
        "request_kind": "human_override",
        "doc_path": edit["path"],
        "slots": list(edit["slots"]),
        "owner_sections": list(edit["owner_sections"]),
        "diff_summary": edit["diff_summary"],
        "dedupe_key": edit["dedupe_key"],
    }
    return artifactslib.publish(
        app_dir, body, meta, registry, consensus=True,
        reconcile_request=True,
        on_error=lambda message: _warn(on_warn, message))


def finish_render(app_dir, context, written, orch_dir, app="project",
                  workflow="workflow", phase="complete", on_warn=None):
    """Publish reconciliation requests, clear reclaimed files, and commit."""
    if not context or not context.get("enabled"):
        return False
    state = context.get("state") or load_state(app_dir, on_warn)
    for edit in context.get("edits") or []:
        aid = _publish_request(app_dir, edit, orch_dir, on_warn)
        if aid:
            state["files"].setdefault(edit["path"], {})["artifact_id"] = aid
    written_set = set(written or [])
    for path in context.get("cleared") or set():
        if path in written_set:
            state["files"].pop(path, None)
    try:
        save_state(app_dir, state)
    except OSError as exc:
        _warn(on_warn, "could not persist post-render state (%s)" % exc)
    stamp = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
    message = "docs: %s %s milestone %s (%s)" % (app, workflow, phase, stamp)
    return _commit(os.path.join(app_dir, "docs"), message, on_warn=on_warn)


def override_note(overrides):
    paths = sorted(overrides or [])
    if not paths:
        return ""
    return ("\n\n## Human-overridden documentation\n\n"
            "The renderer intentionally preserved these files verbatim; their "
            "contents may differ from current artifacts until reconciliation:\n"
            + "\n".join("- `%s`" % path for path in paths) + "\n")
