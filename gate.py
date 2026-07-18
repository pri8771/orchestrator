#!/usr/bin/env python3
"""
gate.py — the artifact pre-push gate (V3 board 4.12).

ONE gate runs where artifacts enter the bus: deterministic user shell hooks
(hooks.json) plus semantic llm_rules guardrails. This module is a clean leaf —
STANDARD LIBRARY + procutil + schemas ONLY, never orchestrator or artifacts — so
the publish path can consult it without an import cycle and it is unit-testable
without an engine. The semantic verdict is produced by an INJECTED
``llm_runner(prompt) -> str`` callable (the orchestrator wires a real agent
turn); this module never imports or invokes an agent itself.

Contract for hook authors (Claude Code-style), documented here so operators
write hooks that behave:
  * stdin is ONE JSON object: {"body": <markdown>, "meta": <proposed artifact>}.
  * exit 0  -> allow the publish.
  * exit 2  -> BLOCK; whatever the hook writes to stderr is fed back (redacted,
               length-capped) into the producing agent's bounded retry.
  * ANY other exit code, a timeout, or a spawn failure -> hook_error: surfaced
               as a visible banner (+ a mistakes line at the call site) but does
               NOT block — a broken hook must never silently wedge every publish.

A `command` may be a shell string (run via `/bin/sh -c`, the affordance
operators expect) or an argv list (run directly). Either way procutil.run_capture
runs it in a fresh process group so a timeout kills the whole tree.
"""

import json
import os
import re
import subprocess

import procutil
import schemas

HOOKS_FILENAME = "hooks.json"
_DEFAULT_HOOK_TIMEOUT = 30
_STDERR_CAP = 2000            # redact THEN cap hook stderr before it enters a prompt

# Anchored exactly like orchestrator.QUALITY_PASS_RE/QUALITY_FAIL_RE so the
# semantic verdict is parsed the same disciplined way the quality gate is.
GATE_PASS_RE = re.compile(r"GATE:\s*PASS", re.IGNORECASE)
GATE_FAIL_RE = re.compile(r"GATE:\s*FAIL", re.IGNORECASE)

FEEDBACK_HEADER = "===== GATE FEEDBACK (fix and re-emit the artifact) ====="


def gate_passed(text):
    """PASS present AND FAIL absent — mirrors orchestrator._quality_passed.
    Empty, unparseable, or both-present all count as NOT passed (fail-closed)."""
    t = text or ""
    return bool(GATE_PASS_RE.search(t)) and not bool(GATE_FAIL_RE.search(t))


# --------------------------------------------------------------------------
# Config loading — seed-less; absent files mean an empty (allow-all) gate,
# a present-but-corrupt file fails CLOSED (unreadable=True + one banner).
# --------------------------------------------------------------------------
def _norm_hook(raw):
    """Normalize one hook entry -> {id, argv, types, sections, timeout}, or None
    when it has no runnable command (the caller reports that, never silent)."""
    if not isinstance(raw, dict):
        return None
    command = raw.get("command")
    if isinstance(command, str) and command.strip():
        argv = ["/bin/sh", "-c", command]
    elif (isinstance(command, list) and command
            and all(isinstance(a, str) for a in command)):
        argv = list(command)
    else:
        return None
    matcher = raw.get("matcher") if isinstance(raw.get("matcher"), dict) else {}
    def _slist(key):
        v = matcher.get(key)
        return [x for x in v if isinstance(x, str)] if isinstance(v, list) else []
    try:
        timeout = int(raw.get("timeout_s") or _DEFAULT_HOOK_TIMEOUT)
    except (TypeError, ValueError):
        timeout = _DEFAULT_HOOK_TIMEOUT
    if timeout <= 0:
        timeout = _DEFAULT_HOOK_TIMEOUT
    return {"id": str(raw.get("id") or "hook"), "argv": argv,
            "types": _slist("artifact_types"), "sections": _slist("sections"),
            "timeout": timeout}


def _norm_rule(raw):
    """Normalize one llm_rule -> {id, rule, applies_to}, or None with no rule."""
    if not isinstance(raw, dict):
        return None
    rule = raw.get("rule")
    if not isinstance(rule, str) or not rule.strip():
        return None
    applies = raw.get("applies_to")
    applies = ([t for t in applies if isinstance(t, str)]
               if isinstance(applies, list) else [])
    return {"id": str(raw.get("id") or "rule"), "rule": rule,
            "applies_to": applies}


