#!/usr/bin/env python3
"""
Local model manager (V2 spec §12) — Ollama first.

One small, dependency-free module for everything local-model:

* the curated model registry (local_models.json, spec §12.3) — load it without
  ever raising, so a missing/corrupt registry degrades to "no recommendations";
* detection — is the `ollama` server actually reachable on loopback, and which
  model tags are already pulled (`ollama list`, parsed best-effort);
* a machine-readable report merged into `--doctor --json` (spec §27) and
  consumed by the GUI's Local Models settings.

Probes are best-effort and never raise. Lifecycle mutations are confined to
Ollama's loopback API and a flocked runtime ledger; tests inject fake runners /
openers so the suite passes with or without Ollama installed.
"""

import errno
import fcntl
import json
import os
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import procutil

REGISTRY_FILENAME = "local_models.json"
REGISTRY_PUBLIC_FIELDS = (
    "id", "label", "installed", "license", "license_url", "commercial_use",
    "min_ram_gb", "recommended_ram_gb", "size_gb", "context_tokens", "roles",
    "notes",
)

# Ollama's loopback-only HTTP API. A cheap GET here is the single reliable
# "is the server running?" probe (`ollama` the binary existing says nothing
# about the server; `ollama ps` spins up a client that can hang on a wedged
# daemon — the HTTP probe has a hard timeout).
OLLAMA_TAGS_URL = "http://127.0.0.1:11434/api/tags"
OLLAMA_PS_URL = "http://127.0.0.1:11434/api/ps"
OLLAMA_GENERATE_URL = "http://127.0.0.1:11434/api/generate"
LOADED_MODELS_FILENAME = "loaded_models.json"
LOADED_MODELS_LOCK_FILENAME = "loaded_models.lock"
DEFAULT_UNKNOWN_SIZE_GB = 8.0


_STRING_FIELDS = ("label", "runtime", "license", "license_url", "notes")
_NUMERIC_FIELDS = ("min_ram_gb", "recommended_ram_gb", "size_gb", "context_tokens")


def _entry_is_well_formed(m):
    """id-presence was the only check; a GUI/hand edit can still ship a
    numeric label, a roles STRING instead of a list, or a non-numeric RAM
    figure that later breaks a doctor/GUI consumer expecting the documented
    shape. Reject those here instead of letting them ship silently."""
    if not isinstance(m, dict) or not str(m.get("id", "")).strip():
        return False
    for field in _STRING_FIELDS:
        if field in m and not isinstance(m[field], str):
            return False
    for field in _NUMERIC_FIELDS:
        if field in m and not isinstance(m[field], (int, float)):
            return False
    if "roles" in m and not isinstance(m["roles"], list):
        return False
    if "commercial_use" in m and not isinstance(m["commercial_use"], bool):
        return False
    return True


def load_registry(here):
    """The curated local-model registry (spec §12.3) as a dict. A missing,
    unreadable, or malformed file NEVER raises — it yields an empty registry
    (schema_version 1, no models) so callers need no special-casing."""
    empty = {"schema_version": 1, "models": []}
    path = os.path.join(here, REGISTRY_FILENAME)
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return empty
    if not isinstance(data, dict) or not isinstance(data.get("models"), list):
        return empty
    # Keep only well-formed entries: an id is required, and any of the
    # documented fields present must be the right basic type.
    data["models"] = [m for m in data["models"] if _entry_is_well_formed(m)]
    return data


_TOTAL_RAM_GB = None
_TOTAL_RAM_LOCK = threading.Lock()


