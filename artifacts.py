#!/usr/bin/env python3
"""
artifacts.py — the on-disk artifact store for the V3 artifact bus (board 4.1).

One directory per artifact under <project_dir>/artifacts/:

    artifacts/
        .publish.lock          # flock guard file (never listed)
        .tmp-<id>.<pid>/       # in-flight publish (never listed)
        <id>/
            body.md
            meta.json

Design rules (V3 sections plan §4 + repo ground rules):

  * Atomic, durable publish (§6.1): body.md and meta.json are written and
    fsynced inside a dot-prefixed tmp dir, the tmp dir's own entries are
    fsynced, THEN the dir is os.rename()d into place — a single
    same-volume transaction for directories on APFS (the same property
    migrate_layout stakes whole-session renames on) — and the artifacts/
    parent dir is fsynced last, so publish() returns — and the caller may
    emit artifact_published — only once the artifact is durably,
    completely on disk (R2). At the OS level a power cut may lose the
    LAST publish entirely but can never surface a torn one; F_FULLFSYNC
    is deliberately not used, so drive-cache reordering on a true power
    cut sits outside the guarantee.
  * One exclusive flock on artifacts/.publish.lock spans the whole
    publish (mint -> write -> rename), the guard-file sequence proven by
    acquire_app_lock. Publishes happen once per phase close, so
    contention is nil, and holding the lock across the writes buys the
    tmp-sweep invariant: a ".tmp-*" dir can only exist while its creator
    holds the lock, so any ".tmp-*" a lock HOLDER sees is a crash orphan
    and is reaped — no age heuristics.
  * Readers skip every dot-prefixed entry — one rule hides .publish.lock,
    .tmp-*, and .DS_Store alike; mint_id() can never produce an id that
    starts with a dot.
  * Ids are minted lowercase-ASCII ([a-z0-9-]) from the NFC'd title, so
    APFS case-insensitivity and NFC/NFD coalescing are structurally
    irrelevant (the 3.0 lock-encoding lesson, solved by constraining the
    alphabet instead of encoding it). Collisions bump -2, -3, ... under
    the flock; the id never derives from content.
  * meta.json carries the FULL sections-plan §4 field set from day one
    ({id, type, source{section,session,phase,turn}, version, supersedes,
    lineage, content_hash, keywords, doc_slots, status}) so 4.3's
    lineage/versioning semantics migrate nothing. Day one: version is
    always 1, supersedes always null, lineage always []. content_hash is
    the sha256 of body.md's bytes ONLY — meta cannot hash itself, and
    4.3's convergence check ("descendant hash == direct ancestor's") must
    not be defeated by meta fields that always differ (ts, version).
  * Republishing the same title mints a NEW id (slug-2) at version 1 —
    supersedes chains are 4.3's job, deliberately not inferred here.
  * The type registry (<orch_dir>/artifact_types.json) is
    seed-then-disk-wins with the sections.py error discipline: a corrupt
    or mis-shaped file is reported through on_error and the COMPLETE seed
    set is substituted — all-or-default, reported-never-silent; the
    user's file is never clobbered.

Stdlib + schemas only; no other repo module may be imported here.
"""

import copy
import datetime
import fcntl
import hashlib
import json
import os
import re
import shutil
import threading
import unicodedata

import schemas

# The closed status vocabulary. "superseded" and "converged" are written
# only by 4.3's lineage machinery; a publisher may set only the first two.
STATUS = ("draft", "published", "superseded", "converged")
_PUBLISHER_STATUS = ("draft", "published")

# The 4.2 publication fence. This module may import ONLY schemas, so
# equality with sections.contract_fence("artifact") is pinned by a test
# rather than an import — the snippet and the extractor must never drift.
FENCE_TAG = "artifact-json"
BLOCK_REQUIRED = ("type", "title")

REGISTRY_BASENAME = "artifact_types.json"

# Seed registry: "required" lists the keys a publish (or a 4.2 artifact-json
# block) must carry — "body" is satisfied by a non-blank body.md. The five
# board types each pin the per-type payload key 4.3+ consumes (notably
# reconcile.parents, the branch-head list); finding_report and spec_bundle
# are included because the SHIPPED section manifests already declare them
# (a shipped section prompting for a type the shipped registry rejects
# would be broken by design — pinned by a consistency test).
SEED_TYPES = {
    "idea": {"required": ["title", "body"],
             "default_status": "published"},
    "research_brief": {"required": ["title", "body", "sources"],
                       "default_status": "published"},
    "opportunity_signal": {"required": ["title", "body", "evidence"],
                           "default_status": "published"},
    "gap": {"required": ["title", "body", "impact"],
            "default_status": "published"},
    "reconcile": {"required": ["title", "body", "parents"],
                  "default_status": "published"},
    "finding_report": {"required": ["title", "body"],
                       "default_status": "published"},
    "spec_bundle": {"required": ["title", "body"],
                    "default_status": "published"},
}


