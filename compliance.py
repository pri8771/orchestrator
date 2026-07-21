#!/usr/bin/env python3
"""Enrollment compliance-report contract and deterministic validation."""

import os

import docslint
import schemas


FENCE_TAG = "compliance-json"
VERDICTS = ("compliant", "non-compliant", "not-applicable",
            "cannot-determine")


def ios_rule_areas(orch_dir):
    """Return the exact, stable ids for every shipped iOS knowledge file."""
    base = os.path.join(orch_dir, "knowledge", "ios")
    try:
        names = sorted(name for name in os.listdir(base)
                       if name.endswith(".md")
                       and os.path.isfile(os.path.join(base, name)))
    except OSError:
        return []
    return ["knowledge/ios/%s" % name for name in names]


def validate_findings(findings, target_root=None, expected_rules=None):
    """Validate compliance findings without coercing any verdict.

    When ``target_root`` is supplied, every evidence path must resolve to a
    real file inside the enrolled origin.  When ``expected_rules`` is supplied,
    the report must cover each rule area exactly once.
    """
    errors = []
    if not isinstance(findings, list) or not findings:
        return ["findings must be a non-empty list"]
    seen = set()
    for index, finding in enumerate(findings):
        label = "finding %d" % (index + 1)
        if not isinstance(finding, dict):
            errors.append("%s is not an object" % label)
            continue
        rule = finding.get("rule")
        rule = rule.strip() if isinstance(rule, str) else ""
        if not rule:
            errors.append("%s has no rule area" % label)
        elif rule in seen:
            errors.append("%s duplicates rule area %r" % (label, rule))
        else:
            seen.add(rule)
        verdict = finding.get("verdict")
        if verdict not in VERDICTS:
            errors.append("%s verdict must be one of: %s"
                          % (label, ", ".join(VERDICTS)))
        evidence = finding.get("evidence_paths")
        if not isinstance(evidence, list) or not evidence \
                or any(not isinstance(path, str) or not path.strip()
                       for path in evidence):
            errors.append("%s needs one or more evidence_paths" % label)
        elif target_root is not None:
            for path in evidence:
                if not docslint.existing_target_file(target_root, path):
                    errors.append("%s evidence path does not name a file "
                                  "inside the enrolled origin: %s"
                                  % (label, path))
        why = finding.get("why")
        if not isinstance(why, str) or not why.strip():
            errors.append("%s has no why" % label)
    if expected_rules is not None:
        expected = set(expected_rules)
        missing = sorted(expected - seen)
        unknown = sorted(seen - expected)
        if missing:
            errors.append("missing rule area(s): %s" % ", ".join(missing))
        if unknown:
            errors.append("unknown rule area(s): %s" % ", ".join(unknown))
    return errors


def parse_output(text, target_root, orch_dir):
    """Return ``(report, errors)`` for one fenced compliance contract."""
    parse_errors = []
    blocks = schemas.extract_structured_blocks(
        text or "", FENCE_TAG, required_fields=["findings"],
        on_error=parse_errors.append)
    if len(blocks) != 1:
        parse_errors.append("expected exactly one fenced ```%s``` block; got %d"
                            % (FENCE_TAG, len(blocks)))
        return None, parse_errors
    report = blocks[0]
    errors = validate_findings(
        report.get("findings"), target_root=target_root,
        expected_rules=ios_rule_areas(orch_dir))
    return (report if not errors else None), parse_errors + errors


def prompt_contract(orch_dir):
    """Machine contract with the exact current rule-area ids."""
    areas = ios_rule_areas(orch_dir)
    return (
        "COMPLIANCE MACHINE CONTRACT (required): audit EVERY rule area listed "
        "below. In the coordinator wrap-up emit exactly ONE fenced "
        "```compliance-json``` block containing "
        '{"findings": [{"rule": "knowledge/ios/<file>.md", "verdict": '
        '"compliant|non-compliant|not-applicable|cannot-determine", '
        '"evidence_paths": ["repo/relative/file"], "why": "how the observed '
        'evidence supports this exact verdict"}]}. Every finding needs at least '
        "one REAL repo-relative evidence file, including not-applicable and "
        "cannot-determine findings. Never coerce cannot-determine into pass or "
        "fail. Rule ids (one finding each):\n- " + "\n- ".join(areas) + "\n")


def render_report(findings):
    """Render a deterministic human-readable artifact body."""
    counts = {verdict: 0 for verdict in VERDICTS}
    for finding in findings:
        counts[finding["verdict"]] += 1
    lines = ["# Enrollment Compliance Report", "",
             "Verdicts remain distinct; `cannot-determine` is not counted as "
             "compliant or non-compliant.", "", "## Verdict counts", ""]
    for verdict in VERDICTS:
        lines.append("- %s: %d" % (verdict, counts[verdict]))
    lines.extend(["", "## Findings", ""])
    for finding in findings:
        lines.extend([
            "### %s" % finding["rule"], "",
            "- Verdict: **%s**" % finding["verdict"],
            "- Evidence: %s" % ", ".join(
                "`%s`" % path for path in finding["evidence_paths"]),
            "- Why: %s" % finding["why"], "",
        ])
    return "\n".join(lines).rstrip() + "\n"
