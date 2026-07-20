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
NOTION_STATE_FILENAME = "notion_export_state.json"
NOTION_DIFF_FILENAME = "NOTION_EXPORT_DIFF.md"
NOTION_STATE_SCHEMA_VERSION = 1
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


def _atomic_text(path, value):
    tmp = "%s.%d.tmp" % (path, os.getpid())
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(value)
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


def _content_hash(value):
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"),
                         ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _notion_paths(app_dir):
    integrations = os.path.join(app_dir, "integrations")
    return {
        "dir": integrations,
        "payload": os.path.join(integrations,
                                "project_management_backfill.json"),
        "state": os.path.join(integrations, NOTION_STATE_FILENAME),
        "diff": os.path.join(integrations, NOTION_DIFF_FILENAME),
    }


def _load_notion_payload(path, on_warn=None):
    try:
        with open(path, encoding="utf-8") as fh:
            payload = json.load(fh)
    except FileNotFoundError:
        _warn(on_warn, "nothing to export — project management backfill is missing")
        return None
    except (OSError, ValueError) as exc:
        _warn(on_warn, "nothing to export — project management backfill is "
              "unreadable (%s)" % exc)
        return None
    if not isinstance(payload, dict) or not isinstance(payload.get("notion"), dict):
        _warn(on_warn, "nothing to export — backfill notion payload must be an object")
        return None
    return payload


def _entity_hashes(payload, on_warn=None):
    """Return the stable Notion snapshot, or None for structurally unsafe data."""
    notion = payload["notion"]
    properties = notion.get("project_properties")
    pages = notion.get("pages")
    rows = notion.get("task_database_rows")
    if not isinstance(properties, dict) or not isinstance(pages, list) or \
            not isinstance(rows, list):
        _warn(on_warn, "notion payload requires project_properties object, "
              "pages array, and task_database_rows array")
        return None

    page_hashes = {}
    for index, page in enumerate(pages):
        if not isinstance(page, dict) or not isinstance(page.get("title"), str) \
                or not isinstance(page.get("path", ""), str):
            _warn(on_warn, "notion page %d lacks a string title/path; refusing "
                  "an ambiguous diff" % index)
            return None
        key = json.dumps([page["title"], page.get("path", "")],
                         ensure_ascii=False, separators=(",", ":"))
        if key in page_hashes:
            _warn(on_warn, "duplicate notion page identity %s; refusing an "
                  "ambiguous diff" % key)
            return None
        page_hashes[key] = _content_hash(page)

    row_hashes = {}
    for index, row in enumerate(rows):
        if not isinstance(row, dict) or not isinstance(row.get("external_id"), str) \
                or not row.get("external_id"):
            _warn(on_warn, "notion task row %d lacks external_id; refusing an "
                  "ambiguous diff" % index)
            return None
        key = row["external_id"]
        if key in row_hashes:
            _warn(on_warn, "duplicate notion task external_id %s; refusing an "
                  "ambiguous diff" % key)
            return None
        row_hashes[key] = _content_hash(row)

    return {
        "schema_version": NOTION_STATE_SCHEMA_VERSION,
        "payload_hash": _content_hash(notion),
        "entities": {
            "pages": page_hashes,
            "task_database_rows": row_hashes,
            "project_properties": {
                "project_properties": _content_hash(properties),
            },
        },
    }


def _empty_notion_state():
    return {
        "schema_version": NOTION_STATE_SCHEMA_VERSION,
        "payload_hash": "",
        "entities": {
            "pages": {}, "task_database_rows": {},
            "project_properties": {},
        },
    }


def _load_notion_state(path, on_warn=None):
    try:
        with open(path, encoding="utf-8") as fh:
            state = json.load(fh)
        entities = state.get("entities") if isinstance(state, dict) else None
        if not isinstance(entities, dict) or any(
                not isinstance(entities.get(name), dict) for name in
                ("pages", "task_database_rows", "project_properties")):
            raise ValueError("state must contain all entity hash maps")
        return state, "ok"
    except FileNotFoundError:
        return _empty_notion_state(), "missing"
    except (OSError, ValueError) as exc:
        _warn(on_warn, "notion export state unreadable (%s) — the diff will "
              "explicitly show the current payload as added" % exc)
        return _empty_notion_state(), "corrupt"


def _notion_diff(previous, current):
    result = {}
    for name in ("pages", "task_database_rows", "project_properties"):
        before = previous["entities"].get(name, {})
        after = current["entities"].get(name, {})
        before_keys = set(before)
        after_keys = set(after)
        result[name] = {
            "added": sorted(after_keys - before_keys),
            "changed": sorted(key for key in before_keys & after_keys
                              if before[key] != after[key]),
            "removed": sorted(before_keys - after_keys),
        }
    return result


def _diff_has_changes(diff):
    return any(values[kind] for values in diff.values()
               for kind in ("added", "changed", "removed"))