# ---------------------------------------------------------------------------
# Paths — no other module builds these by hand.
# ---------------------------------------------------------------------------
def artifacts_root(project_dir):
    return os.path.join(project_dir, "artifacts")


def artifact_dir(project_dir, artifact_id):
    return os.path.join(artifacts_root(project_dir), artifact_id)


def _registry_path(orch_dir):
    return os.path.join(orch_dir, REGISTRY_BASENAME)


# ---------------------------------------------------------------------------
# Type registry: seed-then-disk-wins, all-or-default, reported-never-silent.
# ---------------------------------------------------------------------------
def _seed_registry_doc():
    return {"schema_version": schemas.SCHEMA_VERSION,
            "types": copy.deepcopy(SEED_TYPES)}


def ensure_seeded_artifact_types(orch_dir):
    """Materialize the seed registry on first use, never clobbering an
    existing file — even an invalid one (invalid gets a banner from
    load_registry, not a clobber). The seed is written via a PER-WRITER
    tmp name + os.replace, so concurrent first loads — processes or
    threads — can never tear the file."""
    path = _registry_path(orch_dir)
    try:
        if os.path.exists(path):
            return
        os.makedirs(orch_dir, exist_ok=True)
        tmp = "%s.%d.%x.tmp" % (path, os.getpid(), threading.get_ident())
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(_seed_registry_doc(), fh, indent=2, sort_keys=True)
        os.replace(tmp, path)
    except OSError:
        pass


def _registry_problem(doc):
    """First structural problem with a loaded registry doc, or None."""
    if not isinstance(doc, dict):
        return "top level is not an object"
    types = doc.get("types")
    if not isinstance(types, dict) or not types:
        return "'types' is not a non-empty object"
    for name, entry in types.items():
        if not isinstance(entry, dict):
            return "type %r is not an object" % (name,)
        required = entry.get("required", [])
        if (not isinstance(required, list)
                or any(not isinstance(f, str) for f in required)):
            return "type %r 'required' is not a list of strings" % (name,)
        status = entry.get("default_status", "published")
        if status not in _PUBLISHER_STATUS:
            return ("type %r default_status %r is not one of %s"
                    % (name, status, "/".join(_PUBLISHER_STATUS)))
    return None


def load_registry(orch_dir, on_error=None):
    """Load <orch_dir>/artifact_types.json (seeding it first if absent).

    A user-edited file on disk always wins. A corrupt or mis-shaped file
    is reported via on_error and the complete seed set is returned —
    all-or-default, never a silent fallback, never a half-applied file.
    Unknown keys inside a type entry are tolerated (4.3 adds its own)."""
    if on_error is None:
        on_error = lambda _msg: None
    ensure_seeded_artifact_types(orch_dir)
    path = _registry_path(orch_dir)
    try:
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
    except (OSError, ValueError) as exc:
        on_error("artifact_types.json unreadable (%s) — using built-in "
                 "seed types" % (exc,))
        return _seed_registry_doc()
    problem = _registry_problem(doc)
    if problem is not None:
        on_error("artifact_types.json invalid (%s) — using built-in "
                 "seed types" % (problem,))
        return _seed_registry_doc()
    return doc


# ---------------------------------------------------------------------------
# Id minting.
# ---------------------------------------------------------------------------
_SLUG_STRIP = re.compile(r"[^a-z0-9]+")


def _slug(text):
    s = unicodedata.normalize("NFC", text or "").lower()
    s = _SLUG_STRIP.sub("-", s).strip("-")
    return s[:60].rstrip("-")


def mint_id(project_dir, type_name, title):
    """Mint a unique artifact id: lowercase-ASCII slug of the title, with
    -2/-3/... bumps on collision (a punctuation-only or non-ASCII-only
    title falls back to the type name). Uniqueness holds only under the
    publish flock — two unlocked callers could mint the same id, which is
    why publish() is the only engine caller."""
    slug = _slug(title) or _slug(str(type_name)) or "artifact"
    candidate = slug
    n = 2
    while os.path.isdir(artifact_dir(project_dir, candidate)):
        candidate = "%s-%d" % (slug, n)
        n += 1
    return candidate


