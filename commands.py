"""V3 9.5: the /command registry — data, not behavior. commands.json layers
fleet -> section -> project by name (later layer wins), mirroring roles.py's
load_agent_library (9.4) and modelrouting's overlay shape (3.4 precedent).

Kinds dispatch, they don't decide: builtin names an existing verb (9.8 wires
which); template expands into the composer for the USER to send (never
auto-fired, §13.5); delegation rides 4.6's @-mention machinery via its
`target` section; meta runs exactly one advisory call_agent turn whose output
renders as a card (the executor lives in orchestrator.py beside the 4.6
dispatch — this module is the pure registry + parser, stdlib only, so it stays
importable without executing engine code, same as sections.py/roles.json).

Unknown command: the caller must show a visible banner and NOT forward the
raw text as a chat message (§6.2 never silently discard — the text stays in
the composer for the user to fix or send as plain text; §13.3 a hostile
'command' string is data, never code)."""
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
COMMANDS_FILENAME = "commands.json"
SCHEMA_VERSION = 1
KINDS = ("builtin", "template", "delegation", "meta")

# Base set per the sections plan's command table (§14.4): fully functional
# template/delegation entries and builtins wired by 9.8. Meta entries are
# advisory-card content: the 9.5
# executor quotes their arguments as data and performs exactly one turn.
DEFAULT_COMMANDS = {
    "schema_version": SCHEMA_VERSION,
    "commands": [
        {"name": "vote", "kind": "builtin",
         "description": "Request a forced weighted vote at the next round barrier."},
        {"name": "consensus", "kind": "builtin",
         "description": "Request a coordinator consensus attempt at the next round barrier."},
        {"name": "cast", "kind": "builtin",
         "description": "Add or remove a roster member at the next round barrier."},
        {"name": "fork", "kind": "builtin",
         "description": "Fork this session through the existing session fork verb."},
        {"name": "summarize", "kind": "template",
         "template": "@Documentation summarize this conversation so far.",
         "description": "Ask Documentation to summarize the conversation."},
        {"name": "decision", "kind": "template",
         "template": "@Planning what decision are we converging on here?",
         "description": "Ask Planning to name the emerging decision."},
        {"name": "audit", "kind": "delegation", "target": "qa",
         "description": "Deep-dive: QA audits this conversation."},
        {"name": "research", "kind": "delegation", "target": "research",
         "description": "Deep-dive: Research investigates a question."},
        {"name": "compare", "kind": "builtin",
         "description": "Compare one prompt across selected enabled models without changing the room."},
        {"name": "status", "kind": "builtin",
         "description": "Show this session's persisted run status."},
        {"name": "cost", "kind": "builtin",
         "description": "Show this session's honest metered/unmetered cost rollup."},
        {"name": "help", "kind": "builtin",
         "description": "Show the effective layered command registry grouped by kind."},
        {"name": "model-effort", "kind": "meta",
         "description":
             "Return an advisory card only; do not perform the task. Use "
             "the seeded model-effort rubric to recommend one model and "
             "one supported effort, with a one-line rationale. Label a "
             "known-price estimate with ≈; otherwise say unmetered. End "
             "with 'Run with this' as an explicit next action that would "
             "use the original input unchanged."},
        {"name": "gen-prompt", "kind": "meta",
         "description":
             "Return an editable structured-prompt card only; do not run "
             "or answer the rough input. Organize it into Goal, Context, "
             "Constraints, Output format, and Acceptance criteria. End "
             "with explicit Insert and Cancel actions; neither action is "
             "automatic and the command itself sends nothing."},
        {"name": "snippet", "kind": "template",
         "template": "Choose a saved snippet in the composer; review the "
                     "rendered text before sending.",
         "description": "Insert a named saved snippet into the composer; "
                        "never auto-send it."},
    ],
}

# Names are data; behavior remains at the existing engine/GUI verb seams.
# The pure table is deliberately testable without importing orchestrator.py.
COMMAND_VERBS = {
    "vote": "barrier_vote",
    "consensus": "barrier_consensus",
    "cast": "roster_cast",
    "fork": "fork_session",
    "audit": "delegate_qa",
    "research": "delegate_research",
    "decision": "template_decision",
    "summarize": "template_summarize",
    "compare": "compare_models",
    "status": "session_status",
    "cost": "session_cost",
    "help": "registry_help",
}


def dispatch_registered(name, args, handlers):
    """Call exactly one injected existing-verb handler for a base command.

    Returns ``(handled, result)``. Unknown/deleted registry names are a clean
    no-op; callers remain responsible for checking the effective layered
    registry before invoking this table.
    """
    verb = COMMAND_VERBS.get(name)
    if verb is None or verb not in handlers:
        return False, None
    return True, handlers[verb](args)