def _render_notion_diff(diff, state_status):
    lines = [
        "# Notion Export Diff", "",
        "_Dry run only. This report does not contact Notion._", "",
    ]
    if state_status == "corrupt":
        lines += [
            "> Previous export state was unreadable. Every current entity is "
            "therefore shown explicitly as added; review before authorizing "
            "snapshot recording.", "",
        ]
    if not _diff_has_changes(diff):
        lines += ["No changes since the last recorded export snapshot.", ""]
        return "\n".join(lines)
    labels = {
        "pages": "Pages",
        "task_database_rows": "Task database rows",
        "project_properties": "Project properties",
    }
    for name in ("pages", "task_database_rows", "project_properties"):
        lines += ["## %s" % labels[name], ""]
        values = diff[name]
        if not any(values.values()):
            lines += ["No changes.", ""]
            continue
        for kind in ("added", "changed", "removed"):
            for key in values[kind]:
                lines.append("- %s: `%s`" % (kind, key.replace("`", "\\`")))
        lines.append("")
    return "\n".join(lines)


def record_notion_snapshot(app_dir, snapshot, on_warn=None):
    """Shipped delivery seam: record hashes only; perform no remote delivery.

    A future HTTP implementation belongs behind this same injected-callable
    seam and must follow the API key-file/header-only rules.  This implementation
    deliberately has no network or token handling.
    """
    paths = _notion_paths(app_dir)
    os.makedirs(paths["dir"], exist_ok=True)
    _atomic_json(paths["state"], snapshot)
    message = "snapshot recorded; no remote Notion delivery is wired"
    _warn(on_warn, message)
    return {"recorded": True, "remote_delivered": False, "message": message}


def export_notion(app_dir, deliver=False, delivery_fn=None, on_warn=None):
    """Create a fresh dry-run diff, optionally recording its snapshot.

    ``delivery_fn`` receives ``(app_dir, snapshot, on_warn=...)``.  It is never
    called unless this invocation successfully wrote the diff report.  The only
    shipped implementation is :func:`record_notion_snapshot`, which records
    local state and truthfully performs no remote delivery.
    """
    warnings = []

    def warn(message):
        message = str(message)
        if message.startswith("docsync: "):
            message = message[len("docsync: "):]
        warnings.append(message)
        _warn(on_warn, message)

    paths = _notion_paths(app_dir)
    payload = _load_notion_payload(paths["payload"], warn)
    if payload is None:
        return {
            "ok": True, "status": "nothing-to-export", "fresh_diff": False,
            "delivered": False, "remote_delivered": False, "diff": None,
            "report_path": None, "warnings": warnings,
            "message": "nothing to export",
        }
    snapshot = _entity_hashes(payload, warn)
    if snapshot is None:
        return {
            "ok": False, "status": "refused", "fresh_diff": False,
            "delivered": False, "remote_delivered": False, "diff": None,
            "report_path": None, "warnings": warnings,
            "message": "unsafe notion payload; export refused",
        }
    previous, state_status = _load_notion_state(paths["state"], warn)
    diff = _notion_diff(previous, snapshot)
    report = _render_notion_diff(diff, state_status)
    try:
        os.makedirs(paths["dir"], exist_ok=True)
        _atomic_text(paths["diff"], report)
    except OSError as exc:
        warn("could not atomically write the fresh Notion diff (%s)" % exc)
        return {
            "ok": False, "status": "refused", "fresh_diff": False,
            "delivered": False, "remote_delivered": False, "diff": diff,
            "report_path": None, "warnings": warnings,
            "message": "delivery refused because no fresh diff was recorded",
        }

    result = {
        "ok": True, "status": "dry-run", "fresh_diff": True,
        "delivered": False, "remote_delivered": False, "diff": diff,
        "report_path": paths["diff"], "warnings": warnings,
        "message": ("dry-run diff generated with changes" if
                    _diff_has_changes(diff) else "dry-run diff: no changes"),
    }
    if not deliver:
        return result
    if not _diff_has_changes(diff):
        result.update({"status": "no-changes",
                       "message": "no changes; snapshot delivery was not run"})
        return result

    fn = delivery_fn or record_notion_snapshot
    try:
        delivery = fn(app_dir, snapshot, on_warn=warn)
    except Exception as exc:
        warn("snapshot delivery step failed (%s)" % exc)
        result.update({"ok": False, "status": "delivery-failed",
                       "message": "snapshot delivery step failed"})
        return result
    if not isinstance(delivery, dict) or not delivery.get("recorded"):
        warn("delivery callable did not confirm snapshot recording")
        result.update({"ok": False, "status": "delivery-failed",
                       "message": "snapshot delivery was not confirmed"})
        return result
    recorded, recorded_status = _load_notion_state(paths["state"], warn)
    if recorded_status != "ok" or recorded != snapshot:
        warn("delivery callable confirmation lacked a matching durable snapshot")
        result.update({"ok": False, "status": "delivery-failed",
                       "message": "snapshot delivery was not durably verified"})
        return result
    result.update({
        "status": "snapshot-recorded", "delivered": True,
        "remote_delivered": bool(delivery.get("remote_delivered", False)),
        "message": str(delivery.get("message") or
                       "snapshot recorded; no remote delivery wired"),
    })
    return result
