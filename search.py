"""Workspace-level full-text search over messages.jsonl (V3 board 2.6).

Indexes every project's messages.jsonl (the 2.4 per-turn index) into ONE
SQLite database at <root>/.orch-search.db — a dot-file, so engine
find_apps and the GUI's project discovery never mistake it (or its
WAL/SHM siblings) for a project. stdlib sqlite3 only.

Design:
  * messages is a REGULAR table (search must work without FTS5); an FTS5
    mirror (messages_fts) is created only when the probe passes. FTS5
    absent or the mirror broken => LIKE fallback over messages.content
    with %/_ escaped, and the returned status SAYS so
    ("degraded:fts5-unavailable") — a capability loss is surfaced, never
    rendered as silently-empty results.
  * Turn CONTENT comes from each line's content_path (.md) — the JSONL is
    an index, not a body store. Blocks are recovered by ORDER-PRESERVING
    pairing: a phase file's index lines and its authored "**…**" blocks
    are both append-ordered, so each line takes the next block whose
    header is CONSISTENT with its kind/round (system notes pair with
    nothing and are skipped). A line whose block cannot be paired indexes
    metadata-only (content "") — never the wrong text.
  * Incremental cursor per project = (byte offset, last line length,
    last line sha256). On open, the stored tail is re-read and hashed; any
    mismatch means the file was REWRITTEN (2.4 resume reconciliation) =>
    DELETE the project's rows and rescan from zero. The delete is the part
    upsert-by-turn_id cannot do: reconciliation removes lines, and an
    upsert never removes rows — without it the index would permanently
    claim turns the transcript dropped.
  * artifact_published events (2.5 vocabulary) are indexed from
    events.jsonl by FULL RESCAN, not a cursor: every index tick deletes
    the project's artifact rows and re-reads the whole events.jsonl, so
    retractions and rewrites can never leave phantom rows. The
    "ev|<project>" cursor key of the abandoned incremental design
    survives only as a defensive delete in _prune_vanished, shedding
    rows written by older DBs.

CLI:  python3 search.py [--root DIR] --reindex
      python3 search.py [--root DIR] --query TEXT [--json] [--limit N]
"""

import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys

import artifacts as artifactslib

DB_FILENAME = ".orch-search.db"
SCHEMA_VERSION = "2"
STATUS_OK = "ok"
STATUS_DEGRADED = "degraded:fts5-unavailable"

_BLOCK_HDR_RE = re.compile(r"^\*\*(.+?)\*\*\s*$")
_SECTION_RE = re.compile(r"^### ")


def db_path(root):
    return os.path.join(root, DB_FILENAME)


def _fts5_available(conn):
    try:
        conn.execute("CREATE VIRTUAL TABLE temp._fts_probe USING fts5(x)")
        conn.execute("DROP TABLE temp._fts_probe")
        return True
    except sqlite3.Error:
        return False


def open_db(root):
    """Open (creating if needed) the workspace index. Returns (conn,
    status); status is degraded when FTS5 is unavailable."""
    conn = sqlite3.connect(db_path(root), timeout=5.0)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("""CREATE TABLE IF NOT EXISTS meta (
        key TEXT PRIMARY KEY, value TEXT)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS cursors (
        project TEXT PRIMARY KEY, offset INTEGER NOT NULL,
        last_len INTEGER NOT NULL, last_sha TEXT NOT NULL)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS messages (
        project TEXT NOT NULL, turn_id TEXT NOT NULL, phase TEXT,
        kind TEXT, agent TEXT, persona TEXT, round INTEGER,
        content_path TEXT, content TEXT DEFAULT '',
        PRIMARY KEY (project, turn_id))""")
    conn.execute("""CREATE TABLE IF NOT EXISTS artifacts (
        project TEXT NOT NULL, artifact_id TEXT NOT NULL,
        type TEXT, version INTEGER, path TEXT, ts TEXT,
        status TEXT, content TEXT DEFAULT '',
        PRIMARY KEY (project, artifact_id, version))""")
    columns = {r[1] for r in conn.execute("PRAGMA table_info(artifacts)")}
    if "status" not in columns:
        conn.execute("ALTER TABLE artifacts ADD COLUMN status TEXT")
    if "content" not in columns:
        conn.execute("ALTER TABLE artifacts ADD COLUMN content TEXT DEFAULT ''")
    conn.execute(
        "INSERT OR REPLACE INTO meta (key, value) VALUES ('schema_version', ?)",
        (SCHEMA_VERSION,))
    status = STATUS_OK
    if _fts5_available(conn):
        conn.execute("""CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts
            USING fts5(content, project UNINDEXED, turn_id UNINDEXED)""")
        conn.execute("""CREATE VIRTUAL TABLE IF NOT EXISTS artifacts_fts
            USING fts5(content, project UNINDEXED, artifact_id UNINDEXED)""")
    else:
        status = STATUS_DEGRADED
    conn.commit()
    return conn, status


