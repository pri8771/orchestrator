#!/usr/bin/env python3
"""
docs.py — deterministic documentation renderer (V2 spec §24 / §2.3).

Assembles human-readable project docs from the phases' final outputs WITHOUT
asking an agent to write them — a pure, non-AI render so the output is stable and
never fabricated. If a phase emitted a structured ```phase-output-json``` block
(doc_sections), those keyed sections are used; otherwise the phase's prose final
output is included under its title. A phase that never ran renders as N/A.

Standard library only.
"""

import hashlib
import json
import os
import re

import schemas


def _write(path, text):
    # Per-writer temp name so concurrent renders don't share one ".tmp", and
    # clean it up if the write/replace fails so a stale half-file isn't left
    # behind (the caller's `except OSError: pass` would otherwise hide it).
    tmp = "%s.%d.tmp" % (path, os.getpid())
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except OSError:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def _read_text(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read()
    except OSError:
        return ""


def _read_json(path, fallback):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return fallback


def _phase_meta(phase):
    if hasattr(phase, "key"):
        return {
            "key": phase.key,
            "title": getattr(phase, "title", phase.key.replace("_", " ").title()),
            "folder": getattr(phase, "folder", phase.key),
            "file": getattr(phase, "file", phase.key + ".md"),
        }
    key = phase[0]
    if len(phase) == 2:
        return {
            "key": key,
            "title": phase[1],
            "folder": key,
            "file": key + ".md",
        }
    return {
        "key": key,
        "title": (phase[3] if len(phase) > 3 else key.replace("_", " ").title()),
        "folder": (phase[1] if len(phase) > 1 else key),
        "file": (phase[2] if len(phase) > 2 else key + ".md"),
    }


def _jira_label(value, default):
    """Sanitize a value for use as a Jira label. Jira rejects whitespace (and
    is picky about other punctuation) in labels; `owner_lane` normally comes
    from the fixed BUILD_LANE_IDS set (no spaces), but a malformed/free-text
    value from an agent could still reach here, so this is a defensive
    normalization rather than trusting the source. Never returns ''."""
    s = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(value or "").strip()).strip("-")
    return s or default


def _issue_from_task(task, app, epic_key):
    tid = str(task.get("id") or "")
    title = str(task.get("title") or tid or "Untitled task")
    desc = [
        "Generated from orchestrator task `%s` for `%s`." % (tid or "unknown", app),
        "",
        "Owner lane: `%s`" % str(task.get("owner_lane") or ""),
        "Files: %s" % ", ".join(task.get("files") or []),
        "",
        "Acceptance criteria:",
    ]
    desc += ["- %s" % a for a in (task.get("acceptance_criteria") or [])]
    return {
        "external_id": tid,
        "issue_type": "Task",
        "summary": title,
        "description": "\n".join(desc).strip(),
        "status": str(task.get("status") or "pending"),
        "epic_external_id": epic_key,
        "labels": ["orchestrator", "generated-app", _jira_label(task.get("owner_lane"), "lane")],
        "fields": {
            "owner_lane": task.get("owner_lane"),
            "files": task.get("files") or [],
            "depends_on": task.get("depends_on") or [],
            "acceptance_criteria": task.get("acceptance_criteria") or [],
            "source_project": app,
            "source_task_id": tid,
        },
    }


def render_project_management_backfill(app, original_prompt, ordered_phases,
                                       phase_outputs, tasks, interfaces,
                                       verify_summary=""):
    """Machine-readable payload for later Jira/Notion creation. This is not an
    API push; it records the fields needed to backfill a board/project without
    re-parsing all the prose after the run."""
    phase_keys = [k for k, _ in ordered_phases]
    epics = [
        {"external_id": "EPIC-PRODUCT", "name": "Product Definition",
         "phase_keys": [k for k in phase_keys if k in (
             "prompt_contract", "product_research", "portfolio_selection",
             "initial_discussion", "per_app_product_brief", "next_steps_small",
             "detailed_discussion", "app_features")]},
        {"external_id": "EPIC-DESIGN-ARCH", "name": "Design & Architecture",
         "phase_keys": [k for k in phase_keys if k in (
             "design_discussion", "design_handoff", "ios_architecture_review",
             "tech_specs", "project_plan")]},
        {"external_id": "EPIC-BUILD", "name": "Implementation",
         "phase_keys": [k for k in phase_keys if k in (
             "task_assignments", "implementation_readiness_gate",
             "build_coordination", "build_verification")]},
        {"external_id": "EPIC-QA-LAUNCH", "name": "QA & Launch",
         "phase_keys": [k for k in phase_keys if k in (
             "human_qa_checklist", "app_store_readiness", "final_review",
             "portfolio_audit")]},
    ]
    epics = [e for e in epics if e["phase_keys"]]
    issues = [_issue_from_task(t, app, "EPIC-BUILD") for t in (tasks or [])]
    pages = [
        {"title": "Original Prompt", "type": "prompt", "content": original_prompt},
        {"title": "Project Documentation", "type": "doc",
         "path": "docs/PROJECT_DOCUMENTATION.md"},
        {"title": "Complete Project Dossier", "type": "doc",
         "path": "docs/COMPLETE_PROJECT_DOSSIER.md"},
        {"title": "Full Transcript", "type": "transcript",
         "path": "docs/FULL_TRANSCRIPT.txt"},
        {"title": "PRD", "type": "doc", "path": "docs/PRD.md"},
        {"title": "Technical Architecture", "type": "doc",
         "path": "docs/TECHNICAL_ARCHITECTURE.md"},
        {"title": "QA Report", "type": "doc", "path": "docs/QA_REPORT.md"},
        {"title": "Known Limitations", "type": "doc",
         "path": "docs/KNOWN_LIMITATIONS.md"},
    ]
    return {
        "schema_version": schemas.SCHEMA_VERSION,
        "project": {
            "name": app,
            "source": "orchestrator",
            "original_prompt": original_prompt,
            "verification": verify_summary,
        },
        "jira": {
            "board_type": "kanban",
            "suggested_project_key": "".join(ch for ch in app.upper()
                                             if ch.isalnum())[:10] or "APP",
            "statuses": ["Backlog", "Selected", "In Progress", "In Review", "Done"],
            "issue_types": ["Epic", "Task", "Bug", "Story"],
            "epics": epics,
            "issues": issues,
        },
        "notion": {
            "project_properties": {
                "Name": app,
                "Status": "Done" if (verify_summary or "").upper().startswith("VERIFIED")
                else "Needs review",
                "Source": "Orchestrator",
                "Workflow Phases": len(ordered_phases),
            },
            "pages": pages,
            "task_database_rows": issues,
            "interfaces": interfaces or [],
        },
        "phase_outputs": phase_outputs or {},
    }