_VALID_ID = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")


def _valid_artifact_id(artifact_id):
    """Exactly the shape mint_id produces. Rejecting '.', '..', '/',
    absolute paths, and empty ids keeps caller-supplied ids (events, GUI
    routing, artifact-json blocks) from escaping the store."""
    return (isinstance(artifact_id, str)
            and _VALID_ID.match(artifact_id) is not None)


# ---------------------------------------------------------------------------
# Publish.
# ---------------------------------------------------------------------------
def _fsync_write(path, data):
    """Write bytes and fsync the fd — content must be durable BEFORE the
    rename makes it visible, so a surfaced artifact can never be torn."""
    with open(path, "wb") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())


def _sweep_orphans(root):
    """Reap crash-orphaned in-flight dirs. Only called while HOLDING the
    publish flock: a live publisher's .tmp-* cannot exist outside the
    lock, so anything matching here is provably dead."""
    try:
        names = os.listdir(root)
    except OSError:
        return
    for name in names:
        if name.startswith(".tmp-"):
            shutil.rmtree(os.path.join(root, name), ignore_errors=True)


def _string_list(value):
    if isinstance(value, list) and all(isinstance(x, str) for x in value):
        return list(value)
    return None


def _now_iso():
    return (datetime.datetime.now(datetime.timezone.utc)
            .astimezone().isoformat(timespec="seconds"))