def _has_fts(conn):
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE name='messages_fts'").fetchone()
    return row is not None


def _has_artifact_fts(conn):
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE name='artifacts_fts'").fetchone()
    return row is not None


# ---------------------------------------------------------------------------
# Content pairing
# ---------------------------------------------------------------------------

def _split_blocks(md_text):
    """The file's '**…**'-headed blocks, in order: [(header, block_text)].
    A block ends at the next bold header or '### ' section line."""
    lines = md_text.splitlines()
    starts = [i for i, ln in enumerate(lines) if _BLOCK_HDR_RE.match(ln)]
    blocks = []
    for n, i in enumerate(starts):
        end = len(lines)
        for j in range(i + 1, len(lines)):
            if _BLOCK_HDR_RE.match(lines[j]) or _SECTION_RE.match(lines[j]):
                end = j
                break
        blocks.append((lines[i], "\n".join(lines[i:end]).strip()))
    return blocks


def _header_matches(header, kind, rnd):
    """Is this block header consistent with an index line of kind/round?
    Deliberately display-name-agnostic (labels change; numbers don't)."""
    h = header.lower()
    if kind == "human":
        return h.startswith("**you (human)")
    if kind == "repair":
        return h.startswith("**repair %d" % rnd)
    if kind == "vote":
        return h.endswith("— vote**")
    if kind == "tally":
        return "vote tally" in h
    if kind == "contract":
        return "-json repair**" in h
    if kind == "quality":
        return h.startswith("**quality gate") and ("round %d" % rnd) in h
    if kind == "review":
        return "cross-review (iteration %d)" % rnd in h
    if kind == "integrator":
        return h.startswith("**integrator") and ("iteration %d" % rnd) in h
    if kind == "coordinator":
        return h.startswith("**coordinator") and (
            "round %d" % rnd in h or "iteration %d" % rnd in h)
    if kind == "retry":
        return "(retry on " in h and ("round %d" % rnd) in h
    # turn / pass / skip
    return ("round %d" % rnd) in h or ("iteration %d" % rnd) in h


def _pair_contents(md_text, line_rows):
    """Order-preserving pairing: each row (dict with kind/round) takes the
    next consistent block. Returns [content_or_empty] aligned to rows."""
    blocks = _split_blocks(md_text)
    out = []
    ptr = 0
    for row in line_rows:
        content = ""
        j = ptr
        while j < len(blocks):
            header, text = blocks[j]
            if _header_matches(header, row.get("kind") or "",
                               int(row.get("round") or 0)):
                content = text
                ptr = j + 1
                break
            j += 1
        out.append(content)
    return out


# ---------------------------------------------------------------------------
# Incremental indexing
# ---------------------------------------------------------------------------

def _tail_sig(path, offset, last_len):
    """(exists, valid) for a stored cursor against the current file."""
    try:
        size = os.path.getsize(path)
    except OSError:
        return False, False
    if offset > size or last_len > offset:
        return True, False
    if offset == 0:
        return True, True
    with open(path, "rb") as fh:
        fh.seek(offset - last_len)
        tail = fh.read(last_len)
    return True, hashlib.sha256(tail).hexdigest()


