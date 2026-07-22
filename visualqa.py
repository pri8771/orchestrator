#!/usr/bin/env python3
"""
Visual QA gate: "it compiles" and "it does what was asked" still miss "it
LOOKS finished". After the release gate passes, this module boots the iOS
Simulator, installs and launches the built app, captures light- and dark-mode
screenshots, and asks a LOCAL vision model (default qwen2.5vl:3b via the
Ollama loopback API — zero cloud tokens) to grade them against the design
handoff. A FAIL routes into the same bounded iterate-repair loop the release
and adherence gates use.

Contract (mirrors the other gates): EVERY step is best-effort. Missing
xcodebuild/simctl, no simulator, vision model not pulled, Ollama down,
unparseable grader output — all degrade to "skip" (None), never to a blocked
run. Screenshots + verdict persist to <app>/docs/screenshots/ and
<app>/docs/visual_qa.json so the GUI and the repair prompt can cite them.

Config (config.yaml -> runtime):
  visual_qa_enabled: true      # the gate itself
  visual_qa_model: ""          # "" = auto panel of installed vision models
                               # (unanimous BAD fails); or one model id
  visual_qa_build_timeout: 600 # seconds for the simulator build
"""

import base64
import json
import os
import plistlib
import re
import shutil
import subprocess
import time
import urllib.request

import localmodels as lmlib
import turncontext as tcxlib
import verify as verifylib

OLLAMA_GENERATE_URL = "http://127.0.0.1:11434/api/generate"
DEFAULT_VISION_MODEL = "qwen2.5vl:3b"
# Known vision-capable Ollama tags, best judge first. Small VLMs are noisy
# graders (measured: qwen2.5vl:3b false-BADs a clearly-finished screen), so
# every installed candidate votes and only a UNANIMOUS BAD fails a screen —
# a false FAIL burns a whole repair loop; a false PASS just stays quiet.
VISION_CANDIDATES = ("gemma3:12b", "gemma3:4b", "qwen2.5vl:3b")

# Small local VLMs (3-4B) echo JSON templates back instead of filling them
# in, so the grading contract is deliberately binary: one image per call, the
# first OK/BAD token wins. Word-boundary match, case-insensitive.
_OK_BAD_RE = re.compile(r"\b(OK|BAD)\b", re.IGNORECASE)


def _run(cmd, cwd=None, timeout=120):
    """subprocess.run wrapper -> (code, stdout, stderr); never raises."""
    try:
        p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                           timeout=timeout)
        return p.returncode, p.stdout or "", p.stderr or ""
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, "", str(exc)


def tools_available():
    return bool(shutil.which("xcodebuild")) and bool(shutil.which("xcrun"))


def ollama_up(timeout=3):
    try:
        with urllib.request.urlopen(lmlib.OLLAMA_TAGS_URL, timeout=timeout):
            return True
    except OSError:
        return False


def resolve_vision_models(cfg_model, installed):
    """The grader panel. An explicit configured model (if pulled) grades
    alone; otherwise every installed VISION_CANDIDATES entry votes."""
    inst = set(installed or [])
    want = (cfg_model or "").strip()
    if want:
        return [want] if want in inst else []
    return [m for m in VISION_CANDIDATES if m in inst]


