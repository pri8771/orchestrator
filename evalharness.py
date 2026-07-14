#!/usr/bin/env python3
"""
Eval harness (task #12): quality tuning as measurement, not vibes.

score_project() reads the artifacts every run already persists — verify
results, adherence.json, visual_qa.json, ui_crawl.json, design_lint.json,
events.jsonl, rating.json — and produces one comparable scorecard per
project. report() renders a golden-set table; run it before/after any config
change (rounds, models, rules) to see whether quality actually moved.

The golden prompt set lives in evals/golden/*.md (one initial_prompt each);
`--eval-report` scores existing projects, it never launches builds itself —
launching stays an explicit, quota-spending human decision.

Composite score (0-100): compile 30 + adherence 25 + visual 15 + flows 15 +
crawl hygiene 10 + lint 5, with the human rating shown alongside (never
folded in — the point is comparing the machine's view against yours).
"""

import json
import os

import fleetlearn as fllib
import verify as verifylib


def _load(app_dir, rel):
    try:
        with open(os.path.join(app_dir, rel), encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def _events_span(app_dir):
    """(wall_seconds, turns) from events.jsonl; (0, 0) when unavailable."""
    first = last = None
    turns = 0
    try:
        with open(os.path.join(app_dir, "events.jsonl"),
                  encoding="utf-8") as fh:
            for line in fh:
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue
                ts = ev.get("ts")
                if ts:
                    first = first or ts
                    last = ts
                if ev.get("kind") == "turn_completed":
                    turns += 1
    except OSError:
        pass
    if not first or not last:
        return 0, turns
    try:
        import datetime as _dt
        f = _dt.datetime.fromisoformat(str(first))
        l = _dt.datetime.fromisoformat(str(last))
        return max(0, int((l - f).total_seconds())), turns
    except ValueError:
        return 0, turns


def score_project(app_dir):
    """One comparable scorecard dict for a finished (or attempted) project."""
    st = _load(app_dir, "agent_state.json") or {}
    verify_ok = False
    verify_ran = False
    # verify.py's canonical accessor, not a hand-rolled re-read of the file:
    # persist_verify_result() writes a bare JSON LIST of records, not a dict
    # with an "attempts"/"results" key, so parse it the same way verify.py
    # itself does or "ran"/"ok" silently stay False for every real project.
    records = verifylib.load_verify_results(app_dir)
    if records:
        latest = records[-1]
        verify_ran = bool(latest.get("ran"))
        verify_ok = bool(latest.get("ok"))
    adh = _load(app_dir, "docs/adherence.json")
    vqa = _load(app_dir, "docs/visual_qa.json")
    crawl = _load(app_dir, "docs/ui_crawl.json")
    lint = _load(app_dir, "docs/design_lint.json")
    rating = fllib.load_rating(app_dir)

    crawl_ran = isinstance(crawl, dict)
    lint_ran = isinstance(lint, dict)
    flows = (crawl or {}).get("flows") or []
    flows_passed = sum(1 for f in flows if f.get("passed"))
    dead = len((crawl or {}).get("dead_taps") or [])
    crashes = len((crawl or {}).get("crashes") or [])
    lint_errors = len((lint or {}).get("errors") or [])

    def pts(cond, full):
        return full if cond else 0

    adh_score = (adh or {}).get("score")
    vqa_score = (vqa or {}).get("score")
    composite = 0
    composite += pts(verify_ok, 30)
    composite += int(round((adh_score or 0) * 0.25)) \
        if isinstance(adh_score, (int, float)) else 0
    composite += int(round((vqa_score or 0) * 0.15)) \
        if isinstance(vqa_score, (int, float)) else 0
    composite += int(round(15.0 * flows_passed / len(flows))) if flows else 0
    # Missing evidence must never score as "zero defects" — only award crawl
    # hygiene / lint points when that gate's report actually exists.
    composite += pts(crawl_ran and crashes == 0 and dead <= 1, 10)
    composite += pts(lint_ran and lint_errors == 0, 5)

    wall, turns = _events_span(app_dir)
    return {
        "app": os.path.basename(app_dir.rstrip(os.sep)),
        "done": bool(st.get("done")),
        "error": str(st.get("error") or "")[:80],
        "compile_ok": verify_ok,
        "compile_ran": verify_ran,
        "adherence": adh_score if adh_score is not None else "",
        "visual": vqa_score if vqa_score is not None else "",
        "flows": "%d/%d" % (flows_passed, len(flows)) if flows else "",
        "crashes": crashes if crawl_ran else "n/a",
        "dead_buttons": dead if crawl_ran else "n/a",
        "lint_errors": lint_errors if lint_ran else "n/a",
        "repairs": int(st.get("release_gate_repairs") or 0),
        "wall_minutes": round(wall / 60.0, 1) if wall else "",
        "turns": turns,
        "composite": composite,
        "human_rating": rating["verdict"] if rating else "",
    }


def report(root, apps=None):
    """Markdown table over the given app slugs (default: every project)."""
    if not apps:
        try:
            apps = [n for n in sorted(os.listdir(root))
                    if not n.startswith(".")
                    and os.path.exists(os.path.join(
                        root, n, "initial_prompt", "initial_prompt.md"))]
        except OSError:
            apps = []
    rows = [score_project(os.path.join(root, a)) for a in apps]
    cols = ("app", "composite", "compile_ok", "adherence", "visual", "flows",
            "crashes", "dead_buttons", "lint_errors", "repairs",
            "wall_minutes", "turns", "human_rating")
    lines = ["# Eval report", "",
             "| " + " | ".join(cols) + " |",
             "|" + "---|" * len(cols)]
    for r in rows:
        lines.append("| " + " | ".join(str(r.get(c, "")) for c in cols) + " |")
    scored = [r["composite"] for r in rows if r.get("done")]
    if scored:
        lines += ["", "Mean composite over %d finished project(s): %.1f"
                  % (len(scored), sum(scored) / len(scored))]
    return "\n".join(lines) + "\n"
