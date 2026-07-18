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
                          findings=None):
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
    try:
        _write(os.path.join(docs_dir, "phase_discussions.json"),
               json.dumps(phase_entries, indent=2))
        written.append("docs/phase_discussions.json")
        _write(os.path.join(docs_dir, "COMPLETE_PROJECT_DOSSIER.md"), dossier)
        written.append("docs/COMPLETE_PROJECT_DOSSIER.md")
        _write(os.path.join(docs_dir, "FULL_TRANSCRIPT.txt"), full_transcript)
        written.append("docs/FULL_TRANSCRIPT.txt")
        _write(os.path.join(docs_dir, "PROJECT_RECORD.json"),
               json.dumps(record, indent=2))
        written.append("docs/PROJECT_RECORD.json")
        _write(os.path.join(integrations_dir, "project_management_backfill.json"),
               json.dumps(backfill, indent=2))
        written.append("integrations/project_management_backfill.json")
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
# owner_section (5.3) and min_chars (5.4) are declared-but-inert here; sources[]
# stays empty until 5.2 wires artifact→slot ingestion. This is a template, never
# project content — nothing here is a fabricated fact.
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
                   "sources": [], "owner_section": None, "min_chars": None}
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
        return data
    except (OSError, ValueError, TypeError) as exc:
        if on_warn:
            on_warn("doc_map.json at %s is unreadable (%s) — falling back to the "
                    "built-in default blueprint." % (path, exc))
        return default


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


def write_project_docs(app_dir, app, ordered_phases, phase_outputs,
                       consensus_status=None, workflow_name="app_build",
                       verify_summary="", findings=None, blocked_conflict=None,
                       conversation_end=None, orch_dir=None, on_warn=None):
    """Render + persist the full doc set (§24) into <app_dir>/docs/:
    PROJECT_DOCUMENTATION.md, LAUNCH_READINESS.md, phase_outputs.json, plus
    PRD.md / TECHNICAL_ARCHITECTURE.md / QA_REPORT.md when their source phases
    exist in this workflow, and KNOWN_LIMITATIONS.md always. Best-effort;
    returns the list of files written."""
    docs_dir = os.path.join(app_dir, "docs")
    os.makedirs(docs_dir, exist_ok=True)
    written = []
    try:
        _write(os.path.join(docs_dir, "phase_outputs.json"),
               json.dumps(phase_outputs or {}, indent=2))
        written.append("docs/phase_outputs.json")
        _write(os.path.join(docs_dir, "PROJECT_DOCUMENTATION.md"),
               render_project_documentation(app, ordered_phases, phase_outputs, workflow_name))
        written.append("docs/PROJECT_DOCUMENTATION.md")
        _write(os.path.join(docs_dir, "LAUNCH_READINESS.md"),
               render_launch_readiness(app, ordered_phases, phase_outputs,
                                       consensus_status, verify_summary,
                                       findings=findings))
        written.append("docs/LAUNCH_READINESS.md")
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
                _write(os.path.join(docs_dir, filename), md)
                written.append("docs/" + filename)
        _write(os.path.join(docs_dir, "KNOWN_LIMITATIONS.md"),
               render_known_limitations(app, ordered_phases, phase_outputs,
                                        consensus_status, verify_summary,
                                        findings=findings,
                                        blocked_conflict=blocked_conflict,
                                        conversation_end=conversation_end))
        written.append("docs/KNOWN_LIMITATIONS.md")
    except OSError:
        pass
    return written
