"""V3 board 3.0 (sub-PR A): the nested workspace layout — engine side.

Nested discovery with every recursion guard, flat regression, session-id
addressing, the lock-encoding contract (shared fixture, collision cases),
call-log filename flattening, session minting, the dry-run/apply/idempotent
migration, and search-index pruning of migrated-away projects.
"""
import io
import json
import os
import shutil
import tempfile
import unittest

import messages as msglib
import orchestrator as orch
import search

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURE = os.path.join(HERE, "tests", "fixtures", "lock_encoding.json")


def _mint(root, rel, prompt="p", marker=True):
    d = os.path.join(root, rel)
    os.makedirs(os.path.join(d, "initial_prompt"), exist_ok=True)
    if marker and "/" in rel:
        open(os.path.join(root, rel.split("/")[0], orch.SECTIONS_MARKER),
             "w").close()
    with open(os.path.join(d, "initial_prompt", "initial_prompt.md"),
              "w", encoding="utf-8") as fh:
        fh.write(prompt)
    return d


class NestedBase(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="orch_nested_")

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)


class TestDiscovery(NestedBase):
    def test_flat_and_nested_coexist(self):
        _mint(self.root, "flatapp")
        _mint(self.root, "gloam/ideas/first-chat")
        _mint(self.root, "gloam/research/deep-dive")
        self.assertEqual(orch.find_apps(self.root),
                         ["flatapp", "gloam/ideas/first-chat",
                          "gloam/research/deep-dive"])

    def test_flat_project_is_never_recursed(self):
        # A dir with its OWN initial_prompt is flat, full stop — an inner
        # chat-shaped dir inside it must stay invisible (T7).
        _mint(self.root, "flatapp")
        _mint(self.root, "flatapp/ideas/sneaky")
        self.assertEqual(orch.find_apps(self.root), ["flatapp"])

    def test_legacy_agent_state_root_is_never_recursed(self):
        # 0.2 rule: root-level agent_state.json = legacy single-chat
        # project forever, even without initial_prompt.
        legacy = os.path.join(self.root, "legacy")
        os.makedirs(legacy)
        with open(os.path.join(legacy, "agent_state.json"), "w") as fh:
            fh.write("{}")
        _mint(self.root, "legacy/ideas/hidden")
        self.assertEqual(orch.find_apps(self.root), [])

    def test_dot_and_archived_skips_at_every_level(self):
        _mint(self.root, "p/.hidden/c")
        _mint(self.root, "p/s/.hidden")
        d = _mint(self.root, "p/s/archived-chat")
        open(os.path.join(d, ".orch_archived"), "w").close()
        sdir = _mint(self.root, "p/archived-section/c")
        open(os.path.join(os.path.dirname(sdir), ".orch_archived"),
             "w").close()
        _mint(self.root, "p/s/live")
        self.assertEqual(orch.find_apps(self.root), ["p/s/live"])

    def test_unmarked_wrapper_dirs_are_never_discovered(self):
        # The old flat contract promised nested dirs inside wrapper/batch
        # folders are IGNORED — verified regression: without the marker
        # rule, a user archive like backups/2025/myproj would suddenly RUN.
        _mint(self.root, "backups/keep/oldproj", marker=False)
        self.assertEqual(orch.find_apps(self.root), [])
        open(os.path.join(self.root, "backups", orch.SECTIONS_MARKER),
             "w").close()
        self.assertEqual(orch.find_apps(self.root), ["backups/keep/oldproj"])

    def test_unaddressable_segment_dirs_are_not_discovered(self):
        # Discovery/addressing symmetry: a chat dir named "a..b" can never
        # be targeted by --app, so it must not be discovered either.
        _mint(self.root, "p/s/a..b")
        _mint(self.root, "p/s/fine")
        self.assertEqual(orch.find_apps(self.root), ["p/s/fine"])

    def test_unaddressable_project_segment_is_not_recursed(self):
        _mint(self.root, "a..b/s/c")   # marker minted, but unaddressable
        self.assertEqual(orch.find_apps(self.root), [])

    def test_process_app_join_reaches_the_nested_dir(self):
        d = _mint(self.root, "p/s/c", prompt="nested prompt")
        self.assertEqual(
            orch.read_initial_prompt(os.path.join(self.root, "p/s/c")),
            "nested prompt")
        self.assertEqual(d, os.path.join(self.root, "p", "s", "c"))