def publish(project_dir, body_text, meta, registry, on_error=None):
    """Atomically publish one artifact; return its minted id, or None with
    the reason reported through on_error.

    Nothing touches the disk unless the publish is fully valid (a partial
    artifact must never appear); on success the artifact directory is
    durably in place before this returns, so a caller may emit
    artifact_published on the returned id without lying (§13.2/R2).
    Engine-owned meta fields (id, version, supersedes, lineage,
    content_hash, ts) are never taken from the caller — smuggled values
    are preserved under meta["fields"] rather than silently discarded."""
    if on_error is None:
        on_error = lambda _msg: None
    if not isinstance(meta, dict):
        on_error("publish rejected: meta is not an object")
        return None
    types = registry.get("types") if isinstance(registry, dict) else None
    if not isinstance(types, dict):
        on_error("publish rejected: registry has no usable 'types' table")
        return None
    type_name = meta.get("type")
    if not isinstance(type_name, str) or type_name not in types:
        on_error("publish rejected: unknown artifact type %r" % (type_name,))
        return None
    entry = types[type_name]
    body_text = "" if body_text is None else str(body_text)

    # Required-field view: per-type payload keys may live at meta top level
    # or under meta["fields"]; "body" is satisfied by a non-blank body.md.
    fields = meta.get("fields")
    view = dict(fields) if isinstance(fields, dict) else {}
    view.update({k: v for k, v in meta.items() if k != "fields"})
    view["body"] = body_text if body_text.strip() else None
    ok, missing = schemas.validate_required_fields(
        view, entry.get("required", []))
    if not ok:
        on_error("publish rejected: type %r missing required field(s): %s"
                 % (type_name, ", ".join(missing)))
        return None

    title = meta.get("title")
    if not isinstance(title, str):
        title = "" if title is None else str(title)

    status = meta.get("status")
    default_status = entry.get("default_status", "published")
    if status is None:
        status = default_status
    elif status not in _PUBLISHER_STATUS:
        on_error("publish: status %r is not one of %s — using %r"
                 % (status, "/".join(_PUBLISHER_STATUS), default_status))
        status = default_status

    # §6.2 never silently discard: caller keys the schema doesn't own land
    # under "fields" verbatim (including smuggled engine-owned ones), and
    # a schema-owned key with the WRONG SHAPE is reported and preserved
    # there too — the invalid-status discipline, never a silent zeroing.
    extras = dict(fields) if isinstance(fields, dict) else {}
    if fields is not None and not isinstance(fields, dict):
        on_error("publish: 'fields' is not an object — preserving the "
                 "original under fields['fields']")
        extras["fields"] = fields

    # Provenance: all four keys always present ("" when unknown) — never
    # blank-by-omission.
    src = meta.get("source")
    if src is not None and not isinstance(src, dict):
        on_error("publish: 'source' is not an object — provenance left "
                 "blank, original preserved under fields['source']")
        extras["source"] = src
        src = None
    src = src if isinstance(src, dict) else {}
    source = {k: (src.get(k) if isinstance(src.get(k), str) else "")
              for k in ("section", "session", "phase", "turn")}

    def _take_string_list(key):
        raw = meta.get(key)
        value = _string_list(raw)
        if value is None and raw is not None:
            on_error("publish: %r is not a list of strings — preserving "
                     "the original under fields" % (key,))
            extras[key] = raw
        return value or []

    keywords = _take_string_list("keywords")
    doc_slots = _take_string_list("doc_slots")

    consumed = {"type", "title", "source", "status", "keywords",
                "doc_slots", "fields"}
    for key, value in meta.items():
        if key not in consumed:
            extras[key] = value

    try:
        body_bytes = body_text.encode("utf-8")
    except UnicodeEncodeError as exc:
        on_error("publish rejected: body is not encodable UTF-8 (%s)"
                 % (exc,))
        return None
    root = artifacts_root(project_dir)
    try:
        os.makedirs(root, exist_ok=True)
        guard_fd = os.open(os.path.join(root, ".publish.lock"),
                           os.O_CREAT | os.O_RDWR, 0o644)
    except OSError as exc:
        on_error("publish failed: cannot open %s (%s)" % (root, exc))
        return None
    try:
        fcntl.flock(guard_fd, fcntl.LOCK_EX)
    except OSError as exc:
        # Acquisition failure (ENOLCK/EOPNOTSUPP on network volumes) must
        # report, not raise — and the outer try/finally that would close
        # the fd has not been entered yet.
        os.close(guard_fd)
        on_error("publish failed: cannot lock %s (%s)" % (root, exc))
        return None
    try:
        _sweep_orphans(root)
        aid = mint_id(project_dir, type_name, title)
        tmp = os.path.join(root, ".tmp-%s.%d" % (aid, os.getpid()))
        try:
            os.mkdir(tmp)
            _fsync_write(os.path.join(tmp, "body.md"), body_bytes)
            record = {
                "schema_version": schemas.SCHEMA_VERSION,
                "id": aid,
                "type": type_name,
                "title": title,
                "source": source,
                "version": 1,
                "supersedes": None,
                "lineage": [],
                "content_hash": hashlib.sha256(body_bytes).hexdigest(),
                "keywords": keywords,
                "doc_slots": doc_slots,
                "status": status,
                "ts": _now_iso(),
                "fields": extras,
            }
            _fsync_write(
                os.path.join(tmp, "meta.json"),
                json.dumps(record, indent=2, sort_keys=True).encode("utf-8"))
            # fsync the tmp DIR itself: the dirents naming body.md and
            # meta.json live in ITS data, and POSIX does not persist them
            # as a side effect of fsyncing the files — without this a
            # power cut could surface artifacts/<id>/ missing a child,
            # the torn state the module rules out.
            tdfd = os.open(tmp, os.O_RDONLY)
            try:
                os.fsync(tdfd)
            finally:
                os.close(tdfd)
            # os.rename (not os.replace): the destination must not exist —
            # mint under the lock guarantees it, and a violated invariant
            # should fail loudly rather than merge directories.
            os.rename(tmp, artifact_dir(project_dir, aid))
        except Exception as exc:
            shutil.rmtree(tmp, ignore_errors=True)
            on_error("publish failed for type %r (%s) — nothing was stored"
                     % (type_name, exc))
            return None
        # The rename made the artifact visible: from here on, failure may
        # only be reported as success-with-warning — "nothing was stored"
        # about an artifact readers can already see would make the caller
        # skip artifact_published and a retry mint a -2 duplicate
        # (R2, §12.4). A failed parent-dir fsync merely widens the
        # accepted power-loss residual to this last publish's dirent.
        try:
            dfd = os.open(root, os.O_RDONLY)
            try:
                os.fsync(dfd)
            finally:
                os.close(dfd)
        except OSError as exc:
            on_error("publish %r: artifacts dir fsync failed (%s) — the "
                     "artifact is live but may not survive a power cut"
                     % (aid, exc))
    finally:
        try:
            fcntl.flock(guard_fd, fcntl.LOCK_UN)
        finally:
            os.close(guard_fd)
    return aid


