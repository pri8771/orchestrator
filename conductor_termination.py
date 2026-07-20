"""V3 7.5(a): the conductor's goal + quiescence termination layers — two of
the four-layer stack that decide 'the work is done' vs 'the work has gone
quiet with items still open'. Budgets + stall (7.5b) extend this same seam.

A run must ALWAYS be able to end (§12.1's autonomous analogue: every run
reaches a terminal state in bounded time). This is a pure-decision leaf: it
reads the AUTHORITATIVE artifact store / eval score / DoD grade for a session
and returns a verdict; conductor.py owns the ledger line, the report file, and
the routing skip. Every terminal outcome is honest (§13.2): a
converged-with-open-items report enumerates what is NOT done, and a
partially-met goal is NEVER reported as complete (§23 verify-don't-self-report:
the predicate re-checks artifacts/DoD/eval, never a session's claim of done).

Deliberate deviation (documented): termination reports are written to the
conductor-owned <root>/.conductor/reports/, NOT published into a project's
artifact bus. The conductor writing an artifact into a project section would
itself be a capability-gated workspace write (7.4); its terminal reports are
its own record, consumed by 7.8 notifications / 7.10 Mission Control off the
ledger + these report files.

Drift handled (verified against the tree, not the card's stale citations):
- costs.py has no per-provider daily request counter and no DoD-met boolean;
  those feed 7.5b / are approximated here (DoD reads docs/adherence.json's
  graded verdict — the only DoD signal that exists).
- The goal predicate fires ONLY when the goal names >=1 recognized check AND
  every named check passes; a goal with no recognized checks never fires
  (guards against all([]) == True trivially terminating a run).
"""
import hashlib
import json
import os

CONDUCTOR_DIRNAME = ".conductor"   # local copy: this leaf must not import conductor
GOAL_MANIFEST_FILENAME = "goal_manifest.json"
REPORTS_DIRNAME = "reports"
SCHEMA_VERSION = 1

# The safe default for a missing/corrupt/invalid manifest: NEVER terminate by
# goal (an autonomous run must not stop because it wrongly believes it is done)
# and quiescence disabled. Budgets/stall (7.5b) still apply from their own
# defaults regardless — a broken goal manifest must not mean uncapped running.
SAFE_DEFAULT_MANIFEST = {
    "schema_version": SCHEMA_VERSION,
    "goal": None,               # None => the goal predicate never fires
    "quiescence_cycles": None,  # None => quiescence disabled
    "budgets": None,            # validated/consumed by 7.5b
    "stall": None,              # validated/consumed by 7.5b
}

_GOAL_CHECKS = ("doc_gap_empty", "dod_tier", "eval_threshold")

# The authoritative agent_state fields that signify REAL progress. Deliberately
# EXCLUDES last_processed (a heartbeat save_state bumps on every write, even in
# an unproductive retry loop): quiescence keys off this subset so an
# alive-but-stuck session still goes quiescent (the case quiescence exists for).
_PROGRESS_FIELDS = ("current_phase", "done", "error", "status",
                    "consensus_status")


def _warn(on_warn, msg):
    if on_warn:
        on_warn("conductor termination: %s" % msg)


# --------------------------------------------------------------------------- #
# Manifest loading + validation
# --------------------------------------------------------------------------- #
def load_goal_manifest(root, on_warn=None):
    """Read <root>/goal_manifest.json into a validated manifest, or the safe
    default (+ a visible banner) on any missing/corrupt/invalid file. NEVER
    raises: a broken manifest disables goal-based termination while budgets
    (7.5b) stay enforced — it must never wedge or crash the conductor."""
    path = os.path.join(root, GOAL_MANIFEST_FILENAME)
    if not os.path.exists(path):
        return dict(SAFE_DEFAULT_MANIFEST)
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as exc:
        _warn(on_warn, "goal_manifest.json unreadable (%s) — goal termination "
                       "DISABLED, budgets still enforced" % exc)
        return dict(SAFE_DEFAULT_MANIFEST)
    if not isinstance(data, dict):
        _warn(on_warn, "goal_manifest.json is not an object — goal termination "
                       "DISABLED, budgets still enforced")
        return dict(SAFE_DEFAULT_MANIFEST)
    return normalize_manifest(data, on_warn)


