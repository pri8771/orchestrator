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

import collections
import copy
import datetime
import fcntl
import hashlib
import json
import os
import re
import reprlib
import shutil
import threading
import unicodedata

import schemas
import compliance as compliancelib

# The closed status vocabulary. "superseded" and "converged" are written
# only by 4.3's lineage machinery. "published" is RETIRED (V3 4.8): it split
# into the honest pair {pending_review, final} the bus admission gate reads.
STATUS = ("draft", "pending_review", "final", "superseded", "converged",
          "quarantined", "killed", "tombstoned")
# The ONLY status a publisher (an agent's artifact-json block) may request is
# an explicit "draft" hold; every other status is assigned by the type's
# finalization policy (4.8), never by the publisher.
_PUBLISHER_STATUS = ("draft",)

# 4.8 per-type finalization policy (in the type registry). auto_final_on_
# publish finalizes terminal engine evidence immediately; consensus finalizes
# only from a CONSENSUS: YES phase; review/human remain pending explicitly.
FINALIZATION_POLICIES = ("auto_final_on_publish", "auto_final_on_consensus",
                         "requires_review_gate", "requires_human")
# The SAFEST policy — nothing auto-flows. Missing/unknown finalization falls
# here (never silently to auto): un-reviewed artifacts must not propagate.
_SAFE_FINALIZATION = "requires_human"

# The 4.2 publication fence. This module may import ONLY schemas, so
# equality with sections.contract_fence("artifact") is pinned by a test
# rather than an import — the snippet and the extractor must never drift.
FENCE_TAG = "artifact-json"
BLOCK_REQUIRED = ("type", "title")

# 4.3 lineage: the depth cap when a type entry carries no "max_depth".
# The cap stops runaway derivation loops AT THE BUS, so no consumer
# (manual today, the M7 Conductor later) can bypass it.
DEFAULT_MAX_DEPTH = 4

REGISTRY_BASENAME = "artifact_types.json"
TOMBSTONE_BODY = ("# Tombstoned artifact\n\n"
                  "This body was reclaimed by lifecycle GC. Provenance and "
                  "lineage remain in meta.json.\n")


class ArtifactReadError(Exception):
    """Typed artifact-resolution failure carrying any surviving metadata."""

    code = "artifact_unreadable"

    def __init__(self, message, meta=None):
        super().__init__(message)
        self.meta = dict(meta) if isinstance(meta, dict) else None


class TombstonedArtifactError(ArtifactReadError):
    code = "tombstoned"

