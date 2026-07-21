#!/usr/bin/env python3
"""One-shot V2 fleet-config to V3 section-manifest migrator.

Dry-run by default. ``--apply`` writes atomically after backing up every tuned
source; unknown data blocks application unless ``--allow-unmapped`` is explicit.
"""

import argparse
import copy
import difflib
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

import modelrouting
import sections


HERE = os.path.dirname(os.path.abspath(__file__))
RECEIPT = ".orch-migrated-v3.json"

# Raw shipped-file hashes are the installed/no-git fallback. A mismatch with no
# recoverable baseline migrates the whole document (safe over-migration), never
# falsely labels it seed-identical.
SEED_HASHES = {
    "workflows/answer_question.json": "b1e6e84b5e9a900bf7d9d8267ea7af76387cfb727bd6ef4c0329460cf05e5e34",
    "workflows/app_build.json": "5c13d9c437825c05ef673abd7384a4040325f4741989e2949a568f75965b997a",
    "workflows/app_build_child.json": "0ce1d81a7e3b021bbe4140826a1d3aadd436a88aba8983bdd156edc2559011b4",
    "workflows/app_spec.json": "061573cdb0c7956a42d1cd74c9d608a1bf8539ac3945c1bea025ed35a61b92aa",
    "workflows/audit.json": "11e266de7156e6e96725f447ab95f5404d15da3ccf27bb84f9f8869437ba5d2f",
    "workflows/brainstorm.json": "41522a9684267855afcc25da73cd7d041b18ba681deffef3df0c01e0f9044da7",
    "workflows/chat_ideas.json": "98bf6f9d170780d8124eaa27cbbc6d87c6d812f6b2d1d7e79ceb21dd272a6b5b",
    "workflows/chat_research.json": "009a88421840edcfff9187d073106ebcb20772e7fb86cd5264c5824148f1f73b",
    "workflows/documentation.json": "b2eaab75d2d4542a5ce2bd1c48cfad3db1171fc6b11e53f51b24715a2eeacd9a",
    "workflows/enroll.json": "20eb16aff88a491966bf8c0c1717a49a0474ab9d427819b8de7a95ea82ee3c2e",
    "workflows/full_max.json": "3d641d184994a29ec7620f47fd5afeb3cfc98626bf4496e8d6137955b8da005a",
    "workflows/iterate.json": "90f8bf68795ced68686444e31229776c9592c09232aca41e01ca7fcb33587acd",
    "workflows/library_mining.json": "c527898ee7f69fe0f4e3276d6f13d38b39d6a3d4028923ce96ee4132f674f15a",
    "workflows/productionize.json": "27f7e468ac7f1e44b679b8aeac3f7557511a56d7f1b839b16bd8b5c90e6d3d09",
    "workflows/prototype.json": "2b1746a473a543bb5b84a4706746dbde6c52c4e5267a9ba8237d24195e91dced",
    "workflows/research.json": "1a5f695e33f705e9cc1be3642ac283dd0ffad896b09dbb6bc0984536417c09ea",
    "workflows/sprint.json": "15867bf2794c52524abc20210983e2c53a1fae0a8d071b61664935758496a2a4",
    "workflows/vslice.json": "67a95b3165e2bef1dc9f11612a0678e65f9fc23cddd49d8fa18d2462b7b2ea6f",
    "phase_rules.json": "0bb0d1751c668dcc78bd55e6c90e3bc6572a3bcf6b02ce4cd20170bcb148b558",
    "model_routing.json": "a84bbacd5673d00f83aef8fa0451854d37ccb826334d8c20d4e82e618eda2b10",
}

WORKFLOW_SECTIONS = {
    "brainstorm": "ideas", "chat_ideas": "ideas",
    "research": "research", "chat_research": "research",
    "audit": "qa", "app_spec": "planning",
    "library_mining": "library", "documentation": "documentation",
    "enroll": "documentation",
    "app_build": "build", "app_build_child": "build",
    "full_max": "build", "iterate": "build", "productionize": "build",
    "prototype": "build", "sprint": "build", "vslice": "build",
}
WORKFLOW_FIELDS = {"name", "title", "description", "target", "build_phase",
                   "budget", "overrides", "phases"}
