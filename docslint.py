#!/usr/bin/env python3
"""Deterministic provenance lint for enrollment documentation.

Enrollment rebuilds documentation from an existing repository without changing
that repository.  These four tags are the complete provenance vocabulary:

* ``[VERIFIED: <repo-relative-path>]``
* ``[FROM-THEIR-DOCS: <file>]``
* ``[UNVERIFIED]``
* ``[RESEARCH: <source-url-or-name>]``

The linter is deliberately conservative: each prose paragraph needs at least
one exact tag, and every path-bearing tag is independently checked against the
enrolled origin.  A paragraph is never deleted or rewritten; findings are
persisted for the human-facing gap report.
"""

import json
import os
import re
import tempfile


REPORT_RELATIVE_PATH = os.path.join("docs", "provenance_lint.json")

_TAG_RE = re.compile(
    r"\[(VERIFIED|FROM-THEIR-DOCS|RESEARCH):\s*([^\]\n]+?)\]"
    r"|\[UNVERIFIED\]")
_PATH_TAG_RE = re.compile(
    r"\[(VERIFIED|FROM-THEIR-DOCS):\s*([^\]\n]+?)\]")
_FENCE_RE = re.compile(r"^\s*(```|~~~)")
_STRUCTURAL_RE = re.compile(
    r"^\s*(?:#{1,6}\s+|[-*_]{3,}\s*$|<!--|\|?\s*:?-{3,}:?\s*\|)")


def _paragraphs(text):
    """Yield ``(line_number, paragraph)`` for prose outside fenced code.

    Headings, thematic breaks, HTML comments, and Markdown table separators
    are structure rather than factual prose.  List items remain prose and are
    checked.  Consecutive nonblank lines form one paragraph, matching the
    board's explicitly permitted paragraph-level granularity.
    """
    out = []
    current = []
    start = 0
    fenced = False

    def flush():
        if current:
            paragraph = "\n".join(current).strip()
            if paragraph and not _STRUCTURAL_RE.match(paragraph):
                out.append((start, paragraph))
            current[:] = []

    for number, line in enumerate((text or "").splitlines(), 1):
        if _FENCE_RE.match(line):
            flush()
            fenced = not fenced
            continue
        if fenced:
            continue
        if not line.strip():
            flush()
            continue
        if _STRUCTURAL_RE.match(line):
            # Structural lines delimit prose; do not let a heading at the top
            # of a paragraph exempt the factual text immediately beneath it.
            flush()
            continue
        if not current:
            start = number
        current.append(line)
    flush()
    return out


def _existing_target_file(target_root, claimed_path):
    """True only for an existing file contained by ``target_root``."""
    if not target_root or not os.path.isdir(target_root):
        return False
    claimed = (claimed_path or "").strip()
    if not claimed or os.path.isabs(claimed):
        return False
    root = os.path.realpath(target_root)
    resolved = os.path.realpath(os.path.join(root, claimed))
    try:
        contained = os.path.commonpath((root, resolved)) == root
    except ValueError:
        return False
    return contained and os.path.isfile(resolved)


def lint_text(text, target_root, source="doc_rebuild/doc_rebuild.md"):
    """Return a JSON-safe provenance report for rebuilt Markdown."""
    violations = []
    paragraphs = _paragraphs(text)
    for index, (line, paragraph) in enumerate(paragraphs, 1):
        if not _TAG_RE.search(paragraph):
            violations.append({
                "kind": "untagged_paragraph",
                "source": source,
                "paragraph": index,
                "line": line,
                "detail": "factual prose has no enrollment provenance tag",
            })
        # Validate every claimed local citation, even when another valid tag is
        # also present.  Otherwise a fabricated path could hide beside
        # [UNVERIFIED] and pass the lint.
        for match in _PATH_TAG_RE.finditer(paragraph):
            claimed = match.group(2).strip()
            if not _existing_target_file(target_root, claimed):
                violations.append({
                    "kind": "fabricated_citation",
                    "source": source,
                    "paragraph": index,
                    "line": line,
                    "tag": match.group(1),
                    "path": claimed,
                    "detail": "cited path does not name a file inside the enrolled origin",
                })
    return {
        "schema_version": 1,
        "source": source,
        "paragraphs_checked": len(paragraphs),
        "violation_count": len(violations),
        "violations": violations,
    }


def write_report(app_dir, report):
    """Atomically persist a report under the orchestrator project."""
    path = os.path.join(app_dir, REPORT_RELATIVE_PATH)
    parent = os.path.dirname(path)
    os.makedirs(parent, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".provenance-lint-", suffix=".json",
                               dir=parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=2, sort_keys=True)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return path


def load_report(app_dir):
    """Load a persisted report; missing/corrupt data safely means no report."""
    try:
        with open(os.path.join(app_dir, REPORT_RELATIVE_PATH),
                  encoding="utf-8") as fh:
            value = json.load(fh)
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def render_gap_section(report):
    """Render provenance violations for inclusion in ``GAP_REPORT.md``."""
    if not isinstance(report, dict):
        return ""
    violations = report.get("violations")
    if not isinstance(violations, list):
        return ""
    lines = ["## Documentation provenance", ""]
    if not violations:
        lines.append("- None — every rebuilt prose paragraph has provenance, "
                     "and every local citation resolves inside the enrolled origin.")
    else:
        lines.append("- Violations: %d (flagged for human review; content was not deleted)."
                     % len(violations))
        for finding in violations:
            if not isinstance(finding, dict):
                continue
            kind = finding.get("kind") or "provenance_violation"
            where = "%s:%s" % (finding.get("source") or "rebuilt docs",
                                finding.get("line") or "?")
            detail = finding.get("detail") or "provenance violation"
            claimed = ("; cited path: %s" % finding.get("path")) \
                if finding.get("path") else ""
            lines.append("- **%s** at %s — %s%s"
                         % (kind, where, detail, claimed))
    return "\n".join(lines) + "\n"


def violation_count(report):
    """Return the trustworthy count from the findings list, not metadata."""
    violations = report.get("violations") if isinstance(report, dict) else None
    return len(violations) if isinstance(violations, list) else 0
