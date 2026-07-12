#!/usr/bin/env python3
"""
seed_demo.py — create sample + demo projects for the orchestrator GUI.

The agent CLIs (codex / claude / agy / gemini) may not be installed/logged-in
on every machine, so a real `run.sh` pass cannot always produce data. This
script writes projects in the EXACT on-disk format that orchestrator.py emits
(agent_state.json + per-phase Markdown transcripts) so the GUI has realistic,
multi-state data to render. When you later install + log into the CLIs, a real
`bash run.sh --app sample_app` produces identical-format files the GUI reads.

Stdlib only. Idempotent: re-running overwrites the demo apps.

Usage:
  python3 .orchestrator/seed_demo.py            # seed sample_app + demo apps
  python3 .orchestrator/seed_demo.py --root DIR # custom workspace root
"""

import argparse
import datetime as _dt
import hashlib
import json
import os

# Default workspace: ORCH_ROOT env, else <repo>/workspace next to this engine dir.
ROOT_DEFAULT = os.environ.get("ORCH_ROOT") or os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "workspace")

# (key, folder, filename, title, purpose) — mirrors orchestrator.PHASES.
PHASES = [
    ("initial_discussion", "initial_discussion", "initial_discussion.md",
     "Initial Discussion",
     "Establish a shared, precise understanding of what this app is, who it is "
     "for, the core problem it solves, hard scope boundaries, and measurable "
     "success criteria."),
    ("next_steps_small", "next_steps_small", "next_steps_small.md",
     "Next Steps Small",
     "Define the smallest valuable slice / immediate next steps that de-risk and "
     "validate the concept with minimal effort."),
    ("detailed_discussion", "detailed_discussion", "detailed_discussion.md",
     "Detailed Discussion",
     "Deeply analyze requirements, edge cases, constraints, assumptions, risks, "
     "and open questions, resolving ambiguity left from earlier phases."),
    ("app_features", "app_features", "app_features.md",
     "App Features",
     "Enumerate and prioritize the full feature set with an explicit scoring / "
     "MoSCoW model and clear in-scope vs out-of-scope lines."),
    ("design_discussion", "design_discussion", "design_discussion.md",
     "Design Discussion",
     "Decide UX flows, information architecture, key screens/states, and the "
     "design principles that govern them."),
    ("tech_specs", "tech_specs", "tech_specs.md",
     "Tech Specs",
     "Decide architecture, technology stack, data model, interfaces/APIs, and "
     "non-functional requirements with justified trade-offs."),
    ("project_plan", "project_plan", "project_plan.md",
     "Project Plan",
     "Produce milestones, sequencing, estimates, dependencies, and a risk-managed "
     "delivery plan."),
    ("task_assignments", "task_assignments", "task_assignments.md",
     "Task Assignments",
     "Produce concrete workstreams, file ownership, forbidden edit zones, branch "
     "names, merge strategy, testing responsibilities, communication protocol, "
     "and a conflict-prevention plan."),
    ("build_coordination", "build_coordination", "agent_messages.md",
     "Build Coordination",
     "Iterative build coordination via agent messages. Planning and task "
     "assignment only unless build_code_changes_enabled is true."),
    ("final_review", "final_review", "final_review.md",
     "Final Review",
     "Final review of completeness, consistency, risks, quality gates, and a "
     "clear go / no-go decision."),
]

DISPLAY = {"codex": "Codex", "claude": "Claude", "gemini": "Gemini"}
SIGNATURE = {"codex": "From Codex", "claude": "From Claude", "gemini": "From Gemini"}

# Per-agent voice so transcripts read believably and critique each other by name.
STANCE = {
    "codex": "I favor the most pragmatic, minimal-effort path that ships value fast.",
    "claude": "I focus on correctness, edge cases, and failure modes others gloss over.",
    "gemini": "I optimize for simplicity and a clean user experience above all.",
}