PHASE_FIELDS = {"key", "folder", "file", "title", "purpose", "rounds",
                "roles", "writes", "reads_target", "verify", "checkpoint",
                "structurally_required", "requires_verification",
                "doc_sections", "test_deliverable", "conversational"}
RULE_ENTRY_FIELDS = {"rules", "required_output", "acceptance_checks"}
ROUTING_PHASE_FIELDS = set(modelrouting.PHASE_FIELDS) | {
    "timeout", "roles", "rounds", "cast_size", "instructions"}


class MigrationError(Exception):
    pass


def _sha_bytes(data):
    return hashlib.sha256(data).hexdigest()


def _file_hash(path):
    try:
        with open(path, "rb") as fh:
            return _sha_bytes(fh.read())
    except OSError:
        return None


def _read_json(path):
    with open(path, encoding="utf-8") as fh:
        value = json.load(fh)
    if not isinstance(value, dict):
        raise MigrationError("%s: top level must be an object" % path)
    return value


def _json_text(value):
    return json.dumps(value, indent=2, sort_keys=True) + "\n"


def _baseline(workspace, rel, current_hash):
    """Immutable shipped JSON, or None when only whole-doc carry is honest."""
    workspace = os.path.abspath(workspace)
    if workspace != HERE:
        path = os.path.join(HERE, rel)
        try:
            return _read_json(path)
        except (OSError, ValueError, MigrationError):
            return None
    try:
        raw = subprocess.run(
            ["git", "show", "HEAD:" + rel], cwd=workspace,
            capture_output=True, check=True, timeout=10).stdout
        if SEED_HASHES.get(rel) != _sha_bytes(raw):
            # A user may commit tuned fleet JSON. HEAD is then not a pristine
            # seed merely because git can read it; whole-document carry is the
            # only non-lossy claim available.
            return None
        value = json.loads(raw.decode("utf-8"))
        return value if isinstance(value, dict) else None
    except (OSError, ValueError, subprocess.SubprocessError):
        if SEED_HASHES.get(rel) == current_hash:
            try:
                return _read_json(os.path.join(workspace, rel))
            except (OSError, ValueError, MigrationError):
                pass
        return None


def _delta_paths(seed, current, prefix=""):
    if seed == current:
        return []
    if isinstance(seed, dict) and isinstance(current, dict):
        out = []
        for key in sorted(set(seed) | set(current)):
            path = prefix + "/" + str(key)
            if key not in seed:
                out.append(path + " (added)")
            elif key not in current:
                out.append(path + " (removed)")
            else:
                out.extend(_delta_paths(seed[key], current[key], path))
        return out
    if isinstance(seed, list) and isinstance(current, list):
        # Phase arrays are keyed by phase key so one insertion does not report
        # every later index as changed.
        if all(isinstance(x, dict) and isinstance(x.get("key"), str)
               for x in seed + current):
            return _delta_paths({x["key"]: x for x in seed},
                                {x["key"]: x for x in current}, prefix)
    return [prefix or "/"]


def _slug(value):
    slug = re.sub(r"[^a-z0-9_-]+", "-", str(value).strip().lower()).strip("-")
    return slug or "custom-workflow"


def _section_stub(name, workflow):
    sid = _slug(name)
    return {
        "id": sid, "title": str(workflow.get("title") or name).strip(),
        "workflow": copy.deepcopy(workflow), "default_mode": "manual",
        "artifact_types_emitted": [], "artifact_types_accepted": [],
        "dod_tier": "standard",
        "capabilities": {"writes": "workspace", "exec": False,
                         "external": False},
        "local_pins": [],
        "migration_review_required": True,
    }


def _is_ios_rule(text):
    low = str(text).lower()
    return any(token in low for token in (
        "ios", "iphone", "ipad", "swiftui", "xcode", "app store",
        "apple framework", "dark mode", "dynamic type"))


def _unknown_workflow_keys(rel, doc):
    out = []
    for key in sorted(set(doc) - WORKFLOW_FIELDS):
        out.append("%s: /%s" % (rel, key))
    phases = doc.get("phases")
    if not isinstance(phases, list):
        out.append("%s: /phases is not a list" % rel)
        return out
    for idx, phase in enumerate(phases):
        if not isinstance(phase, dict):
            out.append("%s: /phases/%d is not an object" % (rel, idx))
            continue
        label = phase.get("key", idx)
        for key in sorted(set(phase) - PHASE_FIELDS):
            out.append("%s: /phases/%s/%s" % (rel, label, key))
    return out


