"""V3 board 2.3a: the cfg["_…"] key inventory + the monotonic grep gate.

The engine threads per-run/per-phase/per-turn state through underscore keys
mutated onto a shared cfg dict (shallow-copied per thread) — the loose-state
model 2.3's TurnContext migration retires. This module is both:

1. THE GATE: the allowlist below is monotonically shrinking. A NEW
   underscore key assignment anywhere in the scanned files fails this test
   — new per-turn state rides TurnContext fields (or an explicit, reviewed
   allowlist addition with a stated reason), never a fresh cfg["_ key.
2. THE GENERATOR: `python3 -m tests.test_cfg_key_inventory` (run directly)
   regenerates TURNCONTEXT_INVENTORY.md — the writer/reader site map the
   2.3(b/c) migration works from.

Scan shape: any subscript assignment `<name>["_key"] = …` counts as a write
regardless of the variable name (cfg/acfg/fcfg/qcfg/base/c/…) — every such
dict in the engine IS a cfg copy; `.get("_key")` and non-assignment
subscripts count as reads.
"""
import os
import re
import unittest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCANNED = ("orchestrator.py", "visualqa.py", "urlfetch.py", "uicrawl.py")

# Single leading underscore only: __dunder__ keys are round-offset dict
# entries (_seen_chars["__coord__"], _lane_seen["__integrator__"]), not
# cfg state.
#
# 2.3(b) scanner amendment — the (a) regexes had four verified blind spots:
# setdefault() writes (the _claude_sessions/_codex_sessions maps were
# invisible), tuple-unpack targets (only the LAST target counted, so
# _personalities was misfiled as read-only), pop() lifecycle sites, and the
# ONE dynamic-key write ('_%s_sessions' % agent, orchestrator.py:1574 area)
# which no literal scanner can attribute — recorded under the synthetic
# name below so the inventory names it instead of hiding it.
_KEY = r'"(_[a-z0-9][a-z0-9_]*)"'
_SUB_RE = re.compile(r'\[\s*' + _KEY + r'\s*\]')
_SETDEFAULT_RE = re.compile(r'\.setdefault\(\s*' + _KEY)
_POP_RE = re.compile(r'\.pop\(\s*' + _KEY)
_GET_RE = re.compile(r'\.get\(\s*' + _KEY)
_STRING_RE = re.compile(r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'')
_DYNAMIC_RE = re.compile(r'\.setdefault\(\s*"_%s')
DYNAMIC_SENTINEL = "_<dynamic>_%s_sessions"

# The frozen inventory (2.3a baseline, regenerated ONCE in 2.3b when the
# scanner amendment surfaced four keys the old regex could not see —
# _claude_sessions/_codex_sessions (setdefault), _personalities (first
# tuple-unpack target), and the dynamic-sessions sentinel. That growth is
# the gate becoming honest, not new state; the monotonic rule applies from
# THIS baseline). Shrink freely as TurnContext absorbs keys; grow ONLY with
# a reviewed reason recorded in the commit.
ALLOWED_WRITTEN_KEYS = {
    DYNAMIC_SENTINEL,
    "_agent_health", "_agent_role_overrides",
    "_app_dir", "_autonomy", "_base_models",
    "_base_resolved", "_budget",
    "_checked_any_agent_runnable",
    "_claude_sessions", "_codex_sessions", "_completeness",
    "_deadline", "_explicit_app",
    "_gemini_disabled_reason", "_gemini_unavailable",
    "_installed_ollama_models", "_iter_verify_toolchain_absent",
    "_noted_local_active_limit",
    "_noted_local_lane_skip", "_noted_local_ram_gate", "_noted_ollama_sprint_skip",
    "_noted_ollama_uninstalled_skip", "_original_prompt", "_personalities",
    "_phase_deadline",
    "_prior_discussions",
    "_resolved", "_role_by_id",
    "_roles", "_round_multiplier",
    "_routing",
    "_sim_ctx",
    "_state", "_target_path",
    "_target_paths", "_tech_stack_block",
    "_url_context", "_warned_no_git_repo",
    "_workflow_name", "_workflow_target", "_workflow_verify_spec",
}

# Keys migrated to the TurnContext view: raw writes of these now live ONLY
# in turncontext.py, which is deliberately outside SCANNED — the gate above
# catches any raw reintroduction in the engine files because the keys are
# no longer allowlisted. 2.3b tranche 1 = band B; 2.3c tranche c1 = band C
# (thread_copy/session/model-override protocols) + band B′ (phase-routing
# copy keys). _resolved stays allowlisted: its two band-A base-resolution
# writes are still raw (per-call PATCHES migrated with patch_agent_model).
TRANCHE1_MIGRATED = {
    "_turn_timeout", "_verify_context", "_phase_exemplar", "_phase_playbook",
    "_knowledge", "_allow_writes", "_session_cwd", "_prior_disc_cap",
    "_target_digest", "_read_dir",
}
TRANCHE_C1_MIGRATED = {
    "_session", "_health_key", "_claude_model_override", "_new_session_id",
    "_drop_prior_discussions", "_build_dir",
    "_phase_key", "_routed_turn_timeout", "_routed_rounds",
    "_phase_instructions", "_role_routing", "_noted_indep_grader",
}


