"""Recorded artifact-stream driver for the real Conductor.

The harness owns only fixture materialization and result normalization.  Route
planning, guards, permissions, termination, ledger persistence, recovery, and
delegation minting all run through their production implementations.
"""
import contextlib
import hashlib
import json
import os
import shutil
import tempfile

import conductor
import events


ROUTE_DECISIONS = {
    "route_proposed", "route_approved", "route_recovered", "mint_failed",
    "converged", "budget_exhausted", "unroutable", "denied",
    "approval_requested", "enqueue_failed", "route_deferred",
}
TERMINAL_DECISIONS = {"goal_met", "converged_open_items", "stalled",
                      "budget_exhausted"}


def _read_json(path, default):
    try:
        with open(path, encoding="utf-8") as fh:
            value = json.load(fh)
    except (OSError, ValueError):
        return default
    return value


def _write_json(path, value):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(value, fh, indent=1, sort_keys=True)
        fh.write("\n")


class RecordedEval:
    """One isolated fixture workspace."""

    def __init__(self, fixture_dir, root=None):
        self.fixture_dir = fixture_dir
        self.root = root or tempfile.mkdtemp(prefix="conductor-eval-")
        self._owns_root = root is None
        self.project = "eval"
        self.state = conductor.default_state()
        self.records = []
        with open(os.path.join(fixture_dir, "stream.jsonl"),
                  encoding="utf-8") as fh:
            for line in fh:
                if line.strip():
                    self.records.append(json.loads(line))
        self.routing = _read_json(os.path.join(fixture_dir, "routing.json"), {})
        self.manifest = _read_json(
            os.path.join(fixture_dir, "goal_manifest.json"), {})
        self.answers = _read_json(
            os.path.join(fixture_dir, "classifier_answers.json"), {})
        self.expected = _read_json(
            os.path.join(fixture_dir, "expected.json"), {})

    def close(self):
        if self._owns_root:
            shutil.rmtree(self.root, ignore_errors=True)

    def prepare(self, records=None):
        os.makedirs(os.path.join(self.root, self.project), exist_ok=True)
        open(os.path.join(self.root, self.project, ".orch-sections"),
             "a").close()
        _write_json(os.path.join(self.root, self.project, "routing.json"),
                    self.routing)
        if self.manifest:
            _write_json(os.path.join(self.root, "goal_manifest.json"),
                        self.manifest)
        for record in self.records if records is None else records:
            self.materialize(record)
        return self

    def materialize(self, record):
        sid = record["session"]
        app_dir = os.path.join(self.root, sid)
        os.makedirs(os.path.join(app_dir, "initial_prompt"), exist_ok=True)
        prompt = os.path.join(app_dir, "initial_prompt", "initial_prompt.md")
        if not os.path.exists(prompt):
            with open(prompt, "w", encoding="utf-8") as fh:
                fh.write("recorded conductor evaluation\n")
        _write_json(os.path.join(app_dir, "agent_state.json"),
                    record.get("agent_state", {
                        "current_phase": "recorded", "done": False,
                        "status": "running", "error": None,
                    }))
        for relpath, content in record.get("files", {}).items():
            path = os.path.join(app_dir, relpath)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(content)
        meta = dict(record["artifact"])
        body = record.get("body", "recorded body\n")
        meta.setdefault("content_hash",
                        hashlib.sha256(body.encode("utf-8")).hexdigest())
        meta.setdefault("status", "final")
        meta.setdefault("version", 1)
        meta.setdefault("supersedes", None)
        meta.setdefault("lineage", [])
        meta.setdefault("branch", "")
        meta.setdefault("depth", len(meta["lineage"]))
        meta.setdefault("hop_count", 0)
        meta.setdefault("source", {"section": sid.split("/")[1]})
        artifact_dir = os.path.join(app_dir, "artifacts", meta["id"])
        os.makedirs(artifact_dir, exist_ok=True)
        _write_json(os.path.join(artifact_dir, "meta.json"), meta)
        with open(os.path.join(artifact_dir, "body.md"), "w",
                  encoding="utf-8") as fh:
            fh.write(body)
        events.emit_event(app_dir, "artifact_published",
                          artifact_id=meta["id"], type=meta.get("type"),
                          version=meta["version"], path=artifact_dir)

    @contextlib.contextmanager
    def recorded_classifier(self):
        original = conductor._build_classifier
        answers = dict(self.answers)
        conductor._build_classifier = lambda _root: (
            lambda artifact_type, candidates:
                answers.get(artifact_type)
                if answers.get(artifact_type) in candidates else None)
        try:
            yield
        finally:
            conductor._build_classifier = original

    def poll(self, count=1):
        with self.recorded_classifier():
            for _ in range(count):
                self.state = conductor.full_poll(
                    self.root, self.state, emit=lambda *_a: None,
                    route_engine=conductor.route_engine)
        return self.state

    def replay(self, dial=None):
        self.prepare([])
        if dial is not None:
            self.state["oversight"] = {"dial": dial}
            os.makedirs(conductor.conductor_dir(self.root), exist_ok=True)
            _write_json(os.path.join(conductor.conductor_dir(self.root),
                                     "oversight.json"), {"dial": dial})
        groups = []
        for record in self.records:
            batch = record.get("batch", len(groups))
            if groups and groups[-1][0] == batch:
                groups[-1][1].append(record)
            else:
                groups.append((batch, [record]))
        for _batch, records in groups:
            for record in records:
                self.materialize(record)
            self.poll(1)
        self.poll(int(self.expected.get("extra_polls", 0)))
        return self.result()

    def result(self):
        ledger = [r for r in conductor.read_ledger(self.root)
                  if isinstance(r, dict)]
        decisions = [r["decision"] for r in ledger
                     if r.get("decision") != "observed"]
        routes = []
        for rec in ledger:
            if not rec.get("route_id"):
                continue
            detail = rec.get("detail") if isinstance(rec.get("detail"), dict) \
                else {}
            routes.append({"decision": rec.get("decision"),
                           "artifact_id": detail.get("artifact_id"),
                           "target": detail.get("target")})
        terminal = {}
        for sid, value in sorted(self.state.get("terminated", {}).items()):
            terminal[sid] = value.get("reason")
        if self.state.get("halted"):
            terminal["__workspace__"] = self.state["halted"].get("reason")
        delegations = []
        for dirpath, _dirs, files in os.walk(self.root):
            if "delegation.json" in files:
                rec = _read_json(os.path.join(dirpath, "delegation.json"), {})
                request = rec.get("request") if isinstance(rec, dict) else {}
                delegations.append({
                    "session": os.path.relpath(dirpath, self.root),
                    "route_id": request.get("route_id"),
                })
        pending = []
        try:
            import conductor_permissions as permissions
            pending = sorted(a.get("action_id")
                             for a in permissions.read_pending(self.root))
        except Exception:
            pass
        return {"decisions": decisions, "routes": routes,
                "terminal": terminal,
                "delegations": sorted(delegations,
                                      key=lambda x: x["session"]),
                "pending": pending, "ledger": ledger}

    def assert_expected(self, testcase, result=None, expected=None):
        result = result or self.result()
        expected = expected or self.expected
        actual = {"decisions": result["decisions"],
                  "terminal": result["terminal"]}
        wanted = {"decisions": expected.get("decisions", []),
                  "terminal": expected.get("terminal", {})}
        if "routes" in expected:
            actual["routes"] = result["routes"]
            wanted["routes"] = expected["routes"]
        if actual != wanted:
            upto = min(len(actual["decisions"]), len(wanted["decisions"]))
            step = next((i for i in range(upto)
                         if actual["decisions"][i] !=
                         wanted["decisions"][i]), upto)
            testcase.fail(
                "fixture %s step %d: expected %r, actual %r; ledger tail=%s"
                % (os.path.basename(self.fixture_dir), step, wanted, actual,
                   json.dumps(result["ledger"][-5:], sort_keys=True,
                              default=str)))


def canonical_result(result):
    """Byte-stable comparison surface: excludes timestamps and temp paths."""
    stable = {"decisions": result["decisions"],
              "routes": result["routes"],
              "terminal": result["terminal"],
              "pending_count": len(result["pending"]),
              "delegation_route_ids": sorted(
                  d["route_id"] for d in result["delegations"]
                  if d.get("route_id"))}
    return json.dumps(stable, sort_keys=True, separators=(",", ":"))