def render_complete_project_dossier(app, original_prompt, ordered_phases,
                                    phase_entries, tasks, interfaces,
                                    consensus_status=None, verify_summary="",
                                    findings=None):
    """Very detailed, deterministic Markdown archive for handoff/backfill.

    This is intentionally verbose: the concise docs remain available, while this
    file preserves enough context for a human, Notion workspace, or Jira backfill
    to understand how every decision emerged.
    """
    lines = [
        "# %s — Complete Project Dossier" % app,
        "",
        "_Detailed deterministic archive of the orchestrator run. It includes the "
        "original prompt, final phase outputs, full discussion transcripts, task "
        "backlog, interface contracts, verification status, and recorded findings. "
        "Nothing here is inferred or fabricated._",
        "",
        "## Original Prompt",
        "",
        original_prompt.strip() or "_N/A_",
        "",
        "## Phase Map",
        "",
    ]
    outputs_by_key = {e["key"]: e.get("output", "") for e in (phase_entries or [])}
    for key, title in ordered_phases or []:
        ran = bool((outputs_by_key.get(key) or "").strip())
        con = (consensus_status or {}).get(key)
        status = "consensus" if con else ("completed" if ran else "not run")
        lines.append("- **%s** (`%s`) — %s" % (title, key, status))
    lines += ["", "## Final Phase Outputs", ""]
    titles = dict(ordered_phases or [])
    for entry in phase_entries or []:
        title = entry.get("title") or titles.get(entry.get("key"), entry.get("key", "Phase"))
        lines.append("### %s" % title)
        lines.append("")
        lines.append(_phase_section(title, entry.get("output", "")))
        lines.append("")
    lines += ["## Full Discussion Transcripts", ""]
    for entry in phase_entries or []:
        title = entry.get("title") or entry.get("key", "Phase")
        lines.append("### %s" % title)
        lines.append("")
        discussion = (entry.get("discussion_markdown") or "").strip()
        lines.append(discussion if discussion else "_N/A — no transcript file was recorded._")
        lines.append("")
    lines += ["## Task Backlog", ""]
    if tasks:
        lines.append("```json")
        lines.append(json.dumps({"tasks": tasks}, indent=2))
        lines.append("```")
    else:
        lines.append("_N/A — no tasks.json entries were recorded._")
    lines += ["", "## Interface Contracts", ""]
    if interfaces:
        lines.append("```json")
        lines.append(json.dumps({"interfaces": interfaces}, indent=2))
        lines.append("```")
    else:
        lines.append("_N/A — no interfaces.json entries were recorded._")
    lines += ["", "## Verification", "", verify_summary or "_N/A — no verification summary recorded._", ""]
    lines += ["## Findings", ""]
    if findings:
        lines.append("```json")
        lines.append(json.dumps(findings, indent=2))
        lines.append("```")
    else:
        lines.append("_No findings recorded._")
    return "\n".join(lines)


def render_full_transcript_txt(app, original_prompt, phase_entries):
    """Plain-text archive of the whole conversation, in phase order."""
    lines = [
        "%s — FULL ORCHESTRATOR TRANSCRIPT" % app,
        "=" * (len(app) + 32),
        "",
        "ORIGINAL PROMPT",
        "-" * 15,
        original_prompt.strip() or "N/A",
        "",
    ]
    for entry in phase_entries or []:
        title = entry.get("title") or entry.get("key", "Phase")
        key = entry.get("key", "")
        header = "%s (%s)" % (title, key) if key else title
        lines += [
            "",
            header.upper(),
            "-" * len(header),
            (entry.get("discussion_markdown") or "").strip()
            or "N/A — no transcript file was recorded.",
            "",
        ]
    return "\n".join(lines).rstrip() + "\n"


def write_project_archive(app_dir, app, phases, original_prompt, state,
                          workflow_name="app_build", verify_summary="",
                          findings=None, human_overrides=None):
    """Persist the whole completed run in stable JSON files:
    - docs/phase_discussions.json: every phase markdown transcript
    - docs/PROJECT_RECORD.json: prompt, outputs, discussions, tasks, interfaces
    - integrations/project_management_backfill.json: Jira/Notion payload
    Best-effort; returns list of files written."""
    docs_dir = os.path.join(app_dir, "docs")
    integrations_dir = os.path.join(app_dir, "integrations")
    os.makedirs(docs_dir, exist_ok=True)
    os.makedirs(integrations_dir, exist_ok=True)
    phase_outputs = (state or {}).get("phase_outputs", {})
    phase_entries = []
    ordered = []
    for phase in phases or []:
        meta = _phase_meta(phase)
        key = meta["key"]
        rel = os.path.join(meta["folder"], meta["file"])
        ordered.append((key, meta["title"]))
        phase_entries.append({
            "key": key,
            "title": meta["title"],
            "path": rel,
            "output": phase_outputs.get(key, ""),
            "discussion_markdown": _read_text(os.path.join(app_dir, rel)),
        })
    tasks = _read_json(os.path.join(app_dir, "tasks.json"), {}).get("tasks", [])
    interfaces = _read_json(os.path.join(app_dir, "interfaces.json"), {}).get("interfaces", [])
    record = {
        "schema_version": schemas.SCHEMA_VERSION,
        "app": app,
        "workflow": workflow_name,
        "original_prompt": original_prompt,
        "phase_order": [k for k, _ in ordered],
        "phase_discussions": phase_entries,
        "phase_outputs": phase_outputs,
        "consensus_status": (state or {}).get("consensus_status", {}),
        "vote_results": (state or {}).get("vote_results", {}),
        "tasks": tasks,
        "interfaces": interfaces,
        "verify_summary": verify_summary,
        "findings": findings or [],
    }
    backfill = render_project_management_backfill(
        app, original_prompt, ordered, phase_outputs, tasks, interfaces,
        verify_summary=verify_summary)
    dossier = render_complete_project_dossier(
        app, original_prompt, ordered, phase_entries, tasks, interfaces,
        consensus_status=(state or {}).get("consensus_status", {}),
        verify_summary=verify_summary, findings=findings or [])
    full_transcript = render_full_transcript_txt(app, original_prompt, phase_entries)
    written = []

    overrides = set(human_overrides or [])

    def put(rel, text):
        if rel in overrides:
            return
        _write(os.path.join(app_dir, rel), text)
        written.append(rel)

    try:
        put("docs/phase_discussions.json", json.dumps(phase_entries, indent=2))
        put("docs/COMPLETE_PROJECT_DOSSIER.md", dossier)
        put("docs/FULL_TRANSCRIPT.txt", full_transcript)
        put("docs/PROJECT_RECORD.json", json.dumps(record, indent=2))
        put("integrations/project_management_backfill.json",
            json.dumps(backfill, indent=2))
    except OSError:
        pass
    return written


def _phase_section(title, output):
    """Render one phase's contribution: structured doc_sections if the phase
    emitted a phase-output-json block, else its prose final output."""
    blocks = schemas.extract_structured_blocks(output or "", "phase-output-json")
    parts = []
    for b in blocks:
        ds = b.get("doc_sections") or {}
        if isinstance(ds, dict):
            for key, val in ds.items():
                nice = key.replace(".", " · ").replace("_", " ").title()
                parts.append("**%s**\n\n%s\n" % (nice, str(val).strip()))
    if parts:
        return "\n".join(parts)
    # Fall back to prose: strip any trailing structured markers for readability.
    body = (output or "").strip()
    return body if body else "_Not applicable — this phase did not run._"


def render_project_documentation(app, ordered_phases, phase_outputs, workflow_name="app_build"):
    """ordered_phases: list of (key, title). phase_outputs: {key: final_output}.
    Returns the PROJECT_DOCUMENTATION.md text."""
    lines = ["# %s — Project Documentation" % app, ""]
    lines.append("_Generated deterministically from the orchestrator's phase "
                 "outputs (workflow: `%s`). Non-AI render — nothing here is "
                 "fabricated; empty phases are marked N/A._" % workflow_name)
    lines.append("")
    lines.append("## Contents\n")
    for key, title in ordered_phases:
        ran = bool((phase_outputs or {}).get(key, "").strip())
        lines.append("- %s%s" % (title, "" if ran else " — _N/A_"))
    lines.append("")
    for key, title in ordered_phases:
        lines.append("## %s\n" % title)
        lines.append(_phase_section(title, (phase_outputs or {}).get(key, "")))
        lines.append("")
    return "\n".join(lines)