# Seed registry: "required" lists the keys a publish (or a 4.2 artifact-json
# block) must carry — "body" is satisfied by a non-blank body.md. The five
# board types each pin the per-type payload key 4.3+ consumes (notably
# reconcile.parents, the branch-head list). Every type declared by a shipped
# section is included because a section prompting for a type the shipped
# registry rejects would be broken by design — pinned by a consistency test.
SEED_TYPES = {
    "design_brief": {"required": ["title", "body"],
                     "finalization": "auto_final_on_consensus"},
    "extraction_plan": {"required": ["title", "body"],
                        "finalization": "requires_human"},
    "failure": {"required": ["title", "body", "error_class", "message",
                             "last_checkpoint", "cost_spent", "events_cursor",
                             "source_session"],
                "finalization": "auto_final_on_publish"},
    "idea": {"required": ["title", "body"],
             "finalization": "auto_final_on_consensus"},
    "research_brief": {"required": ["title", "body", "sources"],
                       "finalization": "auto_final_on_consensus"},
    "opportunity_signal": {"required": ["title", "body", "evidence"],
                           "finalization": "auto_final_on_consensus"},
    "gap": {"required": ["title", "body", "impact"],
            "finalization": "auto_final_on_consensus"},
    "reconcile": {"required": ["title", "body", "parents"],
                  "finalization": "auto_final_on_consensus"},
    "finding_report": {"required": ["title", "body"],
                       "finalization": "requires_review_gate"},
    "compliance_report": {"required": ["title", "body", "findings"],
                          "finalization": "auto_final_on_consensus"},
    "knowledge_hint": {"required": ["title", "body", "evidence"],
                       "finalization": "auto_final_on_consensus"},
    "launch_plan": {"required": ["title", "body"],
                    "finalization": "requires_human"},
    "legal_flag": {"required": ["title", "body", "blocking_concern"],
                   "finalization": "requires_review_gate"},
    "positioning_brief": {"required": ["title", "body"],
                          "finalization": "auto_final_on_consensus"},
    "plan": {"required": ["title", "body"],
             "finalization": "auto_final_on_consensus"},
    "postmortem": {"required": ["title", "body"],
                   "finalization": "requires_human"},
    "release_checklist": {"required": ["title", "body"],
                          "finalization": "requires_human"},
    "runbook": {"required": ["title", "body"],
                "finalization": "requires_human"},
    "spec_bundle": {"required": ["title", "body"],
                    "finalization": "requires_human"},
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
        # finalization is validated at READ time (_finalization_policy) with a
        # per-type SAFE fallback, NOT here — an unknown value must not drop the
        # user's whole edited registry to seeds (all-or-default), and a mistyped
        # policy degrades to the SAFEST behavior, unlike a mistyped max_depth.
        cap = entry.get("max_depth", 0)
        if not isinstance(cap, int) or isinstance(cap, bool) or cap < 0:
            # A mistyped cap silently ignored is a DISABLED loop guard —
            # validate-when-present, all-or-default otherwise.
            return ("type %r max_depth %r is not a non-negative int"
                    % (name, cap))
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
# 4.8 finalization policy (read-time, per-type, SAFE fallback).
# ---------------------------------------------------------------------------
def _finalization_policy(entry, on_error):
    """A type entry's finalization policy. Absent -> the SAFEST policy,
    silently (a structural omission in a legacy/user file). Present-but-
    unknown -> the SAFEST policy WITH a banner (a typo the user must see;
    fall back SAFE and LOUD, never to auto)."""
    pol = entry.get("finalization") if isinstance(entry, dict) else None
    if pol in FINALIZATION_POLICIES:
        return pol
    if pol is not None:
        on_error("finalization policy %r is not one of %s — using the safest "
                 "(%s)" % (pol, "/".join(FINALIZATION_POLICIES),
                           _SAFE_FINALIZATION))
    return _SAFE_FINALIZATION


def _initial_status(policy, consensus):
    """The status a fresh publish gets: auto_final_on_consensus is 'final'
    ONLY on a consensus phase; auto_final_on_publish is always final; every
    review policy publishes 'pending_review' until an explicit finalize()."""
    if policy == "auto_final_on_publish":
        return "final"
    if policy == "auto_final_on_consensus":
        return "final" if consensus else "pending_review"
    return "pending_review"


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


def _lineage_lock_path(project_dir, root_id):
    # Root ids match _VALID_ID ([a-z0-9-], <=60 chars), so no quoting is
    # needed; the dot prefix hides the file from every reader and from
    # _sweep_orphans (which reaps only ".tmp-").
    return os.path.join(artifacts_root(project_dir),
                        ".lineage-%s.lock" % root_id)


# One bad meta.json must never blind the store or crash a reader (the
# house rule: reported via on_error, never raised). These helpers coerce
# the lineage fields off disk to their intended types, so a hand-edited
# or upgrade-era corrupt value degrades to documented legacy behavior
# instead of poisoning sort keys, lock names, or the derivation math.
def _int_field(value, fallback):
    return value if isinstance(value, int) and not isinstance(value, bool) \
        else fallback


def _lineage_list(meta, on_error):
    """meta.lineage as a list of str ancestor ids; a non-list is reported
    and treated as legacy-empty, non-string members are dropped."""
    lin = meta.get("lineage")
    if isinstance(lin, list):
        return [x for x in lin if isinstance(x, str)]
    if lin is not None:
        on_error("artifact %r: lineage is not a list — treating as legacy "
                 "(empty)" % (meta.get("id"),))
    return []


def _meta_root(meta):
    """The lineage root id of any member meta (lineage[0], else self)."""
    lin = meta.get("lineage")
    if isinstance(lin, list) and lin and isinstance(lin[0], str):
        return lin[0]
    return meta.get("id")


# Bounded rendering of untrusted values in refusal/error messages: an
# agent-emitted parents bomb (200k ids) or a megabyte id must not inflate
# the WARN/event stream, which the call site caps at [:300] downstream.
_CLIP = reprlib.Repr()
_CLIP.maxstring = 200
_CLIP.maxlist = 10
_CLIP.maxother = 200


def _short(value):
    return _CLIP.repr(value)


def _derivation_plan(project_dir, pid, body_bytes, type_name, title,
                     entry, source_section, on_error):
    """The lineage fields for a supersedes=<pid> publish, computed UNDER
    the lineage lock (the caller holds it). None = refused, reported."""
    parent = load_meta(project_dir, pid, on_error=on_error)
    if parent is None:
        on_error("publish rejected: cannot derive — parent %r is missing "
                 "or unreadable" % (pid,))
        return None
    if parent.get("status") == "converged":
        on_error("publish rejected: cannot derive from CONVERGED %r — a "
                 "convergence tombstone ends its loop" % (pid,))
        return None
    plineage = _lineage_list(parent, on_error)
    pdepth = _int_field(parent.get("depth"), len(plineage))
    cap = entry.get("max_depth", DEFAULT_MAX_DEPTH)
    if type_name != "reconcile" and pdepth + 1 > cap:
        on_error("publish REFUSED: deriving from %r would exceed the "
                 "lineage depth cap (%d) — the chain is done deriving"
                 % (pid, cap))
        return None
    # One scan: existing children decide linear-vs-branch, and catch a
    # crash-retried duplicate derivation (§12.4 — refuse, never return
    # an id this call didn't create).
    bhash = hashlib.sha256(body_bytes).hexdigest()
    children = []
    for m in list_artifacts(project_dir):
        if m.get("supersedes") == pid:
            children.append(m)
            if (m.get("type"), m.get("title"),
                    m.get("content_hash")) == (type_name, title, bhash):
                on_error("publish rejected: duplicate derivation — this "
                         "content is already published as %r"
                         % (m.get("id"),))
                return None
    pver = _int_field(parent.get("version"), 1)
    phops = _int_field(parent.get("hop_count"), 0)
    psection = ((parent.get("source") or {}).get("section")
                if isinstance(parent.get("source"), dict) else "") or ""
    # A Fixer can fail inside the same Execution section that spawned it.
    # Failure-of-failure is still another autonomous repair hop, so it must
    # consume budget even when the section label is unchanged.
    hop = 1 if type_name == "failure" else (
        0 if (source_section and source_section == psection) else 1)
    plan = {
        "supersedes": pid,
        "lineage": list(plineage) + [pid],
        "version": pver + 1,
        # First child stores "" (labeled a only when siblings exist);
        # the k-th collision writer stores chr(ord('a')+k) — letters
        # never shuffle after the fact.
        "branch": "" if not children else chr(ord("a") + len(children)),
        "depth": pdepth + 1,
        "hop_count": phops + hop,
        "forced_status": None,
        "sensitivity": ("private" if parent.get("sensitivity") == "private"
                        else "normal"),
    }
    if bhash == parent.get("content_hash"):
        on_error("publish note: content is byte-identical to %r — "
                 "auto-marking status 'converged' (never routable, "
                 "never derivable)" % (pid,))
        plan["forced_status"] = "converged"
    return plan


def is_routable(meta):
    """False for convergence tombstones — THE shared predicate (4.5
    route_push, 7.2 pre-route guards, 4.8 admission wraps it). Pure,
    meta-only, never raises."""
    return isinstance(meta, dict) and meta.get("status") != "converged"


def publish(project_dir, body_text, meta, registry, on_error=None,
            supersedes=None, consensus=False, gate=None,
            reconcile_request=False, sensitivity=None):
    """Atomically publish one artifact; return its minted id, or None with
    the reason reported through on_error.

    ``gate`` (V3 4.12): an engine-owned pre-push gate outcome. When it carries
    a truthy ``failures`` list the artifact is written with status
    'quarantined' — it lands on disk, fully inspectable, but is_admissible
    rejects it so it never routes (4.5) or is retrieved (4.7); quarantine is
    evidence, never a silent drop. Recorded verbatim under meta['gate']. A
    publisher can never request quarantine — it is set ONLY from this param
    (a smuggled meta['gate'] falls through to fields, non-authoritative).

    ``reconcile_request`` (V3 5.5) is likewise engine-owned: it permits the
    documentation sync service to publish a human-override request with empty
    parents without weakening the ordinary reconcile type's >=2-head merge
    contract. Merely smuggling request_kind in meta never enables it.

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
    requested_sensitivity = meta.get("sensitivity", "normal")
    if requested_sensitivity not in ("normal", "private"):
        on_error("publish rejected: sensitivity must be 'normal' or 'private'")
        return None
    if sensitivity is not None and sensitivity not in ("normal", "private"):
        on_error("publish rejected: engine sensitivity must be 'normal' or 'private'")
        return None

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
    if type_name == "plan":
        errors = []
        blocks = schemas.extract_structured_blocks(
            body_text, "plan-json", required_fields=["steps"],
            on_error=errors.append)
        if errors or len(blocks) != 1:
            for error in errors:
                on_error("publish rejected: plan %s" % error)
            if len(blocks) != 1:
                on_error("publish rejected: plan body must contain exactly "
                         "one fenced ```plan-json``` block")
            return None
        steps = blocks[0].get("steps")
        if not isinstance(steps, list) or not steps:
            on_error("publish rejected: plan-json.steps must be a non-empty "
                     "list")
            return None
        seen_steps = set()
        for index, step in enumerate(steps):
            valid, absent = schemas.validate_required_fields(
                step, ["id", "title"])
            if not valid:
                on_error("publish rejected: plan step %d missing required "
                         "field(s): %s" % (index, ", ".join(absent)))
                return None
            step_id = step.get("id")
            normalized_step_id = step_id.strip() if isinstance(
                step_id, str) else ""
            if not normalized_step_id or normalized_step_id in seen_steps:
                on_error("publish rejected: plan step %d has an invalid or "
                         "duplicate id" % index)
                return None
            seen_steps.add(normalized_step_id)
            if not isinstance(step.get("title"), str) \
                    or not step["title"].strip() \
                    or not isinstance(step.get("target_section"), str) \
                    or not step["target_section"].strip() \
                    or not isinstance(step.get("expected_artifact_type"), str) \
                    or not step["expected_artifact_type"].strip():
                on_error("publish rejected: plan step %d must carry non-empty "
                         "title, target_section, and expected_artifact_type"
                         % index)
                return None
    if type_name == "failure":
        classes = {"agent_exhausted", "config_error",
                   "release_gate_budget_exhausted", "crash",
                   "budget_exhausted", "stalled"}
        checkpoint = view.get("last_checkpoint")
        cost = view.get("cost_spent")
        cursor = view.get("events_cursor")
        if view.get("error_class") not in classes:
            on_error("publish rejected: failure.error_class is not recognized")
            return None
        if not isinstance(view.get("message"), str) \
                or not view["message"].strip():
            on_error("publish rejected: failure.message must be non-empty")
            return None
        if not isinstance(checkpoint, dict) \
                or not isinstance(checkpoint.get("phase"), str) \
                or not isinstance(checkpoint.get("round"), int) \
                or isinstance(checkpoint.get("round"), bool) \
                or checkpoint.get("round") < 0 \
                or not isinstance(checkpoint.get("completed_phases_count"), int) \
                or isinstance(checkpoint.get("completed_phases_count"), bool) \
                or checkpoint.get("completed_phases_count") < 0:
            on_error("publish rejected: failure.last_checkpoint is malformed")
            return None
        if not isinstance(cost, dict) \
                or not isinstance(cost.get("cost_micro_usd"), int) \
                or isinstance(cost.get("cost_micro_usd"), bool) \
                or cost.get("cost_micro_usd") < 0:
            on_error("publish rejected: failure.cost_spent is malformed")
            return None
        if not isinstance(cursor, int) or isinstance(cursor, bool) or cursor < 0:
            on_error("publish rejected: failure.events_cursor is malformed")
            return None
        if not isinstance(view.get("source_session"), str) \
                or not view["source_session"].strip():
            on_error("publish rejected: failure.source_session must be non-empty")
            return None
    if type_name == "compliance_report":
        compliance_errors = compliancelib.validate_findings(
            view.get("findings"))
        if compliance_errors:
            for error in compliance_errors:
                on_error("publish rejected: compliance_report %s" % error)
            return None

    title = meta.get("title")
    if not isinstance(title, str):
        title = "" if title is None else str(title)

    # 4.8: status is assigned by the type's finalization POLICY (final vs
    # pending_review), not by the publisher — the ONLY thing a block may
    # request is an explicit "draft" hold.
    policy = _finalization_policy(entry, on_error)
    requested = meta.get("status")
    if requested == "draft":
        status = "draft"
    else:
        if requested is not None:
            on_error("publish: status %r is not settable by a publisher — "
                     "assigning by the %r finalization policy"
                     % (requested, policy))
        status = _initial_status(policy, bool(consensus))

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
    # V3 9.4: persona/preset provenance is optional. Preset-less turns stay
    # byte-identical; a bound turn records enough to reproduce its sampling.
    for key in ("persona_id", "preset_id", "preset_params_hash"):
        if isinstance(src.get(key), str) and src[key]:
            source[key] = src[key]
    if ("preset_params_hash" not in source
            and isinstance(src.get("preset_params"), dict)):
        source["preset_params_hash"] = hashlib.sha256(json.dumps(
            src["preset_params"], sort_keys=True, separators=(",", ":")
        ).encode("utf-8")).hexdigest()

    raw_plan_ref = meta.get("plan_ref")
    if raw_plan_ref is None and isinstance(src.get("plan_ref"), dict):
        raw_plan_ref = src.get("plan_ref")
    plan_ref = None
    if raw_plan_ref is not None:
        if isinstance(raw_plan_ref, dict) \
                and isinstance(raw_plan_ref.get("plan_id"), str) \
                and isinstance(raw_plan_ref.get("plan_version"), int) \
                and not isinstance(raw_plan_ref.get("plan_version"), bool) \
                and raw_plan_ref.get("plan_version") > 0 \
                and isinstance(raw_plan_ref.get("step_id"), str):
            plan_ref = {key: raw_plan_ref[key] for key in
                        ("plan_id", "plan_version", "step_id")}
        else:
            on_error("publish: plan_ref is malformed — preserving it under "
                     "fields, not claiming intent provenance")
            extras["plan_ref"] = raw_plan_ref

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
                "doc_slots", "fields", "plan_ref", "sensitivity"}
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
    except OSError as exc:
        on_error("publish failed: cannot open %s (%s)" % (root, exc))
        return None

    # 4.3 lineage lock — STRICT hierarchy: lineage lock OUTSIDE,
    # .publish.lock INSIDE, always; no path takes a lineage lock while
    # holding the publish lock, and no publish holds two lineage locks —
    # deadlock impossible. Every write that creates an edge into a
    # lineage holds that lineage's lock, so the derivation plan computed
    # under it can never go stale.
    lin_fd = None
    lroot = None
    parents = None
    # V3 5.5 uses the existing reconcile bus type for a *request* to
    # reconcile a human-overridden rendered document.  It is explicitly not a
    # 4.3 lineage merge: empty parents plus this engine-recognized request kind
    # publish an ordinary root artifact, while every other reconcile retains
    # the strict >=2-current-heads invariant below.
    is_reconcile_request = (reconcile_request and type_name == "reconcile"
                            and extras.get("request_kind") == "human_override"
                            and extras.get("parents") == [])
    if supersedes is not None:
        if type_name == "reconcile":
            on_error("publish rejected: a reconcile merges via its "
                     "'parents' list — it may not also supersede")
            return None
        if not _valid_artifact_id(supersedes):
            on_error("publish rejected: supersedes %s is not a valid "
                     "artifact id" % (_short(supersedes),))
            return None
        pre = load_meta(project_dir, supersedes, on_error=on_error)
        if pre is None:
            on_error("publish rejected: cannot derive — parent %s is "
                     "missing or unreadable" % (_short(supersedes),))
            return None
        plin = _lineage_list(pre, on_error)
        lroot = plin[0] if plin else supersedes
    elif type_name == "reconcile" and not is_reconcile_request:
        # Merge declaration: >=2 distinct valid ids sharing ONE root
        # (the root names the lock; head-set equality is re-checked
        # under it). Per-element validity is checked BEFORE set(), so an
        # unhashable list/dict member (valid JSON an agent could emit)
        # takes the refusal path instead of raising TypeError.
        raw = extras.get("parents")
        if (not isinstance(raw, list) or len(raw) < 2
                or any(not _valid_artifact_id(p) for p in raw)
                or len(set(raw)) != len(raw)):
            on_error("publish rejected: reconcile 'parents' must be a "
                     "list of >=2 distinct artifact ids, got %s"
                     % (_short(raw),))
            return None
        parents = list(raw)
        roots = set()
        for pid in parents:
            m = load_meta(project_dir, pid, on_error=on_error)
            if m is None:
                on_error("publish rejected: reconcile parent %s is "
                         "missing or unreadable" % (_short(pid),))
                return None
            roots.add(_meta_root(m))
        if len(roots) != 1:
            on_error("publish rejected: reconcile parents span %d "
                     "lineages (%s) — a reconcile merges ONE lineage"
                     % (len(roots), sorted(map(repr, roots))))
            return None
        lroot = roots.pop()
    if lroot is not None:
        try:
            lin_fd = os.open(_lineage_lock_path(project_dir, lroot),
                             os.O_CREAT | os.O_RDWR, 0o644)
            fcntl.flock(lin_fd, fcntl.LOCK_EX)
        except OSError as exc:
            if lin_fd is not None:
                os.close(lin_fd)
            on_error("publish failed: cannot lock lineage %r (%s)"
                     % (lroot, exc))
            return None
    try:
        plan = None
        if supersedes is not None:
            # TOCTOU: the parent is RE-read under the lock (it may have
            # converged or vanished since the pre-check above).
            plan = _derivation_plan(project_dir, supersedes, body_bytes,
                                    type_name, title, entry,
                                    source["section"], on_error)
            if plan is None:
                return None
            if plan["forced_status"] is not None:
                status = plan["forced_status"]
        elif parents is not None:
            plan = _reconcile_plan(project_dir, parents,
                                   source["section"], on_error)
            if plan is None:
                return None
            extras["parents"] = plan.pop("parents_sorted")
        # 4.12 pre-push gate: an outcome carrying recorded failures forces
        # quarantine, overriding any policy/derivation status — a blocked
        # artifact must never masquerade as final/pending. publish stays the
        # single status assigner.
        if gate and gate.get("failures"):
            status = "quarantined"
        final_sensitivity = "private" if (
            sensitivity == "private"
            or requested_sensitivity == "private"
            or (plan and plan.get("sensitivity") == "private")) else "normal"
        try:
            guard_fd = os.open(os.path.join(root, ".publish.lock"),
                               os.O_CREAT | os.O_RDWR, 0o644)
        except OSError as exc:
            on_error("publish failed: cannot open %s (%s)" % (root, exc))
            return None
        try:
            fcntl.flock(guard_fd, fcntl.LOCK_EX)
        except OSError as exc:
            # Acquisition failure (ENOLCK/EOPNOTSUPP on network volumes)
            # must report, not raise — and the try/finally that would
            # close the fd has not been entered yet.
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
                ts = _now_iso()
                record = {
                    "schema_version": schemas.SCHEMA_VERSION,
                    "id": aid,
                    "type": type_name,
                    "title": title,
                    "source": source,
                    "version": plan["version"] if plan else 1,
                    "supersedes": plan["supersedes"] if plan else None,
                    "lineage": list(plan["lineage"]) if plan else [],
                    "branch": plan["branch"] if plan else "",
                    "depth": plan["depth"] if plan else 0,
                    "hop_count": plan["hop_count"] if plan else 0,
                    "content_hash": hashlib.sha256(body_bytes).hexdigest(),
                    "keywords": keywords,
                    "doc_slots": doc_slots,
                    "status": status,
                    "sensitivity": final_sensitivity,
                    # 4.8: WHO set each status, WHEN — provenance for the gate.
                    "status_history": [{"status": status, "at": ts,
                                        "by": source.get("session")
                                        or "engine"}],
                    "ts": ts,
                    "fields": extras,
                }
                # 4.12: the engine-owned gate record (attempts/failures/
                # source_hash) — present ONLY when the publish path passed
                # one, so an ungated publish's meta is byte-identical to
                # before this card.
                if gate is not None:
                    record["gate"] = gate
                if plan_ref is not None:
                    record["plan_ref"] = plan_ref
                _fsync_write(
                    os.path.join(tmp, "meta.json"),
                    json.dumps(record, indent=2,
                               sort_keys=True).encode("utf-8"))
                # fsync the tmp DIR itself: the dirents naming body.md
                # and meta.json live in ITS data, and POSIX does not
                # persist them as a side effect of fsyncing the files —
                # without this a power cut could surface artifacts/<id>/
                # missing a child, the torn state the module rules out.
                tdfd = os.open(tmp, os.O_RDONLY)
                try:
                    os.fsync(tdfd)
                finally:
                    os.close(tdfd)
                # os.rename (not os.replace): the destination must not
                # exist — mint under the lock guarantees it, and a
                # violated invariant should fail loudly rather than
                # merge directories.
                os.rename(tmp, artifact_dir(project_dir, aid))
            except Exception as exc:
                shutil.rmtree(tmp, ignore_errors=True)
                on_error("publish failed for type %r (%s) — nothing was "
                         "stored" % (type_name, exc))
                return None
            # The rename made the artifact visible: from here on, failure
            # may only be reported as success-with-warning — "nothing was
            # stored" about an artifact readers can already see would
            # make the caller skip artifact_published and a retry mint a
            # -2 duplicate (R2, §12.4). A failed parent-dir fsync merely
            # widens the accepted power-loss residual to this last
            # publish's dirent.
            try:
                dfd = os.open(root, os.O_RDONLY)
                try:
                    os.fsync(dfd)
                finally:
                    os.close(dfd)
            except OSError as exc:
                on_error("publish %r: artifacts dir fsync failed (%s) — "
                         "the artifact is live but may not survive a "
                         "power cut" % (aid, exc))
        finally:
            try:
                fcntl.flock(guard_fd, fcntl.LOCK_UN)
            finally:
                os.close(guard_fd)
        return aid
    finally:
        if lin_fd is not None:
            try:
                fcntl.flock(lin_fd, fcntl.LOCK_UN)
            finally:
                os.close(lin_fd)


def publish_one_block(project_dir, block, source, registry, on_error=None,
                      consensus=False, gate=None, sensitivity=None):
    """Publish ONE already-parsed ```artifact-json``` block — the per-block step
    of publish_from_output, factored out so the 4.12 gated publish path and the
    ungated path share ONE meta construction and can never drift. ``block`` is
    a dict with 'type'/'title' and an optional string 'body'; ``source`` is the
    engine provenance dict (it overrides anything the block claims); ``gate`` is
    threaded to publish() to quarantine a blocked artifact. Returns the
    published id, or None (reason reported)."""
    if on_error is None:
        on_error = lambda _msg: None
    meta = dict(block) if isinstance(block, dict) else {}
    body = meta.pop("body", None)
    if body is not None and not isinstance(body, str):
        on_error("artifact-json block %r: 'body' must be a single string "
                 "— skipped" % (meta.get("title"),))
        return None
    meta["source"] = dict(source) if isinstance(source, dict) else {}
    return publish(project_dir, body, meta, registry, on_error=on_error,
                   consensus=consensus, gate=gate,
                   sensitivity=sensitivity)


def publish_from_output(project_dir, final_output, source, registry,
                        on_error=None, dedupe_against=None, consensus=False,
                        sensitivity=None):
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
        # Peek the body WITHOUT mutating the block (publish_one_block re-copies
        # and pops it); the non-string skip stays ahead of the dedupe so the
        # hash below can never see a non-encodable body.
        body = block.get("body")
        if body is not None and not isinstance(body, str):
            on_error("artifact-json block %r: 'body' must be a single "
                     "string — skipped" % (block.get("title"),))
            continue
        if dedupe_against:
            title = block.get("title")
            title = title if isinstance(title, str) else str(title)
            try:
                bhash = hashlib.sha256(
                    (body or "").encode("utf-8")).hexdigest()
            except UnicodeEncodeError:
                bhash = None  # publish() will reject the block anyway
            if bhash and (block.get("type"), title, bhash) in dedupe_against:
                on_error("artifact %r already published by this phase "
                         "close — skipped (crash-resume dedupe)" % (title,))
                continue
        aid = publish_one_block(project_dir, block, src, registry,
                                on_error=on_error, consensus=consensus,
                                sensitivity=sensitivity)
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
        on_error("artifact %s has an invalid id" % (_short(artifact_id),))
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
        on_error("artifact %s has an invalid id" % (_short(artifact_id),))
        return None
    # This is only a tombstone guard, not a second legacy read failure: an
    # absent/corrupt meta must not add a duplicate error before body.md's own
    # established diagnostic below.
    meta = load_meta(project_dir, artifact_id, on_error=lambda _m: None)
    if meta is not None and meta.get("status") == "tombstoned":
        on_error("artifact %r is tombstoned; provenance survives in meta.json"
                 % (artifact_id,))
        return None
    path = os.path.join(artifact_dir(project_dir, artifact_id), "body.md")
    try:
        with open(path, encoding="utf-8", newline="") as fh:
            return fh.read()
    except OSError as exc:
        on_error("artifact %r body unreadable: %s" % (artifact_id, exc))
        return None


def resolve_artifact(project_dir, artifact_id, on_error=None):
    """Resolve one artifact as ``(meta, body)`` or raise a typed read error.

    This strict seam is for user-facing resolvers. Legacy ``read_body`` keeps
    its reported-never-raised contract, while a tombstone here is distinct
    from corruption/missing data and carries the complete surviving meta.
    """
    errors = []
    report = errors.append if on_error is None else on_error
    meta = load_meta(project_dir, artifact_id, on_error=report)
    if meta is None:
        raise ArtifactReadError(
            errors[-1] if errors else "artifact is missing or unreadable")
    if meta.get("status") == "tombstoned":
        raise TombstonedArtifactError(
            "artifact %r is tombstoned; provenance survives in meta.json"
            % artifact_id, meta=meta)
    body = read_body(project_dir, artifact_id, on_error=report)
    if body is None:
        raise ArtifactReadError(
            errors[-1] if errors else "artifact body is unreadable", meta=meta)
    return meta, body


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


# ---------------------------------------------------------------------------
# 4.3 read side: the lineage index, head computation, latest_final,
# staleness. House style holds: reported via on_error, never raised.
# ---------------------------------------------------------------------------
def lineage_index(project_dir, on_error=None):
    """One O(N) scan powering every lineage read helper — pass it via
    index= to lineage_heads/latest_final/is_stale when badging a whole
    store (4.9), so N artifacts cost one scan, not N."""
    idx = {"children": {}, "reconciled": {}, "by_id": {}}
    metas = list_artifacts(project_dir, on_error=on_error)
    for m in metas:
        idx["by_id"][m["id"]] = m
        parent = m.get("supersedes")
        if isinstance(parent, str):
            idx["children"].setdefault(parent, []).append(m)
    # Reconcile edges are added in a SECOND pass, and ONLY when the
    # reconcile shares the head's lineage root. A reconcile whose parents
    # name a foreign lineage (a 4.2-era store wrote `parents` unvalidated,
    # so real upgrade data can carry this) must never splice two lineages
    # together and make latest_final silently return a foreign artifact.
    for m in metas:
        if m.get("type") != "reconcile":
            continue
        declared = (m.get("fields") or {}).get("parents") \
            if isinstance(m.get("fields"), dict) else None
        if not isinstance(declared, list):
            continue
        mroot = _meta_root(m)
        for head in declared:
            if not isinstance(head, str):
                continue
            hm = idx["by_id"].get(head)
            if hm is not None and _meta_root(hm) == mroot:
                idx["reconciled"].setdefault(head, []).append(m)
    return idx


def _members_from(idx, root_id):
    """Every member of a lineage reachable from its root via supersedes
    edges and reconcile merges. The visited set bounds hand-edited
    cycles — the walk always terminates."""
    members = set()
    frontier = [root_id]
    while frontier:
        cur = frontier.pop()
        if cur in members:
            continue
        members.add(cur)
        frontier.extend(c["id"] for c in idx["children"].get(cur, []))
        frontier.extend(r["id"] for r in idx["reconciled"].get(cur, []))
    return members


def _branch_label(meta):
    """Display label for refusal messages: the ""-branch head is 'a'
    when named alongside siblings (v3-a/v3-b per the card)."""
    return meta.get("branch") or "a"


def lineage_heads(project_dir, root, on_error=None, *, index=None):
    """The unreconciled live head metas of ``root``'s lineage (root may
    be ANY member id), sorted by (version, branch, id). None only for an
    unknown id. Converged tombstones are pruned (they are always
    leaves), so a converged tip resolves to its live ancestor; a node
    is consumed by a reconcile that lists it. No refusal semantics here
    — latest_final layers those on."""
    if on_error is None:
        on_error = lambda _msg: None
    if not isinstance(root, str):
        # An unhashable/non-str id must report like any other unknown id,
        # never raise at the dict lookup below (readers never raise).
        on_error("lineage %s: unknown artifact" % (_short(root),))
        return None
    idx = index if index is not None else lineage_index(
        project_dir, on_error=on_error)
    meta = idx["by_id"].get(root)
    if meta is None:
        on_error("lineage %r: unknown artifact" % (root,))
        return None
    root_id = _meta_root(meta)
    members = _members_from(idx, root_id)
    heads = []
    for mid in members:
        m = idx["by_id"].get(mid)
        if m is None or m.get("status") == "converged":
            continue
        live_kids = [c for c in idx["children"].get(mid, [])
                     if c.get("status") != "converged"
                     and c["id"] in members]
        if live_kids:
            continue
        if [r for r in idx["reconciled"].get(mid, [])
                if r["id"] in members]:
            continue
        heads.append(m)
    # Type-safe sort key: a hand-edited/upgrade meta with "version":"2"
    # or a numeric branch must not raise a str/int comparison TypeError —
    # exactly the branched (>=2 heads) case latest_final exists to
    # referee. m["id"] is always the directory-name str (list_artifacts
    # forces it). Never drop a mistyped head: that would let latest_final
    # silently pick a winner.
    def _head_key(m):
        return (_int_field(m.get("version"), 0),
                m.get("branch") if isinstance(m.get("branch"), str) else "",
                m["id"])
    heads.sort(key=_head_key)
    return heads


def latest_final(project_dir, root, on_error=None, *, index=None):
    """The resolved tip meta of a lineage, or None with the reason
    reported. On a branched, unreconciled lineage this REFUSES, naming
    every head — it never silently picks a winner (R2). After a valid
    reconcile it returns the reconcile artifact; a superseded reconcile
    resolves to its child; a branched reconcile refuses again,
    recursively, from the same head computation."""
    if on_error is None:
        on_error = lambda _msg: None
    idx = index if index is not None else lineage_index(
        project_dir, on_error=on_error)
    heads = lineage_heads(project_dir, root, on_error=on_error, index=idx)
    if heads is None:
        return None
    if len(heads) == 1:
        return heads[0]
    if not heads:
        on_error("lineage %r is unresolvable (no live head — cycle or "
                 "corruption)" % (root,))
        return None
    named = ", ".join("%s (v%s-%s)" % (m["id"],
                                       _int_field(m.get("version"), 0),
                                       _branch_label(m))
                      for m in heads)
    on_error("lineage %r is branched: heads %s — publish a 'reconcile' "
             "artifact listing all heads" % (root, named))
    return None


def lineage_is_killed(project_dir, root, on_error=None, *, index=None):
    """Whether any current head marks the entire lineage ``killed``.

    Branch ambiguity is handled conservatively: a kill decision on one live
    head hides the lineage rather than allowing a sibling branch to resurrect
    it through retrieval/search.
    """
    idx = index if index is not None else lineage_index(
        project_dir, on_error=on_error)
    heads = lineage_heads(project_dir, root, on_error=on_error, index=idx)
    return bool(heads and any(h.get("status") == "killed" for h in heads))


def is_lifecycle_excluded(project_dir, meta, on_error=None, *, index=None):
    """Shared retrieval/search exclusion for dead or reclaimed content."""
    if not isinstance(meta, dict):
        return True
    if meta.get("status") in ("killed", "tombstoned"):
        return True
    idx = index if index is not None else lineage_index(
        project_dir, on_error=on_error)
    return lineage_is_killed(project_dir, _meta_root(meta),
                             on_error=on_error, index=idx)


def _reconcile_plan(project_dir, parents, source_section, on_error):
    """Under the lineage lock: the parent set must EQUAL the current
    unreconciled head set (a reconcile naming one head, a non-head
    interior node, a subset, or stale heads is refused, naming the
    difference). Reconciles are cap-EXEMPT — they are the mandated exit
    from a branched state, and refusing one would wedge latest_final
    forever."""
    idx = lineage_index(project_dir, on_error=on_error)
    metas = []
    for pid in parents:
        m = idx["by_id"].get(pid)
        if m is None:
            on_error("publish rejected: reconcile parent %s vanished "
                     "before the lineage lock" % (_short(pid),))
            return None
        metas.append(m)
    heads = lineage_heads(project_dir, metas[0]["id"],
                          on_error=on_error, index=idx) or []
    head_ids = sorted(h["id"] for h in heads)
    if sorted(parents) != head_ids:
        on_error("publish rejected: reconcile parents %s must equal the "
                 "lineage's current unreconciled head set %s"
                 % (sorted(parents), head_ids))
        return None
    chains = [_lineage_list(m, on_error) + [m["id"]] for m in metas]
    lcp = []
    for step in zip(*chains):
        if all(v == step[0] for v in step):
            lcp.append(step[0])
        else:
            break
    depths = [_int_field(m.get("depth"), len(_lineage_list(m, on_error)))
              for m in metas]
    hops = [_int_field(m.get("hop_count"), 0) for m in metas]
    versions = [_int_field(m.get("version"), 1) for m in metas]
    sections = [(m.get("source") or {}).get("section") or ""
                for m in metas if isinstance(m.get("source"), dict)]
    inc = 0 if (source_section
                and source_section in [s for s in sections if s]) else 1
    return {"supersedes": None, "lineage": lcp,
            "version": max(versions) + 1, "branch": "",
            "depth": max(depths) + 1, "hop_count": max(hops) + inc,
            "forced_status": None, "parents_sorted": sorted(parents),
            "sensitivity": ("private" if any(
                m.get("sensitivity") == "private" for m in metas)
                else "normal")}


def is_stale(project_dir, meta, on_error=None, *, index=None):
    """True iff this artifact has an AUTHORITATIVE successor: exactly
    one non-converged superseding child (the linear next version), or
    any reconcile listing it as a parent. Rival unreconciled branch
    heads do NOT make their parent stale — there is no authoritative
    successor yet, and a stale badge would push consumers toward the
    exact guess latest_final refuses. A converged child never stales
    its parent (the parent IS the live content). Shared by 4.9's badge
    and 4.7's retrieval preference."""
    if not isinstance(meta, dict):
        return False
    mid = meta.get("id")
    if not isinstance(mid, str):
        return False
    idx = index if index is not None else lineage_index(
        project_dir, on_error=on_error)
    if idx["reconciled"].get(mid):
        return True
    live = [c for c in idx["children"].get(mid, [])
            if c.get("status") != "converged"]
    return len(live) == 1