def build_for_simulator(build_dir, dd_path, timeout=600):
    """Build the app into OUR DerivedData dir so the .app lands somewhere
    deterministic. Reuses verify.py's project/scheme discovery. Returns the
    .app path or "" (with a note) on any failure."""
    build_dir = os.path.abspath(build_dir)
    ws = verifylib._find(build_dir, ".xcworkspace")
    proj = verifylib._find(build_dir, ".xcodeproj")
    if ws:
        target_flag, target = ["-workspace"], ws[0]
    elif proj:
        target_flag, target = ["-project"], proj[0]
    else:
        return "", "no .xcodeproj/.xcworkspace under app_build"
    cwd = os.path.dirname(target)
    scheme = verifylib._xcode_scheme(target_flag, target, cwd, timeout=120)
    cmd = ["xcodebuild"] + target_flag + [target]
    if scheme:
        cmd += ["-scheme", scheme]
    # Unlike verify's compile-only check, we must INSTALL this build — so keep
    # the default simulator ad-hoc signing (CODE_SIGNING_ALLOWED=NO produces
    # bundles simctl refuses to install).
    cmd += ["-sdk", "iphonesimulator",
            "-destination", "generic/platform=iOS Simulator",
            "-configuration", "Debug",
            "-derivedDataPath", dd_path,
            "build"]
    code, _out, err = _run(cmd, cwd, timeout)
    if code != 0:
        return "", "simulator build failed: %s" % err.strip()[-200:]
    products = os.path.join(dd_path, "Build", "Products",
                            "Debug-iphonesimulator")
    try:
        apps = [os.path.join(products, n) for n in sorted(os.listdir(products))
                if n.endswith(".app")]
    except OSError:
        apps = []
    return (apps[0], "") if apps else ("", "built, but no .app in %s" % products)


def bundle_id(app_path):
    try:
        with open(os.path.join(app_path, "Info.plist"), "rb") as fh:
            return str(plistlib.load(fh).get("CFBundleIdentifier") or "")
    except (OSError, ValueError, plistlib.InvalidFileException):
        return ""


def pick_simulator():
    """A booted device if any, else boot the newest available iPhone.
    Returns (udid, note); udid "" = no usable simulator."""
    code, out, _err = _run(["xcrun", "simctl", "list", "devices", "--json"],
                           timeout=30)
    if code != 0:
        return "", "simctl list failed"
    try:
        devices = json.loads(out).get("devices", {})
    except ValueError:
        return "", "simctl list returned no JSON"
    iphones = []
    for runtime, devs in devices.items():
        if "iOS" not in runtime:
            continue
        for d in devs:
            if not d.get("isAvailable"):
                continue
            name = d.get("name", "")
            if d.get("state") == "Booted":
                return d.get("udid", ""), "using booted %s" % name
            if "iPhone" in name:
                iphones.append((runtime, name, d.get("udid", "")))
    if not iphones:
        return "", "no available iPhone simulator"
    runtime, name, udid = sorted(iphones)[-1]   # newest runtime, last name
    _run(["xcrun", "simctl", "boot", udid], timeout=60)
    code, _o, _e = _run(["xcrun", "simctl", "bootstatus", udid, "-b"],
                        timeout=180)
    if code != 0:
        return "", "could not boot %s" % name
    return udid, "booted %s (%s)" % (name, runtime.rsplit(".", 1)[-1])


def _install_with_rescue(udid, app_path):
    """simctl install, with one rescue retry: app-extension placeholders
    (widgets etc.) are a common install blocker for debug builds, and they
    aren't what main-screen visual QA judges — strip PlugIns, re-sign ad-hoc,
    try again. Returns "" on success, else the error note."""
    code, _o, err = _run(["xcrun", "simctl", "install", udid, app_path],
                         timeout=120)
    if code == 0:
        return ""
    first_err = err.strip()[:160]
    plugins = os.path.join(app_path, "PlugIns")
    if os.path.isdir(plugins):
        shutil.rmtree(plugins, ignore_errors=True)
        _run(["codesign", "--force", "--deep", "--sign", "-", app_path],
             timeout=60)
        code, _o, err = _run(["xcrun", "simctl", "install", udid, app_path],
                             timeout=120)
        if code == 0:
            return ""
    return "install failed: %s" % first_err