def _open_secret_findings(findings):
    """The unresolved secret_hardcoded findings out of a findings list (§23)."""
    return [f for f in (findings or [])
            if isinstance(f, dict) and f.get("category") == "secret_hardcoded"
            and str(f.get("status") or "open") == "open"]


def render_launch_readiness(app, ordered_phases, phase_outputs, consensus_status,
                            verify_summary="", findings=None):
    """A short readiness summary: which phases converged, the honest verification
    status, and the secret-scan gate (§23). ``findings`` is the persisted
    docs/findings.json list — None means no scan/review ever ran."""
    total = len(ordered_phases)
    done = sum(1 for k, _ in ordered_phases if (phase_outputs or {}).get(k, "").strip())
    agreed = sum(1 for k, _ in ordered_phases if (consensus_status or {}).get(k))
    secret_open = _open_secret_findings(findings)
    lines = ["# %s — Launch Readiness" % app, ""]
    # A failed secret gate must be plainly visible — right under the title (§23).
    if secret_open:
        lines.append("⛔ **LAUNCH BLOCKED — %d unresolved secret_hardcoded "
                     "finding(s). See docs/findings.json.**\n" % len(secret_open))
    lines.append("_Honest, non-AI summary. 'Verified' means a real tool ran and "
                 "passed; anything else is stated plainly._\n")
    lines.append("- Phases completed: **%d / %d**" % (done, total))
    lines.append("- Phases reaching consensus: **%d / %d**" % (agreed, total))
    if verify_summary:
        lines.append("- Build verification: **%s**" % verify_summary)
    if secret_open:
        lines.append("- Secret scan: FAIL — %d unresolved secret_hardcoded "
                     "finding(s)" % len(secret_open))
    elif findings is None:
        lines.append("- Secret scan: not run (no generated source was scanned)")
    else:
        lines.append("- Secret scan: PASS (0 hardcoded secrets)")
    lines.append("")
    lines.append("## Per-phase status\n")
    for key, title in ordered_phases:
        ran = bool((phase_outputs or {}).get(key, "").strip())
        con = (consensus_status or {}).get(key)
        mark = "✅ consensus" if con else ("• completed" if ran else "— not run")
        lines.append("- %s — %s" % (title, mark))
    return "\n".join(lines)


# Which workflow phases feed each composed spec doc (§24). A doc whose source
# phases are ALL absent from the workflow (e.g. PRD for an audit run) is
# skipped rather than fabricated; a source phase that exists but never ran
# renders as N/A.
PRD_SOURCES = ("prompt_contract", "product_research", "portfolio_selection",
               "initial_discussion", "per_app_product_brief", "next_steps_small",
               "detailed_discussion", "app_features")
ARCH_SOURCES = ("design_discussion", "design_handoff", "ios_architecture_review",
                "tech_specs", "project_plan", "implementation_readiness_gate")
QA_SOURCES = ("build_verification", "human_qa_checklist", "app_store_readiness",
              "final_review", "portfolio_audit")

_NA = "_N/A — this phase did not run._"

# The three composed docs' intros, byte-frozen. Extracted to named constants so
# the doc_map (below) and any fallback render share ONE copy — a change here is
# a change everywhere, and tests/fixtures/doc_render_frozen.json pins the bytes.
_PRD_INTRO = ("_Deterministic render of the discovery/scoping phases' final "
              "decisions. Non-AI; missing phases are N/A, never invented._")
_ARCH_INTRO = ("_Deterministic render of the design/spec/planning phases' final "
               "decisions. Non-AI; missing phases are N/A, never invented._")
_QA_INTRO = ("_Deterministic render of the review phase's decision and the real "
             "verification result. Non-AI; nothing here is fabricated._")

# V3 board 5.1: the doc blueprint is data (sections/documentation/doc_map.json),
# not hardcoded tuples. The App Factory 11-category / 40-section handoff standard
# ships as the built-in default; PRD_SOURCES/ARCH_SOURCES/QA_SOURCES above stay
# the SINGLE source of truth that feeds both the seed and the corrupt-file
# fallback (never a second copy). The 40 blueprint slots are inert scaffold at
# this stage (owner_section / min_chars / sources left empty) — boards 5.2-5.4
# extend THIS file, they never fork a new one.
DOC_MAP_FILENAME = "doc_map.json"
_ORCH_DIR = os.path.dirname(os.path.abspath(__file__))

# The 11-category standard = the section taxonomy (orchestrator-v3-sections-plan
# §2). Categories are the top-level buckets of the 40-section handoff blueprint.
_BLUEPRINT_CATEGORIES = [
    {"category_id": "ideas", "title": "Ideas"},
    {"category_id": "research", "title": "Research"},
    {"category_id": "planning_spec", "title": "Planning & Spec"},
    {"category_id": "design", "title": "Design"},
    {"category_id": "build", "title": "Build (Prototype & Engineering)"},
    {"category_id": "qa_redteam", "title": "QA & Red Team"},
    {"category_id": "documentation", "title": "Documentation"},
    {"category_id": "gtm", "title": "Go-to-Market"},
    {"category_id": "legal_compliance", "title": "Legal & Compliance"},
    {"category_id": "execution_ops", "title": "Execution & Operations"},
    {"category_id": "library_knowledge", "title": "Library & Knowledge"},
]

# The 40-section handoff blueprint, grouped 1:1 under the 11 categories above.
# owner_section is populated (5.3) as the slot's own category — the 11 blueprint
# categories ARE the section taxonomy, mapped 1:1. min_chars (5.4) stays inert;
# sources[] stays empty until a doc_map edit wires slot→phase fallbacks. This is
# a template, never project content — nothing here is a fabricated fact.
_BLUEPRINT_SLOT_SEED = [
    ("ideas", "problem_statement", "Problem Statement"),
    ("ideas", "target_user", "Target User"),
    ("ideas", "value_proposition", "Value Proposition"),
    ("research", "market_landscape", "Market Landscape"),
    ("research", "competitor_analysis", "Competitor Analysis"),
    ("research", "user_research", "User Research"),
    ("research", "technical_feasibility", "Technical Feasibility"),
    ("planning_spec", "product_requirements", "Product Requirements"),
    ("planning_spec", "scope_tiers", "Scope Tiers"),
    ("planning_spec", "feature_list", "Feature List"),
    ("planning_spec", "task_graph", "Task Graph"),
    ("planning_spec", "success_metrics", "Success Metrics"),
    ("design", "user_flows", "User Flows"),
    ("design", "information_architecture", "Information Architecture"),
    ("design", "design_language", "Design Language"),
    ("design", "copy_voice", "Copy & Voice"),
    ("build", "technical_architecture", "Technical Architecture"),
    ("build", "data_model", "Data Model"),
    ("build", "api_contracts", "API Contracts"),
    ("build", "build_plan", "Build Plan"),
    ("build", "dependency_map", "Dependency Map"),
    ("qa_redteam", "test_plan", "Test Plan"),
    ("qa_redteam", "qa_report", "QA Report"),
    ("qa_redteam", "known_limitations", "Known Limitations"),
    ("qa_redteam", "security_review", "Security Review"),
    ("documentation", "overview", "Overview"),
    ("documentation", "changelog", "Changelog"),
    ("documentation", "glossary", "Glossary"),
    ("gtm", "positioning", "Positioning"),
    ("gtm", "pricing", "Pricing"),
    ("gtm", "launch_plan", "Launch Plan"),
    ("gtm", "marketing_channels", "Marketing Channels"),
    ("legal_compliance", "privacy_policy", "Privacy Policy"),
    ("legal_compliance", "terms_of_service", "Terms of Service"),
    ("legal_compliance", "app_store_compliance", "App Store Compliance"),
    ("execution_ops", "release_checklist", "Release Checklist"),
    ("execution_ops", "runbook", "Runbook"),
    ("execution_ops", "postmortem_template", "Postmortem Template"),
    ("library_knowledge", "reusable_components", "Reusable Components"),
    ("library_knowledge", "lessons_learned", "Lessons Learned"),
]


