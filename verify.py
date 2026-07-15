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
import socket
import subprocess
import tempfile
import time
import types
import typing
import urllib.error
import urllib.request

import procutil

fcntl: typing.Optional[types.ModuleType]
try:
    import fcntl
except ImportError:  # non-Unix; the cross-process lock degrades to best-effort
    fcntl = None


def _run(cmd, cwd, timeout, env=None):
    # Routed through procutil: xcodebuild spawns build-service daemons that
    # inherit our pipes, so plain subprocess.run(timeout).communicate() can hang
    # the reap forever on a timeout. run_capture kills the whole process group.
    try:
        out, err, code = procutil.run_capture(cmd, cwd=cwd, timeout=timeout, env=env)
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


def _xcode_list_output(target_flag, target, cwd, timeout):
    """Raw `xcodebuild -list` text, or None if the command failed. Shared by
    scheme picking and test-target discovery so both read the same listing."""
    code, out, _err = _run(["xcodebuild"] + target_flag + [target, "-list"],
                           cwd, min(timeout, 120))
    return out if code == 0 else None


def _xcode_schemes_from_list(list_output):
    if not list_output:
        return []
    m = re.search(r"Schemes:\s*\n(.*)", list_output, re.DOTALL)
    if not m:
        return []
    return [line.strip() for line in m.group(1).splitlines() if line.strip()]


def _xcode_scheme(target_flag, target, cwd, timeout):
    out = _xcode_list_output(target_flag, target, cwd, timeout)
    return _pick_scheme(_xcode_schemes_from_list(out), target)


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


def _has_test_target(list_output, schemes):
    """Best-effort discovery of a real XCTest target, mirroring _pick_scheme's
    avoid-false-positive stance: only trust an explicit entry under a
    'Targets:'/'Tests:' section of `xcodebuild -list`, or an actual scheme
    name containing 'test' — never assume a test target exists just because
    run_tests was requested. False negatives are fine (we degrade to a
    build-only verification); false positives would pay for and then fail a
    test run that was never real."""
    if not list_output:
        return False
    for header in ("Targets:", "Tests:"):
        m = re.search(re.escape(header) + r"\s*\n(.*?)(?:\n\s*\n|\Z)",
                      list_output, re.DOTALL)
        if not m:
            continue
        for line in m.group(1).splitlines():
            name = line.strip()
            if name and "test" in name.lower():
                return True
    return any("test" in s.lower() for s in (schemes or []))


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