def _cursor_valid(conn, project, path):
    row = conn.execute(
        "SELECT offset, last_len, last_sha FROM cursors WHERE project=?",
        (project,)).fetchone()
    if row is None:
        return 0, True   # nothing indexed yet — start at zero, no delete
    offset, last_len, last_sha = row
    exists, sig = _tail_sig(path, offset, last_len)
    if not exists:
        return 0, False
    if sig is True:
        return 0, True
    if sig is False or sig != last_sha:
        return 0, False   # rewritten -> delete + rescan
    return offset, True


def _store_cursor(conn, project, offset, last_line):
    # last_line INCLUDES its newline terminator: the validation window is
    # [offset - last_len, offset) and offset sits after the "\n".
    raw = last_line.encode("utf-8", "replace")
    conn.execute(
        "INSERT OR REPLACE INTO cursors (project, offset, last_len, last_sha) "
        "VALUES (?, ?, ?, ?)",
        (project, offset, len(raw), hashlib.sha256(raw).hexdigest()))


def _delete_project_rows(conn, project):
    conn.execute("DELETE FROM messages WHERE project=?", (project,))
    if _has_fts(conn):
        conn.execute("DELETE FROM messages_fts WHERE project=?", (project,))


def _read_new_lines(path, offset):
    """New complete lines after offset. Returns (lines, new_offset,
    last_raw_line). A trailing unterminated line is left for next time."""
    with open(path, "rb") as fh:
        fh.seek(offset)
        chunk = fh.read()
    lines, consumed, last_raw = [], 0, ""
    for m in re.finditer(rb"([^\n]*)\n", chunk):
        raw = m.group(1).decode("utf-8", "replace")
        consumed = m.end()
        if raw.strip():
            lines.append(raw)
            last_raw = raw + "\n"
    return lines, offset + consumed, last_raw


def _projects(root):
    """Flat projects AND nested sessions (V3 3.0: project/section/chat) —
    any dir at either depth with a messages.jsonl. The project column
    carries the session id verbatim."""
    try:
        names = sorted(os.listdir(root))
    except OSError:
        return None   # scan FAILED — callers must not treat as "empty"
    out = []
    complete = [True]   # ANY subtree error makes pruning unsafe

    def probe(pid, d):
        if os.path.isdir(d) and \
                os.path.exists(os.path.join(d, "messages.jsonl")):
            out.append((pid, d))
            return True
        return False

    for name in names:
        if name.startswith("."):
            continue
        app_dir = os.path.join(root, name)
        if not os.path.isdir(app_dir):
            continue
        if probe(name, app_dir):
            continue
        if os.path.exists(os.path.join(app_dir, "agent_state.json")):
            continue   # legacy single-chat project — never recurse
        try:
            entries = sorted(os.listdir(app_dir))
        except OSError:
            # An unreadable project dir HIDES its sessions from this
            # scan — verified: pruning on that wiped their index rows.
            complete[0] = False
            continue
        if ".orch-sections" not in entries:
            continue   # unmarked wrapper dirs are never recursed
        sections = entries
        for section in sections:
            if section.startswith("."):
                continue
            sdir = os.path.join(app_dir, section)
            if not os.path.isdir(sdir):
                continue
            try:
                chats = sorted(os.listdir(sdir))
            except OSError:
                complete[0] = False
                continue
            for chat in chats:
                if chat.startswith("."):
                    continue
                probe("%s/%s/%s" % (name, section, chat),
                      os.path.join(sdir, chat))
    return out, complete[0]