def _default_doc_map():
    """The built-in doc blueprint: the three composed docs (PRD/ARCH/QA) in
    render order, assembled FROM the source-of-truth tuples so this can never
    drift from them, plus the 11-category / 40-section handoff scaffold. This
    feeds both the on-disk seed and the corrupt-file fallback."""
    return {
        "schema_version": schemas.SCHEMA_VERSION,
        "docs": [
            {"doc_id": "prd", "title": "Product Requirements (PRD)",
             "filename": "PRD.md", "kind": "composed",
             "intro": _PRD_INTRO, "sources": list(PRD_SOURCES)},
            {"doc_id": "technical_architecture", "title": "Technical Architecture",
             "filename": "TECHNICAL_ARCHITECTURE.md", "kind": "composed",
             "intro": _ARCH_INTRO, "sources": list(ARCH_SOURCES)},
            {"doc_id": "qa_report", "title": "QA Report",
             "filename": "QA_REPORT.md", "kind": "qa",
             "intro": _QA_INTRO, "sources": list(QA_SOURCES)},
        ],
        "categories": [dict(c) for c in _BLUEPRINT_CATEGORIES],
        "slots": [{"slot_id": sid, "category": cat, "title": title,
                   "sources": [], "owner_section": cat,
                   "min_chars": DEFAULT_MIN_CHARS}
                  for (cat, sid, title) in _BLUEPRINT_SLOT_SEED],
    }


def _doc_map_path(orch_dir):
    return os.path.join(orch_dir, "sections", "documentation", DOC_MAP_FILENAME)


def _valid_doc_map(data):
    """A loaded map is usable only if it is a dict with a docs[] list whose every
    entry carries a filename and a sources LIST (the two fields the renderer
    dereferences). Anything else is treated as corrupt → built-in fallback."""
    if not isinstance(data, dict) or not isinstance(data.get("docs"), list):
        return False
    for e in data["docs"]:
        if not isinstance(e, dict) or not e.get("filename") \
                or not isinstance(e.get("sources"), list):
            return False
    return True


def load_doc_map(orch_dir, on_warn=None):
    """The doc blueprint for a render (V3 board 5.1). Seed-then-disk-wins, exactly
    like workflows.ensure_seeded / sections.load_contracts:

      * absent    → materialize the built-in default to disk (best-effort) and
                    use it — a fresh install writes its seed once, silently.
      * present   → the on-disk map WINS (a founder's edit is authoritative and
                    is never clobbered by re-seeding — §6.2).
      * corrupt   → built-in default AND one on_warn banner. Never silent, never
                    overwrites the user's file.

    on_warn(msg) defaults to a no-op so docs.py stays stdlib-only and import-light
    (it has no emit channel of its own — the orchestrator call site wires one)."""
    default = _default_doc_map()
    path = _doc_map_path(orch_dir)
    if not os.path.exists(path):
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            _write(path, json.dumps(default, indent=2))
        except OSError:
            pass  # a read-only install still renders from the built-in default
        return default
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        if not _valid_doc_map(data):
            raise ValueError("doc_map.json must be an object with a docs[] list "
                             "of entries each having a filename and a sources list")
        _lint_slot_ownership(data, on_warn)   # 5.3: lint, not corruption
        return data
    except (OSError, ValueError, TypeError) as exc:
        if on_warn:
            on_warn("doc_map.json at %s is unreadable (%s) — falling back to the "
                    "built-in default blueprint." % (path, exc))
        return default


def _lint_slot_ownership(data, on_warn):
    """5.3 doc-map lint: report slots whose owner_section is missing or names a
    section the map doesn't declare. A lint, NOT corruption — the slot still
    renders; it is merely excluded from cross-lineage conflict/gap emission. The
    built-in default is valid by construction, so a healthy load banners nothing."""
    if not on_warn:
        return
    cats = {c.get("category_id") for c in (data.get("categories") or [])
            if isinstance(c, dict)}
    for s in (data.get("slots") or []):
        if not isinstance(s, dict):
            continue
        owner = s.get("owner_section")
        if owner is None:
            on_warn("doc_map slot %r has no owner_section — excluded from "
                    "cross-lineage conflict resolution." % (s.get("slot_id"),))
        elif owner not in cats:
            on_warn("doc_map slot %r names unknown owner_section %r — excluded "
                    "from conflict resolution." % (s.get("slot_id"), owner))


def render_composed_doc(app, doc_title, source_keys, ordered_phases,
                        phase_outputs, intro=""):
    """One spec doc assembled from a fixed set of source phases. Returns None if
    NONE of the source phases exist in this workflow (the doc doesn't apply)."""
    titles = dict(ordered_phases or [])
    present = [k for k in source_keys if k in titles]
    if not present:
        return None
    lines = ["# %s — %s" % (app, doc_title), ""]
    if intro:
        lines += [intro, ""]
    for k in present:
        lines.append("## %s\n" % titles[k])
        out = (phase_outputs or {}).get(k, "")
        lines.append(_phase_section(titles[k], out) if out.strip() else _NA)
        lines.append("")
    return "\n".join(lines)


def render_qa_report(app, ordered_phases, phase_outputs, verify_summary="",
                     sources=None, intro=None, doc_title="QA Report"):
    """QA_REPORT.md: the final review phase plus the structured verify result.
    sources / intro / doc_title default to the built-in QA doc so direct callers
    are unaffected; the doc_map path supplies them from the loaded blueprint."""
    md = render_composed_doc(
        app, doc_title,
        QA_SOURCES if sources is None else sources, ordered_phases, phase_outputs,
        intro=_QA_INTRO if intro is None else intro)
    if md is None:
        return None
    return (md + "## Build Verification\n\n"
            + (verify_summary if (verify_summary or "").strip()
               else "_N/A — no structured verification result exists._") + "\n")


