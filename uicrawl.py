#!/usr/bin/env python3
"""
UI crawl gate: after visual QA, drive the BUILT app like a user — tap every
hittable element, walk forward/back through navigation, and execute the
spec's declared user journeys (flows.json) — using one generic XCUITest
runner (uitest-runner/) that targets any app by bundle id. No per-app test
code is ever generated; flows are DATA the runner interprets.

Findings and policy (config runtime.ui_crawl_*):
  * a crash during the crawl        -> FAIL (always; repro path recorded)
  * a declared flow failing         -> FAIL (missing/broken feature)
  * dead taps / back violations     -> WARN by default (noisy detectors;
                                       promote via ui_crawl_fail_on_dead_buttons)

Self-learning: every crash's tap path is appended to <app>/flows.json as a
permanent regression flow — the next run replays the exact edge case that
broke this one. Report + per-screen screenshots persist under
<app>/docs/ui_crawl/ (screens feed the visual-QA panel's coverage).

Contract (mirrors the other gates): every step is best-effort — missing
xcodegen/xcodebuild, no simulator, runner build failure, unparseable report
all degrade to "skip", never to a blocked run.
"""

import json
import os
import shutil
import subprocess

import localmodels as lmlib
import visualqa as vqalib

RUNNER_DIRNAME = "uitest-runner"
_HERE = os.path.dirname(os.path.abspath(__file__))


def _run(cmd, cwd=None, timeout=120, env=None):
    try:
        p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                           timeout=timeout, env=env)
        return p.returncode, p.stdout or "", p.stderr or ""
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, "", str(exc)


def runner_dir(here=None):
    return os.path.join(here or _HERE, RUNNER_DIRNAME)


def tools_available():
    return bool(shutil.which("xcodebuild")) and bool(shutil.which("xcrun")) \
        and bool(shutil.which("xcodegen"))


def _sources_signature(rdir):
    """mtime signature of the runner spec+sources — rebuild only on change."""
    sig = []
    for root, _dns, fns in os.walk(os.path.join(rdir, "Sources")):
        for fn in sorted(fns):
            p = os.path.join(root, fn)
            try:
                sig.append("%s:%d" % (fn, int(os.path.getmtime(p))))
            except OSError:
                pass
    try:
        sig.append("yml:%d" % int(os.path.getmtime(os.path.join(rdir, "project.yml"))))
    except OSError:
        pass
    return "|".join(sig)


def ensure_runner(emit, here=None, build_timeout=600):
    """Generate + build-for-testing the crawler runner (cached by a source
    signature). Returns the .xctestrun path or "" on any failure."""
    rdir = runner_dir(here)
    if not os.path.isdir(rdir):
        emit("UI crawl: runner sources missing (%s) — skipping." % rdir)
        return ""
    dd = os.path.join(rdir, ".dd")
    marker = os.path.join(dd, ".built_sig")
    want = _sources_signature(rdir)
    have = ""
    try:
        with open(marker, encoding="utf-8") as fh:
            have = fh.read().strip()
    except OSError:
        pass
    xctestrun = _find_xctestrun(dd)
    if want == have and xctestrun:
        return xctestrun
    if not os.path.exists(os.path.join(rdir, "CrawlerRunner.xcodeproj")) \
            or want != have:
        code, _o, err = _run(["xcodegen", "generate"], cwd=rdir, timeout=120)
        if code != 0:
            emit("UI crawl: xcodegen failed (%s) — skipping." % err.strip()[:120])
            return ""
    emit("UI crawl: building the crawler runner (one-time, cached)…")
    code, _o, err = _run(
        ["xcodebuild", "build-for-testing",
         "-project", os.path.join(rdir, "CrawlerRunner.xcodeproj"),
         "-scheme", "CrawlerRunner",
         "-destination", "generic/platform=iOS Simulator",
         "-derivedDataPath", dd],
        cwd=rdir, timeout=build_timeout)
    if code != 0:
        emit("UI crawl: runner build failed (%s) — skipping." % err.strip()[-160:])
        return ""
    xctestrun = _find_xctestrun(dd)
    if xctestrun:
        try:
            with open(marker, "w", encoding="utf-8") as fh:
                fh.write(want)
        except OSError:
            pass
    return xctestrun