def _unknown_rule_keys(rel, doc):
    out = []
    for key in sorted(set(doc) - {"schema_version", "global_app_rules",
                                  "phases"}):
        out.append("%s: /%s" % (rel, key))
    phases = doc.get("phases")
    if not isinstance(phases, dict):
        return out + ["%s: /phases is not an object" % rel]
    for phase, entry in phases.items():
        if not isinstance(entry, dict):
            out.append("%s: /phases/%s is not an object" % (rel, phase))
            continue
        for key in sorted(set(entry) - RULE_ENTRY_FIELDS):
            out.append("%s: /phases/%s/%s" % (rel, phase, key))
        for key in RULE_ENTRY_FIELDS:
            if key in entry and not isinstance(entry[key], list):
                out.append("%s: /phases/%s/%s is not a list" %
                           (rel, phase, key))
    if "global_app_rules" in doc and not isinstance(
            doc.get("global_app_rules"), list):
        out.append("%s: /global_app_rules is not a list" % rel)
    return out


def _unknown_routing_keys(rel, doc):
    out = []
    known_top = {"schema_version", "_docs", "_examples", "enabled",
                 "fallback", "phases"}
    for key in sorted(set(doc) - known_top):
        out.append("%s: /%s" % (rel, key))
    fb = doc.get("fallback")
    if isinstance(fb, dict):
        for key in sorted(set(fb) - {"cloud_to_local", "local_model", "chains"}):
            out.append("%s: /fallback/%s" % (rel, key))
    elif fb is not None:
        out.append("%s: /fallback is not an object" % rel)
    phases = doc.get("phases")
    if not isinstance(phases, dict):
        return out + ["%s: /phases is not an object" % rel]
    for phase, entry in phases.items():
        if not isinstance(entry, dict):
            out.append("%s: /phases/%s is not an object" % (rel, phase))
            continue
        for key in sorted(set(entry) - ROUTING_PHASE_FIELDS):
            out.append("%s: /phases/%s/%s" % (rel, phase, key))
        for key in modelrouting.PHASE_FIELDS:
            if key in entry and not isinstance(entry[key], str):
                out.append("%s: /phases/%s/%s is not a string" %
                           (rel, phase, key))
        for key in ("timeout", "rounds", "cast_size"):
            if key in entry and (not isinstance(entry[key], int)
                                 or isinstance(entry[key], bool)):
                out.append("%s: /phases/%s/%s is not an integer" %
                           (rel, phase, key))
        if "instructions" in entry and not isinstance(
                entry["instructions"], str):
            out.append("%s: /phases/%s/instructions is not a string" %
                       (rel, phase))
        roles = entry.get("roles")
        if isinstance(roles, dict):
            for role, values in roles.items():
                if role not in modelrouting.ROLE_NAMES:
                    out.append("%s: /phases/%s/roles/%s" %
                               (rel, phase, role))
                elif isinstance(values, dict):
                    for key in sorted(set(values) - set(modelrouting.PHASE_FIELDS)):
                        out.append("%s: /phases/%s/roles/%s/%s" %
                                   (rel, phase, role, key))
                    for key, value in values.items():
                        if key in modelrouting.PHASE_FIELDS and not isinstance(
                                value, str):
                            out.append("%s: /phases/%s/roles/%s/%s is not "
                                       "a string" % (rel, phase, role, key))
        elif roles is not None:
            out.append("%s: /phases/%s/roles is not an object" %
                       (rel, phase))
    return out


def _phase_keys(workflow):
    phases = workflow.get("phases") if isinstance(workflow, dict) else None
    return [p.get("key") for p in phases if isinstance(p, dict)
            and isinstance(p.get("key"), str)] if isinstance(phases, list) else []


