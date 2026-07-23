#!/usr/bin/env python3
"""
Deterministic design + dependency lint (task #2 / #4): zero-token,
machine-checked enforcement of the rules the build phase is already
instructed to follow. Runs over app_build/ Swift sources after the release
gate; violations of the hard rules route into the bounded repair loop with
file:line findings, softer signals are warnings.

Hard rules (errors — the same rules phase_rules.json mandates):
  * inline_color      Color(red:/UIColor(red:/#colorLiteral outside DesignSystem*.swift
  * raw_font_size     .font(.system(size: outside DesignSystem*.swift
  * banned_package    an SPM dependency named in tech_stack.json "banned"

Soft signals (warnings):
  * missing_design_system   >= 3 Swift files but no DesignSystem*.swift
  * todo_marker              TODO/FIXME left in source
  * empty_action            a control with an empty action closure — the
                             Rulebook §16 "fake feature" (decorative control)
  * unlisted_package         a third-party SPM dependency not in "allowed"
                             (strict mode promotes this to an error)

tech_stack.json (next to the engine) is the approved-library registry:
  {"allowed": [{"name": "...", "url_contains": "...", "for": "..."}],
   "banned": [{"name": "...", "why": "..."}], "notes": "..."}

Everything is best-effort: unreadable files/registry degrade to no findings.
"""

import json
import os
import re

import buildpolicy as buildpolicylib

TECH_STACK_FILENAME = "tech_stack.json"

_INLINE_COLOR = re.compile(r"Color\s*\(\s*red\s*:|UIColor\s*\(\s*red\s*:|#colorLiteral\s*\(")
# Only a genuine numeric literal after `size:` is the violation — a
# DesignSystem token reference (e.g. `size: DS.IconSize.tab`) starts with a
# letter/underscore and must not false-positive here (observed live: this
# exact shape burned two repair rounds on Aura chasing an already-correct
# call site before the distinction was added).
_RAW_FONT = re.compile(
    r"\.font\s*\(\s*\.system\s*\(\s*size\s*:\s*[-+]?\.?\d")
_TODO = re.compile(r"//\s*(TODO|FIXME)\b", re.IGNORECASE)
# Rulebook §16 (fake-feature prohibition): a control whose action is an empty
# closure is decorative masquerading as functional. Two single-line shapes:
# an empty `action: {}` argument, or a Button with an empty trailing `{ }` body.
# Soft signal (warning) — an intentional placeholder should be *visibly
# disabled with a reason*, which this flags rather than silently allows.
_EMPTY_ACTION = re.compile(
    r"action\s*:\s*\{\s*\}"
    r"|Button\s*\([^{}]*\)\s*\{\s*\}")
_SPM_URL = re.compile(r"\.package\s*\(\s*url\s*:\s*\"([^\"]+)\"")
_SPM_NAME = re.compile(r"\.package\s*\(\s*name\s*:\s*\"([^\"]+)\"")