def _find_xctestrun(dd):
    products = os.path.join(dd, "Build", "Products")
    try:
        runs = sorted(n for n in os.listdir(products) if n.endswith(".xctestrun"))
    except OSError:
        return ""
    return os.path.join(products, runs[-1]) if runs else ""


def run_crawler(xctestrun, udid, bundle_id, report_dir, flows_path="",
                max_screens=25, max_seconds=300, only=None, clean=True):
    """One xcodebuild test-without-building invocation. only selects a single
    test ("crawl"/"flows"); clean=False preserves an earlier invocation's
    report/screenshots (the Swift side merges keys). Returns (report, note)."""
    os.makedirs(report_dir, exist_ok=True)
    if clean:
        for n in list(os.listdir(report_dir)):
            try:
                os.remove(os.path.join(report_dir, n))
            except OSError:
                pass
    env = dict(os.environ)
    env["TEST_RUNNER_CRAWL_TARGET"] = bundle_id
    env["TEST_RUNNER_CRAWL_REPORT_DIR"] = report_dir
    env["TEST_RUNNER_CRAWL_MAX_SCREENS"] = str(int(max_screens))
    env["TEST_RUNNER_CRAWL_MAX_SECONDS"] = str(int(max_seconds))
    if flows_path:
        env["TEST_RUNNER_CRAWL_FLOWS"] = flows_path
    cmd = ["xcodebuild", "test-without-building",
           "-xctestrun", xctestrun, "-destination", "id=%s" % udid]
    if only in ("crawl", "flows"):
        cmd += ["-only-testing:CrawlerUITests/CrawlerTests/test%s"
                % ("Crawl" if only == "crawl" else "Flows")]
    code, out, err = _run(cmd, timeout=int(max_seconds) + 420, env=env)
    report_path = os.path.join(report_dir, "crawl_report.json")
    try:
        with open(report_path, encoding="utf-8") as fh:
            report = json.load(fh)
    except (OSError, ValueError):
        tail = (err or out).strip()[-200:]
        return None, "no crawl report (exit %d): %s" % (code, tail)
    return report, ""


def append_regression_flows(app_dir, crashes):
    """Self-learning: a crash's recorded tap path becomes a PERMANENT
    regression flow in <app>/flows.json — future runs replay the exact edge
    case. Deduped by name; never raises. Returns names added."""
    usable = []
    for c in crashes or []:
        path = [s for s in (c.get("path") or [])
                if isinstance(s, dict) and (s.get("id") or s.get("label"))]
        if path:
            usable.append(path)
    if not usable:
        return []
    flows_path = os.path.join(app_dir, "flows.json")
    root = {"flows": []}
    try:
        with open(flows_path, encoding="utf-8") as fh:
            loaded = json.load(fh)
        if isinstance(loaded, dict) and isinstance(loaded.get("flows"), list):
            root = loaded
    except (OSError, ValueError):
        pass
    existing = {f.get("name") for f in root["flows"] if isinstance(f, dict)}
    added = []
    for path in usable:
        taps = [s.get("id") or s.get("label") for s in path]
        name = "regression: crash via " + " > ".join(t for t in taps if t)[:120]
        if name in existing:
            continue
        root["flows"].append({
            "name": name,
            "steps": [{"tap": t} for t in taps if t],
            "origin": "ui_crawl_crash",
        })
        existing.add(name)
        added.append(name)
    if added:
        try:
            with open(flows_path, "w", encoding="utf-8") as fh:
                json.dump(root, fh, indent=2)
        except OSError:
            return []
    return added


