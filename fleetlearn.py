#!/usr/bin/env python3
"""
Fleet learning (tasks #5/#6/#7/#16): the system's memory across projects.

Four cooperating pieces, all file-based and best-effort:

* INCIDENTS — every gate failure is recorded to <app>/docs/incidents.jsonl
  with a BLAME ATTRIBUTION: the upstream phase(s) whose output should have
  prevented it. This is what lets non-build phases (research, specs, design)
  learn from outcomes they can't directly observe.

* RATINGS — <app>/rating.json ({"verdict": "good"|"bad", "note": "..."}),
  written by the GUI's Rate menu. presort() ranks unrated projects by the
  implicit failure signals already on disk so a human confirms labels
  instead of auditing the whole fleet cold.

* ANTI-PATTERN LEDGER — build_ledger() clusters incidents + bad ratings into
  knowledge/anti_patterns.md ("the mistakes this fleet keeps making"). The
  existing knowledge splicer scores and injects it into build/design phases
  like any other cheatsheet, so every recorded failure teaches future runs.

* EXEMPLARS + SCORECARDS — export_exemplars() snapshots a rated-good
  project's phase outputs into knowledge/exemplars/<phase>/ for few-shot
  splicing; scorecards() aggregates per-phase incident counts + forced votes
  fleet-wide so investment (rubrics, models, effort) goes where the pipeline
  is actually weak.
"""

import json
import os
import time

# Which upstream phases share the blame for each gate's failures. The build
# phase always co-owns (it wrote the code); the interesting entries are the
# DISCUSSION phases whose output permitted the failure.
ATTRIBUTION = {
    "release_gate": ["tech_specs", "build_coordination"],
    "adherence": ["app_features", "prompt_contract", "build_coordination"],
    "visual_qa": ["design_handoff", "build_coordination"],
    "ui_crawl_crash": ["build_coordination"],
    "ui_crawl_flow": ["task_assignments", "build_coordination"],
    "design_lint": ["design_handoff", "build_coordination"],
    "change_request": ["app_features", "product_research"],
    "research_unsupported": ["product_research"],
}

# Phases whose outputs make good few-shot exemplars.
EXEMPLAR_PHASES = ("product_research", "app_features", "design_handoff",
                   "tech_specs", "task_assignments")


def record_incident(app_dir, gate, detail, phases=None, repro=None):
    """Append one incident to <app>/docs/incidents.jsonl. Never raises."""
    entry = {
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "gate": str(gate),
        "detail": str(detail)[:500],
        "blamed_phases": list(phases if phases is not None
                              else ATTRIBUTION.get(gate, [])),
    }
    if repro:
        entry["repro"] = repro
    try:
        os.makedirs(os.path.join(app_dir, "docs"), exist_ok=True)
        with open(os.path.join(app_dir, "docs", "incidents.jsonl"), "a",
                  encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")
    except OSError:
        pass
    return entry


def load_incidents(app_dir):
    out = []
    try:
        with open(os.path.join(app_dir, "docs", "incidents.jsonl"),
                  encoding="utf-8") as fh:
            for line in fh:
                try:
                    out.append(json.loads(line))
                except ValueError:
                    continue
    except OSError:
        pass
    return out


def load_rating(app_dir):
    try:
        with open(os.path.join(app_dir, "rating.json"), encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict) and data.get("verdict") in ("good", "bad"):
            return data
    except (OSError, ValueError):
        pass
    return None


def save_rating(app_dir, verdict, note=""):
    if verdict not in ("good", "bad", None):
        return False
    path = os.path.join(app_dir, "rating.json")
    try:
        if verdict is None:
            if os.path.exists(path):
                os.remove(path)
            return True
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"verdict": verdict, "note": str(note or "")[:500],
                       "ts": time.strftime("%Y-%m-%d %H:%M:%S")}, fh, indent=2)
        return True
    except OSError:
        return False


