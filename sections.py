"""Section manifests — SECTION = DATA (V3 board 3.1).

A section is a chat studio described entirely by
sections/<name>/section.json:

    {id, title, workflow, default_mode, artifact_types_emitted,
     artifact_types_accepted, dod_tier}

`workflow` is either a workflow NAME (resolved via workflows.load_workflow)
or an inline workflow JSON object (workflows.Workflow.from_json). Unknown
fields are tolerated AND preserved through a load/save cycle — future
tasks add fields without breaking older engines.

Three inherited engine disciplines, deliberately:

  * seed-then-disk-wins (workflows.ensure_seeded precedent): shipped
    manifests materialize to JSON only when the file is absent; a
    user-edited file is NEVER overwritten — even an invalid one (invalid
    gets a banner, not a clobber).
  * all-or-default: a corrupt section.json never half-applies. The loader
    returns the complete built-in default for that section; the returned
    object always satisfies the full schema.
  * VISIBLE fallback (plan ground rule 4 — the silent-fallback habit is a
    bug in an interactive product): every substitution of a default for a
    missing/corrupt user file emits exactly one config_fallback event
    naming the section, the file, and the specific error. A healthy run
    emits none.

The shipped manifest CONTENT here is a minimal placeholder pair (ideas /
research, mirroring the M1 chat workflows) — task 3.6 ships the real
eleven; this module owns only the machinery.

Standard library only.
"""

import copy
import json
import os

import events as evlib
import workflows as wflib

SECTIONS_DIRNAME = "sections"
SECTION_FILENAME = "section.json"
DEFAULT_MODE = "manual"
DEFAULT_DOD_TIER = "standard"

# Placeholder shipped manifests (3.6 ships the real set — machinery only).
_BUILTINS = {
    "ideas": {
        "id": "ideas",
        "title": "Ideas",
        "workflow": "chat_ideas",
        "default_mode": "manual",
        "artifact_types_emitted": ["idea_batch"],
        "artifact_types_accepted": ["research_report"],
        "dod_tier": "standard",
    },
    "research": {
        "id": "research",
        "title": "Research",
        "workflow": "chat_research",
        "default_mode": "manual",
        "artifact_types_emitted": ["research_report"],
        "artifact_types_accepted": ["idea_batch"],
        "dod_tier": "standard",
    },
}


class Section(object):
    """One loaded section manifest with its workflow resolved. `extra`
    carries unknown manifest fields verbatim (preserved on save)."""

    __slots__ = ("id", "title", "workflow", "workflow_name", "default_mode",
                 "artifact_types_emitted", "artifact_types_accepted",
                 "dod_tier", "extra")

    def __init__(self, id, title, workflow, workflow_name, default_mode,
                 artifact_types_emitted, artifact_types_accepted, dod_tier,
                 extra=None):
        self.id = id
        self.title = title
        self.workflow = workflow            # a workflows.Workflow object
        self.workflow_name = workflow_name  # name or "(inline)"
        self.default_mode = default_mode
        self.artifact_types_emitted = list(artifact_types_emitted)
        self.artifact_types_accepted = list(artifact_types_accepted)
        self.dod_tier = dod_tier
        self.extra = dict(extra or {})

    def to_json(self):
        d = {
            "id": self.id, "title": self.title,
            "workflow": (self.workflow_name
                         if self.workflow_name != "(inline)"
                         else self.workflow.to_json()),
            "default_mode": self.default_mode,
            "artifact_types_emitted": list(self.artifact_types_emitted),
            "artifact_types_accepted": list(self.artifact_types_accepted),
            "dod_tier": self.dod_tier,
        }
        for k, v in self.extra.items():
            d.setdefault(k, v)
        return d


_KNOWN_FIELDS = ("id", "title", "workflow", "default_mode",
                 "artifact_types_emitted", "artifact_types_accepted",
                 "dod_tier")


def _sections_dir(orch_dir):
    return os.path.join(orch_dir, SECTIONS_DIRNAME)


def section_path(name, orch_dir):
    return os.path.join(_sections_dir(orch_dir), name, SECTION_FILENAME)


def _display_path(name):
    """The engine-relative manifest path for banners: stable, meaningful,
    and immune to the event redactor's entropy heuristic (an absolute
    tmp/user path can get partially redacted)."""
    return os.path.join(SECTIONS_DIRNAME, name, SECTION_FILENAME)