def _verify_xcode(build_dir, timeout, run_tests=False):
    """Compile-only by default; when ``run_tests`` is set AND a real test
    target is discoverable, also runs `xcodebuild ... test` — but only AFTER
    a successful build. Running the (slow, simulator-boot-heavy, sometimes
    flaky) test action against code that doesn't even compile would just burn
    the timeout twice for the same failure, so build failure short-circuits
    the test run entirely (mirrors the repair loop's own build-then-check
    order). A requested-but-undiscoverable test target degrades gracefully to
    build-only verification — this must never hard-fail a run just because
    the generated project has no tests yet."""
    if not shutil.which("xcodebuild"):
        return {"ran": False, "ok": False, "tool": "xcodebuild",
                "summary": "xcodebuild not found (Xcode command line tools "
                           "unavailable) — skipping compile check.", "errors": "",
                "tests_ran": False, "tests_ok": None}
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
                "errors": "", "tests_ran": False, "tests_ok": None}
    cwd = os.path.dirname(target)
    list_output = _xcode_list_output(target_flag, target, cwd, timeout)
    schemes = _xcode_schemes_from_list(list_output)
    scheme = _pick_scheme(schemes, target)
    cmd_base = ["xcodebuild"] + target_flag + [target]
    if scheme:
        cmd_base += ["-scheme", scheme]
    common_flags = ["-sdk", "iphonesimulator",
                    "-destination", "generic/platform=iOS Simulator",
                    "-configuration", "Debug",
                    "CODE_SIGNING_ALLOWED=NO", "CODE_SIGNING_REQUIRED=NO"]
    code, out, err = _run(cmd_base + common_flags + ["build"], cwd, timeout)
    ok = (code == 0)
    combined = out + "\n" + err
    result = {
        "ran": True, "ok": ok, "tool": "xcodebuild",
        "summary": ("compiled cleanly for the iOS Simulator"
                    if ok else "compile FAILED for the iOS Simulator"),
        "errors": "" if ok else _errors_tail(combined),
        "scheme": scheme or "(default)",
        # Additive fields (kept separate from ok/ran so nothing that reads the
        # existing fields breaks): whether a test action actually ran, and
        # whether it passed. None means "no test action was attempted".
        "tests_ran": False, "tests_ok": None,
    }
    if not run_tests:
        return result
    if not ok:
        result["summary"] += " (tests skipped: build failed)"
        return result
    if not _has_test_target(list_output, schemes):
        result["summary"] += " (run_tests requested but no test target " \
                              "discoverable — build-only verification)"
        return result
    tcode, tout, terr = _run(cmd_base + common_flags + ["test"], cwd, timeout)
    tests_ok = (tcode == 0)
    result["tests_ran"] = True
    result["tests_ok"] = tests_ok
    result["summary"] += " + tests passed" if tests_ok else " + TESTS FAILED"
    if not tests_ok:
        result["errors"] = (result.get("errors", "")
                            + "\n" + _errors_tail(tout + "\n" + terr)).strip()
    return result


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


def _has_python_tests(build_dir):
    """True if the project ships a discoverable test suite (test_*.py / *_test.py
    / conftest.py) — worth running to catch unresolved imports."""
    if _find(build_dir, "conftest.py"):
        return True
    for hit in _find(build_dir, ".py"):
        base = os.path.basename(hit)
        if base.startswith("test_") or base.endswith("_test.py"):
            return True
    return False


def _verify_shell(build_dir, command, timeout):
    if not command:
        # Auto-detect a sensible check.
        kind = detect_project(build_dir)
        if kind == "spm":
            return _verify_spm(build_dir, timeout)
        if kind == "node":
            # Route to the real install-then-build verifier rather than the old
            # `npm run build --if-present || node -e exit(0)` one-liner, which
            # never installed deps and whose `||` masked build failures as
            # ok=True. _verify_web itself skips cleanly (ran=False) when npm is
            # absent. This entry is reached both here and from _verify_http's
            # "couldn't detect how to boot" fallback.
            return _verify_web(build_dir, {}, timeout)
        if kind == "python" and shutil.which("python3"):
            # compileall is syntax-only and would bless code with unresolved
            # imports as "verified". If the project ships tests, run them too —
            # they exercise real imports/behavior — otherwise keep the syntax check.
            if _has_python_tests(build_dir):
                command = ("python3 -m compileall -q . && "
                           "python3 -m unittest discover -q")
            else:
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
        # Use the caller's `port` (env PORT is set to it by _verify_http) rather
        # than a hardcoded 3000 — the old hardcode both ignored an explicit
        # spec["port"] and collided under concurrent verifications regardless
        # of the free-port allocation above.
        if "start" in scripts:
            return "npm start", cwd, port
        if "dev" in scripts:
            return "npm run dev", cwd, port
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