def is_intent_stale(project_dir, meta, on_error=None, *, index=None):
    """True when an output cites a plan version with an authoritative edit.

    Missing/malformed provenance is not guessed into staleness. A dangling
    plan reference is reported but remains false because the store cannot
    prove that a newer approved intent exists.
    """
    if on_error is None:
        on_error = lambda _m: None
    if not isinstance(meta, dict):
        return False
    ref = meta.get("plan_ref")
    if not isinstance(ref, dict):
        return False
    plan_id = ref.get("plan_id")
    version = ref.get("plan_version")
    if not isinstance(plan_id, str) or not plan_id \
            or not isinstance(version, int) or isinstance(version, bool) \
            or version <= 0:
        on_error("artifact %r has malformed plan_ref" % meta.get("id"))
        return False
    idx = index if index is not None else lineage_index(
        project_dir, on_error=on_error)
    plan = idx.get("by_id", {}).get(plan_id) if isinstance(idx, dict) else None
    if not isinstance(plan, dict) or plan.get("type") != "plan":
        on_error("artifact %r cites missing plan %r" %
                 (meta.get("id"), plan_id))
        return False
    if plan.get("version") != version:
        return True
    return is_stale(project_dir, plan, on_error=on_error, index=idx)


# ---------------------------------------------------------------------------
# 4.5 PUSH routing: materialize an admissible artifact into a target
# session's carryover context. artifacts.py stays a schemas-only leaf — it
# does NOT import orchestrator, emit events, or read config; the CLI hook
# owns those. State I/O here is self-contained (never orchestrator.load_state,
# which resets a transiently-corrupt file to defaults — data loss).
# ---------------------------------------------------------------------------
# The carryover value is pre-capped to this many chars so build_context's
# tail-keep _budget (which cuts the FRONT) never truncates the provenance
# header. The CLI hook passes the target's resolved
# runtime.max_prior_output_chars so the push-time cap equals the render cap.
CARRYOVER_BUDGET_CHARS = 4000
_ROUTE_TRUNC_MARKER = "\n[...routed artifact body truncated to fit context budget...]"