class TestAddressing(NestedBase):
    def test_valid_ids(self):
        self.assertEqual(orch.parse_session_id("flatapp"), "flatapp")
        self.assertEqual(orch.parse_session_id("p/s/c"), "p/s/c")

    def test_rejections(self):
        for bad in ("", "a/b", "a/b/c/d", "a//c", "../x/y", "a/../c",
                    "/abs/o/lute", "a/b/", ".hidden/b/c", "a/.h/c",
                    "a/b/.c", "a\\b\\c", "a/b/c..d/e"):
            self.assertIsNone(orch.parse_session_id(bad), bad)

    def test_valid_app_slug_is_byte_identical_for_flat(self):
        # T1: the flat guard itself is untouched.
        for name, ok in (("gloam", True), ("a--b", True), ("a.b", True),
                         (".h", False), ("a/b", False), ("a..b", False),
                         ("", False)):
            self.assertEqual(orch.valid_app_slug(name), ok, name)


class TestFinalizeInValidation(NestedBase):
    """A-79: --finalize-in must ride main()'s session-id validation like its
    siblings (route_from/route_to) — an invalid id exits 2 via the clean
    ap.error instead of crashing _do_finalize with an AttributeError."""

    def test_invalid_finalize_in_is_a_clean_cli_error(self):
        import sys
        import unittest.mock as mock
        argv = ["orchestrator.py", "--root", self.root,
                "--finalize-artifact", "x", "--finalize-in", "a/b"]
        err = io.StringIO()
        with mock.patch.object(sys, "argv", argv), \
                mock.patch.object(sys, "stderr", err):
            with self.assertRaises(SystemExit) as ctx:
                orch.main()
        self.assertEqual(ctx.exception.code, 2)
        self.assertIn("invalid project name", err.getvalue())

    def test_do_finalize_guard_returns_2_not_attributeerror(self):
        # Belt-and-suspenders for direct callers that skip main()'s loop.
        class _Args:
            finalize_in = "a/b"
            finalize_artifact = "x"
            finalize_by = "cli"
            by_human = False
        self.assertEqual(orch._do_finalize({"root": self.root}, _Args()), 2)