def _prune_vanished(conn, root, live_projects):
    """Migrated/removed sessions must leave the index (R2: a hit that
    cannot jump is a lie). Cursor keys are project or "ev|project"."""
    live = set(live_projects)
    # Derive staleness from EVERY table carrying project rows, not just
    # messages: an artifact-only session (empty messages.jsonl) has zero
    # messages rows, and a corrupt-only one leaves just a cursor — judging
    # by messages alone kept their rows as ghost hits forever.
    seen = {p for (p,) in conn.execute("SELECT DISTINCT project FROM messages")}
    seen |= {p for (p,) in conn.execute("SELECT DISTINCT project FROM artifacts")}
    seen |= {p.split("|", 1)[-1] if p.startswith("ev|") else p
             for (p,) in conn.execute("SELECT DISTINCT project FROM cursors")}
    stale = sorted(seen - live)
    for p in stale:
        _delete_project_rows(conn, p)
        conn.execute("DELETE FROM artifacts WHERE project=?", (p,))
        if _has_artifact_fts(conn):
            conn.execute("DELETE FROM artifacts_fts WHERE project=?", (p,))
        conn.execute("DELETE FROM cursors WHERE project IN (?, ?)",
                     (p, "ev|%s" % p))
    return len(stale)


def _upsert_messages(conn, project, app_dir, rows):
    """Upsert rows (already parsed dicts) with content pairing per file."""
    by_file = {}
    for r in rows:
        by_file.setdefault(r.get("content_path") or "", []).append(r)
    has_fts = _has_fts(conn)
    for content_path, _new in by_file.items():
        # Re-pair the WHOLE file's rows (existing + new) so incremental
        # appends can never mis-align the order-based pairing.
        existing = [
            dict(zip(("turn_id", "phase", "kind", "agent", "persona",
                      "round", "content_path"), r))
            for r in conn.execute(
                "SELECT turn_id, phase, kind, agent, persona, round, "
                "content_path FROM messages WHERE project=? AND "
                "content_path=? ORDER BY rowid", (project, content_path))]
        seen = {r["turn_id"] for r in existing}
        merged = existing + [r for r in _new
                             if (r.get("turn_id") or "") not in seen]
        try:
            with open(os.path.join(app_dir, content_path),
                      encoding="utf-8", errors="replace") as fh:
                md_text = fh.read()
        except OSError:
            md_text = ""
        contents = _pair_contents(md_text, merged)
        for row, content in zip(merged, contents):
            tid = row.get("turn_id") or ""
            if not tid:
                continue
            conn.execute(
                "INSERT OR REPLACE INTO messages (project, turn_id, phase, "
                "kind, agent, persona, round, content_path, content) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (project, tid, row.get("phase"), row.get("kind"),
                 row.get("agent"), row.get("persona"),
                 int(row.get("round") or 0), content_path, content))
            if has_fts:
                conn.execute("DELETE FROM messages_fts WHERE project=? AND "
                             "turn_id=?", (project, tid))
                conn.execute(
                    "INSERT INTO messages_fts (content, project, turn_id) "
                    "VALUES (?, ?, ?)", (content, project, tid))