def _implicit_score(app_dir):
    """Failure pressure from artifacts already on disk (higher = worse).
    Cheap: reads only small JSON files."""
    score, signals = 0, []

    def _load(rel):
        try:
            with open(os.path.join(app_dir, rel), encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, ValueError):
            return None

    st = _load("agent_state.json") or {}
    if st.get("error"):
        score += 2
        signals.append("aborted: %s" % str(st["error"])[:60])
    n = int(st.get("release_gate_repairs") or 0)
    if n:
        score += n
        signals.append("%d release-gate repair(s)" % n)
    fb = st.get("fallback_counts") or {}
    if sum(fb.values()) >= 3:
        score += 1
        signals.append("%d fallback rescues" % sum(fb.values()))
    adh = _load("docs/adherence.json")
    if adh and str(adh.get("verdict")).upper() == "FAIL":
        score += 2
        signals.append("adherence FAIL")
    vqa = _load("docs/visual_qa.json")
    if vqa and str(vqa.get("verdict")).upper() == "FAIL":
        score += 2
        signals.append("visual QA FAIL")
    crawl = _load("docs/ui_crawl.json")
    if crawl:
        if crawl.get("crashes"):
            score += 2
            signals.append("crash in UI crawl")
        if len(crawl.get("dead_taps") or []) >= 2:
            score += 1
            signals.append("%d dead buttons" % len(crawl["dead_taps"]))
    lint = _load("docs/design_lint.json")
    if lint and lint.get("errors"):
        score += 1
        signals.append("%d lint error(s)" % len(lint["errors"]))
    try:
        with open(os.path.join(app_dir, "initial_prompt",
                               "initial_prompt.md"), encoding="utf-8") as fh:
            crs = fh.read().count("## Change requested")
        if crs:
            score += crs
            signals.append("%d change request(s)" % crs)
    except OSError:
        pass
    return score, signals


def presort(root):
    """Rank every project for human label confirmation. Returns a list of
    {app, rating, proposed, score, signals}, worst first."""
    rows = []
    try:
        names = sorted(os.listdir(root))
    except OSError:
        return rows
    for name in names:
        app_dir = os.path.join(root, name)
        if name.startswith(".") or not os.path.isdir(app_dir):
            continue
        if not os.path.exists(os.path.join(app_dir, "initial_prompt",
                                           "initial_prompt.md")):
            continue
        score, signals = _implicit_score(app_dir)
        rating = load_rating(app_dir)
        rows.append({
            "app": name,
            "rating": rating["verdict"] if rating else "",
            "proposed": "bad" if score >= 3 else ("good" if score == 0 else ""),
            "score": score,
            "signals": signals,
        })
    rows.sort(key=lambda r: -r["score"])
    return rows


def scorecards(root):
    """Fleet-wide per-phase weakness map: incidents attributed to each phase
    plus forced-vote counts (a phase that keeps needing the vote never truly
    converges). Returns {phase: {incidents, forced_votes, examples}}."""
    cards = {}

    def card(phase):
        return cards.setdefault(phase, {"incidents": 0, "forced_votes": 0,
                                        "examples": []})
    try:
        names = sorted(os.listdir(root))
    except OSError:
        return cards
    for name in names:
        app_dir = os.path.join(root, name)
        if name.startswith(".") or not os.path.isdir(app_dir):
            continue
        for inc in load_incidents(app_dir):
            for phase in inc.get("blamed_phases") or []:
                c = card(phase)
                c["incidents"] += 1
                if len(c["examples"]) < 5:
                    c["examples"].append("%s: %s (%s)"
                                         % (name, inc.get("detail", "")[:90],
                                            inc.get("gate", "")))
        try:
            with open(os.path.join(app_dir, "agent_state.json"),
                      encoding="utf-8") as fh:
                st = json.load(fh)
            for phase, vote in (st.get("vote_results") or {}).items():
                if isinstance(vote, dict) and vote:
                    card(phase)["forced_votes"] += 1
        except (OSError, ValueError):
            continue
    return cards