def capture_screens(udid, app_path, bid, shots_dir):
    """Install, launch, screenshot in light AND dark mode (the design rules
    demand both palettes). Returns (paths, note)."""
    os.makedirs(shots_dir, exist_ok=True)
    note = _install_with_rescue(udid, app_path)
    if note:
        return [], note
    # Pre-grant privacy permissions so a first-launch system permission dialog
    # (location, photos, camera, …) doesn't cover the app in the screenshot —
    # the grader would score that covered screen BAD even though the app is
    # fine. This is a false-failure the gate used to hit for any app that asks
    # for a permission on launch (e.g. a location app).
    #
    # ORDERING IS THE WHOLE FIX. The grant MUST land before the first launch
    # that triggers the prompt: verified live (Xcode 26.6 / iOS 26.5) that
    # granting before the first launch suppresses the dialog, but once the
    # springboard alert is on screen NO simctl grant or relaunch dismisses it —
    # only an actual tap can, and simctl has no tap primitive. So we grant, then
    # settle, then launch. `grant all` covers the common services; the explicit
    # location grants are belt-and-suspenders for the classic launch prompt on a
    # toolchain where `all`'s service enumeration skips location. All best-effort
    # — a service the app doesn't use just errors harmlessly (exit ignored).
    #
    # Deliberately NOT preceded by `privacy reset`: a fresh install is already
    # "not determined" and `grant` overrides a stale "denied" on its own, so
    # reset buys nothing while opening a window where a silently-no-op grant
    # would ARM the exact dialog we're preventing.
    #
    # Residual (documented, not hidden): a dialog for an on-launch permission
    # that `all`/location don't cover — notifications, App Tracking Transparency,
    # which `simctl privacy` has no service for — still grades as it looks. That
    # is rare and version-dependent; the uicrawl gate runs right after on a
    # virgin reinstall as a second, imperfect backstop (its `app.alerts` query
    # targets the app, so it may miss a springboard-owned alert).
    for svc in ("all", "location", "location-always"):
        _run(["xcrun", "simctl", "privacy", udid, "grant", svc, bid], timeout=30)
    time.sleep(1)   # let the TCC write settle before the first launch reads it
    shots = []
    for mode in ("light", "dark"):
        _run(["xcrun", "simctl", "ui", udid, "appearance", mode], timeout=30)
        code, _o, err = _run(["xcrun", "simctl", "launch",
                              "--terminate-running-process", udid, bid],
                             timeout=60)
        if code != 0:
            return shots, "launch failed (%s): %s" % (mode, err.strip()[:160])
        time.sleep(4)   # let the first screen settle
        path = os.path.join(shots_dir, "main_%s.png" % mode)
        code, _o, err = _run(["xcrun", "simctl", "io", udid, "screenshot",
                              path], timeout=30)
        if code == 0 and os.path.exists(path):
            shots.append(path)
    _run(["xcrun", "simctl", "ui", udid, "appearance", "light"], timeout=30)
    _run(["xcrun", "simctl", "terminate", udid, bid], timeout=30)
    return shots, ""


def parse_ok_bad(reply):
    """First OK/BAD token in the reply -> "OK"/"BAD"/None."""
    m = _OK_BAD_RE.search(reply or "")
    return m.group(1).upper() if m else None