def _read_one(path):
    """Parse one hooks.json into (hooks_list, rules_list); raise ValueError on a
    structurally invalid file so the caller marks the whole config unreadable."""
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        raise ValueError("hooks.json must be a JSON object")
    hooks = data.get("hooks") or []
    rules = data.get("llm_rules") or []
    if not isinstance(hooks, list) or not isinstance(rules, list):
        raise ValueError("'hooks' and 'llm_rules' must each be a list")
    return hooks, rules


def load_gate_config(workspace_dir, section_dir, on_error=None):
    """The gate config for a publish: <section_dir>/hooks.json then
    <workspace_dir>/hooks.json, unioned (section policy + project-wide policy;
    both layers run). Returns {"hooks": [...], "llm_rules": [...], "unreadable":
    bool}. Absent files -> empty gate, silent. A present-but-corrupt file ->
    unreadable=True plus ONE on_error banner (fail closed, never silent, never
    half-applied)."""
    if on_error is None:
        on_error = lambda _m: None
    hooks, rules, unreadable = [], [], False
    for base in (section_dir, workspace_dir):
        if not base:
            continue
        path = os.path.join(base, HOOKS_FILENAME)
        if not os.path.exists(path):
            continue
        try:
            raw_hooks, raw_rules = _read_one(path)
        except (OSError, ValueError, TypeError) as exc:
            unreadable = True
            on_error("gate config %s is unreadable (%s) — publishes here "
                     "quarantine until it is fixed" % (path, exc))
            continue
        for raw in raw_hooks:
            n = _norm_hook(raw)
            if n is None:
                on_error("gate config %s: a hook entry has no runnable "
                         "'command' — skipped" % (path,))
            else:
                hooks.append(n)
        for raw in raw_rules:
            n = _norm_rule(raw)
            if n is None:
                on_error("gate config %s: an llm_rule has no 'rule' text — "
                         "skipped" % (path,))
            else:
                rules.append(n)
    return {"hooks": hooks, "llm_rules": rules, "unreadable": unreadable}


def gate_is_active(config):
    """The golden switch: True iff there is anything to enforce. When False the
    publish path takes the byte-identical legacy route (zero subprocess spawns,
    no agent turn)."""
    return bool(config.get("hooks") or config.get("llm_rules")
                or config.get("unreadable"))


# --------------------------------------------------------------------------
# Deterministic hooks
# --------------------------------------------------------------------------
def _hook_matches(hook, artifact_type, section):
    if hook["types"] and artifact_type not in hook["types"]:
        return False
    if hook["sections"] and section not in hook["sections"]:
        return False
    return True


def run_deterministic_hooks(config, artifact_type, section, body, meta,
                            cwd=None, on_error=None):
    """Run every matching hook. Returns {"blocks": [(hook_id, feedback)],
    "errors": [(hook_id, reason)]}. exit 0 = allow, exit 2 = block (stderr
    redacted + capped as feedback); any other exit code / timeout / spawn
    failure = a NON-blocking error surfaced through on_error. Never raises."""
    if on_error is None:
        on_error = lambda _m: None
    result = {"blocks": [], "errors": []}
    payload = json.dumps({"body": body, "meta": meta}, default=str)
    for hook in config.get("hooks", []):
        if not _hook_matches(hook, artifact_type, section):
            continue
        try:
            _out, err, code = procutil.run_capture(
                hook["argv"], cwd=cwd, timeout=hook["timeout"],
                input_text=payload)
        except subprocess.TimeoutExpired:
            result["errors"].append((hook["id"],
                                     "timed out after %ds" % hook["timeout"]))
            on_error("gate hook %r timed out after %ds — NOT blocking, but this "
                     "artifact went unchecked by it"
                     % (hook["id"], hook["timeout"]))
            continue
        except (OSError, ValueError) as exc:
            # ValueError: Popen rejects an argv with an embedded NUL byte (a
            # hooks.json string can smuggle one) — treat it as a spawn failure,
            # never let it escape (the "Never raises" contract above).
            result["errors"].append((hook["id"], "could not run (%s)" % exc))
            on_error("gate hook %r could not run (%s) — NOT blocking, artifact "
                     "unchecked by it" % (hook["id"], exc))
            continue
        if code == 0:
            continue
        if code == 2:
            feedback = schemas.redact_secrets(err or "")[:_STDERR_CAP]
            result["blocks"].append((hook["id"], feedback))
        else:
            result["errors"].append((hook["id"], "exit %s" % code))
            on_error("gate hook %r exited %s (not 0=allow / 2=block) — NOT "
                     "blocking; fix the hook to use the 0/2 contract"
                     % (hook["id"], code))
    return result