def normalize_manifest(data, on_warn=None):
    """Deny-safe normalization: an invalid goal/quiescence value degrades to
    the never-terminate default (with a banner), never to a looser one."""
    out = dict(SAFE_DEFAULT_MANIFEST)
    goal = data.get("goal")
    if isinstance(goal, dict):
        out["goal"] = _normalize_goal(goal, on_warn) or None
    elif goal is not None:
        _warn(on_warn, "goal must be an object — goal termination disabled")
    qc = data.get("quiescence_cycles")
    if isinstance(qc, bool):          # bool is an int subclass; reject it
        _warn(on_warn, "quiescence_cycles must be a positive int — disabled")
    elif isinstance(qc, int) and qc > 0:
        out["quiescence_cycles"] = qc
    elif qc is not None:
        _warn(on_warn, "quiescence_cycles must be a positive int — disabled")
    if isinstance(data.get("budgets"), dict):
        out["budgets"] = data["budgets"]
    if isinstance(data.get("stall"), dict):
        out["stall"] = data["stall"]
    return out


def _normalize_goal(goal, on_warn=None):
    """Keep only recognized, well-typed sub-checks; warn+drop the rest. An
    empty result means 'no recognized checks' -> the predicate never fires."""
    import completeness as complib
    clean = {}
    if "doc_gap_empty" in goal:
        if goal["doc_gap_empty"] is True:
            clean["doc_gap_empty"] = True
        elif goal["doc_gap_empty"] is not False:
            _warn(on_warn, "goal.doc_gap_empty must be a bool — dropped")
    if "dod_tier" in goal:
        tier = goal["dod_tier"]
        if isinstance(tier, str) and tier in complib.DOD_ORDER:
            clean["dod_tier"] = tier
        else:
            _warn(on_warn, "goal.dod_tier %r not a known tier — dropped"
                  % (tier,))
    if "eval_threshold" in goal:
        thr = goal["eval_threshold"]
        if isinstance(thr, bool) or not isinstance(thr, (int, float)):
            _warn(on_warn, "goal.eval_threshold must be numeric — dropped")
        else:
            clean["eval_threshold"] = thr
    for k in goal:
        if k not in _GOAL_CHECKS:
            _warn(on_warn, "goal has unknown key %r — ignored" % (k,))
    return clean


# --------------------------------------------------------------------------- #
# Goal predicate — three independent sub-checks, each recorded individually
# --------------------------------------------------------------------------- #
def goal_predicate(app_dir, manifest, on_warn=None):
    """Evaluate the goal against the authoritative store/score/grade. Returns
    {met, checks, evidence}. met is True ONLY when the goal names >=1
    recognized check AND every named check passes; a goal with no recognized
    checks never fires (all([]) is True — that must not terminate a run)."""
    goal = (manifest or {}).get("goal")
    checks, evidence = {}, {}
    if not isinstance(goal, dict) or not goal:
        return {"met": False, "checks": checks, "evidence": evidence}
    if "doc_gap_empty" in goal:
        checks["doc_gap_empty"], evidence["doc_gap_empty"] = \
            _check_gap_empty(app_dir, on_warn)
    if "dod_tier" in goal:
        checks["dod_tier"], evidence["dod_tier"] = \
            _check_dod(app_dir, goal["dod_tier"], on_warn)
    if "eval_threshold" in goal:
        checks["eval_threshold"], evidence["eval_threshold"] = \
            _check_eval(app_dir, goal["eval_threshold"], on_warn)
    met = bool(checks) and all(checks.values())
    return {"met": met, "checks": checks, "evidence": evidence}


def _open_gaps(app_dir, on_warn=None):
    """Ids of the session's OPEN documentation gaps: status='final' gap
    artifacts (superseded/converged are a different status, excluded). NOTE:
    a gap is not auto-retired when its slot is later filled, so this is the
    CONSERVATIVE direction — a stale gap keeps the goal from firing, never the
    reverse (a run is never falsely declared done)."""
    import artifacts as artlib
    gaps = artlib.list_artifacts(app_dir, type="gap", status="final",
                                 on_error=lambda m: _warn(on_warn, m))
    return [g.get("id") for g in gaps if g.get("id")]