class TestLockEncoding(NestedBase):
    def test_fixture_pins_both_implementations(self):
        with open(FIXTURE, encoding="utf-8") as fh:
            doc = json.load(fh)
        self.assertGreaterEqual(len(doc["cases"]), 8)
        for case in doc["cases"]:
            self.assertEqual(orch.encode_lock_name(case["id"]), case["lock"],
                             case["id"])

    def test_flat_names_stay_raw(self):
        self.assertEqual(orch.encode_lock_name("gloam"), "gloam")
        self.assertEqual(orch.encode_lock_name("proj--section--chat"),
                         "proj--section--chat")

    def test_collision_cases_yield_distinct_locks(self):
        nested = orch.encode_lock_name("proj/section/chat")
        literal_flat = orch.encode_lock_name("proj--section--chat")
        adversarial_flat = orch.encode_lock_name("proj%2Fsection%2Fchat")
        self.assertNotEqual(nested, literal_flat)
        # The hash suffix is the whole defense here: without it the
        # adversarial flat name IS the quoted nested triple.
        self.assertNotEqual(nested, adversarial_flat)
        self.assertTrue(nested.startswith(adversarial_flat))

    def test_nfc_and_nfd_spellings_share_one_lock(self):
        # APFS is normalization-insensitive for DIRS, but percent-encoding
        # ASCII-fies bytes so the fs could never coalesce two stems —
        # verified double-run before quoting the NFC form.
        import unicodedata
        nfc = "café/section/chat"
        nfd = unicodedata.normalize("NFD", nfc)
        self.assertNotEqual(nfc, nfd)
        self.assertEqual(orch.encode_lock_name(nfc),
                         orch.encode_lock_name(nfd))

    def test_case_variant_ids_share_one_digest(self):
        # APFS (case/normalization-insensitive) maps Gloam/… and gloam/…
        # to ONE session dir — verified double-run without this: the
        # digest coalesces so the lock files collide exactly when the
        # dirs do; the quoted part still separates them on case-SENSITIVE
        # volumes.
        a = orch.encode_lock_name("gloam/ideas/x")
        b = orch.encode_lock_name("Gloam/Ideas/X")
        self.assertNotEqual(a, b, "quoted parts must stay distinct")
        self.assertEqual(a.rsplit(".", 1)[1], b.rsplit(".", 1)[1],
                         "digests must coalesce for fs-coalescing ids")

    def test_stale_probe_consults_the_encoded_session_lock(self):
        # Verified failure: basename-derived probing read c.lock while a
        # live run held p%2Fs%2Fc.<hash>.lock — resume then clobbered a
        # live session's state.
        orig = orch.LOCKS_DIR
        orch.LOCKS_DIR = os.path.join(self.root, ".orch-locks")
        try:
            app_dir = _mint(self.root, "p/s/c")
            self.assertTrue(orch.acquire_app_lock("p/s/c", 3600))
            state = {"status": "running",
                     "last_processed": "2020-01-01 00:00:00"}
            self.assertFalse(
                orch._is_stale_running_state(app_dir, state, app="p/s/c"),
                "a LIVE encoded lock must block the stale verdict")
            orch.release_app_lock("p/s/c")
        finally:
            orch.LOCKS_DIR = orig

    def test_acquire_and_release_nested_lock(self):
        orig = orch.LOCKS_DIR
        orch.LOCKS_DIR = os.path.join(self.root, ".orch-locks")
        try:
            self.assertTrue(orch.acquire_app_lock("p/s/c", 3600))
            expected = os.path.join(orch.LOCKS_DIR,
                                    orch.encode_lock_name("p/s/c") + ".lock")
            self.assertTrue(os.path.exists(expected))
            self.assertFalse(os.path.exists(
                os.path.join(orch.LOCKS_DIR, "p")),
                "a nested id must never nest under .orch-locks")
            orch.release_app_lock("p/s/c")
            self.assertFalse(os.path.exists(expected))
        finally:
            orch.LOCKS_DIR = orig

    def test_call_log_filename_is_flattened(self):
        orig = orch.LOG_DIR
        orch.LOG_DIR = os.path.join(self.root, "logs")
        os.makedirs(orch.LOG_DIR)
        try:
            orch.write_call_log("p/s/c", "design", 1, "codex", "cmd", "o",
                                "e", 0)
            names = os.listdir(orch.LOG_DIR)
            self.assertEqual(len(names), 1)
            self.assertIn(orch.encode_lock_name("p/s/c"), names[0])
            self.assertNotIn("/", names[0])
        finally:
            orch.LOG_DIR = orig


class TestMinting(NestedBase):
    def test_create_session_nested(self):
        d = orch.create_session(self.root, "p/s/c", "build me",
                                workflow="chat_ideas")
        self.assertEqual(orch.read_initial_prompt(d), "build me\n")
        with open(os.path.join(d, "workflow.txt"), encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "chat_ideas\n")
        self.assertEqual(orch.find_apps(self.root), ["p/s/c"])

    def test_create_session_refuses_existing_and_invalid(self):
        orch.create_session(self.root, "p/s/c", "x")
        with self.assertRaises(orch.AppError):
            orch.create_session(self.root, "p/s/c", "x")
        with self.assertRaises(orch.AppError):
            orch.create_session(self.root, "../evil", "x")

    def test_refuses_nesting_into_a_user_wrapper_dir(self):
        # Verified: minting the marker on a pre-existing unmarked dir made
        # an archived project inside it suddenly discoverable/runnable.
        os.makedirs(os.path.join(self.root, "backups", "keep", "oldproj"))
        with self.assertRaises(orch.AppError):
            orch.create_session(self.root, "backups/new/chat", "x")
        # An EMPTY pre-existing dir is harmless — minting proceeds.
        os.makedirs(os.path.join(self.root, "fresh"))
        orch.create_session(self.root, "fresh/s/c", "x")
        self.assertEqual(orch.find_apps(self.root), ["fresh/s/c"])

    def test_section_level_file_is_a_clean_refusal(self):
        os.makedirs(os.path.join(self.root, "p"))
        open(os.path.join(self.root, "p", orch.SECTIONS_MARKER), "w").close()
        open(os.path.join(self.root, "p", "s"), "w").close()   # a FILE
        with self.assertRaises(orch.AppError):
            orch.create_session(self.root, "p/s/c", "x")

    def test_create_session_refuses_nesting_under_a_flat_project(self):
        # Verified data-hiding: a session nested inside a flat project is
        # invisible to every discovery surface forever.
        _mint(self.root, "gloam")
        with self.assertRaises(orch.AppError):
            orch.create_session(self.root, "gloam/ideas/new", "x")
        legacy = os.path.join(self.root, "old")
        os.makedirs(legacy)
        open(os.path.join(legacy, "agent_state.json"), "w").write("{}")
        with self.assertRaises(orch.AppError):
            orch.create_session(self.root, "old/ideas/new", "x")


