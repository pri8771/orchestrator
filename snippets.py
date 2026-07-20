"""V3 9.7 saved-snippet data loader.

Layers merge by snippet name in fleet -> section -> project order. A missing
layer is normal; a corrupt layer warns and contributes nothing, so one bad
file never blanks the other scopes. The fleet seed lives at
``library/snippets.json`` and is created only when absent — disk always wins.

This is a stdlib-only leaf. Composer insertion and typed-variable controls
remain GUI concerns; this module owns only the shared data contract.
"""
import copy
import json
import os
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SNIPPETS_FILENAME = "snippets.json"
VARIABLE_TYPES = ("string", "number", "boolean", "choice")

DEFAULT_SNIPPETS = [
    {"name": "audit-this", "phase": "",
     "text": "Audit this for correctness, risks, and missing evidence, "
             "focusing on {{focus}}.",
     "variables": [{"name": "focus", "label": "Focus", "type": "string",
                    "required": False}]},
    {"name": "poke-holes", "phase": "",
     "text": "Poke holes in this proposal and name the strongest "
             "counterargument."},
    {"name": "simplify", "phase": "",
     "text": "Simplify this without losing the important constraints or "
             "edge cases."},
    {"name": "devils-advocate", "phase": "",
     "text": "Take the devil's-advocate position and make the best case "
             "against this."},
    {"name": "whats-missing", "phase": "",
     "text": "What is missing, assumed, or insufficiently supported here?"},
    {"name": "summarize-decisions", "phase": "",
     "text": "Summarize the decisions, rationale, owners, and unresolved "
             "questions."},
    {"name": "make-it-concrete", "phase": "",
     "text": "Make this concrete with specific steps, examples, and "
             "acceptance criteria."},
    {"name": "ship-check", "phase": "",
     "text": "Run a ship-readiness check focused on {{focus}} and list only "
             "actionable blockers.",
     "variables": [{"name": "focus", "label": "Focus", "type": "string",
                    "required": False}]},
]


def _warn(on_warn, message):
    if on_warn:
        on_warn("snippets: %s" % message)


def fleet_path(orch_dir=None):
    return os.path.join(os.path.abspath(orch_dir or HERE), "library",
                        SNIPPETS_FILENAME)


def section_path(orch_dir, section):
    if not section:
        return None
    return os.path.join(os.path.abspath(orch_dir), "sections", section,
                        SNIPPETS_FILENAME)


def project_path(project_dir):
    if not project_dir:
        return None
    return os.path.join(os.path.abspath(project_dir), SNIPPETS_FILENAME)


def _valid_name(value):
    return isinstance(value, str) and bool(value.strip())


def _normalize_variable(raw):
    if not isinstance(raw, dict) or not _valid_name(raw.get("name")):
        return None
    kind = raw.get("type")
    if kind not in VARIABLE_TYPES:
        return None
    options = raw.get("options", [])
    if not isinstance(options, list) or not all(
            isinstance(item, str) and item for item in options):
        return None
    if kind == "choice" and not options:
        return None
    required = raw.get("required", False)
    if not isinstance(required, bool):
        return None
    default = raw.get("default")
    if default is not None and not isinstance(default, (str, int, float, bool)):
        return None
    out = {"name": raw["name"],
           "label": raw.get("label") if isinstance(raw.get("label"), str)
           else raw["name"],
           "type": kind, "required": required}
    if options:
        out["options"] = list(options)
    if default is not None:
        out["default"] = default
    return out


def _normalize_snippet(raw, source, on_warn):
    if not isinstance(raw, dict) or not _valid_name(raw.get("name")) \
            or not isinstance(raw.get("text"), str) or not raw["text"]:
        _warn(on_warn, "%s: invalid snippet entry — skipped" % source)
        return None
    out = {"name": raw["name"],
           "phase": raw.get("phase") if isinstance(raw.get("phase"), str)
           else "",
           "text": raw["text"]}
    if "variables" not in raw:
        return out
    variables = raw.get("variables")
    if not isinstance(variables, list):
        _warn(on_warn, "%s: snippet %r has malformed variables — loaded as "
              "plain text" % (source, raw["name"]))
        return out
    normalized = [_normalize_variable(item) for item in variables]
    if any(item is None for item in normalized):
        _warn(on_warn, "%s: snippet %r has malformed variables — loaded as "
              "plain text" % (source, raw["name"]))
        return out
    out["variables"] = normalized
    return out


def _load_layer(path, source, on_warn=None):
    if not path or not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError) as exc:
        _warn(on_warn, "%s unreadable (%s) — layer skipped" % (source, exc))
        return {}
    if not isinstance(data, list):
        _warn(on_warn, "%s is not a snippet array — layer skipped" % source)
        return {}
    result = {}
    for raw in data:
        snippet = _normalize_snippet(raw, source, on_warn)
        if snippet:
            result[snippet["name"]] = snippet
    return result


def ensure_seeded(orch_dir=None, on_warn=None):
    """Create the fleet file only when absent. Existing/corrupt files win."""
    path = fleet_path(orch_dir)
    if os.path.exists(path):
        return path
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        save_snippets(DEFAULT_SNIPPETS, path)
    except OSError as exc:
        _warn(on_warn, "could not seed fleet snippets.json (%s)" % exc)
    return path


def load_snippets(orch_dir=None, section=None, project_dir=None, on_warn=None):
    """Return the merged snippets as a name-keyed dict (later layer wins)."""
    orch_dir = os.path.abspath(orch_dir or HERE)
    ensure_seeded(orch_dir, on_warn)
    merged = {}
    merged.update(_load_layer(fleet_path(orch_dir), "fleet snippets.json",
                              on_warn))
    merged.update(_load_layer(section_path(orch_dir, section),
                              "section %r snippets.json" % section, on_warn))
    merged.update(_load_layer(project_path(project_dir),
                              "project snippets.json", on_warn))
    return merged


def save_snippets(snippets, path):
    """Atomically save a list (or name-keyed dict) without mutating input."""
    values = list(snippets.values()) if isinstance(snippets, dict) \
        else list(snippets)
    parent = os.path.dirname(os.path.abspath(path))
    os.makedirs(parent, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".snippets-", suffix=".tmp", dir=parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(copy.deepcopy(values), handle, indent=2,
                      ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