def _banner(app_dir, section, path, error):
    """The visible-fallback banner. app_dir may be None (engine-scope load
    outside a project) — emit_event no-ops then, matching events' contract;
    the substitution itself is still returned honestly."""
    evlib.emit_event(app_dir, "config_fallback", section=section,
                     file=path, error=str(error)[:400])


def _default_section(name, orch_dir):
    raw = copy.deepcopy(_BUILTINS.get(name) or dict(
        _BUILTINS["ideas"], id=name, title=name.title(),
    ))
    return _from_raw(name, raw, orch_dir, path="(built-in)",
                     app_dir=None, allow_banner=False)


def _from_raw(name, raw, orch_dir, path, app_dir, allow_banner=True):
    """Build a Section from a parsed dict. Raises ValueError on a shape
    violation — the CALLER decides all-or-default."""
    if not isinstance(raw, dict):
        raise ValueError("section.json must be a JSON object")
    for field in ("id", "title", "workflow"):
        if field not in raw:
            raise ValueError("missing required field %r" % field)
    wf_field = raw["workflow"]
    if isinstance(wf_field, dict):
        wf = wflib.Workflow.from_json(wf_field)   # ValueError/KeyError bubble
        wf_name = "(inline)"
    elif isinstance(wf_field, str) and wf_field.strip():
        wf_name = wf_field.strip()
        wf = wflib.load_workflow(wf_name, orch_dir)
        if wf.name != wf_name and wf_name not in wflib.list_workflows(orch_dir):
            # load_workflow fell back silently (its own contract); the
            # section loader surfaces it — ground rule 4.
            if allow_banner:
                _banner(app_dir, name, path,
                        "workflow %r not found — using %r"
                        % (wf_name, wf.name))
    else:
        raise ValueError("workflow must be a name or an inline object")
    emitted = raw.get("artifact_types_emitted", [])
    accepted = raw.get("artifact_types_accepted", [])
    if not isinstance(emitted, list) or not isinstance(accepted, list):
        raise ValueError("artifact type fields must be lists")
    extra = {k: v for k, v in raw.items() if k not in _KNOWN_FIELDS}
    return Section(
        id=str(raw["id"]), title=str(raw["title"]),
        workflow=wf, workflow_name=wf_name,
        default_mode=str(raw.get("default_mode", DEFAULT_MODE)),
        artifact_types_emitted=[str(t) for t in emitted],
        artifact_types_accepted=[str(t) for t in accepted],
        dod_tier=str(raw.get("dod_tier", DEFAULT_DOD_TIER)),
        extra=extra)


def load_section(name, orch_dir, app_dir=None):
    """The section named `name`: on-disk manifest first (GUI edits live
    there), else the built-in default. NEVER raises and never partially
    applies — any missing/corrupt file yields the full default plus one
    config_fallback banner event. Healthy loads emit nothing."""
    path = section_path(name, orch_dir)
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as fh:
                raw = json.load(fh)
            return _from_raw(name, raw, orch_dir, _display_path(name), app_dir)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            _banner(app_dir, name, _display_path(name), exc)
            return _default_section(name, orch_dir)
    if name not in _BUILTINS:
        _banner(app_dir, name, _display_path(name),
                "no manifest on disk and no built-in")
    return _default_section(name, orch_dir)


def ensure_seeded_sections(orch_dir):
    """Materialize shipped manifests on first run — never clobbering an
    existing file, even an invalid one. Best-effort (OSError swallowed)."""
    try:
        for name, raw in _BUILTINS.items():
            d = os.path.join(_sections_dir(orch_dir), name)
            os.makedirs(d, exist_ok=True)
            path = os.path.join(d, SECTION_FILENAME)
            if not os.path.exists(path):
                with open(path, "w", encoding="utf-8") as fh:
                    json.dump(raw, fh, indent=2)
    except OSError:
        pass


def list_sections(orch_dir):
    """Names available: union of shipped built-ins and on-disk dirs that
    carry a section.json. Sorted."""
    names = set(_BUILTINS)
    try:
        for fn in os.listdir(_sections_dir(orch_dir)):
            if os.path.exists(section_path(fn, orch_dir)):
                names.add(fn)
    except OSError:
        pass
    return sorted(names)