def _index_artifacts(conn, project, app_dir):
    """Rebuild one project's live artifact corpus from authoritative meta.

    Events prove publication happened, but cannot prove a later kill or GC.
    A bounded meta scan on each index tick is what makes exclusion immediate;
    --reindex therefore also purges rows indexed before a kill decision.
    """
    conn.execute("DELETE FROM artifacts WHERE project=?", (project,))
    if _has_artifact_fts(conn):
        conn.execute("DELETE FROM artifacts_fts WHERE project=?", (project,))
    idx = artifactslib.lineage_index(app_dir)
    n = 0
    # Preserve the 2.6 metadata-only contract for historical publication
    # events whose artifact directory predates the structured store. They have
    # no searchable body; any id with authoritative meta is handled below.
    event_path = os.path.join(app_dir, "events.jsonl")
    try:
        with open(event_path, encoding="utf-8") as fh:
            old_events = list(fh)
    except OSError:
        old_events = []
    for raw in old_events:
        try:
            evt = json.loads(raw)
        except ValueError:
            continue
        aid = str(evt.get("artifact_id") or "") if isinstance(evt, dict) else ""
        if (not isinstance(evt, dict)
                or evt.get("kind") != "artifact_published"
                or not aid or aid in idx["by_id"]):
            continue
        conn.execute(
            "INSERT OR REPLACE INTO artifacts (project, artifact_id, type, "
            "version, path, ts, status, content) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (project, aid, evt.get("type"), int(evt.get("version") or 0),
             evt.get("path"), evt.get("ts"), None, ""))
        n += 1
    for aid, meta in sorted(idx["by_id"].items()):
        if artifactslib.is_lifecycle_excluded(
                app_dir, meta, index=idx):
            continue
        body = artifactslib.read_body(app_dir, aid)
        if body is None:
            continue
        content_path = os.path.join("artifacts", aid, "body.md")
        conn.execute(
            "INSERT OR REPLACE INTO artifacts (project, artifact_id, type, "
            "version, path, ts, status, content) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (project, aid, meta.get("type"), int(meta.get("version") or 0),
             content_path, meta.get("ts"), meta.get("status"), body))
        if _has_artifact_fts(conn):
            conn.execute(
                "INSERT INTO artifacts_fts (content, project, artifact_id) "
                "VALUES (?, ?, ?)", (body, project, aid))
        n += 1
    return n


def index_incremental(root):
    """Index every project's new messages.jsonl lines. Returns a stats
    dict; 'rescans' counts cursor invalidations (file rewrites)."""
    conn, status = open_db(root)
    stats = {"status": status, "projects": 0, "new_lines": 0, "rescans": 0,
             "artifacts": 0}
    try:
        scan = _projects(root)
        if scan is None:
            # A transient listdir failure must NEVER masquerade as an
            # empty workspace — pruning on it would wipe the whole index.
            stats["scan_failed"] = True
            return stats
        found, scan_complete = scan
        if scan_complete:
            stats["pruned"] = _prune_vanished(conn, root,
                                              [p for p, _ in found])
        else:
            # Partial scan (an unreadable subtree): index what we found,
            # prune NOTHING — absence is not evidence here.
            stats["scan_partial"] = True
        for project, app_dir in found:
            stats["projects"] += 1
            path = os.path.join(app_dir, "messages.jsonl")
            offset, valid = _cursor_valid(conn, project, path)
            if not valid:
                _delete_project_rows(conn, project)
                stats["rescans"] += 1
            lines, new_offset, last_raw = _read_new_lines(path, offset)
            rows = []
            for raw in lines:
                try:
                    obj = json.loads(raw)
                except ValueError:
                    continue   # corrupt line skipped, never aborts
                if isinstance(obj, dict) and obj.get("turn_id"):
                    rows.append(obj)
            if rows:
                _upsert_messages(conn, project, app_dir, rows)
            stats["new_lines"] += len(rows)
            if last_raw:
                _store_cursor(conn, project, new_offset, last_raw)
            stats["artifacts"] += _index_artifacts(conn, project, app_dir)
        conn.commit()
    finally:
        conn.close()
    return stats


def reindex(root):
    """Drop everything and rebuild — must equal the incremental result."""
    conn, _status = open_db(root)
    try:
        conn.execute("DELETE FROM messages")
        conn.execute("DELETE FROM artifacts")
        conn.execute("DELETE FROM cursors")
        if _has_fts(conn):
            conn.execute("DELETE FROM messages_fts")
        if _has_artifact_fts(conn):
            conn.execute("DELETE FROM artifacts_fts")
        conn.commit()
    finally:
        conn.close()
    return index_incremental(root)


# ---------------------------------------------------------------------------
# Query
# ---------------------------------------------------------------------------

def _fts_query_string(text):
    """User text -> safe FTS5 MATCH string: each term quoted (implicit
    AND), so MATCH syntax (operators, quotes) can't error or inject."""
    terms = [t for t in re.split(r"\s+", text.strip()) if t]
    return " ".join('"%s"' % t.replace('"', '""') for t in terms)