def run_ui_crawl(cfg, cget, emit, app, app_dir, state, prompt):
    """The gate. Returns a human reason string on FAIL (crash or declared
    flow failure; dead buttons are warnings unless promoted), else None."""
    if not bool(cget(cfg, "runtime.ui_crawl_enabled", True)):
        return None
    build_dir = os.path.join(app_dir, "app_build")
    if not os.path.isdir(build_dir):
        return None
    if not tools_available():
        emit("UI crawl: xcodebuild/xcrun/xcodegen unavailable — skipping.")
        return None
    try:
        xctestrun = ensure_runner(emit)
        if not xctestrun:
            return None
        # Reuse the simulator + installed app visual QA just used; build and
        # install ourselves when visual QA was skipped.
        ctx = cfg.get("_sim_ctx") or {}
        udid, bid = ctx.get("udid", ""), ctx.get("bundle_id", "")
        app_path = ctx.get("app_path", "")
        if udid and bid and app_path:
            # Virgin state: visual QA's launch consumed one-shot onboarding —
            # reinstall so the crawl and declared flows see a first run.
            _run(["xcrun", "simctl", "uninstall", udid, bid], timeout=60)
            note = vqalib._install_with_rescue(udid, app_path)
            if note:
                emit("UI crawl: %s — skipping." % note)
                return None
        if not (udid and bid):
            dd = os.path.join(app_dir, ".orchestrator_runtime", "visual_qa_dd")
            timeout = int(cget(cfg, "runtime.visual_qa_build_timeout", 600) or 600)
            app_path, note = vqalib.build_for_simulator(build_dir, dd, timeout)
            if not app_path:
                emit("UI crawl: %s — skipping." % note)
                return None
            bid = vqalib.bundle_id(app_path)
            udid, note = vqalib.pick_simulator()
            if not (udid and bid):
                emit("UI crawl: %s — skipping." % (note or "no bundle id"))
                return None
            _run(["xcrun", "simctl", "uninstall", udid, bid], timeout=60)
            note = vqalib._install_with_rescue(udid, app_path)
            if note:
                emit("UI crawl: %s — skipping." % note)
                return None
        report_dir = os.path.join(app_dir, "docs", "ui_crawl")
        flows_path = os.path.join(app_dir, "flows.json")
        max_scr = int(cget(cfg, "runtime.ui_crawl_max_screens", 25) or 25)
        max_sec = int(cget(cfg, "runtime.ui_crawl_max_seconds", 300) or 300)
        declared = []
        try:
            with open(flows_path, encoding="utf-8") as fh:
                loaded = json.load(fh)
            if isinstance(loaded, dict) and isinstance(loaded.get("flows"), list):
                declared = [f for f in loaded["flows"] if isinstance(f, dict)]
        except (OSError, ValueError):
            pass
        emit("UI crawl: crawling %s on %s%s…"
             % (bid, udid[:8],
                " + %d declared flow(s)" % len(declared) if declared else ""))
        os.makedirs(report_dir, exist_ok=True)
        for n in list(os.listdir(report_dir)):
            try:
                os.remove(os.path.join(report_dir, n))
            except OSError:
                pass

        def _fresh_install():
            # One-shot state (onboarding, seeds) must not leak between flows
            # or into the crawl — every scenario starts from first launch.
            if app_path:
                _run(["xcrun", "simctl", "uninstall", udid, bid], timeout=60)
                vqalib._install_with_rescue(udid, app_path)

        # Each declared flow runs in its own invocation on a virgin install.
        flow_results = []
        for fl in declared:
            tmp = os.path.join(report_dir, "_one_flow.json")
            try:
                with open(tmp, "w", encoding="utf-8") as fh:
                    json.dump({"flows": [fl]}, fh)
            except OSError:
                continue
            _fresh_install()
            rep_f, note = run_crawler(xctestrun, udid, bid, report_dir,
                                      flows_path=tmp, max_screens=max_scr,
                                      max_seconds=max_sec, only="flows",
                                      clean=False)
            if rep_f and rep_f.get("flows"):
                flow_results.extend(rep_f["flows"])
            else:
                emit("UI crawl: flow '%s' invocation failed (%s)."
                     % (fl.get("name", "?"), note))
            try:
                os.remove(tmp)
            except OSError:
                pass

        _fresh_install()
        report, note = run_crawler(xctestrun, udid, bid, report_dir,
                                   max_screens=max_scr, max_seconds=max_sec,
                                   only="crawl", clean=False)
        if report is None:
            emit("UI crawl: %s — skipping." % note)
            return None
        report["flows"] = flow_results

        crashes = report.get("crashes") or []
        dead = report.get("dead_taps") or []
        backv = report.get("back_violations") or []
        flows = report.get("flows") or []
        failed_flows = [f for f in flows if not f.get("passed")]
        summary = {
            "bundle_id": bid,
            "screens": len(report.get("screens") or []),
            "edges": len(report.get("edges") or []),
            "crashes": crashes,
            "dead_taps": dead,
            "back_violations": backv,
            "flows": flows,
        }
        # Multi-screen visual grading: every screen the crawl discovered
        # goes through the same local vision panel (WARN-only — the
        # dedicated visual-QA gate already gates the main screens; this
        # extends eyes to screens 2..N for free).
        try:
            panel = vqalib.resolve_vision_models(
                cget(cfg, "runtime.visual_qa_model", ""),
                lmlib.installed_models_cached())
            shots = sorted(
                os.path.join(report_dir, n) for n in os.listdir(report_dir)
                if n.endswith(".png"))[:6]
            if panel and shots and vqalib.ollama_up():
                grades = vqalib.grade(panel, shots, "", str(prompt))
                if grades:
                    summary["screen_grades"] = grades
                    for issue in (grades.get("issues") or [])[:3]:
                        emit("UI crawl WARN: screen %s looks unfinished — %s"
                             % (issue.get("screen", "?"),
                                issue.get("note", "")[:100]))
        except Exception:  # noqa: BLE001 - advisory grading only
            pass
        try:
            os.makedirs(os.path.join(app_dir, "docs"), exist_ok=True)
            with open(os.path.join(app_dir, "docs", "ui_crawl.json"), "w",
                      encoding="utf-8") as fh:
                json.dump(summary, fh, indent=2)
        except OSError:
            pass
        emit("UI crawl: %d screen(s), %d edge(s), %d dead tap(s), "
             "%d back violation(s), %d crash(es), %d/%d flow(s) passed."
             % (summary["screens"], summary["edges"], len(dead), len(backv),
                len(crashes), len(flows) - len(failed_flows), len(flows)))

        # Self-learning: crashes become permanent regression flows.
        for name in append_regression_flows(app_dir, crashes):
            emit("UI crawl: learned regression flow — %s" % name)

        if crashes:
            c = crashes[0]
            via = (c.get("tapped") or {})
            return ("app crashed during UI crawl (tapped %s; repro saved to "
                    "flows.json)" % (via.get("id") or via.get("label") or "?"))
        if failed_flows:
            f = failed_flows[0]
            more = "" if len(failed_flows) == 1 \
                else " (+%d more)" % (len(failed_flows) - 1)
            return "declared user flow '%s' failed: %s%s" % (
                f.get("name", "?"), f.get("failure", ""), more)
        if dead and bool(cget(cfg, "runtime.ui_crawl_fail_on_dead_buttons",
                              False)):
            d = dead[0].get("tapped") or {}
            return "%d dead button(s) — e.g. '%s' does nothing when tapped" % (
                len(dead), d.get("id") or d.get("label") or "?")
        for d in dead[:5]:
            t = d.get("tapped") or {}
            emit("UI crawl WARN: '%s' (%s) does nothing when tapped."
                 % (t.get("id") or t.get("label") or "?", t.get("kind", "?")))
        for b in backv[:3]:
            emit("UI crawl WARN: back navigation did not restore the previous "
                 "screen (after visiting %s)." % (b.get("after_visiting", "?")))
        emit("App '%s': UI crawl PASS." % app)
        return None
    except Exception as exc:  # noqa: BLE001 - the gate must never kill a run
        emit("WARN UI crawl skipped (%s)." % exc)
        return None