def publish_from_output(project_dir, final_output, source, registry,
                        on_error=None, dedupe_against=None):
    """Extract every ```artifact-json``` block from a phase's Final
    Output and publish each — the V3 4.2 publication seam. Returns the
    list of published ids (possibly empty; no blocks publishes nothing).

    Scans final_output ONLY, never the transcript: artifacts carry no
    merge key, so a transcript scan would republish every superseded
    draft from earlier rounds. Every malformed block (bad JSON, missing
    field, unknown type, non-string body) is reported through on_error
    and skipped — siblings still publish, and nothing ever raises
    (§13.3). Engine provenance in ``source`` overrides anything the
    block claims.

    ``dedupe_against``: a set of (type, title, content_hash) triples for
    artifacts already published by this same phase close — a block
    matching one is reported and skipped, so a crash-resume re-close
    (durable publish landed, but the phase's skip-on-resume state did
    not) cannot duplicate every artifact under a -2 id. A CHANGED body
    hashes differently and still publishes (genuine republish
    semantics are 4.3's supersedes territory, untouched here)."""
    if on_error is None:
        on_error = lambda _msg: None
    blocks = schemas.extract_structured_blocks(
        final_output or "", FENCE_TAG,
        required_fields=list(BLOCK_REQUIRED), on_error=on_error)
    src = dict(source) if isinstance(source, dict) else {}
    published = []
    for block in blocks:
        meta = dict(block)
        body = meta.pop("body", None)
        if body is not None and not isinstance(body, str):
            on_error("artifact-json block %r: 'body' must be a single "
                     "string — skipped" % (meta.get("title"),))
            continue
        if dedupe_against:
            title = meta.get("title")
            title = title if isinstance(title, str) else str(title)
            try:
                bhash = hashlib.sha256(
                    (body or "").encode("utf-8")).hexdigest()
            except UnicodeEncodeError:
                bhash = None  # publish() will reject the block anyway
            if bhash and (meta.get("type"), title, bhash) in dedupe_against:
                on_error("artifact %r already published by this phase "
                         "close — skipped (crash-resume dedupe)" % (title,))
                continue
        meta["source"] = src
        aid = publish(project_dir, body, meta, registry, on_error=on_error)
        if aid is not None:
            published.append(aid)
    return published


# ---------------------------------------------------------------------------
# Read APIs — tolerant of corruption, reported never raised.
# ---------------------------------------------------------------------------
def load_meta(project_dir, artifact_id, on_error=None):
    """The parsed meta.json for one artifact, or None (reported)."""
    if on_error is None:
        on_error = lambda _msg: None
    if not _valid_artifact_id(artifact_id):
        on_error("artifact %r has an invalid id" % (artifact_id,))
        return None
    path = os.path.join(artifact_dir(project_dir, artifact_id), "meta.json")
    try:
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
    except (OSError, ValueError) as exc:
        on_error("artifact %r meta unreadable: %s" % (artifact_id, exc))
        return None
    if not isinstance(doc, dict):
        on_error("artifact %r meta is not an object" % (artifact_id,))
        return None
    return doc


def read_body(project_dir, artifact_id, on_error=None):
    """The body.md text for one artifact, or None (reported). Returned
    text matches the published bytes exactly — newline="" disables
    universal-newline translation, which would otherwise mangle \\r\\n
    bodies and break re-hash-equals-content_hash round-trips."""
    if on_error is None:
        on_error = lambda _msg: None
    if not _valid_artifact_id(artifact_id):
        on_error("artifact %r has an invalid id" % (artifact_id,))
        return None
    path = os.path.join(artifact_dir(project_dir, artifact_id), "body.md")
    try:
        with open(path, encoding="utf-8", newline="") as fh:
            return fh.read()
    except OSError as exc:
        on_error("artifact %r body unreadable: %s" % (artifact_id, exc))
        return None


def list_artifacts(project_dir, type=None, status=None, on_error=None):
    """Every readable artifact meta (sorted by id), optionally filtered.

    Dot-prefixed entries (in-flight publishes, the lock file, .DS_Store)
    are invisible. A corrupt meta.json is reported via on_error and that
    artifact skipped — one bad artifact never blinds the store. The
    directory name is authoritative for the id: a hand-edited meta id is
    reported and overridden."""
    if on_error is None:
        on_error = lambda _msg: None
    root = artifacts_root(project_dir)
    try:
        names = sorted(os.listdir(root))
    except OSError:
        return []
    out = []
    for name in names:
        if name.startswith("."):
            continue
        if not os.path.isdir(os.path.join(root, name)):
            continue
        meta = load_meta(project_dir, name, on_error=on_error)
        if meta is None:
            continue
        if meta.get("id") != name:
            on_error("artifact %r meta id %r does not match its directory "
                     "— the directory name wins" % (name, meta.get("id")))
            meta = dict(meta)
            meta["id"] = name
        if type is not None and meta.get("type") != type:
            continue
        if status is not None and meta.get("status") != status:
            continue
        out.append(meta)
    return out