def _snippet(content, text):
    low, needle = content.lower(), text.strip().lower().split()
    pos = low.find(needle[0]) if needle else -1
    if pos < 0:
        return content[:120]
    start = max(0, pos - 40)
    return ("…" if start else "") + content[start:pos + 80] + "…"


def query(root, text, limit=20):
    """Top hits for text. Returns {"status": …, "hits": [...]}; status is
    degraded when the LIKE fallback served the query."""
    conn, status = open_db(root)
    hits = []
    try:
        use_fts = status == STATUS_OK and _has_fts(conn)
        artifact_rows = []
        if use_fts:
            fts = _fts_query_string(text)
            if not fts:
                return {"status": status, "hits": []}
            sql = ("SELECT m.project, m.phase, m.round, m.agent, m.kind, "
                   "m.turn_id, m.content_path, "
                   "snippet(messages_fts, 0, '', '', '…', 12) "
                   "FROM messages_fts f JOIN messages m "
                   "ON m.project = f.project AND m.turn_id = f.turn_id "
                   "WHERE messages_fts MATCH ? ORDER BY bm25(messages_fts) "
                   "LIMIT ?")
            rows = conn.execute(sql, (fts, limit)).fetchall()
            if _has_artifact_fts(conn):
                artifact_rows = conn.execute(
                    "SELECT a.project, NULL, 0, NULL, 'artifact', "
                    "a.artifact_id, a.path, "
                    "snippet(artifacts_fts, 0, '', '', '…', 12) "
                    "FROM artifacts_fts f JOIN artifacts a "
                    "ON a.project=f.project AND "
                    "a.artifact_id=f.artifact_id "
                    "WHERE artifacts_fts MATCH ? LIMIT ?",
                    (fts, limit)).fetchall()
        else:
            esc = (text.replace("\\", "\\\\").replace("%", r"\%")
                   .replace("_", r"\_"))
            sql = ("SELECT project, phase, round, agent, kind, turn_id, "
                   "content_path, content FROM messages "
                   "WHERE content LIKE ? ESCAPE '\\' LIMIT ?")
            rows = [(p, ph, r, a, k, t, cp, _snippet(c, text))
                    for p, ph, r, a, k, t, cp, c in conn.execute(
                        sql, ("%" + esc + "%", limit)).fetchall()]
            artifact_rows = [
                (p, None, 0, None, "artifact", aid, path,
                 _snippet(content, text))
                for p, aid, path, content in conn.execute(
                    "SELECT project, artifact_id, path, content FROM artifacts "
                    "WHERE content LIKE ? ESCAPE '\\' LIMIT ?",
                    ("%" + esc + "%", limit)).fetchall()]
        for p, ph, r, a, k, t, cp, sn in (rows + artifact_rows)[:limit]:
            hits.append({"project": p, "phase": ph, "round": r, "agent": a,
                         "kind": k, "turn_id": t, "content_path": cp,
                         "snippet": sn})
    finally:
        conn.close()
    return {"status": status, "hits": hits}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--root", default=os.getcwd())
    ap.add_argument("--reindex", action="store_true")
    ap.add_argument("--query")
    ap.add_argument("--limit", type=int, default=20)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    if args.reindex:
        stats = reindex(args.root)
        print(json.dumps(stats) if args.json else
              "reindexed: %(projects)d project(s), %(new_lines)d line(s), "
              "status %(status)s" % stats)
        if stats["status"] != STATUS_OK:
            print("WARNING: %s" % stats["status"], file=sys.stderr)
        return 0
    if args.query is not None:
        stats = index_incremental(args.root)   # queries see fresh data
        res = query(args.root, args.query, limit=args.limit)
        if res["status"] != STATUS_OK:
            print("WARNING: search running degraded — %s" % res["status"],
                  file=sys.stderr)
        if args.json:
            print(json.dumps(res))
        else:
            for h in res["hits"]:
                print("%s/%s r%s %s [%s] %s" % (
                    h["project"], h["phase"], h["round"], h["agent"],
                    h["turn_id"], h["snippet"]))
        return 0
    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