def _discover_overlays(workspace):
    found = []
    skip = {".git", ".build", "gui", "tests", "workflows", "sections",
            "__pycache__"}
    for cur, dirs, files in os.walk(workspace):
        dirs[:] = [d for d in dirs if d not in skip and not d.startswith(".")]
        path = os.path.join(cur, "model_routing.json")
        if "model_routing.json" in files and os.path.abspath(path) != \
                os.path.abspath(os.path.join(workspace, "model_routing.json")):
            found.append(os.path.relpath(path, workspace))
    return sorted(found)


def _receipt_state(workspace, source_hashes):
    path = os.path.join(workspace, RECEIPT)
    if not os.path.exists(path):
        return None, []
    try:
        receipt = _read_json(path)
    except (OSError, ValueError, MigrationError) as exc:
        return None, ["migration receipt is unreadable: %s" % exc]
    if receipt.get("source_hashes") != source_hashes:
        return None, []
    drift = []
    for rel, expected in (receipt.get("target_hashes") or {}).items():
        if _file_hash(os.path.join(workspace, rel)) != expected:
            drift.append("receipt target changed after migration: %s" % rel)
    return receipt if not drift else None, drift


def build_plan(workspace):
    workspace = os.path.abspath(workspace)
    source_docs = {}
    source_hashes = {}
    baselines = {}
    invalid = []
    invalid_rels = []
    missing = []
    rels = ["phase_rules.json", "model_routing.json"]
    wf_dir = os.path.join(workspace, "workflows")
    try:
        rels += ["workflows/" + name for name in sorted(os.listdir(wf_dir))
                 if name.endswith(".json")]
    except OSError:
        pass
    rels = sorted(set(rels) | set(SEED_HASHES))
    for rel in rels:
        path = os.path.join(workspace, rel)
        digest = _file_hash(path)
        source_hashes[rel] = digest
        if digest is None:
            missing.append(rel)
            continue
        try:
            source_docs[rel] = _read_json(path)
        except (OSError, ValueError, MigrationError) as exc:
            invalid.append("%s: unreadable JSON (%s)" % (rel, exc))
            invalid_rels.append(rel)
            continue
        baselines[rel] = _baseline(workspace, rel, digest)
    receipt, drift = _receipt_state(workspace, source_hashes)
    if receipt:
        return {"workspace": workspace, "already_migrated": True,
                "receipt": receipt, "source_hashes": source_hashes,
                "targets": {}, "entries": [], "seed_identical": [],
                "defaults": missing,
                "unmapped": [], "overlays": _discover_overlays(workspace),
                "backups": []}

    plan = {"workspace": workspace, "already_migrated": False,
            "source_hashes": source_hashes, "targets": {}, "entries": [],
            "seed_identical": [], "defaults": missing,
            "unmapped": invalid + drift,
            "overlays": _discover_overlays(workspace), "backups": []}
    targets = plan["targets"]

    def target(rel, default):
        if rel not in targets:
            path = os.path.join(workspace, rel)
            try:
                targets[rel] = _read_json(path)
            except (OSError, ValueError, MigrationError):
                targets[rel] = copy.deepcopy(default)
        return targets[rel]

    workflow_docs = {os.path.basename(rel)[:-5]: doc
                     for rel, doc in source_docs.items()
                     if rel.startswith("workflows/")}
    workflow_sections = dict(WORKFLOW_SECTIONS)
    # Existing section manifests are authoritative for new/data-only studios.
    sec_root = os.path.join(workspace, "sections")
    try:
        sec_names = sorted(os.listdir(sec_root))
    except OSError:
        sec_names = []
    for sec in sec_names:
        path = os.path.join(sec_root, sec, "section.json")
        try:
            manifest = _read_json(path)
        except (OSError, ValueError, MigrationError):
            continue
        wf = manifest.get("workflow")
        if isinstance(wf, str):
            workflow_sections.setdefault(wf, sec)

    tuned_workflows = {}
    for name, doc in sorted(workflow_docs.items()):
        rel = "workflows/%s.json" % name
        seed = baselines.get(rel)
        deltas = _delta_paths(seed, doc) if seed is not None else \
            ([] if SEED_HASHES.get(rel) == source_hashes.get(rel)
             else ["/ (complete document; pristine baseline unavailable)"])
        if not deltas:
            plan["seed_identical"].append(rel)
            continue
        plan["unmapped"].extend(_unknown_workflow_keys(rel, doc))
        section = workflow_sections.get(name)
        if section is None:
            section = _slug(name)
            workflow_sections[name] = section
            dest_rel = "sections/%s/section.json" % section
            manifest = target(dest_rel, _section_stub(name, doc))
            manifest.update(_section_stub(name, doc))
            destination = dest_rel + ":/workflow (custom; review required)"
        else:
            dest_rel = "sections/%s/section.json" % section
            manifest = target(dest_rel, _section_stub(section, doc))
            current_ref = manifest.get("workflow")
            if current_ref == name or (isinstance(current_ref, dict)
                                       and current_ref.get("name") == name):
                manifest["workflow"] = copy.deepcopy(doc)
                destination = dest_rel + ":/workflow"
            else:
                variants = manifest.get("workflow_variants")
                variants = dict(variants) if isinstance(variants, dict) else {}
                variants[name] = copy.deepcopy(doc)
                manifest["workflow_variants"] = variants
                destination = dest_rel + ":/workflow_variants/%s" % name
        tuned_workflows[name] = doc
        plan["backups"].append(rel)
        for delta in deltas:
            plan["entries"].append({"source": rel, "delta": delta,
                                    "destination": destination})

    owners = {}
    # Existing rules explicitly name their studio ownership.
    for sec in sec_names:
        try:
            rules = _read_json(os.path.join(sec_root, sec, "rules.json"))
        except (OSError, ValueError, MigrationError):
            rules = {}
        for phase in (rules.get("phases") or {}):
            owners.setdefault(phase, set()).add(sec)
    # Workflow phase membership fills uncovered and custom phases.
    for name, doc in workflow_docs.items():
        sec = workflow_sections.get(name)
        if sec:
            for phase in _phase_keys(doc):
                owners.setdefault(phase, set()).add(sec)

    rules_rel = "phase_rules.json"
    rules_doc = source_docs.get(rules_rel)
    rules_seed = baselines.get(rules_rel)
    if rules_doc is not None:
        plan["unmapped"].extend(_unknown_rule_keys(rules_rel, rules_doc))
        deltas = _delta_paths(rules_seed, rules_doc) if rules_seed is not None \
            else ([] if SEED_HASHES.get(rules_rel) == source_hashes.get(rules_rel)
                  else ["/ (complete document; pristine baseline unavailable)"])
        if not deltas:
            plan["seed_identical"].append(rules_rel)
        else:
            plan["backups"].append(rules_rel)
            if (rules_seed or {}).get("schema_version", 1) != \
                    rules_doc.get("schema_version", 1):
                plan["unmapped"].append(
                    "%s: /schema_version changed" % rules_rel)
            seed_phases = (rules_seed or {}).get("phases") or {}
            live_phases = rules_doc.get("phases") or {}
            for phase in sorted(set(seed_phases) | set(live_phases)):
                if seed_phases.get(phase) == live_phases.get(phase):
                    continue
                phase_owners = sorted(owners.get(phase) or [])
                if not phase_owners:
                    plan["unmapped"].append(
                        "%s: /phases/%s has no owning section" %
                        (rules_rel, phase))
                    continue
                for sec in phase_owners:
                    dest = "sections/%s/rules.json" % sec
                    out = target(dest, {"schema_version": 1, "phases": {}})
                    out.setdefault("schema_version", 1)
                    out.setdefault("phases", {})[phase] = copy.deepcopy(
                        live_phases.get(phase) or {})
                    plan["entries"].append({
                        "source": rules_rel,
                        "delta": "/phases/%s" % phase,
                        "destination": dest + ":/phases/%s" % phase})
            old_globals = (rules_seed or {}).get("global_app_rules") or []
            new_globals = rules_doc.get("global_app_rules") or []
            for rule in new_globals:
                if rule in old_globals:
                    continue
                if _is_ios_rule(rule):
                    dest = "sections/build/target_policy.json"
                    policy = target(dest, {"schema_version": 1,
                                           "targets": {"ios": {}}})
                    ios = policy.setdefault("targets", {}).setdefault("ios", {})
                    app_rules = ios.setdefault("app_rules", [])
                    if rule not in app_rules:
                        app_rules.append(rule)
                    destination = dest + ":/targets/ios/app_rules"
                else:
                    destination = "phase_rules.json:/global_app_rules (left fleet-level)"
                plan["entries"].append({"source": rules_rel,
                                        "delta": "/global_app_rules + %r" % rule,
                                        "destination": destination})
            for rule in old_globals:
                if rule in new_globals:
                    continue
                if _is_ios_rule(rule):
                    dest = "sections/build/target_policy.json"
                    policy = target(dest, {"schema_version": 1,
                                           "targets": {"ios": {}}})
                    ios = policy.setdefault("targets", {}).setdefault("ios", {})
                    app_rules = ios.setdefault("app_rules", [])
                    if rule in app_rules:
                        app_rules.remove(rule)
                    destination = dest + ":/targets/ios/app_rules"
                else:
                    destination = "phase_rules.json:/global_app_rules (removed fleet-level)"
                plan["entries"].append({"source": rules_rel,
                                        "delta": "/global_app_rules - %r" % rule,
                                        "destination": destination})

    routing_rel = "model_routing.json"
    routing_doc = source_docs.get(routing_rel)
    routing_seed = baselines.get(routing_rel)
    if routing_doc is not None:
        plan["unmapped"].extend(_unknown_routing_keys(routing_rel, routing_doc))
        deltas = _delta_paths(routing_seed, routing_doc) \
            if routing_seed is not None else \
            ([] if SEED_HASHES.get(routing_rel) == source_hashes.get(routing_rel)
             else ["/ (complete document; pristine baseline unavailable)"])
        if not deltas:
            plan["seed_identical"].append(routing_rel)
        else:
            plan["backups"].append(routing_rel)
            if (routing_seed or {}).get("schema_version", 1) != \
                    routing_doc.get("schema_version", 1):
                plan["unmapped"].append(
                    "%s: /schema_version changed" % routing_rel)
            seed_phases = (routing_seed or {}).get("phases") or {}
            live_phases = routing_doc.get("phases") or {}
            for phase, entry in sorted(live_phases.items()):
                if seed_phases.get(phase) == entry:
                    continue
                phase_owners = sorted(owners.get(phase) or [])
                if not phase_owners:
                    plan["unmapped"].append(
                        "%s: /phases/%s has no owning section" %
                        (routing_rel, phase))
                    continue
                for sec in phase_owners:
                    dest = "sections/%s/model_routing.json" % sec
                    out = target(dest, {"schema_version": 1, "phases": {}})
                    out.setdefault("schema_version", 1)
                    out.setdefault("phases", {})[phase] = copy.deepcopy(entry)
                    plan["entries"].append({
                        "source": routing_rel, "delta": "/phases/%s" % phase,
                        "destination": dest + ":/phases/%s" % phase})
            for phase in sorted(set(seed_phases) - set(live_phases)):
                plan["entries"].append({
                    "source": routing_rel,
                    "delta": "/phases/%s (removed)" % phase,
                    "destination": routing_rel + ":/phases/%s "
                                   "(removal remains fleet-level)" % phase})
            for key in ("enabled", "fallback"):
                if (routing_seed or {}).get(key) != routing_doc.get(key):
                    plan["entries"].append({
                        "source": routing_rel, "delta": "/" + key,
                        "destination": routing_rel + ":/" + key
                                       + " (left fleet-level)"})
            for key in ("_docs", "_examples"):
                if (routing_seed or {}).get(key) != routing_doc.get(key):
                    plan["entries"].append({
                        "source": routing_rel, "delta": "/" + key,
                        "destination": routing_rel + ":/" + key
                                       + " (documentation left fleet-level)"})

    plan["backups"] = sorted(set(plan["backups"] + invalid_rels))
    plan["unmapped"] = sorted(set(plan["unmapped"]))
    return plan


