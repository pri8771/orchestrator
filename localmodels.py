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

Everything here is best-effort and side-effect free: probes never raise, they
just answer False/empty. Tests inject fake runners / monkeypatch the probes so
the suite passes with or without Ollama installed.
"""

import json
import os
import subprocess
import threading
import time
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