def _assignment_positions(code):
    """Indexes of ALL statement-level assignment `=` signs (empty if none).

    Only a bare `=` at bracket depth 0 splits the line — a kwarg `=` inside a
    call (os.makedirs(cfg["_build_dir"], exist_ok=True)) is NOT an assignment;
    the first version of this splitter misread every such read as a write.
    ALL depth-0 positions are returned because a chained assignment
    (`routing = cfg["_routing"] = load(...)`, the 2.3c2 blind spot) has a
    target between two `=` signs that a first-match splitter files as RHS.
    String literals are masked (same length, so indices stay aligned) so
    brackets/`=` inside them can't skew the depth count."""
    masked = _STRING_RE.sub(lambda m: "\x00" * len(m.group(0)), code)
    depth = 0
    positions = []
    for i, c in enumerate(masked):
        if c in "([{":
            depth += 1
        elif c in ")]}":
            depth -= 1
        elif c == "=" and depth == 0:
            prev = masked[i - 1] if i else ""
            nxt = masked[i + 1] if i + 1 < len(masked) else ""
            if prev not in "=!<>+-*/%&|^:@" and nxt != "=":
                positions.append(i)
    return positions


def scan(root=HERE, files=SCANNED):
    """Return {key: {"writes": [(file, line)], "reads": [(file, line)]}}.

    Writes: every subscript key on the LHS of an assignment (tuple-unpack
    targets included), every setdefault(), every pop() (a mutation site the
    migration must account for), and the dynamic-key sentinel. Reads: .get()
    everywhere plus subscripts on the RHS / in non-assignment lines."""
    inv = {}

    def add(kind, key, site):
        inv.setdefault(key, {"writes": [], "reads": []})[kind].append(site)

    for fn in files:
        path = os.path.join(root, fn)
        if not os.path.exists(path):
            continue
        with open(path, encoding="utf-8") as fh:
            file_lines = fh.readlines()
        for lineno, line in enumerate(file_lines, 1):
            code = line.split("#", 1)[0]   # comments are not sites
            site = (fn, lineno)
            if _DYNAMIC_RE.search(code):
                add("writes", DYNAMIC_SENTINEL, site)
            for m in _SETDEFAULT_RE.finditer(code):
                add("writes", m.group(1), site)
            for m in _POP_RE.finditer(code):
                add("writes", m.group(1), site)
            for m in _GET_RE.finditer(code):
                add("reads", m.group(1), site)
            positions = _assignment_positions(code)
            if positions:
                # Every segment before the LAST `=` is a target list (a
                # chain `a = b["_k"] = v` writes BOTH targets); only the
                # final segment is the RHS.
                lhs, rhs = code[:positions[-1]], code[positions[-1] + 1:]
                for m in _SUB_RE.finditer(lhs):
                    add("writes", m.group(1), site)
                for m in _SUB_RE.finditer(rhs):
                    add("reads", m.group(1), site)
            else:
                for m in _SUB_RE.finditer(code):
                    add("reads", m.group(1), site)
    return inv


class TestScannerAmendment(unittest.TestCase):
    """2.3(b): each formerly-blind write shape must be detected. Every case
    here was sabotage-validated against the 2.3(a) regex (which missed all
    four) before landing."""

    def _scan_snippet(self, text):
        import tempfile
        d = tempfile.mkdtemp()
        with open(os.path.join(d, "probe.py"), "w", encoding="utf-8") as fh:
            fh.write(text)
        return scan(root=d, files=("probe.py",))

    def test_setdefault_is_a_write(self):
        inv = self._scan_snippet('cfg.setdefault("_sess_map", {})\n')
        self.assertEqual([("probe.py", 1)], inv["_sess_map"]["writes"])

    def test_tuple_unpack_targets_are_all_writes(self):
        inv = self._scan_snippet('cfg["_pa"], cfg["_pb"] = load()\n')
        self.assertTrue(inv["_pa"]["writes"], "first tuple target missed")
        self.assertTrue(inv["_pb"]["writes"], "second tuple target missed")

    def test_pop_is_a_mutation_site(self):
        inv = self._scan_snippet('cfg.pop("_gone", None)\n')
        self.assertEqual([("probe.py", 1)], inv["_gone"]["writes"])

    def test_dynamic_key_records_the_sentinel(self):
        inv = self._scan_snippet('c.setdefault("_%s_sessions" % agent, {})\n')
        self.assertEqual([("probe.py", 1)], inv[DYNAMIC_SENTINEL]["writes"])

    def test_chained_assignment_counts_the_middle_target(self):
        # The c631159 first-match splitter filed the middle target of
        # `routing = cfg["_routing"] = load(...)` (orchestrator.py, the
        # band-D cache write) as an RHS read.
        inv = self._scan_snippet('routing = cfg["_routing"] = load(x)\n')
        self.assertEqual([("probe.py", 1)], inv["_routing"]["writes"])
        self.assertFalse(inv["_routing"]["reads"])

    def test_kwarg_equals_is_not_an_assignment(self):
        # The first splitter misread this real idiom (orchestrator.py:610)
        # as a write of _build_dir.
        inv = self._scan_snippet(
            'os.makedirs(cfg["_build_dir"], exist_ok=True)\n')
        self.assertFalse(inv["_build_dir"]["writes"])
        self.assertTrue(inv["_build_dir"]["reads"])

    def test_rhs_subscript_still_reads_and_comparison_not_a_write(self):
        inv = self._scan_snippet(
            'x = cfg["_r1"]\nif cfg["_r2"] == 3:\n    pass\n')
        self.assertFalse(inv["_r1"]["writes"])
        self.assertFalse(inv["_r2"]["writes"])
        self.assertTrue(inv["_r1"]["reads"] and inv["_r2"]["reads"])

    def test_uicrawl_is_scanned(self):
        self.assertIn("uicrawl.py", SCANNED)