def total_ram_gb():
    """Physical RAM in whole GB (0 when undetectable). Cached per process —
    used by the engine's local-model RAM gate so a roster entry that can't
    possibly fit is benched instead of swapping the machine to a crawl."""
    global _TOTAL_RAM_GB
    with _TOTAL_RAM_LOCK:
        if _TOTAL_RAM_GB is not None:
            return _TOTAL_RAM_GB
        gb = 0
        try:  # macOS
            out = subprocess.run(["sysctl", "-n", "hw.memsize"],
                                 capture_output=True, text=True, timeout=5)
            gb = int(int(out.stdout.strip()) // (1024 ** 3))
        except (OSError, ValueError, subprocess.SubprocessError):
            try:  # POSIX fallback
                gb = int(os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
                         // (1024 ** 3))
            except (OSError, ValueError, AttributeError):
                gb = 0
        _TOTAL_RAM_GB = max(0, gb)
        return _TOTAL_RAM_GB


def parse_ollama_list(text):
    """Model tags from `ollama list` stdout. The output is a padded table

        NAME                ID              SIZE      MODIFIED
        llama3.1:8b         42182419e950    4.7 GB    4 weeks ago

    so the tag is the first whitespace-delimited token of each non-header row.
    Pure function (easy to test); tolerates empty/garbage input."""
    models = []
    for line in (text or "").splitlines():
        parts = line.split()
        if not parts:
            continue
        if parts[0].upper() == "NAME":   # header row
            continue
        models.append(parts[0])
    return models


def installed_models(run=None):
    """Locally pulled Ollama model tags via `ollama list`. Best-effort: a
    missing binary, dead server, timeout, or weird output just returns [] —
    this must never take down a doctor/preflight pass. ``run`` is injectable
    for tests (defaults to the hardened procutil runner)."""
    run = run or procutil.run_capture
    try:
        out, _err, code = run(["ollama", "list"], timeout=10)
    except (OSError, subprocess.SubprocessError):
        return []
    if code != 0:
        return []
    return parse_ollama_list(out)


_INSTALLED_CACHE = {"ts": 0.0, "models": []}
_CACHE_LOCK = threading.Lock()


def installed_models_cached(ttl=60, run=None):
    """installed_models() behind a short module-level TTL cache. The cloud->
    local fallback consults this on the failure hot path — it must not pay a
    10s `ollama list` subprocess on every rescued turn."""
    now = time.time()
    # Single-flight: hold the lock across the refresh so concurrent rescued
    # turns don't each spawn the 10s `ollama list`. A second caller that was
    # blocked here re-checks the TTL and returns the just-refreshed cache
    # instead of running its own subprocess.
    with _CACHE_LOCK:
        if now - _INSTALLED_CACHE["ts"] < ttl:
            return list(_INSTALLED_CACHE["models"])
        models = installed_models(run=run)
        _INSTALLED_CACHE.update(ts=time.time(), models=list(models))
        return list(models)


# Hugging Face model search, restricted to GGUF repos — exactly the set Ollama
# can pull directly with `ollama pull hf.co/<org>/<repo>` (no conversion step).
HF_SEARCH_URL = ("https://huggingface.co/api/models?search=%s&filter=gguf"
                 "&sort=downloads&direction=-1&limit=%d")


def _fetch_json(url, timeout=10):
    req = urllib.request.Request(url, headers={"User-Agent": "orchestrator-local-models"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def search_remote(query, limit=15, fetch=None, here=None):
    """Search installable open-source models for the Model Library.

    Two sources, merged: the curated registry (offline, license-vetted) and
    live Hugging Face GGUF repos, each pullable as `ollama pull hf.co/<repo>`.
    Gated repos are dropped — an anonymous pull cannot fetch them. Best-effort:
    offline just means curated-only results plus a note. Never raises."""
    query = str(query or "").strip()
    fetch = fetch or _fetch_json
    here = here or os.path.dirname(os.path.abspath(__file__))
    try:
        limit = max(1, min(int(limit), 50))
    except (TypeError, ValueError):
        limit = 15
    installed = set(installed_models_cached())
    results, note = [], ""
    # Curated matches: every query token must appear somewhere in the entry
    # ("qwen coder" matches qwen3-coder:30b even though the words aren't adjacent).
    tokens = query.lower().split()
    for m in load_registry(here).get("models", []):
        hay = " ".join([m["id"], str(m.get("label", "")), str(m.get("notes", "")),
                        " ".join(m.get("roles") or [])]).lower()
        if tokens and not all(t in hay for t in tokens):
            continue
        results.append({"id": m["id"], "label": str(m.get("label", "")),
                        "source": "curated", "license": str(m.get("license", "")),
                        "size_gb": m.get("size_gb"), "downloads": None, "likes": None,
                        "installed": m["id"] in installed})
    if query:
        try:
            hits = fetch(HF_SEARCH_URL % (urllib.parse.quote(query), limit))
            for h in hits if isinstance(hits, list) else []:
                repo = str(h.get("id") or h.get("modelId") or "").strip()
                if not repo or "/" not in repo or h.get("gated"):
                    continue
                tag = "hf.co/%s" % repo
                lic = ""
                for t in h.get("tags") or []:
                    if isinstance(t, str) and t.startswith("license:"):
                        lic = t.split(":", 1)[1]
                        break
                results.append({"id": tag, "label": repo.split("/")[-1],
                                "source": "huggingface", "license": lic,
                                "size_gb": None, "downloads": h.get("downloads"),
                                "likes": h.get("likes"),
                                "installed": tag in installed
                                or "%s:latest" % tag in installed})
        except Exception as exc:  # noqa: BLE001 — offline degrades to curated-only
            note = ("Hugging Face search unavailable (%s) — showing the curated "
                    "registry only." % exc)
    return {"query": query, "results": results[:limit], "note": note}


def server_running(timeout=3):
    """True if a local Ollama server is reachable on loopback (spec §12.1).
    HTTP probe with a short hard timeout; never raises."""
    try:
        req = urllib.request.Request(OLLAMA_TAGS_URL, method="GET")
        with urllib.request.urlopen(req, timeout=timeout):
            return True
    except Exception:   # noqa: BLE001 - any failure just means "not running"
        return False


def loaded_models_ps(timeout=3, opener=None, on_warn=None):
    """Return Ollama's live resident models in a stable, small shape.

    ``/api/ps`` is ground truth for residency. The probe never raises; a dead
    or malformed endpoint yields ``[]`` and a specific warning when supplied.
    ``opener`` is injectable for deterministic tests.
    """
    warn = on_warn or (lambda _message: None)
    opener = opener or urllib.request.urlopen
    try:
        req = urllib.request.Request(OLLAMA_PS_URL, method="GET")
        with opener(req, timeout=timeout) as resp:
            raw = json.loads(resp.read().decode("utf-8"))
        models = raw.get("models") if isinstance(raw, dict) else None
        if not isinstance(models, list):
            raise ValueError("response has no models list")
        result = []
        for item in models:
            if not isinstance(item, dict):
                continue
            model = str(item.get("name") or item.get("model") or "").strip()
            if not model:
                continue
            try:
                size = max(0, int(item.get("size") or item.get("size_bytes") or 0))
            except (TypeError, ValueError):
                size = 0
            result.append({"model": model, "size_bytes": size,
                           "expires_at": item.get("expires_at")})
        return result
    except Exception as exc:  # noqa: BLE001 - probe degradation is non-fatal
        warn("Ollama /api/ps unavailable (%s); continuing without eviction" % exc)
        return []


def unload_model(model, timeout=10, opener=None, on_warn=None):
    """Ask Ollama to unload ``model`` immediately. Never raises."""
    warn = on_warn or (lambda _message: None)
    opener = opener or urllib.request.urlopen
    payload = json.dumps({"model": str(model), "prompt": "", "stream": False,
                          "keep_alive": 0}).encode("utf-8")
    try:
        req = urllib.request.Request(
            OLLAMA_GENERATE_URL, data=payload,
            headers={"Content-Type": "application/json"}, method="POST")
        with opener(req, timeout=timeout) as resp:
            resp.read()
        return True
    except Exception as exc:  # noqa: BLE001 - current generation must proceed
        warn("Could not unload local model %s (%s); continuing over budget"
             % (model, exc))
        return False


def _runtime_paths(here):
    runtime = os.path.join(here, "runtime")
    return (runtime, os.path.join(runtime, LOADED_MODELS_FILENAME),
            os.path.join(runtime, LOADED_MODELS_LOCK_FILENAME))


def _lock_file(lock_path, timeout, on_warn):
    """Acquire an exclusive flock by a deadline, returning an open file."""
    try:
        fh = open(lock_path, "a+", encoding="utf-8")
    except OSError as exc:
        on_warn("Local-model ledger lock could not open (%s); continuing unlocked"
                % exc)
        return None
    deadline = time.monotonic() + max(0.0, float(timeout))
    while True:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fh
        except OSError as exc:
            if exc.errno not in (errno.EACCES, errno.EAGAIN):
                fh.close()
                on_warn("Local-model ledger lock failed (%s); continuing unlocked"
                        % exc)
                return None
            if time.monotonic() >= deadline:
                fh.close()
                on_warn("Local-model ledger lock timed out; continuing without "
                        "lifecycle bookkeeping")
                return None
            time.sleep(0.01)


def _read_ledger_unlocked(path, on_warn):
    if not os.path.exists(path):
        return {"schema_version": 1, "models": {}}
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict) or not isinstance(data.get("models"), dict):
            raise ValueError("expected an object with a models object")
        for name, entry in data["models"].items():
            if not isinstance(name, str) or not isinstance(entry, dict):
                raise ValueError("models entries must be objects keyed by tag")
            int(entry.get("size_bytes") or 0)
            float(entry.get("last_used") or 0)
        reservations = data.get("reservations", {})
        if not isinstance(reservations, dict):
            raise ValueError("reservations must be an object")
        for name, entry in reservations.items():
            if not isinstance(name, str) or not isinstance(entry, dict):
                raise ValueError("reservation entries must be objects keyed by tag")
            int(entry.get("size_bytes") or 0)
            float(entry.get("created_at") or 0)
        return data
    except (OSError, ValueError) as exc:
        on_warn("Local-model ledger is corrupt (%s); rebuilding from Ollama /api/ps"
                % exc)
        return {"schema_version": 1, "models": {}}


def _write_ledger_unlocked(path, data):
    tmp = "%s.tmp.%d" % (path, os.getpid())
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, sort_keys=True)
        fh.write("\n")
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def _ledger_update(here, update, on_warn=None, lock_timeout=2.0):
    warn = on_warn or (lambda _message: None)
    runtime, path, lock_path = _runtime_paths(here)
    try:
        os.makedirs(runtime, exist_ok=True)
    except OSError as exc:
        warn("Local-model runtime directory unavailable (%s); continuing without "
             "lifecycle bookkeeping" % exc)
        return None
    lock = _lock_file(lock_path, lock_timeout, warn)
    if lock is None:
        return None
    try:
        data = _read_ledger_unlocked(path, warn)
        result = update(data)
        _write_ledger_unlocked(path, data)
        return result
    except Exception as exc:  # noqa: BLE001 - bookkeeping cannot fail a turn
        warn("Local-model lifecycle bookkeeping failed (%s); continuing generation"
             % exc)
        return None
    finally:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        finally:
            lock.close()


def read_loaded_ledger(here, on_warn=None):
    """Return a snapshot of the flock-protected lifecycle ledger."""
    holder = {}

    def capture(data):
        holder.update(json.loads(json.dumps(data)))
    result = _ledger_update(here, capture, on_warn=on_warn)
    return holder if result is not None or holder else {"schema_version": 1,
                                                        "models": {}}


def _registry_size_bytes(model, here):
    for item in load_registry(here).get("models", []):
        if item.get("id") == model and item.get("size_gb"):
            return int(float(item["size_gb"]) * (1024 ** 3))
    return 0


def default_memory_budget_gb():
    """A conservative automatic budget: 60% of RAM, or 8GB if unknown."""
    total = total_ram_gb()
    return max(1.0, total * 0.60) if total else DEFAULT_UNKNOWN_SIZE_GB


def ensure_capacity(model, budget_gb, pins=(), on_warn=None, here=None,
                    ps_probe=None, unload=None, lock_timeout=2.0, now=None):
    """Evict LRU non-pinned residents until ``model`` fits the memory budget.

    Decisions are serialized with the ledger flock. Failure to probe, lock, or
    unload is visible but never prevents the caller's current generation.
    Returns the model ids successfully evicted, in order.
    """
    here = here or os.path.dirname(os.path.abspath(__file__))
    warn = on_warn or (lambda _message: None)
    ps_probe = ps_probe or (lambda: loaded_models_ps(on_warn=warn))
    unload = unload or (lambda victim: unload_model(victim, on_warn=warn))
    pinset = set(str(p) for p in pins)
    try:
        budget_bytes = int(float(budget_gb) * (1024 ** 3))
    except (TypeError, ValueError):
        budget_bytes = int(default_memory_budget_gb() * (1024 ** 3))
        warn("Invalid local-model memory budget %r; using %.1fGB"
             % (budget_gb, budget_bytes / float(1024 ** 3)))
    evicted = []

    def plan_and_apply(data):
        # Probe under the same flock that protects the decision and incoming
        # reservation. Two processes therefore cannot both plan from one
        # pre-eviction snapshot and over-admit concurrent loads.
        try:
            resident = list(ps_probe())
        except Exception as exc:
            warn("Ollama /api/ps unavailable (%s); continuing without eviction"
                 % exc)
            resident = []
        history = data.setdefault("models", {})
        # /api/ps is residency truth: drop stale history from the working
        # ledger and rebuild live entries while retaining last_used.
        live = {}
        for item in resident:
            name = item["model"]
            previous = history.get(name) if isinstance(history.get(name), dict) else {}
            live[name] = {"size_bytes": int(item.get("size_bytes") or 0),
                          "expires_at": item.get("expires_at"),
                          "last_used": float(previous.get("last_used") or 0),
                          "pinned": name in pinset}
        history.clear()
        history.update(live)
        timestamp = float(time.time() if now is None else now)
        reservations = data.setdefault("reservations", {})
        # A killed process must not reserve memory forever. Reservations are
        # planning state, not residency claims; /api/ps remains truth for the
        # models mapping above.
        reservations = {
            name: value for name, value in reservations.items()
            if isinstance(value, dict)
            and timestamp - float(value.get("created_at") or 0) < 120
        }
        data["reservations"] = reservations
        incoming_size = _registry_size_bytes(model, here)
        if not incoming_size:
            incoming_size = live.get(model, {}).get("size_bytes", 0)
        if not incoming_size:
            incoming_size = int(DEFAULT_UNKNOWN_SIZE_GB * (1024 ** 3))
            warn("Unknown size for local model %s; reserving conservative %.1fGB"
                 % (model, DEFAULT_UNKNOWN_SIZE_GB))
        used = sum(v["size_bytes"] for v in live.values())
        used += sum(int(v.get("size_bytes") or 0)
                    for name, v in reservations.items() if name != model)
        required = 0 if model in live else incoming_size
        candidates = sorted(
            (name for name in live if name not in pinset and name != model),
            key=lambda name: (live[name]["last_used"], name))
        while used + required > budget_bytes and candidates:
            victim = candidates.pop(0)
            try:
                unloaded = bool(unload(victim))
            except Exception as exc:  # noqa: BLE001 - injected/alternate unloaders
                unloaded = False
                warn("Could not unload local model %s (%s); continuing over budget"
                     % (victim, exc))
            if unloaded:
                used -= live[victim]["size_bytes"]
                history.pop(victim, None)
                evicted.append(victim)
                warn("Evicted local model %s for %s under %.1fGB budget"
                     % (victim, model, budget_bytes / float(1024 ** 3)))
            else:
                # Do not spin forever retrying a daemon that refused unload.
                break
        if used + required > budget_bytes:
            warn("Local model %s will run over %.1fGB budget: remaining models "
                 "are pinned or could not be unloaded" %
                 (model, budget_bytes / float(1024 ** 3)))
        if model not in live:
            reservations[model] = {"size_bytes": incoming_size,
                                   "created_at": timestamp,
                                   "pinned": model in pinset}
        return list(evicted)

    result = _ledger_update(here, plan_and_apply, on_warn=warn,
                            lock_timeout=lock_timeout)
    return result or []


def mark_model_used(model, here=None, pins=(), size_bytes=0, on_warn=None,
                    lock_timeout=2.0, now=None):
    """Flocked last-used update; concurrent processes cannot lose entries."""
    here = here or os.path.dirname(os.path.abspath(__file__))
    timestamp = float(time.time() if now is None else now)

    def mark(data):
        data.setdefault("reservations", {}).pop(model, None)
        models = data.setdefault("models", {})
        old = models.get(model) if isinstance(models.get(model), dict) else {}
        models[model] = {"size_bytes": int(size_bytes or old.get("size_bytes") or
                                           _registry_size_bytes(model, here)),
                         "expires_at": old.get("expires_at"),
                         "last_used": timestamp,
                         "pinned": model in set(pins)}
        return True
    return bool(_ledger_update(here, mark, on_warn=on_warn,
                               lock_timeout=lock_timeout))


def release_model_reservation(model, here=None, on_warn=None,
                              lock_timeout=2.0):
    """Drop a before-load reservation after a failed generate request."""
    here = here or os.path.dirname(os.path.abspath(__file__))

    def release(data):
        data.setdefault("reservations", {}).pop(model, None)
        return True
    return bool(_ledger_update(here, release, on_warn=on_warn,
                               lock_timeout=lock_timeout))


def report(here, selected):
    """The `local_models` block of the doctor/preflight JSON (spec §27):
    server reachability, the configured model (models.ollama) and whether it's
    actually pulled, plus the curated registry with per-model installed flags."""
    selected = str(selected or "").strip()
    # Cached (like search_remote): --doctor/GUI polls hit this repeatedly and
    # must not re-pay the full `ollama list` subprocess on every call.
    installed = set(installed_models_cached())
    registry = []
    for m in load_registry(here).get("models", []):
        entry = {k: m.get(k) for k in REGISTRY_PUBLIC_FIELDS if k in m}
        entry["id"] = m["id"]
        entry["label"] = str(m.get("label", ""))
        entry["installed"] = m["id"] in installed
        registry.append(entry)
    return {
        "server_running": server_running(),
        "selected": selected,
        "selected_installed": bool(selected) and selected in installed,
        "registry": registry,
    }