def is_admissible(project_dir, meta, on_error=None, *, index=None):
    """THE shared admission predicate for routing (4.5 route_push, later
    4.7 retrieval + 7.2 guards). 'Admissible' means the live, routable
    tip of a RESOLVED lineage:
      * is_routable(meta)                — not a converged tombstone (4.3)
      * status == 'final'                — the finalization policies' (4.8)
                                           terminal state; not draft/
                                           pending_review/superseded
      * not is_stale(...)                — no authoritative successor
      * latest_final(root) IS this id    — lineage not branched/dangling
    Refusals are reported via on_error. This body IS the policy-driven
    status check 4.8 promised ('published' is RETIRED — see STATUS), kept
    behind route_push and its callers as the one admission seam."""
    if on_error is None:
        on_error = lambda _m: None
    if not isinstance(meta, dict):
        on_error("artifact meta is not an object")
        return False
    aid = meta.get("id")
    if not is_routable(meta):
        on_error("artifact %r is converged — not routable" % (aid,))
        return False
    if meta.get("status") != "final":
        on_error("artifact %r status is %r, not 'final' — not admissible "
                 "(finalize it first)" % (aid, meta.get("status")))
        return False
    if is_stale(project_dir, meta, on_error=on_error, index=index):
        on_error("artifact %r is superseded — route the lineage head, "
                 "not a stale version" % (aid,))
        return False
    root = _meta_root(meta)
    tip = latest_final(project_dir, root, on_error=on_error, index=index)
    if tip is None:
        return False  # latest_final already reported (branched / cycle)
    if tip.get("id") != aid:
        on_error("artifact %r is not the resolved tip of lineage %r "
                 "(tip is %r)" % (aid, root, tip.get("id")))
        return False
    return True


