#!/usr/bin/env python3
"""Read-only intake helpers for adopting an existing codebase.

This module may read the enrolled origin, but every write is rooted in the
orchestrator workspace supplied by the caller.  Keeping that boundary in a
small stdlib-only leaf makes it possible to test independently of the engine.
"""

import json
import os
import re
import shutil
import subprocess
import time


class EnrollError(ValueError):
    """A safe, operator-actionable enrollment refusal."""


_SOURCE_EXTENSIONS = {
    ".c", ".cc", ".cpp", ".cs", ".css", ".go", ".h", ".hpp", ".html",
    ".java", ".js", ".jsx", ".kt", ".kts", ".m", ".md", ".mm", ".php",
    ".py", ".rb", ".rs", ".sh", ".sql", ".swift", ".ts", ".tsx", ".vue",
}
_SKIP_DIRS = {
    ".git", ".build", ".gradle", ".idea", ".next", ".swiftpm", ".venv",
    "DerivedData", "Pods", "build", "dist", "node_modules", "vendor",
}


def slugify(name):
    """Turn a directory basename into the engine's plain project slug shape."""
    value = re.sub(r"[^a-z0-9-]+", "-", str(name or "").strip().lower())
    value = re.sub(r"-+", "-", value).strip("-")[:80].rstrip("-")
    return value or "enrolled-project"


def valid_slug(value):
    text = str(value or "")
    return bool(text) and len(text) <= 80 and not text.startswith(".") \
        and ".." not in text and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", text) is not None


def _overlaps_workspace(source, workspace):
    """True when either tree contains the other.

    The second direction matters too: enrolling a parent of the workspace
    would make every subsequent orchestrator write a write into the origin.
    """
    try:
        common = os.path.commonpath([source, workspace])
    except ValueError:
        return False
    return common in (source, workspace)