class TestMigration(NestedBase):
    def _plan(self, apply=False):
        lines = []
        orch.migrate_layout(self.root, apply=apply, out=lines.append)
        return lines

    def test_dry_run_reports_and_moves_nothing(self):
        _mint(self.root, "gloam--ideas--first")
        _mint(self.root, "gloam--research--deep")
        _mint(self.root, "legacyproj")                 # flat, no '--' triple
        _mint(self.root, "odd--pair")                  # 2 segments: ambiguous
        os.makedirs(os.path.join(self.root, "x--y--z"))   # no initial_prompt
        lines = self._plan(apply=False)
        self.assertIn("MIGRATE: gloam--ideas--first -> gloam/ideas/first",
                      lines)
        self.assertIn("MIGRATE: gloam--research--deep -> gloam/research/deep",
                      lines)
        self.assertIn("SKIP (ambiguous): odd--pair", lines)
        self.assertIn("SKIP (not a session): x--y--z", lines)
        self.assertTrue(os.path.isdir(
            os.path.join(self.root, "gloam--ideas--first")),
            "dry-run must move nothing")

    def test_apply_moves_renames_lock_and_is_idempotent(self):
        _mint(self.root, "gloam--ideas--first")
        locks = os.path.join(self.root, ".orch-locks")
        os.makedirs(locks)
        with open(os.path.join(locks, "gloam--ideas--first.lock"), "w") as fh:
            fh.write("pid=999999\n")   # stale — rides along
        orig = orch._app_lock_has_live_owner
        orch._app_lock_has_live_owner = lambda name: False
        try:
            self._plan(apply=True)
        finally:
            orch._app_lock_has_live_owner = orig
        self.assertFalse(os.path.exists(
            os.path.join(self.root, "gloam--ideas--first")))
        self.assertEqual(orch.find_apps(self.root), ["gloam/ideas/first"])
        self.assertTrue(os.path.exists(os.path.join(
            locks, orch.encode_lock_name("gloam/ideas/first") + ".lock")))
        again = self._plan(apply=True)
        self.assertIn("migration applied: 0 to migrate, 0 skipped", again)

    def test_live_locked_session_is_refused(self):
        _mint(self.root, "gloam--ideas--live")
        locks = os.path.join(self.root, ".orch-locks")
        os.makedirs(locks)
        with open(os.path.join(locks, "gloam--ideas--live.lock"), "w") as fh:
            fh.write("pid=%d\n" % os.getpid())
        orig = orch._app_lock_has_live_owner
        orch._app_lock_has_live_owner = lambda name: True
        try:
            lines = self._plan(apply=True)
        finally:
            orch._app_lock_has_live_owner = orig
        self.assertIn("SKIP (running): gloam--ideas--live", lines)
        self.assertTrue(os.path.isdir(
            os.path.join(self.root, "gloam--ideas--live")))

    def test_migration_refuses_a_user_wrapper_destination(self):
        os.makedirs(os.path.join(self.root, "backups", "keep", "old"))
        _mint(self.root, "backups--sec--chat")
        lines = self._plan(apply=True)
        self.assertTrue(any("SKIP (project-level conflict" in l
                            and "backups--sec--chat" in l for l in lines),
                        lines)
        self.assertEqual(orch.find_apps(self.root), ["backups--sec--chat"])

    def test_project_level_conflict_is_skipped_not_hidden(self):
        # THE verification finding: migrating gloam--ideas--x while a flat
        # project "gloam" exists would nest the chat inside it — invisible
        # to discovery, index pruned. Must SKIP loudly instead.
        _mint(self.root, "gloam")
        _mint(self.root, "gloam--ideas--x")
        lines = self._plan(apply=True)
        self.assertTrue(any(l.startswith(
            "SKIP (project-level conflict — existing flat project): "
            "gloam--ideas--x") for l in lines), lines)
        self.assertTrue(os.path.isdir(
            os.path.join(self.root, "gloam--ideas--x")),
            "the source must be left untouched")
        self.assertEqual(orch.find_apps(self.root),
                         ["gloam", "gloam--ideas--x"])

    def test_clean_no_op_on_empty_workspace(self):
        _mint(self.root, "plainproject")
        lines = self._plan(apply=True)
        self.assertEqual(lines,
                         ["migration applied: 0 to migrate, 0 skipped"])