def _check_gap_empty(app_dir, on_warn):
    """POSITIVE evidence required (§23): 'no open gap artifacts' is proof of
    completeness ONLY if a gap scan actually ran. An artifact store that was
    never created also lists zero gaps — indistinguishable at the query API —
    so a never-run session would otherwise pass vacuously. docs/GAP_REPORT.md
    exists iff write_project_docs rendered the blueprint (i.e. real work
    happened); absent it, the check FAILS rather than falsely passing."""
    if not os.path.exists(os.path.join(app_dir, "docs", "GAP_REPORT.md")):
        return False, {"error": "no_gap_report", "note": "no scan has run"}
    ids = _open_gaps(app_dir, on_warn)
    return (not ids), {"open_gap_count": len(ids), "open_gap_ids": ids[:20]}


def _check_dod(app_dir, tier, on_warn):
    """DoD sub-check. No deterministic per-item DoD checker exists in the repo
    (verified): the only DoD signal is docs/adherence.json's graded verdict
    (the grader is instructed to fold DoD items into its requirement grading).
    Conservative: a missing/failing grade fails the check."""
    path = os.path.join(app_dir, "docs", "adherence.json")
    try:
        with open(path, encoding="utf-8") as fh:
            adh = json.load(fh)
    except (OSError, ValueError):
        return False, {"tier": tier, "error": "no_adherence_grade"}
    if not isinstance(adh, dict):
        return False, {"tier": tier, "error": "malformed_adherence_grade"}
    verdict = str(adh.get("verdict", "")).upper()
    return (verdict == "PASS"), {"tier": tier, "verdict": verdict,
                                 "score": adh.get("score")}


def _check_eval(app_dir, threshold, on_warn):
    """evalharness.score_project(app_dir)['composite'] >= threshold. The eval
    must never crash the conductor: any failure fails the check safely."""
    import evalharness
    try:
        thr = float(threshold)
    except (TypeError, ValueError):
        return False, {"error": "non_numeric_threshold"}
    try:
        score = evalharness.score_project(app_dir)
    except Exception as exc:  # noqa: BLE001 - eval faults must not stop the loop
        _warn(on_warn, "score_project failed (%s) — eval check fails" % exc)
        return False, {"error": str(exc), "threshold": thr}
    if not isinstance(score, dict):
        return False, {"error": "no_score", "threshold": thr}
    # POSITIVE evidence (§23): score_project seeds composite=0 and only awards
    # credit per gate whose evidence file EXISTS. An untouched project scores 0,
    # which would satisfy any threshold <= 0 — a false completion. Require the
    # harness to have actually evaluated a real run (a build ran, or the session
    # reached done) before its score can satisfy the goal.
    if not (score.get("compile_ran") or score.get("done")):
        return False, {"error": "not_evaluated", "threshold": thr}
    composite = score.get("composite")
    if not isinstance(composite, (int, float)) or isinstance(composite, bool):
        return False, {"error": "no_composite", "threshold": thr}
    return (composite >= thr), {"composite": composite, "threshold": thr}


# --------------------------------------------------------------------------- #
# Quiescence — N consecutive cycles with zero NEW non-superseded final artifacts
# --------------------------------------------------------------------------- #
def progress_digest(sstate):
    """A digest of the session's REAL-progress agent_state fields (phase, done,
    error, status, consensus) — excluding the last_processed heartbeat that
    save_state bumps on every write. Quiescence keys off a CHANGE in this so an
    alive-but-stuck session (spinning in a retry loop, heartbeating but never
    advancing a phase) still goes quiescent."""
    if not isinstance(sstate, dict):
        return ""
    payload = {k: sstate.get(k) for k in _PROGRESS_FIELDS}
    blob = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def genuine_final_ids(app_dir, on_warn=None):
    """Ids of the session's GENUINE live final artifacts: status='final',
    MINUS any whose content_hash re-derives an ancestor's (the codebase never
    tombstones a superseded artifact — it stays 'final' — so 'progress' can't
    be 'the final set shrank'; the real oscillation trap is a lineage whose new
    head reproduces earlier content, which is exactly guard_route's CONVERGED
    condition evaluated over the whole ancestry). Such a final is churn, not
    progress, and must not reset the idle counter."""
    import artifacts as artlib
    finals = artlib.list_artifacts(app_dir, status="final",
                                   on_error=lambda m: _warn(on_warn, m))
    if not finals:
        return set()
    index = artlib.lineage_index(app_dir, on_error=lambda m: _warn(on_warn, m))
    by_id = index.get("by_id", {}) if isinstance(index, dict) else {}
    out = set()
    for m in finals:
        aid = m.get("id")
        if not aid:
            continue
        chash = m.get("content_hash")
        anc = {by_id.get(a, {}).get("content_hash")
               for a in (m.get("lineage") or [])}
        if chash is not None and chash in anc:
            continue   # oscillation: content re-derives an ancestor — not new
        out.add(str(aid))
    return out


