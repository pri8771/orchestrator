#!/usr/bin/env python3
"""
Verification loop — actually compile/build what the agents wrote.

The single biggest gap between "impressive transcript" and "trustworthy tool"
is that nothing checked whether the generated project builds. This module runs
a real build and returns a structured result the engine feeds back into repair
iterations.

It is deliberately best-effort and never raises: if the toolchain isn't present
(no Xcode, no swift, etc.) it returns ``ran=False`` with a clear note and the
build phase simply proceeds unverified rather than crashing.

Verification is compile-only: for iOS we build for the *simulator* with signing
disabled, because that's the cleanest "does this actually compile" gate and
needs no device, team, or provisioning profile. Device-signing correctness is a
separate deterministic pass (fix_ios_signing in orchestrator.py).
"""

import contextlib
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import tempfile
import time
import urllib.error
import urllib.request

import procutil

try:
    import fcntl
except ImportError:  # non-Unix; the cross-process lock degrades to best-effort
    fcntl = None


def _run(cmd, cwd, timeout):
    # Routed through procutil: xcodebuild spawns build-service daemons that
    # inherit our pipes, so plain subprocess.run(timeout).communicate() can hang
    # the reap forever on a timeout. run_capture kills the whole process group.
    try:
        out, err, code = procutil.run_capture(cmd, cwd=cwd, timeout=timeout)
        return code, out, err
    except subprocess.TimeoutExpired:
        return 124, "", "verification timed out after %ss" % timeout
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, "", str(exc)


def _find(build_dir, suffix):
    hits = []
    for dirpath, dirnames, filenames in os.walk(build_dir):
        dirnames[:] = [d for d in dirnames
                       if d not in (".git", "node_modules", ".build", "DerivedData")]
        for name in dirnames + filenames:
            if name.endswith(suffix):
                hits.append(os.path.join(dirpath, name))
        # Match .xcodeproj/.xcworkspace bundles at their own level (above), but do
        # NOT descend into them — the project.xcworkspace *inside* an .xcodeproj is
        # scheme-less and must never be mistaken for a real top-level workspace.
        dirnames[:] = [d for d in dirnames
                       if not d.endswith((".xcodeproj", ".xcworkspace"))]
    return sorted(hits, key=len)  # shallowest first


def detect_project(build_dir):
    """Return a coarse project kind for auto-verification."""
    if not build_dir or not os.path.isdir(build_dir):
        return "empty"
    if _find(build_dir, ".xcworkspace") or _find(build_dir, ".xcodeproj"):
        return "xcode"
    if _find(build_dir, "project.yml") or _find(build_dir, "project.yaml") \
            or _find(build_dir, "Project.swift"):
        return "xcode"   # XcodeGen/Tuist spec — the .xcodeproj is generated at verify time
    if _find(build_dir, "Package.swift"):
        return "spm"
    if os.path.exists(os.path.join(build_dir, "package.json")):
        return "node"
    if os.path.exists(os.path.join(build_dir, "requirements.txt")) \
            or _find(build_dir, "pyproject.toml"):
        return "python"
    return "unknown"


def _errors_tail(text, limit=3000):
    lines = [ln for ln in text.splitlines()
             if re.search(r"\b(error|fatal|failed|cannot find|no such)\b", ln,
                          re.IGNORECASE)]
    if not lines:
        lines = text.splitlines()[-40:]
    tail = "\n".join(lines[-60:])
    return tail[-limit:]


def _xcode_scheme(target_flag, target, cwd, timeout):
    code, out, _err = _run(["xcodebuild"] + target_flag + [target, "-list"],
                           cwd, min(timeout, 120))
    if code != 0:
        return None
    m = re.search(r"Schemes:\s*\n(.*)", out, re.DOTALL)
    if not m:
        return None
    schemes = [line.strip() for line in m.group(1).splitlines() if line.strip()]
    return _pick_scheme(schemes, target)


