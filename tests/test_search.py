"""V3 board 2.6: the workspace FTS index over messages.jsonl.

Round-trips (index -> query -> the exact md block), incremental cursors
with delete+rescan on rewrite, reindex parity, degraded LIKE mode with
surfaced status, LIKE escaping, dot-file vs project discovery, the
artifact fixture path, corrupt-line resilience, and the 10k perf gate.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import unittest.mock

import events as evlib
import messages as msglib
import orchestrator as orch
import search


def _write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


class SearchBase(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="orch_search_")
        self.app = os.path.join(self.root, "gloam")

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def seed_phase(self, phase="design", rounds=2, project_dir=None):
        """A real-shaped phase .md + its 2.4 index lines."""
        app = project_dir or self.app
        md = os.path.join(app, "design", "d.md")
        text = "# Phase design\n\n### Round 1\n\n"
        blocks = []
        for rnd in range(1, rounds + 1):
            if rnd > 1:
                text += "\n### Round %d\n\n" % rnd
            b = ("**Codex — Round %d**\n\nthe kraken proposal mark%d\n"
                 % (rnd, rnd))
            blocks.append(b.strip())
            text += b
            c = ("**Coordinator (Codex) — decision after round %d**\n\n"
                 "keep going.\n" % rnd)
            blocks.append(c.strip())
            text += "\n" + c
        _write(md, text)
        for rnd in range(1, rounds + 1):
            msglib.append_message(app, "design", "turn", "codex", md, rnd=rnd)
            msglib.append_message(app, "design", "coordinator", "codex", md,
                                  rnd=rnd)
        return md, blocks


class TestRoundTrip(SearchBase):
    def test_index_query_returns_the_exact_block(self):
        self.seed_phase()
        stats = search.index_incremental(self.root)
        self.assertEqual(stats["new_lines"], 4)
        res = search.query(self.root, "kraken mark2")
        self.assertTrue(res["hits"])
        hit = res["hits"][0]
        self.assertEqual(hit["turn_id"], "design:2:codex:turn")
        self.assertEqual(hit["project"], "gloam")
        # content_path resolves back to the .md that holds the block
        md = os.path.join(self.app, hit["content_path"])
        with open(md, encoding="utf-8") as fh:
            self.assertIn("kraken proposal mark2", fh.read())

    def test_incremental_second_run_writes_nothing(self):
        self.seed_phase()
        search.index_incremental(self.root)
        stats = search.index_incremental(self.root)
        self.assertEqual(stats["new_lines"], 0)
        self.assertEqual(stats["rescans"], 0)

    def test_new_lines_after_first_index_are_picked_up(self):
        md, _ = self.seed_phase()
        search.index_incremental(self.root)
        with open(md, "a", encoding="utf-8") as fh:
            fh.write("\n### Round 3\n\n**Codex — Round 3**\n\nzanzibar\n")
        msglib.append_message(self.app, "design", "turn", "codex", md, rnd=3)
        stats = search.index_incremental(self.root)
        self.assertEqual(stats["new_lines"], 1)
        self.assertEqual(stats["rescans"], 0)
        self.assertTrue(search.query(self.root, "zanzibar")["hits"])

    def test_rewrite_invalidates_cursor_and_drops_removed_turns(self):
        # 2.4 reconciliation rewrites messages.jsonl smaller; the index
        # must DELETE the dropped turn, which upserts alone cannot do.
        md, _ = self.seed_phase(rounds=3)
        search.index_incremental(self.root)
        self.assertTrue(search.query(self.root, "mark3")["hits"])
        msglib.reconcile_messages(self.app, "design", keep_below_round=3)
        stats = search.index_incremental(self.root)
        self.assertEqual(stats["rescans"], 1)
        self.assertFalse(search.query(self.root, "mark3")["hits"],
                         "a reconciled-away turn must leave the index")
        hits = search.query(self.root, "kraken")["hits"]
        ids = [h["turn_id"] for h in hits]
        self.assertEqual(len(ids), len(set(ids)), "no duplicates on rescan")

    def test_reindex_equals_incremental(self):
        self.seed_phase()
        search.index_incremental(self.root)
        inc = search.query(self.root, "kraken")["hits"]
        search.reindex(self.root)
        full = search.query(self.root, "kraken")["hits"]
        self.assertEqual(sorted(h["turn_id"] for h in inc),
                         sorted(h["turn_id"] for h in full))

    def test_corrupt_jsonl_line_is_skipped(self):
        md, _ = self.seed_phase()
        with open(msglib.messages_path(self.app), "a",
                  encoding="utf-8") as fh:
            fh.write("{not json}\n")
        msglib.append_message(self.app, "design", "turn", "codex", md, rnd=9)
        stats = search.index_incremental(self.root)
        self.assertEqual(stats["new_lines"], 5, "corrupt line skipped, "
                                                "valid neighbors indexed")


class TestDegradedMode(SearchBase):
    def _degraded(self):
        return unittest.mock.patch.object(
            search, "_fts5_available", lambda conn: False)

    def test_like_fallback_serves_hits_and_says_so(self):
        self.seed_phase()
        with self._degraded():
            stats = search.index_incremental(self.root)
            self.assertEqual(stats["status"], search.STATUS_DEGRADED)
            res = search.query(self.root, "kraken")
            self.assertEqual(res["status"], search.STATUS_DEGRADED)
            self.assertTrue(res["hits"], "degraded is slower, never empty")

    def test_like_wildcards_are_escaped(self):
        self.seed_phase()
        with self._degraded():
            search.index_incremental(self.root)
            res = search.query(self.root, "%")
            self.assertFalse(res["hits"],
                             "a literal %% query must not match everything")

    def test_mode_parity_on_the_same_corpus(self):
        self.seed_phase()
        with self._degraded():
            search.index_incremental(self.root)
            degraded_ids = {h["turn_id"]
                            for h in search.query(self.root, "kraken")["hits"]}
        os.remove(search.db_path(self.root))
        search.index_incremental(self.root)
        fts_ids = {h["turn_id"]
                   for h in search.query(self.root, "kraken")["hits"]}
        self.assertEqual(degraded_ids, fts_ids)


class TestPairing(SearchBase):
    def test_system_blocks_are_skipped_not_mispaired(self):
        app = self.app
        md = os.path.join(app, "build", "b.md")
        _write(md, "# hdr\n\n### Iteration 1\n\n"
                   "**lane-a (Pragmatist) — Iteration 1**\n\nlane output\n\n"
                   "**Build verification — iteration 1 FAILED (swift)**\n\n"
                   "```\nerrs\n```\n\n"
                   "**Integrator (Codex) — after iteration 1**\n\nwired.\n")
        msglib.append_message(app, "build", "turn", "lane-a", md,
                              agent="codex", rnd=1)
        msglib.append_message(app, "build", "integrator", "codex", md, rnd=1)
        search.index_incremental(self.root)
        hits = search.query(self.root, "wired")["hits"]
        self.assertEqual([h["turn_id"] for h in hits],
                         ["build:1:codex:integrator"],
                         "the verify system block must not shift pairing")
        self.assertFalse(search.query(self.root, "errs")["hits"],
                         "unauthored system text is not indexed")

    def test_unpairable_line_indexes_metadata_only(self):
        md = os.path.join(self.app, "design", "d.md")
        _write(md, "# hdr\n")   # no blocks at all
        msglib.append_message(self.app, "design", "turn", "codex", md, rnd=1)
        search.index_incremental(self.root)
        conn, _ = search.open_db(self.root)
        try:
            (content,) = conn.execute(
                "SELECT content FROM messages WHERE turn_id=?",
                ("design:1:codex:turn",)).fetchone()
        finally:
            conn.close()
        self.assertEqual(content, "", "never the wrong text — empty instead")


class TestDiscoveryAndArtifacts(SearchBase):
    def test_db_files_never_look_like_projects(self):
        self.seed_phase()
        search.index_incremental(self.root)
        for suffix in ("", "-wal", "-shm"):
            p = search.db_path(self.root) + suffix
            if not os.path.exists(p):
                _write(p, "")
        _write(os.path.join(self.app, "initial_prompt",
                            "initial_prompt.md"), "p")
        apps = orch.find_apps(self.root)
        self.assertEqual([os.path.basename(a) for a in apps], ["gloam"])

    def test_artifact_published_fixture_is_indexed(self):
        self.seed_phase()
        evlib.emit_event(self.app, "artifact_published",
                         artifact_id="idea-batch-1", type="idea_batch",
                         version=2, path="artifacts/idea-batch-1.md")
        search.index_incremental(self.root)
        conn, _ = search.open_db(self.root)
        try:
            rows = conn.execute(
                "SELECT project, artifact_id, type, version "
                "FROM artifacts").fetchall()
        finally:
            conn.close()
        self.assertEqual(rows, [("gloam", "idea-batch-1", "idea_batch", 2)])


class TestPruneVanished(SearchBase):
    """Regression (audit A-33): _prune_vanished derived staleness from the
    messages table only, so sessions whose only indexed content was artifacts
    (or just a cursor) were never pruned after deletion — ghost hits forever."""

    def _seed_artifact(self, app, aid="spec-001",
                       body="The zanzibar protocol design notes"):
        # Hand-crafted structured store: exactly what lineage_index/read_body
        # consume (meta.json + body.md), without dragging in publish machinery.
        adir = os.path.join(app, "artifacts", aid)
        os.makedirs(adir, exist_ok=True)
        _write(os.path.join(adir, "meta.json"), json.dumps(
            {"id": aid, "type": "spec", "version": 1, "status": "final",
             "ts": "2026-07-18T00:00:00+00:00"}))
        _write(os.path.join(adir, "body.md"), body)

    def test_artifact_only_session_is_pruned_after_delete(self):
        # Empty messages.jsonl -> zero messages rows -> the old stale set
        # never contained "myapp"; its artifact rows answered queries forever.
        app = os.path.join(self.root, "myapp")
        _write(os.path.join(app, "messages.jsonl"), "")
        self._seed_artifact(app)
        search.index_incremental(self.root)
        self.assertTrue(search.query(self.root, "zanzibar")["hits"],
                        "artifact must be searchable while the session lives")
        shutil.rmtree(app)
        stats = search.index_incremental(self.root)
        self.assertEqual(stats.get("pruned"), 1)
        self.assertFalse(search.query(self.root, "zanzibar")["hits"],
                         "a deleted session's artifacts must leave the index")
        conn, _ = search.open_db(self.root)
        try:
            n = conn.execute("SELECT COUNT(*) FROM artifacts "
                             "WHERE project='myapp'").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(n, 0)

    def test_prune_removes_artifact_fts_rows_too(self):
        # Even for projects WITH messages rows the old loop left artifacts_fts
        # rows behind — invisible to query (the JOIN drops them) but leaked in
        # the index file permanently.
        self.seed_phase()
        self._seed_artifact(self.app, aid="spec-002", body="orphaned fts body")
        search.index_incremental(self.root)
        conn, status = search.open_db(self.root)
        conn.close()
        if status != search.STATUS_OK:
            self.skipTest("FTS5 unavailable — artifacts_fts does not exist")
        shutil.rmtree(self.app)
        search.index_incremental(self.root)
        conn, _ = search.open_db(self.root)
        try:
            n = conn.execute("SELECT COUNT(*) FROM artifacts_fts "
                             "WHERE project='gloam'").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(n, 0, "stale artifacts_fts rows must be pruned")

    def test_cursor_only_session_is_pruned_after_delete(self):
        # A messages.jsonl holding only corrupt lines stores a cursor (any
        # non-blank line does) but zero messages/artifacts rows — the cursor
        # row too must go when the session vanishes.
        app = os.path.join(self.root, "curse")
        _write(os.path.join(app, "messages.jsonl"), "{not json}\n")
        search.index_incremental(self.root)
        shutil.rmtree(app)
        stats = search.index_incremental(self.root)
        self.assertEqual(stats.get("pruned"), 1)
        conn, _ = search.open_db(self.root)
        try:
            n = conn.execute("SELECT COUNT(*) FROM cursors WHERE project "
                             "IN ('curse', 'ev|curse')").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(n, 0)


class TestPerf(SearchBase):
    def test_median_query_under_50ms_on_10k_messages(self):
        md = os.path.join(self.app, "design", "d.md")
        corpus_lines = ["# hdr\n"]
        jsonl = []
        for i in range(10000):
            corpus_lines.append(
                "**Codex — Round %d**\n\nfiller words alpha%d bravo "
                "charlie delta echo foxtrot golf hotel india\n\n" % (i, i))
            jsonl.append(json.dumps(
                {"turn_id": "design:%d:codex:turn" % i, "phase": "design",
                 "kind": "turn", "agent": "codex", "persona": "",
                 "round": i, "ts": "2026-07-18T00:00:00+00:00",
                 "content_path": "design/d.md"}))
        _write(md, "".join(corpus_lines))
        _write(msglib.messages_path(self.app), "\n".join(jsonl) + "\n")
        search.index_incremental(self.root)
        conn, status = search.open_db(self.root)
        conn.close()
        if status != search.STATUS_OK:
            self.skipTest("FTS5 unavailable — perf gate is FTS-mode only")
        search.query(self.root, "alpha7777")   # warm-up excluded
        times = []
        for _ in range(20):
            t0 = time.monotonic()
            res = search.query(self.root, "alpha7777")
            times.append(time.monotonic() - t0)
            self.assertTrue(res["hits"])
        times.sort()
        median = times[len(times) // 2]
        self.assertLess(median, 0.050,
                        "median query %.1fms exceeds the 50ms gate"
                        % (median * 1000))


class TestCli(SearchBase):
    def test_query_json_shape(self):
        self.seed_phase()
        out = subprocess.run(
            [sys.executable, os.path.join(os.path.dirname(
                os.path.dirname(os.path.abspath(__file__))), "search.py"),
             "--root", self.root, "--query", "kraken", "--json"],
            capture_output=True, text=True, timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr)
        res = json.loads(out.stdout)
        self.assertIn("status", res)
        self.assertTrue(res["hits"])
        self.assertEqual(
            set(res["hits"][0]),
            {"project", "phase", "round", "agent", "kind", "turn_id",
             "content_path", "snippet"})


if __name__ == "__main__":
    unittest.main()