# --------------------------------------------------------------------------
# Semantic guardrails (one injected agent turn)
# --------------------------------------------------------------------------
def _rules_prompt(rules, artifact_type, body, meta):
    lines = [
        "You are a strict release gate. Judge whether the artifact below "
        "satisfies EVERY rule. Reply with EXACTLY one line: either",
        "  GATE: PASS",
        "or",
        "  GATE: FAIL — <the specific rule(s) it breaks and why>",
        "",
        "RULES:",
    ]
    for r in rules:
        lines.append("- (%s) %s" % (r["id"], r["rule"]))
    lines += [
        "",
        "ARTIFACT type: %s" % (artifact_type,),
        "ARTIFACT title: %s" % (meta.get("title") if isinstance(meta, dict)
                                else ""),
        "ARTIFACT body:",
        body or "",
    ]
    return "\n".join(lines)


def _fail_reason(verdict):
    """Pull the model's stated reason out of a 'GATE: FAIL — ...' line; fall
    back to a loud 'unparseable' reason when the verdict is neither PASS nor a
    recognizable FAIL (still fail-closed at the caller)."""
    t = (verdict or "").strip()
    m = GATE_FAIL_RE.search(t)
    if m:
        tail = t[m.end():].lstrip(" —-:\t").strip()
        return "rule check failed: %s" % (tail or "no reason given")
    return "gate verdict was unparseable (neither GATE: PASS nor GATE: FAIL)"


def evaluate_llm_rules(config, artifact_type, body, meta, llm_runner,
                       on_error=None):
    """Evaluate the matching llm_rules in ONE injected agent turn. Returns
    {"passed": bool, "reasons": [str]}. No matching rules -> trivially passed,
    no turn spent. A missing runner, a runner exception, or an unparseable
    verdict all count as FAIL (fail-closed, loud)."""
    rules = [r for r in config.get("llm_rules", [])
             if not r["applies_to"] or artifact_type in r["applies_to"]]
    if not rules:
        return {"passed": True, "reasons": []}
    if llm_runner is None:
        return {"passed": False, "reasons": ["no gate evaluator available"]}
    try:
        verdict = llm_runner(_rules_prompt(rules, artifact_type, body, meta))
    except Exception as exc:  # the injected runner must never crash the gate
        return {"passed": False,
                "reasons": ["gate evaluator error (%s)" % exc]}
    if gate_passed(verdict):
        return {"passed": True, "reasons": []}
    return {"passed": False, "reasons": [_fail_reason(verdict)]}


def _wrap_feedback(reasons):
    return FEEDBACK_HEADER + "\n" + "\n".join("- %s" % r for r in reasons)


def gate_block(config, cwd, block, section, llm_runner, on_error=None):
    """Gate ONE proposed artifact block ({type, title, body, ...}). Deterministic
    hooks run first — a hook block short-circuits before any agent turn is
    spent. Returns {"passed": bool, "reasons": [str], "feedback": str}; feedback
    is the GATE FEEDBACK block to append to the producing agent's retry prompt
    (empty when passed). Hook ERRORS are surfaced via on_error and never block."""
    if on_error is None:
        on_error = lambda _m: None
    meta = dict(block) if isinstance(block, dict) else {}
    body = meta.get("body")
    body = body if isinstance(body, str) else ("" if body is None else str(body))
    artifact_type = meta.get("type")
    det = run_deterministic_hooks(config, artifact_type, section, body, meta,
                                  cwd=cwd, on_error=on_error)
    # Hook ERRORS (exit≠0/2, timeout, spawn failure) are non-blocking but are
    # returned so the call site can record the promised mistakes line — a
    # broken/unchecked gate hook must be visible in the ledger, not just as a
    # transient banner.
    errors = list(det["errors"])
    reasons = ["hook %r blocked (exit 2): %s" % (hid, fb or "(no stderr)")
               for hid, fb in det["blocks"]]
    if reasons:
        return {"passed": False, "reasons": reasons, "errors": errors,
                "feedback": _wrap_feedback(reasons)}
    verdict = evaluate_llm_rules(config, artifact_type, body, meta, llm_runner,
                                 on_error=on_error)
    if not verdict["passed"]:
        return {"passed": False, "reasons": verdict["reasons"], "errors": errors,
                "feedback": _wrap_feedback(verdict["reasons"])}
    return {"passed": True, "reasons": [], "errors": errors, "feedback": ""}