def render_plan(plan, applying=False):
    if plan.get("already_migrated"):
        return "V3 config migration: already migrated (receipt hashes verified)\n"
    lines = ["V3 config migration %s" %
             ("APPLY PLAN — no files changed yet" if applying else
              "DRY-RUN — no files changed")]
    for rel in plan.get("seed_identical", []):
        lines.append("= %s: no tuning, section seed wins" % rel)
    for rel in plan.get("defaults", []):
        lines.append("= %s: missing, existing defaults/section seed remain" % rel)
    for entry in plan.get("entries", []):
        lines.append("~ %s%s -> %s" %
                     (entry["source"], entry["delta"], entry["destination"]))
    for overlay in plan.get("overlays", []):
        lines.append("= %s: per-app overlay detected; left in place" % overlay)
    for rel, value in sorted(plan.get("targets", {}).items()):
        path = os.path.join(plan["workspace"], rel)
        try:
            with open(path, encoding="utf-8") as fh:
                before = fh.read().splitlines(True)
        except OSError:
            before = []
        after = _json_text(value).splitlines(True)
        lines.extend(line.rstrip("\n") for line in difflib.unified_diff(
            before, after, fromfile=rel + " (before)",
            tofile=rel + " (after)"))
    if plan.get("unmapped"):
        lines.append("UNMAPPED (blocks --apply unless --allow-unmapped):")
        lines.extend("! " + item for item in plan["unmapped"])
    lines.append("summary: %d tuned delta(s), %d target file(s), %d unmapped" %
                 (len(plan.get("entries", [])), len(plan.get("targets", {})),
                  len(plan.get("unmapped", []))))
    return "\n".join(lines) + "\n"