class TestCfgKeyGate(unittest.TestCase):
    def test_no_new_underscore_keys(self):
        inv = scan()
        written = {k for k, v in inv.items() if v["writes"]}
        new = written - ALLOWED_WRITTEN_KEYS
        self.assertFalse(
            new,
            "NEW cfg underscore key(s) written: %s — new per-turn state rides "
            "TurnContext fields (V3 board 2.3); if a key is genuinely "
            "unavoidable, add it to ALLOWED_WRITTEN_KEYS with a reviewed "
            "reason in the commit message." % sorted(new))

    def test_allowlist_carries_no_dead_entries(self):
        # Shrinkage must be conscious: an allowlist entry nobody writes any
        # more is stale documentation — remove it in the same PR that
        # migrated the key.
        inv = scan()
        written = {k for k, v in inv.items() if v["writes"]}
        dead = ALLOWED_WRITTEN_KEYS - written
        self.assertFalse(
            dead,
            "Allowlist entries no longer written (remove them): %s"
            % sorted(dead))

    def test_inventory_doc_is_current(self):
        # The committed map must match the code — regen when it drifts.
        path = os.path.join(HERE, "TURNCONTEXT_INVENTORY.md")
        self.assertTrue(os.path.exists(path),
                        "run: python3 tests/test_cfg_key_inventory.py")
        with open(path, encoding="utf-8") as fh:
            doc = fh.read()
        for key in sorted({k for k, v in scan().items() if v["writes"]}):
            self.assertIn("`%s`" % key, doc,
                          "TURNCONTEXT_INVENTORY.md is stale — regenerate "
                          "with: python3 tests/test_cfg_key_inventory.py")


def render(inv):
    written = {k: v for k, v in inv.items() if v["writes"]}
    read_only = {k: v for k, v in inv.items() if not v["writes"] and v["reads"]}
    lines = []
    lines.append("# TurnContext migration inventory (V3 board 2.3a)")
    lines.append("")
    lines.append("GENERATED — do not hand-edit. Regenerate with:")
    lines.append("`python3 tests/test_cfg_key_inventory.py`. The companion")
    lines.append("gate (tests/test_cfg_key_inventory.py) fails on any NEW")
    lines.append("underscore key and on stale allowlist entries.")
    lines.append("")
    lines.append("%d written keys · %d write sites · %d read sites across %s"
                 % (len(written),
                    sum(len(v["writes"]) for v in written.values()),
                    sum(len(v["reads"]) for v in inv.values()),
                    ", ".join(SCANNED)))
    lines.append("")
    lines.append("| key | writes | reads | write sites |")
    lines.append("|---|---|---|---|")
    for key in sorted(written):
        v = written[key]
        sites = "; ".join("%s:%d" % s for s in v["writes"][:6])
        if len(v["writes"]) > 6:
            sites += " …(+%d)" % (len(v["writes"]) - 6)
        lines.append("| `%s` | %d | %d | %s |"
                     % (key, len(v["writes"]), len(v["reads"]), sites))
    if read_only:
        lines.append("")
        lines.append("Read-only keys (written nowhere in the scanned files —")
        lines.append("either dead reads or written via non-subscript paths;")
        lines.append("verify before migrating):")
        lines.append("")
        for key in sorted(read_only):
            v = read_only[key]
            lines.append("- `%s` — %d read(s): %s"
                         % (key, len(v["reads"]),
                            "; ".join("%s:%d" % s for s in v["reads"][:4])))
    lines.append("")
    return "\n".join(lines)


if __name__ == "__main__":
    inventory = scan()
    out_path = os.path.join(HERE, "TURNCONTEXT_INVENTORY.md")
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write(render(inventory))
    print("wrote %s (%d written keys)" % (
        out_path, len([k for k, v in inventory.items() if v["writes"]])))