class TestSearchIntegration(NestedBase):
    def test_scan_failure_never_wipes_the_index(self):
        # Verified: a transient listdir failure looked like an empty
        # workspace and _prune_vanished deleted EVERY row.
        flat = _mint(self.root, "plainproj")
        md = os.path.join(flat, "design", "d.md")
        os.makedirs(os.path.dirname(md))
        with open(md, "w", encoding="utf-8") as fh:
            fh.write("# h\n\n### Round 1\n\n**Codex — Round 1**\n\nplugh\n")
        msglib.append_message(flat, "design", "turn", "codex", md, rnd=1)
        search.index_incremental(self.root)
        self.assertTrue(search.query(self.root, "plugh")["hits"])
        os.chmod(self.root, 0o311)   # listdir raises EACCES
        try:
            stats = search.index_incremental(self.root)
        finally:
            os.chmod(self.root, 0o755)
        self.assertTrue(stats.get("scan_failed"))
        self.assertEqual(stats.get("pruned"), None)
        self.assertTrue(search.query(self.root, "plugh")["hits"],
                        "rows must survive a failed scan")

    def test_partial_scan_never_prunes(self):
        # Verified one level down from the root case: an unreadable
        # PROJECT dir hid its sessions and pruning wiped their rows.
        nested = _mint(self.root, "gloam/ideas/first")
        md = os.path.join(nested, "design", "d.md")
        os.makedirs(os.path.dirname(md))
        with open(md, "w", encoding="utf-8") as fh:
            fh.write("# h\n\n### Round 1\n\n**Codex — Round 1**\n\nxyzqq\n")
        msglib.append_message(nested, "design", "turn", "codex", md, rnd=1)
        search.index_incremental(self.root)
        self.assertTrue(search.query(self.root, "xyzqq")["hits"])
        blocked = os.path.join(self.root, "gloam")
        os.chmod(blocked, 0o000)
        try:
            stats = search.index_incremental(self.root)
        finally:
            os.chmod(blocked, 0o755)
        self.assertTrue(stats.get("scan_partial"))
        self.assertIsNone(stats.get("pruned"))
        self.assertTrue(search.query(self.root, "xyzqq")["hits"],
                        "an unreadable subtree must never prune its rows")

    def test_nested_sessions_index_and_migrated_ids_prune(self):
        flat = _mint(self.root, "gloam--ideas--first")
        md = os.path.join(flat, "design", "d.md")
        os.makedirs(os.path.dirname(md))
        with open(md, "w", encoding="utf-8") as fh:
            fh.write("# h\n\n### Round 1\n\n**Codex — Round 1**\n\nxyzzy\n")
        msglib.append_message(flat, "design", "turn", "codex", md, rnd=1)
        search.index_incremental(self.root)
        self.assertTrue(search.query(self.root, "xyzzy")["hits"])
        orig = orch._app_lock_has_live_owner
        orch._app_lock_has_live_owner = lambda name: False
        try:
            orch.migrate_layout(self.root, apply=True, out=lambda _l: None)
        finally:
            orch._app_lock_has_live_owner = orig
        stats = search.index_incremental(self.root)
        self.assertEqual(stats.get("pruned"), 1,
                         "the dead flat id must leave the index")
        hits = search.query(self.root, "xyzzy")["hits"]
        self.assertEqual([h["project"] for h in hits],
                         ["gloam/ideas/first"],
                         "the nested id must serve the hit now")


if __name__ == "__main__":
    unittest.main()