def render_known_limitations(app, ordered_phases, phase_outputs, consensus_status,
                             verify_summary="", findings=None, blocked_conflict=None,
                             conversation_end=None):
    """KNOWN_LIMITATIONS.md: the honest gaps the engine actually observed —
    phases not run, phases without consensus, verification not passed,
    unresolved findings, and a blocked_conflict. Never inferred or invented.

    conversation_end (V3 board 1.1): {phase_key: end_reason} for conversational
    phases — a chat has no consensus BY DESIGN, so the "completed without
    consensus (decided by vote or last recap)" wording would be false on both
    counts for a user-ended conversation."""
    lines = ["# %s — Known Limitations" % app, "",
             "_Honest, non-AI list of gaps the engine recorded during the run. "
             "Nothing here is inferred or fabricated._", ""]
    items = []
    for k, title in (ordered_phases or []):
        if not (phase_outputs or {}).get(k, "").strip():
            items.append("Phase **%s** did not run — its decisions are missing." % title)
    for k, title in (ordered_phases or []):
        if not ((phase_outputs or {}).get(k, "").strip()
                and not (consensus_status or {}).get(k)):
            continue
        conv_reason = (conversation_end or {}).get(k)
        if conv_reason:
            # A conversational phase never has consensus by design; only an
            # unfinished close (idle timeout / shutdown / deadline) is a gap.
            if conv_reason != "ended by user":
                items.append("Conversation **%s** closed on %s — it may be "
                             "unfinished." % (title, conv_reason))
            continue
        items.append("Phase **%s** completed without consensus (decided by "
                     "vote or last recap)." % title)
    vs = (verify_summary or "").strip()
    if not vs:
        items.append("The build was never verified by a real tool — no structured "
                     "verification result exists.")
    elif not vs.upper().startswith("VERIFIED"):
        items.append("Build verification did not pass: %s." % vs)
    for f in (findings or []):
        if isinstance(f, dict) and str(f.get("status") or "open") == "open":
            items.append("Unresolved %s finding (%s): %s."
                         % (f.get("severity", "?"), f.get("category", "?"),
                            f.get("title", "untitled")))
    if blocked_conflict:
        detail = (blocked_conflict.get("detail") if isinstance(blocked_conflict, dict)
                  else str(blocked_conflict))
        items.append("Build is blocked on an unresolved lane merge conflict: %s." % detail)
    if items:
        lines += ["- %s" % i for i in items]
    else:
        lines.append("- None recorded — every included phase ran and reached "
                     "consensus, and verification passed.")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# V3 board 5.2: SUBSCRIBE-mode artifact ingestion into blueprint slots.
# docs.py never imports artifacts.py — the store is reached through an INJECTED
# reader (any object exposing lineage_index / latest_final / lineage_heads /
# is_admissible / read_body, which the artifacts module satisfies as-is). So
# `import docs` still works with artifacts.py absent (leaf purity), and reader=
# None skips the whole layer → the 5.1 doc set stays byte-identical.
# ---------------------------------------------------------------------------
_BLUEPRINT_INTRO = (
    "_Deterministic render of the App Factory handoff blueprint. Artifact-filled "
    "slots name their source; unfilled slots stay visibly unfilled — nothing "
    "here is fabricated._")


def _artifact_root(meta):
    """The lineage root id of an artifact meta (lineage[0], else its own id).
    Inline so docs.py needs no artifacts import."""
    lin = meta.get("lineage")
    if isinstance(lin, list) and lin and isinstance(lin[0], str):
        return lin[0]
    return meta.get("id")


def _slot_selector(slot):
    """(tag, types) for a blueprint slot's artifact ingestion. The OPTIONAL
    per-slot 'artifact' {types:[...], match:"<tag>"} defaults to match=slot_id,
    types=any — so the default 40-slot map ingests any admissible final artifact
    tagged with the slot's own id, with zero doc_map surgery."""
    sel = slot.get("artifact") if isinstance(slot.get("artifact"), dict) else {}
    match = sel.get("match")
    tag = match if isinstance(match, str) and match else slot.get("slot_id")
    raw = sel.get("types")
    types = ({t for t in raw if isinstance(t, str)}
             if isinstance(raw, list) else set())
    return tag, types


def _artifact_matches(meta, tag, types):
    """True iff an artifact meta CLAIMS a slot: its doc_slots (guarded to a list
    — a corrupt non-list value must degrade, never raise `tag in 1`) contains
    the tag, and its type passes the optional filter. Applied uniformly at root
    discovery, the resolved tip, and branched heads so the effective rule is
    always 'the CLAIMING artifact is an allowed type', never 'some ancestor
    was' (a supersede can change type or drop the tag)."""
    ds = meta.get("doc_slots")
    return (isinstance(ds, list) and tag in ds
            and (not types or meta.get("type") in types))


def _head_label(m):
    """'id (v<n>-<branch|a>)' for a branched-head conflict note — mirrors
    latest_final's own refusal wording."""
    try:
        v = int(m.get("version"))
    except (TypeError, ValueError):
        v = 0
    return "%s (v%s-%s)" % (m.get("id"), v, m.get("branch") or "a")


def _resolve_slot_artifact(project_dir, reader, idx, slot, on_warn):
    """Resolve ONE blueprint slot against the artifact store. Returns exactly one:
       ("fill", tip_meta, body)   an admissible FINAL artifact fills the slot
       ("conflict", root, heads)  a branched lineage claims it (stays unfilled)
       (None, None, None)         no candidate — the caller falls through
    Never raises: reader calls report-not-raise, every access is .get-guarded.
    Final-only is enforced by is_admissible on the resolved tip — latest_final
    returns the live tip regardless of status, so it alone is NOT enough."""
    tag, types = _slot_selector(slot)
    if not tag:
        return (None, None, None)
    swallow = lambda _m: None
    roots = set()
    for m in (idx.get("by_id") or {}).values():
        if _artifact_matches(m, tag, types):
            r = _artifact_root(m)
            if r:
                roots.add(r)
    fills, conflicts = [], []
    for root in sorted(roots):
        tip = reader.latest_final(project_dir, root, on_error=swallow, index=idx)
        if tip is not None:
            # Re-check the CLAIM (tag + type, list-guarded) ON THE RESOLVED TIP,
            # not just at root discovery: a supersede can change type or drop the
            # tag, so an ancestor matching the selector must not admit a tip that
            # no longer does.
            if (_artifact_matches(tip, tag, types)
                    and reader.is_admissible(project_dir, tip,
                                             on_error=swallow, index=idx)):
                body = reader.read_body(project_dir, tip.get("id"),
                                        on_error=on_warn)
                if body is None:
                    on_warn("blueprint slot %r: artifact %r body unreadable — "
                            "falling through" % (slot.get("slot_id"),
                                                 tip.get("id")))
                else:
                    fills.append((tip, body))
            # A tip that dropped the tag (it lived on an abandoned old version)
            # is neither a fill nor a conflict for this slot — honest, not a pick.
        else:
            heads = reader.lineage_heads(project_dir, root, on_error=swallow,
                                         index=idx) or []
            if (len(heads) >= 2
                    and any(_artifact_matches(h, tag, types) for h in heads)):
                conflicts.append((root, heads))
            elif not heads:
                on_warn("blueprint slot %r: lineage %r unresolvable (cycle) — "
                        "falling through" % (slot.get("slot_id"), root))
    if fills:
        fills.sort(key=lambda tb: (tb[0].get("ts", ""), tb[0].get("id", "")))
        if len(fills) > 1:
            # V3 5.3: >1 admissible final tip from DISTINCT lineage roots =
            # cross-lineage contention (disjoint lineages, neither supersedes the
            # other). NEVER pick a winner (never last-write-wins) — hand every
            # tip to the caller, which renders a conflict note + emits a gap.
            return ("contended", [tb[0] for tb in fills], None)
        return ("fill", fills[-1][0], fills[-1][1])
    if conflicts:
        return ("conflict", conflicts[0][0], conflicts[0][1])
    return (None, None, None)