def now_str():
    return _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def sha256_text(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def agent_message(agent, phase_title, rnd, others):
    # Deterministic critic pick: builtin hash() is per-process salted for str
    # (PYTHONHASHSEED), which would break the "deterministic/idempotent" contract
    # — a re-run must reproduce the same demo transcript.
    crit = others[int(sha256_text(phase_title + agent), 16) % len(others)]
    return (
        "%s Honestly, for the %s stage I keep landing in the same place: keep it "
        "dead simple and get something real in front of people fast. %s made a fair "
        "point last round, but I think it over-complicates the first version — we can "
        "layer that in later if people actually ask for it. Here's the version I'd "
        "commit to, and why I think it wins on the stuff that actually matters for "
        "this app…"
        % (STANCE[agent], phase_title, DISPLAY[crit])
    )


def coordinator_message(phase_title, rnd, final_text):
    return (
        "Okay, I think we've actually landed here. Everyone's circling the same "
        "answer for the %s stage now and that earlier disagreement got resolved in "
        "the back-and-forth. I'm calling it.\n\n"
        "CONSENSUS: YES\n\n"
        "## Final Output\n\n%s"
        % (phase_title, final_text)
    )


def final_text_for(phase_key, phase_title, app):
    return (
        "Decision for **%s** in the %s phase: adopt Option A. "
        "Owners and thresholds are fixed; this output feeds the next phase as a "
        "settled constraint." % (app, phase_title)
    )


def phase_header(app, title, purpose, original_prompt):
    return (
        "# %s — %s\n\n" % (app, title)
        + "_Generated by the autonomous multi-agent orchestrator on %s._\n\n" % now_str()
        + "## Original Prompt\n\n```\n%s\n```\n\n" % original_prompt.strip()
        + "## Phase Purpose\n\n%s\n\n" % purpose
        + "## Rubric\n\nThe agents define and agree a 100-point rubric (criteria + "
        + "weights) within the transcript below before any forced vote.\n\n"
        + "## Transcript\n\n"
    )


def render_phase_md(app, phasedef, original_prompt, rounds, complete):
    key, _folder, _fname, title, purpose = phasedef
    out = [phase_header(app, title, purpose, original_prompt)]
    order = ["codex", "claude", "gemini"]
    final = final_text_for(key, title, app)
    for rnd in range(1, rounds + 1):
        out.append("\n### Round %d\n\n" % rnd)
        for agent in order:
            others = [a for a in order if a != agent]
            block = "**%s — Round %d**\n\n%s\n" % (
                DISPLAY[agent], rnd, agent_message(agent, title, rnd, others))
            out.append("\n" + block)
        # Coordinator turn (Gemini). Declares consensus on the final round only.
        last = (rnd == rounds) and complete
        cresp = (coordinator_message(title, rnd, final) if last
                 else "Good progress this round, but we're not fully agreed yet — a "
                      "couple of things still to hash out before we lock it in.\n\n"
                      "CONSENSUS: NO")
        out.append("\n**Coordinator (Gemini) — decision after round %d**\n\n%s\n"
                   % (rnd, cresp))
    if complete:
        out.append("\n## Coordinator Decision\n\nSee the coordinator's message above.\n")
        out.append("\n## Final Output\n\n%s\n" % final)
        out.append("\n---\n\nCONSENSUS: YES\n")
    return "".join(out), final


def write_phase(app_dir, phasedef, original_prompt, rounds, complete):
    key, folder, fname, _title, _purpose = phasedef
    phase_dir = os.path.join(app_dir, folder)
    os.makedirs(phase_dir, exist_ok=True)
    md, final = render_phase_md(app_dir.rsplit(os.sep, 1)[-1], phasedef,
                                original_prompt, rounds, complete)
    with open(os.path.join(phase_dir, fname), "w", encoding="utf-8") as fh:
        fh.write(md)
    return final


def write_prompt(app_dir, prompt):
    p = os.path.join(app_dir, "initial_prompt")
    os.makedirs(p, exist_ok=True)
    with open(os.path.join(p, "initial_prompt.md"), "w", encoding="utf-8") as fh:
        fh.write(prompt)


def save_state(app_dir, state):
    state["last_processed"] = now_str()
    with open(os.path.join(app_dir, "agent_state.json"), "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2)


def base_state(prompt):
    return {
        "current_phase": None, "current_round": 0, "next_agent": None,
        "prompt_hash": sha256_text(prompt), "completed_phases": [],
        "phase_outputs": {}, "last_processed": None, "consensus_status": {},
        "vote_results": {}, "done": False, "error": None,
    }


def seed_complete(root, app, prompt, rounds=2):
    app_dir = os.path.join(root, app)
    write_prompt(app_dir, prompt)
    state = base_state(prompt)
    for phasedef in PHASES:
        key = phasedef[0]
        final = write_phase(app_dir, phasedef, prompt, rounds, complete=True)
        state["completed_phases"].append(key)
        state["phase_outputs"][key] = final
        state["consensus_status"][key] = True
    state["done"] = True
    save_state(app_dir, state)
    print("Seeded COMPLETE project: %s (%d phases)" % (app, len(PHASES)))


def seed_in_progress(root, app, prompt, done_count=2, rounds=2):
    app_dir = os.path.join(root, app)
    write_prompt(app_dir, prompt)
    state = base_state(prompt)
    for phasedef in PHASES[:done_count]:
        key = phasedef[0]
        final = write_phase(app_dir, phasedef, prompt, rounds, complete=True)
        state["completed_phases"].append(key)
        state["phase_outputs"][key] = final
        state["consensus_status"][key] = True
    # The current phase: one round written, no final output yet, not complete.
    cur = PHASES[done_count]
    write_phase(app_dir, cur, prompt, rounds=1, complete=False)
    state["current_phase"] = cur[0]
    state["current_round"] = 1
    state["next_agent"] = "claude"
    save_state(app_dir, state)
    print("Seeded IN-PROGRESS project: %s (current=%s)" % (app, cur[0]))


def seed_aborted(root, app, prompt, done_count=4, rounds=2):
    app_dir = os.path.join(root, app)
    write_prompt(app_dir, prompt)
    state = base_state(prompt)
    for phasedef in PHASES[:done_count]:
        key = phasedef[0]
        final = write_phase(app_dir, phasedef, prompt, rounds, complete=True)
        state["completed_phases"].append(key)
        state["phase_outputs"][key] = final
        state["consensus_status"][key] = True
    state["current_phase"] = PHASES[done_count][0]
    state["error"] = ("Codex returned empty output — refusing to fabricate a "
                      "response. See logs/.")
    save_state(app_dir, state)
    print("Seeded ABORTED project: %s (error on %s)" % (app, PHASES[done_count][0]))


def main():
    ap = argparse.ArgumentParser(description="Seed sample + demo orchestrator projects")
    ap.add_argument("--root", default=ROOT_DEFAULT)
    args = ap.parse_args()
    root = args.root
    os.makedirs(root, exist_ok=True)

    # 1) The real README seed — no state, ready for a real run once CLIs exist.
    write_prompt(
        os.path.join(root, "sample_app"),
        "# Sample App\n"
        "Build a tiny CLI to-do manager with add/list/done commands and JSON "
        "storage.\nAudience: solo developers. Keep scope minimal.\n",
    )
    print("Seeded NEW project: sample_app (prompt only, no state)")

    # 2) Demo projects covering every GUI state.
    seed_in_progress(
        root, "taskquill",
        "# TaskQuill\n"
        "A keyboard-first task manager for indie developers. Capture tasks in "
        "under one second, group by project, and sync to a local JSON file.\n"
        "Audience: solo devs who live in the terminal. Keep it offline-first.\n",
    )
    seed_complete(
        root, "ledgerly",
        "# Ledgerly\n"
        "A minimal personal-finance ledger CLI: import CSV bank exports, "
        "categorize transactions, and report monthly spend by category.\n"
        "Audience: privacy-conscious individuals. No cloud, no accounts.\n",
    )
    seed_aborted(
        root, "notes_sync",
        "# Notes Sync\n"
        "Two-way sync between local Markdown notes and a remote store, with "
        "conflict resolution and a dry-run mode.\nAudience: writers and devs.\n",
    )
    print("\nDone. Open the GUI to browse these projects.")


if __name__ == "__main__":
    main()