_NAME_EXTRA = "-_"


def _warn(on_warn, msg):
    if on_warn:
        on_warn("commands: %s" % msg)


def _valid_name(name):
    return (isinstance(name, str) and name
            and all(c.isalnum() or c in _NAME_EXTRA for c in name)
            and name[0].isalnum())


def commands_path(a_dir):
    return os.path.join(a_dir, COMMANDS_FILENAME)


def _section_commands_path(orch_dir, section):
    if not section:
        return None
    return os.path.join(orch_dir, "sections", section, COMMANDS_FILENAME)


def _validate_entry(raw, source, on_warn):
    if not isinstance(raw, dict):
        _warn(on_warn, "%s: a commands entry is not an object — skipped"
             % source)
        return None
    name = raw.get("name")
    if not _valid_name(name):
        _warn(on_warn, "%s: entry has an invalid/missing 'name' — skipped"
             % source)
        return None
    kind = raw.get("kind")
    if kind not in KINDS:
        _warn(on_warn, "%s: command %r has unknown kind %r — skipped"
             % (source, name, kind))
        return None
    if kind == "template" and not isinstance(raw.get("template"), str):
        _warn(on_warn, "%s: template command %r missing 'template' — skipped"
             % (source, name))
        return None
    if kind == "delegation" and not _valid_name(raw.get("target")):
        _warn(on_warn, "%s: delegation command %r missing/invalid 'target' "
                       "— skipped" % (source, name))
        return None
    return {"name": name, "kind": kind, "template": raw.get("template"),
           "target": raw.get("target"),
           "description": str(raw.get("description") or "")}


def _load_layer(path, source, on_warn):
    """One commands.json layer -> {name: entry}. A missing file is silently
    empty (normal — not every layer exists); a present-but-corrupt file warns
    and contributes nothing (§6.2: the rest of the OTHER layers still load —
    only THIS layer's malformed entries are individually dropped, a bad file
    doesn't take the whole registry down)."""
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as exc:
        _warn(on_warn, "%s unreadable (%s) — layer skipped" % (source, exc))
        return {}
    if not isinstance(data, dict) or not isinstance(data.get("commands"),
                                                     list):
        _warn(on_warn, "%s is not a valid commands document — layer skipped"
             % source)
        return {}
    out = {}
    for entry in data["commands"]:
        norm = _validate_entry(entry, source, on_warn)
        if norm:
            out[norm["name"]] = norm
    return out


def ensure_seeded(orch_dir=None, on_warn=None):
    """Materialize the fleet commands.json on first use — never clobber an
    existing file, even an invalid one; disk wins (the house seeding
    contract, workflows.ensure_seeded/sections.ensure_seeded_sections)."""
    orch_dir = orch_dir or HERE
    path = commands_path(orch_dir)
    if os.path.exists(path):
        return
    try:
        os.makedirs(orch_dir, exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(DEFAULT_COMMANDS, fh, indent=2)
    except OSError as exc:
        _warn(on_warn, "could not seed fleet commands.json (%s)" % exc)


def load_commands(orch_dir=None, section=None, project_dir=None, on_warn=None):
    """Merge fleet -> section -> project commands.json by name (a later
    layer's entry for the same name REPLACES the earlier one — project wins
    over section wins over fleet, matching modelrouting's overlay
    precedent). Returns {name: entry}."""
    orch_dir = os.path.abspath(orch_dir or HERE)
    ensure_seeded(orch_dir, on_warn)
    merged = {}
    merged.update(_load_layer(commands_path(orch_dir), "fleet commands.json",
                              on_warn))
    sec_path = _section_commands_path(orch_dir, section)
    if sec_path:
        merged.update(_load_layer(sec_path, "section %r commands.json"
                                  % section, on_warn))
    if project_dir:
        merged.update(_load_layer(commands_path(project_dir),
                                  "project commands.json", on_warn))
    return merged


def parse_command(msg):
    """A leading '/' at position 0 ONLY — no leading-whitespace tolerance, so
    a chat message that merely contains '/vote' mid-sentence (or after
    whitespace) is untouched plain text, distinct from 4.6's @-mention parser
    which tolerates leading whitespace. Returns {'name', 'args'} or None."""
    if not msg or not msg.startswith("/"):
        return None
    rest = msg[1:]
    if not rest or rest[0].isspace():
        return None   # a bare '/' or '/ foo' names nothing
    parts = rest.split(None, 1)
    name = parts[0]
    if not _valid_name(name):
        return None
    return {"name": name, "args": parts[1] if len(parts) > 1 else ""}