def _pick_scheme(schemes, target):
    """Choose which scheme verification builds. First-listed is WRONG for
    projects with local Swift packages: package schemes (e.g. CockpitData)
    sort before the app scheme, so the gate would compile a package and bless
    an app that doesn't build (MarketingCampaignCockpit post-mortem). Prefer
    the scheme named like the project, then a related name, then first."""
    if not schemes:
        return None
    base = os.path.splitext(os.path.basename(target))[0].lower()
    for s in schemes:
        if s.lower() == base:
            return s
    for s in schemes:
        sl = s.lower()
        if sl and (sl in base or base in sl):
            return s
    return schemes[0]


def _generate_xcodeproj(build_dir, timeout):
    """Agents sometimes emit an XcodeGen ``project.yml`` or Tuist ``Project.swift``
    instead of a committed ``.xcodeproj``. Without this, the compile gate finds no
    project and the build silently escapes verification (and repair). Generate the
    project so xcodebuild has something to compile. Returns a short note for the
    summary; best-effort and never raises."""
    ymls = _find(build_dir, "project.yml") + _find(build_dir, "project.yaml")
    if ymls:
        if not shutil.which("xcodegen"):
            return " (found project.yml but `xcodegen` is not installed)"
        cwd = os.path.dirname(ymls[0])
        code, _out, err = _run(["xcodegen", "generate"], cwd, min(timeout, 120))
        return " (ran xcodegen generate)" if code == 0 \
            else " (xcodegen generate failed: %s)" % (err.strip()[:120])
    tuist = _find(build_dir, "Project.swift")
    if tuist:
        if not shutil.which("tuist"):
            return " (found Project.swift but `tuist` is not installed)"
        cwd = os.path.dirname(tuist[0])
        code, _out, err = _run(["tuist", "generate", "--no-open"], cwd, min(timeout, 180))
        return " (ran tuist generate)" if code == 0 \
            else " (tuist generate failed: %s)" % (err.strip()[:120])
    return ""


def _verify_xcode(build_dir, timeout):
    if not shutil.which("xcodebuild"):
        return {"ran": False, "ok": False, "tool": "xcodebuild",
                "summary": "xcodebuild not found (Xcode command line tools "
                           "unavailable) — skipping compile check.", "errors": ""}
    # Absolute so that running xcodebuild with cwd=project_dir can't mis-resolve a
    # relative -project/-workspace path.
    build_dir = os.path.abspath(build_dir)
    ws = _find(build_dir, ".xcworkspace")
    proj = _find(build_dir, ".xcodeproj")
    gen_note = ""
    if not ws and not proj:
        # No committed project — try generating one from an XcodeGen/Tuist spec.
        gen_note = _generate_xcodeproj(build_dir, timeout)
        ws = _find(build_dir, ".xcworkspace")
        proj = _find(build_dir, ".xcodeproj")
    if ws:
        target_flag, target = ["-workspace"], ws[0]
    elif proj:
        target_flag, target = ["-project"], proj[0]
    else:
        return {"ran": False, "ok": False, "tool": "xcodebuild",
                "summary": "no .xcodeproj/.xcworkspace found%s." % gen_note,
                "errors": ""}
    cwd = os.path.dirname(target)
    scheme = _xcode_scheme(target_flag, target, cwd, timeout)
    cmd = ["xcodebuild"] + target_flag + [target]
    if scheme:
        cmd += ["-scheme", scheme]
    cmd += ["-sdk", "iphonesimulator",
            "-destination", "generic/platform=iOS Simulator",
            "-configuration", "Debug",
            "CODE_SIGNING_ALLOWED=NO", "CODE_SIGNING_REQUIRED=NO",
            "build"]
    code, out, err = _run(cmd, cwd, timeout)
    ok = (code == 0)
    combined = out + "\n" + err
    return {
        "ran": True, "ok": ok, "tool": "xcodebuild",
        "summary": ("compiled cleanly for the iOS Simulator"
                    if ok else "compile FAILED for the iOS Simulator"),
        "errors": "" if ok else _errors_tail(combined),
        "scheme": scheme or "(default)",
    }