def render_handoff_blueprint(app, doc_map, ordered_phases, phase_outputs,
                             project_dir, reader, on_warn=None, contentions=None,
                             coverage=None, required_slots=None):
    """HANDOFF_BLUEPRINT.md (V3 5.2/5.3/5.4): the 40 blueprint slots grouped under
    the 11 categories, each filled by per-slot precedence artifact > phase-output
    (slot.sources) > empty — NEVER merged. An artifact fill names its source (id +
    version); a same-lineage branch renders a branch-conflict note; >1 admissible
    final from DISJOINT lineages is a cross-lineage conflict (5.3) — an explicit
    note, the slot left UNFILLED, and (when caller-collected) a contention record
    appended to `contentions` for idempotent gap emission. An unfilled slot is
    visibly unfilled (never invented). Returns the markdown, or None when the map
    declares no slots.

    Two OPTIONAL out-params, both default None ⇒ no collection ⇒ string output
    byte-identical to a 5.2 render (the slot is resolved exactly once regardless):
      * `contentions` — 5.3 cross-lineage conflict records (for conflict gaps).
      * `coverage` (5.4) — ONE record per slot {slot_id, slot_title, category,
        owner_section, status: filled|thin|empty|conflict, reason: None|thin|
        empty|lineage_conflict, min_chars, observed_chars, evidence, content_hash}
        driving GAP_REPORT.md + empty/thin gap emission.
    Reader-gated by the caller so reader=None keeps 5.1 byte-identical."""
    if on_warn is None:
        on_warn = lambda _m: None
    slots = doc_map.get("slots") or []
    if required_slots is not None:
        required = set(required_slots)
        slots = [slot for slot in slots if slot.get("slot_id") in required]
    if not slots:
        return None
    valid_owners = {c.get("category_id") for c in (doc_map.get("categories") or [])
                    if isinstance(c, dict)}
    idx = reader.lineage_index(project_dir, on_error=on_warn)
    lines = ["# %s — Handoff Blueprint" % app, "", _BLUEPRINT_INTRO, ""]
    by_cat = {}
    for s in slots:
        by_cat.setdefault(s.get("category"), []).append(s)
    seen_cat = set()

    def _cov(s, status, reason, observed, evidence, content_hash, min_chars):
        if coverage is None:
            return
        coverage.append({
            "slot_id": s.get("slot_id"),
            "slot_title": s.get("title") or s.get("slot_id"),
            "category": s.get("category"),
            "owner_section": s.get("owner_section"),
            "status": status, "reason": reason,
            "min_chars": min_chars, "observed_chars": observed,
            "evidence": evidence, "content_hash": content_hash,
        })

    def _emit(cat_id, title):
        seen_cat.add(cat_id)
        group = by_cat.get(cat_id) or []
        if not group:
            return
        lines.append("## %s\n" % (title or cat_id))
        for s in group:
            lines.append("### %s\n" % (s.get("title") or s.get("slot_id")))
            mc = _slot_min_chars(s)
            kind, a, b = _resolve_slot_artifact(project_dir, reader, idx, s,
                                                on_warn)
            if kind == "fill":
                body_s = str(b).strip()
                lines.append(body_s)
                lines.append("")
                lines.append("_Source: artifact %s v%s_"
                             % (a.get("id"), a.get("version", 1)))
                thin = len(body_s) < mc
                _cov(s, "thin" if thin else "filled", "thin" if thin else None,
                     len(body_s),
                     "%s v%s" % (a.get("id"), a.get("version", 1)),
                     a.get("content_hash"), mc)
            elif kind == "contended":
                # V3 5.3: cross-lineage conflict. Deterministic (sorted by id) so
                # the note and any gap are publish-order-independent — never
                # last-write-wins. A reconcile CANNOT merge disjoint lineages, so
                # the resolution advice is supersede-to-drop-claim / withdraw.
                tips = sorted(a, key=lambda m: m.get("id") or "")
                n = len(tips)
                owner = s.get("owner_section")
                ids = [m.get("id") for m in tips]
                ids_vers = ", ".join("%s (v%s)" % (m.get("id"),
                                                   m.get("version", 1))
                                     for m in tips)
                if owner in valid_owners:
                    lines.append("_Unresolved — cross-lineage ownership conflict "
                                 "(section '%s'): %s — %d artifacts from disjoint "
                                 "lineages claim this slot; none supersedes "
                                 "another, so the engine will not pick a winner. "
                                 "A 'gap' artifact (reason lineage_conflict) "
                                 "records it. Slot left unfilled — supersede all "
                                 "but one lineage's tip to drop its claim, or "
                                 "withdraw the extra contenders._"
                                 % (owner, ids_vers, n))
                    if contentions is not None:
                        contentions.append({
                            "slot_id": s.get("slot_id"),
                            "slot_title": s.get("title") or s.get("slot_id"),
                            "owner_section": owner,
                            "contending": ids,
                            "content_hashes": [m.get("content_hash")
                                               for m in tips],
                            "versions": [m.get("version", 1) for m in tips],
                            "roots": [_artifact_root(m) for m in tips],
                        })
                    _cov(s, "conflict", "lineage_conflict", 0,
                         ",".join(x for x in ids if x), None, mc)
                else:
                    # Unowned/unknown-owner: still NEVER pick, but no gap (no
                    # owner to attribute). load_doc_map already linted the owner.
                    lines.append("_Unresolved — cross-lineage conflict on a slot "
                                 "with no valid owner_section (%r): %s (%d "
                                 "lineages) claim it. The engine will not pick a "
                                 "winner; no gap emitted (fix owner_section in "
                                 "doc_map.json). Slot left unfilled._"
                                 % (owner, ids_vers, n))
                    _cov(s, "conflict", None, 0,
                         ",".join(x for x in ids if x), None, mc)
            elif kind == "conflict":
                named = ", ".join(_head_label(h) for h in b)
                lines.append("_Unresolved — lineage '%s' is branched across "
                             "heads %s; publish a 'reconcile' artifact. Slot "
                             "left unfilled._" % (a, named))
                # A same-lineage branch has finals but none RESOLVES (latest_final
                # refused) — it satisfies the EMPTY predicate ("no final matched")
                # and must appear in the work queue; evidence names the heads so
                # the gap body is honest about why it is empty.
                head_ids = ",".join(h.get("id") or "" for h in (b or []))
                _cov(s, "empty", "empty", 0, "branch-heads:" + head_ids, "", mc)
            else:
                # Guard a corrupt slot 'sources' (disk-wins doc_map isn't
                # validated at the slot level): a string would iterate
                # char-by-char and silently drop the fill; an int/bool would
                # raise `for k in 5` and abort the WHOLE blueprint. Degrade this
                # one slot visibly instead.
                srcs = s.get("sources")
                if srcs and not isinstance(srcs, list):
                    on_warn("blueprint slot %r: 'sources' is %s, not a list — "
                            "ignored" % (s.get("slot_id"),
                                         type(srcs).__name__))
                    srcs = []
                keys = [k for k in (srcs or [])
                        if isinstance(k, str)
                        and (phase_outputs or {}).get(k, "").strip()]
                parts = [(phase_outputs or {}).get(k, "") for k in keys]
                if parts:
                    body = "\n\n".join(parts)
                    lines.append(_phase_section(s.get("title") or "", body))
                    observed = len(body.strip())
                    thin = observed < mc
                    ch = hashlib.sha256(body.encode("utf-8")).hexdigest()
                    _cov(s, "thin" if thin else "filled",
                         "thin" if thin else None, observed,
                         "phase:" + ",".join(keys), ch, mc)
                else:
                    lines.append("_Unfilled — no artifact or phase output for "
                                 "this slot._")
                    _cov(s, "empty", "empty", 0, None, "", mc)
            lines.append("")

    for c in (doc_map.get("categories") or []):
        _emit(c.get("category_id"), c.get("title"))
    for cat_id in by_cat:                     # any slot whose category is
        if cat_id not in seen_cat:            # unknown still renders (honesty)
            _emit(cat_id, cat_id or "Other")
    return "\n".join(lines)


DEFAULT_MIN_CHARS = 200