def _atomic_json(path, value):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".migrate-v3-", suffix=".tmp",
                               dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(_json_text(value))
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
        dfd = os.open(os.path.dirname(path), os.O_RDONLY)
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


def _backup(path):
    backup = path + ".v2.bak"
    if os.path.exists(backup):
        return backup
    fd, tmp = tempfile.mkstemp(prefix=".v2-backup-", suffix=".tmp",
                               dir=os.path.dirname(path))
    os.close(fd)
    try:
        shutil.copy2(path, tmp)
        os.replace(tmp, backup)
    finally:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass
    return backup


def apply_plan(plan, allow_unmapped=False, after_target=None):
    if plan.get("already_migrated"):
        return plan.get("receipt")
    if plan.get("unmapped") and not allow_unmapped:
        raise MigrationError("unmapped data blocks --apply; review the report "
                             "or pass --allow-unmapped")
    workspace = plan["workspace"]
    backup_manifest = {}
    for rel in plan.get("backups", []):
        src = os.path.join(workspace, rel)
        backup_manifest[rel] = os.path.relpath(_backup(src), workspace)
    written = []
    for rel, value in sorted(plan.get("targets", {}).items()):
        _atomic_json(os.path.join(workspace, rel), value)
        written.append(rel)
        if after_target is not None:
            after_target(rel)
    # Positive validation at the actual loaders, not JSON parse alone.
    section_ids = sorted({rel.split("/")[1] for rel in written
                          if rel.startswith("sections/")
                          and rel.count("/") >= 2})
    for section_id in section_ids:
        sections.load_section(section_id, workspace)
        errors = [entry for entry in sections.lint_section(
            section_id, workspace) if entry.get("severity") == "error"]
        if errors:
            raise MigrationError("migrated section %r failed lint: %s" %
                                 (section_id, errors[0].get("message")))
    receipt = {
        "schema_version": 1, "status": "complete",
        "source_hashes": dict(plan.get("source_hashes") or {}),
        "target_hashes": {rel: _file_hash(os.path.join(workspace, rel))
                          for rel in written},
        "backups": backup_manifest,
        "moved": copy.deepcopy(plan.get("entries") or []),
        "unmapped_allowed": list(plan.get("unmapped") or [])
                            if allow_unmapped else [],
        "per_app_overlays_left_in_place": list(plan.get("overlays") or []),
    }
    _atomic_json(os.path.join(workspace, RECEIPT), receipt)
    return receipt


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--workspace", default=HERE)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--allow-unmapped", action="store_true")
    args = parser.parse_args(argv)
    plan = build_plan(args.workspace)
    sys.stdout.write(render_plan(plan, applying=args.apply))
    if not args.apply:
        return 0
    try:
        apply_plan(plan, allow_unmapped=args.allow_unmapped)
    except MigrationError as exc:
        print("APPLY REFUSED: %s" % exc, file=sys.stderr)
        return 2
    print("APPLIED: %d target file(s); originals preserved as .v2.bak" %
          len(plan.get("targets", {})))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
