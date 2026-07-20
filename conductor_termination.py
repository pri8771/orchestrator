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
import copy
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
    raises. See load_goal_manifest_ex for the status this collapses (callers
    that must not let a CORRUPT file silently drop active budgets use that
    instead — 'budgets still enforced' is a lie for this simple form alone,
    since it has nowhere to fall back to)."""
    return load_goal_manifest_ex(root, on_warn)[0]


def load_goal_manifest_ex(root, on_warn=None):
    """Like load_goal_manifest, but also returns a status: 'ok' (parsed and
    normalized), 'missing' (no file — budgets were never configured, safe to
    report as none), or 'corrupt' (a file exists but failed to parse/validate
    — the caller must NOT treat this the same as 'missing': it should fall
    back to the LAST KNOWN GOOD budgets rather than silently going uncapped,
    since a corrupt manifest could be a torn write over a previously-valid
    one, not evidence budgets were never wanted)."""
    path = os.path.join(root, GOAL_MANIFEST_FILENAME)
    if not os.path.exists(path):
        return dict(SAFE_DEFAULT_MANIFEST), "missing"
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as exc:
        _warn(on_warn, "goal_manifest.json unreadable (%s) — goal/quiescence "
                       "termination DISABLED for this poll" % exc)
        return dict(SAFE_DEFAULT_MANIFEST), "corrupt"
    if not isinstance(data, dict):
        _warn(on_warn, "goal_manifest.json is not an object — goal/quiescence "
                       "termination DISABLED for this poll")
        return dict(SAFE_DEFAULT_MANIFEST), "corrupt"
    return normalize_manifest(data, on_warn), "ok"


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
        # deep-copied: a caller (e.g. pipeline_presets.validate_preset, whose
        # own docstring promises "mutates nothing") must not hand back a
        # structure that ALIASES the input — a caller mutating their own
        # dict after normalization would otherwise retroactively corrupt the
        # already-returned manifest (the exact "editing a live preset must
        # not mutate the running Conductor" guarantee 7.11 depends on).
        out["budgets"] = copy.deepcopy(data["budgets"])
    if isinstance(data.get("stall"), dict):
        out["stall"] = copy.deepcopy(data["stall"])
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


def _scan_finals(app_dir, on_warn=None):
    """One pass over the live final artifacts, classifying each as GENUINE or
    OSCILLATING. A final oscillates when its content_hash re-derives an
    ancestor's — exactly guard_route's CONVERGED condition, evaluated over the
    whole ancestry (the codebase never tombstones a superseded artifact, so
    'progress' can't be 'the final set shrank'; churn is a lineage whose new
    head reproduces earlier content). Returns (genuine_ids, oscillating_ids).
    ONE definition shared by quiescence (genuine finals) and stall (oscillation)
    so the two can never drift apart."""
    import artifacts as artlib
    finals = artlib.list_artifacts(app_dir, status="final",
                                   on_error=lambda m: _warn(on_warn, m))
    if not finals:
        return set(), set()
    index = artlib.lineage_index(app_dir, on_error=lambda m: _warn(on_warn, m))
    by_id = index.get("by_id", {}) if isinstance(index, dict) else {}
    genuine, oscillating = set(), set()
    for m in finals:
        aid = m.get("id")
        if not aid:
            continue
        chash = m.get("content_hash")
        anc = {by_id.get(a, {}).get("content_hash")
               for a in (m.get("lineage") or [])}
        if chash is not None and chash in anc:
            oscillating.add(str(aid))   # re-derives an ancestor — churn
        else:
            genuine.add(str(aid))
    return genuine, oscillating


def genuine_final_ids(app_dir, on_warn=None):
    """Ids of the session's genuine live finals (oscillation excluded — see
    _scan_finals). Progress for quiescence = a NEW id in this set."""
    return _scan_finals(app_dir, on_warn)[0]


def has_oscillation(app_dir, on_warn=None):
    """True when any live final re-derives an ancestor's content — the global
    supersedes-oscillation stall signal (§ the card: oscillation is stall, not
    progress). Shares _scan_finals with quiescence so the two agree by
    construction."""
    return bool(_scan_finals(app_dir, on_warn)[1])


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
# Budgets (7.5b) — hard caps halt routing; per-provider daily quota defers it
# --------------------------------------------------------------------------- #
# A conductor "turn" = one route it actually minted. Bounds how much
# autonomous ACTION the conductor takes, read from its OWN ledger — independent
# of agent-side token accounting (which the spend cap covers). Includes
# route_recovered: a restart-recovered mint is a REAL turn that already
# happened (the session dir exists) — counting only route_approved would let a
# host that crashes shortly after each mint take unbounded turns while the
# cap reports zero used.
_TURN_DECISIONS = ("route_approved", "route_recovered")


def count_turns(ledger):
    return sum(1 for r in ledger
               if isinstance(r, dict) and r.get("decision") in _TURN_DECISIONS)


def wall_clock_elapsed(ledger, now):
    """Seconds since the conductor's first-ever ledger entry — the run's age
    across restarts (the ledger persists). 0.0 when there is no history."""
    for r in ledger:
        if isinstance(r, dict) and isinstance(r.get("ts"), (int, float)):
            return max(0.0, now - r["ts"])
    return 0.0


def provider_spend_usd(records):
    """{provider: usd} summed from REAL costs.py micro-USD (R2: real spend, not
    estimates or char counts). Records without a provider/cost are ignored —
    KNOWN GAP: costs.py never guesses a price, so an unpriced (uncatalogued)
    model's turns are invisible to the spend cap entirely; the turns/wall_clock
    caps are the only backstop for such a provider."""
    micro = {}
    for r in records:
        if not isinstance(r, dict):
            continue
        p, c = r.get("provider"), r.get("cost_micro_usd")
        if isinstance(p, str) and p and isinstance(c, int) and not \
                isinstance(c, bool):
            micro[p] = micro.get(p, 0) + c
    return {p: v / 1_000_000 for p, v in micro.items()}


def provider_requests_today(records, today):
    """{provider: count} of requests dated `today` ('YYYY-MM-DD' prefix of the
    record ts) — the substrate the daily quota reads (one record == one
    request; e.g. Gemini free tier ~20/day)."""
    out = {}
    for r in records:
        if not isinstance(r, dict):
            continue
        p, ts = r.get("provider"), r.get("ts")
        if isinstance(p, str) and p and isinstance(ts, str) and \
                ts[:10] == today:
            out[p] = out.get(p, 0) + 1
    return out


def _pos_num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and v >= 0


def budget_check(budgets, ledger, records, now, today):
    """Evaluate HARD caps (turns, wall_clock_s, per-provider spend USD) plus the
    soft per-provider daily request quota. A hard cap hit -> {exhausted: True}
    (the conductor halts ALL routing). Providers at/over their daily quota ->
    over_quota set (routes to them DEFER, not halt). Deny-safe: a missing or
    ill-typed cap is simply not enforced — never a fabricated limit."""
    result = {"exhausted": False, "reason": None, "evidence": {},
              "over_quota": set()}
    if not isinstance(budgets, dict):
        return result
    ev = result["evidence"]
    tcap = budgets.get("turns")
    if _pos_num(tcap) and isinstance(tcap, int):
        used = count_turns(ledger)
        ev["turns"] = {"used": used, "cap": tcap}
        if used >= tcap:
            return {**result, "exhausted": True, "reason": "turns_exhausted"}
    wcap = budgets.get("wall_clock_s")
    if _pos_num(wcap):
        used = wall_clock_elapsed(ledger, now)
        ev["wall_clock_s"] = {"used": round(used, 1), "cap": wcap}
        if used >= wcap:
            return {**result, "exhausted": True,
                    "reason": "wall_clock_exhausted"}
    per = budgets.get("per_provider")
    if isinstance(per, dict):
        spend = provider_spend_usd(records)
        reqs = provider_requests_today(records, today)
        over = set()
        for prov, caps in per.items():
            if not isinstance(caps, dict):
                continue
            scap = caps.get("spend")
            if _pos_num(scap):
                used = spend.get(prov, 0.0)
                ev.setdefault("spend_usd", {})[prov] = {
                    "used": round(used, 6), "cap": scap}
                if used >= scap:
                    return {**result, "exhausted": True,
                            "reason": "spend_exhausted", "provider": prov}
            qcap = caps.get("requests_per_day")
            if _pos_num(qcap) and isinstance(qcap, int) \
                    and reqs.get(prov, 0) >= qcap:
                over.add(prov)
        if over:
            ev["over_quota"] = sorted(over)
        result["over_quota"] = over
    return result


# --------------------------------------------------------------------------- #
# Stall (7.5b) — vote_undecided x2 or supersedes oscillation -> escalate
# --------------------------------------------------------------------------- #
def stall_check(app_dir, sstate, stall_cfg, on_warn=None):
    """A session is stalled when it closed >= limit phases as 'vote_undecided'
    (a forced vote that never decided — orchestrator records it in
    agent_state.phase_resolutions) OR its artifact lineage is oscillating
    (has_oscillation). A stalled session escalates to a human: routing into it
    stops; other sessions keep going. Default vote limit is 2.

    KNOWN LIMIT: phase_resolutions is keyed by phase name, so a phase key
    force-voted undecided more than once (e.g. re-executed on retry) still
    counts once, not per-occurrence — a session oscillating within one
    repeatedly-retried phase relies on quiescence (if configured) rather than
    this vote path to eventually stall out."""
    limit = 2
    if isinstance(stall_cfg, dict):
        v = stall_cfg.get("vote_undecided_limit")
        if isinstance(v, int) and not isinstance(v, bool) and v > 0:
            limit = v
    pr = sstate.get("phase_resolutions") if isinstance(sstate, dict) else None
    undecided = sum(1 for x in pr.values() if x == "vote_undecided") \
        if isinstance(pr, dict) else 0
    if undecided >= limit:
        return {"stalled": True, "reason": "vote_undecided",
                "evidence": {"undecided": undecided, "limit": limit},
                "note": "escalated to human — routing into this session stops"}
    if has_oscillation(app_dir, on_warn):
        return {"stalled": True, "reason": "supersedes_oscillation",
                "evidence": {"detail": "a lineage head re-derives an ancestor"},
                "note": "escalated to human — routing into this session stops"}
    return {"stalled": False, "reason": None, "evidence": {}}


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