def _slot_min_chars(slot):
    """The thinness threshold for a slot: its own min_chars (doc_map data, disk
    wins) when a sane non-negative int, else the code floor (covers older/hand-
    edited maps still carrying null). bool is rejected (True/False are ints)."""
    v = slot.get("min_chars")
    if isinstance(v, int) and not isinstance(v, bool) and v >= 0:
        return v
    return DEFAULT_MIN_CHARS


def _slot_gap_dedupe_key(slot_id, reason, content_hash):
    """Idempotency key for an empty/thin gap: slot + reason + the content_hash of
    the CURRENT fill (or "" for empty). Includes the reason literal so an empty
    and a later thin gap for the same slot are distinct keys, and so it can never
    collide with a 5.3 conflict key (those are pure sorted content-hash lines —
    a reason literal is neither 64 hex chars nor present there)."""
    payload = (slot_id or "") + "\n" + reason + "\n" + (content_hash or "")
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _conflict_dedupe_key(slot_id, content_hashes):
    """Deterministic idempotency key for a cross-lineage conflict: the slot plus
    the SORTED content_hashes of the contending tips. Stable across re-renders of
    the same contention; changes when the contending set changes (a new claimant
    or a new-content tip) so a genuinely different conflict emits a fresh gap."""
    payload = (slot_id or "") + "\n" + "\n".join(sorted(
        h for h in content_hashes if isinstance(h, str)))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _conflict_gap_body(c):
    """Deterministic, non-AI gap body — every fact is drawn from the contending
    metas (ids, versions, roots, content_hashes), nothing invented (R3)."""
    lines = [
        "# Doc-slot ownership conflict: %s (%s)" % (c["slot_title"],
                                                    c["slot_id"]),
        "",
        "Section '%s' owns this blueprint slot, but %d final artifacts from "
        "disjoint lineages claim it. The engine will not pick a winner (never "
        "last-write-wins); the slot is left unfilled until resolved."
        % (c["owner_section"], len(c["contending"])),
        "",
        "Contending artifacts:",
    ]
    for aid, ver, root, h in zip(c["contending"], c["versions"],
                                 c["roots"], c["content_hashes"]):
        lines.append("- %s v%s (lineage root %s, content_hash %s)"
                     % (aid, ver, root, h))
    lines += ["",
              "Resolution: supersede one lineage's tip so it no longer claims "
              "this slot, or withdraw one contender. A reconcile cannot merge "
              "disjoint lineages."]
    return "\n".join(lines)


def _slot_gap_meta(r, key):
    """The meta for an empty/thin gap. THE 7.5 STABILITY CONTRACT — the gap
    `fields` schema {slot_id, owner_section, reason(empty|thin|lineage_conflict),
    doc, min_chars, observed_chars, evidence, dedupe_key} is what the Conductor's
    goal predicate parses. ADDITIVE-ONLY: a new field is fine, but renaming or
    removing one (or changing `reason`'s enum) is a breaking change that must bump
    schemas.SCHEMA_VERSION (publish stamps schema_version on every meta)."""
    reason = r["reason"]
    impact = ("Blueprint slot '%s' (owned by section '%s') is %s: %d of the "
              "required %d characters. The handoff is incomplete until the "
              "owning section fills it."
              % (r["slot_id"], r["owner_section"], reason,
                 r["observed_chars"], r["min_chars"]))
    return {
        "type": "gap",
        "title": "Doc gap (%s): %s" % (reason, r["slot_id"]),
        "source": {"section": "documentation"},
        "impact": impact,
        "slot_id": r["slot_id"],
        "owner_section": r["owner_section"],
        "reason": reason,
        "doc": "HANDOFF_BLUEPRINT.md",
        "min_chars": r["min_chars"],
        "observed_chars": r["observed_chars"],
        "evidence": r.get("evidence"),
        "dedupe_key": key,
    }


def _slot_gap_body(r):
    """Deterministic, non-AI gap body — every fact from the coverage record."""
    ev = r.get("evidence") or "none (no artifact or phase output feeds it)"
    return "\n".join([
        "# Doc gap (%s): %s (%s)" % (r["reason"], r["slot_title"], r["slot_id"]),
        "",
        "Owning section: %s" % r["owner_section"],
        "Reason: %s — %d of %d required characters."
        % (r["reason"], r["observed_chars"], r["min_chars"]),
        "Partial evidence: %s" % ev,
        "",
        "Fill this slot by routing it to the owning section (manual Send-to). "
        "Nothing here is inferred — this reports an observed absence.",
    ])


def _try_publish_gap(reader, project_dir, registry, body, meta, key,
                     open_keys, warn):
    """Publish one gap, report-not-raise; on success record the key so the pass
    de-dupes within itself, on failure warn (the render/report already stand)."""
    slot = meta.get("slot_id")
    gid = None
    try:
        gid = reader.publish(project_dir, body, meta, registry,
                             on_error=warn, consensus=True)
    except Exception as exc:               # publish should report-not-raise;
        warn("gap publish raised for slot %r (%s) — the report stands"
             % (slot, exc))                # defend anyway
    if gid is None:
        warn("gap publish FAILED for slot %r — the gap is in GAP_REPORT.md but "
             "no gap artifact was recorded" % (slot,))
    else:
        open_keys.add(key)


def _emit_gaps(project_dir, reader, orch_dir, coverage, contentions, on_warn):
    """Publish one idempotent 'gap' artifact per empty/thin slot (from coverage)
    AND per cross-lineage contention (5.3, from contentions), through the injected
    artifacts reader (docs.py never imports artifacts). One shared open-key set
    dedupes all three reasons. Best-effort: a publish failure degrades to a
    visible warning — it never raises, so a store fault can't fail the render."""
    warn = on_warn or (lambda _m: None)
    registry = reader.load_registry(orch_dir, on_error=warn)
    existing = reader.list_artifacts(project_dir, type="gap", on_error=warn)
    open_keys = set()
    for g in (existing or []):
        f = g.get("fields")
        if not isinstance(f, dict):
            continue                       # a corrupt gap meta must not abort the
                                           # scan (never-raises: §13.3)
        if (g.get("status") != "converged"
                and isinstance(f.get("dedupe_key"), str)):
            open_keys.add(f["dedupe_key"])
    for r in (coverage or []):
        if r.get("reason") not in ("empty", "thin"):
            continue
        key = _slot_gap_dedupe_key(r["slot_id"], r["reason"],
                                   r.get("content_hash"))
        if key in open_keys:
            continue                       # idempotent: this state is recorded
        _try_publish_gap(reader, project_dir, registry, _slot_gap_body(r),
                         _slot_gap_meta(r, key), key, open_keys, warn)
    for c in (contentions or []):
        key = _conflict_dedupe_key(c["slot_id"], c["content_hashes"])
        if key in open_keys:
            continue
        hashes = sorted(h for h in c["content_hashes"] if isinstance(h, str))
        impact = ("Blueprint slot '%s' (owned by section '%s') renders unfilled "
                  "while %d disjoint-lineage artifacts contend; the handoff is "
                  "missing this section until one lineage's claim is withdrawn."
                  % (c["slot_id"], c["owner_section"], len(c["contending"])))
        meta = {
            "type": "gap",
            "title": "Doc-slot conflict: %s" % c["slot_id"],
            "source": {"section": "documentation"},
            "impact": impact,
            "slot_id": c["slot_id"],
            "owner_section": c["owner_section"],
            "reason": "lineage_conflict",
            "contending": sorted(c["contending"]),
            "content_hashes": hashes,
            "dedupe_key": key,
        }
        _try_publish_gap(reader, project_dir, registry, _conflict_gap_body(c),
                         meta, key, open_keys, warn)