def _rewrite_meta_atomic(project_dir, artifact_id, meta):
    """Durably replace one artifact's meta.json in place: per-writer tmp
    beside it, fsync, os.replace, fsync the dir. The tmp lives INSIDE
    artifacts/<id>/ (not the store root), so publish's _sweep_orphans
    (which reaps artifacts/.tmp-*) never touches it."""
    adir = artifact_dir(project_dir, artifact_id)
    path = os.path.join(adir, "meta.json")
    tmp = "%s.%d.%x.tmp" % (path, os.getpid(), threading.get_ident())
    data = json.dumps(meta, indent=2, sort_keys=True).encode("utf-8")
    with open(tmp, "wb") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)
    dfd = os.open(adir, os.O_RDONLY)
    try:
        os.fsync(dfd)
    finally:
        os.close(dfd)


def _rewrite_body_atomic(project_dir, artifact_id, body):
    adir = artifact_dir(project_dir, artifact_id)
    path = os.path.join(adir, "body.md")
    tmp = "%s.%d.%x.tmp" % (path, os.getpid(), threading.get_ident())
    try:
        _fsync_write(tmp, body.encode("utf-8"))
        os.replace(tmp, path)
        dfd = os.open(adir, os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    finally:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass


def tombstone(project_dir, artifact_id, *, at=None, by="gc", on_error=None):
    """Atomically make one artifact logically tombstoned, then compact body.

    The meta transition is the commit point and lands before body compaction.
    Thus a kill at any instruction leaves the artifact either untouched or
    already non-readable as a tombstone. Re-running repairs an interrupted
    compaction without appending duplicate status history.
    """
    if on_error is None:
        on_error = lambda _m: None
    root = artifacts_root(project_dir)
    try:
        guard_fd = os.open(os.path.join(root, ".publish.lock"),
                           os.O_CREAT | os.O_RDWR, 0o644)
        fcntl.flock(guard_fd, fcntl.LOCK_EX)
    except OSError as exc:
        if "guard_fd" in locals():
            os.close(guard_fd)
        on_error("tombstone failed: cannot lock artifact store (%s)" % exc)
        return None
    try:
        meta = load_meta(project_dir, artifact_id, on_error=on_error)
        if meta is None:
            return None
        transitioned = meta.get("status") != "tombstoned"
        if transitioned:
            hist = meta.get("status_history")
            hist = list(hist) if isinstance(hist, list) else []
            hist.append({"status": "tombstoned", "at": at or _now_iso(),
                         "by": by})
            meta = dict(meta)
            meta["status"] = "tombstoned"
            meta["status_history"] = hist
            try:
                _rewrite_meta_atomic(project_dir, artifact_id, meta)
            except OSError as exc:
                on_error("tombstone failed writing %r meta (%s) — artifact "
                         "unchanged" % (artifact_id, exc))
                return None
        try:
            _rewrite_body_atomic(project_dir, artifact_id, TOMBSTONE_BODY)
        except OSError as exc:
            on_error("artifact %r is tombstoned but body compaction was "
                     "interrupted (%s); rerun GC to finish" %
                     (artifact_id, exc))
            return {"meta": meta, "transitioned": transitioned,
                    "compacted": False}
        return {"meta": meta, "transitioned": transitioned,
                "compacted": True}
    finally:
        try:
            fcntl.flock(guard_fd, fcntl.LOCK_UN)
        finally:
            os.close(guard_fd)


def _workspace_artifact_projects(workspace):
    """Project dirs containing an artifact store, excluding dot archives."""
    found = []
    for cur, dirs, _files in os.walk(workspace):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        if "artifacts" in dirs:
            found.append(cur)
            dirs.remove("artifacts")
    return sorted(found)


def gc_plan(workspace, keep_versions=5, on_error=None):
    """Return a deterministic, mutation-free tombstone plan for a workspace."""
    if on_error is None:
        on_error = lambda _m: None
    try:
        keep = int(keep_versions)
    except (TypeError, ValueError):
        keep = 5
    keep = max(1, keep)
    candidates = []
    stub_size = len(TOMBSTONE_BODY.encode("utf-8"))
    for project_dir in _workspace_artifact_projects(workspace):
        idx = lineage_index(project_dir, on_error=on_error)
        roots = sorted({_meta_root(m) for m in idx["by_id"].values()})
        for root_id in roots:
            heads = lineage_heads(project_dir, root_id, on_error=on_error,
                                  index=idx)
            # An unreconciled branch protects all bodies: an old shared node
            # is still evidence for deciding which branch wins.
            if not heads or len(heads) != 1:
                continue
            head = heads[0]
            head_version = _int_field(head.get("version"), 1)
            for aid in sorted(_members_from(idx, root_id)):
                meta = idx["by_id"].get(aid)
                if meta is None or aid == head.get("id"):
                    continue
                status = meta.get("status")
                body_path = os.path.join(artifact_dir(project_dir, aid),
                                         "body.md")
                try:
                    body_size = os.path.getsize(body_path)
                except OSError:
                    body_size = 0
                repair = status == "tombstoned" and body_size != stub_size
                distance = head_version - _int_field(meta.get("version"), 1)
                if not repair and (status == "tombstoned"
                                   or distance < keep
                                   or not is_stale(project_dir, meta,
                                                  on_error=on_error,
                                                  index=idx)):
                    continue
                rel_project = os.path.relpath(project_dir, workspace)
                candidates.append({
                    "project": rel_project,
                    "artifact_id": aid,
                    "bytes": max(0, body_size - stub_size),
                    "reason": ("finish interrupted tombstone compaction"
                               if repair else
                               "superseded by %d newer version(s); keep=%d"
                               % (distance, keep)),
                    "repair": repair,
                })
    candidates.sort(key=lambda c: (c["project"], c["artifact_id"]))
    return {"workspace": os.path.abspath(workspace),
            "keep_versions": keep,
            "candidates": candidates,
            "count": len(candidates),
            "bytes": sum(c["bytes"] for c in candidates)}


def apply_gc_plan(plan, on_error=None, on_tombstone=None):
    """Apply a generated plan per artifact; safe and idempotent after kill."""
    if on_error is None:
        on_error = lambda _m: None
    if on_tombstone is None:
        on_tombstone = lambda _item: None
    workspace = os.path.abspath(plan.get("workspace") or "") \
        if isinstance(plan, dict) else ""
    results = []
    for item in plan.get("candidates", []) if isinstance(plan, dict) else []:
        rel = item.get("project") if isinstance(item, dict) else None
        aid = item.get("artifact_id") if isinstance(item, dict) else None
        if not isinstance(rel, str) or not _valid_artifact_id(aid):
            on_error("GC plan contains an invalid candidate — skipped")
            continue
        project_dir = os.path.abspath(os.path.join(workspace, rel))
        try:
            inside = os.path.commonpath([workspace, project_dir]) == workspace
        except ValueError:
            inside = False
        if not inside:
            on_error("GC plan candidate escapes the workspace — skipped")
            continue
        result = tombstone(project_dir, aid, on_error=on_error)
        if not result or not result.get("compacted"):
            continue
        done = dict(item)
        done["transitioned"] = bool(result.get("transitioned"))
        results.append(done)
        if result.get("transitioned"):
            on_tombstone(done)
    return results


def finalize(project_dir, artifact_id, by, registry, *, human=False,
             at=None, on_error=None):
    """Transition an artifact to 'final', appending a status_history entry.
    Returns the updated meta, or None (refused, reported). Legal FROM-states
    are 'pending_review' and 'draft'; 'final' (re-finalize), 'converged' (a
    4.3 tombstone — never clobbered), and 'superseded' are refused loudly
    with meta UNTOUCHED. A 'requires_human' type finalizes ONLY when
    human=True (the review gate for a requires_review_gate type may be any
    identity). ``at`` is caller-supplied for deterministic provenance."""
    if on_error is None:
        on_error = lambda _m: None
    if not _valid_artifact_id(artifact_id):
        on_error("finalize rejected: %s is not a valid artifact id"
                 % (_short(artifact_id),))
        return None
    if not isinstance(by, str) or not by.strip():
        on_error("finalize rejected: a finalizer identity ('by') is required")
        return None
    at = at if isinstance(at, str) and at else _now_iso()
    root = artifacts_root(project_dir)
    try:
        guard_fd = os.open(os.path.join(root, ".publish.lock"),
                           os.O_CREAT | os.O_RDWR, 0o644)
    except OSError as exc:
        on_error("finalize failed: no artifact store at %s (%s)" % (root, exc))
        return None
    try:
        fcntl.flock(guard_fd, fcntl.LOCK_EX)
    except OSError as exc:
        os.close(guard_fd)
        on_error("finalize failed: cannot lock %s (%s)" % (root, exc))
        return None
    try:
        meta = load_meta(project_dir, artifact_id, on_error=on_error)
        if meta is None:
            return None
        cur = meta.get("status")
        if cur == "converged":
            on_error("finalize refused: %r is converged (a 4.3 tombstone) — "
                     "never finalizable" % (artifact_id,))
            return None
        if cur == "final":
            on_error("finalize refused: %r is already final" % (artifact_id,))
            return None
        if cur not in ("pending_review", "draft"):
            on_error("finalize refused: %r has status %r — only pending_review "
                     "or draft may be finalized" % (artifact_id, cur))
            return None
        types = registry.get("types") if isinstance(registry, dict) else None
        entry = types.get(meta.get("type")) if isinstance(types, dict) else None
        policy = _finalization_policy(entry or {}, on_error)
        if policy == "requires_human" and not human:
            on_error("finalize refused: type %r requires a HUMAN finalizer "
                     "(--by-human)" % (meta.get("type"),))
            return None
        hist = meta.get("status_history")
        hist = list(hist) if isinstance(hist, list) else []
        hist.append({"status": "final", "at": at, "by": by})
        new_meta = dict(meta)
        new_meta["status"] = "final"
        new_meta["status_history"] = hist
        try:
            _rewrite_meta_atomic(project_dir, artifact_id, new_meta)
        except OSError as exc:
            on_error("finalize failed writing %r meta (%s) — status unchanged"
                     % (artifact_id, exc))
            return None
        return new_meta
    finally:
        try:
            fcntl.flock(guard_fd, fcntl.LOCK_UN)
        finally:
            os.close(guard_fd)


def _state_path(session_dir):
    return os.path.join(session_dir, "agent_state.json")


def _read_target_state(session_dir):
    """(state, corrupt): absent -> (None, False); valid dict -> (dict,
    False); present-but-unparseable/non-dict -> (None, True). Unlike
    orchestrator.load_state, a corrupt file is NEVER silently reset."""
    path = _state_path(session_dir)
    if not os.path.exists(path):
        return None, False
    try:
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
    except (OSError, ValueError):
        return None, True
    if not isinstance(doc, dict):
        return None, True
    return doc, False


def _write_state_atomic(session_dir, state):
    path = _state_path(session_dir)
    tmp = "%s.%d.tmp" % (path, os.getpid())
    data = json.dumps(state, indent=2, default=str).encode("utf-8")
    with open(tmp, "wb") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def _render_carryover(meta, source_project_dir, body, budget):
    """Provenance header (kept as a prefix) + body. The returned value is
    ALWAYS <= budget so build_context's tail-keep _budget (which cuts the
    FRONT) takes its no-op fast path and never eats the header. When the
    budget is pathologically small (< header length) the body is dropped
    and the header itself is capped to budget — the provenance prefix is
    the last thing to go, never the first."""
    src = meta.get("source") if isinstance(meta.get("source"), dict) else {}
    header = ("[routed artifact] %s %s v%s\n"
              "source: %s/%s · phase %s\n"
              "routed-from: %s · %s\n---\n"
              % (meta.get("type", ""), meta.get("id", ""),
                 meta.get("version", 1),
                 src.get("section", ""), src.get("session", ""),
                 src.get("phase", ""),
                 os.path.basename(source_project_dir.rstrip("/") or
                                  source_project_dir),
                 _now_iso()))
    if len(header) >= budget:
        return header[:budget]                       # header itself capped
    if len(header) + len(body) <= budget:
        return header + body                         # everything fits
    room = budget - len(header)                      # > 0
    if room > len(_ROUTE_TRUNC_MARKER):
        return header + body[:room - len(_ROUTE_TRUNC_MARKER)] \
            + _ROUTE_TRUNC_MARKER
    return header + body[:room]                      # no room for the marker


def route_push(artifact_id, source_project_dir, target_session_dir,
               on_error=None, *, budget=None, default_state=None):
    """Materialize a provenance-headed reference to an ADMISSIBLE artifact
    into target_session_dir's carryover context, so the target's next
    phases treat it as authoritative prior context. Returns a result dict:
      {"status":"routed",  root,artifact_id,version,target,route_id}
      {"status":"noop",    ...}        idempotent re-push of same (id,ver)
      {"status":"refused", "reason":...}   NOTHING written to the target

    Keyed by the lineage ROOT ("artifact:<root>") so a newer version
    (which mints a new id) REPLACES the prior entry rather than adding a
    second. A machine-readable state["routed_artifacts"][root] ledger
    drives idempotency/versioning; build_context never reads it. The whole
    read-modify-write is flock-serialized and atomic — a concurrent push
    or a crash never tears the target's agent_state.json."""
    if on_error is None:
        on_error = lambda _m: None
    if budget is None:
        budget = CARRYOVER_BUDGET_CHARS
    if not _valid_artifact_id(artifact_id):
        on_error("route rejected: %s is not a valid artifact id"
                 % (_short(artifact_id),))
        return {"status": "refused", "reason": "invalid artifact id"}
    meta = load_meta(source_project_dir, artifact_id, on_error=on_error)
    if meta is None:
        on_error("route rejected: artifact %r not found under %s"
                 % (artifact_id, source_project_dir))
        return {"status": "refused", "reason": "artifact not found"}
    if not is_admissible(source_project_dir, meta, on_error=on_error):
        return {"status": "refused", "reason": "artifact not admissible"}
    body = read_body(source_project_dir, artifact_id, on_error=on_error)
    if body is None:
        return {"status": "refused", "reason": "artifact body unreadable"}
    if not os.path.isdir(target_session_dir):
        on_error("route rejected: target %r does not exist"
                 % (target_session_dir,))
        return {"status": "refused", "reason": "target does not exist"}

    root = _meta_root(meta)
    version = meta.get("version") if isinstance(
        meta.get("version"), int) else 1
    # Namespace the carryover key and the idempotency ledger by the SOURCE
    # project: artifact ids are title-slugs unique only WITHIN a project, so
    # two projects' "Dark mode" both mint id "dark-mode" — keying by the
    # bare root would collide and treat the second, unrelated push as an
    # idempotent no-op, silently dropping its content.
    source = os.path.basename(source_project_dir.rstrip("/") or
                              source_project_dir)
    led_key = "%s/%s" % (source, root)
    key = "artifact:%s" % led_key
    target = os.path.basename(target_session_dir.rstrip("/") or
                              target_session_dir)
    try:
        lock_fd = os.open(os.path.join(target_session_dir,
                                       ".carryover.lock"),
                          os.O_CREAT | os.O_RDWR, 0o644)
    except OSError as exc:
        on_error("route failed: cannot lock target %r (%s)"
                 % (target, exc))
        return {"status": "refused", "reason": "cannot lock target"}
    # Acquire the lock BEFORE establishing the unlock finally — otherwise a
    # LOCK_EX failure would close the fd here AND the finally would flock/
    # close it again (EBADF), masking the return with a raise.
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
    except OSError as exc:
        os.close(lock_fd)
        on_error("route failed: cannot lock target %r (%s)"
                 % (target, exc))
        return {"status": "refused", "reason": "cannot lock target"}
    try:
        state, corrupt = _read_target_state(target_session_dir)
        if corrupt:
            on_error("route rejected: target %r agent_state.json is "
                     "corrupt — nothing written" % (target,))
            return {"status": "refused", "reason": "corrupt target state"}
        if state is None:
            state = copy.deepcopy(default_state) \
                if isinstance(default_state, dict) else {}
        ledger = state.get("routed_artifacts")
        ledger = ledger if isinstance(ledger, dict) else {}
        prev = ledger.get(led_key)
        if isinstance(prev, dict):
            prev_id = prev.get("artifact_id")
            prev_ver = prev.get("version") if isinstance(
                prev.get("version"), int) else 0
            if prev_id == artifact_id:
                return {"status": "noop", "root": root,
                        "artifact_id": artifact_id, "version": version,
                        "target": target}
            if version <= prev_ver:
                on_error("route rejected: %r v%s does not supersede the "
                         "routed v%s of lineage %r"
                         % (artifact_id, version, prev_ver, root))
                return {"status": "refused", "reason": "version downgrade"}
        carry = state.get("carryover_outputs")
        carry = carry if isinstance(carry, dict) else {}
        carry[key] = _render_carryover(meta, source_project_dir, body,
                                       budget)
        state["carryover_outputs"] = carry
        ledger[led_key] = {"artifact_id": artifact_id, "version": version,
                           "source": source,
                           "content_hash": meta.get("content_hash"),
                           "at": _now_iso()}
        state["routed_artifacts"] = ledger
        try:
            _write_state_atomic(target_session_dir, state)
        except (OSError, TypeError, ValueError) as exc:
            on_error("route failed writing target %r state (%s) — nothing "
                     "stored" % (target, exc))
            return {"status": "refused", "reason": "state write failed"}
    finally:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        finally:
            os.close(lock_fd)
    return {"status": "routed", "root": root, "artifact_id": artifact_id,
            "version": version, "target": target,
            "route_id": "push-%s-%s" % (root, version)}


# ---------------------------------------------------------------------------
# 4.7 PULL retrieval: score the project's admissible artifacts against a
# phase query and return a budgeted, provenance-headed block for
# build_context. Modeled on knowledge.retrieve's score/sort/budget shape,
# but gated on is_admissible (4.8 tightens that one seam) and lineage-aware
# (only the live tip participates; a branched lineage contributes nothing).
# Bounded scan: one lineage_index over meta.json; each admitted body is read
# ONCE and stat-cached (knowledge.py's (mtime_ns,size) pattern).
# ---------------------------------------------------------------------------
_ART_WORD_RE = re.compile(r"[a-z0-9@._+]+")
ARTIFACT_CONTEXT_HEADER = "\n\n===== RELEVANT PROJECT ARTIFACTS ====="
# LRU-bounded (newest wins) so a long-lived / multi-project process cannot
# accumulate an open-ended body corpus — unlike knowledge.py's fixed-corpus
# cache, artifact bodies are unbounded over a fleet's lifetime.
_ART_BODY_CACHE: collections.OrderedDict = collections.OrderedDict()
_ART_BODY_CACHE_MAX = 256


def _art_terms(text):
    return set(_ART_WORD_RE.findall((text or "").lower()))


def _cached_body(project_dir, artifact_id, on_error):
    """(body_text, body_terms) for an artifact, stat-cached so a run's
    repeated retrievals never re-read an unchanged body.md. None if
    unreadable (reported)."""
    path = os.path.join(artifact_dir(project_dir, artifact_id), "body.md")
    try:
        st = os.stat(path)
        key = (st.st_mtime_ns, st.st_size)
    except OSError:
        key = None
    cached = _ART_BODY_CACHE.get(path)
    if key is not None and cached is not None and cached[0] == key:
        _ART_BODY_CACHE.move_to_end(path)
        return cached[1]
    body = read_body(project_dir, artifact_id, on_error=on_error)
    if body is None:
        return None
    value = (body, _art_terms(body))
    if key is not None:
        _ART_BODY_CACHE[path] = (key, value)
        _ART_BODY_CACHE.move_to_end(path)
        while len(_ART_BODY_CACHE) > _ART_BODY_CACHE_MAX:
            _ART_BODY_CACHE.popitem(last=False)
    return value


def _score_artifact(query_terms, meta, body_terms):
    """Keyword+title hits count triple; body-term overlap is the tie-breaker
    (knowledge.py:131 shape). One keyword hit outranks up to two body hits.
    Type-guarded: a hostile non-list keywords or non-string title degrades
    to empty rather than raising (retrieval must never crash a phase)."""
    strong = set()
    kws = meta.get("keywords")
    for k in (kws if isinstance(kws, list) else []):
        if isinstance(k, str):
            strong |= _art_terms(k)
    title = meta.get("title")
    strong |= _art_terms(title if isinstance(title, str) else "")
    return len(query_terms & strong) * 3 + len(query_terms & body_terms)


def retrieve(project_dir, query_text, max_chars=6000, top_k=3,
             sensitivity_filter=None, on_error=None, *, exclude_session=None,
             exclude_section=None):
    """A budgeted, provenance-headed '===== RELEVANT PROJECT ARTIFACTS ====='
    block of the top-k admissible artifacts scoring against query_text, or
    '' when nothing is relevant.

    Only the LIVE, admissible tip of each lineage participates
    (is_admissible: status 'final', not stale, not converged, the resolved
    latest); a branched/unreconciled lineage contributes NOTHING and its
    skip is reported once via on_error — retrieval never guesses a branch.
    The current session's own publications are omitted (self-echo, keyed on
    the (exclude_section, exclude_session) PAIR — a chat slug is unique only
    within a section, so a sibling section's same-named chat is NOT
    excluded). sensitivity_filter (meta -> admit?) is the 8.5 seam; default
    admits all. A corrupt meta is skipped and reported, never fatal.
    Provenance headers survive the char budget: a body is truncated, a
    header is never partially cut."""
    if on_error is None:
        on_error = lambda _m: None
    query_terms = _art_terms(query_text)
    if not query_terms:
        return ""
    sens = sensitivity_filter or (lambda _m: True)
    idx = lineage_index(project_dir, on_error=on_error)
    scored = []
    seen_roots = set()
    seen_tips = set()
    # Iterate the metas lineage_index already loaded — a second list_artifacts
    # would re-scan every meta.json and double-report each corrupt one.
    for meta in sorted(idx["by_id"].values(), key=lambda m: m["id"]):
        root = _meta_root(meta)
        if root in seen_roots:
            continue
        seen_roots.add(root)
        if lineage_is_killed(project_dir, root, on_error=on_error,
                             index=idx):
            continue
        tip = latest_final(project_dir, root, on_error=on_error, index=idx)
        if tip is None:                 # branched/cycle: reported once, no guess
            continue
        # Two distinct root keys can resolve to the same tip when a member's
        # lineage is hand-corrupted to empty while its supersedes edge holds;
        # dedup on the RESOLVED tip so it is never double-injected.
        if tip["id"] in seen_tips:
            continue
        seen_tips.add(tip["id"])
        if tip.get("status") in ("killed", "tombstoned"):
            continue
        if not is_admissible(project_dir, tip, on_error=None, index=idx):
            continue                    # non-final tip: passive omission
        src = tip.get("source") if isinstance(tip.get("source"), dict) else {}
        if (exclude_session is not None
                and src.get("session") == exclude_session
                and src.get("section") == exclude_section):
            continue                    # self-echo (section+chat pair)
        if not sens(tip):
            continue
        bt = _cached_body(project_dir, tip["id"], on_error)
        if bt is None:
            continue
        body, body_terms = bt
        s = _score_artifact(query_terms, tip, body_terms)
        if s > 0:
            scored.append((s, tip, body))
    if not scored:
        return ""
    scored.sort(key=lambda x: (-x[0], x[1]["id"]))
    parts = [ARTIFACT_CONTEXT_HEADER]
    used = len(ARTIFACT_CONTEXT_HEADER)
    for _s, tip, body in scored[:top_k]:
        src = tip.get("source") if isinstance(tip.get("source"), dict) else {}
        header = ("\n\n----- %s %s v%s (from %s/%s · phase %s) -----\n"
                  % (tip.get("type", ""), tip["id"], tip.get("version", 1),
                     src.get("section", ""), src.get("session", ""),
                     src.get("phase", "")))
        room = max_chars - used
        if len(header) >= room:         # a header must NEVER be partially cut
            break
        if len(header) + len(body) <= room:
            chunk = header + body
        else:
            avail = room - len(header)
            if avail > len(_ROUTE_TRUNC_MARKER):
                chunk = header + body[:avail - len(_ROUTE_TRUNC_MARKER)] \
                    + _ROUTE_TRUNC_MARKER
            else:
                chunk = header + body[:avail]
        parts.append(chunk)
        used += len(chunk)
        if used >= max_chars:
            break
    if len(parts) == 1:                 # only the block header, no artifact
        return ""
    return "".join(parts)