def _free_port():
    """An ephemeral TCP port that's free at the moment of the call. Best-effort:
    a small race remains between closing this probe socket and the server
    binding it (the same trade-off test frameworks like pytest-xdist accept) —
    but it's vastly cheaper than a fixed port (the old default 8000/3000)
    colliding when two verifications run at once (parallel portfolio children
    or build workers each booting a server to verify)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# Paths a generated server's boot command must never be able to write to,
# even though it's LLM-written and running with the operator's own file
# permissions. Deliberately a DENY-list layered on (allow default), not an
# allow-list: an allow-list would need to anticipate every legitimate cache/
# temp/scratch path an arbitrary npm/python/etc. server might touch, and
# getting that wrong would make verification unreliable — worse than
# unsandboxed. This blocks the specific things that actually matter
# (credential theft/tampering, clobbering the engine's own source) without
# constraining anything else.
_SANDBOX_DENY_WRITE_SUBPATHS = (
    "~/.ssh", "~/.aws", "~/.orchestrator", "~/.gnupg", "~/.netrc",
    "~/Library/Keychains",
)


def _sandbox_wrap(cmd_str, write_root=None):
    """Wrap a shell command with macOS sandbox-exec (Seatbelt) so a generated
    server's boot command (or `npm install`/`npm run build`) can't write to
    credentials or the engine's own source, while leaving everything else
    (network, the build_dir itself, language-runtime caches, /tmp) alone so
    legitimate commands still work.

    ``write_root`` — when the thing being verified lives *under* the engine
    directory (e.g. an operator who set the workspace root inside this repo, or
    the sample-run layout), the blanket deny on engine_dir would block a
    legitimate `npm install` writing into build_dir/node_modules. Passing the
    build_dir as write_root appends an explicit write-allow for it *after* the
    deny block (Seatbelt: last matching rule wins), so the project being built
    is always writable while credentials/engine-source stay denied.

    Returns (argv, profile_path_to_clean_up_or_None). Best-effort and never
    raises: on any non-macOS host, a missing sandbox-exec, or a profile that
    can't be written, returns the plain unsandboxed argv unchanged — a failed
    sandbox attempt must never be the reason verification can't run at all."""
    plain = ["/bin/sh", "-lc", cmd_str]
    if not shutil.which("sandbox-exec"):
        return plain, None
    engine_dir = os.path.dirname(os.path.abspath(__file__))
    deny_paths = [os.path.expanduser(p) for p in _SANDBOX_DENY_WRITE_SUBPATHS] + [engine_dir]
    lines = (["(version 1)", "(allow default)", "(deny file-write*"]
             + ['  (subpath "%s")' % p for p in deny_paths]
             + [")"])
    if write_root:
        # Re-allow writes under the project being verified — wins over the deny
        # above because Seatbelt applies the last matching rule.
        lines.append('(allow file-write* (subpath "%s"))' % os.path.abspath(write_root))
    profile = "\n".join(lines)
    try:
        fd, profile_path = tempfile.mkstemp(prefix="verify_sandbox_", suffix=".sb")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(profile)
    except OSError:
        return plain, None
    return ["sandbox-exec", "-f", profile_path, "/bin/sh", "-lc", cmd_str], profile_path


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
    # An explicit spec["port"] is honored as-is; otherwise allocate a free
    # ephemeral port rather than a fixed default (was 8000) so two
    # verifications running at once — parallel portfolio children or build
    # workers — don't collide and one falsely report "did not respond".
    port = int(spec.get("port") or _free_port())
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
    argv, sandbox_profile_path = _sandbox_wrap(start)
    try:
        outfh = open(out_path, "w", encoding="utf-8")
        proc = subprocess.Popen(argv, cwd=cwd, env=env,
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
        if sandbox_profile_path:
            try:
                os.remove(sandbox_profile_path)
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


# ---------------------------------------------------------------------------
# Web / npm build verification (install-then-build). The one thing no path
# above does is actually INSTALL dependencies before building: detect_project
# returns "node" for any package.json, but the old auto route (_verify_shell)
# ran `npm run build --if-present || node -e "process.exit(0)"` with NO install
# — so a deps-having app either couldn't build (node_modules missing) or, worse,
# the `||` swallowed the failure and falsely reported ok=True. This installs,
# then builds, and reports honestly.
#
# Deliberately v1 = install + build only (no Playwright/browser tests): running
# a headless browser suite means a multi-hundred-MB browser download and a much
# larger, flakier surface — a separate follow-on, not the compile-gate this is.
# ---------------------------------------------------------------------------

# Env var names (and name fragments) whose values are secrets an npm install's
# postinstall scripts have no business reading. `npm install` runs arbitrary
# LLM-chosen dependency code; scrubbing these from the child env means a
# malicious/curious postinstall can't read our provider keys straight out of
# the environment. (Reads of ~/.ssh etc. are a separate, documented residual
# risk — see KNOWN_LIMITATIONS; this closes the cheap, high-value hole.)
_SECRET_ENV_FRAGMENTS = ("API_KEY", "TOKEN", "SECRET", "PASSWORD", "PASSWD",
                         "CREDENTIAL", "PRIVATE_KEY", "SESSION")
_SECRET_ENV_PREFIXES = ("AWS_", "ANTHROPIC_", "OPENAI_", "GEMINI_", "GOOGLE_",
                        "GITHUB_", "GH_", "NPM_", "SLACK_", "STRIPE_")


def _is_secret_env(name):
    up = name.upper()
    if any(up.startswith(p) for p in _SECRET_ENV_PREFIXES):
        return True
    return any(frag in up for frag in _SECRET_ENV_FRAGMENTS)


def _npm_env(cache_dir):
    """A hardened environment for `npm`: our own process env MINUS anything
    secret-shaped, PLUS npm quiet/non-interactive flags and a per-verify cache
    dir (so a poisoned package can't persist into the shared ~/.npm across
    runs). Browser-download opt-outs are set defensively even though v1 runs no
    browser tests — a dependency's postinstall must never trigger a huge
    download inside a verification."""
    env = {k: v for k, v in os.environ.items() if not _is_secret_env(k)}
    env["CI"] = "1"
    env["npm_config_cache"] = cache_dir
    env["npm_config_fund"] = "false"
    env["npm_config_audit"] = "false"
    env["npm_config_progress"] = "false"
    env["NO_UPDATE_NOTIFIER"] = "1"
    env["PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD"] = "1"
    env["PUPPETEER_SKIP_DOWNLOAD"] = "1"
    env["PUPPETEER_SKIP_CHROMIUM_DOWNLOAD"] = "true"
    return env


# stderr signatures of an ENVIRONMENTAL install failure (registry unreachable,
# private-registry auth, rate limit) — as opposed to a genuine manifest/
# dependency error in what the agents wrote. Environmental failures are
# ran=False ("unverified"), never a release-blocking ok=False: a network blip
# must not fail a build the toolchain never got to actually judge, exactly as a
# missing toolchain returns ran=False everywhere else in this module.
_NPM_ENV_FAILURE_RE = re.compile(
    r"ENOTFOUND|EAI_AGAIN|ETIMEDOUT|ECONNREFUSED|ECONNRESET|ENETUNREACH|"
    r"network|getaddrinfo|socket hang up|registry|rate.?limit|"
    r"\b401\b|\b403\b|\b429\b|E401|E403|E429|ERR_SOCKET|"
    r"request to .* failed", re.IGNORECASE)


def _run_sandboxed(cmd_str, cwd, timeout, env, write_root):
    """_run under _sandbox_wrap, cleaning up the generated Seatbelt profile
    afterward (like _verify_http does for the boot command)."""
    argv, profile_path = _sandbox_wrap(cmd_str, write_root=write_root)
    try:
        return _run(argv, cwd, timeout, env=env)
    finally:
        if profile_path:
            try:
                os.remove(profile_path)
            except OSError:
                pass


def _nearest_package_json(build_dir):
    hits = _find(build_dir, "package.json")   # shallowest-first, skips node_modules
    return hits[0] if hits else None


def _verify_web(build_dir, spec, timeout):
    """Install deps, then build, for a web/npm project. ran=False (unverified)
    only when npm is absent, there's no package.json, or the install failed for
    an environmental reason (network/auth/timeout). A real dependency/manifest
    error or a failing build script is ran=True/ok=False."""
    spec = spec or {}
    if not shutil.which("npm"):
        return {"ran": False, "ok": False, "tool": "npm",
                "summary": "npm not found — skipping.", "errors": ""}
    pkg = _nearest_package_json(build_dir)
    if not pkg:
        return {"ran": False, "ok": False, "tool": "npm",
                "summary": "no package.json — nothing to build.", "errors": ""}
    cwd = os.path.dirname(pkg)
    try:
        with open(pkg, encoding="utf-8") as fh:
            scripts = (json.load(fh).get("scripts") or {})
    except (OSError, ValueError):
        scripts = {}

    cache_dir = tempfile.mkdtemp(prefix="verify_npm_cache_")
    env = _npm_env(cache_dir)
    try:
        # --- install (plain `npm install`: handles lockfile-present and
        # lockfile-absent alike, and — unlike `npm ci` with a fallback — runs
        # each dependency's postinstall exactly once, not twice). ---
        # Split the budget so a slow install can't starve the build.
        install_timeout = max(60, int(timeout * 0.7))
        code, out, err = _run_sandboxed("npm install", cwd, install_timeout,
                                        env=env, write_root=build_dir)
        if code != 0:
            blob = out + "\n" + err
            if code == 124 or _NPM_ENV_FAILURE_RE.search(blob):
                # Environmental — unverified, not a build failure.
                return {"ran": False, "ok": False, "tool": "npm install",
                        "summary": "npm install could not complete (network/"
                                   "registry/auth/timeout) — unverified.",
                        "errors": _errors_tail(blob)}
            return {"ran": True, "ok": False, "tool": "npm install",
                    "summary": "npm install FAILED (dependency/manifest error)",
                    "errors": _errors_tail(blob)}

        # --- build ---
        if "build" not in scripts:
            return {"ran": True, "ok": True, "tool": "npm install",
                    "summary": "dependencies installed cleanly (no build script "
                               "to run)", "errors": ""}
        build_timeout = max(60, timeout - install_timeout)
        code, out, err = _run_sandboxed("npm run build", cwd, build_timeout,
                                        env=env, write_root=build_dir)
        ok = (code == 0)
        return {"ran": True, "ok": ok, "tool": "npm build",
                "summary": "npm run build succeeded" if ok else "npm run build FAILED",
                "errors": "" if ok else _errors_tail(out + "\n" + err)}
    finally:
        shutil.rmtree(cache_dir, ignore_errors=True)


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
            return _verify_xcode(build_dir, timeout, run_tests=bool(spec.get("run_tests")))
        if vtype in ("swift", "spm"):
            return _verify_spm(build_dir, timeout)
        if vtype in ("http", "server", "boot"):
            return _verify_http(build_dir, spec, timeout)
        if vtype in ("web", "npm", "node"):
            return _verify_web(build_dir, spec, timeout)
        if vtype == "shell":
            return _verify_shell(build_dir, spec.get("command"), timeout)
        # auto
        kind = detect_project(build_dir)
        if kind == "xcode":
            return _verify_xcode(build_dir, timeout)
        if kind == "spm":
            return _verify_spm(build_dir, timeout)
        if kind == "node":
            return _verify_web(build_dir, spec, timeout)
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
        # tz-aware (local + offset), matching events.py, so ordering against
        # events.jsonl stays unambiguous across DST transitions. Consumers
        # (GUI VerificationCard, shepherd.sh) treat this as an opaque string.
        "timestamp": _dt.datetime.now().astimezone().isoformat(timespec="seconds"),
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
        # Additive, purely observational by default (runtime.tests_gate_release,
        # default False, is the only thing that lets these affect the release
        # gate — see _release_gate_failure in orchestrator.py): whether a real
        # `xcodebuild test` action ran, and whether it passed. Both stay
        # False/None for every non-xcodebuild verifier and for xcodebuild runs
        # that didn't request run_tests, so existing consumers of ok/ran are
        # completely unaffected.
        "tests_ran": bool(result.get("tests_ran")),
        "tests_ok": result.get("tests_ok"),
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