def _verify_spm(build_dir, timeout):
    if not shutil.which("swift"):
        return {"ran": False, "ok": False, "tool": "swift build",
                "summary": "swift not found — skipping.", "errors": ""}
    pkgs = _find(build_dir, "Package.swift")
    cwd = os.path.dirname(pkgs[0]) if pkgs else build_dir
    code, out, err = _run(["swift", "build"], cwd, timeout)
    ok = (code == 0)
    return {"ran": True, "ok": ok, "tool": "swift build",
            "summary": "swift build succeeded" if ok else "swift build FAILED",
            "errors": "" if ok else _errors_tail(out + "\n" + err)}


def _verify_shell(build_dir, command, timeout):
    if not command:
        # Auto-detect a sensible check.
        kind = detect_project(build_dir)
        if kind == "spm":
            return _verify_spm(build_dir, timeout)
        if kind == "node" and shutil.which("node"):
            command = "npm run build --if-present || node -e \"process.exit(0)\""
        elif kind == "python" and shutil.which("python3"):
            command = "python3 -m compileall -q ."
        else:
            return {"ran": False, "ok": False, "tool": "shell",
                    "summary": "no verification command and no auto-detectable "
                               "build for this project — skipping.", "errors": ""}
    code, out, err = _run(["/bin/sh", "-lc", command], build_dir, timeout)
    ok = (code == 0)
    return {"ran": True, "ok": ok, "tool": "shell",
            "summary": ("`%s` succeeded" % command) if ok
                       else ("`%s` FAILED (exit %s)" % (command, code)),
            "errors": "" if ok else _errors_tail(out + "\n" + err)}