def load_tech_stack(here):
    """Active Build-target stack, with the fleet-neutral file as legacy.

    V3 8.4 moved platform dependencies under ``sections/build``.  A checkout
    without Sections remains compatible with the old registry shape.
    """
    out = {"allowed": [], "banned": [], "notes": ""}
    policy = buildpolicylib.load_target_policy(here, "app")
    if policy is not None:
        data = policy.get("tech_stack", {})
    else:
        try:
            with open(os.path.join(here, TECH_STACK_FILENAME),
                      encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return out
    if isinstance(data, dict):
        for k in ("allowed", "banned"):
            v = data.get(k)
            if isinstance(v, list):
                out[k] = [e for e in v if isinstance(e, dict) and e.get("name")]
        out["notes"] = str(data.get("notes") or "")
    return out


def render_tech_stack(stack):
    """Human block spliced into tech-spec/build context ('' when empty)."""
    if not stack["allowed"] and not stack["banned"]:
        return ""
    lines = ["===== APPROVED TECH STACK (tech_stack.json — binding) ====="]
    if stack["allowed"]:
        lines.append("Allowed third-party packages (anything else needs justification):")
        for e in stack["allowed"]:
            lines.append("  - %s%s" % (e["name"],
                                       (" — " + e["for"]) if e.get("for") else ""))
    if stack["banned"]:
        lines.append("BANNED packages (never add these):")
        for e in stack["banned"]:
            lines.append("  - %s%s" % (e["name"],
                                       (" — " + e["why"]) if e.get("why") else ""))
    if stack["notes"]:
        lines.append(stack["notes"])
    return "\n".join(lines)


def _swift_files(build_dir):
    out = []
    for dp, dns, fns in os.walk(build_dir):
        dns[:] = [d for d in dns
                  if d not in (".git", ".build", "DerivedData", "Pods")]
        for fn in fns:
            if fn.endswith(".swift"):
                out.append(os.path.join(dp, fn))
    return out


def _is_design_system(path):
    return os.path.basename(path).lower().startswith("designsystem")


def _code_portion(line, in_block):
    """The line with Swift comments and string LITERALS blanked out, so the
    code-token checks (inline_color / raw_font_size / empty_action) match REAL
    code — not a forbidden pattern that merely appears in a doc comment or a
    string. Observed live: `.font(.system(size:))` written inside a `///`
    comment that documented AVOIDING it hard-failed a clean build. Handles
    `//` line comments, `/* */` block comments (across lines via in_block), and
    "..." strings with \\" escapes. Best-effort — triple-quoted strings and
    string interpolation are rare for these tokens; a miss degrades toward the
    pre-existing over-matching, never a crash. Returns (code, in_block_after).
    Never strips a real violation: only comment and string bytes are removed,
    and neither is ever executable code."""
    out = []
    i, n = 0, len(line)
    in_str = False
    while i < n:
        two = line[i:i + 2]
        if in_block:
            if two == "*/":
                in_block = False
                i += 2
                continue
            i += 1
            continue
        if in_str:
            c = line[i]
            if c == "\\":       # skip the escaped character (e.g. \")
                i += 2
                continue
            if c == '"':
                in_str = False
            i += 1
            continue
        if two == "//":         # line comment: the rest is not code
            break
        if two == "/*":
            in_block = True
            i += 2
            continue
        if line[i] == '"':
            in_str = True
            i += 1
            continue
        out.append(line[i])
        i += 1
    return "".join(out), in_block


def scan(build_dir, here):
    """Lint app_build. Returns (errors, warnings): lists of
    {rule, file, line, detail} with repo-relative paths. Never raises."""
    errors, warnings = [], []
    if not os.path.isdir(build_dir):
        return errors, warnings
    files = _swift_files(build_dir)
    has_ds = any(_is_design_system(p) for p in files)

    for path in files:
        rel = os.path.relpath(path, build_dir)
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                lines = fh.read().splitlines()
        except OSError:
            continue
        ds_file = _is_design_system(path)
        # Tests/previews may hardcode fixtures; only product source is held
        # to the token rules.
        test_file = "test" in rel.lower() or "preview" in rel.lower()
        in_block = False   # /* */ comment state, carried across lines per file
        for i, line in enumerate(lines, 1):
            # Code-token checks match the CODE portion only (comments + string
            # literals stripped); todo_marker keeps the raw line because a TODO
            # lives in a comment by definition.
            code, in_block = _code_portion(line, in_block)
            if not ds_file and not test_file:
                if _INLINE_COLOR.search(code):
                    errors.append({"rule": "inline_color", "file": rel,
                                   "line": i,
                                   "detail": "hardcoded color — use a "
                                             "DesignSystem token"})
                if _RAW_FONT.search(code):
                    errors.append({"rule": "raw_font_size", "file": rel,
                                   "line": i,
                                   "detail": "raw font size — use the "
                                             "DesignSystem type ramp"})
            if _TODO.search(line):
                warnings.append({"rule": "todo_marker", "file": rel,
                                 "line": i, "detail": line.strip()[:100]})
            if not test_file and _EMPTY_ACTION.search(code):
                warnings.append({"rule": "empty_action", "file": rel,
                                 "line": i,
                                 "detail": "control with an empty action — "
                                           "implement it or visibly disable it "
                                           "with a reason (%s)" % line.strip()[:60]})

    if len(files) >= 3 and not has_ds:
        warnings.append({"rule": "missing_design_system", "file": "app_build/",
                         "line": 0,
                         "detail": "no DesignSystem*.swift — the design "
                                   "handoff's token spec was never realized"})

    # Letterboxing hard error: an iOS app target with NO launch-screen
    # configuration anywhere (UILaunchScreen/UILaunchStoryboardName in an
    # Info.plist, an INFOPLIST_KEY_UILaunchScreen* build setting, or a
    # LaunchScreen storyboard) renders in a legacy-sized, letterboxed window.
    # Observed live: a 630pt-tall window on a 912pt screen cropped the timer
    # screen's Start button clean off the bottom — declared flows failed and
    # screens graded BAD while the Swift code itself was fine. Deterministic,
    # so gated hard (M-004 in knowledge/ios/observed-mistakes.md).
    has_xcodeproj = False
    launch_ok = False
    plists = []
    for dirpath, dirnames, filenames in os.walk(build_dir):
        dirnames[:] = [d for d in dirnames
                       if d not in (".git", "DerivedData", ".dd", ".build",
                                    "node_modules")]
        for d in dirnames:
            if d.endswith(".xcodeproj"):
                has_xcodeproj = True
        for fn in filenames:
            p = os.path.join(dirpath, fn)
            low = os.path.relpath(p, build_dir).lower()
            if fn == "Info.plist" and "test" not in low:
                plists.append(p)
            if fn.startswith("LaunchScreen") and fn.endswith(".storyboard"):
                launch_ok = True
            if fn in ("project.pbxproj", "project.yml"):
                try:
                    with open(p, encoding="utf-8", errors="replace") as fh:
                        if "UILaunchScreen" in fh.read():
                            launch_ok = True
                except OSError:
                    pass
    if has_xcodeproj and not launch_ok:
        for p in plists:
            try:
                with open(p, "rb") as fh:
                    data = fh.read()
            except OSError:
                continue
            if b"UILaunchScreen" in data or b"UILaunchStoryboardName" in data:
                launch_ok = True
                break
        if not launch_ok:
            where = os.path.relpath(plists[0], build_dir) if plists \
                else "app_build/"
            errors.append({"rule": "missing_launch_screen", "file": where,
                           "line": 0,
                           "detail": "no UILaunchScreen/UILaunchStoryboardName "
                                     "anywhere — iOS letterboxes the app into a "
                                     "legacy-size window, cropping layouts (add "
                                     "UILaunchScreen to the app target's "
                                     "Info.plist)"})

    stack = load_tech_stack(here)
    banned = {e["name"].lower() for e in stack["banned"]}
    allowed = {e["name"].lower() for e in stack["allowed"]} \
        | {(e.get("url_contains") or "").lower() for e in stack["allowed"]}
    for path in files:
        if os.path.basename(path) != "Package.swift":
            continue
        rel = os.path.relpath(path, build_dir)
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except OSError:
            continue
        deps = _SPM_URL.findall(text) + _SPM_NAME.findall(text)
        for dep in deps:
            low = dep.lower()
            name = low.rstrip("/").rsplit("/", 1)[-1].replace(".git", "")
            if any(b and b in low for b in banned) or name in banned:
                errors.append({"rule": "banned_package", "file": rel,
                               "line": 0, "detail": dep})
            elif stack["allowed"] and not any(a and a in low for a in allowed):
                warnings.append({"rule": "unlisted_package", "file": rel,
                                 "line": 0,
                                 "detail": "%s — not in tech_stack.json "
                                           "allowed list" % dep})
    return errors, warnings


def run_design_lint(cfg, cget, emit, app, app_dir, here):
    """The gate. Returns a reason string when hard rules are violated (the
    caller routes it into the repair loop with the findings persisted to
    docs/design_lint.json), else None."""
    if not bool(cget(cfg, "runtime.design_lint_enabled", True)):
        return None
    build_dir = os.path.join(app_dir, "app_build")
    if not os.path.isdir(build_dir):
        return None
    try:
        errors, warnings = scan(build_dir, here)
        if bool(cget(cfg, "runtime.design_lint_strict", False)):
            errors = errors + [w for w in warnings
                               if w["rule"] == "unlisted_package"]
            warnings = [w for w in warnings if w["rule"] != "unlisted_package"]
        try:
            os.makedirs(os.path.join(app_dir, "docs"), exist_ok=True)
            with open(os.path.join(app_dir, "docs", "design_lint.json"), "w",
                      encoding="utf-8") as fh:
                json.dump({"errors": errors, "warnings": warnings}, fh,
                          indent=2)
        except OSError:
            pass
        for w in warnings[:5]:
            emit("Design lint WARN: %s %s:%s %s"
                 % (w["rule"], w["file"], w["line"], w["detail"]))
        if errors:
            head = errors[0]
            reason = ("%d design/dependency lint error(s) — e.g. %s at %s:%s "
                      "(%s). Full list in docs/design_lint.json"
                      % (len(errors), head["rule"], head["file"],
                         head["line"], head["detail"]))
            emit("App '%s': DESIGN LINT FAILED — %s" % (app, reason))
            return reason
        emit("App '%s': design lint PASS (%d warning(s))."
             % (app, len(warnings)))
        return None
    except Exception as exc:  # noqa: BLE001 - the gate must never kill a run
        emit("WARN design lint skipped (%s)." % exc)
        return None