def render_gap_report(app, coverage):
    """GAP_REPORT.md (V3 5.4): a deterministic, non-AI render of the CURRENT slot
    scan — per-category coverage counts, then one line per empty/thin/conflicted
    slot (title, owning section, reason, evidence). It reports OBSERVED absence;
    it never infers or fabricates content (R3). Rendered from the scan, NOT from
    list_artifacts, so a stale gap artifact can never make it lie (R2): 'no gaps'
    means the current scan is clean, which is exactly 7.5's termination signal."""
    cov = coverage or []
    total = len(cov)
    counts = {"filled": 0, "thin": 0, "empty": 0, "conflict": 0}
    for r in cov:
        counts[r.get("status", "empty")] = counts.get(r.get("status"), 0) + 1
    lines = ["# %s — Gap Report" % app, "",
             "_Honest, non-AI scan of the handoff blueprint. Nothing here is "
             "inferred; each line reports an observed empty, thin, or conflicted "
             "slot._", "",
             "## Coverage", "",
             "- Filled: %d / %d" % (counts["filled"], total),
             "- Thin (under min_chars): %d" % counts["thin"],
             "- Empty: %d" % counts["empty"],
             "- Lineage conflicts: %d" % counts["conflict"], "",
             "## Open gaps", ""]
    rows = [r for r in cov if r.get("status") != "filled"]
    if not rows:
        lines.append("- None — every blueprint slot is filled to its minimum. "
                     "Handoff is complete.")
    else:
        for r in rows:
            reason = r.get("reason") or "conflict (unowned)"
            ev = r.get("evidence") or "none"
            lines.append("- **%s** (%s) — %s; %d/%d chars; evidence: %s"
                         % (r.get("slot_title") or r.get("slot_id"),
                            r.get("owner_section"), reason,
                            r.get("observed_chars", 0), r.get("min_chars", 0),
                            ev))
    return "\n".join(lines) + "\n"


def write_project_docs(app_dir, app, ordered_phases, phase_outputs,
                       consensus_status=None, workflow_name="app_build",
                       verify_summary="", findings=None, blocked_conflict=None,
                       conversation_end=None, orch_dir=None, on_warn=None,
                       artifact_reader=None, human_overrides=None,
                       override_notice="", required_slots=None):
    """Render + persist the full doc set (§24) into <app_dir>/docs/:
    PROJECT_DOCUMENTATION.md, LAUNCH_READINESS.md, phase_outputs.json, plus
    PRD.md / TECHNICAL_ARCHITECTURE.md / QA_REPORT.md when their source phases
    exist in this workflow, and KNOWN_LIMITATIONS.md always. Best-effort;
    returns the list of files written."""
    docs_dir = os.path.join(app_dir, "docs")
    os.makedirs(docs_dir, exist_ok=True)
    written = []
    overrides = set(human_overrides or [])

    def put(rel, text):
        if rel in overrides:
            return
        _write(os.path.join(app_dir, rel), text)
        written.append(rel)

    try:
        put("docs/phase_outputs.json",
            json.dumps(phase_outputs or {}, indent=2))
        put("docs/PROJECT_DOCUMENTATION.md",
            render_project_documentation(app, ordered_phases, phase_outputs,
                                         workflow_name))
        put("docs/LAUNCH_READINESS.md",
            render_launch_readiness(app, ordered_phases, phase_outputs,
                                    consensus_status, verify_summary,
                                    findings=findings) + override_notice)
        # V3 board 5.1: the composed docs (PRD / TECHNICAL_ARCHITECTURE / QA) are
        # driven by the loaded doc_map instead of hardcoded tuples. Iterating the
        # map's docs[] in its stored order preserves the historical write order
        # (PRD.md, TECHNICAL_ARCHITECTURE.md, QA_REPORT.md); a doc whose sources
        # are all absent still returns None and is skipped, never fabricated.
        doc_map = load_doc_map(orch_dir or _ORCH_DIR, on_warn)
        for entry in doc_map.get("docs", []):
            filename = entry.get("filename")
            if not filename:
                continue
            title = entry.get("title") or entry.get("doc_id") or ""
            intro = entry.get("intro", "")
            sources = entry.get("sources") or []
            if entry.get("kind") == "qa":
                md = render_qa_report(app, ordered_phases, phase_outputs,
                                      verify_summary, sources=sources,
                                      intro=intro, doc_title=title)
            else:
                md = render_composed_doc(app, title, sources, ordered_phases,
                                         phase_outputs, intro=intro)
            if md is not None:
                put("docs/" + filename, md)
        put("docs/KNOWN_LIMITATIONS.md",
            render_known_limitations(app, ordered_phases, phase_outputs,
                                     consensus_status, verify_summary,
                                     findings=findings,
                                     blocked_conflict=blocked_conflict,
                                     conversation_end=conversation_end))
        # V3 5.2: SUBSCRIBE artifact ingestion. Reader-gated and LAST, in its own
        # guard, so a store fault can never disturb the 5.1 docs already written
        # and reader=None leaves the doc set byte-identical to 5.1.
        if artifact_reader is not None:
            try:
                contentions, coverage = [], []
                bp = render_handoff_blueprint(
                    app, doc_map, ordered_phases, phase_outputs, app_dir,
                    artifact_reader, on_warn=on_warn, contentions=contentions,
                    coverage=coverage, required_slots=required_slots)
                if bp is not None:
                    put("docs/HANDOFF_BLUEPRINT.md", bp)
                    # 5.4: the gap report is scan-derived (never from the bus),
                    # so it can't surface stale gaps — written even when clean.
                    put("docs/GAP_REPORT.md",
                        render_gap_report(app, coverage) + override_notice)
                # Publish empty/thin + conflict gaps AFTER the docs are on disk,
                # so the report survives even if gap emission fails (5.3/5.4).
                if coverage or contentions:
                    _emit_gaps(app_dir, artifact_reader, orch_dir or _ORCH_DIR,
                               coverage, contentions, on_warn)
            except Exception as exc:      # never crash the run on a store fault
                if on_warn:
                    on_warn("HANDOFF_BLUEPRINT render skipped: %s" % exc)
    except OSError:
        pass
    return written


def recompute_gap_report(app_dir, app, ordered_phases, phase_outputs,
                         orch_dir, artifact_reader, required_slots,
                         on_warn=None):
    """Re-scan one live session after a Situation switch.

    Only the generated handoff/gap pair is replaced; phase docs and human
    inputs are untouched. Existing artifacts and phase outputs remain the
    evidence source, so narrowing a Situation never discards prior work.
    Returns the scoped coverage records, or None when rendering failed.
    """
    try:
        doc_map = load_doc_map(orch_dir or _ORCH_DIR, on_warn)
        contentions, coverage = [], []
        blueprint = render_handoff_blueprint(
            app, doc_map, ordered_phases, phase_outputs, app_dir,
            artifact_reader, on_warn=on_warn, contentions=contentions,
            coverage=coverage, required_slots=required_slots)
        if blueprint is None:
            return None
        docs_dir = os.path.join(app_dir, "docs")
        os.makedirs(docs_dir, exist_ok=True)
        _write(os.path.join(docs_dir, "HANDOFF_BLUEPRINT.md"), blueprint)
        _write(os.path.join(docs_dir, "GAP_REPORT.md"),
               render_gap_report(app, coverage))
        if coverage or contentions:
            _emit_gaps(app_dir, artifact_reader, orch_dir or _ORCH_DIR,
                       coverage, contentions, on_warn)
        return coverage
    except Exception as exc:
        if on_warn:
            on_warn("Situation gap recompute failed: %s" % exc)
        return None