# ---------------------------------------------------------------------------
# HTTP boot-and-hit verification (for backends / web servers)
# ---------------------------------------------------------------------------
def _detect_start(build_dir, port):
    """Best-effort: figure out how to start the server the agents built and where
    it lives. Returns (command, cwd, base_hint) or (None, None, None)."""
    # Node: nearest package.json with a start/dev script.
    for pkg in _find(build_dir, "package.json"):
        try:
            with open(pkg, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            continue
        scripts = (data.get("scripts") or {})
        cwd = os.path.dirname(pkg)
        if "start" in scripts:
            return "npm start", cwd, 3000
        if "dev" in scripts:
            return "npm run dev", cwd, 3000
    # Python: a module exposing a FastAPI/Flask `app`.
    for cand in ("main.py", "app.py", "server.py"):
        for hit in _find(build_dir, cand):
            try:
                with open(hit, encoding="utf-8") as fh:
                    src = fh.read()
            except OSError:
                continue
            cwd = os.path.dirname(hit)
            mod = os.path.basename(hit)[:-3]
            if "FastAPI(" in src or "fastapi" in src.lower():
                # `mod` is interpolated into a command later run via `/bin/sh
                # -lc`, so an agent-authored file named e.g. `x;whoami main.py`
                # would inject shell. A uvicorn import target must be a valid
                # Python identifier anyway; reject anything else and fall
                # through to the compile/import check.
                if not mod.isidentifier():
                    continue
                if shutil.which("uvicorn"):
                    return "uvicorn %s:app --host 127.0.0.1 --port %d" % (mod, port), cwd, port
                return "python3 -m uvicorn %s:app --host 127.0.0.1 --port %d" % (mod, port), cwd, port
            if "Flask(" in src:
                # Same shell exposure — quote the filename so a crafted name
                # can't break out of the `python3 <file>` invocation.
                return "python3 %s" % shlex.quote(os.path.basename(hit)), cwd, port
    return None, None, None


def _http_ok(url, timeout=3):
    """Return the HTTP status if the server responds at all (any status), else None."""
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status
    except urllib.error.HTTPError as e:
        return e.code            # responded (e.g. 404) — the server IS up
    except Exception:
        return None


def _verify_http(build_dir, spec, timeout):
    port = int(spec.get("port") or 8000)
    start = spec.get("start")
    cwd = build_dir
    if start:
        base_port = port
    else:
        start, cwd, base_port = _detect_start(build_dir, port)
        if not start:
            # Couldn't figure out how to boot it — fall back to a compile/import
            # check so we still say *something*, rather than crashing.
            return _verify_shell(build_dir, spec.get("command"), timeout)
        port = base_port
    health = spec.get("health") or "http://127.0.0.1:%d/health" % port
    root = "http://127.0.0.1:%d/" % port
    ready_timeout = int(spec.get("ready_timeout") or min(60, timeout))

    env = dict(os.environ)
    env["PORT"] = str(port)
    proc = None
    server_pgid = None
    # Write the server log to a temp file OUTSIDE the built project — dropping
    # it into build_dir pollutes the generated app tree that later gets
    # committed/shipped.
    log_fd, out_path = tempfile.mkstemp(prefix="verify_server_", suffix=".log")
    os.close(log_fd)
    try:
        outfh = open(out_path, "w", encoding="utf-8")
        proc = subprocess.Popen(["/bin/sh", "-lc", start], cwd=cwd, env=env,
                                stdout=outfh, stderr=subprocess.STDOUT,
                                start_new_session=True)
        # Register the server's process group so a run-wide SIGTERM
        # (procutil.kill_live_groups) also tears it down — otherwise a booted
        # server leaks past the run.
        try:
            server_pgid = os.getpgid(proc.pid)
        except OSError:
            server_pgid = proc.pid
        procutil.track_pgid(server_pgid)
    except (OSError, subprocess.SubprocessError) as exc:
        try:
            os.remove(out_path)
        except OSError:
            pass
        return {"ran": True, "ok": False, "tool": "http boot",
                "summary": "could not start the server (`%s`)" % start,
                "errors": str(exc)}

    booted = False
    status = None
    deadline = time.time() + ready_timeout
    try:
        while time.time() < deadline:
            if proc.poll() is not None:      # server exited before responding
                break
            status = _http_ok(health) or _http_ok(root)
            if status is not None:
                booted = True
                break
            time.sleep(0.5)
    finally:
        if proc and proc.poll() is None:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            except OSError:
                pass
            try:
                proc.wait(timeout=10)
            except (subprocess.TimeoutExpired, OSError):
                # Graceful stop failed — escalate so a wedged server can't leak
                # past the verification run.
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                except OSError:
                    pass
                try:
                    proc.wait(timeout=5)
                except (subprocess.TimeoutExpired, OSError):
                    pass
        if server_pgid is not None:
            procutil.untrack_pgid(server_pgid)
        try:
            outfh.close()
        except OSError:
            pass

    log_tail = ""
    try:
        with open(out_path, encoding="utf-8") as fh:
            log_tail = _errors_tail(fh.read())
    except OSError:
        pass
    finally:
        try:
            os.remove(out_path)   # temp log — never leave it behind
        except OSError:
            pass
    if booted:
        return {"ran": True, "ok": True, "tool": "http boot",
                "summary": "server booted (`%s`) and responded %s on :%d"
                           % (start, status, port), "errors": ""}
    return {"ran": True, "ok": False, "tool": "http boot",
            "summary": "server did not respond within %ds (`%s`)" % (ready_timeout, start),
            "errors": log_tail}


def run_verification(build_dir, spec, timeout=1200):
    """Run the verification described by ``spec`` ({type, command?}).

    Returns a dict: ran, ok, tool, summary, errors. ``ran=False`` means the
    check couldn't run (toolchain absent) and should be treated as "unverified",
    not "failed". Never raises.
    """
    spec = spec or {}
    vtype = (spec.get("type") or "auto").lower()
    try:
        if not build_dir or not os.path.isdir(build_dir):
            return {"ran": False, "ok": False, "tool": vtype,
                    "summary": "no build directory to verify.", "errors": ""}
        if vtype == "xcodebuild":
            return _verify_xcode(build_dir, timeout)
        if vtype in ("swift", "spm"):
            return _verify_spm(build_dir, timeout)
        if vtype in ("http", "server", "boot"):
            return _verify_http(build_dir, spec, timeout)
        if vtype == "shell":
            return _verify_shell(build_dir, spec.get("command"), timeout)
        # auto
        kind = detect_project(build_dir)
        if kind == "xcode":
            return _verify_xcode(build_dir, timeout)
        if kind == "spm":
            return _verify_spm(build_dir, timeout)
        return _verify_shell(build_dir, spec.get("command"), timeout)
    except Exception as exc:  # defensive: verification must never abort a run
        return {"ran": False, "ok": False, "tool": vtype,
                "summary": "verification errored: %s" % exc, "errors": ""}


# ---------------------------------------------------------------------------
# V2 spec §15/§16: persisted, structured verification results + gate helpers.
# verify_results.json is an append-style JSON array (newest last), written
# atomically. Every verification attempt — initial and each repair — is recorded.
# ---------------------------------------------------------------------------
import json as _json
import datetime as _dt


def verification_status(result):
    """verified | failed | unverified from a raw run_verification dict (§15)."""
    if not isinstance(result, dict):
        return "unverified"
    if not result.get("ran"):
        return "unverified"
    return "verified" if result.get("ok") else "failed"


def _vr_path(app_dir):
    return os.path.join(app_dir, "verify_results.json")


@contextlib.contextmanager
def _vr_lock(app_dir):
    """Exclusive lock (flock) around a verify_results.json read-modify-write so
    concurrent verifications — parallel portfolio children, or build-worker
    threads in one process — don't lose each other's records. Best-effort: on a
    platform without fcntl, or if the lock file can't be opened, it degrades to
    no locking rather than raising."""
    if fcntl is None:
        yield
        return
    fh = None
    try:
        fh = open(_vr_path(app_dir) + ".lock", "w")
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
    except OSError:
        if fh is not None:
            fh.close()
        yield
        return
    try:
        yield
    finally:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        fh.close()


def load_verify_results(app_dir):
    """Return the list of persisted verification records (oldest first). []
    if none/unreadable. Never raises."""
    try:
        with open(_vr_path(app_dir), encoding="utf-8") as fh:
            data = _json.load(fh)
        return data if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def persist_verify_result(app_dir, phase_key, result, attempt=0,
                          prompt_hash=None, workflow=None):
    """Append a structured record for one verification attempt to
    verify_results.json (atomic tmp + os.replace). Returns the record written.
    Best-effort; never raises."""
    result = result or {}
    record = {
        "schema_version": 1,
        "timestamp": _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "phase": phase_key,
        "workflow": workflow,
        "prompt_hash": prompt_hash,
        "attempt": int(attempt),
        "repair_attempt": bool(attempt and attempt > 0),
        "ran": bool(result.get("ran")),
        "ok": bool(result.get("ok")),
        "status": verification_status(result),
        "tool": result.get("tool", "none"),
        "scheme": result.get("scheme"),
        "summary": result.get("summary", ""),
        "errors": (result.get("errors", "") or "")[:8000],
        "tests": result.get("tests"),
    }
    try:
        with _vr_lock(app_dir):
            records = load_verify_results(app_dir)
            records.append(record)
            # Per-writer temp name so a concurrent writer on another thread/
            # process can't clobber this one's half-written file (the flock
            # serializes, but a unique name is belt-and-suspenders).
            tmp = "%s.%d.tmp" % (_vr_path(app_dir), os.getpid())
            with open(tmp, "w", encoding="utf-8") as fh:
                _json.dump(records, fh, indent=2)
            os.replace(tmp, _vr_path(app_dir))
    except OSError:
        pass
    return record


def latest_verify_result(app_dir, prompt_hash=None, phase_key=None):
    """Most recent record, optionally filtered by prompt_hash/phase. None if none."""
    records = load_verify_results(app_dir)
    for rec in reversed(records):
        if prompt_hash is not None and rec.get("prompt_hash") not in (None, prompt_hash):
            continue
        if phase_key is not None and rec.get("phase") != phase_key:
            continue
        return rec
    return None


def summarize_verify_results(results, latest=None):
    """Short human line for the final-review context / GUI (§15)."""
    latest = latest or (results[-1] if results else None)
    if not latest:
        return "No verification result exists yet."
    return "Latest verification: %s (%s) — %s [attempt %s]" % (
        latest.get("status", "unverified").upper(), latest.get("tool", "none"),
        latest.get("summary", ""), latest.get("attempt", 0))