def quiescence_step(prev, app_dir, limit, prog_digest, on_warn=None):
    """Advance the per-session idle counter. `prev` is the persisted record
    {"finals": [genuine_final_ids], "progress": digest, "idle": N} (or None on
    first sight). The counter resets on real activity: a genuinely NEW genuine
    final artifact (see genuine_final_ids — oscillation excluded) OR a change in
    the session's real-progress digest. It increments only when neither moved.
    `finals` is the CURRENT genuine set (not an ever-growing history), so the
    persisted record stays bounded. Returns (record, converged)."""
    cur = genuine_final_ids(app_dir, on_warn)
    if prev is None:
        # first sight is the baseline, never itself a converged tick
        return {"finals": sorted(cur), "progress": prog_digest,
                "idle": 0}, False
    prev_finals = set(prev.get("finals") or [])
    progressed = prev.get("progress") != prog_digest
    if (cur - prev_finals) or progressed:
        idle = 0
    else:
        idle = int(prev.get("idle") or 0) + 1
    record = {"finals": sorted(cur), "progress": prog_digest, "idle": idle}
    return record, (limit is not None and idle >= limit)


def converged_report(app_dir, goal_verdict, quiescence_record, on_warn=None):
    """The converged-with-open-items payload — what is NOT done (§13.2: never
    fabricate completion). Enumerates the failing goal sub-checks and the open
    documentation gaps so the report is honest about remaining work."""
    unmet = [{"goal_check": name,
              "evidence": (goal_verdict.get("evidence") or {}).get(name)}
             for name, ok in (goal_verdict.get("checks") or {}).items()
             if not ok]
    return {"idle_cycles": quiescence_record.get("idle"),
            "unmet_goal_checks": unmet,
            "open_gaps": _open_gaps(app_dir, on_warn),
            "note": "converged with open items — NOT complete"}


# --------------------------------------------------------------------------- #
# Report file — the conductor-owned durable terminal record
# --------------------------------------------------------------------------- #
def write_report(root, sid, reason, payload):
    """Durably write one termination report to <root>/.conductor/reports/. One
    file per (session, reason); atomic tmp+replace + fsync so a crash never
    leaves a half-written report. Returns the path."""
    rdir = os.path.join(root, CONDUCTOR_DIRNAME, REPORTS_DIRNAME)
    os.makedirs(rdir, exist_ok=True)
    # Injective filename: a readable slug PLUS a hash of the raw sid. The slug
    # alone is ambiguous ('a__b/c' and 'a/b/c' both squash to 'a__b__c', and
    # project names legitimately contain '_'); the hash disambiguates so one
    # session's report can never silently clobber another's.
    slug = sid.replace("/", "-").replace(os.sep, "-")
    digest = hashlib.sha256(sid.encode("utf-8")).hexdigest()[:12]
    path = os.path.join(rdir, "%s__%s.%s.json" % (slug, digest, reason))
    rec = {"schema_version": SCHEMA_VERSION, "session": sid, "reason": reason,
           "payload": payload}
    tmp = "%s.%d.tmp" % (path, os.getpid())
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(rec, fh, ensure_ascii=False, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except OSError:
        try:
            os.remove(tmp)
        except OSError:
            pass
        return None
    return path
