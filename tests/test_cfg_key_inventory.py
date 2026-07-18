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
SCANNED = ("orchestrator.py", "visualqa.py", "urlfetch.py")

# Single leading underscore only: __dunder__ keys are round-offset dict
# entries (_seen_chars["__coord__"], _lane_seen["__integrator__"]), not
# cfg state.
_WRITE_RE = re.compile(r'\w+\[\s*"(_[a-z0-9][a-z0-9_]*)"\s*\]\s*=(?!=)')
_REF_RE = re.compile(r'(?:\[\s*"(_[a-z0-9][a-z0-9_]*)"\s*\]|\.get\(\s*"(_[a-z0-9][a-z0-9_]*)")')

# The frozen inventory (2.3a baseline). Shrink freely as TurnContext absorbs
# keys; grow ONLY with a reviewed reason recorded in the commit.
ALLOWED_WRITTEN_KEYS = {
    "_agent_health", "_agent_role_overrides", "_allow_writes",
    "_app_dir", "_autonomy", "_base_models",
    "_base_resolved", "_budget", "_build_dir",
    "_checked_any_agent_runnable", "_claude_model_override", "_completeness",
    "_deadline", "_drop_prior_discussions", "_explicit_app",
    "_gemini_disabled_reason", "_gemini_unavailable", "_health_key",
    "_installed_ollama_models", "_iter_verify_toolchain_absent", "_knowledge",
    "_new_session_id", "_noted_indep_grader", "_noted_local_active_limit",
    "_noted_local_lane_skip", "_noted_local_ram_gate", "_noted_ollama_sprint_skip",
    "_noted_ollama_uninstalled_skip", "_original_prompt", "_phase_deadline",
    "_phase_exemplar", "_phase_instructions", "_phase_key",
    "_phase_playbook", "_prior_disc_cap", "_prior_discussions",
    "_read_dir", "_resolved", "_role_by_id",
    "_role_routing", "_roles", "_round_multiplier",
    "_routed_rounds", "_routed_turn_timeout", "_routing",
    "_session", "_session_cwd", "_sim_ctx",
    "_state", "_target_digest", "_target_path",
    "_target_paths", "_tech_stack_block", "_turn_timeout",
    "_url_context", "_verify_context", "_warned_no_git_repo",
    "_workflow_name", "_workflow_target", "_workflow_verify_spec",
}


def scan(root=HERE, files=SCANNED):
    """Return {key: {"writes": [(file, line)], "reads": [(file, line)]}}."""
    inv = {}
    for fn in files:
        path = os.path.join(root, fn)
        if not os.path.exists(path):
            continue
        for lineno, line in enumerate(open(path, encoding="utf-8"), 1):
            code = line.split("#", 1)[0]   # comments are not sites
            for m in _WRITE_RE.finditer(code):
                inv.setdefault(m.group(1), {"writes": [], "reads": []})[
                    "writes"].append((fn, lineno))
            for m in _REF_RE.finditer(code):
                key = m.group(1) or m.group(2)
                # A write line also matches the subscript ref pattern; only
                # count it once, as a write.
                if _WRITE_RE.search(code) and any(
                        w == (fn, lineno)
                        for w in inv.get(key, {}).get("writes", [])):
                    continue
                inv.setdefault(key, {"writes": [], "reads": []})[
                    "reads"].append((fn, lineno))
    return inv


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