def _ask_one(model, image_b64, mode, purpose, timeout=240):
    """One binary judgment for one screenshot. Returns (verdict, note) where
    verdict is "OK"/"BAD"/None (None = model unusable for this image)."""
    dark_extra = (" In dark mode, BAD also means unreadable low-contrast "
                  "text (e.g. dark text on a dark background)."
                  if mode == "dark" else "")
    ask = (
        "This is a screenshot of an iOS app (%s mode). The app is supposed "
        "to be: %s\n\n"
        "Is this a real, working, styled app screen with a CLEAN, READABLE "
        "layout?\n"
        "Answer with exactly one word first — OK or BAD — then one short "
        "sentence saying what you actually see.\n"
        "OK = a designed app screen with visible, readable content whose text "
        "and controls are cleanly laid out (nothing overlapping). A "
        "first-launch EMPTY STATE is also OK when it is clearly styled and "
        "shows a message plus a button inviting the user to add their first "
        "item — mostly empty space around a styled message and button is a "
        "deliberate design, not a defect.\n"
        "BAD = a blank or nearly-blank screen, placeholder text only, the "
        "iOS home screen instead of the app, a crash/error dialog, OR a broken "
        "layout: text or controls that OVERLAP / collide / are stacked on top "
        "of each other, a label sitting over a value, or text clipped or cut "
        "off at an edge. Look carefully for overlapping text — it is the most "
        "common defect and it is always BAD.%s"
        % (mode, purpose[:200] or "an iOS app", dark_extra))
    body = json.dumps({"model": model, "prompt": ask, "images": [image_b64],
                       "stream": False}).encode("utf-8")
    req = urllib.request.Request(OLLAMA_GENERATE_URL, data=body,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            reply = json.loads(resp.read().decode("utf-8")).get("response", "")
    except (OSError, ValueError):
        return None, ""
    note = " ".join((reply or "").split())[:200]
    return parse_ok_bad(reply), note


def grade(models, image_paths, design_spec, prompt_summary, timeout=240):
    """Grade the screenshots with a panel of local vision models. One image
    per call with a binary OK/BAD contract — the only thing 3-4B VLMs answer
    reliably (they echo JSON templates back instead of filling them). A
    screen fails only when the FULL configured panel answered and every
    grader said BAD — a grader that errors out or answers unparseably
    removes the panel's authority to fail that screen (it can still pass),
    so one flaky 4B model can never be a "unanimous" panel of one. Returns
    {"score", "verdict", "issues"} or None when no image got a usable answer.
    design_spec is accepted for signature stability; the binary questions
    deliberately don't feed it to a small model."""
    del design_spec  # small-model contract: short questions only
    if isinstance(models, str):
        models = [models]
    results = []   # (name, screen_verdict, worst_note)
    inconclusive = []   # {"screen", "note"}: BAD votes with no failing quorum
    for p in image_paths:
        try:
            with open(p, "rb") as fh:
                b64 = base64.b64encode(fh.read()).decode("ascii")
        except OSError:
            continue
        mode = "dark" if "dark" in os.path.basename(p).lower() else "light"
        votes = []
        for model in models:
            verdict, note = _ask_one(model, b64, mode, prompt_summary,
                                     timeout=timeout)
            if verdict is not None:
                votes.append((verdict, "%s: %s" % (model, note)))
        if not votes:
            continue
        bad_votes = [n for v, n in votes if v == "BAD"]
        # Unanimity is judged against the CONFIGURED panel, not the models
        # that happened to answer: len(votes) == len(models) is what makes
        # "every grader said BAD" mean every grader, not every survivor.
        screen_bad = (len(votes) == len(models)
                      and len(bad_votes) == len(votes))
        if bad_votes and not screen_bad and len(votes) < len(models):
            inconclusive.append({
                "screen": os.path.basename(p),
                "note": "%d/%d grader(s) answered; not a failing quorum (%s)"
                        % (len(votes), len(models), bad_votes[0])})
        results.append((os.path.basename(p),
                        "BAD" if screen_bad else "OK",
                        bad_votes[0] if screen_bad else ""))
    if not results:
        return None
    bad = [(name, note) for name, v, note in results if v == "BAD"]
    return {
        "score": int(round(100.0 * (len(results) - len(bad)) / len(results))),
        "verdict": "FAIL" if bad else "PASS",
        "issues": [{"screen": name, "severity": "high", "note": note}
                   for name, note in bad],
        "inconclusive": inconclusive,
    }


def run_visual_qa(cfg, cget, emit, app, app_dir, state, prompt):
    """The gate. Returns a human reason string on FAIL (caller routes it into
    the iterate-repair loop), else None (pass or skip). cget/emit are injected
    so this module stays import-light and easy to test."""
    if not bool(cget(cfg, "runtime.visual_qa_enabled", True)):
        return None
    build_dir = os.path.join(app_dir, "app_build")
    if not os.path.isdir(build_dir):
        return None
    if not tools_available():
        emit("Visual QA: xcodebuild/xcrun unavailable — skipping.")
        return None
    if not ollama_up():
        emit("Visual QA: Ollama server not running — skipping (start it to "
             "enable free local screenshot grading).")
        return None
    models = resolve_vision_models(cget(cfg, "runtime.visual_qa_model", ""),
                                   lmlib.installed_models_cached())
    if not models:
        emit("Visual QA: no vision model pulled (ollama pull %s) — skipping."
             % DEFAULT_VISION_MODEL)
        return None
    try:
        dd = os.path.join(app_dir, ".orchestrator_runtime", "visual_qa_dd")
        timeout = int(cget(cfg, "runtime.visual_qa_build_timeout", 600) or 600)
        app_path, note = build_for_simulator(build_dir, dd, timeout)
        if not app_path:
            emit("Visual QA: %s — skipping." % note)
            return None
        bid = bundle_id(app_path)
        if not bid:
            emit("Visual QA: no CFBundleIdentifier in built app — skipping.")
            return None
        udid, note = pick_simulator()
        if not udid:
            emit("Visual QA: %s — skipping." % note)
            return None
        emit("Visual QA: %s; launching %s." % (note, bid))
        shots_dir = os.path.join(app_dir, "docs", "screenshots")
        shots, note = capture_screens(udid, app_path, bid, shots_dir)
        # Downstream gates (UI crawl) reuse this booted simulator + installed
        # app instead of building/installing again.
        tcxlib.TurnContext(cfg).sim_ctx = {
            "udid": udid, "bundle_id": bid, "app_path": app_path}
        if not shots:
            # The app not even launching IS a finding — but it's the release
            # gate's job to catch broken builds; report and skip here.
            emit("Visual QA: %s — skipping." % (note or "no screenshots"))
            return None
        design_spec = ""
        for k in ("design_handoff", "design", "design_discussion"):
            v = (state.get("phase_outputs") or {}).get(k)
            if v:
                design_spec = str(v)
                break
        emit("Visual QA: grading %d screenshot(s) with %s (local, free; "
             "unanimous BAD fails)." % (len(shots), " + ".join(models)))
        verdict = grade(models, shots, design_spec, str(prompt))
        result = {
            "models": models,
            "screenshots": [os.path.relpath(s, app_dir) for s in shots],
            "verdict": (verdict or {}).get("verdict", "SKIPPED"),
            "score": (verdict or {}).get("score"),
            "issues": (verdict or {}).get("issues", []),
            "inconclusive": (verdict or {}).get("inconclusive", []),
        }
        for entry in (verdict or {}).get("inconclusive", []):
            emit("Visual QA: %s — %s (screen NOT failed; a partial panel "
                 "cannot fail a screen)." % (entry.get("screen", "?"),
                                             entry.get("note", "")))
        try:
            os.makedirs(os.path.join(app_dir, "docs"), exist_ok=True)
            with open(os.path.join(app_dir, "docs", "visual_qa.json"), "w",
                      encoding="utf-8") as fh:
                json.dump(result, fh, indent=2)
        except OSError:
            pass
        if verdict is None:
            emit("Visual QA: grader reply unparseable — skipping.")
            return None
        if verdict["verdict"] == "FAIL":
            notes = "; ".join(
                "%s: %s" % (i.get("screen", "?"), i.get("note", ""))
                for i in verdict["issues"][:4] if isinstance(i, dict))
            reason = "visual QA failed (score %s)%s" % (
                verdict.get("score", "?"), " — " + notes if notes else "")
            emit("App '%s': VISUAL QA FAILED — %s" % (app, reason))
            return reason
        emit("App '%s': visual QA PASS (score %s)."
             % (app, verdict.get("score", "?")))
        return None
    except Exception as exc:  # noqa: BLE001 - the gate must never kill a run
        emit("WARN visual QA skipped (%s)." % exc)
        return None