def _summarize_with_local_model(clusters, model, timeout=120):
    """Optional polish: one local-model call to phrase the ledger tersely.
    Returns markdown or "" (deterministic fallback text is always written)."""
    try:
        import urllib.request
        prompt = (
            "Rewrite these recurring app-factory failure clusters as a terse "
            "'DO NOT repeat these mistakes' list for coding/design agents. "
            "One bullet per cluster, imperative, concrete, max 25 words each. "
            "Output ONLY the bullets.\n\n" +
            "\n".join("- [%s x%d] %s" % (c["gate"], c["count"],
                                         "; ".join(c["examples"][:3]))
                      for c in clusters))
        body = json.dumps({"model": model, "prompt": prompt,
                           "stream": False}).encode("utf-8")
        req = urllib.request.Request(
            "http://127.0.0.1:11434/api/generate", data=body,
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8")).get("response", "")
    except Exception:  # noqa: BLE001 - purely cosmetic step
        return ""


def read_ledger(here, max_chars=2000):
    """The current anti-pattern ledger text, for prompt injection — the READ
    half of the fleet-learning loop (build_ledger writes it; without this,
    "every recorded failure teaches future runs" was write-only). Applies the
    same ORCH_LEDGER_DIR redirection as build_ledger so tests never read the
    repo's own tracked file. Empty string when absent/unreadable — injection
    is best-effort and must never block a phase."""
    override = os.environ.get("ORCH_LEDGER_DIR")
    if override and os.path.abspath(here) == os.path.dirname(os.path.abspath(__file__)):
        here = override
    path = os.path.join(here, "knowledge", "anti_patterns.md")
    try:
        with open(path, encoding="utf-8") as fh:
            text = fh.read().strip()
    except OSError:
        return ""
    return text[:max_chars] if text else ""


def build_ledger(root, here, model=""):
    """Cluster incidents + bad ratings fleet-wide and write
    knowledge/anti_patterns.md. Returns (path, cluster_count).

    ORCH_LEDGER_DIR redirects a write aimed at THIS engine checkout (the
    directory holding this file) somewhere else. The test suite exports it
    (tests/__init__.py) so no test — in-process or via a spawned engine
    subprocess — can ever rewrite the repo's own tracked ledger file. A
    caller passing any other ``here`` (tests use tmp dirs) is untouched."""
    override = os.environ.get("ORCH_LEDGER_DIR")
    if override and os.path.abspath(here) == os.path.dirname(os.path.abspath(__file__)):
        here = override
    counts = {}
    try:
        names = sorted(os.listdir(root))
    except OSError:
        names = []
    for name in names:
        app_dir = os.path.join(root, name)
        if name.startswith(".") or not os.path.isdir(app_dir):
            continue
        for inc in load_incidents(app_dir):
            gate = inc.get("gate", "other")
            c = counts.setdefault(gate, {"gate": gate, "count": 0,
                                         "examples": []})
            c["count"] += 1
            if len(c["examples"]) < 6:
                c["examples"].append(inc.get("detail", "")[:110])
        rating = load_rating(app_dir)
        if rating and rating["verdict"] == "bad" and rating.get("note"):
            c = counts.setdefault("human_rating", {"gate": "human_rating",
                                                   "count": 0, "examples": []})
            c["count"] += 1
            if len(c["examples"]) < 6:
                c["examples"].append("%s: %s" % (name, rating["note"][:110]))
    clusters = sorted(counts.values(), key=lambda c: -c["count"])
    if not clusters:
        return "", 0
    lines = [
        "# Fleet anti-patterns (auto-generated — do not repeat these)",
        "",
        "Recurring failures recorded across this factory's runs: build "
        "verification, prompt adherence, visual QA, UI crawl, design lint, "
        "and human ratings. Treat every bullet as a design/build rule.",
        "",
    ]
    polished = _summarize_with_local_model(clusters, model) if model else ""
    if polished.strip():
        lines.append(polished.strip())
        lines.append("")
    lines.append("## Raw clusters")
    for c in clusters:
        lines.append("")
        lines.append("### %s — %d incident(s)" % (c["gate"], c["count"]))
        for e in c["examples"]:
            lines.append("- %s" % e)
    path = os.path.join(here, "knowledge", "anti_patterns.md")
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
    except OSError:
        return "", len(clusters)
    return path, len(clusters)


def export_exemplars(app_dir, here, app_name=""):
    """Snapshot a rated-good project's phase outputs (+ DesignSystem.swift)
    into knowledge/exemplars/<phase>/ for few-shot splicing. Returns the list
    of files written."""
    name = app_name or os.path.basename(app_dir.rstrip(os.sep))
    written = []
    try:
        with open(os.path.join(app_dir, "agent_state.json"),
                  encoding="utf-8") as fh:
            outputs = (json.load(fh).get("phase_outputs") or {})
    except (OSError, ValueError):
        return written
    for key in EXEMPLAR_PHASES:
        out = outputs.get(key)
        if not out or len(str(out)) < 200:
            continue
        dest_dir = os.path.join(here, "knowledge", "exemplars", key)
        try:
            os.makedirs(dest_dir, exist_ok=True)
            dest = os.path.join(dest_dir, "%s.md" % name)
            with open(dest, "w", encoding="utf-8") as fh:
                fh.write("# Exemplar %s output (from rated-good project "
                         "'%s')\n\n%s\n" % (key, name, str(out)[:12000]))
            written.append(dest)
        except OSError:
            continue
    # The realized design system is the strongest design exemplar there is.
    for dp, dns, fns in os.walk(os.path.join(app_dir, "app_build")):
        dns[:] = [d for d in dns if d not in (".git", ".build")]
        for fn in fns:
            if fn.lower().startswith("designsystem") and fn.endswith(".swift"):
                try:
                    dest_dir = os.path.join(here, "knowledge", "exemplars",
                                            "design_handoff")
                    os.makedirs(dest_dir, exist_ok=True)
                    with open(os.path.join(dp, fn), encoding="utf-8") as src:
                        code = src.read()[:12000]
                    dest = os.path.join(dest_dir, "%s-DesignSystem.swift.md"
                                        % name)
                    with open(dest, "w", encoding="utf-8") as fh:
                        fh.write("# DesignSystem.swift from rated-good "
                                 "project '%s'\n\n```swift\n%s\n```\n"
                                 % (name, code))
                    written.append(dest)
                except OSError:
                    pass
                return written
    return written


def render_fleet_report(root, here):
    """One human-readable markdown: presort table + phase scorecards."""
    rows = presort(root)
    cards = scorecards(root)
    lines = ["# Fleet report", "", "## Label confirmation queue "
             "(worst first — confirm or flip the proposal)", ""]
    lines.append("| project | rated | proposed | score | signals |")
    lines.append("|---|---|---|---|---|")
    for r in rows[:40]:
        lines.append("| %s | %s | %s | %d | %s |"
                     % (r["app"], r["rating"] or "—", r["proposed"] or "—",
                        r["score"], "; ".join(r["signals"])[:100]))
    lines += ["", "## Phase scorecards (where the pipeline is weak)", ""]
    lines.append("| phase | incidents | forced votes |")
    lines.append("|---|---|---|")
    for phase, c in sorted(cards.items(), key=lambda kv: -kv[1]["incidents"]):
        lines.append("| %s | %d | %d |"
                     % (phase, c["incidents"], c["forced_votes"]))
    for phase, c in sorted(cards.items(), key=lambda kv: -kv[1]["incidents"]):
        if c["examples"]:
            lines += ["", "### %s" % phase]
            lines += ["- %s" % e for e in c["examples"]]
    return "\n".join(lines) + "\n"