def _git_root(path):
    try:
        proc = subprocess.run(
            ["git", "-C", path, "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=10, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0 or not proc.stdout.strip():
        return None
    return os.path.realpath(proc.stdout.strip())


def _top_level_inventory(source):
    items = []
    try:
        entries = sorted(os.scandir(source), key=lambda ent: ent.name.lower())
    except OSError as exc:
        raise EnrollError("enroll target is not readable: %s" % exc)
    for entry in entries:
        try:
            if entry.is_symlink():
                kind = "symlink (not followed)"
                size = None
            elif entry.is_dir(follow_symlinks=False):
                kind = "directory"
                size = None
            elif entry.is_file(follow_symlinks=False):
                kind = "file"
                size = entry.stat(follow_symlinks=False).st_size
            else:
                kind = "other"
                size = None
        except OSError:
            kind = "unreadable entry"
            size = None
        items.append({"name": entry.name, "kind": kind, "bytes": size})
    return items


def _project_markers(source, inventory):
    names = {item["name"] for item in inventory}
    lower = {name.lower(): name for name in names}
    found = []
    for name in sorted(names):
        if name.endswith(".xcodeproj"):
            found.append(("Xcode project", name))
        elif name.endswith(".xcworkspace"):
            found.append(("Xcode workspace", name))
    exact = (
        ("Package.swift", "Swift package"),
        ("pyproject.toml", "Python project"),
        ("package.json", "JavaScript/TypeScript package"),
        ("Cargo.toml", "Rust project"),
        ("go.mod", "Go module"),
        ("build.gradle", "Gradle project"),
        ("build.gradle.kts", "Gradle project"),
    )
    for marker, label in exact:
        actual = lower.get(marker.lower())
        if actual:
            found.append((label, actual))
    for name in sorted(names):
        if name.endswith(".sln"):
            found.append((".NET solution", name))
    return found


def _readme_excerpt(source, inventory, limit=50):
    candidates = [item["name"] for item in inventory
                  if item["kind"] == "file"
                  and item["name"].lower().startswith("readme")]
    if not candidates:
        return None, []
    name = sorted(candidates, key=lambda value: (value.lower() != "readme.md",
                                                 value.lower()))[0]
    path = os.path.join(source, name)
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            lines = []
            for _ in range(limit):
                line = fh.readline()
                if line == "":
                    break
                lines.append(line.rstrip("\n\r"))
    except OSError:
        return name, None
    return name, lines


def _line_counts(source):
    counts = {}
    skipped = 0
    for dirpath, dirnames, filenames in os.walk(source, followlinks=False):
        dirnames[:] = sorted(
            name for name in dirnames
            if name not in _SKIP_DIRS
            and not os.path.islink(os.path.join(dirpath, name)))
        for filename in sorted(filenames):
            path = os.path.join(dirpath, filename)
            if os.path.islink(path):
                continue
            ext = os.path.splitext(filename)[1].lower()
            if ext not in _SOURCE_EXTENSIONS:
                continue
            try:
                with open(path, encoding="utf-8", errors="replace") as fh:
                    lines = sum(1 for _line in fh)
            except OSError:
                skipped += 1
                continue
            rec = counts.setdefault(ext, {"files": 0, "lines": 0})
            rec["files"] += 1
            rec["lines"] += lines
    return counts, skipped


def inspect_origin(source):
    """Return only directly observed intake facts; never model-generated prose."""
    absolute = os.path.realpath(os.path.expanduser(str(source or "")))
    if not absolute or not os.path.isdir(absolute):
        raise EnrollError("enroll target must be an existing directory")
    inventory = _top_level_inventory(absolute)
    readme_name, readme_lines = _readme_excerpt(absolute, inventory)
    counts, skipped = _line_counts(absolute)
    markers = _project_markers(absolute, inventory)
    git_root = _git_root(absolute)
    return {
        "source": absolute,
        "inventory": inventory,
        "markers": markers,
        "readme_name": readme_name,
        "readme_lines": readme_lines,
        "line_counts": counts,
        "unreadable_source_files": skipped,
        "is_git_root": git_root == absolute,
    }


def render_initial_prompt(facts):
    """Render a deterministic, observation-only enrollment brief."""
    out = [
        "# Enrollment intake — observed facts only",
        "",
        "The enrolled origin is read-only. All generated work belongs in this orchestrator project.",
        "",
        "## Origin",
        "",
        "- Absolute path: `%s`" % facts["source"],
        "- Git repository root: `%s`" % ("yes" if facts["is_git_root"] else "no"),
        "",
        "## Detected project type",
        "",
    ]
    if facts["markers"]:
        for label, marker in facts["markers"]:
            out.append("- %s (observed marker: `%s`)" % (label, marker))
    else:
        out.append("- Unknown: no recognized top-level project marker was observed.")
    out.extend(["", "## Top-level inventory", ""])
    if facts["inventory"]:
        for item in facts["inventory"]:
            suffix = ", %d bytes" % item["bytes"] if item["bytes"] is not None else ""
            out.append("- `%s` — %s%s" % (item["name"], item["kind"], suffix))
    else:
        out.append("- The directory is empty.")
    out.extend(["", "## README excerpt", ""])
    if facts["readme_name"] is None:
        out.append("No top-level README file was observed.")
    elif facts["readme_lines"] is None:
        out.append("The top-level README `%s` was observed but could not be read."
                   % facts["readme_name"])
    else:
        out.append("Quoted verbatim from the first up to 50 lines. "
                   "[FROM-THEIR-DOCS: %s]" % facts["readme_name"])
        out.append("")
        if facts["readme_lines"]:
            out.extend("> " + line if line else ">" for line in facts["readme_lines"])
        else:
            out.append("> (empty file)")
    out.extend([
        "", "## Source line counts", "",
        "Observed newline-delimited lines in recognized source/document files; "
        "dependency, build, VCS, and symlinked directories were not traversed.", "",
    ])
    if facts["line_counts"]:
        for ext, rec in sorted(facts["line_counts"].items()):
            out.append("- `%s`: %d line(s) across %d file(s)" %
                       (ext, rec["lines"], rec["files"]))
    else:
        out.append("- No files with recognized source/document extensions were observed.")
    if facts["unreadable_source_files"]:
        out.append("- %d recognized source file(s) could not be read."
                   % facts["unreadable_source_files"])
    return "\n".join(out).rstrip() + "\n"


def scaffold(workspace, source, name=None):
    """Create one enroll project without ever writing inside ``source``."""
    root = os.path.realpath(os.path.expanduser(str(workspace or "")))
    origin = os.path.realpath(os.path.expanduser(str(source or "")))
    if not root:
        raise EnrollError("workspace root is required")
    if not os.path.isdir(origin):
        raise EnrollError("enroll target must be an existing directory")
    if _overlaps_workspace(origin, root):
        raise EnrollError("enroll target and workspace must be disjoint read-only/write trees")
    slug = str(name) if name is not None else slugify(os.path.basename(origin))
    if not valid_slug(slug):
        raise EnrollError("invalid enrollment name %r; use letters, numbers, '-' or '_'" % slug)

    facts = inspect_origin(origin)
    os.makedirs(root, exist_ok=True)
    app_dir = os.path.join(root, slug)
    try:
        os.mkdir(app_dir)
    except FileExistsError:
        raise EnrollError("project %r already exists; choose a different --name" % slug)
    except OSError as exc:
        raise EnrollError("cannot create enrollment project %r: %s" % (slug, exc))
    try:
        os.makedirs(os.path.join(app_dir, "initial_prompt"))
        with open(os.path.join(app_dir, "target_path.txt"), "w", encoding="utf-8") as fh:
            fh.write(origin + "\n")
        with open(os.path.join(app_dir, "workflow.txt"), "w", encoding="utf-8") as fh:
            fh.write("enroll\n")
        with open(os.path.join(app_dir, "initial_prompt", "initial_prompt.md"),
                  "w", encoding="utf-8") as fh:
            fh.write(render_initial_prompt(facts))
    except BaseException:
        shutil.rmtree(app_dir, ignore_errors=True)
        raise
    warnings = []
    if not facts["is_git_root"]:
        warnings.append("target is not a Git repository root; promotion will snapshot-copy it")
    return {"slug": slug, "app_dir": app_dir, "facts": facts, "warnings": warnings}


def _run_checked(argv, cwd=None, timeout=120):
    try:
        proc = subprocess.run(argv, cwd=cwd, capture_output=True, text=True,
                              timeout=timeout, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        raise EnrollError("%s failed: %s" % (argv[0], exc))
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "exit %d" % proc.returncode).strip()
        raise EnrollError("%s failed: %s" % (" ".join(argv[:3]), detail))
    return proc.stdout.strip()


def _assert_no_origin_links(clone_root, origin):
    """Refuse clone symlinks that would turn clone writes into origin writes."""
    for dirpath, dirnames, filenames in os.walk(clone_root, followlinks=False):
        for name in dirnames + filenames:
            path = os.path.join(dirpath, name)
            if not os.path.islink(path):
                continue
            target = os.path.realpath(path)
            try:
                inside_origin = os.path.commonpath([target, origin]) == origin
            except ValueError:
                inside_origin = False
            if inside_origin:
                raise EnrollError(
                    "clone contains a symlink back into the read-only origin: %s"
                    % os.path.relpath(path, clone_root))


def prepare_writable_clone(app_dir, source, slug):
    """Materialize ``app_build`` without ever writing into ``source``.

    The clone/copy is built under a private sibling and renamed into place
    only after Git initialization and branch creation succeed.  A failed
    promotion therefore leaves no runnable partial build tree.
    """
    project = os.path.realpath(os.path.expanduser(str(app_dir or "")))
    origin = os.path.realpath(os.path.expanduser(str(source or "")))
    if not os.path.isdir(project):
        raise EnrollError("enrollment project directory does not exist")
    if not os.path.isdir(origin):
        raise EnrollError("enrolled origin directory does not exist")
    if _overlaps_workspace(origin, project):
        raise EnrollError("enrolled origin and project must remain disjoint")
    if not valid_slug(slug):
        raise EnrollError("invalid enrollment slug %r" % slug)
    destination = os.path.join(project, "app_build")
    staging = os.path.join(project, ".app_build.enroll.tmp")
    if os.path.lexists(destination):
        marker = os.path.join(destination, ".git", "orchestrator-enroll.json")
        try:
            with open(marker, encoding="utf-8") as fh:
                existing = json.load(fh)
        except (OSError, ValueError, TypeError):
            existing = None
        expected_branch = "enroll/%s" % slug
        try:
            current_branch = (_run_checked(
                ["git", "branch", "--show-current"], cwd=destination)
                if not os.path.islink(destination) else None)
        except EnrollError:
            current_branch = None
        if isinstance(existing, dict) and existing.get("source") == origin \
                and existing.get("slug") == slug \
                and current_branch == expected_branch:
            _assert_no_origin_links(destination, origin)
            return {"path": destination, "branch": "enroll/%s" % slug,
                    "source": origin, "created_at": existing.get("created_at")}
        raise EnrollError("app_build already exists; refusing to overwrite it")
    if os.path.lexists(staging):
        raise EnrollError("a prior clone staging directory exists; inspect %s" % staging)

    try:
        if _git_root(origin) == origin:
            _run_checked(["git", "clone", "--no-hardlinks", "--", origin, staging])
        else:
            os.mkdir(staging)
            _run_checked(["rsync", "-a", "--", origin.rstrip(os.sep) + os.sep,
                          staging.rstrip(os.sep) + os.sep])
            _run_checked(["git", "init", "-q"], cwd=staging)
            _run_checked(["git", "config", "user.email", "orchestrator@local"],
                         cwd=staging)
            _run_checked(["git", "config", "user.name", "Orchestrator"],
                         cwd=staging)
            _run_checked(["git", "add", "-A"], cwd=staging)
            stamp = int(os.path.getmtime(origin))
            message = "enrolled snapshot of %s at mtime-%d" % (origin, stamp)
            _run_checked(["git", "commit", "-q", "--allow-empty", "-m", message],
                         cwd=staging)
        _assert_no_origin_links(staging, origin)
        branch = "enroll/%s" % slug
        _run_checked(["git", "checkout", "-q", "-B", branch],
                     cwd=staging)
        created_at = time.strftime("%Y-%m-%d %H:%M:%S")
        marker = os.path.join(staging, ".git", "orchestrator-enroll.json")
        with open(marker, "w", encoding="utf-8") as fh:
            json.dump({"source": origin, "slug": slug,
                       "created_at": created_at}, fh, sort_keys=True)
            fh.write("\n")
        # A concurrent writer does not get displaced, even if it created only
        # an empty directory after our initial preflight.
        if os.path.lexists(destination):
            raise EnrollError("app_build appeared during clone; refusing to overwrite it")
        os.replace(staging, destination)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return {
        "path": destination,
        "branch": branch,
        "source": origin,
        "created_at": created_at,
    }
