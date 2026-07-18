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

# Shipped section manifests (V3 3.6): Ideas<-brainstorm,
# Research<-research, QA<-audit, Planning<-app_spec. MUST
# match sections/<name>/section.json byte-for-byte (the
# consistency test pins it); the repo dirs are the source,
# this literal only seeds App-Support engine copies.
_BUILTINS = {
    "ideas": {
        "id": "ideas",
        "title": "Ideas",
        "workflow": "brainstorm",
        "default_mode": "manual",
        "artifact_types_emitted": [
                "idea"
        ],
        "artifact_types_accepted": [
                "opportunity_signal"
        ],
        "dod_tier": "standard"
    },
    "research": {
        "id": "research",
        "title": "Research",
        "workflow": "research",
        "default_mode": "manual",
        "artifact_types_emitted": [
                "research_brief",
                "opportunity_signal"
        ],
        "artifact_types_accepted": [
                "idea"
        ],
        "dod_tier": "standard"
    },
    "qa": {
        "id": "qa",
        "title": "QA",
        "workflow": "audit",
        "default_mode": "manual",
        "artifact_types_emitted": [
                "finding_report"
        ],
        "artifact_types_accepted": [
                "any"
        ],
        "dod_tier": "strict"
    },
    "planning": {
        "id": "planning",
        "title": "Planning",
        "workflow": "app_spec",
        "default_mode": "manual",
        "artifact_types_emitted": [
                "spec_bundle"
        ],
        "artifact_types_accepted": [
                "idea",
                "research_brief"
        ],
        "dod_tier": "strict"
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
    # An unknown-name fallback must FAIL CLOSED on bus capabilities: a
    # custom section whose manifest is corrupt/missing must not inherit
    # the ideas builtin's artifact types — a parse error would silently
    # flip a declared-closed publication gate to open (V3 4.2).
    raw = copy.deepcopy(_BUILTINS.get(name) or dict(
        _BUILTINS["ideas"], id=name, title=name.title(),
        artifact_types_emitted=[], artifact_types_accepted=[],
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


CONTRACTS_FILENAME = "contracts.json"

# The shipped structured-output contracts (V3 board 3.3),
# materialized VERBATIM from the historical orchestrator
# constants by the 3.3 generation script — prompt BYTES are the
# product; tests/fixtures/contracts_frozen.json pins every
# assembled output. phase_key "@decisions" is gated by the
# engine's decisions predicate; "@summary" appends to EVERY
# phase unconditionally. required_fields names a key of
# schemas.REQUIRED_FIELDS (resolved at extraction time) so the
# publication snippet and the extraction call can never drift.
DEFAULT_CONTRACTS = [
    {
        "phase_key": "task_assignments",
        "contract": "tasks",
        "fence_tag": "tasks-json",
        "required_fields": None,
        "prompt_snippet": "MACHINE CONTRACT (required): in your wrap-up, alongside the prose, emit ONE fenced ```tasks-json``` block containing a single JSON object of the form {\"tasks\": [{\"id\": \"T-001\", \"title\": ..., \"owner_lane\": ..., \"files\": [...], \"depends_on\": [], \"acceptance_criteria\": [...], \"requirement_ids\": [\"R-001\"], \"status\": \"pending\"}]}. owner_lane must be one of: data_domain, primary_ui, services_utilities, polish_resilience. depends_on lists task ids and the graph must be acyclic. requirement_ids lists the id(s) from requirements.json this task fulfills \u2014 every CORE requirement needs at least one task naming it, or the build is missing part of what was promised. The build workers are assigned their lane's tasks from this block, so make it complete.\n",
    },
    {
        "phase_key": "task_assignments",
        "contract": "flows",
        "fence_tag": "flows-json",
        "required_fields": None,
        "prompt_snippet": "MACHINE CONTRACT (required): ALSO emit ONE fenced ```flows-json``` block of the form {\"flows\": [{\"name\": \"add first item\", \"steps\": [{\"tap\": \"home.addItemButton\"}, {\"type\": \"Sample\", \"into\": \"addEdit.nameField\"}, {\"tap\": \"addEdit.saveButton\"}, {\"assert_exists\": \"Sample\"}, {\"back\": true}]}]}. Cover EVERY primary user journey the app promises (3-8 flows). Every tap/type step MUST name a stable dotted accessibilityIdentifier (screen.controlName) \u2014 PREFER accessibilityIdentifiers over visible labels: an identifier is a contract the build MUST honor, whereas a visible label drifts during the build. NEVER name a bare glyph or symbol as a tap/type token ('+', 'x', '\u00d7', '\u00b0F', a gear icon): an icon-only control has no matchable text, so reference it by its accessibilityIdentifier \u2014 or, if you must use a label, name the EXACT full accessibilityLabel the control will expose ('Add custom tea'), never the glyph. assert_exists MAY instead name a user-visible string you expect on screen (e.g. the item you just created). When an asserted state only arrives after an app-determined delay \u2014 a timer completing, a countdown reaching zero \u2014 add \"timeout\": <seconds> to THAT step (e.g. {\"assert_exists\": \"Ready\", \"timeout\": 95}), sized to the app's own duration plus margin: the default wait is ~5 seconds, so a timer's completion state can NEVER pass without a declared timeout. Route such flows through the SHORTEST built-in duration. Never route a flow through a control hidden behind a gesture (swipe actions, long-press context menus): the gate taps directly-visible controls only, so a journey needing edit/delete must use a visibly exposed affordance. Whatever a step names, the built app has to expose that EXACT string on the control or the UI-crawl gate fails the journey. These are executed against the real built app by the UI-crawl gate \u2014 a journey you forget to declare is a journey nobody verifies.\n",
    },
    {
        "phase_key": "app_features",
        "contract": "requirements",
        "fence_tag": "requirements-json",
        "required_fields": None,
        "prompt_snippet": "MACHINE CONTRACT (required): in your wrap-up, alongside the prose, emit ONE fenced ```requirements-json``` block of the form {\"requirements\": [{\"id\": \"R-001\", \"text\": \"...\", \"core\": true|false}]}. Number EVERY testable promise the app makes (from the original prompt plus agreed features). The adherence gate grades the BUILT app against exactly this list \u2014 an unlisted requirement is an unverified one.\n",
    },
    {
        "phase_key": "tech_specs",
        "contract": "interfaces",
        "fence_tag": "interfaces-json",
        "required_fields": None,
        "prompt_snippet": "MACHINE CONTRACT (required): in your wrap-up, alongside the prose, emit ONE fenced ```interfaces-json``` block containing a single JSON object of the form {\"interfaces\": [{\"name\": ..., \"kind\": \"struct|protocol|function|enum|endpoint\", \"language\": ..., \"signature\": ..., \"owning_lane\": ..., \"notes\": ...}]}. owning_lane must be one of: data_domain, primary_ui, services_utilities, polish_resilience. This is the shared type/signature contract every parallel build worker codes against, so include every cross-lane type, API and function.\n",
    },
    {
        "phase_key": "extraction_candidates",
        "workflow_target": "library_mining",
        "contract": "extraction",
        "fence_tag": "extraction-json",
        "required_fields": "extraction_package",
        "prompt_snippet": "MACHINE CONTRACT (optional but recommended): for your TOP extraction candidate, ALSO emit ONE fenced ```extraction-json``` block of the form {\"package_name\": \"NetworkKit\", \"platforms\": [], \"products\": [], \"public_api\": [{\"kind\": \"protocol|struct|class|enum|func\", \"name\": \"APIClient\", \"signature\": \"func send(_ r: Request) async throws -> Response\"}], \"adopting_repos\": [\"repo-a\", \"repo-b\"]}. The orchestrator scaffolds this into a real, COMPILABLE Swift Package skeleton (the public API becomes buildable stubs, your signature preserved as documentation) and runs `swift build` to prove it holds together \u2014 so name the package and its public surface precisely. One package (your single best candidate); implementations are left as stubs for a human to fill in.\n",
    },
    {
        "phase_key": "@decisions",
        "contract": "decisions",
        "fence_tag": "decisions-json",
        "required_fields": None,
        "prompt_snippet": "MACHINE CONTRACT (required): in your wrap-up, alongside the prose, emit ONE fenced ```decisions-json``` block containing a single JSON object of the form {\"decisions\": [{\"id\": \"DEC-<phase>-001\", \"decision\": ..., \"rationale\": ..., \"rejected_alternatives\": [...], \"constraints\": [...], \"supersedes\": null}]}. Record every decision this phase actually made, one entry each: the decision in one sentence, why it won, what was rejected, and any hard constraints later phases must respect. Set \"supersedes\" to an earlier decision id ONLY when this decision replaces it. These entries become the authoritative cross-phase DECISIONS LOG injected into every later phase, so completeness beats prose.\n",
    },
    {
        "phase_key": "@artifact",
        "contract": "artifact",
        "fence_tag": "artifact-json",
        "required_fields": "artifact_block",
        "prompt_snippet": "MACHINE CONTRACT (when this chat produced publishable results): in your wrap-up, alongside the prose, emit ONE fenced ```artifact-json``` block PER artifact to publish to the shared bus, each a single JSON object of the form {\"type\": <one of: __TYPES__>, \"title\": \"a short human title\", \"body\": <the artifact's complete markdown content as ONE JSON string — write any triple-backtick inside it as \\u0060\\u0060\\u0060>, \"keywords\": [...], \"doc_slots\": []}. Include the type's own payload fields where they apply (e.g. \"sources\" for a research_brief, \"evidence\" for an opportunity_signal, \"impact\" for a gap, \"parents\" for a reconcile). Publish only results worth routing to another section — a wrap-up with nothing publishable emits no block at all.\n",
    },
    {
        "phase_key": "@summary",
        "contract": "phase_summary",
        "fence_tag": "phase-summary-json",
        "required_fields": "phase_summary",
        "prompt_snippet": "MACHINE CONTRACT (required): in your wrap-up, alongside the prose, emit ONE fenced ```phase-summary-json``` block containing a single JSON object of the form {\"phase\": \"<this phase's key>\", \"one_paragraph_summary\": ..., \"key_decisions\": [...decision ids from decisions.json this phase made, if any...], \"open_risks\": [...]}. Once this phase is no longer one of the most recently completed phases, THIS summary \u2014 not the raw transcript above \u2014 is what later phases see, so make the paragraph self-contained: state what was actually decided or produced, not just that a discussion happened.\n",
    },
]


def _contracts_display_path(name):
    return os.path.join(SECTIONS_DIRNAME, name, CONTRACTS_FILENAME)


def load_contracts(orch_dir, section=None, app_dir=None):
    """The contract entries for a run: shipped defaults, overlaid by the
    session's sections/<section>/contracts.json when one exists. Overlay
    semantics: an entry with the same (phase_key, contract) REPLACES the
    default; a new pair adds. Absent file = defaults, silent; PRESENT but
    corrupt = defaults + one config_fallback banner (never silent, never
    half-applied)."""
    defaults = [dict(e) for e in DEFAULT_CONTRACTS]
    if not section:
        return defaults
    path = os.path.join(_sections_dir(orch_dir), section, CONTRACTS_FILENAME)
    if not os.path.exists(path):
        return defaults
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        entries = data.get("contracts") if isinstance(data, dict) else None
        if not isinstance(entries, list):
            raise ValueError("contracts.json must be an object with a "
                             "'contracts' list")
        overlay = []
        for e in entries:
            if not isinstance(e, dict) or "phase_key" not in e \
                    or "prompt_snippet" not in e:
                raise ValueError("each contract entry needs phase_key and "
                                 "prompt_snippet")
            overlay.append(dict(e))
    except (OSError, ValueError, KeyError, TypeError) as exc:
        _banner(app_dir, section, _contracts_display_path(section), exc)
        return defaults
    merged = list(defaults)
    for e in overlay:
        okey = (e.get("phase_key"), e.get("contract"))
        for i, d in enumerate(merged):
            if (d.get("phase_key"), d.get("contract")) == okey:
                merged[i] = e
                break
        else:
            merged.append(e)
    return merged


def assemble_contract(contracts, phase_key, workflow_target,
                      include_decisions, artifact_types=None):
    """The wrap-up contract text: phase-matching entries in list order,
    then @decisions when the engine's predicate asked, then @artifact
    ONLY when the session's section declares emitted artifact types
    (V3 4.2 — falsy means byte-identical output, so every legacy/flat
    prompt is untouched by construction), then @summary ALWAYS — joined
    exactly like the historical constant assembly (byte-parity pinned by
    tests/fixtures/contracts_frozen.json)."""
    parts = []
    for e in contracts:
        ekey = e.get("phase_key")
        # "@" keys are engine-gated pseudo-entries (@decisions/@artifact/
        # @summary): a workflow phase LITERALLY keyed "@artifact" must not
        # direct-match one — it would bypass the gate below and leak the
        # snippet with its __TYPES__ placeholder unsubstituted.
        if ekey != phase_key or (ekey or "").startswith("@"):
            continue
        gate = e.get("workflow_target")
        if gate and workflow_target != gate:
            continue
        parts.append(e["prompt_snippet"])
    if include_decisions:
        parts.extend(e["prompt_snippet"] for e in contracts
                     if e.get("phase_key") == "@decisions")
    if artifact_types:
        types_str = ", ".join(artifact_types)
        parts.extend(e["prompt_snippet"].replace("__TYPES__", types_str)
                     for e in contracts
                     if e.get("phase_key") == "@artifact")
    parts.extend(e["prompt_snippet"] for e in contracts
                 if e.get("phase_key") == "@summary")
    return "\n".join(parts)


def contract_fence(name, contracts=None):
    """(fence_tag, required_fields_key) for a named contract — THE single
    source both the publication snippet and the extraction call sites
    read, so they can never drift apart."""
    for e in (contracts or DEFAULT_CONTRACTS):
        if e.get("contract") == name:
            return e.get("fence_tag"), e.get("required_fields")
    return None, None


# ---------------------------------------------------------------------------
# Scaffold + lint (V3 board 3.7). All logic lives HERE — orchestrator.py
# carries only the argparse hook and the exit-code mapping.
# ---------------------------------------------------------------------------

TEMPLATE_DIRNAME = "_template"


def _valid_section_name(name):
    """Mirror of the engine's valid_app_slug rules (pinned equal by test —
    sections.py cannot import orchestrator without a cycle): plain folder
    name, no separators, no '..', not hidden."""
    n = str(name or "")
    return bool(n) and ".." not in n and not n.startswith(".") \
        and "/" not in n and "\\" not in n and os.sep not in n


def scaffold_section(name, orch_dir):
    """Create sections/<name>/ from sections/_template/, substituting the
    identity fields. Refuses an invalid name or an existing dir — never
    overwrites. Returns the new section dir path; raises ValueError on
    refusal (the CLI maps it to a specific error message)."""
    if not _valid_section_name(name) or name == TEMPLATE_DIRNAME:
        raise ValueError("invalid section name %r — use a plain folder name "
                         "(no separators, '..', or leading '.')" % name)
    src = os.path.join(_sections_dir(orch_dir), TEMPLATE_DIRNAME)
    if not os.path.isdir(src):
        raise ValueError("sections/_template/ is missing from the engine dir")
    dest = os.path.join(_sections_dir(orch_dir), name)
    if os.path.exists(dest):
        raise ValueError("sections/%s/ already exists — refusing to "
                         "overwrite (edit it in place, or pick another "
                         "name)" % name)
    os.makedirs(dest)
    for fn in sorted(os.listdir(src)):
        with open(os.path.join(src, fn), encoding="utf-8") as fh:
            text = fh.read()
        text = text.replace("__NAME__", name)
        text = text.replace("__TITLE__", name.replace("-", " ").title())
        with open(os.path.join(dest, fn), "w", encoding="utf-8") as fh:
            fh.write(text)
    return dest


def _lint_entry(severity, file, field, message):
    return {"severity": severity, "file": file, "field": field,
            "message": message}


def lint_section(name, orch_dir):
    """Structured lint report [{severity, file, field, message}].

    severity 'error' is EXACTLY the set of defects that would emit a
    runtime config_fallback banner (lint and the loaders share validation
    code, so they cannot drift); 'warning' marks runnable-but-ignored
    configuration (e.g. a corrupt routing overlay, which the runtime
    routing loader fails open on today)."""
    import roles as roleslib

    report = []
    sdir = os.path.join(_sections_dir(orch_dir), name)
    if not os.path.isdir(sdir):
        return [_lint_entry("error", "-", "-",
                            "sections/%s/ does not exist" % name)]

    def parse(fn):
        path = os.path.join(sdir, fn)
        if not os.path.exists(path):
            return None, False
        try:
            with open(path, encoding="utf-8") as fh:
                return json.load(fh), True
        except (OSError, ValueError) as exc:
            report.append(_lint_entry(
                "error" if fn != "routing.json" else "warning",
                fn, "-", "does not parse as JSON: %s" % exc))
            return None, True

    # section.json — shares _from_raw with the loader.
    raw, present = parse(SECTION_FILENAME)
    workflow_names = wflib.list_workflows(orch_dir)
    if not present:
        report.append(_lint_entry("error", SECTION_FILENAME, "-",
                                  "missing — the section would load the "
                                  "built-in default with a banner"))
    elif raw is not None:
        try:
            _from_raw(name, raw, orch_dir, path="(lint)", app_dir=None,
                      allow_banner=False)
        except (ValueError, KeyError, TypeError) as exc:
            report.append(_lint_entry("error", SECTION_FILENAME,
                                      "-", str(exc)))
        wf_field = raw.get("workflow") if isinstance(raw, dict) else None
        if isinstance(wf_field, str) and wf_field.strip() \
                and wf_field.strip() not in workflow_names:
            report.append(_lint_entry(
                "error", SECTION_FILENAME, "workflow",
                "workflow %r not found — the run would banner and fall "
                "back to %r" % (wf_field.strip(), wflib.DEFAULT_WORKFLOW)))

    # roles.json — pool validity via the same _valid_pool the loader uses.
    raw, present = parse("roles.json")
    if raw is not None and isinstance(raw, dict):
        for field, keys in (("personalities", ("id", "name", "style")),
                            ("roles", ("id", "name", "focus"))):
            pool = raw.get(field)
            if pool is not None and not roleslib._valid_pool(pool, keys):
                report.append(_lint_entry(
                    "error", "roles.json", field,
                    "invalid pool — every entry needs %s; the runtime "
                    "falls through with a banner" % (keys,)))
    elif raw is not None:
        report.append(_lint_entry("error", "roles.json", "-",
                                  "must be a JSON object"))

    # contracts.json — entry shape + fence-tag collisions.
    raw, present = parse(CONTRACTS_FILENAME)
    if raw is not None:
        entries = raw.get("contracts") if isinstance(raw, dict) else None
        if not isinstance(entries, list):
            report.append(_lint_entry(
                "error", CONTRACTS_FILENAME, "contracts",
                "must be an object with a 'contracts' list"))
        else:
            fences = {}
            for i, e in enumerate(entries):
                if not isinstance(e, dict) or "phase_key" not in e \
                        or "prompt_snippet" not in e:
                    report.append(_lint_entry(
                        "error", CONTRACTS_FILENAME,
                        "contracts[%d]" % i,
                        "each entry needs phase_key and prompt_snippet"))
                    continue
                tag = e.get("fence_tag")
                cname = e.get("contract")
                if tag and fences.get(tag, cname) != cname:
                    report.append(_lint_entry(
                        "warning", CONTRACTS_FILENAME,
                        "contracts[%d].fence_tag" % i,
                        "fence tag %r used by two different contracts — "
                        "extraction cannot tell them apart" % tag))
                if tag:
                    fences[tag] = cname

    # rules.json — the same shape _load_layer enforces.
    raw, present = parse("rules.json")
    if raw is not None and (not isinstance(raw, dict)
                            or not isinstance(raw.get("phases"), dict)):
        report.append(_lint_entry(
            "error", "rules.json", "phases",
            "must be an object with a 'phases' object — the runtime "
            "overlay banners and falls through"))

    # routing.json parse handled by parse() (warning severity: the runtime
    # routing loader fails open today, no banner).
    parse("routing.json")

    # cross-section refs in the workflow's phase role lists (3.5 syntax).
    try:
        section = load_section(name, orch_dir)
        for ph in section.workflow.phases:
            ids = ph.get("roles") if hasattr(ph, "get") else None
            if not ids:
                continue
            misses = []
            roleslib.resolve_phase_role_refs(
                ids, _sections_dir(orch_dir),
                on_missing=lambda ref, why: misses.append((ref, why)))
            for ref, why in misses:
                report.append(_lint_entry(
                    "error", "workflow:%s" % section.workflow_name,
                    "phases[%s].roles" % ph.key,
                    "dangling cast reference %r (%s) — the runtime "
                    "banners and drops it" % (ref, why)))
    except Exception as exc:  # noqa: BLE001 - lint must never crash
        report.append(_lint_entry("warning", "-", "-",
                                  "cross-section ref check skipped: %s" % exc))
    return report


def lint_exit_code(report):
    """0 clean, 1 warnings-only (runnable), 2 errors (the section would
    banner-fallback at runtime)."""
    if any(e["severity"] == "error" for e in report):
        return 2
    if report:
        return 1
    return 0
