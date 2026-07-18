#!/usr/bin/env python3
"""
Autonomous multi-agent orchestrator.

Coordinates three locally-installed, already-logged-in AI CLIs — Codex,
Claude (Claude Code), and Gemini/Antigravity — through a sequence of
Markdown-file discussion phases for each app/project under a root folder.

Design goals (see README.md):
  * No API keys. Uses your normal subscription CLI sessions only.
  * Standard library ONLY (a tiny built-in YAML reader is included).
  * Orchestrator — not the agents — controls turn order.
  * Detailed, substantive, rubric-driven debate that converges to consensus
    or, failing that, a forced weighted vote (no agent votes for itself).
  * Real-time, timestamped, flushed terminal output + structured logs.
  * Robust single-instance file locking with stale-lock detection.

Run:  python3 orchestrator.py [--once] [--watch N] [--app NAME] [--doctor]
"""

import argparse
import concurrent.futures
import datetime as _dt
import hashlib
import itertools
import json
import errno
import fcntl
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid

# Sibling modules (same directory; Python puts the script dir on sys.path[0]).
import unicodedata
import urllib.request
import urllib.error
import urllib.parse

import artifacts as artifactslib
import sessions as seslib
import memory as memlib
import backfill as backfilllib
import events as evlib
import localmodels as lmlib
import modelrouting as mrlib
import roles as roleslib
import sections as seclib
import workflows as wflib
import schemas as schemalib
import resilience as reslib
import docs as docslib
import completeness as complib
import global_resource as grlib
import knowledge as knowlib
import messages as msglib
import mistakes as mistklib
import phase_rules as phaseruleslib
import portfolio as portfoliolib
import postmortem as pmlib
import swiftscaffold as swiftscaffoldlib
import urlfetch as urlfetchlib
import verify as verifylib
import visualqa as vqalib
import uicrawl as uicrawllib
import designlint as dlintlib
import fleetlearn as fllib
import evalharness as evallib
import traces as traceslib
import turncontext as tcxlib
import procutil

HERE = os.path.dirname(os.path.abspath(__file__))
traceslib.on_warn = lambda msg: emit("WARN " + msg)
LOG_DIR = os.path.join(HERE, "logs")
# Per-app locks. Default is engine-local, but main() re-points this at
# <root>/.orch-locks once the workspace root is known: two engine copies (a
# repo checkout and the GUI's Application Support install) must contend for the
# SAME lock when they target the same workspace, or one app can run twice.
LOCKS_DIR = os.path.join(HERE, "locks")
_HELD_LOCKS: "set[str]" = set()

# Phases are now driven by pluggable workflows (see workflows.py). Each app can
# run a different workflow (build an app, answer a question, research a topic,
# productionize a prototype). PHASES stays as a compatibility alias for the
# default app-building pipeline; the engine actually iterates the app's resolved
# workflow's phases. A Phase still unpacks as the legacy (key, folder, file,
# purpose) tuple, so downstream code is unchanged.
PHASES = wflib.load_workflow(wflib.DEFAULT_WORKFLOW).phases

# Canonical agent order for every round. "ollama" (the local model, V2 spec §12)
# is deliberately LAST so a local model can never shadow a cloud agent anywhere
# order implies preference — and it is NOT in COORDINATOR_PREFERENCE at all.
AGENT_ORDER = ["codex", "claude", "gemini", "ollama"]


def _derive_display(agent_id):
    """A readable label for any agent id, including dynamically-registered ones
    like 'local:qwen2.5-coder:7b' (-> 'Qwen2.5 Coder'). V2 spec §4.1 / gap 28:
    no display/signature lookup may KeyError on an unknown identity."""
    s = str(agent_id or "")
    base = s.split(":", 1)[1] if s.startswith("local:") else s
    base = base.split(":")[0]  # drop a size tag like ':7b'
    label = base.replace("-", " ").replace("_", " ").strip().title()
    return ("%s (local)" % label) if s.startswith("local:") else (label or s)


def _split_local_roster(value):
    """Parse legacy-friendly local model rosters into a clean ordered list.

    The YAML parser in this repo handles only nested maps and scalars, so this
    accepts either:

    - a Python list (from config.json): ["qwen3-coder:30b", "glm5.2:latest"]
    - a delimiter-separated string: "qwen3-coder:30b, glm5.2:latest"

    Returns a unique, trimmed list while preserving first-seen order.
    """
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        raw_items = list(value)
    else:
        text = str(value)
        if not text.strip():
            return []
        raw_items = re.split(r"[;,]", text)
    out = []
    seen = set()
    for item in raw_items:
        model = str(item).strip()
        if not model or model in seen:
            continue
        seen.add(model)
        out.append(model)
    return out


class _AgentLabelMap(dict):
    """dict that derives a label for an unknown agent id instead of raising —
    so every existing DISPLAY[agent] call site is safe for dynamic identities."""
    def __missing__(self, key):
        return _derive_display(key)


class _AgentSigMap(dict):
    def __missing__(self, key):
        return "From %s" % _derive_display(key)


SIGNATURE = _AgentSigMap({"codex": "From Codex", "claude": "From Claude",
                          "gemini": "From Gemini", "ollama": "From Local (Ollama)"})
DISPLAY = _AgentLabelMap({"codex": "Codex", "claude": "Claude", "gemini": "Gemini",
                          "ollama": "Local (Ollama)"})


def ordered_agents(active):
    """Stable speaking order for a set of active agents: the canonical trio first
    (in AGENT_ORDER), then any additional dynamic identities (e.g. local models)
    in sorted order — so an enabled agent is NEVER silently dropped just because
    it isn't in the hardcoded AGENT_ORDER (V2 spec §4.1 / gap 28 acceptance (a))."""
    known = [a for a in AGENT_ORDER if a in active]
    extra = sorted(a for a in active if a not in AGENT_ORDER)
    return known + extra


class AgentError(Exception):
    """Raised when an agent CLI cannot produce a usable response."""


class AppError(Exception):
    """Raised to abort processing a single app (other apps continue)."""


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------
def now_str():
    return _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# Quiet mode: suppress the terminal echo (the log file is always written).
# Set by ORCH_QUIET=1 (the test suite) or runtime.stream_terminal_output=false.
_QUIET = False


def _quiet():
    return _QUIET or os.environ.get("ORCH_QUIET", "") == "1"


# Serializes the file appends in emit()/live_log(): both are called from
# parallel discussion/build worker threads, and two threads writing a long
# redacted line to the same file with plain open("a") can interleave and corrupt
# it. Stdout printing stays outside the lock (print is already atomic enough).
_LOG_LOCK = threading.Lock()


def emit(msg):
    """Timestamped, immediately-flushed terminal line + append to text log."""
    line = "[%s] %s" % (now_str(), msg)
    if not _quiet():
        print(line, flush=True)
    try:
        with _LOG_LOCK:
            with open(os.path.join(LOG_DIR, "orchestrator.log"), "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Minimal YAML / JSON config loader (stdlib only). The mini-YAML parser now
# lives in miniyaml.py (extracted to start decomposing this monolith); it's
# re-exported here so existing references (orchestrator.parse_min_yaml, the
# tests) keep working unchanged.
# ---------------------------------------------------------------------------
from miniyaml import parse_min_yaml, coerce_scalar as _coerce_scalar, \
    strip_inline_comment as _strip_inline_comment  # noqa: E402,F401


def load_config():
    """Load config.json (if present) else config.yaml via the built-in mini-YAML
    reader. Platform: like resolve_root below, path handling here assumes POSIX
    (os.path.join / os.path.exists on forward-slash paths) — untested on Windows."""
    json_path = os.path.join(HERE, "config.json")
    yaml_path = os.path.join(HERE, "config.yaml")
    if os.path.exists(json_path):
        with open(json_path, encoding="utf-8") as fh:
            return json.load(fh)
    if os.path.exists(yaml_path):
        with open(yaml_path, encoding="utf-8") as fh:
            return parse_min_yaml(fh.read())
    raise SystemExit("No config.yaml or config.json found in %s" % HERE)


def resolve_root(cfg, cli_root=None):
    """Resolve the workspace root. Precedence: --root CLI flag > ORCH_ROOT env >
    config root. `~` is expanded, and a relative path is resolved against this
    repo (the parent of the engine dir) — so the shipped default,
    `~/Documents/iOS-App-Factory`, is portable across machines even though it
    isn't repo-relative: it depends only on the current user's home directory,
    never a hardcoded absolute path baked in for one specific machine/user
    (V2 spec §27).

    Platform: POSIX paths only, same as procutil (the engine targets macOS/
    Linux). ``os.path.expanduser``'s `~` expansion and the forward-slash join
    below assume a POSIX-shaped HOME; on Windows this needs `~` -> USERPROFILE
    handling and drive-letter-aware path joining, neither of which is
    implemented here."""
    root = cli_root or os.environ.get("ORCH_ROOT") or cfg.get("root") or ""
    root = os.path.expanduser(str(root))
    if root and not os.path.isabs(root):
        root = os.path.join(os.path.dirname(HERE), root)
    return os.path.abspath(root) if root else ""


def cget(cfg, path, default=None):
    cur = cfg
    for part in path.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return default
    return cur


# ---------------------------------------------------------------------------
# Locking
# ---------------------------------------------------------------------------
def _extract_lock_pid(info):
    if not info:
        return None
    m = re.search(r"\bpid=(\d+)\b", str(info))
    return int(m.group(1)) if m else None


def _pid_alive(pid):
    if pid is None:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError as e:
        if e.errno == errno.ESRCH:
            return False
        if e.errno == errno.EPERM:
            return True
        return False


def _parse_state_time(value):
    if not value:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S",
                "%Y-%m-%dT%H:%M:%SZ"):
        try:
            return _dt.datetime.strptime(value, fmt).timestamp()
        except ValueError:
            pass
    return None


def _parse_state_pid(state):
    pid = state.get("runner_pid")
    if pid is None:
        return None
    try:
        pid_i = int(pid)
    except (TypeError, ValueError):
        return None
    return pid_i if pid_i > 0 else None


def _app_lock_has_live_owner(app):
    p = _app_lock_path(app)
    if not os.path.exists(p):
        return False
    try:
        with open(p, encoding="utf-8") as fh:
            info = fh.read().strip()
    except OSError:
        return False
    lock_pid = _extract_lock_pid(info)
    return bool(lock_pid and _pid_alive(lock_pid))


def _is_stale_running_state(app_dir, state, stale_seconds=5400, app=None):
    """Conservatively detect a state that is marked running but no longer active.

    Recovery is intentionally limited: only clear runtime metadata when we can
    say the orchestrator lock/pid is not live. This is to prevent "running"
    states with no process from blocking UI interpretation after crashes or forced
    stops.
    """
    if str(state.get("status") or "").lower() != "running":
        return False
    if state.get("done") or state.get("error") or state.get("awaiting_approval") or \
       state.get("blocked_conflict"):
        return False
    # Nested sessions: basename(app_dir) is just the chat segment — the
    # caller passes the full session id so the REAL encoded lock is
    # consulted (verified failure: the probe read c.lock, not the
    # p%2Fs%2Fc.<hash>.lock a live run held).
    app = app or os.path.basename(app_dir)
    pid = _parse_state_pid(state)
    if pid and _pid_alive(pid):
        # Known live process from state is authoritative enough to trust.
        return False
    if _app_lock_has_live_owner(app):
        # Legacy/older state may not store pid, but lock is still active.
        return False
    last = _parse_state_time(state.get("last_processed") or state.get("last_processed_at"))
    if last is not None:
        if time.time() - last <= stale_seconds:
            return False
    return True


# NOTE: the legacy machine-wide global lock (acquire_lock/release_lock/LOCK_PATH)
# was removed — locking is per-app (<workspace>/.orch-locks/<app>.lock), handled
# by the functions below.


# Per-app locks: different apps run concurrently; one app can't run twice.
def encode_lock_name(session_id):
    """Lock filename stem for a session id (V3 board 3.0).

    Flat names stay RAW — a running flat run's lock must survive the
    layout upgrade byte-identically. Nested ids ("project/section/chat")
    percent-encode every byte outside [A-Za-z0-9_.~-] and append an
    8-hex sha256 suffix. The suffix is what makes collisions impossible
    across namespaces: a legal flat dir can literally be named
    "proj%2Fsection%2Fchat", colliding with the encoded triple — with
    the hash, an accidental collision cannot happen, and a deliberately
    mimicking flat dir yields only a loud lock-DENIAL ("already
    running"), never a shared lock. The GUI must mirror this encoding
    byte-for-byte — tests/fixtures/lock_encoding.json pins both sides
    (the Swift half lands with sub-PR B; until then the GUI is flat-only).
    """
    sid = str(session_id)
    if "/" not in sid:
        return sid
    # Quote the NFC form: APFS coalesces the session DIRS of NFC/NFD
    # spellings, but percent-encoding ASCII-fies the bytes so the fs
    # could never coalesce two differently-normalized lock stems —
    # verified double-run without this normalization.
    quoted = urllib.parse.quote(unicodedata.normalize("NFC", sid), safe="")
    # The digest is computed over the NFC + casefolded id: on a
    # case/normalization-INSENSITIVE filesystem (macOS APFS default) two
    # ids the fs maps to ONE session dir also map to ONE lock file (the
    # quoted parts coalesce at the fs level exactly like flat locks
    # always did; identical digests keep them coalesced). On a
    # case-SENSITIVE volume the quoted parts differ, so genuinely
    # distinct dirs keep distinct locks. Verified: without this, a casing
    # typo in --app let two orchestrators run one nested session.
    # .lower() not .casefold(): Swift has no full case folding, and the
    # two sides MUST agree byte-for-byte (Python str.lower == Swift
    # lowercased() under Unicode default case conversion). Residual: APFS
    # may fold pairs .lower() does not (ss/ß) — accepted and documented.
    canon = unicodedata.normalize("NFC", sid).lower()
    digest = hashlib.sha256(canon.encode("utf-8")).hexdigest()[:8]
    return "%s.%s" % (quoted, digest)


def _app_lock_path(app):
    return os.path.join(LOCKS_DIR, "%s.lock" % encode_lock_name(app))


def _pid_looks_like_orchestrator(pid):
    """Best-effort cmdline check telling a live orchestrator apart from an
    unrelated process that recycled the pid in a leftover lock file. Unknown
    (ps failed) counts as an orchestrator: blocking is safer than stealing."""
    try:
        out = subprocess.run(["ps", "-p", str(pid), "-o", "command="],
                             capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return True
    return "orchestrator" in (out or "").lower()


def acquire_app_lock(app, stale_seconds):
    """Take <LOCKS_DIR>/<app>.lock, or return False if a live run holds it.

    The whole check/reclaim/create sequence runs under an flock'd guard file,
    so two contenders serialize instead of racing (without it, both can judge
    a lock stale and the loser's os.remove deletes the winner's fresh lock).
    Creation is atomic (O_EXCL) so same-second starts can't both conclude "no
    lock". A live owner blocks us while its lock mtime is fresh; the run
    heartbeat (_start_run_heartbeat) keeps a healthy owner fresh indefinitely.
    A live-but-stale owner is only reclaimed when its cmdline shows the pid
    was recycled by an unrelated process — a real orchestrator that merely
    missed heartbeats (e.g. the machine slept) keeps its lock."""
    os.makedirs(LOCKS_DIR, exist_ok=True)
    p = _app_lock_path(app)
    payload = "pid=%d host=%s started=%s\n" % (os.getpid(), socket.gethostname(), now_str())
    guard_fd = os.open(p + ".guard", os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(guard_fd, fcntl.LOCK_EX)
        for _attempt in range(2):
            try:
                fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
            except FileExistsError:
                try:
                    age = time.time() - os.path.getmtime(p)
                    with open(p, encoding="utf-8") as fh:
                        info = fh.read().strip()
                except OSError:
                    # Lock vanished between open and stat — retry the create.
                    continue
                lock_pid = _extract_lock_pid(info)
                alive = _pid_alive(lock_pid)
                if alive and (age <= stale_seconds
                              or _pid_looks_like_orchestrator(lock_pid)):
                    return False
                emit("App '%s': reclaiming %s lock (age %ds): %s"
                     % (app, "recycled-pid" if alive else "orphaned", int(age), info))
                try:
                    os.remove(p)
                except OSError:
                    pass
                continue
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(payload)
            _HELD_LOCKS.add(p)
            return True
        return False
    finally:
        try:
            fcntl.flock(guard_fd, fcntl.LOCK_UN)
        finally:
            os.close(guard_fd)


def _remove_lock_if_owned(path):
    """Remove a lock file only if it still records THIS process's pid — after
    any reclaim the path may already belong to a newer run, and deleting a
    lock we don't own is how two runs end up unguarded."""
    try:
        with open(path, encoding="utf-8") as fh:
            owned = _extract_lock_pid(fh.read()) == os.getpid()
    except OSError:
        return
    if owned:
        try:
            os.remove(path)
        except OSError:
            pass


def release_app_lock(app):
    p = _app_lock_path(app)
    _remove_lock_if_owned(p)
    _HELD_LOCKS.discard(p)


def _start_run_heartbeat(app, app_dir, interval=30):
    """Refresh the app lock's and state file's mtimes while a run is active.

    One build round can sit inside a single agent CLI call for many minutes
    with no state writes. Without a heartbeat: (a) the GUI's freshness window
    (state mtime < 240s) marks the run dead, so the UI looks frozen mid-call;
    and (b) the lock file ages past runtime.stale_lock_seconds, letting a
    second orchestrator "reclaim" a live run's lock — the two processes then
    fight over the same app. Returns a threading.Event; set() it to stop."""
    stop = threading.Event()
    paths = (_app_lock_path(app), state_path(app_dir))

    def _beat():
        while not stop.wait(interval):
            for p in paths:
                try:
                    os.utime(p, None)
                except OSError:
                    pass

    threading.Thread(target=_beat, name="run-heartbeat-%s" % app,
                     daemon=True).start()
    return stop


# ---------------------------------------------------------------------------
# CLI / model resolution
# ---------------------------------------------------------------------------
def which(name):
    return shutil.which(name)


def _probe_cache_path(filename):
    """Path for a 4h probe-verdict cache file, in the first writable dir. A
    bundled/read-only engine dir would otherwise silently fail every write, so
    the verdict never persists and the CLI re-probes (spending tokens/latency)
    on every run. Falls back to ~/.orchestrator, then the temp dir. Resolved per
    call (not cached) so it always tracks the current engine dir."""
    for d in (HERE, os.path.expanduser("~/.orchestrator")):
        try:
            os.makedirs(d, exist_ok=True)
            probe = os.path.join(d, ".orch_write_test.%d" % os.getpid())
            with open(probe, "w", encoding="utf-8") as fh:
                fh.write("")
            os.remove(probe)
            return os.path.join(d, filename)
        except OSError:
            continue
    return os.path.join(tempfile.gettempdir(), filename)


def detect_codex_model(cfg):
    """Return preferred Codex model if it actually works right now, else the
    configured default, else '' (let Codex use its own default).

    `codex models` needs a TTY and `codex model list` doesn't exist, so the old
    listing probes always failed headless and the preferred model NEVER ran.
    The only reliable headless check is a real one-token `codex exec` call; a
    plan usage-limit reply ("hit your usage limit ... try again at ...") counts
    as unavailable. The verdict is cached on disk for 4h so startup normally
    costs nothing and a reset limit is picked up the same morning."""
    preferred = cget(cfg, "models.codex_preferred_if_available", "") or ""
    fallback = cget(cfg, "models.codex", "") or ""
    if not (preferred and which("codex")):
        return fallback
    cache_p = _probe_cache_path(".codex_model_probe.json")
    try:
        with open(cache_p, encoding="utf-8") as fh:
            c = json.load(fh)
        if c.get("model") == preferred and time.time() - c.get("ts", 0) < 4 * 3600:
            return preferred if c.get("ok") else fallback
    except (OSError, ValueError):
        pass
    ok = False
    try:
        out, err, code = procutil.run_capture(
            ["codex", "exec", "--sandbox", "read-only", "--skip-git-repo-check",
             "-c", "model_reasoning_effort=low", "--model", preferred, "-"],
            timeout=120, input_text="Reply with exactly: OK")
        blob = (out + err).lower()
        ok = code == 0 and "usage limit" not in blob and "error:" not in blob \
            and "unsupported model" not in blob
    except (OSError, subprocess.SubprocessError):
        ok = False
    try:
        with open(cache_p, "w", encoding="utf-8") as fh:
            json.dump({"model": preferred, "ok": ok, "ts": time.time()}, fh)
    except OSError:
        pass
    if not ok:
        emit("Codex preferred model %s unavailable (probe failed/limit hit) — using %s."
             % (preferred, fallback))
    return preferred if ok else fallback


def valid_gemini_model(model):
    if not model:
        return False
    m = str(model).strip()
    # Antigravity display names like "Gemini 3.5 Flash (High)" are NOT valid
    # gemini-CLI ids. Only pass through ids that look like a model slug.
    return bool(re.match(r"^[a-zA-Z0-9._-]+$", m)) and "gemini" in m.lower()


# ---------------------------------------------------------------------------
# Agent invocation
# ---------------------------------------------------------------------------
def _agent_heartbeat(cfg):
    """No-output heartbeat (seconds) for an agent turn: kill a CLI that emits zero
    bytes for this long as a suspected hang, well before the full per-agent
    timeout. 0/absent disables it. See procutil.run_capture and config.yaml
    (runtime.agent_no_output_heartbeat_seconds)."""
    try:
        return int(cget(cfg, "runtime.agent_no_output_heartbeat_seconds", 0) or 0) or None
    except (TypeError, ValueError):
        return None


def _run_subprocess(cmd, cwd, timeout, env=None, heartbeat=None, input_text=None):
    # Delegate to the shared hardened runner: it runs the CLI in its own process
    # group and kills the WHOLE group on timeout (or on the no-output heartbeat),
    # so a helper that inherited our stdout pipe (agy/gemini does this) can't
    # deadlock the reap and hang the build. Still raises TimeoutExpired to
    # preserve call_agent's graceful path. See procutil for the full rationale.
    return procutil.run_capture(cmd, cwd=cwd, timeout=timeout, env=env,
                                heartbeat=heartbeat, input_text=input_text)


def _gemini_api_key(cfg):
    """A Gemini API key from the environment or a local key file. With a key,
    the gemini CLI runs reliably headless over HTTP — unlike agy, which needs an
    interactive terminal. The key file lives OUTSIDE the repo
    (~/.orchestrator/gemini_api_key, or models.gemini_api_key_file) so a real key
    can never be committed or pushed by run.sh's auto-commit. Returns None if no
    key is set."""
    for var in ("GEMINI_API_KEY", "GOOGLE_API_KEY"):
        v = os.environ.get(var)
        if v and v.strip():
            return v.strip()
    # Only outside-the-repo locations: the legacy in-repo path is dropped so a
    # real key is never read from (and at risk of being committed from) the repo.
    paths = [cget(cfg, "models.gemini_api_key_file", "")] if \
        cget(cfg, "models.gemini_api_key_file", "") else [
            os.path.expanduser("~/.orchestrator/gemini_api_key"),
        ]
    for path in paths:
        try:
            with open(path, encoding="utf-8") as fh:
                k = fh.read().strip()
                if k:
                    return k
        except OSError:
            continue
    return None


def _agent_cwd(cfg):
    """Return (cwd, ephemeral). During the build phase (when writes are allowed)
    agents work directly in the app's persistent build dir; otherwise they run in
    a throwaway temp dir so discussion-phase agents can't touch project files."""
    if cfg.get("_allow_writes") and cfg.get("_build_dir"):
        os.makedirs(cfg["_build_dir"], exist_ok=True)
        return cfg["_build_dir"], False
    # Audit mode: run READ-ONLY inside the target codebase so codex/claude can
    # grep/read the real files. Gated on `not _allow_writes` (so writes are always
    # off here — codex gets --sandbox read-only, claude no acceptEdits) and returns
    # ephemeral=False so the target is NEVER rmtree'd. It can only ever read.
    if cfg.get("_read_dir") and not cfg.get("_allow_writes") \
            and os.path.isdir(cfg["_read_dir"]):
        return cfg["_read_dir"], False
    if cget(cfg, "runtime.isolate_agent_cwd", True):
        return tempfile.mkdtemp(prefix="orch_agent_"), True
    return cfg["root"], False


_CODEX_SESSION_ID_RE = re.compile(r"^session id:\s*([0-9a-fA-F-]{36})\s*$", re.MULTILINE)


def run_codex(cfg, prompt, timeout):
    model = cfg["_resolved"]["codex_model"]
    # Allow file writes only during an enabled build phase; otherwise read-only.
    sandbox = "workspace-write" if cfg.get("_allow_writes") else "read-only"
    # Chat turns use low reasoning (fast, ~10s, natural); the build phase bumps it
    # up so generated code is solid. `codex exec` otherwise defaults to heavy
    # reasoning that takes many minutes per message.
    effort = (cget(cfg, "models.codex_build_reasoning", "medium") if cfg.get("_allow_writes")
              else cget(cfg, "models.codex_reasoning", "low"))
    sess = cfg.get("_session")
    if sess and sess.get("resume") and sess.get("id"):
        # `codex exec resume` is a distinct subcommand with its own flag set —
        # notably NO --sandbox: resuming always runs workspace-write
        # regardless of what the original session used. call_agent_sessioned
        # only resumes codex sessions when _allow_writes is already true
        # (build phases), where workspace-write is exactly what would have
        # been requested anyway — see the comment there for why a read-only
        # discussion session is never resumed this way.
        cmd = ["codex", "exec", "resume", sess["id"], "--skip-git-repo-check"]
    else:
        cmd = ["codex", "exec", "--sandbox", sandbox, "--skip-git-repo-check"]
    if effort:
        cmd += ["-c", "model_reasoning_effort=%s" % effort]
    if model:
        cmd += ["--model", model]
    # Pass prompt over stdin to avoid CLI shells that read prompt twice when
    # supplied both as positional and stdin (the root cause of intermittent
    # `Reading additional input from stdin...` hangs in this repo).
    cmd += ["-"]
    cwd, ephemeral = _agent_cwd(cfg)
    try:
        out, err, code = _run_subprocess(cmd, cwd, timeout,
                                         heartbeat=_agent_heartbeat(cfg),
                                         input_text=prompt)
    finally:
        if ephemeral:
            shutil.rmtree(cwd, ignore_errors=True)
    # Unlike claude/gemini, codex assigns its OWN session id — there's no
    # --session-id flag to pre-assign one on the creating call — so it has to
    # be scraped from the banner it prints. call_agent_sessioned reads this
    # back to learn what to store for the next resume.
    if sess is not None and not sess.get("resume"):
        m = _CODEX_SESSION_ID_RE.search(out or "")
        if m:
            tcxlib.TurnContext(cfg).stash_new_session_id(m.group(1))
    return out, err, code, _display_cmd(cmd + ["<prompt on stdin>"])


def run_claude(cfg, prompt, timeout):
    # Optional per-call model override (e.g. a stronger integrator model).
    model = cfg.get("_claude_model_override") or cfg["_resolved"]["claude_model"]
    # Prompt goes over stdin, not argv: on argv the full prompt (and any secret
    # spliced into it) is visible to every local user via `ps`/`/proc/<pid>/
    # cmdline`. `claude -p` reads the prompt from stdin when no positional is
    # given, matching run_codex/run_ollama.
    cmd = ["claude", "-p"]
    # CLI-session reuse (set by call_agent_sessioned): the first turn creates a
    # session with a known id; later turns resume it with only the delta prompt,
    # skipping the full-context cold start that dominates per-call latency.
    sess = cfg.get("_session")
    if sess and sess.get("resume"):
        cmd += ["--resume", sess["id"]]
    elif sess:
        cmd += ["--session-id", sess["id"]]
    if model:
        cmd += ["--model", model]
    # Reasoning-effort parity with Codex: `claude --effort <level>` (the
    # installed CLI accepts low|medium|high|xhigh|max). Two config keys mirror
    # the codex pair — chat turns use models.claude_reasoning, build turns
    # models.claude_build_reasoning. Empty (the default) omits the flag
    # entirely, i.e. exactly today's behavior.
    effort = (cget(cfg, "models.claude_build_reasoning", "") if cfg.get("_allow_writes")
              else cget(cfg, "models.claude_reasoning", ""))
    effort = str(effort or "").strip().lower()
    if effort:
        cmd += ["--effort", effort]
    if cfg.get("_allow_writes"):
        # Let Claude Code edit files in the build dir during the build phase.
        cmd += ["--permission-mode", "acceptEdits"]
    extra = cget(cfg, "runtime.claude_extra_args", None)
    if isinstance(extra, list):
        cmd += [str(x) for x in extra]
    # claude -p buffers the entire answer and emits ZERO bytes until it is
    # done, so the generic no-output heartbeat (tuned for streaming CLIs like
    # codex) would kill every long claude turn as a "hang". Use a separate,
    # larger window for claude only.
    hb = None
    try:
        hb = int(cget(cfg, "runtime.claude_no_output_heartbeat_seconds", 600) or 0) or None
    except (TypeError, ValueError):
        hb = 600
    # Sessions are stored per working directory, so a resumed session MUST run
    # from the same cwd as its first turn. Discussion phases normally use an
    # ephemeral temp dir; with a session active use the stable per-app dir
    # instead (still no writes — acceptEdits is only granted in build phases).
    if sess and cfg.get("_session_cwd"):
        cwd, ephemeral = cfg["_session_cwd"], False
        os.makedirs(cwd, exist_ok=True)
    else:
        cwd, ephemeral = _agent_cwd(cfg)
    try:
        out, err, code = _run_subprocess(cmd, cwd, timeout, heartbeat=hb,
                                         input_text=prompt)
    finally:
        if ephemeral:
            shutil.rmtree(cwd, ignore_errors=True)
    return out, err, code, _display_cmd(cmd + ["<prompt on stdin>"])


# Antigravity/gemini failure fingerprints. `agy` needs a controlling terminal;
# run headless (as the orchestrator does) it prints one of these instead of a
# real answer AND still exits 0 — so we must treat them as failures rather than
# accept the error text as the agent's "response". The gemini-cli OAuth tier for
# individuals is also deprecated, producing IneligibleTier / auth errors.
_GEMINI_ERR_MARKERS = (
    "could not open tty", "error opening tty", "bubbletea", "cli error:",
    "device not configured", "ineligibletier", "no longer supported",
    "please set an auth method", "migrate to the antigravity",
)


def _looks_like_gemini_error(text):
    t = (text or "").strip().lower()
    if not t:
        return True
    # A genuine reply is usually more than a one-line control/error message.
    return any(m in t for m in _GEMINI_ERR_MARKERS)


def detect_gemini_available(cfg):
    """Startup probe for the gemini agent (mirrors detect_codex_model): one
    tiny headless `gemini -p` call decides whether gemini joins the run AT ALL,
    instead of every turn burning its timeout on the same auth/TTY failure.

    Returns (ok, reason). Unavailable when the CLI exits 41 (the CLI's auth/
    quota error, typical with no GEMINI_API_KEY), errors, or prints one of the
    known TTY/tier fingerprints. The verdict is cached on disk for 4h
    (.gemini_probe.json next to the engine) so startup normally costs nothing
    and a fixed key/tier is picked up the same morning. When only `agy` is on
    PATH there is no cheap headless probe — defer to run_gemini's existing
    per-process fail-fast memo instead of pre-judging it here."""
    if not which("gemini"):
        if which("agy"):
            return True, ""
        return False, "no gemini/agy CLI on PATH"
    cache_p = _probe_cache_path(".gemini_probe.json")
    try:
        with open(cache_p, encoding="utf-8") as fh:
            c = json.load(fh)
        if time.time() - c.get("ts", 0) < 4 * 3600:
            return bool(c.get("ok")), str(c.get("reason", "") or "")
    except (OSError, ValueError):
        pass
    api_key = _gemini_api_key(cfg)
    env = dict(os.environ)
    if api_key:
        env["GEMINI_API_KEY"] = api_key
        env.pop("GOOGLE_GENAI_USE_GCA", None)   # force API-key auth, not OAuth
    model = cget(cfg, "models.gemini_fallback", "")
    cmd = ["gemini", "--skip-trust", "-p", "Reply with exactly: OK"]
    if valid_gemini_model(model):
        cmd += ["-m", str(model).strip()]
    ok, reason = False, ""
    try:
        out, err, code = procutil.run_capture(cmd, timeout=90, env=env)
        if code == 41:
            reason = ("gemini CLI exit 41 (auth/quota error%s)"
                      % ("" if api_key else "; no GEMINI_API_KEY / key file"))
        elif code != 0:
            reason = ("gemini CLI exit %s: %s"
                      % (code, (err.strip() or out.strip())[:160]))
        elif _looks_like_gemini_error(out):
            reason = ("gemini CLI error output%s: %s"
                      % ("" if api_key else " (no GEMINI_API_KEY / key file)",
                         out.strip()[:160]))
        else:
            ok = True
    except (OSError, subprocess.SubprocessError) as exc:
        reason = "gemini probe failed: %s" % exc
    try:
        with open(cache_p, "w", encoding="utf-8") as fh:
            json.dump({"ok": ok, "reason": reason, "ts": time.time()}, fh)
    except OSError:
        pass
    return ok, reason


def run_gemini(cfg, prompt, timeout):
    """Gemini access, in preference order: the gemini CLI WITH an API key
    (reliable headless — pure HTTP, no terminal), then Antigravity `agy` (only
    usable with a real terminal), then the keyless gemini CLI. Never fake output,
    and never accept agy's headless TTY error as if it were a real response."""
    # A keyless environment that already proved unusable stays unusable for the
    # rest of this process: fail fast instead of re-probing agy/gemini (~35s of
    # TTY errors) on every round of every phase.
    if cfg.get("_gemini_unavailable"):
        raise AgentError(cfg["_gemini_unavailable"])
    notes = []

    # 1) Preferred when a key is configured: gemini CLI with GEMINI_API_KEY. This
    #    is the only Gemini path that works reliably in a headless/GUI run.
    api_key = _gemini_api_key(cfg)
    if api_key and which("gemini"):
        env = dict(os.environ)
        env["GEMINI_API_KEY"] = api_key
        env.pop("GOOGLE_GENAI_USE_GCA", None)   # force API-key auth, not OAuth
        model = cfg["_resolved"]["gemini_model"]
        # --skip-trust: agents run in temp/app_build dirs the CLI treats as
        # untrusted; we've already sandboxed the cwd ourselves. Prompt goes
        # over stdin, not argv: on argv the full prompt (and any secret
        # spliced into it) is visible to every local user via `ps`/`/proc/
        # <pid>/cmdline`. Omitting -p makes gemini read stdin as the whole
        # prompt (combining -p with piped stdin is unreliable upstream).
        cmd = ["gemini", "--skip-trust"]
        if model:
            cmd += ["-m", model]
        if cfg.get("_allow_writes"):
            cmd += ["--yolo"]
        cwd, ephemeral = _agent_cwd(cfg)
        try:
            out, err, code = _run_subprocess(cmd, cwd, timeout, env=env,
                                             heartbeat=_agent_heartbeat(cfg),
                                             input_text=prompt)
        finally:
            if ephemeral:
                shutil.rmtree(cwd, ignore_errors=True)
        if out.strip() and not _looks_like_gemini_error(out):
            return out, err, code, _display_cmd(cmd + ["<prompt on stdin>"])
        detail = (err.strip() or out.strip())[:100] if (out.strip() or err.strip()) else "empty"
        notes.append("gemini(key) -> %s" % detail)

    # 2) Antigravity `agy` — only if no working key; needs an interactive terminal.
    use_agy = cget(cfg, "runtime.gemini_use_agy", True)
    if use_agy and which("agy"):
        # Verified Antigravity interface is `agy [flags] -p <prompt>` (print mode).
        # During the build phase agy must be allowed to write files in its cwd, so
        # auto-approve tool permissions. --print-timeout keeps a headless agy from
        # hanging. The legacy exec/run forms are kept last as a fallback.
        writeflag = ["--dangerously-skip-permissions"] if cfg.get("_allow_writes") else []
        pt = ["--print-timeout", "%ds" % min(int(timeout or 300), 300)]
        for tmpl in ([["agy"] + writeflag + pt + ["-p", prompt],
                      ["agy", "exec", "-p", prompt],
                      ["agy", "run", "-p", prompt]]):
            cwd, ephemeral = _agent_cwd(cfg)
            try:
                out, err, code = _run_subprocess(tmpl, cwd, timeout,
                                                 heartbeat=_agent_heartbeat(cfg))
            except (OSError, subprocess.SubprocessError) as exc:
                notes.append("agy attempt failed: %s" % exc)
                out, err, code = "", str(exc), 1
            finally:
                if ephemeral:
                    shutil.rmtree(cwd, ignore_errors=True)
            if code == 0 and out.strip() and not _looks_like_gemini_error(out):
                return out, err, code, _display_cmd(tmpl)
            reason = ("TTY/CLI error (needs a terminal)"
                      if _looks_like_gemini_error(out) and out.strip() else
                      ("empty" if not out.strip() else "code=%s" % code))
            notes.append("agy '%s' -> %s" % (" ".join(tmpl[:2]), reason))
        emit("Antigravity (agy) unusable headless (%s); trying gemini CLI."
             % "; ".join(notes))
    if which("gemini"):
        model = cfg["_resolved"]["gemini_model"]
        # Prompt over stdin, not argv — see the API-key path above for why.
        cmd = ["gemini"]
        if model:
            cmd += ["-m", model]
        if cfg.get("_allow_writes"):
            cmd += ["--yolo"]   # let Gemini act/write during the build phase
        cwd, ephemeral = _agent_cwd(cfg)
        try:
            out, err, code = _run_subprocess(cmd, cwd, timeout, heartbeat=_agent_heartbeat(cfg),
                                             input_text=prompt)
        finally:
            if ephemeral:
                shutil.rmtree(cwd, ignore_errors=True)
        if out.strip() and not _looks_like_gemini_error(out):
            return out, err, code, _display_cmd(cmd + ["<prompt on stdin>"])
        notes.append("gemini CLI -> %s"
                     % ("auth/tier error" if out.strip() or err.strip() else "empty"))
    # Nothing produced a real answer — let call_agent skip this agent cleanly.
    msg = ("Gemini unavailable headless (agy needs a terminal; gemini-cli "
           "tier deprecated). " + " | ".join(notes))
    if not api_key:
        # Keyless failures are structural (no TTY / deprecated tier), not
        # transient — cache so later rounds skip the probe instantly. A keyed
        # failure may be a blip, so those keep retrying.
        tcxlib.TurnContext(cfg).gemini_unavailable = msg
    raise AgentError(msg)


def _ollama_up():
    """True if a local Ollama server is reachable on loopback (localmodels.py
    owns the single probe; this alias keeps existing call sites/monkeypatches)."""
    return lmlib.server_running()


def run_ollama(cfg, prompt, timeout):
    """The 'ollama' roster agent (V2 spec §12): runs the selected local model
    (models.ollama) via `ollama run <model>` with the prompt on STDIN — no
    ARG_MAX limit, same hardened timeout/heartbeat machinery as every other
    runner, and nothing ever leaves this Mac."""
    model = str(cget(cfg, "models.ollama", "") or "").strip() \
        or cfg.get("_resolved", {}).get("ollama_model", "")
    if not model:
        raise AgentError("Local (Ollama) has no model selected — set models.ollama "
                         "in config.yaml (see local_models.json for the curated list).")
    # models.ollama_reasoning IS honored on the HTTP path (run_local, used by
    # dynamic local:<model> agents) as Ollama's /api/generate "think" field —
    # but this function shells out to the plain `ollama run <model>` CLI with
    # the prompt on stdin, which exposes no equivalent think-mode flag as
    # invoked here. Accepted but ignored on this path; warn once per phase so
    # a routing edit that only works for the other path doesn't look silently
    # broken.
    if cget(cfg, "models.ollama_reasoning", ""):
        if tcxlib.TurnContext(cfg).note_once(
                "ollama_reasoning_noop_cli", cfg.get("_phase_key")):
            emit("Phase '%s': routing sets ollama_reasoning, but the roster "
                 "'ollama' agent runs via the `ollama run` CLI, which this "
                 "engine invokes with no think-mode flag — accepted but "
                 "ignored on this path. (The HTTP local:<model> path, "
                 "run_local, does honor it as Ollama's 'think' field.)"
                 % cfg.get("_phase_key"))
    cmd = ["ollama", "run", model]
    cwd, ephemeral = _agent_cwd(cfg)
    try:
        out, err, code = _run_subprocess(cmd, cwd, timeout, heartbeat=_agent_heartbeat(cfg),
                                         input_text=prompt)
    finally:
        if ephemeral:
            shutil.rmtree(cwd, ignore_errors=True)
    return out, err, code, _display_cmd(cmd + ["<prompt on stdin>"])


def run_local(cfg, prompt, timeout, model=None):
    """Local-model adapter (V2 spec §12): talks to Ollama's loopback-only HTTP
    API. Never sends data off the machine. ``model`` is an Ollama tag like
    'qwen2.5-coder:7b'. Defaults to models.local_default, then the configured
    models.ollama, then llama3.1:8b as a last resort. Returns the same
    (out, err, code, command) shape as the CLI runners."""
    model = (model or cget(cfg, "models.local_default", "")
             or cget(cfg, "models.ollama", "") or "llama3.1:8b")
    url = "http://127.0.0.1:11434/api/generate"
    body = {"model": model, "prompt": prompt, "stream": False}
    # Real Ollama capability (verified against /api/generate's documented
    # request schema, not invented): reasoning-capable models (deepseek-r1,
    # qwen3, ...) accept a boolean "think" field. Effort levels below
    # "medium" map to omitting it (== today's behavior, no chain-of-thought
    # overhead on cheap/fast turns); "medium" and above turn it on. Models
    # that don't support thinking silently ignore the field per Ollama's API.
    _effort = str(cget(cfg, "models.ollama_reasoning", "") or "").strip().lower()
    if _effort and _effort != "low":
        body["think"] = True
    payload = json.dumps(body).encode("utf-8")
    cmd = "ollama:generate model=%s" % model
    try:
        req = urllib.request.Request(url, data=payload,
                                     headers={"Content-Type": "application/json"},
                                     method="POST")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        # 2.8: the ONLY runner with real token counts. The 4-tuple contract
        # is frozen, so they ride the traces side-channel.
        traceslib.set_last_usage({
            "prompt_tokens": body.get("prompt_eval_count"),
            "completion_tokens": body.get("eval_count")})
        return (body.get("response", ""), "", 0, cmd)
    except urllib.error.HTTPError as exc:
        return ("", "ollama HTTP %s: %s" % (exc.code, exc.reason), 1, cmd)
    except urllib.error.URLError as exc:
        return ("", "ollama unreachable (is `ollama serve` running?): %s" % exc.reason, 1, cmd)
    except Exception as exc:  # noqa: BLE001 - defensive; never crash the pipeline
        return ("", "ollama error: %s" % exc, 1, cmd)


# Roster runners ("ollama" = the configured local model via `ollama run`);
# dynamic local:<model> ids dispatch to run_local (see resolve_runner).
RUNNERS = {"codex": run_codex, "claude": run_claude, "gemini": run_gemini,
           "ollama": run_ollama}


def resolve_runner(agent):
    """Return a callable (cfg, prompt, timeout)->(out,err,code,cmd) for any agent
    id, including dynamic 'local:<model>' identities (V2 spec §4.1/§12)."""
    if isinstance(agent, str) and agent.startswith("local:"):
        model = agent.split(":", 1)[1]
        return lambda cfg, prompt, timeout: run_local(cfg, prompt, timeout, model=model)
    return RUNNERS[agent]


def _display_cmd(cmd):
    # Truncate the (possibly huge) prompt arg for readable logs/commands.
    shown = []
    for part in cmd:
        if len(part) > 120:
            shown.append(part[:117] + "...")
        else:
            shown.append(part)
    return " ".join(shown)


def write_call_log(app, phase, rnd, agent, command, stdout, stderr, code):
    ts = _dt.datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    # Local identities may embed a path-ish tag (local:hf.co/org/repo) — keep
    # the log filename flat.
    fname = "%s__%s__%s__r%s__%s.json" % (ts, encode_lock_name(app), phase,
                                          rnd, str(agent).replace("/", "-"))
    record = {
        "timestamp": now_str(),
        "app": app,
        "phase": phase,
        "round": rnd,
        "agent": agent,
        # §17: the command is persisted REDACTED, same as stdout/stderr — a
        # secret spliced into an argv (env dump, key flag) must never reach disk.
        "command": schemalib.redact_secrets(command if isinstance(command, str)
                                            else " ".join(str(c) for c in command)),
        "exit_code": code,
        "stdout": stdout,
        "stderr": stderr,
    }
    try:
        with open(os.path.join(LOG_DIR, fname), "w", encoding="utf-8") as fh:
            json.dump(record, fh, indent=2)
    except OSError as exc:
        emit("WARN could not write call log: %s" % exc)


def prune_logs(log_dir=None, retention_days=14, max_log_bytes=5 * 1024 * 1024):
    """Bound the logs/ directory: delete per-call *.json logs (and rotated
    *.log.1 files) older than retention_days, and rotate orchestrator.log to
    orchestrator.log.1 once it crosses max_log_bytes. retention_days <= 0
    disables age pruning. Best-effort — log hygiene must never take a run down.
    Configure via runtime.log_retention_days (default 14)."""
    log_dir = log_dir or LOG_DIR
    try:
        if retention_days and int(retention_days) > 0:
            cutoff = time.time() - float(retention_days) * 86400.0
            for name in os.listdir(log_dir):
                if not (name.endswith(".json") or name.endswith(".log.1")):
                    continue
                p = os.path.join(log_dir, name)
                try:
                    if os.path.isfile(p) and os.path.getmtime(p) < cutoff:
                        os.remove(p)
                except OSError:
                    continue
        main_log = os.path.join(log_dir, "orchestrator.log")
        if max_log_bytes and os.path.isfile(main_log) \
                and os.path.getsize(main_log) > int(max_log_bytes):
            os.replace(main_log, main_log + ".1")
    except OSError:
        pass


# Set by the SIGTERM/SIGINT handler so parallel worker threads stop launching
# NEW agent calls: previously they treated their killed CLI as a skippable
# failure and kept starting fresh rounds while main blocked in executor exit.
_SHUTDOWN = threading.Event()


# Cloud agents (calls leave the machine). Local identities never fall back further.
_CLOUD_AGENTS = ("codex", "claude", "gemini")


def _local_fallback_model(cfg, agent):
    """The installed local Ollama tag to retry a failed CLOUD turn on, or ""
    when fallback must not run (disabled in model_routing.json, agent already
    local, shutting down, sprint budget spent, or nothing pulled)."""
    if agent not in _CLOUD_AGENTS or _SHUTDOWN.is_set():
        return ""
    routing = cfg.get("_routing") or mrlib.load_routing(HERE)
    if not mrlib.fallback_enabled(routing):
        return ""
    _dl = cfg.get("_phase_deadline") or cfg.get("_deadline")
    if _dl and _dl - time.time() < 8:
        return ""
    installed = lmlib.installed_models_cached()
    if not installed:
        return ""
    roster = list((cfg.get("_resolved") or {}).get("ollama_roster") or [])
    configured = str(cget(cfg, "models.ollama", "") or "").strip()
    return mrlib.fallback_model(routing, configured, roster, installed)


def _fallback_steps(cfg, agent):
    """Ordered rescue steps for a failed cloud turn: the agent's configured
    chain first (same-provider model retries and local:<tag> entries), then
    the legacy cloud->local net as the final step. [] when fallback must not
    run (disabled, local agent, shutting down, sprint budget spent)."""
    if agent not in _CLOUD_AGENTS or _SHUTDOWN.is_set():
        return []
    routing = cfg.get("_routing") or mrlib.load_routing(HERE)
    if not mrlib.fallback_enabled(routing):
        return []
    _dl = cfg.get("_phase_deadline") or cfg.get("_deadline")
    if _dl and _dl - time.time() < 8:
        return []
    steps = list(mrlib.fallback_chain(routing, agent))
    net = _local_fallback_model(cfg, agent)
    if net:
        steps.append("local:%s" % net)
    seen, out = set(), []
    for s in steps:
        if s and s not in seen:
            seen.add(s)
            out.append(s)
    return out


def _event_app_dir(cfg, app=None):
    """The project dir whose events.jsonl a call site should append to.

    The pipeline stashes it in cfg["_app_dir"]; older/test call paths fall
    back to <root>/<app>. None (which makes evlib.emit_event a no-op) when
    neither is known — events must never invent a location."""
    d = cfg.get("_app_dir")
    if d:
        return d
    root = cfg.get("root")
    if root and app:
        return os.path.join(root, str(app))
    return None


def _primary_model_label(cfg, agent):
    resolved = cfg.get("_resolved") or {}
    if agent == "claude":
        return cfg.get("_claude_model_override") or resolved.get("claude_model") or "claude"
    if agent == "codex":
        return resolved.get("codex_model") or "codex"
    if agent == "gemini":
        return resolved.get("gemini_model") or "gemini"
    return str(agent)


def _patch_agent_model(fcfg, agent, model):
    """Point one provider at a different model for a single retry call.
    Body lives on TurnContext (2.3c) — the copy-before-mutate discipline
    is part of the band-C write protocol."""
    tcxlib.TurnContext(fcfg).patch_agent_model(agent, model)


def _bump_fallback_count(cfg, agent):
    """Aggregate per-run fallback rescues into agent_state.json
    (state["fallback_counts"][agent]) so any UI can badge degraded operation
    without parsing transcripts (spec §6 fallback visibility). Uses the live
    in-memory state dict (cfg["_state"]) — a disk read-modify-write here would
    be clobbered by the pipeline's next save_state. Best-effort: never raises."""
    try:
        st = cfg.get("_state")
        app_dir = cfg.get("_app_dir")
        if st is None or not app_dir:
            return
        # Hold the state lock across the read-modify-write so parallel build
        # workers bumping different agents don't lose each other's increments.
        with _STATE_LOCK:
            counts = st.setdefault("fallback_counts", {})
            key = str(agent)
            counts[key] = int(counts.get(key, 0) or 0) + 1
            save_state(app_dir, st)
    except Exception:  # noqa: BLE001 - a badge must never take a run down
        pass


def call_agent(cfg, app, phase, rnd, agent, prompt):
    """_call_agent_once plus the fallback ladder: when a cloud turn fails FOR
    ANY REASON (5-hour cap, rate limit, timeout, logged-out or missing CLI,
    empty output), the same prompt is retried down the agent's configured
    chain — sibling models on the same provider first (usage caps are usually
    per model tier), then local models on this Mac — before the turn is lost.
    Every rescued reply is clearly attributed in the transcript, and every
    step is mirrored as an agent_fallback event in <app>/events.jsonl
    (from_model/to_model/reason/status) so UIs no longer have to parse the
    transcript marker. Configure in model_routing.json -> fallback (GUI:
    Settings -> Routing)."""
    def _produced(model, status):
        # V3 board 1.11: one message_produced per DELIVERED reply. Every
        # sessioned path routes through call_agent, so these three returns
        # cover chats and debates alike.
        evlib.emit_event(_event_app_dir(cfg, app), "message_produced",
                         project=app, phase=phase, round=rnd,
                         agent=str(agent), model_used=str(model),
                         status=status)

    try:
        out = _call_agent_once(cfg, app, phase, rnd, agent, prompt)
        _produced(_primary_model_label(cfg, agent), "direct")
        return out
    except AgentError as exc:
        steps = _fallback_steps(cfg, agent)
        if not steps:
            raise
        primary = _primary_model_label(cfg, agent)
        _ev_dir = _event_app_dir(cfg, app)

        def _fallback_event(to_model, status, reason):
            evlib.emit_event(_ev_dir, "agent_fallback", project=app, phase=phase,
                             round=rnd, agent=str(agent), from_model=primary,
                             to_model=to_model, status=status,
                             reason=str(reason)[:200])
            # Mistakes ledger (never raises): record fallback OUTCOMES, not the
            # per-step "attempt" transitions the event stream already carries.
            if status != "attempt":
                mistklib.append_mistake(_ev_dir, {
                    "app": app, "workflow": cfg.get("_workflow_name"),
                    "phase": phase, "agent": str(agent), "cls": "agent_fallback",
                    "summary": "%s -> %s (%s): %s"
                               % (primary, to_model or "(none)", status,
                                  str(reason)[:160])})

        installed = lmlib.installed_models_cached()
        for step in steps:
            local_tag = step[6:] if step.startswith("local:") else \
                (step if step in installed else "")
            if step.startswith("local:") and local_tag not in installed:
                emit("Fallback step %s skipped — model not pulled." % step)
                _fallback_event(step, "skipped", "model not pulled")
                continue
            # Include the caller's lane/slug (if any) in the fallback health
            # key too — parallel-build lanes key circuit-breaker health by
            # their worker slug (distinct from the bare agent id) before
            # calling call_agent, and a fallback key of just
            # "fallback:<agent>:<step>" would collide across concurrent
            # lanes retrying the same agent+step, corrupting one lane's
            # circuit-breaker state with another's. The bare-agent-id case
            # (discussion-round turns, single-agent phases, or no health key
            # at all) keeps the original key format unchanged.
            _lane = cfg.get("_health_key")
            fcfg = tcxlib.TurnContext(cfg).thread_copy(
                health_key=("fallback:%s:%s:%s" % (agent, step, _lane)
                            if _lane and _lane != agent
                            else "fallback:%s:%s" % (agent, step)),
                stateless=True)   # retries are stateless
            to_model = "local:%s" % local_tag if local_tag else step
            try:
                if local_tag:
                    emit("%s (%s) failed (%s) — retrying locally on %s."
                         % (DISPLAY[agent], primary, str(exc)[:140], local_tag))
                    _fallback_event(to_model, "attempt", exc)
                    text = _call_agent_once(fcfg, app, phase, rnd,
                                            "local:%s" % local_tag, prompt,
                                            parent_call=getattr(exc, "trace_id", None))
                    _fallback_event(to_model, "rescued", exc)
                    _bump_fallback_count(cfg, agent)
                    _produced(to_model, "fallback")
                    return ("_[Fallback: %s answered on this Mac because %s (%s) "
                            "was unavailable.]_\n\n%s"
                            % (_derive_display("local:%s" % local_tag),
                               DISPLAY[agent], primary, text))
                emit("%s (%s) failed (%s) — retrying on %s."
                     % (DISPLAY[agent], primary, str(exc)[:140], step))
                _fallback_event(to_model, "attempt", exc)
                _patch_agent_model(fcfg, agent, step)
                text = _call_agent_once(fcfg, app, phase, rnd, agent, prompt,
                                        parent_call=getattr(exc, "trace_id", None))
                _fallback_event(to_model, "rescued", exc)
                _bump_fallback_count(cfg, agent)
                _produced(to_model, "fallback")
                return ("_[Fallback: %s answered on %s because %s was "
                        "unavailable.]_\n\n%s"
                        % (DISPLAY[agent], step, primary, text))
            except AgentError as fexc:
                emit("Fallback step %s failed too: %s" % (step, fexc))
                _fallback_event(to_model, "failed", fexc)
        _fallback_event("", "exhausted", exc)
        raise exc


# Short CLI replies that are really provider limit/auth banners, not content.
# exp7 regression: "You've hit your monthly spend limit · raise it at
# claude.ai/settings/usage" was accepted as a phase output and even became a
# coordinator decision. Only near-verbatim banners qualify (length-capped) — a
# real turn that merely QUOTES a limit message must never be discarded.
_PROVIDER_BANNERS = (
    "hit your monthly spend limit",
    "hit your usage limit",
    "claude.ai/settings/usage",
    "quota exceeded",
    "credit balance is too low",
    "invalid api key",
    "please run /login",
    "not logged in",
)


def _provider_banner(text):
    if len(text) > 400:
        return None
    low = text.lower()
    for sig in _PROVIDER_BANNERS:
        if sig in low:
            return sig
    return None


# Provider pacing (rate-limit protection): minimum gap between consecutive
# calls to the SAME cloud provider, across all threads and lanes in this
# process. Burst fan-outs are what trip per-minute limits; a short stagger is
# invisible next to a multi-minute turn. Local models are never paced.
_PACE_LOCK = threading.Lock()
_PACE_LAST = {}  # type: dict[str, float]


def _pace_provider(cfg, agent):
    base = str(agent).split(":", 1)[0]
    if base not in ("codex", "claude", "gemini"):
        return
    try:
        gap = float(cget(cfg, "runtime.provider_min_gap_seconds", 8) or 0)
    except (TypeError, ValueError):
        gap = 0.0
    if gap <= 0:
        return
    while not _SHUTDOWN.is_set():
        with _PACE_LOCK:
            now = time.time()
            wait = _PACE_LAST.get(base, 0.0) + gap - now
            if wait <= 0:
                _PACE_LAST[base] = now
                return
        time.sleep(min(wait, 1.0))


def _err_with_trace(err, trace):
    """Attach the failed call's trace id so the fallback ladder can chain
    rescue attempts to the primary (parent_call) — an explicit attribute,
    never a cfg key (2.3 gate)."""
    if trace:
        err.trace_id = traceslib.trace_id(trace)
    return err


def _call_agent_once(cfg, app, phase, rnd, agent, prompt, parent_call=None):
    """Invoke an agent CLI, log everything, and return its text response.

    Enforces the mandatory trailing signature and refuses to fabricate output
    when a CLI yields nothing usable."""
    if _SHUTDOWN.is_set():
        raise AgentError("orchestrator is shutting down")
    raw_timeout = cfg.get("_turn_timeout")
    if raw_timeout is None:
        raw_timeout = cget(cfg, "runtime.timeout_seconds_per_agent", 1200)
    timeout = int(raw_timeout or 0)
    timeout = timeout if timeout > 0 else None
    if agent == "ollama" or str(agent).startswith("local:"):
        # A local turn slower than this cap is hurting more than helping —
        # time it out and let the round proceed (breaker benches repeat
        # offenders). 0 disables.
        _lcap = int(cget(cfg, "runtime.local_turn_timeout_seconds", 600) or 0)
        if _lcap > 0:
            timeout = min(timeout, _lcap) if timeout else _lcap
    # Sprint/time-budget mode: never let a single turn run past the phase (or run)
    # deadline. Capping each turn to the time remaining is what turns the wall-clock
    # ceiling into a HARD guarantee — a hung turn can't blow the budget.
    _dl = cfg.get("_phase_deadline") or cfg.get("_deadline")
    if _dl:
        _remaining = _dl - time.time()
        if _remaining < 8:
            raise AgentError("%s skipped — sprint time budget reached." % DISPLAY[agent])
        timeout = max(8, min(timeout or int(_remaining), int(_remaining)))
    # Circuit breaker (§4.2), in-memory on cfg for this run: if this agent tripped
    # too many failures and is still in cooldown, skip it at ~zero cost instead of
    # paying its full timeout again. When retry_after elapses this naturally becomes
    # the half-open probe (it's simply tried again).
    # Health is keyed per WORKER when set (parallel builds pass a per-lane
    # "_health_key" so concurrent threads never mutate one shared dict), and by
    # the bare agent id on the sequential path.
    _hkey = cfg.get("_health_key") or agent
    health = _agent_health(cfg, agent, _hkey)
    if reslib.in_cooldown(health, time.time()):
        raise AgentError("%s skipped — in cooldown (%s)."
                         % (DISPLAY[agent], health.get("failure_signature") or "repeated failures"))
    # due_for_probe: cooldown just elapsed but status is still "down" (nothing
    # has recorded success/failure since). The call below IS the half-open
    # probe — this only makes that moment visible in the log, so "agent came
    # back after a cooldown" reads differently from "agent's first call ever."
    if reslib.due_for_probe(health, time.time()):
        emit("%s: cooldown elapsed — attempting recovery probe (%s)."
             % (DISPLAY[agent], health.get("failure_signature") or "repeated failures"))
    # Global cross-project worker cap (§4.5): claim a machine-wide slot before
    # spending an agent turn, so many concurrent projects can't oversubscribe the
    # machine or the provider rate limits. Fail-open + timeout-safe (proceeds rather
    # than hangs). Off by default; enable runtime.global_worker_cap_enabled.
    _rclass = None
    _claimed = False
    _claim_token = False
    if bool(cget(cfg, "runtime.global_worker_cap_enabled", False)):
        _is_local = isinstance(agent, str) and (agent == "ollama" or agent.startswith("local:"))
        _rclass = "local_model" if _is_local else "cli_remote"
        _cap = int(cget(cfg, "runtime.global_worker_cap.%s" % _rclass,
                        1 if _rclass == "local_model" else 12))
        _claim_token = grlib.claim_slot(app, _rclass, _cap, max_wait=min(int(timeout or 300), 300))
        _claimed = _claim_token is not False
        if not _claimed:
            emit("Global cap for %s busy — proceeding uncapped for %s." % (_rclass, DISPLAY[agent]))
    # Everything after a successful claim runs inside the try whose finally
    # releases the slot: Thread.start() below can genuinely raise (RuntimeError
    # under thread exhaustion — realistic exactly when many parallel workers
    # run), and a raise between claim and the guarded region used to leak the
    # slot until the 6-hour age reap.
    _hb_stop = threading.Event()
    try:
        # Stagger same-provider calls (runtime.provider_min_gap_seconds) so
        # burst fan-outs don't trip per-minute rate limits.
        _pace_provider(cfg, agent)
        emit("Starting %s for %s / %s round %s" % (DISPLAY[agent], app, phase, rnd))
        # Structured event sink (§6): one turn_started + exactly one turn_completed
        # per call, success or not, so a UI never has to parse transcript prose.
        _ev_dir = _event_app_dir(cfg, app)
        _model_req = _primary_model_label(cfg, agent)
        evlib.emit_event(_ev_dir, "turn_started", project=app, phase=phase,
                         round=rnd, agent=str(agent), model_requested=_model_req)
        t0 = time.time()
        # 2.8: crash-valid trace — a 'started' record exists on disk before
        # the runner runs; every arm below finalizes it exactly once.
        traceslib.pop_last_usage()   # a stale local count must never attach
        _trace = traceslib.write_started(
            _ev_dir, app=app, phase=phase, round=rnd, agent=str(agent),
            producer=str(agent), model=_model_req, rendered_prompt=prompt,
            parent_call=parent_call)

        # Slow-turn heartbeat: long build/integrate turns (10+ min) are legitimate,
        # but silence is indistinguishable from a hang without this — humans kill
        # healthy runs. Emits progress every 5 min until the turn returns.
        def _heartbeat():
            while not _hb_stop.wait(300):
                emit("%s still working on %s/%s round %s — %ds elapsed (timeout %s)."
                     % (DISPLAY[agent], app, phase, rnd,
                        int(time.time() - t0), timeout or "none"))

        threading.Thread(target=_heartbeat, daemon=True).start()
        try:
            out, err, code, command = resolve_runner(agent)(cfg, prompt, timeout)
        except FileNotFoundError as exc:
            # Enabled agent whose CLI binary is missing/uninstalled: skip it like
            # any other unavailable agent instead of crashing the whole run.
            write_call_log(app, phase, rnd, agent, "(missing CLI)", "", str(exc), 127)
            traceslib.finalize(_trace, stderr=str(exc), exit=127,
                               duration_s=time.time() - t0, status="error")
            reslib.record_failure(health, "missing_cli", time.time(), str(exc))
            evlib.emit_event(_ev_dir, "turn_completed", project=app, phase=phase,
                             round=rnd, agent=str(agent), ok=False, exit=127,
                             model_requested=_model_req, reason="missing_cli",
                             dur=round(time.time() - t0, 1))
            raise _err_with_trace(
                AgentError("%s CLI not found on PATH — is it installed? (%s)"
                           % (DISPLAY[agent], exc)), _trace)
        except procutil.NoOutputTimeout as exc:
            write_call_log(app, phase, rnd, agent, "(no-output timeout)", "", "no output", 124)
            traceslib.finalize(_trace, stderr="no output", exit=124,
                               duration_s=time.time() - t0, status="error")
            reslib.record_failure(health, "timeout", time.time(), "no output")
            evlib.emit_event(_ev_dir, "turn_completed", project=app, phase=phase,
                             round=rnd, agent=str(agent), ok=False, exit=124,
                             model_requested=_model_req, reason="no_output_timeout",
                             dur=round(time.time() - t0, 1))
            raise _err_with_trace(
                AgentError("%s produced no output for %ss — killed as a "
                           "suspected hang." % (DISPLAY[agent], exc.timeout)),
                _trace)
        except subprocess.TimeoutExpired:
            write_call_log(app, phase, rnd, agent, "(timeout)", "", "timeout", 124)
            traceslib.finalize(_trace, stderr="timeout", exit=124,
                               duration_s=time.time() - t0, status="error")
            reslib.record_failure(health, "timeout", time.time(), "timed out")
            evlib.emit_event(_ev_dir, "turn_completed", project=app, phase=phase,
                             round=rnd, agent=str(agent), ok=False, exit=124,
                             model_requested=_model_req, reason="timeout",
                             dur=round(time.time() - t0, 1))
            raise _err_with_trace(
                AgentError("%s timed out after %ds"
                           % (DISPLAY[agent], timeout or 0)), _trace)
        except AgentError as exc:
            # A runner-level refusal (e.g. run_gemini's unavailable memo) —
            # still exactly one turn_completed per turn_started.
            evlib.emit_event(_ev_dir, "turn_completed", project=app, phase=phase,
                             round=rnd, agent=str(agent), ok=False,
                             model_requested=_model_req, reason="agent_error",
                             detail=str(exc), dur=round(time.time() - t0, 1))
            traceslib.finalize(_trace, stderr=str(exc),
                               duration_s=time.time() - t0, status="error")
            if _trace and not getattr(exc, "trace_id", None):
                exc.trace_id = traceslib.trace_id(_trace)
            raise
        # V2 spec §17: single redaction chokepoint. Every sink (call log, transcript,
        # live log, docs) is built from call_agent's return value, so scrubbing here
        # means a secret an agent echoed never reaches any persisted artifact.
        out = schemalib.redact_secrets(out)
        err = schemalib.redact_secrets(err)
        _tokens = traceslib.pop_last_usage()
        write_call_log(app, phase, rnd, agent, command, out, err, code)
        text = (out or "").strip()
        if not text:
            emit("%s produced NO stdout (exit=%s). stderr: %s"
                 % (DISPLAY[agent], code, (err or "").strip()[:400]))
            reslib.record_failure(health, reslib.classify_failure(code, err), time.time(), err)
            evlib.emit_event(_ev_dir, "turn_completed", project=app, phase=phase,
                             round=rnd, agent=str(agent), ok=False, exit=code,
                             model_requested=_model_req, reason="empty_output",
                             output_len=0, dur=round(time.time() - t0, 1))
            traceslib.finalize(_trace, response=out, stderr=err, exit=code,
                               tokens=_tokens, duration_s=time.time() - t0,
                               status="error")
            raise _err_with_trace(
                AgentError("%s returned empty output — refusing to fabricate "
                           "a response. See logs/." % DISPLAY[agent]), _trace)
        banner = _provider_banner(text)
        if banner:
            emit("%s returned a provider limit/auth banner, not content (%r) — "
                 "treating the turn as failed." % (DISPLAY[agent], text[:160]))
            reslib.record_failure(health, "usage_limit", time.time(), text[:200])
            evlib.emit_event(_ev_dir, "turn_completed", project=app, phase=phase,
                             round=rnd, agent=str(agent), ok=False, exit=code,
                             model_requested=_model_req, reason="provider_banner",
                             detail=banner, dur=round(time.time() - t0, 1))
            traceslib.finalize(_trace, response=out, stderr=err, exit=code,
                               tokens=_tokens, duration_s=time.time() - t0,
                               status="error")
            raise _err_with_trace(
                AgentError("%s unavailable — provider banner: %s"
                           % (DISPLAY[agent], banner)), _trace)
        dur = time.time() - t0
        traceslib.finalize(_trace, response=out, stderr=err, exit=code,
                           tokens=_tokens, duration_s=dur, status="ok")
        emit("%s responded, %s characters (%.1fs)" % (DISPLAY[agent], f"{len(text):,}", dur))
        evlib.emit_event(_ev_dir, "turn_completed", project=app, phase=phase,
                         round=rnd, agent=str(agent), ok=True, exit=code,
                         model_requested=_model_req, model_used=_model_req,
                         output_len=len(text), dur=round(dur, 1))
    finally:
        _hb_stop.set()
        # Release ONLY the slot this call actually claimed, by its exact token
        # — releasing after a failed claim, or by (pid, resource_class) alone,
        # could free a DIFFERENT concurrent claim's row and corrupt the
        # machine-wide counter.
        if _rclass is not None and _claimed:
            grlib.release(_rclass, _claim_token)
    reslib.record_success(health)
    text = ensure_signature(text, agent)
    return text


def ensure_signature(text, agent):
    # Sign-offs are no longer required — the orchestrator already labels every
    # speaker. Strip any "From X" a model tacked on so the chat reads naturally.
    tail = text.rstrip()
    # SIGNATURE.values() only holds the four static ids; SIGNATURE[agent] derives
    # the right "From <display>" for a dynamic local:<model> id so its sign-off
    # is stripped too.
    candidates = list(SIGNATURE.values())
    if SIGNATURE[agent] not in candidates:
        candidates.append(SIGNATURE[agent])
    for sig in candidates:
        if tail.endswith(sig):
            tail = tail[: -len(sig)].rstrip()
            break
    return tail


def call_agent_sessioned(cfg, app, phase, rnd, agent, full_prompt,
                         delta_prompt=None, session_key=None):
    """call_agent with CLI-session reuse: claude everywhere, codex in
    write-enabled (build) phases only.

    First turn per session_key creates a session (full prompt); later turns
    resume it sending only delta_prompt — the agent already holds the phase
    context, so the cold-start cost (re-reading tens of KB of prompt and
    re-exploring the repo) is paid once per phase instead of every call. ANY
    failure of a resumed call falls back to one stateless full-prompt call, so
    a lost/expired session can never produce a worse result than before.

    Codex is scoped to _allow_writes phases: `codex exec resume` has no
    --sandbox flag and always runs workspace-write regardless of what the
    original session used (verified against a real install — this isn't a
    guess), so resuming a read-only discussion session would silently
    upgrade its sandbox. Discussion-phase codex turns stay stateless.
    Gemini isn't wired up yet — its --session-id/--resume flags exist, but
    verifying they compose correctly with --yolo (the build-phase write
    flag) needs a live check this session's Gemini quota couldn't cover;
    left for a follow-up rather than shipped unverified.

    `cfg` must be a per-call copy (dict(cfg)) when used from threads — the
    session flag rides on it. The shared session map lives on the ORIGINAL cfg
    and is visible through shallow copies."""
    sessionable = agent == "claude" or (agent == "codex" and bool(cfg.get("_allow_writes")))
    reuse = sessionable and session_key and delta_prompt is not None \
        and bool(cget(cfg, "runtime.%s_session_reuse" % agent, True))
    if not reuse:
        return call_agent(cfg, app, phase, rnd, agent, full_prompt)
    tctx = tcxlib.TurnContext(cfg)
    sessions = tctx.session_map(agent)
    sid = sessions.get(session_key)
    try:
        if sid:
            tctx.session = {"id": sid, "resume": True}
            return call_agent(cfg, app, phase, rnd, agent, delta_prompt)
        # claude/gemini let a caller pick the session id up front; codex
        # assigns its own and must be scraped from the first call's output
        # (run_codex stashes it via the TurnContext return channel).
        tctx.session = {"id": None if agent == "codex" else str(uuid.uuid4()),
                        "resume": False}
        out = call_agent(cfg, app, phase, rnd, agent, full_prompt)
        new_sid = tctx.take_new_session_id() if agent == "codex" else cfg["_session"]["id"]
        if new_sid:
            sessions[session_key] = new_sid
        return out
    except AgentError:
        if sessions.get(session_key) is None:
            raise   # first (session-creating) call failed — nothing to fall back from
        sessions.pop(session_key, None)
        tctx.session = None
        return call_agent(cfg, app, phase, rnd, agent, full_prompt)
    finally:
        # Exception-safety: BOTH the session clear and the unharvested-id
        # take must stay in this finally block.
        tctx.session = None
        tctx.take_new_session_id()


def _delta_discuss_prompt(cfg, agent, new_transcript, rnd, extra="", persona=""):
    """Continuation turn for a resumed discussion session: the agent already has
    the full phase context from its first turn, so send only what's new."""
    persona_block = ("\n===== YOUR HAT THIS PHASE =====\n" + persona + "\n") if persona else ""
    new_block = ("===== NEW MESSAGES SINCE YOUR LAST TURN =====\n" + new_transcript.strip()
                 if new_transcript.strip() else
                 "===== NO NEW MESSAGES SINCE YOUR LAST TURN =====")
    return (new_block
            + "\n\n===== YOUR TURN — this is %s talking (round %d) =====\n" % (DISPLAY[agent], rnd)
            + persona_block
            + "Continue the discussion: react to the new messages by name, defend or "
              "update your position, and push toward the best answer for this phase.\n"
            + extra
            + "\n" + COMMON_RULES)


# ---------------------------------------------------------------------------
# Prompt construction
# ---------------------------------------------------------------------------
COMMON_RULES = """\
How to talk in this room:
- Write in plain, natural English, like you're in a lively group chat with two
  sharp friends. NO headings, NO numbered rubrics, NO 0-100 scores, NO
  bullet-point templates, NO sign-off line. Just talk like a person.
- Speak as exactly ONE person (yourself). Don't write the others' messages.
- Go deep and be specific. Make a real argument with concrete examples and think
  out loud. A couple of rich paragraphs, not a shallow blurb.
- React to what the others actually just said, by name (Codex / Claude /
  Gemini / any local model in the room / and the human if they chimed in).
  Quote or paraphrase their point, then build on it or push back on it.
- Genuinely try to CONVINCE the group of whatever you think is the single best
  idea right now — it does NOT have to be your own. If someone makes a better
  case, say so and change your mind out loud.
- Do NOT agree just to agree. If an idea is weak, explain why. Surface the real
  trade-offs and disagreements — that friction is the whole point.
- Make the decisions yourselves; don't punt to "the human."
"""

INDEPENDENT_FIRST_RULES = """\
How to talk in this room:
- Write in plain, natural English. NO headings, NO numbered rubrics, NO 0-100
  scores, NO sign-off line.
- Speak as exactly ONE person (yourself). Don't write the others' messages.
- This is the independent first pass. Do not reference, anticipate, or agree
  with other agents yet. Give your own strongest answer before group debate
  begins.
- Be specific: include the best direction you see, the biggest risk or objection
  to your own direction, and the detail the final phase output must preserve.
- Make the decisions yourselves; don't punt to "the human."
"""


def _budget(text, limit):
    """Tail-keep budgeting. When truncation actually happens, the result is
    prefixed with an explicit marker line — agents must KNOW their context is
    incomplete instead of silently losing the oldest (most foundational) part.

    Correct for the hybrid summary+raw PRIOR PHASE DISCUSSIONS shape too: the
    blocks stay in workflow order (oldest phase first) regardless of whether a
    given block is a full transcript or a compact summary, so a tail-cut still
    keeps the most RECENT phases' content preferentially — which under the
    hybrid policy is exactly the raw, most-detail-worth-preserving material.
    Older phases are already compact summaries by construction, so they rarely
    need trimming at all, which is precisely why the hybrid policy shrinks how
    often this truncation marker fires in the first place."""
    if text is None:
        return ""
    if len(text) <= limit:
        return text
    return "[...earlier context truncated...]\n" + text[-limit:]


def _phase_file_path(app_dir, phase):
    """The persisted Markdown transcript path for a workflow phase."""
    if hasattr(phase, "key"):
        key = phase.key
        folder = getattr(phase, "folder", key)
        fname = getattr(phase, "file", key + ".md")
    else:
        key = phase[0]
        folder = phase[1] if len(phase) > 1 else key
        fname = phase[2] if len(phase) > 2 else key + ".md"
    return key, os.path.join(app_dir, folder, fname)


def prior_discussion_context(app_dir, phases, completed_keys, summaries=None):
    """Full earlier phase transcripts, in workflow order, for downstream phases.

    Final phase outputs are useful decisions; the actual discussions preserve
    disagreements, tradeoffs, rejected ideas, and user interjections. Keep this
    separate from phase_outputs so every later section can build from the full
    prior conversation whenever context budget allows.
    """
    done = set(completed_keys or [])
    blocks = []
    for phase in phases or []:
        key, path = _phase_file_path(app_dir, phase)
        if key not in done:
            continue
        try:
            with open(path, encoding="utf-8") as fh:
                text = fh.read().strip()
        except OSError:
            text = ""
        summ = (summaries or {}).get(key)
        if summ:
            blocks.append("--- %s (summary of prior discussion; decisions and "
                          "disagreements preserved) ---\n%s" % (key, summ))
        elif text:
            blocks.append("--- %s (complete prior discussion transcript) ---\n%s" % (key, text))
    return "\n\n".join(blocks)


def hybrid_prior_context(app_dir, phases, completed_keys, recency_window=2):
    """Like prior_discussion_context, but a genuine size REDUCTION rather than
    an additive block: only the most recent ``recency_window`` completed
    phases get their full raw transcript injected; every OLDER completed
    phase gets its compact phase_summaries.json entry instead (see
    _PHASE_SUMMARY_JSON_INSTRUCTION / merge_phase_summary). Recency still
    matters most for the phase(s) immediately upstream of the current one —
    that's where continuity of an ongoing debate's specific wording/tradeoffs
    is worth the size — while a phase from several steps back is already
    captured in decisions.json (authoritative) and now also this readable
    paragraph, so its multi-thousand-char transcript is pure bloat.

    Never silently drops an older phase's context: if no summary was ever
    persisted for it (e.g. it completed before this feature existed, or its
    wrap-up dropped the phase-summary-json block under format drift), that
    phase falls back to its raw transcript exactly like the legacy behavior.
    """
    done = set(completed_keys or [])
    ordered = []
    for phase in phases or []:
        key, path = _phase_file_path(app_dir, phase)
        if key in done:
            ordered.append((key, path))
    recent = set(k for k, _p in ordered[-recency_window:]) if recency_window > 0 else set()
    summaries = {s.get("phase"): s for s in load_phase_summaries(app_dir)
                if isinstance(s, dict)}

    def _raw(key, path, note=""):
        try:
            with open(path, encoding="utf-8") as fh:
                text = fh.read().strip()
        except OSError:
            text = ""
        if not text:
            return None
        return "--- %s (complete prior discussion transcript%s) ---\n%s" % (
            key, note, text)

    blocks = []
    for key, path in ordered:
        if key in recent:
            b = _raw(key, path)
            if b:
                blocks.append(b)
            continue
        summ = summaries.get(key)
        rendered = render_phase_summary(summ) if summ else ""
        if rendered:
            blocks.append("--- %s (phase summary; raw transcript omitted — see "
                          "the DECISIONS LOG for authoritative decisions) ---\n%s"
                          % (key, rendered))
        else:
            b = _raw(key, path, note="; no summary available")
            if b:
                blocks.append(b)
    return "\n\n".join(blocks)


def _select_prior_context(cfg, app_dir, phases, completed_keys, phasedef):
    """Resolve cfg['_prior_discussions'] for the phase about to run, per
    runtime.phase_summary_policy (sibling to runtime.build_context_policy,
    which governs BUILD WORKER lane context specifically — this knob governs
    the cross-phase history injected into every phase's own coordinator/
    discussion turns). "hybrid" (default): only the most recent phase(s) get
    their raw transcript; older completed phases get their compact
    phase_summaries.json entry instead — a real size reduction, not an
    additive block. "legacy" restores today's inject-every-completed-phase's-
    full-transcript behavior exactly, for compatibility/debugging.

    Split out of the main phase loop so the policy branch and the recency-
    window choice are independently testable without driving the whole run
    loop.
    """
    key = phasedef.key if hasattr(phasedef, "key") else phasedef[0]
    is_build_phase = bool(phasedef.get("writes", False)) \
        if hasattr(phasedef, "get") else (key == "build_coordination")
    # Build-adjacent phases keep only the immediately preceding phase in full
    # (workers already get a further-trimmed context anyway via
    # build_context_policy; the integrator mainly needs continuity with what
    # just happened). Discussion-heavy phases keep the last two so an ongoing
    # multi-phase debate doesn't lose its immediate thread.
    recency_window = 1 if is_build_phase else 2
    policy = str(cget(cfg, "runtime.phase_summary_policy", "hybrid")
                or "hybrid").strip().lower()
    if policy == "legacy":
        return prior_discussion_context(app_dir, phases, completed_keys)
    return hybrid_prior_context(app_dir, phases, completed_keys,
                                recency_window=recency_window)


def build_context(cfg, app, phasedef, original_prompt, prior_outputs, transcript):
    key, _folder, _fname, purpose = phasedef
    pri_lim = int(cget(cfg, "runtime.max_prior_output_chars", 4000))
    tr_lim = int(cget(cfg, "runtime.max_transcript_chars", 14000))
    prior_disc_lim = int(cget(cfg, "runtime.max_prior_discussion_chars", 120000))
    # Build phases: the full planning-debate transcripts matter far less than
    # the final decisions + machine contracts, and at 120k chars they dominate
    # every worker prompt. A tighter cap makes each call faster AND keeps the
    # signal (decisions/contracts are injected separately and stay full-size).
    if cfg.get("_prior_disc_cap"):
        prior_disc_lim = min(prior_disc_lim, int(cfg["_prior_disc_cap"]))
    parts = []
    parts.append("APP: %s" % app)
    parts.append("CURRENT PHASE: %s" % key)
    parts.append("PHASE PURPOSE: %s" % purpose)
    parts.append("\n===== ORIGINAL PROMPT (initial_prompt.md) =====\n%s" % original_prompt.strip())
    if prior_outputs:
        parts.append("\n===== DECISIONS FROM EARLIER PHASES =====")
        for pk, pout in prior_outputs:
            parts.append("\n--- %s (final decision) ---\n%s" % (pk, _budget(pout, pri_lim)))
    # Structured, authoritative decisions log (decisions.json) — injected BEFORE
    # the raw prior discussions and never tail-truncated away (compact by
    # construction; the huge cap is purely defensive).
    dec_log = render_decisions_log(load_decisions(cfg.get("_app_dir") or "")) \
        if cfg.get("_app_dir") else ""
    if dec_log:
        parts.append("\n===== DECISIONS LOG (structured, authoritative) =====\n%s"
                     % _budget(dec_log, 60000))
    prior_discussions = (cfg.get("_prior_discussions") or "").strip()
    if prior_discussions and not cfg.get("_drop_prior_discussions"):
        parts.append("\n===== PRIOR PHASE DISCUSSIONS (build from these; recent phases in "
                     "full, older phases may be summarized — see each block's label) =====\n%s"
                     % _budget(prior_discussions, prior_disc_lim))
    if transcript.strip():
        parts.append("\n===== THIS PHASE'S DISCUSSION SO FAR =====\n%s"
                     % _budget(transcript, tr_lim))
    else:
        parts.append("\n===== THIS PHASE'S DISCUSSION SO FAR =====\n(You are the first speaker.)")
    # Phase-specific playbook rules rendered from phase_rules.json (set at phase start).
    playbook = cfg.get("_phase_playbook", "")
    if playbook:
        parts.append(playbook)
    exemplar = cfg.get("_phase_exemplar", "")
    if exemplar:
        parts.append(exemplar)
    ts_block = cfg.get("_tech_stack_block", "")
    if ts_block and (cfg.get("_allow_writes")
                     or key in ("tech_specs", "ios_architecture_review",
                                "task_assignments")):
        parts.append("\n" + ts_block)
    # Operator-supplied per-phase instructions (Plan tab / routing file) —
    # binding guidance for THIS project's version of the phase.
    phase_instr = cfg.get("_phase_instructions", "")
    if phase_instr:
        parts.append("\n===== OPERATOR INSTRUCTIONS FOR THIS PHASE (binding) =====\n"
                     + phase_instr)
    # Retrieved, curated domain knowledge for this phase (set at phase start).
    know = cfg.get("_knowledge", "")
    if know:
        parts.append(know)
    # V3 4.7 PULL: relevant FINAL artifacts from sibling sections of this
    # project (set at phase start; '' for flat/artifact-free runs, so an
    # unrouted run stays byte-identical). Appended raw like the knowledge
    # block — never wrapped in _budget, which pre-cap already respected.
    art = cfg.get("_artifact_context", "")
    if art:
        parts.append(art)
    # V3 4.13: founder-pinned factory memory (set at phase start; '' for a
    # memory-free repo, so those runs stay byte-identical).
    mem = cfg.get("_memory", "")
    if mem:
        parts.append(mem)
    # Ground-truth text fetched by the ENGINE from URLs in the user's prompt
    # (set once at run start; urlfetch.py). Only the product-definition phases
    # get it — that's where a hallucinated reading of a linked product poisons
    # the whole run; later phases inherit those decisions anyway.
    if urlfetchlib.should_inject(key):
        url_ctx = cfg.get("_url_context", "")
        if url_ctx:
            parts.append(url_ctx)
    # Read-only digest of the target codebase (audit phases only; set at phase start).
    dig = cfg.get("_target_digest", "")
    if dig:
        parts.append(dig)
    # Structured verification context for a requires_verification phase (§16), so
    # the coordinator reasons from the real, persisted result — not a guess.
    vc = cfg.get("_verify_context", "")
    if vc:
        parts.append(vc)
    return "\n".join(parts)


def prompt_discuss(cfg, agent, ctx, phasedef, rnd, extra="", persona="",
                   independent_first=False, conversational=False):
    persona_block = ("\n===== YOUR HAT THIS PHASE =====\n" + persona + "\n") if persona else ""
    if conversational:
        # V3 board 1.1: a chat turn, not a debate turn. The debate goal below
        # ("argue hard… try to win the group over") makes agents fight each
        # other instead of answering the human — keep personas/PASS, change
        # only the goal.
        turn_goal = (
            "You are in an open working conversation with the human and the "
            "other agents. Respond to the human's most recent message directly "
            "and concretely; build on or push back on the other agents by name "
            "where it genuinely helps. This is a conversation, not a debate to "
            "win — do not push for a final decision or consensus.\n"
        )
        if rnd > 1:
            turn_goal += (
                "If you genuinely have NOTHING new to add this round — no "
                "disagreement, no new risk, nothing to sharpen — reply with the "
                "single word PASS instead of restating agreement.\n"
            )
        room_rules = COMMON_RULES
    elif independent_first:
        turn_goal = (
            "Make an independent first-pass contribution for this phase. The "
            "orchestrator is intentionally hiding same-round agent messages so "
            "the group gets real, non-anchored opinions before debate starts.\n"
        )
        room_rules = INDEPENDENT_FIRST_RULES
    else:
        turn_goal = (
            "Jump into the conversation and push it toward the best possible answer "
            "for this phase. Pitch concrete ideas, react to the others by name, argue "
            "hard for whatever you think is strongest right now, and try to win the "
            "group over. Disagree where you really disagree.\n"
        )
        if rnd > 1:
            # Token diet: an honest abstention beats a paragraph of restated
            # agreement — shorter transcripts make every later turn cheaper.
            turn_goal += (
                "If you genuinely have NOTHING new to add this round — no "
                "disagreement, no new risk, nothing to sharpen — reply with the "
                "single word PASS instead of restating agreement.\n"
            )
        room_rules = COMMON_RULES
    return (
        ctx
        + "\n\n===== YOUR TURN — this is %s talking (round %d) =====\n" % (DISPLAY[agent], rnd)
        + persona_block
        + turn_goal
        + extra
        + "\n" + room_rules
    )


def prompt_coordinate(cfg, agent, ctx, phasedef, rnd, is_build=False, final_round=False):
    key = phasedef.key if hasattr(phasedef, "key") else phasedef[0]
    if cfg.get("_workflow_target") == "audit":
        _fmt = ("under `## Final Output` list each finding as its own fenced "
                "```finding-json``` block with fields category(security|bug|update), "
                "severity(Critical|High|Med|Low), confidence(high|medium|low), title, "
                "file, line, why (the concrete failure/exploit scenario), fix (specific "
                "& safe). Dedupe exact duplicates; every finding MUST cite a real "
                "file:line from the target.")
        if key == "recon":
            goal = ("You're the AUDIT LEAD closing the recon phase. Summarize the "
                    "target's architecture and attack surface in plain English: "
                    "stack/versions, entry points and external inputs, where secrets and "
                    "persistence live, dependencies, and the highest-risk areas the later "
                    "phases should focus on. NO vulnerability findings yet. Write "
                    "`CONSENSUS: YES` on its own line, then the summary under "
                    "`## Final Output`.")
        elif key == "report":
            goal = ("You're the AUDIT LEAD closing the report phase. Start with a 3-line "
                    "severity-count summary (Critical/High/Med/Low). Then collect EVERY "
                    "distinct finding from this phase AND from the earlier phases' "
                    "findings above — never drop one. Write `CONSENSUS: YES` on its own "
                    "line, then " + _fmt + " (The tool assembles the final ranked report "
                    "from these blocks, so completeness matters more than prose.)")
        else:
            goal = ("You're the AUDIT LEAD closing this phase. Collect EVERY distinct "
                    "finding anyone raised — never drop one just because another agent "
                    "didn't repeat it. Write `CONSENSUS: YES` on its own line, then "
                    + _fmt)
        return (ctx
                + "\n\n===== %s is wrapping up round %d =====\n" % (DISPLAY[agent], rnd)
                + goal
                + "\nThe only fixed tokens are the `CONSENSUS: YES` line and the "
                + "`## Final Output` heading.\n")
    if is_build:
        if bool(cget(cfg, "runtime.build_code_changes_enabled", False)):
            goal = (
                "You're the INTEGRATOR. The other agents just built their parts in "
                "parallel, each in their own lane. Your job now is to make it all one "
                "working app. You OWN the integration/shared files — %s — the workers "
                "were told NOT to touch those, so wiring them in is on you: actually "
                "edit those files in app_build so everything the workers wrote is "
                "hooked up and the app compiles and runs as a single thing. Resolve "
                "any duplicate/conflicting files the parallel work created, and note "
                "any request a worker made for a shared change.\n"
                "Then, in plain English, recap where the build stands: what now "
                "exists in app_build, what's working, what's still missing, and which "
                "lane should take which concrete piece next. Be honest about gaps.\n"
                "When the app is genuinely functional and the heart of the original "
                "request is built, write the line `CONSENSUS: YES` on its own, then "
                "under `## Final Output` give a plain-English summary of what got "
                "built and how to run it. If there's still real work left, write "
                "`CONSENSUS: NO` and say exactly what each lane should build next "
                "iteration." % INTEGRATION_FILES
            )
            if final_round:
                goal += (
                    "\nThis is the LAST build iteration available — wrap it up now: "
                    "write `CONSENSUS: YES` and a plain-English summary of what got "
                    "built and how to run it under `## Final Output`."
                )
        else:
            goal = (
                "You're keeping the plan on track (no code is being written yet). In "
                "plain English, recap who's doing what next, plus open blockers and "
                "handoffs. If the plan is solid and everyone's aligned, write "
                "`CONSENSUS: YES` then the plan under `## Final Output`; otherwise "
                "`CONSENSUS: NO` and the open items for next iteration."
            )
    elif final_round:
        goal = (
            "This is the FINAL round for this phase. If the group has genuinely "
            "converged, make the call: state the single best answer from the "
            "discussion in plain English and say briefly why. Write the line "
            "`CONSENSUS: YES` on its own, then under `## Final Output` write the "
            "clear, concrete decision so the next phase can build on it.\n"
            "If real disagreement is still open, don't manufacture agreement that "
            "isn't there — write `CONSENSUS: NO` and summarize the competing "
            "positions under `## Final Output` instead; the orchestrator will "
            "settle it with a weighted vote across the group rather than leaving "
            "the call to you alone."
        )
    else:
        goal = (
            "You've been listening to the group talk through this phase. Like a "
            "friend recapping the chat, sum up in plain English where they've "
            "actually landed — what everyone now thinks is the best path, and any "
            "real disagreement that's still open. Don't invent agreement that isn't "
            "there.\n"
            "If they've genuinely converged, write the line `CONSENSUS: YES` on its "
            "own, then under `## Final Output` write the clear decision for this "
            "phase in plain English. If not, write `CONSENSUS: NO` and say what they "
            "still need to hash out next round."
        )
    contract = _phase_contract(cfg, phasedef)
    return (
        ctx
        + "\n\n===== %s is wrapping up round %d =====\n" % (DISPLAY[agent], rnd)
        + goal
        + ("\n" + contract if contract else "")
        + "\nTalk like a person, not a report. The only fixed tokens are the "
        + "`CONSENSUS: YES` / `CONSENSUS: NO` line and the `## Final Output` "
        + "heading — everything else is natural English.\n"
    )


def _session_coords(cfg):
    """(project, section) for a nested session, else (None, None) — the
    one derivation every section-scoped lookup (rules, contracts) shares."""
    app_dir = cfg.get("_app_dir") or ""
    root = cfg.get("root") or ""
    if not app_dir or not root:
        return None, None
    rel = os.path.relpath(app_dir, root)
    parts = rel.split(os.sep)
    if len(parts) != 3 or rel.startswith(".."):
        return None, None
    return parts[0], parts[1]


def _section_dir(cfg):
    """The engine-side section dir (sections/<section>) for a nested
    session, else None — routing and roles layer on it (V3 3.4)."""
    _project, section = _session_coords(cfg)
    return os.path.join(HERE, "sections", section) if section else None


def _phase_rule_layers(cfg):
    """Overlay rule files for the running session (V3 board 3.2), most
    specific first: the session's section rules
    (sections/<section>/rules.json) then the project override
    (<root>/<project>/phase_rules.json). Flat/legacy runs get [] and
    behave byte-identically — two sections naming the same phase key stop
    colliding on the one global entry."""
    project, section = _session_coords(cfg)
    if not section:
        return []
    return [os.path.join(HERE, "sections", section, "rules.json"),
            os.path.join(cfg["root"], project, phaseruleslib.RULES_FILENAME)]


def _rules_fallback_banner(cfg):
    """config_fallback banner callback for corrupt overlay rule files
    (ground rule 4: a PRESENT-but-broken layer surfaces; absence is
    normal and silent)."""
    app_dir = cfg.get("_app_dir")

    def _cb(path, error):
        evlib.emit_event(app_dir, "config_fallback",
                         section=os.path.basename(os.path.dirname(path)),
                         file=os.path.basename(path),
                         error=str(error)[:400])
    return _cb


def prompt_quality_check(cfg, agent, ctx, phasedef, rnd, coordinator_output):
    key = phasedef.key if hasattr(phasedef, "key") else phasedef[0]
    rubric = phaseruleslib.render_phase_quality_rubric(
        HERE, cfg.get("_workflow_target", "app"), key,
        layers=_phase_rule_layers(cfg),
        on_fallback=_rules_fallback_banner(cfg))
    contract = _phase_contract(cfg, phasedef)
    rubric_block = rubric or (
        "No editable phase rubric was found. Still require a concrete, useful "
        "phase artifact that the next phase can act on without restarting debate.")
    return (
        ctx
        + "\n\n===== PHASE QUALITY EVALUATOR — %s after round %d =====\n"
        % (DISPLAY[agent], rnd)
        + "You are checking whether the coordinator's latest `## Final Output` is "
        "strong enough to let this phase close. Do NOT continue the product debate "
        "and do NOT rewrite the whole answer. Judge the latest coordinator output "
        "against the original prompt, prior phase decisions, this phase's actual "
        "discussion, and the rubric below.\n\n"
        + "===== RUBRIC =====\n" + rubric_block + "\n"
        + ("\n===== REQUIRED MACHINE CONTRACT =====\n" + contract if contract else "")
        + "\n===== LATEST COORDINATOR OUTPUT =====\n"
        + _budget(coordinator_output or "", 10000)
        + "\n\nReturn exactly one of these status lines at the top:\n"
        + "QUALITY: PASS\n"
        + "QUALITY: FAIL\n\n"
        + "Pass only if the phase output is concrete, internally consistent, tied "
        "to the original prompt, incorporates the important disagreements or "
        "tradeoffs, covers the phase's required outputs, and leaves the next phase "
        "with a usable artifact. Fail if it is shallow, generic, missing required "
        "decisions, missing a required machine block, contradicts earlier phase "
        "decisions, or papers over unresolved disagreement. Do not fail only "
        "because the answer uses natural prose instead of headings.\n\n"
        + "After the status line, write `## Feedback` and either a short reason it "
        "passes or specific repair instructions for the next round.\n"
    )


def prompt_build_worker(cfg, worker, ctx, rnd, tree, roster_desc, integrator,
                        extra="", persona="", contract=""):
    """Prompt for one concurrent build worker. It runs at the SAME time as the
    others, so it must not wait on them — build its own lane now, stay out of the
    shared integration files, and leave notes for the integrator. ``contract`` is
    the lane's tasks.json slice + the shared interfaces.json contract (§19/§20)."""
    team = INTEGRATION_FILES
    persona_block = ("\n===== YOUR HAT THIS BUILD =====\n" + persona + "\n") if persona else ""
    return (
        ctx
        + "\n\n===== FILES IN app_build RIGHT NOW =====\n" + tree
        + "\n\n===== YOU ARE %s — BUILD ITERATION %d (working in PARALLEL) =====\n"
        % (worker["label"], rnd)
        + persona_block
        + "Your working directory IS the project's app_build folder. Right now, at "
        "the same moment, the other agents are each building their own part:\n"
        + "  " + roster_desc + "\n"
        + "YOUR LANE this build: %s.\n" % worker["lane"]
        + (contract + "\n" if contract else "")
        + "Build your lane for real — create and edit the actual code/files it "
        "needs. Because everyone is writing at once, stay inside your lane so you "
        "don't collide: do NOT edit %s — those are the integrator's (%s) job. If "
        "you need one of those changed, or you need something another lane owns, "
        "say so plainly in your message and %s will wire it up after this round.\n"
        % (team, integrator, integrator)
        + "Don't wait for the others and don't rebuild what already exists in the "
        "file list above — extend it. Each turn: say what you're about to build, "
        "build it, then call out what you left for the integrator or the next "
        "iteration.\n"
        + extra
        + "\nTalk naturally, like you're in a shared build channel with the others "
        "(react to what they said last iteration by name). No headings, no rubrics.\n"
    )


def prompt_vote(cfg, agent, ctx, phasedef, candidates):
    return (
        ctx
        + "\n\n===== TIME TO DECIDE — %s =====\n" % DISPLAY[agent]
        + "You've gone a few rounds without fully agreeing. The ideas on the table "
        + "are: %s.\n" % candidates
        + "In plain English, say which one you think the group should commit to and "
        + "make your best case for it. You can't pick your own idea — get behind "
        + "whichever of the OTHERS' ideas you genuinely think is strongest, and "
        + "explain why it beats the rest.\n"
        + "Then, at the VERY END of your message, cast your ballot as EXACTLY ONE "
        + "fenced block labeled vote-json (the orchestrator tallies these "
        + "mechanically — a missing or malformed ballot means your vote may not "
        + "count):\n"
        + "```vote-json\n"
        + '{"choice": "<name of the agent whose proposal you back — NOT yourself>", '
        + '"confidence": <integer 1-5>, "reason": "<one line>"}\n'
        + "```\n"
    )


def prompt_tally(cfg, agent, ctx, phasedef):
    return (
        ctx
        + "\n\n===== %s calls it =====\n" % DISPLAY[agent]
        + "Read the room from everyone's last messages and make the final call in "
        + "plain English: which idea wins and why, fairly reflecting who got behind "
        + "what. Then under a `## Final Output` heading write the decision clearly, "
        + "and on its own line write `VOTE_DECISION: YES`.\n"
    )


def _phase_quality_target(cfg):
    return cfg.get("_workflow_target", "app")


def _phase_quality_gate_enabled(cfg, is_build):
    if is_build:
        return False
    if _phase_quality_target(cfg) not in ("app", "app_spec"):
        return False
    return bool(cget(cfg, "runtime.phase_quality_gates_enabled", False))


def _independent_first_enabled(cfg, is_build):
    if is_build:
        return False
    if _phase_quality_target(cfg) not in ("app", "app_spec"):
        return False
    return bool(cget(cfg, "runtime.phase_independent_first_round_enabled", False))


def _quality_passed(text):
    return bool(QUALITY_PASS_RE.search(text or "")) and not bool(
        QUALITY_FAIL_RE.search(text or ""))


def pick_quality_evaluator(cfg, active, acting_coord):
    """The agent that grades a coordinator wrap-up. Never the coordinator
    itself while another healthy participant exists — an agent must not judge
    its own synthesis (the whole point of the gate is an independent check).
    Falls back to the coordinator only in effectively single-agent runs."""
    for cand in _coordinator_candidates(cfg, active, require_healthy=True):
        if cand != acting_coord:
            return cand
    for cand in ordered_agents(active):
        if cand != acting_coord and _agent_available(cand, cfg):
            return cand
    return acting_coord


def run_phase_quality_gate(cfg, app, app_dir, phasedef, rnd, coord, ctx,
                           coordinator_output, md_path, transcript,
                           evaluator=None):
    key = phasedef.key if hasattr(phasedef, "key") else phasedef[0]
    grader = evaluator or coord
    qprompt = prompt_quality_check(cfg, grader, ctx, phasedef, rnd, coordinator_output)
    qresp = call_agent(cfg, app, key, "quality-%s" % rnd, grader, qprompt)
    passed = _quality_passed(qresp)
    qblock = "**Quality Gate (%s) — after round %d**\n\n%s\n" % (
        DISPLAY[grader], rnd, qresp)
    append_md(md_path, "\n" + qblock)
    msglib.append_message(app_dir, key, "quality", grader, md_path,
                          agent=grader, rnd=rnd)
    transcript += "\n" + qblock
    live_log(app_dir, key, grader, "phase_quality_%s" % ("passed" if passed else "failed"),
             qresp)
    return passed, qresp, transcript


# Structured machine contracts certain phases must emit alongside their prose
# (V2 spec §19/§20). Injected into both the discussion turns and the
# coordinator's wrap-up prompt; parsed after the phase by parse_tasks_blocks /
# parse_interface_blocks. Lane names must match BUILD_LANE_IDS so tasks can be
# routed to the right parallel build worker.
# _TASKS_JSON_INSTRUCTION moved to sections.DEFAULT_CONTRACTS (V3 3.3) —
# prompt bytes pinned by tests/fixtures/contracts_frozen.json.
# _REQUIREMENTS_JSON_INSTRUCTION moved to sections.DEFAULT_CONTRACTS (V3 3.3) —
# prompt bytes pinned by tests/fixtures/contracts_frozen.json.
# _FLOWS_JSON_INSTRUCTION moved to sections.DEFAULT_CONTRACTS (V3 3.3) —
# prompt bytes pinned by tests/fixtures/contracts_frozen.json.
# _INTERFACES_JSON_INSTRUCTION moved to sections.DEFAULT_CONTRACTS (V3 3.3) —
# prompt bytes pinned by tests/fixtures/contracts_frozen.json.
_QUALITY_RULES_INSTRUCTION = (
    "PRODUCT-QUALITY RULES (a feature is NOT done just because the screen exists "
    "and compiles — build for real use, not the demo path):\n"
    "- Build REAL behavior, never a screenshot-only mockup. Every control does "
    "its real job or is visibly disabled with a reason. No empty button actions, "
    "no toggle that only flips its own look, no search that filters hardcoded data.\n"
    "- The UI must never lie: show 'Saved'/'Deleted'/'Sent'/'Synced'/'Unlocked' "
    "ONLY after it truly happened — never before, never unconditionally, never a "
    "success animation regardless of the real result.\n"
    "- No sample/placeholder data shown as if it were the user's real data. When "
    "there's no real data yet, show an honest empty state, not fabricated stats.\n"
    "- Give every data-driven screen an EXPLICIT state model (an enum like idle/"
    "loading/loaded/empty/failed) — not a pile of loose isLoading/hasError/"
    "showEmpty booleans that can form impossible combinations.\n"
    "- Adaptive layout: reflow, wrap, or scroll — never clip. No hardcoded "
    "screen-level frames. Set a sensible minimum window size on macOS. Honor "
    "Dynamic Type and dark mode (test long text and large text).\n"
    "- NEVER let text or controls OVERLAP or collide. Labels must not sit on "
    "top of values; two values must not stack into each other. When you place "
    "elements by a data value (along a timeline/axis/gradient, e.g. event times "
    "on a chart), you MUST guarantee a minimum gap and de-clutter — when two "
    "would collide (values close together, or a high-latitude day with events "
    "bunched up), offset, group, collapse, or drop a label rather than draw one "
    "over another. A screen where text overlaps is a broken screen, full stop.\n"
    "- Persist real data (must survive relaunch) and preserve the user's input "
    "across failures — a failed save must not wipe the form.\n"
    "- Prevent duplicate submissions (disable the trigger while it's in flight). "
    "Treat user cancellation as a normal outcome, NOT an error.\n"
    "- Accessibility is part of the build, not a cleanup pass: accessible labels "
    "on icon-only buttons, status conveyed by more than color, keyboard-navigable.\n"
    "- Validate real and adversarial input; never surface raw internal errors, "
    "stack traces, file paths, or tokens to the user.\n"
    "- In your wrap-up, HONESTLY name anything left as a placeholder, mock, or "
    "unfinished path — never claim a feature is done on the basis of it compiling.\n"
)
# _EXTRACTION_JSON_INSTRUCTION moved to sections.DEFAULT_CONTRACTS (V3 3.3) —
# prompt bytes pinned by tests/fixtures/contracts_frozen.json.


# _DECISIONS_JSON_INSTRUCTION moved to sections.DEFAULT_CONTRACTS (V3 3.3) —
# prompt bytes pinned by tests/fixtures/contracts_frozen.json.


# _PHASE_SUMMARY_JSON_INSTRUCTION moved to sections.DEFAULT_CONTRACTS (V3 3.3) —
# prompt bytes pinned by tests/fixtures/contracts_frozen.json.


def _decisions_contract_requested(cfg, phasedef):
    """True when this phase's coordinator wrap-up must emit decisions-json:
    every non-build discussion phase of an app/app_spec workflow (product/
    spec/architecture/plan-type phases). Build, verify/repair, and
    target-reading audit phases are excluded — their outputs are code or
    findings, not planning decisions."""
    if (cfg or {}).get("_workflow_target", "app") not in ("app", "app_spec"):
        return False
    if not hasattr(phasedef, "get"):
        return False
    if phasedef.get("writes") or phasedef.get("verify") \
            or phasedef.get("reads_target"):
        return False
    return True


def _contract_snippet(name):
    """One named contract's prompt snippet from the shipped data — the
    repair prompts and phase_extra re-statements read the same bytes the
    wrap-up published."""
    for e in seclib.DEFAULT_CONTRACTS:
        if e.get("contract") == name:
            return e["prompt_snippet"]
    return ""


def _phase_contract(cfg, phasedef):
    """The structured-block instruction(s) for this phase's wrap-up —
    assembled from sections.DEFAULT_CONTRACTS (or the session section's
    contracts.json overlay). The workflow_target gate rides the entry
    data (an unrelated workflow naming "extraction_candidates" must not
    inherit that contract); the decisions predicate stays code; the
    phase summary appends to EVERY phase. Byte parity with the historical
    constants is pinned by tests/fixtures/contracts_frozen.json AND the
    golden transcript suite."""
    key = phasedef.key if hasattr(phasedef, "key") else phasedef[0]
    _project, section = _session_coords(cfg or {})
    contracts = seclib.load_contracts(HERE, section=section,
                                      app_dir=(cfg or {}).get("_app_dir"))
    return seclib.assemble_contract(
        contracts, key, (cfg or {}).get("_workflow_target"),
        include_decisions=_decisions_contract_requested(cfg, phasedef),
        artifact_types=_artifact_types_emitted(cfg, section))


def _artifact_types_emitted(cfg, section):
    """The session section's declared emitted artifact types — [] for
    flat/legacy runs and for sections that publish nothing, which keeps
    the artifact-json snippet out of every prompt that predates the bus
    (byte parity by construction; V3 4.2)."""
    if not section:
        return []
    sec = seclib.load_section(section, HERE,
                              app_dir=(cfg or {}).get("_app_dir"))
    return list(sec.artifact_types_emitted or [])


# Phase-specific extra guidance.
def phase_extra(cfg, key):
    portfolio_note = ""
    if key in ("portfolio_selection", "app_features", "project_plan") and \
            portfoliolib.is_portfolio_parent_prompt(cfg.get("_original_prompt", "")):
        portfolio_note = portfoliolib.PORTFOLIO_JSON_INSTRUCTION
    if cfg.get("_workflow_target") == "audit":
        schema = (
            "When you record findings in the wrap-up, emit EACH finding as one fenced "
            "```finding-json``` block with fields: category(security|bug|update), "
            "severity(Critical|High|Med|Low), confidence(high|medium|low), title, file, "
            "line, why (the concrete failure/exploit scenario — inputs/state → wrong or "
            "unsafe outcome, NOT a restatement of the rule), fix (a specific, minimal, "
            "SAFE change — name the API/config/version). Cite a REAL file:line from the "
            "target digest. No generic advice, no finding without a location; dedupe; "
            "prefer fewer high-confidence findings over many speculative ones.\n")
        if key == "recon":
            return ("Establish the stack, framework/language versions, dependency "
                    "manifests, entry points and external inputs, trust boundaries, and "
                    "where secrets/persistence live — pointing at ACTUAL files in the "
                    "target digest. Do NOT list vulnerabilities yet; this is the map the "
                    "later phases hunt against.\n")
        if key == "security":
            return ("Hunt specifically for: secrets/credentials committed in code, "
                    "injection (SQL/command/path/template), broken authn/authz, insecure "
                    "storage (Keychain vs UserDefaults, plaintext creds), TLS/cert "
                    "validation disabled (ATS exceptions, verify=False, "
                    "rejectUnauthorized:false), over-broad permissions/entitlements and "
                    "missing usage strings, vulnerable/unpinned dependencies, and "
                    "PII/privacy leaks.\n" + schema)
        if key == "bugs":
            return ("Hunt for: data races / main-actor & Sendable issues, unawaited "
                    "promises, swallowed or wrong error handling, nil/None/empty/"
                    "off-by-one/overflow/timezone edge cases, retain cycles / missing "
                    "[weak self], leaked handles/listeners, and plain logic errors.\n"
                    + schema)
        if key == "modernization":
            return ("Hunt for: outdated/abandoned dependencies (versions behind, EOL), "
                    "deprecated/removed APIs, dead code, and performance issues (N+1, "
                    "sync I/O on hot paths, main-thread blocking). Give "
                    "current-vs-recommended and the migration effort/risk.\n" + schema)
        if key == "report":
            return ("Merge every finding raised this run AND in earlier phases. Resolve "
                    "severity disagreements and drop nothing real. The prioritized, "
                    "ranked list is assembled by the tool — you write the 3-line "
                    "executive summary and name the top 5 fixes to do first.\n")
        return ""
    if key == "prompt_contract":
        return (
            "Start by preserving the user's original prompt verbatim in the "
            "discussion output before interpreting it. Then turn it into an "
            "execution contract: hard requirements, explicit non-goals, output "
            "shape, production-readiness definition, and decision rules for later "
            "phases. Do not invent a different assignment.\n"
        )
    if key == "portfolio_selection":
        if portfolio_note:
            return (
                "This is the portfolio split gate. If the user asked for multiple "
                "apps, select the actual apps now and emit the portfolio-json block "
                "that creates one sibling project folder per selected app. Do not "
                "merge multiple requested apps into one app_build folder.\n"
                + portfolio_note
            )
        return (
            "Decide whether this is a single-app run or a portfolio parent. If it "
            "is single-app, say so clearly and explain why no child projects are "
            "needed. If it is multi-app, emit portfolio-json.\n"
        )
    if key == "per_app_product_brief":
        return (
            "Write this as a shippable product brief, not a brainstorm. Cover the "
            "specific user, paid value, core loop, subscription value, competitive "
            "wedge, local-first behavior, cloud-ready path, viral/niche growth "
            "mechanism, and what would make users genuinely keep the app.\n"
        )
    if key == "design_handoff":
        return (
            "Produce a handoff a designer or builder can use directly: screen list, "
            "states, navigation, content hierarchy, visual system, components, "
            "motion notes, accessibility constraints, and a professional Claude "
            "Design prompt/upload instruction when external design is desired.\n"
        )
    if key == "ios_architecture_review":
        return (
            "Review the app specifically as a modern iOS product. Prefer SwiftUI, "
            "Apple frameworks, local persistence first, privacy by design, and "
            "permissive dependencies only. Call out permissions, entitlements, "
            "StoreKit/subscriptions, background work, ML/AR feasibility, and UI/"
            "unit test strategy before tech_specs locks the contracts.\n"
        )
    if key == "task_assignments":
        return (
            "Sort out, in plain English, how you'll actually build this together: "
            "who takes which parts, WHERE the app will live (agree on a folder / "
            "location), how you'll avoid stepping on each other, and how you'll know "
            "it works. End this phase with a clear, agreed division of labor.\n"
            + _contract_snippet("tasks")
        )
    if key == "tech_specs":
        return _contract_snippet("interfaces")
    if key == "build_coordination":
        if bool(cget(cfg, "runtime.build_code_changes_enabled", False)):
            return (
                "This is the BUILD. Your current working directory IS the project's "
                "`app_build` folder — actually create and edit real files there to "
                "build the app the group designed. Each turn: say what you're about "
                "to build, then build it (write the real code/files), then call out "
                "what's left. Keep going until the app is genuinely functional. Talk "
                "naturally about what you just did and what's next.\n"
                "If you generate an Xcode/iOS project, it MUST run on a REAL iPhone: "
                "in EVERY build configuration set CODE_SIGNING_ALLOWED = YES, "
                "CODE_SIGNING_REQUIRED = YES, CODE_SIGN_STYLE = Automatic, and "
                "DEVELOPMENT_TEAM = %s (never leave it empty, never set style Manual "
                "with no team, never disable code signing to make the simulator "
                "happy). Use a real reverse-DNS bundle id.\n"
                % (str(cget(cfg, "ios.development_team", "") or "your Team ID"))
                + "\n" + _QUALITY_RULES_INSTRUCTION
            )
        return (
            "Coordinate the build in plain English: what you'd build next, what "
            "you're waiting on, blockers and handoffs. (Code changes are off, so "
            "describe the work; don't modify files.)\n"
        )
    if key == "implementation_readiness_gate":
        return (
            "This is the last gate before code. Check that the prompt contract, "
            "product brief, design handoff, iOS architecture, tech specs, project "
            "plan, tasks, tests, and acceptance criteria all agree. If they do not, "
            "downgrade scope or name the exact blocker before build starts.\n"
        )
    if key == "build_verification":
        return (
            "This phase verifies the generated app in the existing app_build folder. "
            "Do not restart the build or add new features. Focus on compile/test "
            "failures, minimal repair, missing project files, signing readiness, "
            "and an honest pass/fail summary from the real verifier.\n"
        )
    if key == "human_qa_checklist":
        return (
            "Create a manual QA script that covers every user workflow and every "
            "important state: onboarding, empty, loading, success, error, "
            "persistence, permissions, accessibility, offline behavior, and "
            "subscription/paywall behavior when relevant.\n"
        )
    if key == "app_store_readiness":
        return (
            "Prepare the app for App Store review: positioning, screenshots, privacy "
            "labels, permission strings, subscription/paywall review risks, support "
            "contact, age/content risks, release blockers, and concrete launch copy.\n"
        )
    if key == "portfolio_audit":
        return (
            "For a portfolio parent, audit folder shape and child metadata: one "
            "sibling folder per selected app, correct workflow per child, prompt "
            "requirements preserved, and no category silently skipped. For a "
            "single app, record that portfolio audit is not applicable.\n"
        )
    return portfolio_note


# ---------------------------------------------------------------------------
# Markdown phase file
# ---------------------------------------------------------------------------
def phase_header(app, phasedef, original_prompt):
    key, _folder, _fname, purpose = phasedef
    title = key.replace("_", " ").title()
    return (
        "# %s — %s\n\n" % (app, title)
        + "_Generated by the autonomous multi-agent orchestrator on %s._\n\n" % now_str()
        + "## Original Prompt\n\n```\n%s\n```\n\n" % original_prompt.strip()
        + "## Phase Purpose\n\n%s\n\n" % purpose
        + "## Transcript\n\n"
    )


def append_md(path, text):
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(text)
        if not text.endswith("\n"):
            fh.write("\n")


def write_md(path, text):
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


# Round/iteration header the sequential discussion loop writes at the top of
# every round ("### Round 3") — shared by process_phase's round-resume logic
# below. Anchored to a whole line so it never matches inside a quoted/prose
# mention of "### Round" in an agent's own message body.
_ROUND_HDR_RE = re.compile(r"^### (?:Round|Iteration) (\d+)\s*$", re.MULTILINE)
# The coordinator's decision block header — its presence right after a round's
# header (and before the next round's) is what makes that round "complete":
# every agent turn AND the coordinator turn for it are already on disk.
_COORD_DECISION_RE = re.compile(r"^\*\*Coordinator \(", re.MULTILINE)


def _resume_round_state(existing_md_text):
    """Determine where a phase's round loop should restart after a crash, by
    reading the persisted .md transcript itself rather than trusting the
    persisted current_round counter alone.

    Why the transcript, not the counter: state["current_round"] is written at
    the TOP of each round's body (before that round's agent/coordinator turns
    run), so a crash between that write and the round's completion leaves the
    counter one round AHEAD of what actually made it to disk. Re-running that
    round is safe (nothing of it was ever written). But an eager-counter
    design (bump it after every turn instead) has the opposite failure mode:
    a crash between the LAST turn of round N finishing and the counter update
    for N+1 would leave the counter reading N even though round N's content
    is already fully on disk — resuming "at N" would re-run a round whose
    output is already there, duplicating every agent's post. Parsing the file
    is immune to either lag because it asks the only question that matters:
    which rounds' content actually made it to disk, complete?

    A round counts as "complete" only if a "**Coordinator (" decision block
    appears somewhere between its header and the next round's header (or EOF)
    — i.e. every agent turn AND the coordinator turn for it are present, not
    just some agent posts. Returns (resume_round, safe_transcript_prefix):
    the round to restart the loop at, and the on-disk text truncated to drop
    any trailing INCOMPLETE round (its partial agent posts are discarded and
    that round is redone from scratch — resuming mid-round is not attempted,
    since re-running a "discussion turn" for a round that already got some
    but not all agents' posts would duplicate whichever agents did speak).
    """
    matches = list(_ROUND_HDR_RE.finditer(existing_md_text))
    if not matches:
        # No round ever started (crash before the first "### Round 1" line was
        # appended) — nothing to resume; caller falls back to a fresh start.
        return 1, existing_md_text, len(existing_md_text)
    last_complete_round = 0
    last_complete_end = 0
    for i, m in enumerate(matches):
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(existing_md_text)
        if _COORD_DECISION_RE.search(existing_md_text[start:end]):
            rnd_num = int(m.group(1))
            if rnd_num > last_complete_round:
                last_complete_round = rnd_num
                last_complete_end = end
    if last_complete_round == 0:
        # Rounds were started but NONE completed — drop them all, keep only
        # the header text that precedes the first round marker.
        header_end = matches[0].start()
        return 1, existing_md_text[:header_end], header_end
    header_end = matches[0].start()
    return last_complete_round + 1, existing_md_text[:last_complete_end], header_end


def _drain_inbox_message(app_dir, md_path, transcript, section_label,
                         phase_key=None, rnd=None, slot=""):
    """Worker for drain_human_inbox that ALSO returns the raw drained
    message (or None) so the conversational loop can act on the SAME bytes
    it folded — e.g. parse an @-mention delegation (V3 4.6) without a second
    read of the append-mode inbox. Byte-identical to the historical drain
    for every folded message."""
    inbox = os.path.join(app_dir, "human_inbox.txt")
    try:
        with open(inbox, encoding="utf-8") as fh:
            msg = fh.read().strip()
    except OSError:
        return transcript, None
    if not msg:
        return transcript, None
    try:
        open(inbox, "w", encoding="utf-8").close()  # drained
    except OSError:
        pass
    block = "**You (human) — %s**\n\n%s\n" % (section_label, msg)
    append_md(md_path, "\n" + block)
    if phase_key:
        msglib.append_message(app_dir, phase_key, "human", "human", md_path,
                              rnd=rnd or 0, slot=slot)
    emit("Human joined the conversation: %s" % msg.replace("\n", " ")[:100])
    return transcript + "\n" + block, msg


def drain_human_inbox(app_dir, md_path, transcript, section_label,
                      phase_key=None, rnd=None, slot=""):
    """If the human dropped a message into <app>/human_inbox.txt (e.g. from the
    GUI text box), fold it into the live conversation so the agents see and
    respond to it, then clear the inbox. Returns the (possibly extended)
    transcript. phase_key/rnd/slot feed the messages.jsonl index line; the
    slot is STRUCTURAL (open/coord/closing) because one debate round drains
    twice and counting drains would not survive a resume."""
    transcript, _msg = _drain_inbox_message(
        app_dir, md_path, transcript, section_label,
        phase_key=phase_key, rnd=rnd, slot=slot)
    return transcript


# ---------------------------------------------------------------------------
# V3 board 4.6: @-mention delegation from a chat composer. A human line that
# begins "@<Section> [<tier>[ token=<hex>]] <question>" fires either a
# Quick-take (one guest-persona turn in THIS session — an opinion, no
# artifact) or a Deep-dive (a 4.4-minted sub-session running the target
# section's real workflow; the reply card returns via 4.4's inbox edge).
# The parser is pure; execution rides the conversational drain boundary, so
# a session that never @-mentions is transcript-byte-identical.
# ---------------------------------------------------------------------------
_AT_RE = re.compile(r"^\s*@(.*)", re.DOTALL)
_FIRST_TOKEN_RE = re.compile(r"(\w[\w-]*)[ \t]*(.*)", re.DOTALL)
_TIER_RE = re.compile(
    r"^\[\s*(deep-dive|quick-take)(?:\s+token=([0-9a-zA-Z_-]+))?\s*\]\s*(.*)",
    re.DOTALL | re.IGNORECASE)


def parse_delegation(msg, known_sections):
    """Parse a leading '@<Section> [<tier>[ token=<hex>]] <question>' chat
    line. Returns None unless '@' is the first non-whitespace char (so
    "email me @foo later" never triggers, and every normal chat line is a
    no-op). known_sections maps lowercased id/title -> canonical id; a
    MULTI-WORD title is matched by longest known-key prefix (boundary-
    terminated, so "@researcher" never matches "research"). An
    absent/unknown tier defaults to the SAFE tier (quick-take: no mint)."""
    if not msg:
        return None
    m = _AT_RE.match(msg)
    if not m:
        return None
    after = m.group(1)
    canonical = raw_section = rest = None
    for keyname in sorted(known_sections, key=len, reverse=True):
        n = len(keyname)
        if (after[:n].lower() == keyname
                and (len(after) == n or after[n] in " \t[")):
            canonical = known_sections[keyname]
            raw_section = after[:n]
            rest = after[n:].lstrip(" \t")
            break
    if canonical is None:
        tm = _FIRST_TOKEN_RE.match(after)
        if not tm:
            return None                # a bare "@" is not a delegation
        raw_section, rest = tm.group(1), tm.group(2)
    tier, token, question = "quick-take", None, rest
    tm = _TIER_RE.match(rest)
    if tm:
        # .lower() because group(1) preserves case but the dispatcher and
        # req["tier"] compare against the lowercase literal.
        tier, token, question = tm.group(1).lower(), tm.group(2), tm.group(3)
    return {"tier": tier, "section": canonical, "raw_section": raw_section,
            "question": question.strip(), "token": token,
            "known": canonical is not None}


def _known_sections_map():
    """{lowercased id AND title -> canonical id} for every available
    section. Built only when a message actually starts with '@'."""
    out = {}
    for sid in seclib.list_sections(HERE):
        out[sid.lower()] = sid
        try:
            out[seclib.load_section(sid, HERE).title.lower()] = sid
        except Exception:  # noqa: BLE001 — a broken manifest must not crash chat
            pass
    return out


def _section_guest_ref(target):
    """A cross-section guest-persona ref ("<section>:<id>") that ACTUALLY
    resolves for a Quick-take. Candidates in order: the manifest's optional
    "guest_persona", then every role, then every personality in the
    section's roles.json — each validated against the SAME resolver
    _run_quick_take uses, so a typo'd guest_persona or a roles-less
    (personalities-only) section still yields a working voice. None only if
    nothing resolves."""
    sroot = os.path.join(HERE, "sections")

    def _resolves(gid):
        if not (isinstance(gid, str) and gid):
            return None
        ref = "%s:%s" % (target, gid)
        _kept, guests = roleslib.resolve_phase_role_refs(
            [ref], sroot, on_missing=lambda r, why: None)
        return ref if guests else None

    candidates = []
    try:
        sec = seclib.load_section(target, HERE)
        if isinstance(sec.extra, dict):
            candidates.append(sec.extra.get("guest_persona"))
    except Exception:  # noqa: BLE001 — a broken manifest must not crash chat
        pass
    try:
        with open(os.path.join(sroot, target, "roles.json"),
                  encoding="utf-8") as fh:
            doc = json.load(fh)
        for pool in ("roles", "personalities"):
            for r in (doc.get(pool) or []):
                if isinstance(r, dict):
                    candidates.append(r.get("id"))
    except (OSError, ValueError):
        pass
    for gid in candidates:
        ref = _resolves(gid)
        if ref:
            return ref
    return None


def _delegation_warn(app_dir, md_path, transcript, key, rnd, text):
    """Fold one visible System warning block (never silently drop, never
    crash) — shared by the unknown-section and dangling-guest paths."""
    block = "**System — Round %d**\n\n_%s_\n" % (rnd, text)
    append_md(md_path, "\n" + block)
    msglib.append_message(app_dir, key, "warning", "system", md_path, rnd=rnd)
    emit("Delegation: %s" % text)
    return transcript + "\n" + block


def _deep_dive_delegate(cfg, app, app_dir, key, rnd, md_path, transcript,
                        parsed):
    """Mint a sub-session running the target section's real workflow (the
    question rides its initial_prompt), spawn it, and fold an honest
    in-flight block. reply_to=this chat, so 4.4's return edge delivers the
    brief card back here. Idempotent: a UI resend with the same token mints
    the same dir (4.4 key) and we only spawn a freshly-pending one."""
    target = parsed["section"]
    project, _sec = _session_coords(cfg)
    if not project:
        return _delegation_warn(
            app_dir, md_path, transcript, key, rnd,
            "Deep dive needs a nested chat session — not available here.")
    try:
        sec = seclib.load_section(target, HERE)
        title = sec.title
    except Exception:  # noqa: BLE001
        sec, title = None, target
    # A section-scoped sub-session resolves its workflow by NAME (workflow.txt).
    # An INLINE workflow has no name, so it would silently fall through to the
    # default (app_build) — run the WRONG process. Refuse loudly instead of
    # lying about what ran (R2). Every shipped section names its workflow.
    wf = getattr(sec, "workflow_name", None)
    if not isinstance(wf, str) or wf == "(inline)":
        return _delegation_warn(
            app_dir, md_path, transcript, key, rnd,
            "Deep dive into %s isn't available — its workflow is inline "
            "(not addressable by name)." % title)
    token = parsed["token"] or hashlib.sha256(
        ("%s|%s" % (target, parsed["question"])).encode("utf-8")
    ).hexdigest()[:12]
    req = {"title": parsed["question"][:60] or target,
           "prompt": parsed["question"],
           "workflow": wf, "section": target,
           "origin_session": os.path.relpath(app_dir, cfg["root"]),
           "tier": "deep-dive", "send_token": token}
    sess = seslib.mint_delegation_session(
        cfg["root"], project, target, req, reply_to=app_dir,
        create_session=create_session,
        on_error=lambda m: emit("DELEGATION: %s" % m))
    if not sess:
        return _delegation_warn(
            app_dir, md_path, transcript, key, rnd,
            "Could not start a %s deep dive (see log)." % title)
    rec = seslib.read_delegation(sess, on_error=lambda m: emit(
        "DELEGATION: %s" % m))
    # Only a FRESH mint (status 'pending') spawns AND announces. An
    # idempotent resend hits the same 4.4 dir already 'running'/'failed'/
    # 'replied' — re-folding the block or re-emitting delegation_spawned
    # would duplicate the visible in-flight state and the GUI's pending-card
    # trigger, so it is a clean no-op.
    if not rec or rec.get("status") != "pending":
        return transcript
    pid = seslib.spawn_run(sess, root=cfg["root"],
                           on_error=lambda m: emit("DELEGATION: %s" % m))
    if pid is None:
        # spawn_run set status='failed'. Tell the truth rather than promise
        # a brief that can never arrive (§12.1 / R2).
        return _delegation_warn(
            app_dir, md_path, transcript, key, rnd,
            "Could not launch the %s deep dive (see log)." % title)
    evlib.emit_event(app_dir, "delegation_spawned", target=target,
                     session=os.path.relpath(sess, cfg["root"]),
                     tier="deep-dive")
    block = ("**System — Round %d**\n\n_Deep dive delegated to %s — running "
             "as a sub-session; its brief returns here when ready._\n"
             % (rnd, title))
    append_md(md_path, "\n" + block)
    msglib.append_message(app_dir, key, "delegation", "system", md_path,
                          rnd=rnd)
    return transcript + "\n" + block


def _run_quick_take(cfg, app, app_dir, phasedef, key, rnd, original_prompt,
                    prior_outputs, personas, active, transcript, md_path,
                    parsed, extra=""):
    """One guest-persona turn in the CURRENT session (an OPINION), modeled
    on the retry path: a single labeled block, no round advance, no vote,
    NO sub-session, NO artifact. The distinct "Quick take (guest: …)" label
    + persona chip make it impossible to mistake for the section's real
    process (§13.1)."""
    target = parsed["section"]
    ref = _section_guest_ref(target)
    if not ref:
        return _delegation_warn(
            app_dir, md_path, transcript, key, rnd,
            "Section %s has no guest persona to answer a quick take."
            % target)
    _kept, guests = roleslib.resolve_phase_role_refs(
        [ref], os.path.join(HERE, "sections"),
        on_missing=lambda r, why: None)
    if not guests:
        return _delegation_warn(
            app_dir, md_path, transcript, key, rnd,
            "Quick-take guest %r could not be resolved — treated as chat."
            % ref)
    speaking = ordered_agents(active)
    if not speaking:
        return _delegation_warn(
            app_dir, md_path, transcript, key, rnd,
            "No agent available to give a quick take right now.")
    agent = speaking[0]
    persona = {"role": guests[0], "personality": {}}
    label = roleslib.persona_label(persona)
    try:
        title = seclib.load_section(target, HERE).title
    except Exception:  # noqa: BLE001
        title = target
    acfg = tcxlib.TurnContext(cfg).thread_copy(health_key=agent,
                                               stateless=True)
    qctx = build_context(acfg, app, phasedef, original_prompt, prior_outputs,
                         transcript)
    qextra = ((extra + "\n\n") if extra else "") + (
        "A teammate asked for a QUICK TAKE from a %s guest voice. Answer "
        "'%s' as that guest — a single opinion, and be explicit that it is "
        "your opinion, not researched fact." % (title, parsed["question"]))
    qprompt = prompt_discuss(acfg, agent, qctx, phasedef, rnd, extra=qextra,
                             persona=roleslib.persona_preamble(persona),
                             conversational=True)
    try:
        resp = call_agent(acfg, app, key, rnd, agent, qprompt)
    except AgentError as exc:
        return _delegation_warn(
            app_dir, md_path, transcript, key, rnd,
            "Quick take from %s failed: %s" % (title, exc))
    block = ("**%s — Quick take (guest: %s · %s)**\n\n%s\n"
             % (DISPLAY.get(agent, agent), title, label, resp))
    append_md(md_path, "\n" + block)
    msglib.append_message(app_dir, key, "quicktake", agent, md_path,
                          persona=label, rnd=rnd)
    emit("Quick take: %s answered as a %s guest." % (
        DISPLAY.get(agent, agent), title))
    return transcript + "\n" + block


def _handle_chat_delegation(cfg, app, app_dir, phasedef, key, rnd,
                            original_prompt, prior_outputs, personas,
                            active, transcript, md_path, raw_msg, extra=""):
    """Dispatch a drained chat message: an @-mention fires Quick-take or
    Deep-dive; anything else is a pure no-op (golden-safe). The cheap
    leading-'@' check runs before any section load, so normal chat pays
    nothing."""
    if not raw_msg or not raw_msg.lstrip().startswith("@"):
        return transcript
    known = _known_sections_map()
    parsed = parse_delegation(raw_msg, known)
    if parsed is None:
        return transcript
    if not parsed["known"]:
        return _delegation_warn(
            app_dir, md_path, transcript, key, rnd,
            "No section named “%s”. Treated as a normal chat "
            "message. Known sections: %s."
            % (parsed["raw_section"], ", ".join(sorted(set(known.values())))))
    if parsed["tier"] == "deep-dive":
        return _deep_dive_delegate(cfg, app, app_dir, key, rnd, md_path,
                                   transcript, parsed)
    return _run_quick_take(cfg, app, app_dir, phasedef, key, rnd,
                           original_prompt, prior_outputs, personas, active,
                           transcript, md_path, parsed, extra)


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------
def state_path(app_dir):
    return os.path.join(app_dir, "agent_state.json")


def load_state(app_dir):
    p = state_path(app_dir)
    if os.path.exists(p):
        try:
            with open(p, encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, ValueError):
            pass
    return {
        "current_phase": None,
        "current_round": 0,
        "next_agent": None,
        "prompt_hash": None,
        "completed_phases": [],
        "phase_outputs": {},
        "last_processed": None,
        "consensus_status": {},
        "vote_results": {},
        "runner_pid": None,
        "done": False,
        "error": None,
        # Per-run fallback rescues by agent (spec §6 fallback visibility):
        # {"claude": 2, ...} — bumped by _bump_fallback_count so any UI can
        # badge degraded operation straight from agent_state.json.
        "fallback_counts": {},
    }


def derive_run_status(state):
    """V2 spec §6 status enum, derived from the legacy flags on every save.
    Additive: 'done'/'error'/'blocked_conflict'/'awaiting_approval' stay the
    authoritative raw fields for existing readers; 'status' is the one-word
    rollup (running | done | aborted | blocked_conflict | awaiting_approval)."""
    if state.get("blocked_conflict"):
        return "blocked_conflict"
    if state.get("error"):
        return "aborted"
    if state.get("awaiting_approval"):
        return "awaiting_approval"
    if state.get("done"):
        return "done"
    return "running"


def derive_verification(app_dir, state):
    """verified | failed | unverified — the §15 rollup of the LATEST persisted
    verification record for this run (prompt_hash-scoped, verify_results.json).

    "verified": the latest verify ran and passed; "failed": ran and did not;
    "unverified": no record, or the toolchain was absent (ran=false). This
    makes an all-UNVERIFIED run distinguishable from a genuinely verified one
    in agent_state.json. Observability ONLY — nothing gates on it. Never
    raises; any read problem degrades to "unverified"."""
    try:
        latest = verifylib.latest_verify_result(
            app_dir, prompt_hash=(state or {}).get("prompt_hash"))
        if not latest or not latest.get("ran"):
            return "unverified"
        return "verified" if latest.get("ok") else "failed"
    except Exception:  # noqa: BLE001 - a rollup must never take a save down
        return "unverified"


# Guards every save_state mutation+write. During build_coordination the main
# thread and several parallel build-worker threads share one `state` dict and
# one agent_state.json (workers reach save_state via _bump_fallback_count), so
# an unsynchronized read-modify-write loses updates and two writers racing on the
# same temp file corrupt it. Reentrant so _bump_fallback_count can hold it across
# its own save_state call. One global lock is fine — saves are small and rare.
_STATE_LOCK = threading.RLock()


def reset_state_for_new_prompt(state, phash):
    """A new/changed prompt is a fresh build: clear the pipeline progress AND
    the per-run FAILURE bookkeeping, in place. Mutates and returns ``state``.

    The failure keys are the subtle part — they leak across unrelated runs if
    not cleared: a stale ``release_gate_repairs`` budget makes the very first
    release-gate failure on the NEW prompt report "repair budget exhausted"
    without ever attempting a single repair, and stale ``phase_resolutions``
    surface an old amber "closed without resolution" warning on phases the new
    run hasn't reached yet."""
    state.update({
        "prompt_hash": phash,
        "completed_phases": [],
        "phase_outputs": {},
        "consensus_status": {},
        "vote_results": {},
        "done": False,
        "error": None,
        "release_gate_repairs": 0,
        "phase_resolutions": {},
    })
    return state


def save_state(app_dir, state):
    with _STATE_LOCK:
        state["runner_pid"] = os.getpid()
        state["last_processed"] = now_str()
        state["status"] = derive_run_status(state)
        state["verification"] = derive_verification(app_dir, state)
        # Per-writer temp name so concurrent savers never clobber one shared
        # ".tmp" mid-write; the os.replace onto the real path stays atomic.
        tmp = "%s.%d.%x.tmp" % (state_path(app_dir), os.getpid(),
                                threading.get_ident())
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(state, fh, indent=2)
            os.replace(tmp, state_path(app_dir))
        except Exception:
            try:
                os.remove(tmp)
            except OSError:
                pass
            raise


def sha256_text(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Live log (V2 spec §21): one JSON line per significant event. Only the Python
# coordinator writes this file — never the agents.
# ---------------------------------------------------------------------------
def live_log(app_dir, lane, agent, kind, summary):
    """Append one event line to <app_dir>/.orchestrator_runtime/live_log.jsonl
    (spec §5 location — moved 2026-07-15 from <app_dir>/live_log.jsonl, which
    put a noisy per-turn log directly in a workspace/generated-app git
    history) matching REQUIRED_FIELDS["live_log_entry"] (ts, lane, agent,
    kind, summary). Summaries are whitespace-collapsed, redacted (§17) and
    capped at 280 chars. Best-effort: a broken disk/path must never take a
    run down, so this never raises."""
    try:
        entry = {
            "schema_version": schemalib.SCHEMA_VERSION,
            # tz-aware so live_log entries order unambiguously across DST.
            "ts": _dt.datetime.now().astimezone().isoformat(timespec="seconds"),
            "lane": str(lane or ""),
            "agent": str(agent or ""),
            "kind": str(kind or ""),
            "summary": schemalib.redact_secrets(" ".join(str(summary or "").split()))[:280],
        }
        runtime_dir = _orchestrator_runtime_dir(app_dir)
        with _LOG_LOCK:
            os.makedirs(runtime_dir, exist_ok=True)
            with open(os.path.join(runtime_dir, "live_log.jsonl"), "a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry) + "\n")
    except Exception:  # noqa: BLE001 - logging must never raise
        pass


# ---------------------------------------------------------------------------
# Task backlog (V2 spec §19) and interface contract (§20): parse the structured
# fenced blocks the coordinator emits, validate, and persist to <app>/tasks.json
# and <app>/interfaces.json so the parallel build reads a machine contract.
# ---------------------------------------------------------------------------
def parse_tasks_blocks(text):
    """Extract every ```tasks-json``` block (each block is ONE JSON object:
    either a {"tasks": [...]} wrapper or a single task). Returns (tasks, errors):
    tasks validated against REQUIRED_FIELDS["task_item"] and de-duplicated by id
    (last emission wins, so the coordinator's final revision beats a draft);
    errors is the human-readable list of malformed blocks/items. Never raises."""
    errors = []
    blocks = schemalib.extract_structured_blocks(
        text or "", seclib.contract_fence("tasks")[0],
        on_error=errors.append)
    byid = {}
    for b in blocks:
        items = b["tasks"] if isinstance(b.get("tasks"), list) else [b]
        for t in items:
            ok, missing = schemalib.validate_required_fields(
                t, schemalib.REQUIRED_FIELDS["task_item"])
            if not ok:
                errors.append("task %r missing required field(s): %s"
                              % (t.get("id") if isinstance(t, dict) else t,
                                 ", ".join(missing)))
                continue
            t.setdefault("depends_on", [])
            t.setdefault("acceptance_criteria", [])
            t.setdefault("requirement_ids", [])
            status = str(t.get("status", "pending")).strip().lower()
            t["status"] = status if status in schemalib.TASK_STATUS else "pending"
            byid[str(t["id"])] = t
    # Report (don't drop) two planning errors the build would otherwise swallow:
    # an owner_lane the roster can't route (falls back to showing the task to
    # every worker), and a depends_on pointing at a task that doesn't exist.
    tasks = list(byid.values())
    known_ids = set(byid)
    for t in tasks:
        lane = t.get("owner_lane")
        if lane is not None and str(lane) not in BUILD_LANE_IDS:
            errors.append("task %r has unknown owner_lane %r (expected one of: %s)"
                          % (t.get("id"), lane, ", ".join(BUILD_LANE_IDS)))
        for d in t.get("depends_on") or []:
            if str(d) not in known_ids:
                errors.append("task %r depends_on unknown id %r" % (t.get("id"), d))
    return tasks, errors


def parse_extraction_blocks(text):
    """Extract the library_mining ```extraction-json``` package contract.

    Returns (pkg | None, errors). The LAST valid block wins (mirrors
    'last emission wins' everywhere else). All identifiers — the package name
    and every public_api name — are sanitized HERE and stored sanitized, so the
    downstream scaffolder never sees a raw name: this is the single point that
    collapses reserved-word compile errors, redeclaration collisions, and path
    traversal. A public_api item is kept only when its kind is a known Swift
    kind AND its name sanitizes to something usable; deduped by sanitized name
    (last wins). Never raises."""
    errors = []
    _xf, _xreq = seclib.contract_fence("extraction")
    blocks = schemalib.extract_structured_blocks(
        text or "", _xf,
        required_fields=schemalib.REQUIRED_FIELDS[_xreq],
        on_error=errors.append)
    if not blocks:
        return None, errors
    raw = blocks[-1]   # last valid block wins
    name = schemalib.sanitize_swift_identifier(raw.get("package_name"))
    if not name:
        errors.append("extraction-json package_name %r is not a usable Swift identifier"
                      % (raw.get("package_name"),))
        return None, errors
    api_in = raw.get("public_api")
    if not isinstance(api_in, list):
        errors.append("extraction-json public_api is not a list")
        return None, errors
    byname = {}
    for item in api_in:
        if not isinstance(item, dict):
            errors.append("extraction-json public_api entry is not an object: %r" % (item,))
            continue
        kind = str(item.get("kind", "")).strip().lower()
        if kind not in schemalib.API_KINDS:
            errors.append("extraction-json api item %r has unknown kind %r (expected one of: %s)"
                          % (item.get("name"), item.get("kind"), ", ".join(schemalib.API_KINDS)))
            continue
        sane = schemalib.sanitize_swift_identifier(item.get("name"))
        if not sane:
            errors.append("extraction-json api item name %r is not a usable Swift identifier"
                          % (item.get("name"),))
            continue
        sig = item.get("signature")
        byname[sane] = {"kind": kind, "name": sane,
                        "signature": sig if isinstance(sig, str) else ""}
    pkg = {
        "package_name": name,
        "platforms": [],                          # kept out of the manifest by design
        "products": [],                           # scaffolder derives from package_name
        "public_api": list(byname.values()),
        "adopting_repos": [r for r in (raw.get("adopting_repos") or [])
                           if isinstance(r, str)],
    }
    return pkg, errors


def parse_flows_blocks(text):
    """Extract every ```flows-json``` block ({"flows": [...]}) — the declared
    user journeys the UI-crawl gate replays. Returns (flows, errors); a flow
    needs a name and a non-empty steps list. Never raises."""
    errors = []
    blocks = schemalib.extract_structured_blocks(
        text or "", seclib.contract_fence("flows")[0],
        on_error=errors.append)
    byname = {}
    for b in blocks:
        items = b.get("flows") if isinstance(b.get("flows"), list) else [b]
        for f in items or []:
            if not isinstance(f, dict):
                continue
            name = str(f.get("name") or "").strip()
            steps = f.get("steps")
            if not name or not isinstance(steps, list) or not steps:
                errors.append("flow %r needs a name and non-empty steps" % name)
                continue
            byname[name] = {"name": name, "steps": steps}
    return list(byname.values()), errors


def persist_flows(app_dir, flows):
    """Merge declared flows into <app>/flows.json. Learned regression flows
    (origin=ui_crawl_crash) are PRESERVED — the spec never overwrites what a
    crash taught us; same-name declared flows are replaced."""
    path = os.path.join(app_dir, "flows.json")
    existing = []
    try:
        with open(path, encoding="utf-8") as fh:
            loaded = json.load(fh)
        if isinstance(loaded, dict) and isinstance(loaded.get("flows"), list):
            existing = loaded["flows"]
    except (OSError, ValueError):
        pass
    keep = [f for f in existing if isinstance(f, dict)
            and f.get("origin") == "ui_crawl_crash"]
    _write_json_atomic(path, {"schema_version": 1, "flows": flows + keep})


def parse_requirements_blocks(text):
    """Extract ```requirements-json``` blocks -> (requirements, errors).
    Each needs id + text; core defaults True. Never raises."""
    errors = []
    blocks = schemalib.extract_structured_blocks(
        text or "", seclib.contract_fence("requirements")[0],
        on_error=errors.append)
    byid = {}
    for b in blocks:
        items = b.get("requirements") if isinstance(b.get("requirements"), list) else [b]
        for r in items or []:
            if not isinstance(r, dict):
                continue
            rid = str(r.get("id") or "").strip()
            txt = str(r.get("text") or "").strip()
            if not rid or not txt:
                errors.append("requirement needs id and text: %r" % r)
                continue
            byid[rid] = {"id": rid, "text": txt,
                         "core": bool(r.get("core", True))}
    return list(byid.values()), errors


def persist_requirements(app_dir, requirements):
    _write_json_atomic(os.path.join(app_dir, "requirements.json"),
                       {"schema_version": 1, "requirements": requirements})


def load_requirements(app_dir):
    try:
        with open(os.path.join(app_dir, "requirements.json"),
                  encoding="utf-8") as fh:
            data = json.load(fh)
        reqs = data.get("requirements")
        return reqs if isinstance(reqs, list) else []
    except (OSError, ValueError):
        return []


def _research_source_check(cfg, app, app_dir, final_output):
    """Local-model cross-check of research claims against the prompt's
    fetched URL text (runtime.research_source_check). Records unsupported
    claims to docs/research_verification.json + an incident; never blocks,
    never raises."""
    try:
        url_ctx = (cfg.get("_url_context") or "").strip()
        if not url_ctx or not (final_output or "").strip():
            return
        tag = (cfg.get("_resolved", {}) or {}).get("ollama_model") \
            or str(cget(cfg, "models.ollama", "") or "")
        if not tag or tag not in set(lmlib.installed_models_cached()):
            return
        ask = ("SOURCE TEXT (fetched from the user's own links):\n%s\n\n"
               "RESEARCH OUTPUT:\n%s\n\n"
               "List every factual claim in the RESEARCH OUTPUT that is NOT "
               "supported by the SOURCE TEXT and is not marked ASSUMPTION. "
               "One per line, prefixed 'UNSUPPORTED: '. If everything is "
               "supported or marked, reply exactly: ALL SUPPORTED."
               % (url_ctx[:8000], str(final_output)[:6000]))
        out, _err, code, _cmd = resolve_runner("local:%s" % tag)(cfg, ask, 120)
        if code != 0 or not (out or "").strip():
            return
        unsupported = [l.split("UNSUPPORTED:", 1)[1].strip()[:160]
                       for l in out.splitlines() if "UNSUPPORTED:" in l]
        os.makedirs(os.path.join(app_dir, "docs"), exist_ok=True)
        with open(os.path.join(app_dir, "docs", "research_verification.json"),
                  "w", encoding="utf-8") as fh:
            json.dump({"model": tag, "unsupported": unsupported}, fh, indent=2)
        if unsupported:
            emit("Research source check: %d unsupported claim(s) — see "
                 "docs/research_verification.json." % len(unsupported))
            if len(unsupported) >= 3:
                fllib.record_incident(app_dir, "research_unsupported",
                                      "; ".join(unsupported[:3]))
        else:
            emit("Research source check: all claims supported or marked.")
    except Exception as exc:  # noqa: BLE001 - advisory check only
        emit("WARN research source check skipped (%s)." % exc)


def find_task_cycles(tasks):
    """Detect dependency cycles in the backlog (§19: validate before build).
    Returns a list of "T-001 -> T-002 -> T-001" strings; [] when acyclic.
    depends_on ids that aren't in the backlog are ignored here. Iterative DFS —
    no recursion, never raises."""
    graph = {str(t.get("id")): [str(d) for d in (t.get("depends_on") or [])]
             for t in tasks if isinstance(t, dict)}
    cycles = []
    color = {k: 0 for k in graph}  # 0 white, 1 gray (on path), 2 black
    for start in graph:
        if color[start]:
            continue
        color[start] = 1
        path = [start]
        stack = [(start, iter(graph[start]))]
        while stack:
            node, it = stack[-1]
            descended = False
            for nxt in it:
                if nxt not in graph:
                    continue  # unknown dependency — not a cycle
                if color[nxt] == 1:
                    i = path.index(nxt)
                    cycles.append(" -> ".join(path[i:] + [nxt]))
                elif color[nxt] == 0:
                    color[nxt] = 1
                    path.append(nxt)
                    stack.append((nxt, iter(graph[nxt])))
                    descended = True
                    break
            if not descended:
                color[node] = 2
                path.pop()
                stack.pop()
    return cycles


def _write_json_atomic(path, payload):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2)
    os.replace(tmp, path)


def persist_tasks(app_dir, tasks, errors):
    """Write <app_dir>/tasks.json (§19): schema_version + the validated backlog +
    any parse/cycle errors (recorded, never fatal). Best-effort."""
    try:
        _write_json_atomic(os.path.join(app_dir, "tasks.json"),
                           {"schema_version": schemalib.SCHEMA_VERSION,
                            "tasks": tasks, "errors": errors})
    except OSError as exc:
        emit("WARN could not write tasks.json: %s" % exc)


def load_tasks(app_dir):
    try:
        with open(os.path.join(app_dir, "tasks.json"), encoding="utf-8") as fh:
            data = json.load(fh)
        return data.get("tasks", []) if isinstance(data, dict) else []
    except (OSError, ValueError):
        return []


def parse_interface_blocks(text):
    """Extract every ```interfaces-json``` block (one JSON object per block:
    an {"interfaces": [...]} wrapper or a single interface item). Returns
    (interfaces, errors) with items validated against
    REQUIRED_FIELDS["interface_item"], de-duplicated by name (last wins)."""
    errors = []
    blocks = schemalib.extract_structured_blocks(
        text or "", seclib.contract_fence("interfaces")[0],
        on_error=errors.append)
    byname = {}
    for b in blocks:
        items = b["interfaces"] if isinstance(b.get("interfaces"), list) else [b]
        for it in items:
            ok, missing = schemalib.validate_required_fields(
                it, schemalib.REQUIRED_FIELDS["interface_item"])
            if not ok:
                errors.append("interface %r missing required field(s): %s"
                              % (it.get("name") if isinstance(it, dict) else it,
                                 ", ".join(missing)))
                continue
            byname[str(it["name"])] = it
    return list(byname.values()), errors


def parse_decision_blocks(text):
    """Extract every ```decisions-json``` block (one JSON object per block:
    a {"decisions": [...]} wrapper or a single decision item). Returns
    (decisions, errors) with items validated against
    REQUIRED_FIELDS["decision_item"], de-duplicated by id (last emission wins,
    so the coordinator's final revision beats a draft). Never raises."""
    errors = []
    blocks = schemalib.extract_structured_blocks(
        text or "", seclib.contract_fence("decisions")[0],
        on_error=errors.append)
    byid = {}
    order = []
    for b in blocks:
        items = b["decisions"] if isinstance(b.get("decisions"), list) else [b]
        for d in items:
            ok, missing = schemalib.validate_required_fields(
                d, schemalib.REQUIRED_FIELDS["decision_item"])
            if not ok:
                errors.append("decision %r missing required field(s): %s"
                              % (d.get("id") if isinstance(d, dict) else d,
                                 ", ".join(missing)))
                continue
            d.setdefault("rationale", "")
            for lk in ("rejected_alternatives", "constraints"):
                v = d.get(lk)
                d[lk] = [str(x) for x in v] if isinstance(v, list) else \
                    ([str(v)] if v else [])
            did = str(d["id"])
            if did not in byid:
                order.append(did)
            byid[did] = d
    return [byid[i] for i in order], errors


def _decisions_path(app_dir):
    return os.path.join(app_dir, "decisions.json")


def load_decisions(app_dir):
    try:
        with open(_decisions_path(app_dir), encoding="utf-8") as fh:
            data = json.load(fh)
        return data.get("decisions", []) if isinstance(data, dict) else []
    except (OSError, ValueError):
        return []


def merge_decisions(app_dir, new_decisions):
    """Merge freshly-parsed decisions into <app_dir>/decisions.json.

    New ids are appended; a re-emitted id replaces its entry (last revision
    wins). When an entry's "supersedes" names an existing id, that older entry
    is MARKED superseded (superseded=true, superseded_by=<id>) rather than
    deleted, preserving the audit trail. Atomic write; returns the merged
    list. Best-effort — an unwritable file loses persistence, never the run."""
    existing = load_decisions(app_dir)
    byid = {str(d.get("id")): d for d in existing if isinstance(d, dict)}
    order = [str(d.get("id")) for d in existing if isinstance(d, dict)]
    for nd in new_decisions or []:
        did = str(nd.get("id"))
        sup = nd.get("supersedes")
        if sup is not None and str(sup) in byid and str(sup) != did:
            byid[str(sup)]["superseded"] = True
            byid[str(sup)]["superseded_by"] = did
        if did in byid:
            # Re-emission of an id: replace, but never lose a superseded mark
            # someone else already stamped on it.
            if byid[did].get("superseded") and not nd.get("superseded"):
                nd = dict(nd, superseded=True,
                          superseded_by=byid[did].get("superseded_by"))
            byid[did] = nd
        else:
            byid[did] = nd
            order.append(did)
    merged = [byid[i] for i in order]
    try:
        _write_json_atomic(_decisions_path(app_dir),
                           {"schema_version": schemalib.SCHEMA_VERSION,
                            "decisions": merged})
    except OSError as exc:
        emit("WARN could not write decisions.json: %s" % exc)
    return merged


def render_decisions_log(decisions):
    """Compact human/agent rendering of the decisions log: superseded entries
    are hidden, but each survivor shows what it replaced. '' when empty."""
    lines = []
    superseded = sum(1 for d in decisions or []
                     if isinstance(d, dict) and d.get("superseded"))
    for d in decisions or []:
        if not isinstance(d, dict) or d.get("superseded"):
            continue
        lines.append("- [%s] %s" % (d.get("id"), str(d.get("decision", "")).strip()))
        if d.get("rationale"):
            lines.append("  rationale: %s" % str(d["rationale"]).strip())
        if d.get("rejected_alternatives"):
            lines.append("  rejected: %s"
                         % "; ".join(str(x) for x in d["rejected_alternatives"]))
        if d.get("constraints"):
            lines.append("  constraints: %s"
                         % "; ".join(str(x) for x in d["constraints"]))
        if d.get("supersedes"):
            lines.append("  (supersedes %s)" % d["supersedes"])
    if not lines:
        return ""
    if superseded:
        lines.append("(%d superseded decision(s) hidden — see decisions.json "
                     "for history)" % superseded)
    return "\n".join(lines)


def parse_phase_summary_blocks(text):
    """Extract ```phase-summary-json``` block(s) from ``text``. Last emission
    wins (matches the decisions/tasks/interfaces convention: the coordinator's
    final revision beats a draft). Returns (summary_dict_or_None, errors)."""
    errors = []
    _sf, _sreq = seclib.contract_fence("phase_summary")
    blocks = schemalib.extract_structured_blocks(
        text or "", _sf,
        schemalib.REQUIRED_FIELDS[_sreq], on_error=errors.append)
    return (blocks[-1] if blocks else None), errors


def _phase_summaries_path(app_dir):
    return os.path.join(app_dir, "phase_summaries.json")


def load_phase_summaries(app_dir):
    try:
        with open(_phase_summaries_path(app_dir), encoding="utf-8") as fh:
            data = json.load(fh)
        return data.get("summaries", []) if isinstance(data, dict) else []
    except (OSError, ValueError):
        return []


def phase_summary_for(app_dir, key):
    for s in load_phase_summaries(app_dir):
        if isinstance(s, dict) and s.get("phase") == key:
            return s
    return None


def merge_phase_summary(app_dir, key, summary):
    """Persist/replace this phase's entry in <app_dir>/phase_summaries.json —
    one entry per phase key, in first-seen (workflow) order; re-running a
    phase (e.g. "Request Changes") replaces its entry in place rather than
    duplicating it. Atomic write; best-effort — never blocks the run."""
    existing = load_phase_summaries(app_dir)
    byphase = {s.get("phase"): s for s in existing if isinstance(s, dict)}
    order = [s.get("phase") for s in existing if isinstance(s, dict)]
    if key not in byphase:
        order.append(key)
    byphase[key] = summary
    merged = [byphase[k] for k in order]
    try:
        _write_json_atomic(_phase_summaries_path(app_dir),
                           {"schema_version": schemalib.SCHEMA_VERSION,
                            "summaries": merged})
    except OSError as exc:
        emit("WARN could not write phase_summaries.json: %s" % exc)
    return merged


def render_phase_summary(summary):
    """Compact rendering of one phase_summaries.json entry for prompt
    injection in place of that phase's raw transcript. '' when empty."""
    if not isinstance(summary, dict):
        return ""
    para = str(summary.get("one_paragraph_summary", "")).strip()
    if not para:
        return ""
    lines = [para]
    if summary.get("key_decisions"):
        lines.append("key decisions: %s"
                     % ", ".join(str(x) for x in summary["key_decisions"]))
    if summary.get("open_risks"):
        lines.append("open risks: %s"
                     % ", ".join(str(x) for x in summary["open_risks"]))
    return "\n".join(lines)


def persist_interfaces(app_dir, interfaces, errors):
    """Write <app_dir>/interfaces.json (§20). Best-effort."""
    try:
        _write_json_atomic(os.path.join(app_dir, "interfaces.json"),
                           {"schema_version": schemalib.SCHEMA_VERSION,
                            "interfaces": interfaces, "errors": errors})
    except OSError as exc:
        emit("WARN could not write interfaces.json: %s" % exc)


def load_interfaces(app_dir):
    try:
        with open(os.path.join(app_dir, "interfaces.json"), encoding="utf-8") as fh:
            data = json.load(fh)
        return data.get("interfaces", []) if isinstance(data, dict) else []
    except (OSError, ValueError):
        return []


def load_flows(app_dir):
    """The declared user journeys (flows.json), frozen from the planning phase
    and replayed by the UI-crawl gate. Missing/corrupt -> [] (never raises),
    mirroring load_tasks/load_interfaces."""
    try:
        with open(os.path.join(app_dir, "flows.json"), encoding="utf-8") as fh:
            data = json.load(fh)
        return data.get("flows", []) if isinstance(data, dict) else []
    except (OSError, ValueError):
        return []


# ---------------------------------------------------------------------------
# Generated-source secret scan (V2 spec §17/§23): a deterministic pass over the
# build output feeding the secret_hardcoded launch gate. Findings carry only the
# secret TYPE and file:line — never the value itself.
# ---------------------------------------------------------------------------
_SCAN_SKIP_DIRS = {".git", "node_modules", ".build", "build", "DerivedData",
                   "__pycache__", ".swiftpm", "Pods", ".venv", "venv"}
_SCAN_MAX_BYTES = 1024 * 1024
# Deterministic allowlist (§17): obvious test/mock placeholders are not findings.
_SCAN_ALLOW = ("redacted", "example", "placeholder", "your_", "your-", "dummy",
               "changeme", "sample", "<", "xxxx")
# Code expressions matched by the loose `assignment` pattern (getpass(), env
# lookups, dict access) are not literal secrets.
_SCAN_CODEISH = ("(", ")", "{", "}", "[", "]", ";", ",", "$")


def scan_build_secrets(build_dir):
    """Scan text files under ``build_dir`` with the shared _SECRET_PATTERNS and
    return one finding dict per hit (REQUIRED_FIELDS["finding"]: source=
    secret_scan, category=secret_hardcoded, severity=Critical). Skips .git and
    vendor dirs, binaries (NUL byte), and files >1MB; dedupes per file:line.
    Never raises."""
    findings = []
    if not build_dir or not os.path.isdir(build_dir):
        return findings
    seen = set()
    for dirpath, dirnames, filenames in os.walk(build_dir):
        dirnames[:] = sorted(d for d in dirnames if d not in _SCAN_SKIP_DIRS)
        for fn in sorted(filenames):
            path = os.path.join(dirpath, fn)
            rel = os.path.relpath(path, build_dir)
            try:
                if os.path.getsize(path) > _SCAN_MAX_BYTES:
                    continue
                with open(path, "rb") as fh:
                    raw = fh.read()
            except OSError:
                continue
            if b"\x00" in raw[:8000]:
                continue  # binary
            text = raw.decode("utf-8", errors="replace")
            for name, pat in schemalib._SECRET_PATTERNS:
                for m in pat.finditer(text):
                    if any(a in m.group(0).lower() for a in _SCAN_ALLOW):
                        continue
                    if name == "assignment" and any(c in m.group(1) for c in _SCAN_CODEISH):
                        continue
                    line = text.count("\n", 0, m.start()) + 1
                    if (rel, line) in seen:
                        continue
                    seen.add((rel, line))
                    findings.append({
                        "schema_version": schemalib.SCHEMA_VERSION,
                        "source": "secret_scan",
                        "category": "secret_hardcoded",
                        "severity": "Critical",
                        "confidence": "high",
                        # Type + location ONLY — never the matched value (§17).
                        "title": "Hardcoded secret (%s) at %s:%d" % (name, rel, line),
                        "file": rel,
                        "line": line,
                        "why": "A %s-shaped credential is committed in generated source." % name,
                        "fix": "Move the value to secure configuration (Keychain / env / "
                               "build settings) and rotate the exposed credential.",
                        "status": "open",
                    })
    return findings


def _docs_findings_path(app_dir):
    return os.path.join(app_dir, "docs", "findings.json")


def load_docs_findings(app_dir):
    """The persisted docs/findings.json list, or None if no scan/review ever
    wrote one (None lets the readiness gate say 'not run' honestly)."""
    try:
        with open(_docs_findings_path(app_dir), encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    if isinstance(data, dict) and isinstance(data.get("findings"), list):
        return data["findings"]
    return data if isinstance(data, list) else None


def merge_secret_findings(app_dir, new_findings):
    """Replace the secret_scan findings in docs/findings.json with this run's
    (findings from other sources are preserved). Returns the merged list;
    best-effort."""
    kept = [f for f in (load_docs_findings(app_dir) or [])
            if isinstance(f, dict) and f.get("source") != "secret_scan"]
    merged = kept + list(new_findings or [])
    try:
        os.makedirs(os.path.join(app_dir, "docs"), exist_ok=True)
        _write_json_atomic(_docs_findings_path(app_dir),
                           {"schema_version": schemalib.SCHEMA_VERSION,
                            "findings": merged})
    except OSError as exc:
        emit("WARN could not write docs/findings.json: %s" % exc)
    return merged


# ---------------------------------------------------------------------------
# Phase execution
# ---------------------------------------------------------------------------
CONSENSUS_RE = re.compile(r"CONSENSUS:\s*YES", re.IGNORECASE)
VOTE_RE = re.compile(r"VOTE_DECISION:\s*YES", re.IGNORECASE)
QUALITY_PASS_RE = re.compile(r"QUALITY:\s*PASS", re.IGNORECASE)
QUALITY_FAIL_RE = re.compile(r"QUALITY:\s*FAIL", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Forced-vote ballots: machine-tallied vote-json blocks (deterministic tally in
# Python; the legacy LLM tally survives only as a fallback when fewer than two
# ballots parse). Voter self-votes are invalid — the vote prompt's "you can't
# pick your own idea" rule is enforced here, not just requested.
# ---------------------------------------------------------------------------

def _normalize_vote_choice(raw, voters):
    """Map a ballot's free-text choice ("Codex", "codex", "local:glm4:9b")
    onto a participant id from ``voters``. None when it matches nobody."""
    text = str(raw or "").strip().strip("\"'").lower()
    if not text:
        return None
    for a in voters:
        if text == a.lower() or text == DISPLAY.get(a, a).lower():
            return a
    return None


def parse_vote_ballots(responses, voters, on_error=None):
    """``responses`` is {voter: reply_text}. Returns a list of valid ballots:
    {voter, choice, confidence, reason}. Invalid ballots (missing block, bad
    JSON, unknown choice, self-vote, bad confidence) are reported through
    ``on_error`` and skipped — never silently counted."""
    if on_error is None:
        on_error = lambda _msg: None
    ballots = []
    for voter, text in responses.items():
        blocks = schemalib.extract_structured_blocks(
            text, "vote-json", required_fields=["choice", "confidence"],
            on_error=lambda msg, v=voter: on_error("%s: %s" % (v, msg)))
        if not blocks:
            on_error("%s: no parseable vote-json ballot" % voter)
            continue
        b = blocks[-1]   # models sometimes restate; the final block is the ballot
        choice = _normalize_vote_choice(b.get("choice"), voters)
        if choice is None:
            on_error("%s: ballot names no known participant (%r)"
                     % (voter, b.get("choice")))
            continue
        if choice == voter:
            on_error("%s: self-vote is invalid" % voter)
            continue
        try:
            conf = int(b.get("confidence"))
        except (TypeError, ValueError):
            on_error("%s: confidence %r is not an integer" % (voter, b.get("confidence")))
            continue
        conf = max(1, min(5, conf))
        ballots.append({"voter": voter, "choice": choice, "confidence": conf,
                        "reason": str(b.get("reason") or "")[:400]})
    return ballots


def tally_votes(ballots, voters):
    """Confidence-weighted deterministic tally. Returns (winner, weights) —
    winner is None when there are no ballots. Ties break by total confidence,
    then coordinator preference, then roster order: same inputs, same winner,
    every time."""
    weights = {}
    for b in ballots:
        weights[b["choice"]] = weights.get(b["choice"], 0) + b["confidence"]
    if not weights:
        return None, {}

    def _rank(agent):
        pref = (COORDINATOR_PREFERENCE.index(agent)
                if agent in COORDINATOR_PREFERENCE else len(COORDINATOR_PREFERENCE))
        roster = voters.index(agent) if agent in voters else len(voters)
        return (-weights[agent], pref, roster, agent)

    return sorted(weights, key=_rank)[0], weights

# ---------------------------------------------------------------------------
# Audit findings: parse agent-emitted finding-json blocks, dedupe, rank, render.
# ---------------------------------------------------------------------------
SEV_RANK = {"Critical": 4, "High": 3, "Med": 2, "Low": 1}
_CONF_W = {"high": 1.0, "medium": 0.6, "low": 0.3}
_CAT_ORDER = {"security": 0, "bug": 1, "update": 2}
_CAT_PREFIX = {"security": "SEC", "bug": "BUG", "update": "UPD"}
# Cosmetic only: strip finding-json fences out of a human-facing summary blob.
# Parsing goes through schemas.extract_structured_blocks; this just deletes the
# fenced regions (lazy-to-first-close is correct for removal).
_FIND_STRIP_RE = re.compile(r"```finding-json\b.*?```", re.DOTALL)


def parse_finding_blocks(text):
    """Extract every ```finding-json``` block from text; normalize so malformed
    agent output never crashes the render.

    Uses schemas.extract_structured_blocks — a lazy ```finding-json\\s*(\\{.*?\\})```
    regex (the old approach) can't match array/multi-object bodies and swallows
    text across an unclosed fence, silently dropping real findings. The shared
    extractor scans fence-to-fence and recovers from truncated blocks."""
    out = []
    for d in schemalib.extract_structured_blocks(text or "", "finding-json"):
        sev = str(d.get("severity", "Med")).strip().title()
        sev = {"Medium": "Med", "Moderate": "Med", "Info": "Low",
               "Informational": "Low", "Critical": "Critical", "High": "High",
               "Med": "Med", "Low": "Low"}.get(sev, "Med")
        d["severity"] = sev if sev in SEV_RANK else "Med"
        conf = str(d.get("confidence", "medium")).strip().lower()
        d["confidence"] = conf if conf in _CONF_W else "medium"
        cat = str(d.get("category", "bug")).strip().lower()
        d["category"] = cat if cat in _CAT_ORDER else "bug"
        src = str(d.get("source", "audit")).strip().lower()
        d["source"] = src if src in schemalib.FINDING_SOURCE else "audit"
        d["file"] = str(d.get("file", "")).strip()
        d["title"] = str(d.get("title", "")).strip()
        if not d["title"]:
            continue
        out.append(d)
    return out


def dedup_findings(items):
    seen = {}
    for f in items:
        k = (f["category"], f["file"].lower(), f["title"].lower()[:60])
        cur = seen.get(k)
        if cur is None or _CONF_W[f["confidence"]] > _CONF_W[cur["confidence"]]:
            if cur and len(str(cur.get("fix", ""))) > len(str(f.get("fix", ""))):
                f = {**f, "fix": cur.get("fix", f.get("fix", ""))}
            seen[k] = f
    return list(seen.values())


def rank_findings(items):
    return sorted(items, key=lambda f: (
        -SEV_RANK[f["severity"]] * _CONF_W[f["confidence"]],
        _CAT_ORDER[f["category"]], f["file"]))


def _assign_ids(items):
    counters = {}
    for f in items:
        pre = _CAT_PREFIX[f["category"]]
        counters[pre] = counters.get(pre, 0) + 1
        f["id"] = "%s-%03d" % (pre, counters[pre])
    return items


def render_audit_report(findings, app, target_path, agents="", summary=""):
    """Render the prioritized AUDIT_REPORT.md from ranked+ID'd findings."""
    cats = ("security", "bug", "update")
    sevs = ("Critical", "High", "Med", "Low")
    counts = {s: {c: 0 for c in cats} for s in sevs}
    for f in findings:
        counts[f["severity"]][f["category"]] += 1
    def row(s):
        r = counts[s]
        tot = r["security"] + r["bug"] + r["update"]
        return "| %-8s | %d | %d | %d | **%d** |" % (s, r["security"], r["bug"], r["update"], tot)
    grand = len(findings)
    tot_sec = sum(counts[s]["security"] for s in sevs)
    tot_bug = sum(counts[s]["bug"] for s in sevs)
    tot_upd = sum(counts[s]["update"] for s in sevs)
    top = [f for f in findings if f["severity"] in ("Critical", "High")][:5]
    top_line = ", ".join("%s (%s)" % (f["id"], f["severity"]) for f in top) or "none"

    lines = []
    lines.append("# Audit Report — %s\n" % app)
    lines.append("_Target: `%s` · Read-only static review by the multi-agent "
                 "orchestrator%s._\n" % (target_path or "(unknown)",
                                         (" (agents: %s)" % agents) if agents else ""))
    lines.append("## Executive Summary\n")
    lines.append((summary.strip() or
                  "%d finding(s): %d security, %d bug, %d update. See the ranked list "
                  "below; verify each before acting." % (grand, tot_sec, tot_bug, tot_upd)) + "\n")
    lines.append("| Severity | Security | Bug | Update | Total |")
    lines.append("|----------|:-------:|:---:|:------:|:-----:|")
    for s in sevs:
        lines.append(row(s))
    lines.append("| **Total**| **%d** | **%d** | **%d** | **%d** |" % (tot_sec, tot_bug, tot_upd, grand))
    lines.append("\n**Top priorities:** %s\n" % top_line)

    def block(f):
        return ("### [%s] %s · %s · confidence %s\n"
                "- **Location:** `%s%s`\n- **Why it matters:** %s\n"
                "- **Recommended fix:** %s\n" % (
                    f["id"], f["title"], f["severity"], f["confidence"],
                    f["file"] or "(unspecified)",
                    (":%s" % f["line"]) if f.get("line") not in (None, "", 0) else "",
                    str(f.get("why", "")).strip() or "(not provided)",
                    str(f.get("fix", "")).strip() or "(not provided)"))
    def oneline(f):
        return "- [%s] %s — `%s%s` · %s · %s" % (
            f["id"], f["title"], f["file"] or "(unspecified)",
            (":%s" % f["line"]) if f.get("line") not in (None, "", 0) else "",
            f["severity"], str(f.get("fix", "")).strip()[:120])

    hi = [f for f in findings if f["severity"] in ("Critical", "High")]
    med = [f for f in findings if f["severity"] == "Med"]
    lo = [f for f in findings if f["severity"] == "Low"]
    lines.append("## Critical & High Findings\n")
    lines.append("\n".join(block(f) for f in hi) if hi else "_None._\n")
    lines.append("\n## Medium Findings\n")
    lines.append("\n".join(oneline(f) for f in med) if med else "_None._")
    lines.append("\n## Low / Modernization\n")
    lines.append("\n".join(oneline(f) for f in lo) if lo else "_None._")
    lines.append("\n## Methodology & Limitations\n")
    lines.append("Read-only static review of the target tree — no code was executed "
                 "and nothing in the target was modified. Findings are advisory; verify "
                 "before acting. Confidence reflects false-positive risk, not "
                 "exploitability.\n")
    return "\n".join(lines)


def _installed_local_models(cfg):
    """Installed Ollama model tags as a set, memoized per run on
    cfg["_installed_ollama_models"] (tests inject the key directly) and backed
    by the module TTL cache, so repeated roster builds never re-pay the
    `ollama list` subprocess. Previously the key was read here but never set
    anywhere — a dead read that always fell through to the uncached call."""
    if cfg.get("_installed_ollama_models") is None:
        tcxlib.TurnContext(cfg).installed_ollama_models = \
            list(lmlib.installed_models_cached())
    return set(cfg["_installed_ollama_models"])


def enabled_agents(cfg):
    """The active roster.

    Cloud agents default ON. The local Ollama channel is OFF unless enabled and
    a model exists in configuration. A roster may list multiple local models:

      - models.ollama: "qwen2.5-coder:7b" (legacy single model)
      - models.ollama_roster: "glm4:9b, qwen3:14b"

    When a roster exists, each entry becomes an explicit local identity,
    e.g. "local:glm4:9b". If no roster exists, legacy "ollama"
    (single identity) is used for backward compatibility.

    Sprint/time-budgeted workflows drop local participants unless
    runtime.local_models_in_sprints opts in — local turns can heavily affect
    wall-clock.
    """

    out = []
    local_enabled = cget(cfg, "agents.ollama_enabled", False)
    local_model = str(cget(cfg, "models.ollama", "") or "").strip()
    local_roster = _split_local_roster(
        cfg.get("_resolved", {}).get("ollama_roster",
                                        cget(cfg, "models.ollama_roster", []))
    )

    if not local_roster and local_model:
        local_roster = [local_model]

    skip_local = bool(cfg.get("_budget")) and \
        not bool(cget(cfg, "runtime.local_models_in_sprints", False))
    if skip_local and (local_roster or local_model):
        if not cfg.get("_noted_ollama_sprint_skip"):
            tcxlib.TurnContext(cfg).noted_ollama_sprint_skip = True
            emit("Sprint mode: excluding local Ollama participants from this time-budgeted "
                 "run (runtime.local_models_in_sprints=false).")

    if local_enabled and local_roster and not skip_local and \
            bool(cget(cfg, "runtime.skip_uninstalled_local_models", False)):
        installed = _installed_local_models(cfg)
        kept = [model for model in local_roster if model in installed]
        skipped = [model for model in local_roster if model not in installed]
        if skipped and not cfg.get("_noted_ollama_uninstalled_skip"):
            tcxlib.TurnContext(cfg).noted_ollama_uninstalled_skip = True
            emit("Local Ollama: skipping not-yet-pulled model(s): %s. "
                 "Use Settings -> Local Models or `ollama pull <id>` to add them."
                 % ", ".join(skipped))
        local_roster = kept

    # Hardware gate: bench roster models whose registry min_ram_gb exceeds this
    # machine's physical RAM. The registry carried the numbers as advisory
    # metadata (the GUI's fit labels); the engine now enforces them so a
    # too-big model can't be selected into a run and swap the machine.
    if local_enabled and local_roster and not skip_local and \
            bool(cget(cfg, "runtime.enforce_local_ram_gate", True)):
        total_gb = lmlib.total_ram_gb()
        if total_gb > 0:
            mins = {str(m.get("id")): m.get("min_ram_gb")
                    for m in lmlib.load_registry(HERE).get("models", [])}
            too_big = [m for m in local_roster
                       if isinstance(mins.get(m), (int, float))
                       and float(mins[m]) > total_gb]
            if too_big:
                local_roster = [m for m in local_roster if m not in too_big]
                if not cfg.get("_noted_local_ram_gate"):
                    tcxlib.TurnContext(cfg).noted_local_ram_gate = True
                    emit("Local Ollama: benching %s — registry min RAM exceeds "
                         "this machine's %d GB (disable with "
                         "runtime.enforce_local_ram_gate: false)."
                         % (", ".join(too_big), total_gb))

    # models.local_active_limit: how many roster models actually join runs as
    # participants. Installed models win the slots first (in roster order),
    # then not-yet-pulled ones. 0/missing/garbage = no limit (legacy behavior).
    if local_enabled and local_roster and not skip_local:
        try:
            limit = int(cget(cfg, "models.local_active_limit", 0) or 0)
        except (TypeError, ValueError):
            limit = 0
        if limit > 0 and len(local_roster) > limit:
            installed = _installed_local_models(cfg)
            ordered = ([m for m in local_roster if m in installed]
                       + [m for m in local_roster if m not in installed])
            dropped = ordered[limit:]
            local_roster = ordered[:limit]
            if dropped and not cfg.get("_noted_local_active_limit"):
                tcxlib.TurnContext(cfg).noted_local_active_limit = True
                emit("Local Ollama: models.local_active_limit=%d — active: %s. "
                     "Benched: %s (raise the limit in Settings to include them)."
                     % (limit, ", ".join(local_roster), ", ".join(dropped)))

    for a in AGENT_ORDER:
        if not cget(cfg, "agents.%s_enabled" % a, a != "ollama"):
            continue
        if a == "gemini" and cfg.get("_gemini_disabled_reason"):
            # Startup probe said gemini can't run headless right now (§4.7):
            # keep it out of the roster instead of failing every turn.
            continue
        if a != "ollama":
            out.append(a)

    if local_enabled and not skip_local:
        if local_roster:
            for model in local_roster:
                out.append("local:%s" % model)
        elif local_model:
            out.append("ollama")

    # Per-phase participant filter (model_routing.json "agents"). Only active
    # inside a routed phase (_phase_key is set by _apply_phase_routing);
    # preflight/doctor calls see the unfiltered roster. Fail-open by design.
    phase_key = cfg.get("_phase_key")
    routing = cfg.get("_routing")
    if phase_key and routing:
        out, note = mrlib.filter_agents(routing, phase_key, out)
        if note and tcxlib.TurnContext(cfg).note_once("routing_filter",
                                                      phase_key):
            emit(note)
    return out


# Coordinator/integrator preference. Claude first: it's a strong synthesizer AND
# runs reliably headless. Gemini/Antigravity is last because agy can't run without
# a controlling terminal, so it must never be the sole integrator in an autonomous
# run (the integration + consensus step has to actually execute). "ollama" is
# deliberately ABSENT (V2 spec §10): a local model never coordinates while any
# cloud agent is enabled — it can only fall through to coordinator via
# _pick_coordinator's last-resort active[0] when the roster is local-only.
COORDINATOR_PREFERENCE = ("claude", "codex", "gemini")


def coordinator_agent(active):
    for pref in COORDINATOR_PREFERENCE:
        if pref in active:
            return pref
    return None


# ---------------------------------------------------------------------------
# Parallel build: roster, lanes, and file-tree snapshot
# ---------------------------------------------------------------------------
# Each parallel build worker owns one lane so concurrent workers write disjoint
# files. Integration/shared files (Xcode project, manifests, app entry point)
# are reserved for the integrator so the app still assembles into one thing.
BUILD_LANES = [
    "the core data model + domain logic — data structures, state management, "
    "persistence, and business rules",
    "the primary UI — the main screens the user sees and taps through, plus "
    "navigation and layout",
    "supporting pieces — services/networking, utilities, secondary screens, and "
    "lightweight tests",
    "polish + resilience — settings, empty/loading/error states, edge cases, and "
    "wiring up loose ends",
]

# Stable ids for the lanes above (V2 spec §18.2), 1:1 with BUILD_LANES. These
# are the owner_lane / owning_lane values the tasks.json and interfaces.json
# contracts use, so backlog items route to the right parallel worker.
BUILD_LANE_IDS = ["data_domain", "primary_ui", "services_utilities",
                  "polish_resilience"]

INTEGRATION_FILES = (
    "the Xcode project (*.xcodeproj / project.pbxproj), the dependency manifest "
    "(Package.swift / Podfile / package.json), the app entry point (@main App / "
    "AppDelegate / main()), and any shared top-level config"
)


def _agent_available(agent, cfg=None):
    """True if the CLI (or local runtime) backing this agent is invokable.

    For local identities this also requires the SPECIFIC roster model to
    already be pulled, not just the Ollama server being reachable — a live
    server with the configured model missing fails every turn exactly like no
    server at all, so treating "server up" alone as "available" was a false
    positive. ``local:<tag>`` carries its tag directly; the legacy bare
    "ollama" identity needs ``cfg`` (models.ollama) to know which tag to check
    — with no cfg there's nothing to check against, so it degrades to the old
    server-only behavior."""
    if agent == "ollama" or (isinstance(agent, str) and agent.startswith("local:")):
        if not (which("ollama") and _ollama_up()):
            return False
        if agent.startswith("local:"):
            tag = agent[len("local:"):]
        elif cfg is not None:
            tag = str(cget(cfg, "models.ollama", "") or "").strip()
        else:
            tag = ""
        if not tag:
            return True
        installed = (cfg.get("_installed_ollama_models") if cfg else None) \
            or lmlib.installed_models_cached()
        return tag in installed
    if agent == "gemini":
        return bool(which("agy") or which("gemini"))
    return bool(which(agent))


def build_worker_roster(cfg, active):
    """Decide who builds in parallel this build phase.

    With >=2 distinct agent CLIs installed, each becomes one worker owning a
    lane. With only ONE installed, replicate it into `build_parallel_workers`
    concurrent workers (e.g. three Codex workers) so the build is still parallel
    and fast today. With none installed, return the enabled agents unchanged so
    call_agent surfaces a clear error.

    Either way the roster covers EVERY build lane: the planner is told all of
    BUILD_LANE_IDS are valid owner_lane values, so with fewer workers than lanes
    a lane like polish_resilience would own no worker and its tasks.json items
    would be shown to nobody and silently never built. Extra slots beyond the
    distinct CLIs round-robin the available CLIs."""
    target = int(cget(cfg, "runtime.build_parallel_workers", 3) or 3)
    n_lanes = len(BUILD_LANE_IDS)
    avail = [a for a in ordered_agents(active) if _agent_available(a, cfg)]
    # Local models debate well (10-30s turns) but a 14-30B local WRITING a
    # whole lane takes 25+ min per iteration — and each build round waits for
    # the slowest worker. Keep locals out of the lanes by default; they still
    # participate in every discussion phase and serve the fallback ladder.
    if not bool(cget(cfg, "runtime.locals_in_build_lanes", False)):
        cloud_only = [a for a in avail
                      if not (a == "ollama" or str(a).startswith("local:"))]
        if cloud_only:
            if len(cloud_only) < len(avail) and not cfg.get("_noted_local_lane_skip"):
                tcxlib.TurnContext(cfg).noted_local_lane_skip = True
                emit("Build lanes: excluding local model(s) from lane duty "
                     "(runtime.locals_in_build_lanes=false) — they keep "
                     "discussing and backstopping fallbacks.")
            avail = cloud_only
    if len(avail) >= 2:
        n = max(len(avail), n_lanes)
        agents = [avail[i % len(avail)] for i in range(n)]
    elif len(avail) == 1:
        agents = [avail[0]] * max(1, target, n_lanes)
    else:
        agents = ordered_agents(active) or list(active)

    totals = {}
    for a in agents:
        totals[a] = totals.get(a, 0) + 1
    seen = {}
    roster = []
    for a in agents:
        i = len(roster)
        lane = BUILD_LANES[i % len(BUILD_LANES)]
        lane_id = BUILD_LANE_IDS[i % len(BUILD_LANE_IDS)]
        if totals[a] > 1:
            seen[a] = seen.get(a, 0) + 1
            tag = chr(ord("A") + seen[a] - 1)
            label = "%s %s" % (DISPLAY[a], tag)
            slug = "%s-%s" % (a, tag.lower())
        else:
            label = DISPLAY[a]
            slug = a
        roster.append({"agent": a, "label": label, "slug": slug, "lane": lane,
                       "lane_id": lane_id})
    return roster


def _task_waves(backlog):
    """Topo-layer tasks by depends_on (Kahn layers). Unknown deps count as
    satisfied; any cycle leftovers become a final wave. [] for tiny backlogs
    (< 4 tasks — slicing adds ceremony without value there)."""
    tasks = [t for t in (backlog or []) if isinstance(t, dict) and t.get("id")]
    if len(tasks) < 4:
        return []
    ids = {str(t["id"]) for t in tasks}
    remaining = {str(t["id"]): t for t in tasks}
    placed = set()
    waves = []
    while remaining:
        ready = [t for tid, t in remaining.items()
                 if all(str(d) in placed or str(d) not in ids
                        for d in (t.get("depends_on") or []))]
        if not ready:   # cycle: dump the rest as one final wave
            waves.append(sorted(remaining.values(), key=lambda t: str(t["id"])))
            break
        ready.sort(key=lambda t: str(t["id"]))
        waves.append(ready)
        for t in ready:
            placed.add(str(t["id"]))
            remaining.pop(str(t["id"]), None)
    return waves if len(waves) > 1 else []


def _claim_tasks_for_iteration(roster, backlog, rnd):
    """§19 dynamic task claiming: assign this iteration's still-open
    tasks.json items to workers, replacing the old static "every worker just
    filters backlog by owner_lane == its lane_id" slice.

    Sticky across iterations (a claim persists once made) so a worker keeps
    building the same tasks call to call — that continuity is what the
    codex/claude session-reuse machinery depends on. Stale-claim reversion:
    a claim is cleared the moment its worker is no longer in THIS iteration's
    roster, so a dropped-out worker's tasks don't sit permanently orphaned
    (today's roster is fixed for the whole build phase, so this is a no-op
    in practice — it's a correctness net for whenever roster composition
    becomes dynamic, not dead code by design).

    New/reverted tasks are handed out preferring a worker whose lane_id
    matches the task's owner_lane (same grouping the old static filter used,
    just now split — not duplicated — across every worker that shares a
    lane), then round-robined across the WHOLE roster for anything left
    over: an orphaned/unknown lane label, or a lane with no worker this
    iteration. That's the actual fix for "statically sliced" — a lane with
    nothing of its own no longer leaves its worker idle while another lane
    backs up.

    Mutates claimed_by/claimed_at on backlog's task dicts in place (the
    caller persists them); returns {worker_slug: [claimed task dicts]}."""
    roster_slugs = {w["slug"] for w in roster}
    reverted = 0
    for t in backlog:
        cb = t.get("claimed_by")
        if cb and cb not in roster_slugs:
            t["claimed_by"] = None
            t["claimed_at"] = None
            reverted += 1
    if reverted:
        emit("CLAIM: reverted %d stale claim(s) — claiming worker no longer "
             "in the roster." % reverted)

    claims = {w["slug"]: [] for w in roster}
    for t in backlog:
        cb = t.get("claimed_by")
        if cb in roster_slugs:
            claims[cb].append(t)

    open_tasks = [t for t in backlog if not t.get("claimed_by")
                 and str(t.get("status", "")).lower() != "done"]
    by_lane = {}
    for t in open_tasks:
        by_lane.setdefault(str(t.get("owner_lane")), []).append(t)
    leftover = []
    for lane_id in BUILD_LANE_IDS:
        workers = [w for w in roster if w.get("lane_id") == lane_id]
        lane_tasks = by_lane.pop(lane_id, [])
        if not workers:
            leftover.extend(lane_tasks)
            continue
        for i, t in enumerate(lane_tasks):
            w = workers[i % len(workers)]
            t["claimed_by"] = w["slug"]
            t["claimed_at"] = rnd
            claims[w["slug"]].append(t)
    for lane_tasks in by_lane.values():   # unknown/missing owner_lane labels
        leftover.extend(lane_tasks)
    if leftover:
        emit("CLAIM: %d task(s) redistributed across the roster (unmatched "
             "or overflow lane)." % len(leftover))
    for i, t in enumerate(leftover):
        w = roster[i % len(roster)]
        t["claimed_by"] = w["slug"]
        t["claimed_at"] = rnd
        claims[w["slug"]].append(t)
    return claims


def _flows_contract_block(flows):
    """The DISTINCT control tokens the frozen flows.json journeys tap into or
    type into, rendered as a binding contract for every build worker: each token
    MUST be exposed VERBATIM as that control's .accessibilityIdentifier, because
    the UI-crawl gate matches controls by these exact strings. Reconciles the
    flow-token/build-identifier drift AT THE FIRST build pass instead of only
    reactively via the release-gate repair message. assert_exists tokens are
    excluded — those name user-visible content, not a control to identify. ''
    when there are no such tokens (or no flows)."""
    tokens = []
    seen = set()
    for flow in flows or []:
        if not isinstance(flow, dict):
            continue
        for step in flow.get("steps") or []:
            if not isinstance(step, dict):
                continue
            tok = step.get("tap") or step.get("into")
            if isinstance(tok, str) and tok and tok not in seen:
                seen.add(tok)
                tokens.append(tok)
    if not tokens:
        return ""
    return ("===== DECLARED FLOW TOKENS (flows.json — BINDING CONTRACT) =====\n"
            "The UI-crawl release gate replays these frozen user journeys and "
            "matches controls by these EXACT strings. For EVERY token below, set "
            "it VERBATIM as the .accessibilityIdentifier of the control that "
            "fulfills that step, AND keep a descriptive .accessibilityLabel for "
            "VoiceOver — set BOTH; never replace the label with the token. Each "
            "token's control must be DIRECTLY visible and hittable on its "
            "screen — never hidden behind a swipe action or long-press menu "
            "(the gate cannot perform gestures to reveal it). A token you "
            "don't expose as an identifier is a journey that FAILS:\n"
            + "\n".join("  - %s" % t for t in tokens))


def _worker_contract_block(backlog, claimed, interfaces, wave_note="",
                           flows_contract=""):
    """This worker's CLAIMED tasks (see _claim_tasks_for_iteration) plus the
    full interfaces.json contract and the flows.json identifier contract,
    rendered for one build-worker prompt (§19/§20). backlog is only consulted to
    decide whether the project has a task backlog at all; '' when there's neither
    a backlog nor interfaces nor flow tokens."""
    parts = []
    if wave_note:
        parts.append("===== BUILD WAVE =====\n" + wave_note)
    if backlog:
        if claimed:
            parts.append("===== YOUR CLAIMED TASKS (from tasks.json) =====\n"
                         "Work these tasks (respect depends_on order; meet the "
                         "acceptance criteria):\n" + json.dumps(claimed, indent=2))
        else:
            parts.append("===== YOUR CLAIMED TASKS (from tasks.json) =====\n"
                         "(nothing claimed for you this iteration — support "
                         "the other lanes and the integrator)")
    if interfaces:
        parts.append("===== SHARED INTERFACE CONTRACT (interfaces.json) =====\n"
                     "Code against these EXACT names/signatures; if one must "
                     "change, ask the integrator instead of diverging:\n"
                     + json.dumps(interfaces, indent=2))
    if flows_contract:
        parts.append(flows_contract)
    return "\n\n".join(parts)


def _pick_coordinator(cfg, active):
    """Integrator preference: a reliable, installed agent first (so the decision
    turn actually runs), otherwise deterministic fallback."""
    avail = [a for a in COORDINATOR_PREFERENCE if a in active and _agent_available(a, cfg)]
    if avail:
        return avail[0]
    return coordinator_agent(active) or (active[0] if active else None)


def _agent_health(cfg, agent, health_key=None):
    """Return the mutable health record call_agent() will use for this identity."""
    key = health_key or agent
    return tcxlib.TurnContext(cfg).health_map().setdefault(
        key, reslib.new_health())


def _agent_in_cooldown(cfg, agent, health_key=None, now=None):
    return reslib.in_cooldown(_agent_health(cfg, agent, health_key),
                              time.time() if now is None else now)


def _coordinator_candidates(cfg, active, preferred=None, require_healthy=False):
    """Ordered coordinator/integrator candidates for a decision barrier.

    The normal coordinator preference still applies, but a preferred coordinator
    gets first shot. When require_healthy is true, agents currently in the
    circuit-breaker cooldown are skipped so a build cannot keep selecting the
    same known-bad integrator forever.
    """
    ordered = []
    for a in [preferred] + list(COORDINATOR_PREFERENCE) + ordered_agents(active):
        if not a or a not in active or a in ordered:
            continue
        # Local models coordinate only when no cloud agent is enabled.
        if (a == "ollama" or (isinstance(a, str) and a.startswith("local:"))) \
                and any(c in active for c in COORDINATOR_PREFERENCE):
            continue
        if require_healthy and _agent_in_cooldown(cfg, a):
            continue
        ordered.append(a)
    return ordered


def _pick_live_coordinator(cfg, active, preferred=None):
    candidates = _coordinator_candidates(
        cfg, active, preferred=preferred,
        require_healthy=bool(cget(cfg, "runtime.coordinator_failover_enabled", True)))
    return candidates[0] if candidates else None


def _build_file_tree(build_dir, max_entries=200):
    """A compact snapshot of what's already in app_build so agents can see what
    exists (and don't rebuild it). Skips noise dirs; caps output length."""
    if not build_dir or not os.path.isdir(build_dir):
        return "(app_build is empty — nothing built yet)"
    skip = {".git", "node_modules", ".build", "build", "DerivedData", "__pycache__",
            ".swiftpm", "Pods"}
    lines = []
    for dirpath, dirnames, filenames in os.walk(build_dir):
        dirnames[:] = sorted(d for d in dirnames if d not in skip)
        rel = os.path.relpath(dirpath, build_dir)
        prefix = "" if rel == "." else rel + "/"
        for fn in sorted(filenames):
            if fn.startswith("."):
                continue
            lines.append(prefix + fn)
            if len(lines) >= max_entries:
                lines.append("... (truncated)")
                return "\n".join(lines)
    return "\n".join(lines) if lines else "(app_build is empty — nothing built yet)"


_AUDIT_SKIP = {".git", "node_modules", ".build", "build", "DerivedData",
               "__pycache__", ".swiftpm", "Pods", ".venv", "venv", "dist",
               ".next", "vendor", "Carthage"}
_AUDIT_PRIORITY = {"package.swift", "podfile", "podfile.lock", "requirements.txt",
                   "pyproject.toml", "poetry.lock", "package.json",
                   "package-lock.json", "yarn.lock", "info.plist", "project.yml",
                   ".env", "dockerfile"}
_AUDIT_SRC_EXT = (".swift", ".py", ".js", ".ts", ".jsx", ".tsx", ".m", ".mm",
                  ".plist", ".json", ".yml", ".yaml", ".rb", ".go", ".java",
                  ".kt", ".c", ".h", ".cpp", ".cs", ".php", ".sh", ".entitlements")


def build_target_digest(root, tree_max=400, per_file_cap=8000, total_cap=120000):
    """A read-only digest of a pre-existing TARGET codebase for audit phases: a
    file tree plus char-budgeted bodies of source/manifest files (manifests,
    Info.plist, entry points prioritized first). Pure open().read() — never writes.
    Returns "" if the target is missing."""
    if not root or not os.path.isdir(root):
        return ""
    tree, files, truncated = [], [], False
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in _AUDIT_SKIP)
        rel = os.path.relpath(dirpath, root)
        prefix = "" if rel == "." else rel + "/"
        for fn in sorted(filenames):
            if fn.startswith(".") and fn.lower() not in _AUDIT_PRIORITY:
                continue
            tree.append(prefix + fn)
            low = fn.lower()
            if low in _AUDIT_PRIORITY or low.endswith(_AUDIT_SRC_EXT):
                files.append((prefix + fn, os.path.join(dirpath, fn)))
            if len(tree) >= tree_max:
                tree.append("... (tree truncated)")
                truncated = True
                break
        if truncated:
            break
    # Manifests / entry-point files first, then the rest.
    files.sort(key=lambda t: 0 if os.path.basename(t[0]).lower() in _AUDIT_PRIORITY else 1)
    used, blocks = 0, []
    for rel, path in files:
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                body = fh.read()[:per_file_cap]
        except OSError:
            continue
        chunk = "\n----- %s -----\n%s\n" % (rel, body)
        if used + len(chunk) > total_cap:
            blocks.append("\n... (file-body budget reached; remaining files are "
                          "listed in the tree above) ...\n")
            break
        blocks.append(chunk)
        used += len(chunk)
    return ("\n\n===== TARGET CODEBASE (READ-ONLY) =====\n"
            "You are auditing this pre-existing codebase. It is READ-ONLY — do not "
            "propose editing it in place; only describe findings.\nRoot: %s\n\n"
            "--- FILE TREE ---\n%s\n\n--- KEY FILE CONTENTS ---\n%s"
            % (root, "\n".join(tree), "".join(blocks)))


def build_portfolio_digest(paths, overall_cap=200000):
    """A combined read-only digest of MANY repos (library_mining), each capped so
    the whole portfolio fits one context. Pure reads; never writes."""
    paths = [p for p in (paths or []) if p and os.path.isdir(p)]
    if not paths:
        return ""
    per = max(15000, overall_cap // len(paths))
    blocks = []
    for p in paths:
        d = build_target_digest(p, tree_max=180, per_file_cap=3500, total_cap=per)
        if d:
            name = os.path.basename(p.rstrip("/")) or p
            blocks.append("\n\n########## REPO: %s ##########%s" % (name, d))
    return ("\n\n===== PORTFOLIO (%d repos, READ-ONLY) =====\n"
            "You are analyzing these repositories TOGETHER to find shared/reusable "
            "patterns that could be extracted into a library. Do not propose editing "
            "them in place.%s" % (len(paths), "".join(blocks)))


def fix_ios_signing(build_dir, team="", style="Automatic", bundle_prefix=""):
    """Deterministically make any generated Xcode project under ``build_dir``
    installable on a real iPhone.

    Generated projects frequently ship with ``CODE_SIGNING_ALLOWED = NO`` /
    ``CODE_SIGNING_REQUIRED = NO`` (and Manual style + empty team) so simulator
    builds work without an account — but that produces an UNSIGNED executable
    that a physical device rejects ("The executable is not codesigned").

    This walks every ``*.xcodeproj/project.pbxproj`` and forces signing on. It
    is idempotent: running it on an already-correct project is a no-op.
    Returns the list of pbxproj paths it actually modified.
    """
    changed = []
    if not build_dir or not os.path.isdir(build_dir):
        return changed
    style = style or "Automatic"
    for dirpath, _dirnames, filenames in os.walk(build_dir):
        if not dirpath.endswith(".xcodeproj") or "project.pbxproj" not in filenames:
            continue
        path = os.path.join(dirpath, "project.pbxproj")
        try:
            with open(path, encoding="utf-8") as fh:
                text = fh.read()
        except OSError:
            continue
        orig = text
        text = re.sub(r"CODE_SIGNING_ALLOWED\s*=\s*NO\s*;",
                      "CODE_SIGNING_ALLOWED = YES;", text)
        text = re.sub(r"CODE_SIGNING_REQUIRED\s*=\s*NO\s*;",
                      "CODE_SIGNING_REQUIRED = YES;", text)
        # Prefer automatic signing so a team/profile resolves from the account
        # signed into Xcode rather than requiring a hand-managed profile.
        text = re.sub(r"CODE_SIGN_STYLE\s*=\s*Manual\s*;",
                      "CODE_SIGN_STYLE = %s;" % style, text)
        if team:
            team_val = team.strip()
            # Xcode writes team IDs bare (alnum); quote anything else so the
            # pbxproj stays valid.
            repl = team_val if re.fullmatch(r"[A-Za-z0-9]+", team_val) \
                else '"%s"' % team_val.replace('"', "")
            team_line = "DEVELOPMENT_TEAM = %s;" % repl
            # Point every existing assignment at the team — the old
            # `(""|"")` alternation was two identical dead branches that only
            # matched an empty "" value, so a stale or bare team id slipped
            # through. `[^;]*` covers empty, quoted, and bare-id forms.
            text = re.sub(r'DEVELOPMENT_TEAM\s*=\s*[^;]*;', team_line, text)
            # If the key is absent entirely (the common case for generated
            # projects — the old regex left these unsigned), add it to each
            # target that declares a bundle id.
            if "DEVELOPMENT_TEAM" not in text:
                text = re.sub(
                    r'(PRODUCT_BUNDLE_IDENTIFIER\s*=\s*[^;]*;)',
                    lambda m: m.group(1) + "\n\t\t\t\t" + team_line, text)
        if bundle_prefix:
            text = re.sub(r'PRODUCT_BUNDLE_IDENTIFIER\s*=\s*com\.local\.',
                          "PRODUCT_BUNDLE_IDENTIFIER = %s." % bundle_prefix.rstrip("."),
                          text)
        if text != orig:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(text)
            changed.append(path)
    return changed


# ---------------------------------------------------------------------------
# Git-backed build (V2 spec §5.1 / §5.7): app_build is a PERSISTENT git repo for
# the project's lifetime. Each build iteration is committed, so there is a real,
# rollback-able, crash-durable history of what was built (and version history for
# iterate-mode). Best-effort — if git is unavailable the build proceeds unchanged.
# ---------------------------------------------------------------------------
def _git(build_dir, *args, timeout=60):
    """Run a git command in build_dir; return (code, out, err). Never raises.

    Routed through procutil so a git subcommand that hangs (e.g. a commit hook or
    credential helper that spawns a pipe-inheriting grandchild) is killed by
    process group on timeout instead of deadlocking the reap."""
    try:
        out, err, code = procutil.run_capture(["git", "-C", build_dir] + list(args),
                                              timeout=timeout)
        return code, out, err
    except subprocess.TimeoutExpired:
        return 124, "", "git timed out after %ss" % timeout
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, "", str(exc)


# Build artifacts and secret-shaped files that must never enter the persistent
# build repo. Re-asserted before every `git add -A` so a missing or agent-edited
# .gitignore can't let DerivedData / node_modules / a stray key slip in.
_BUILD_GITIGNORE_RULES = [
    "DerivedData/", "build/", ".build/", "Pods/", ".gradle/", "node_modules/",
    "*.xcuserstate", ".DS_Store", "*.log",
    "*.pem", "*.key", "*.p12", ".env", ".env.*", "gemini_api_key", "*_api_key",
]


def _ensure_build_gitignore(build_dir):
    """Write/restore the build repo's .gitignore so `git add -A` can't stage
    artifacts or secrets. Preserves any extra user rules; only appends missing
    managed rules. Best-effort, never raises."""
    gi = os.path.join(build_dir, ".gitignore")
    try:
        existing = ""
        if os.path.exists(gi):
            with open(gi, encoding="utf-8") as fh:
                existing = fh.read()
        have = set(existing.splitlines())
        missing = [r for r in _BUILD_GITIGNORE_RULES if r not in have]
        if not existing:
            with open(gi, "w", encoding="utf-8") as fh:
                fh.write("# Managed by the orchestrator — build artifacts + secrets.\n")
                fh.write("\n".join(_BUILD_GITIGNORE_RULES) + "\n")
        elif missing:
            sep = "" if existing.endswith("\n") else "\n"
            with open(gi, "a", encoding="utf-8") as fh:
                fh.write(sep + "\n".join(missing) + "\n")
    except OSError:
        pass


_WORKSPACE_GITIGNORE_RULES = [".orchestrator_runtime/"]


def _ensure_workspace_gitignore(root):
    """Write/restore <root>/.gitignore (the factory workspace `run.sh` may
    `git add -A` + commit) so .orchestrator_runtime/ — worktrees and
    live_log.jsonl, pure scratch/log content — never lands in that history.
    Same preserve-extra-rules, append-only-missing idiom as
    _ensure_build_gitignore; best-effort, never raises."""
    gi = os.path.join(root, ".gitignore")
    try:
        existing = ""
        if os.path.exists(gi):
            with open(gi, encoding="utf-8") as fh:
                existing = fh.read()
        have = set(existing.splitlines())
        missing = [r for r in _WORKSPACE_GITIGNORE_RULES if r not in have]
        if not existing:
            with open(gi, "w", encoding="utf-8") as fh:
                fh.write("# Managed by the orchestrator — runtime scratch/log content.\n")
                fh.write("\n".join(_WORKSPACE_GITIGNORE_RULES) + "\n")
        elif missing:
            sep = "" if existing.endswith("\n") else "\n"
            with open(gi, "a", encoding="utf-8") as fh:
                fh.write(sep + "\n".join(missing) + "\n")
    except OSError:
        pass


def ensure_build_repo(build_dir):
    """Initialize app_build as a git repo once (idempotent). Adds a .gitignore for
    build artifacts and an empty initial commit so later commits always have a
    parent. Returns True if the dir is a usable git repo afterward."""
    if not build_dir or not shutil.which("git"):
        return False
    if os.path.isdir(os.path.join(build_dir, ".git")):
        return True
    os.makedirs(build_dir, exist_ok=True)
    if _git(build_dir, "init", "-q")[0] != 0:
        return False
    _git(build_dir, "config", "user.email", "orchestrator@local")
    _git(build_dir, "config", "user.name", "Orchestrator")
    _ensure_build_gitignore(build_dir)
    _git(build_dir, "add", "-A")
    _git(build_dir, "commit", "-q", "--allow-empty", "-m", "orchestrator: build repo initialized")
    return True


def commit_build_state(build_dir, message):
    """Commit whatever the workers wrote this iteration. No-op if nothing changed
    or the dir isn't a git repo. Returns the short commit sha or ''."""
    if not build_dir or not os.path.isdir(os.path.join(build_dir, ".git")):
        return ""
    # Re-assert the ignore rules first: agents may have deleted or rewritten
    # .gitignore mid-build, and a blind add -A would then commit artifacts/secrets.
    _ensure_build_gitignore(build_dir)
    _git(build_dir, "add", "-A")
    # Only commit if there's something staged (avoid empty commits per iteration).
    if _git(build_dir, "diff", "--cached", "--quiet")[0] == 0:
        return ""
    if _git(build_dir, "commit", "-q", "-m", message)[0] != 0:
        return ""
    return _git(build_dir, "rev-parse", "--short", "HEAD")[1].strip()


def tag_build_run(build_dir, tag):
    """Tag the current build HEAD (e.g. 'run-0001'), best-effort."""
    if build_dir and os.path.isdir(os.path.join(build_dir, ".git")) and tag:
        _git(build_dir, "tag", "-f", tag)


# ---------------------------------------------------------------------------
# Worktree-isolated build lanes (V2 spec §5.2-5.5). Each lane builds in its own
# git worktree off the current build HEAD, then lanes are merged back in a
# deterministic order — so concurrent workers can't silently clobber each other's
# files. Gated by runtime.worktree_isolation; ANY failure returns {} so the caller
# transparently falls back to the proven direct-write build.
# ---------------------------------------------------------------------------
def _orchestrator_runtime_dir(app_dir):
    """<app_dir>/.orchestrator_runtime — the spec's scratch/log location
    (worktrees, live_log.jsonl), kept out of any git history the workspace
    or a generated app might have (see _ensure_workspace_gitignore)."""
    return os.path.join(app_dir, ".orchestrator_runtime")


def _worktree_root(build_dir):
    return os.path.join(_orchestrator_runtime_dir(os.path.dirname(build_dir)), "worktrees")


def setup_lane_worktrees(build_dir, roster):
    """Create one worktree per lane (branch lane-<slug>) off HEAD. Returns
    {slug: path}, or {} if git/worktrees are unavailable or any step fails."""
    if not build_dir or not os.path.isdir(os.path.join(build_dir, ".git")):
        return {}
    root = _worktree_root(build_dir)
    os.makedirs(root, exist_ok=True)
    worktrees = {}
    for w in roster:
        slug = w["slug"]
        path = os.path.join(root, slug)
        # Remove any stale worktree/branch from a previous iteration first.
        _git(build_dir, "worktree", "remove", "--force", path)
        shutil.rmtree(path, ignore_errors=True)
        code = _git(build_dir, "worktree", "add", "-f", "-B", "lane-%s" % slug, path, "HEAD")[0]
        if code != 0:
            # Roll back any created worktrees and fall back to direct write.
            cleanup_lane_worktrees(build_dir, worktrees)
            return {}
        worktrees[slug] = path
    return worktrees


def integrate_lane_worktrees(build_dir, roster, worktrees):
    """Commit each lane's changes then merge them into the integration branch in
    deterministic roster order (V2 spec §18.3).

    On a merge conflict the lane is rebased onto the integration HEAD and the
    merge retried once (§18.3 step 9). If it STILL conflicts, the merge is
    aborted and the conflict is surfaced as blocked — the spec explicitly
    forbids last-write-wins resolution (§18.3 step 11, §30 acceptance).

    Returns (notes, blocked): notes is a list of human-readable integration
    notes; blocked is None, or a dict {"lane", "files", "detail"} describing
    the first unresolvable conflict (remaining lanes are left unmerged so the
    user resolves from a consistent integration HEAD)."""
    notes = []
    integ_branch = _git(build_dir, "rev-parse", "--abbrev-ref", "HEAD")[1].strip() or "HEAD"
    for w in roster:
        slug = w["slug"]
        wt = worktrees.get(slug)
        if not wt or not os.path.isdir(wt):
            continue
        _git(wt, "add", "-A")
        if _git(wt, "diff", "--cached", "--quiet")[0] != 0:
            _git(wt, "commit", "-q", "-m", "lane %s" % slug)
        code, _out, _err = _git(build_dir, "merge", "--no-edit", "-q", "lane-%s" % slug)
        if code == 0:
            continue
        conflicted = [f for f in _git(build_dir, "diff", "--name-only",
                                      "--diff-filter=U")[1].splitlines() if f.strip()]
        _git(build_dir, "merge", "--abort")
        # §18.3 step 9: rebase the lane onto the integration HEAD, retry once.
        rb_code = _git(wt, "rebase", integ_branch)[0]
        if rb_code != 0:
            _git(wt, "rebase", "--abort")
        else:
            code, _out, _err = _git(build_dir, "merge", "--no-edit", "-q",
                                    "lane-%s" % slug)
            if code == 0:
                notes.append("lane '%s' conflicted, resolved by rebasing the lane "
                             "onto the integration HEAD" % slug)
                continue
            conflicted = [f for f in _git(build_dir, "diff", "--name-only",
                                          "--diff-filter=U")[1].splitlines() if f.strip()]
            _git(build_dir, "merge", "--abort")
        # §18.3 step 11: unresolvable — block, never last-write-wins.
        blocked = {
            "lane": slug,
            "files": conflicted,
            "detail": ("lane '%s' conflicts with the integration branch on: %s"
                       % (slug, ", ".join(conflicted) or "(unknown files)")),
        }
        notes.append("lane '%s' has an UNRESOLVED file conflict — run blocked "
                     "(blocked_conflict), no lane data was overwritten" % slug)
        return notes, blocked
    return notes, None


def cleanup_lane_worktrees(build_dir, worktrees):
    """Remove lane worktrees + prune. Best-effort."""
    for _slug, wt in (worktrees or {}).items():
        _git(build_dir, "worktree", "remove", "--force", wt)
        shutil.rmtree(wt, ignore_errors=True)
    _git(build_dir, "worktree", "prune")


def _gated_verify_spec(cfg, spec):
    """Global kill-switch for the xcodebuild verify-spec's optional
    "run_tests": true (runtime.run_generated_tests, default True): a workflow
    phase + its verify spec can ASK for a real `xcodebuild test` run, but it
    only actually happens when this runtime knob also allows it. Real test
    runs are slower and can be flakier than a plain compile (simulator boot,
    timing-sensitive UI tests) — this is the one global lever to shut that off
    without editing every workflow's verify block. Purely additive: with the
    knob at its default True, behavior is unchanged from before this existed."""
    if not isinstance(spec, dict) or not spec.get("run_tests"):
        return spec
    if bool(cget(cfg, "runtime.run_generated_tests", True)):
        return spec
    spec = dict(spec)
    spec["run_tests"] = False
    return spec


def _run_iteration_verify(cfg, app, app_dir, phasedef, state, md_path, rnd):
    """Per-iteration compile check for the parallel build (the evidence behind
    the consensus gate, runtime.verify_between_iterations).

    Returns the raw run_verification result dict, or None when the check was
    skipped (knob off, no build dir, or the toolchain is already known absent
    this phase). A ran=False result is persisted once and then cached via
    cfg["_iter_verify_toolchain_absent"] so a doomed subprocess isn't re-paid
    every iteration. Best-effort and non-fatal, like every verify path."""
    if not bool(cget(cfg, "runtime.verify_between_iterations", True)):
        return None
    build_dir = cfg.get("_build_dir")
    if not build_dir or cfg.get("_iter_verify_toolchain_absent"):
        return None
    key = phasedef.key if hasattr(phasedef, "key") else phasedef[0]
    # This build phase rarely carries its own verify spec — reuse the
    # workflow's (usually build_verification's), so both gates compile the
    # same way; else fall back to auto-detection.
    spec = (phasedef.get("verify") if hasattr(phasedef, "get") else None) \
        or cfg.get("_workflow_verify_spec") or {"type": "auto"}
    if isinstance(spec, dict) and spec.get("run_tests"):
        # Per-iteration checks stay build-only even when the reused spec (see
        # comment above) asks for run_tests: a real test suite is slow, and
        # paying for it on every single build iteration (vs. once, at
        # build_verification) would dominate build wall-clock for no extra
        # signal beyond "does it still compile" until the very end anyway.
        spec = dict(spec)
        spec["run_tests"] = False
    timeout = int(cget(cfg, "runtime.iteration_verify_timeout_seconds", 600) or 600)
    hard = int(cget(cfg, "runtime.verify_timeout_seconds", 1200) or 0)
    if hard:
        timeout = min(timeout, hard)
    if cfg.get("_deadline"):
        timeout = max(10, min(timeout, int(cfg["_deadline"] - time.time())))
    res = verifylib.run_verification(build_dir, spec, timeout)
    verifylib.persist_verify_result(app_dir, key, res, attempt=0,
                                    prompt_hash=state.get("prompt_hash"),
                                    workflow=cfg.get("_workflow_name"))
    status = verifylib.verification_status(res)
    emit("ITER-VERIFY %d: %s — %s (%s)" % (rnd, status, res.get("summary", ""),
                                           res.get("tool", "")))
    live_log(app_dir, key, "orchestrator", "verify_result",
             "iteration %d: %s (%s)" % (rnd, status, res.get("summary", "")))
    evlib.emit_event(app_dir, "verify_result", project=app, phase=key,
                     status=status,
                     detail="iteration %d: %s" % (rnd, res.get("summary", "")))
    if not res.get("ran"):
        tcxlib.TurnContext(cfg).iter_verify_toolchain_absent = True
        append_md(md_path, "\n_Iteration %d verification skipped: %s — further "
                  "per-iteration checks disabled for this phase._\n"
                  % (rnd, res.get("summary", "no toolchain")))
    elif not res.get("ok"):
        mistklib.append_mistake(app_dir, {
            "app": app, "workflow": cfg.get("_workflow_name"), "phase": key,
            "cls": "verify_failure",
            "summary": "iteration %d: %s" % (rnd, res.get("summary", ""))})
    return res


def _run_parallel_build(cfg, app, app_dir, phasedef, original_prompt, prior_outputs,
                        state, md_path, max_rounds, transcript, extra, personas=None):
    """Run the build phase with agents working CONCURRENTLY.

    Each iteration: every worker builds its own lane at the same time (real
    subprocesses in parallel threads); then a single integrator turn wires the
    shared files together and decides whether the app is done. Resilient — if a
    worker's CLI isn't logged in it's skipped, and the build proceeds as long as
    at least one worker produced output. Repeated stall iterations are retried
    but bounded by ``runtime.max_build_iterations_without_integrator`` so the
    build does not loop forever when no workers or integrator are reachable.
    Returns (consensus, final_output, transcript)."""
    key, folder, fname, _purpose = phasedef
    active = enabled_agents(cfg)
    roster = build_worker_roster(cfg, active)
    coord = _pick_coordinator(cfg, active)
    build_dir = cfg.get("_build_dir")
    roster_desc = "; ".join("%s builds %s" % (w["label"], w["lane"]) for w in roster)
    # V2 §19/§20: the persisted machine contracts from task_assignments /
    # tech_specs, injected into every worker prompt (per-lane task slice + the
    # full shared interface contract). Missing files just mean no injection.
    backlog = load_tasks(app_dir)
    interfaces = load_interfaces(app_dir)
    flows_contract = _flows_contract_block(load_flows(app_dir))
    if backlog or interfaces:
        emit("Injecting build contracts: %d task(s), %d interface(s)%s."
             % (len(backlog), len(interfaces),
                " + flow-token identifier contract" if flows_contract else ""))
    # Vertical slices (runtime.build_vertical_slices): topo-layer the backlog
    # by depends_on; iteration k works wave k — one coherent slice end-to-end
    # at a time instead of the whole app at once. Waves beyond the iteration
    # budget collapse into the final iteration.
    waves = _task_waves(backlog) \
        if bool(cget(cfg, "runtime.build_vertical_slices", True)) else []
    if len(waves) > 1:
        emit("Vertical slices: %d wave(s) — %s."
             % (len(waves), "; ".join("W%d: %d task(s)" % (i + 1, len(w))
                                      for i, w in enumerate(waves))))

    emit("PARALLEL build: %d worker(s) [%s]; integrator=%s"
         % (len(roster), ", ".join(w["label"] for w in roster),
            DISPLAY.get(coord, coord)))
    append_md(md_path, "\n_Parallel build — %d workers running at once: %s. "
              "Integrator: %s._\n"
              % (len(roster), roster_desc, DISPLAY.get(coord, coord)))

    consensus = False
    final_output = ""
    integrationless_iterations = 0

    # V2 §5.1: make app_build a persistent git repo so each iteration is a
    # rollback-able, crash-durable commit (version history for iterate-mode).
    if ensure_build_repo(build_dir):
        emit("Build repo ready (git) at %s" % build_dir)

    unlimited_rounds = max_rounds <= 0
    max_integrationless = int(cget(
        cfg, "runtime.max_build_iterations_without_integrator", 2) or 0)
    _lane_seen = {}   # per-lane transcript offset for session delta prompts
    build_rounds = itertools.count(1) if unlimited_rounds else range(1, max_rounds + 1)
    for rnd in build_rounds:
        # Sprint watchdog: stop starting new build iterations once the build slice
        # is spent, leaving the reserved tail for verify + a fast review.
        if cfg.get("_phase_deadline") and time.time() >= cfg["_phase_deadline"]:
            emit("Sprint: build time budget reached — finalizing at iteration %d." % rnd)
            break
        if _SHUTDOWN.is_set():
            break
        state["current_round"] = rnd
        state["next_agent"] = "+".join(w["slug"] for w in roster)  # all building at once
        save_state(app_dir, state)
        append_md(md_path, "\n### Iteration %d\n\n" % rnd)
        transcript = drain_human_inbox(app_dir, md_path, transcript, "Iteration %d" % rnd,
                                       phase_key=key, rnd=rnd, slot="open")

        tree = _build_file_tree(build_dir)
        # Worker-lane context policy (runtime.build_context_policy): under
        # "contracts" (default) the raw prior-phase discussion transcripts are
        # dropped from WORKER prompts — they keep the original prompt, prior
        # final decisions, the DECISIONS LOG, playbook/knowledge, file tree and
        # their tasks/interfaces contract, which is what they actually use.
        # The integrator (ictx below) and discussion phases keep full context.
        # "legacy" preserves the old behavior exactly.
        wctx_cfg = cfg
        if str(cget(cfg, "runtime.build_context_policy", "contracts")
               or "contracts").strip().lower() != "legacy":
            wctx_cfg = tcxlib.TurnContext(cfg).thread_copy(
                drop_prior_discussions=True)
        base_ctx = build_context(wctx_cfg, app, phasedef, original_prompt,
                                 prior_outputs, transcript)

        # V2 §5.2-5.5: optionally isolate each lane in its own git worktree so
        # concurrent workers can't clobber each other. Empty dict => transparent
        # fallback to the proven direct-write build.
        worktrees = {}
        if bool(cget(cfg, "runtime.worktree_isolation", False)):
            worktrees = setup_lane_worktrees(build_dir, roster)
            if worktrees:
                emit("Worktree isolation: %d lane(s) in separate worktrees." % len(worktrees))

        # Same for every worker this iteration — compute once, not per-thread.
        _wave_backlog, _wave_note = backlog, ""
        if len(waves) > 1:
            _widx = min(rnd - 1, len(waves) - 1)
            _wave_backlog = waves[_widx]
            _done_n = sum(len(wv) for wv in waves[:_widx])
            _wave_note = ("VERTICAL SLICE — iteration %d works WAVE %d/%d "
                          "ONLY (%d task(s); %d task(s) from earlier waves "
                          "are already built — extend, don't rewrite). "
                          "Finish this wave's tasks end-to-end (UI + logic "
                          "+ states) before touching anything else."
                          % (rnd, _widx + 1, len(waves),
                             len(_wave_backlog), _done_n))
        # §19 dynamic task claiming: decide who builds what BEFORE fan-out,
        # single-threaded (no concurrent writers to race on tasks.json), then
        # persist so the assignment is visible outside this run too.
        claims = _claim_tasks_for_iteration(roster, _wave_backlog, rnd) \
            if _wave_backlog else {w["slug"]: [] for w in roster}
        if backlog:
            persist_tasks(app_dir, backlog, [])

        # ---- fan out: every worker builds its lane concurrently ----
        def _run_worker(pair):
            idx, w = pair
            pers = roleslib.persona_preamble((personas or {}).get(w["agent"]))
            prompt = prompt_build_worker(cfg, w, base_ctx, rnd, tree, roster_desc,
                                         DISPLAY.get(coord, coord), extra=extra,
                                         persona=pers,
                                         contract=_worker_contract_block(
                                             _wave_backlog, claims.get(w["slug"], []),
                                             interfaces, wave_note=_wave_note,
                                             flows_contract=flows_contract))
            # Per-worker cfg: a shallow copy that (a) points an isolated lane at
            # its OWN worktree and (b) keys circuit-breaker health by the worker
            # slug so concurrent threads of the SAME agent never race on one
            # shared health dict (the _agent_health map itself is shared, but
            # each per-slug entry is only ever touched by its own thread).
            wcfg = tcxlib.TurnContext(cfg).thread_copy(health_key=w["slug"])
            # Per-role routing: cheap workers / expensive integrator (§4b).
            _apply_role_routing(wcfg, "worker")
            if worktrees.get(w["slug"]):
                tcxlib.TurnContext(wcfg).build_dir = worktrees[w["slug"]]
            # Resumed lane session: the contract + full context live in the
            # session from iteration 1; later turns get only what's new plus a
            # fresh file tree (the repo changes under them every iteration).
            delta = None
            if w["agent"] == "claude":
                new_part = ("===== NEW SINCE YOUR LAST TURN (integrator pass + "
                            "other lanes) =====\n"
                            + transcript[_lane_seen.get(w["slug"], 0):])
                delta = prompt_build_worker(wcfg, w, new_part, rnd, tree,
                                            roster_desc, DISPLAY.get(coord, coord),
                                            extra=extra, persona=pers, contract="")
            _lane_seen[w["slug"]] = len(transcript)
            try:
                resp = call_agent_sessioned(wcfg, app, key, "%d.%s" % (rnd, w["slug"]),
                                            w["agent"], prompt, delta_prompt=delta,
                                            session_key="%s:%s:lane" % (key, w["slug"]))
                return (idx, resp, None)
            except AgentError as exc:
                emit("Worker %s failed on iteration %d: %s" % (w["label"], rnd, exc))
                return (idx, None, str(exc))

        results = [None] * len(roster)
        with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, len(roster))) as ex:
            futs = {ex.submit(_run_worker, (i, w)): i for i, w in enumerate(roster)}
            for fut in concurrent.futures.as_completed(futs):
                # Belt-and-suspenders: _run_worker already catches AgentError, but
                # anything unexpected must not leave a results slot as None (which
                # would blow up the unpack below).
                try:
                    idx, resp, err = fut.result()
                    results[idx] = (resp, err)
                except Exception as exc:  # noqa: BLE001 - defensive
                    results[futs[fut]] = (None, "unexpected worker error: %s" % exc)

        produced = 0
        for i, w in enumerate(roster):
            resp, err = results[i] if results[i] is not None else (None, "no result")
            plabel = roleslib.persona_label((personas or {}).get(w["agent"]))
            hat = " (%s)" % plabel if plabel else ""
            if err:
                block = ("**%s%s — Iteration %d (skipped: CLI unavailable)**\n\n_%s_\n"
                         % (w["label"], hat, rnd, err))
            else:
                produced += 1
                block = "**%s%s — Iteration %d**\n\n%s\n" % (w["label"], hat, rnd, resp)
                live_log(app_dir, w.get("lane_id") or w["slug"], w["agent"],
                         "agent_turn_completed", resp)
            append_md(md_path, "\n" + block)
            msglib.append_message(app_dir, key, "skip" if err else "turn",
                                  w["slug"], md_path, agent=w["agent"],
                                  role=w.get("label") or "", persona=plabel,
                                  rnd=rnd)
            transcript += "\n" + block
        emit("Iteration %d: %d/%d workers produced output." % (rnd, produced, len(roster)))
        if produced == 0:
            cleanup_lane_worktrees(build_dir, worktrees)
            integrationless_iterations += 1
            block = ("_No build worker produced output in iteration %d. This can happen "
                     "if all worker CLIs are unavailable or still starting up. "
                     "Observed worker stall streak: %d._" % (rnd,
                                                              integrationless_iterations))
            append_md(md_path, "\n" + block)
            transcript += "\n" + block
            final_output = block
            emit("Iteration %d had no worker output (stall streak: %d)."
                 % (rnd, integrationless_iterations))
            if max_integrationless > 0 and integrationless_iterations >= max_integrationless:
                raise AgentError(
                    "Observer stopped build_coordination after %d consecutive "
                    "iteration(s) with no build worker output. Ensure at least "
                    "one build worker can run or disable that worker from the "
                    "active roster and retry." % integrationless_iterations)
            continue

        # ---- merge isolated lanes back into the integration tree (§5.5) ----
        if worktrees:
            conflict_notes, blocked = integrate_lane_worktrees(build_dir, roster, worktrees)
            for note in conflict_notes:
                emit("Integration: %s" % note)
                append_md(md_path, "\n_⚠️ %s_\n" % note)
            if blocked:
                # §18.3 step 11: pause even in Fully Autonomous mode. The lane
                # worktrees are intentionally LEFT IN PLACE so the user can
                # inspect/resolve both sides, then --resume.
                state["blocked_conflict"] = blocked
                save_state(app_dir, state)
                live_log(app_dir, "integrator", "orchestrator", "blocked_conflict",
                         blocked["detail"])
                append_md(md_path, "\n_⛔ blocked_conflict: %s. Lane worktrees "
                          "kept for manual resolution._\n" % blocked["detail"])
                raise AgentError(
                    "blocked_conflict: %s. Resolve the conflict (lane worktrees are "
                    "under %s), then re-run with --resume." %
                    (blocked["detail"], _worktree_root(build_dir)))
            cleanup_lane_worktrees(build_dir, worktrees)

        # ---- optional cross-review: each lane sanity-checks the others' output
        # against the shared contracts BEFORE integration (parallel, no writes;
        # resumed sessions make these turns cheap). Catches interface drift the
        # integrator would otherwise discover mid-merge.
        if produced >= 2 and bool(cget(cfg, "runtime.build_cross_review", True)) \
                and not _SHUTDOWN.is_set():
            def _cross_review(pair):
                i, w = pair
                if not results[i] or results[i][1]:
                    return (i, None)   # this lane produced nothing to review from
                others = "\n\n".join(
                    "--- %s (lane %s) ---\n%s" % (roster[j]["label"], roster[j]["lane"],
                                                  results[j][0])
                    for j in range(len(roster))
                    if j != i and results[j] and not results[j][1] and results[j][0])
                if not others.strip():
                    return (i, None)
                rp = ("Quick integration cross-check — do NOT change any files this "
                      "turn. The other lanes just reported:\n\n" + _budget(others, 12000)
                      + "\n\nIn at most 8 short bullets: flag interface-contract "
                        "violations, duplicated work, or integration risks between "
                        "your lane and theirs that the integrator must handle this "
                        "iteration. If none, reply exactly: No conflicts seen.")
                rcfg = tcxlib.TurnContext(cfg).thread_copy(
                    health_key=w["slug"])
                try:
                    return (i, call_agent_sessioned(
                        rcfg, app, key, "%d.review.%s" % (rnd, w["slug"]), w["agent"],
                        rp, delta_prompt=rp, session_key="%s:%s:lane" % (key, w["slug"])))
                except AgentError:
                    return (i, None)   # review is best-effort; never blocks the build

            with concurrent.futures.ThreadPoolExecutor(max_workers=len(roster)) as ex:
                for fut in concurrent.futures.as_completed(
                        [ex.submit(_cross_review, (i, w)) for i, w in enumerate(roster)]):
                    i, note = fut.result()
                    if note and note.strip().lower() != "no conflicts seen.":
                        rblock = "**%s — cross-review (iteration %d)**\n\n%s\n" % (
                            roster[i]["label"], rnd, note)
                        append_md(md_path, "\n" + rblock)
                        msglib.append_message(
                            app_dir, key, "review", roster[i]["slug"], md_path,
                            agent=roster[i]["agent"],
                            role=roster[i].get("label") or "", rnd=rnd)
                        transcript += "\n" + rblock

        # ---- barrier reached: integrator wires the shared files + decides ----
        transcript = drain_human_inbox(app_dir, md_path, transcript, "Iteration %d" % rnd,
                                       phase_key=key, rnd=rnd, slot="coord")
        failover_enabled = bool(cget(cfg, "runtime.coordinator_failover_enabled", True))
        integrator = _pick_live_coordinator(cfg, active, preferred=coord) or coord
        if integrator != coord:
            note = ("Observer: build integrator failover — %s is unavailable or "
                    "cooling down; using %s for iteration %d."
                    % (DISPLAY.get(coord, coord), DISPLAY.get(integrator, integrator), rnd))
            emit(note)
            append_md(md_path, "\n_%s_\n" % note)
        state["next_agent"] = integrator
        save_state(app_dir, state)
        tree = _build_file_tree(build_dir)
        ictx = (build_context(cfg, app, phasedef, original_prompt, prior_outputs, transcript)
                + "\n\n===== FILES IN app_build AFTER THIS ITERATION =====\n" + tree)
        cresp = None
        attempted_integrators = []
        candidates = ([integrator] if not failover_enabled else
                      _coordinator_candidates(cfg, active, preferred=integrator,
                                              require_healthy=True))
        if not candidates and integrator:
            candidates = [integrator]
        for candidate in candidates:
            if candidate in attempted_integrators:
                continue
            attempted_integrators.append(candidate)
            cprompt = prompt_coordinate(cfg, candidate, ictx, phasedef, rnd,
                                        is_build=True,
                                        final_round=(not unlimited_rounds and rnd == max_rounds))
            icfg = dict(cfg)
            # Per-role routing for the integrator's turn only (§4b).
            _apply_role_routing(icfg, "integrator")
            idelta = None
            if candidate == "claude":
                # Optional stronger model for the integration turn only — the
                # integrator is the build's quality chokepoint.
                _iov = cget(cfg, "models.claude_integrator", "") or ""
                if _iov:
                    tcxlib.TurnContext(icfg).claude_model_override = _iov
                idelta = ("===== NEW SINCE YOUR LAST INTEGRATION PASS =====\n"
                          + transcript[_lane_seen.get("__integrator__", 0):]
                          + "\n\n===== FILES IN app_build AFTER THIS ITERATION =====\n"
                          + tree + "\n"
                          + prompt_coordinate(icfg, candidate, "", phasedef, rnd,
                                              is_build=True,
                                              final_round=(not unlimited_rounds and rnd == max_rounds)))
            try:
                cresp = call_agent_sessioned(icfg, app, key, "%d.integrate" % rnd,
                                             candidate, cprompt, delta_prompt=idelta,
                                             session_key="%s:%s:integrate" % (key, candidate))
                integrator = candidate
                break
            except AgentError as exc:
                emit("Integrator (%s) unavailable on iteration %d: %s"
                     % (DISPLAY.get(candidate, candidate), rnd, exc))
                continue
        if cresp is None:
            integrationless_iterations += 1
            attempted = ", ".join(DISPLAY.get(a, a) for a in attempted_integrators) or "none"
            cresp = ("_No build integrator was available this iteration "
                     "(attempted: %s). The observer recorded this as "
                     "integrationless iteration %d._"
                     % (attempted, integrationless_iterations))
        else:
            integrationless_iterations = 0
        cblock = "**Integrator (%s) — after iteration %d**\n\n%s\n" % (
            DISPLAY.get(integrator, integrator), rnd, cresp)
        append_md(md_path, "\n" + cblock)
        msglib.append_message(app_dir, key, "integrator", integrator, md_path,
                              rnd=rnd)
        transcript += "\n" + cblock
        # The integrator wrote this decision itself — next delta starts after it.
        _lane_seen["__integrator__"] = len(transcript)
        live_log(app_dir, "integrator", integrator, "agent_turn_completed", cresp)
        emit("Appended Integrator (%s) pass to %s/%s"
             % (DISPLAY.get(integrator, integrator), folder, fname))

        # V2 §5.7: commit this iteration's build state as one durable, rollback-able
        # checkpoint (the phase-boundary/version-history commit).
        sha = commit_build_state(build_dir, "orchestrator: %s iteration %d" % (key, rnd))
        if sha:
            emit("Committed build iteration %d -> %s" % (rnd, sha))

        if max_integrationless > 0 and integrationless_iterations >= max_integrationless:
            raise AgentError(
                "Observer stopped build_coordination after %d consecutive "
                "iteration(s) without a working integrator. Check provider login/"
                "health or enable a different coordinator before resuming."
                % integrationless_iterations)

        # Per-iteration verification (runtime.verify_between_iterations): compile
        # the just-committed state; a failure feeds a compact errors tail into
        # the next iteration's context (mirroring _verify_and_repair's loop).
        iter_verify = _run_iteration_verify(cfg, app, app_dir, phasedef, state,
                                            md_path, rnd)
        if iter_verify is not None and iter_verify.get("ran") \
                and not iter_verify.get("ok"):
            vblock = ("**Build verification — iteration %d FAILED (%s)**\n\n"
                      "```\n%s\n```\n_Fix these compile errors next iteration._\n"
                      % (rnd, iter_verify.get("summary", ""),
                         (iter_verify.get("errors", "") or "")[:4000]))
            append_md(md_path, "\n" + vblock)
            transcript += "\n" + vblock

        final_output = cresp
        if CONSENSUS_RE.search(cresp):
            # Evidence-backed consensus: a verifier that RAN and said NO
            # overrides the integrator's claim. CRITICAL fail-open rule: an
            # unverified build (ran=false / no record / knob off) must NEVER
            # block consensus — only real failing evidence does.
            if iter_verify is not None and iter_verify.get("ran") \
                    and not iter_verify.get("ok"):
                note = ("Observer: integrator declared consensus but the build "
                        "fails to compile — continuing.")
                emit(note)
                append_md(md_path, "\n_%s_\n" % note)
                transcript += "\n_%s_\n" % note
                mistklib.append_mistake(app_dir, {
                    "app": app, "workflow": cfg.get("_workflow_name"),
                    "phase": key, "agent": integrator,
                    "cls": "consensus_unverified",
                    "summary": "consensus rejected at iteration %d: %s"
                               % (rnd, iter_verify.get("summary", ""))})
            else:
                consensus = True
                emit("BUILD consensus reached at iteration %d." % rnd)
                break

    return consensus, final_output, transcript


def _verify_and_repair(cfg, app, app_dir, phasedef, state, md_path, transcript, coord):
    """After a build, actually compile it. If it fails, run bounded repair
    iterations that feed the compiler errors back to the integrator (who has
    write access) until it compiles or we run out of tries.

    Returns (transcript, note) where note is a one-line human summary appended to
    the phase's Final Output. Best-effort and non-fatal: a missing toolchain just
    yields 'unverified'."""
    spec = phasedef.get("verify") if hasattr(phasedef, "get") else None
    build_dir = cfg.get("_build_dir")
    if not spec or not build_dir:
        return transcript, ""
    if not bool(cget(cfg, "runtime.verify_build_enabled", True)):
        return transcript, ""
    spec = _gated_verify_spec(cfg, spec)
    key = phasedef.key if hasattr(phasedef, "key") else phasedef[0]
    timeout = int(cget(cfg, "runtime.verify_timeout_seconds", 1200))
    max_repairs = int(spec.get("repair_iterations", cget(cfg, "runtime.verify_repair_iterations", 3)))
    _bud = cfg.get("_budget")
    if _bud and _bud.get("verify_timeout"):
        timeout = min(timeout, int(_bud["verify_timeout"]))

    def _compile_timeout():
        # Sprint: never let a compile run past the hard run deadline.
        if cfg.get("_deadline"):
            return max(10, min(timeout, int(cfg["_deadline"] - time.time())))
        return timeout

    emit("VERIFY: compiling the build in %s (%s)…" % (build_dir, spec.get("type", "auto")))
    append_md(md_path, "\n### Verification\n\n")
    res = verifylib.run_verification(build_dir, spec, _compile_timeout())

    def _log_result(r, attempt_label):
        icon = "✅" if r.get("ok") else ("⚠️" if not r.get("ran") else "❌")
        line = "%s **Verification %s** — %s (%s)" % (
            icon, attempt_label, r.get("summary", ""), r.get("tool", ""))
        append_md(md_path, line + "\n")
        emit("VERIFY %s: %s" % (attempt_label, r.get("summary", "")))
        live_log(app_dir, key, "orchestrator", "verify_result",
                 "%s: %s (%s)" % (attempt_label, verifylib.verification_status(r),
                                  r.get("summary", "")))
        evlib.emit_event(app_dir, "verify_result", project=app, phase=key,
                         status=verifylib.verification_status(r),
                         detail="%s: %s" % (attempt_label, r.get("summary", "")))
        if r.get("ran") and not r.get("ok"):
            mistklib.append_mistake(app_dir, {
                "app": app, "workflow": cfg.get("_workflow_name"), "phase": key,
                "agent": coord, "cls": "verify_failure",
                "summary": "%s: %s" % (attempt_label, r.get("summary", ""))})

    verifylib.persist_verify_result(app_dir, key, res, attempt=0,
                                    prompt_hash=state.get("prompt_hash"),
                                    workflow=cfg.get("_workflow_name"))
    _log_result(res, "initial")
    if not res.get("ran"):
        return transcript, "build left unverified (%s)" % res.get("summary", "no toolchain")
    if res.get("ok"):
        return transcript, "verified: %s" % res.get("summary", "compiles")

    # Repair loop. Adaptive escalation (runtime.adaptive_escalation_enabled):
    # once an attempt crosses runtime.escalate_repair_after_attempt, THAT and
    # every later attempt in this loop run with a stronger reasoning effort
    # (or an explicit runtime.escalation_model_override for claude) — see
    # _maybe_escalate. `cfg` itself is never touched, so a later fresh verify
    # cycle (or any other phase) is unaffected.
    _escalation_logged = False
    for attempt in range(1, max_repairs + 1):
        # Sprint: stop repairing if the run deadline is essentially here.
        if cfg.get("_deadline") and time.time() >= cfg["_deadline"] - 10:
            append_md(md_path, "_Repair loop stopped: sprint time budget reached._\n")
            break
        state["next_agent"] = coord
        save_state(app_dir, state)
        tree = _build_file_tree(build_dir)
        repair_prompt = (
            "The app you're building was just compiled and it FAILED. You have "
            "WRITE access to the app_build folder — fix the actual code so it "
            "compiles.\n\n"
            "===== FILES IN app_build =====\n%s\n\n"
            "===== BUILD TOOL =====\n%s\n\n"
            "===== COMPILER ERRORS (fix these) =====\n%s\n\n"
            "Edit the real files to resolve every error above. Don't just "
            "describe the fix — make it. Prefer the smallest change that makes it "
            "compile without breaking features. When done, briefly say what you "
            "changed." % (tree, res.get("tool", ""), res.get("errors", "")[:8000])
        )
        acfg, _escalation_logged = _maybe_escalate(
            cfg, app, app_dir, key, coord, attempt, _escalation_logged)
        try:
            rresp = call_agent(acfg, app, key, "repair.%d" % attempt, coord, repair_prompt)
        except AgentError as exc:
            emit("Repair agent (%s) unavailable on attempt %d: %s"
                 % (DISPLAY.get(coord, coord), attempt, exc))
            append_md(md_path, "_Repair attempt %d skipped: %s_\n" % (attempt, exc))
            break
        rblock = "**Repair %d (%s)**\n\n%s\n" % (attempt, DISPLAY.get(coord, coord), rresp)
        append_md(md_path, "\n" + rblock)
        msglib.append_message(app_dir, key, "repair", coord, md_path,
                              rnd=attempt)
        transcript += "\n" + rblock

        res = verifylib.run_verification(build_dir, spec, _compile_timeout())
        verifylib.persist_verify_result(app_dir, key, res, attempt=attempt,
                                        prompt_hash=state.get("prompt_hash"),
                                        workflow=cfg.get("_workflow_name"))
        _log_result(res, "after repair %d" % attempt)
        if res.get("ok"):
            return transcript, "verified after %d repair(s): %s" % (attempt, res.get("summary", ""))

    return transcript, "still not compiling after %d repair attempt(s)" % max_repairs


def _apply_phase_routing(cfg, key):
    """Per-phase model routing (model_routing.json): return a phase-scoped cfg
    copy with any claude/codex/gemini/ollama model + reasoning overrides
    patched into models/_resolved, and _phase_key set so enabled_agents can
    apply the phase's participant filter. No overrides -> plain tagged copy."""
    routing = cfg.get("_routing")
    if routing is None:
        routing = mrlib.load_routing_for_session(
            HERE, cfg.get("_app_dir"), section_dir=_section_dir(cfg),
            on_warn=emit)
        tcxlib.TurnContext(cfg).routing = routing
    # Shared mutable run state must exist BEFORE the copy so every phase-scoped
    # copy aliases the same dicts (cooldowns/sessions survive across phases).
    # The health map is _agent_health everywhere else — the old "_health" key was
    # written here and never read, so cooldowns didn't actually survive the copy.
    base_tctx = tcxlib.TurnContext(cfg)
    base_tctx.health_map()
    base_tctx.session_map("claude")
    base_tctx.session_map("codex")
    c = dict(cfg)
    tctx = tcxlib.TurnContext(c)
    tctx.phase_key = key
    ov = mrlib.overrides_for(routing, key)
    if not ov:
        return c
    models = dict(c.get("models") or {})
    resolved = dict(c.get("_resolved") or {})
    if ov.get("claude"):
        models["claude"] = resolved["claude_model"] = ov["claude"]
    if ov.get("codex"):
        models["codex"] = resolved["codex_model"] = ov["codex"]
    if ov.get("codex_reasoning"):
        # One knob per phase: the runner picks build vs chat effort by
        # _allow_writes, so a phase override sets both.
        models["codex_reasoning"] = ov["codex_reasoning"]
        models["codex_build_reasoning"] = ov["codex_reasoning"]
    if ov.get("claude_reasoning"):
        # Same one-knob rule for claude's --effort (see run_claude).
        models["claude_reasoning"] = ov["claude_reasoning"]
        models["claude_build_reasoning"] = ov["claude_reasoning"]
    if ov.get("ollama_reasoning"):
        # Real capability (not invented): Ollama's /api/generate accepts a
        # boolean "think" field for reasoning-capable models (deepseek-r1,
        # qwen3, ...). run_local (the HTTP path used by dynamic local:<model>
        # agents) maps this effort level onto that flag — see run_local. The
        # roster "ollama" agent instead shells out to `ollama run <model>`
        # (run_ollama), which this engine invokes with no equivalent CLI flag,
        # so that path stays accepted-but-noop (warned there, once).
        models["ollama_reasoning"] = ov["ollama_reasoning"]
    # gemini effort parity is accepted-but-noop (evidence-based): the gemini
    # CLI is invoked as `gemini -p ...`, which exposes no effort/thinking
    # control, so the field is honored in the schema but ignored here, loudly
    # (once).
    _roles_ov = ov.get("roles") if isinstance(ov.get("roles"), dict) else {}
    for _noop_field in ("gemini_reasoning",):
        _vals = [ov.get(_noop_field)] + [r.get(_noop_field)
                                         for r in _roles_ov.values()]
        if any(_vals):
            if tcxlib.TurnContext(cfg).note_once(
                    "%s_noop" % _noop_field, key):
                emit("Phase '%s': routing sets %s, but the %s CLI exposes no "
                     "effort control in how this engine invokes it — field "
                     "accepted but ignored."
                     % (key, _noop_field, _noop_field.split("_")[0]))
    if ov.get("gemini"):
        models["gemini_fallback"] = ov["gemini"]
        if valid_gemini_model(ov["gemini"]):
            resolved["gemini_model"] = ov["gemini"]
    if ov.get("ollama"):
        models["ollama"] = resolved["ollama_model"] = ov["ollama"]
        # enabled_agents() only falls back to models.ollama as a participant
        # when NO roster is configured — with a roster active, this override
        # is silently ignored for participant purposes (the roster's own
        # entries win). Surface that once instead of leaving a routing edit
        # that looks like it should do something quietly do nothing.
        _roster_now = _split_local_roster(
            resolved.get("ollama_roster") or cget(c, "models.ollama_roster", []))
        if _roster_now and ov["ollama"] not in _roster_now:
            if tcxlib.TurnContext(cfg).note_once(
                    "ollama_override_shadowed", key):
                emit("Phase '%s': routing sets models.ollama=%r, but "
                     "models.ollama_roster is configured and doesn't include "
                     "it — the roster's own entries are used for this phase "
                     "instead; the override has no effect on participants."
                     % (key, ov["ollama"]))
    if ov.get("timeout"):
        # Per-phase turn timeout: strong models get room to think on the
        # phases that deserve it. process_phase folds this into _turn_timeout.
        routed_timeout = int(ov["timeout"])
        hard_timeout = int(cget(c, "runtime.timeout_seconds_per_agent", 1200) or 0)
        if hard_timeout and routed_timeout > hard_timeout:
            emit("Phase '%s': routing timeout %ds exceeds the configured "
                 "runtime.timeout_seconds_per_agent (%ds) — the routed timeout "
                 "wins on this phase, giving it more room than the general cap."
                 % (key, routed_timeout, hard_timeout))
        tctx.routed_turn_timeout = routed_timeout
    if "rounds" in ov:
        # Per-phase rounds override (project routing beats the workflow's
        # value; 0 = unlimited — debate until natural consensus, no vote).
        tctx.routed_rounds = int(ov["rounds"])
    if ov.get("instructions"):
        # Per-phase operator instructions: spliced into every turn's context
        # by build_context, right next to the phase playbook.
        tctx.phase_instructions = str(ov["instructions"])
    c["models"] = models
    tctx.resolved = resolved
    # Per-role overrides (roles.worker / roles.integrator) are applied later,
    # per lane/turn, by _apply_role_routing — stash the validated sub-dict.
    if _roles_ov:
        tctx.role_routing = _roles_ov
    emit("Phase '%s': model routing active (%s)."
         % (key, ", ".join("%s=%s" % (k, v) for k, v in sorted(ov.items()))))
    return c


def _apply_role_routing(rcfg, role):
    """Patch one per-call cfg copy with the phase's per-ROLE routing overrides
    (model_routing.json phases.<key>.roles.<role>): "worker" for each parallel
    build lane, "integrator" for the integrator's turn only — the cheap-
    workers/expensive-integrator split. ``rcfg`` MUST already be a per-call
    copy (dict(cfg)); models/_resolved are re-copied here so the phase-shared
    dicts are never mutated. Returns rcfg for chaining."""
    rov = (rcfg.get("_role_routing") or {}).get(role) or {}
    if not rov:
        return rcfg
    models = dict(rcfg.get("models") or {})
    resolved = dict(rcfg.get("_resolved") or {})
    if rov.get("claude"):
        models["claude"] = resolved["claude_model"] = rov["claude"]
    if rov.get("codex"):
        models["codex"] = resolved["codex_model"] = rov["codex"]
    if rov.get("codex_reasoning"):
        models["codex_reasoning"] = rov["codex_reasoning"]
        models["codex_build_reasoning"] = rov["codex_reasoning"]
    if rov.get("claude_reasoning"):
        models["claude_reasoning"] = rov["claude_reasoning"]
        models["claude_build_reasoning"] = rov["claude_reasoning"]
    if rov.get("gemini") and valid_gemini_model(rov["gemini"]):
        models["gemini_fallback"] = rov["gemini"]
        resolved["gemini_model"] = rov["gemini"]
    if rov.get("ollama"):
        models["ollama"] = resolved["ollama_model"] = rov["ollama"]
    if rov.get("ollama_reasoning"):
        # See _apply_phase_routing: honored by run_local's HTTP "think" flag,
        # noop on the CLI "ollama run" roster path.
        models["ollama_reasoning"] = rov["ollama_reasoning"]
    # gemini_reasoning: accepted-but-noop (warned once at phase-routing time;
    # the gemini CLI exposes no effort control as invoked here).
    rcfg["models"] = models
    tcxlib.TurnContext(rcfg).resolved = resolved
    return rcfg


def _escalated_effort(current):
    """One-step bump for a reasoning-effort string: 'high' -> 'max', anything
    else (including blank/unrecognized) -> 'high'. Deliberately not a full
    ladder walk — the spec asks for exactly this two-rung bump."""
    return "max" if str(current or "").strip().lower() == "high" else "high"


def _apply_adaptive_escalation(cfg, coord):
    """Failure-triggered escalation lever (runtime.adaptive_escalation_enabled,
    default True): bump the acting coordinator's reasoning effort for exactly
    ONE call. Returns a NEW cfg dict — ``cfg`` itself is never mutated — so
    escalation can never leak into a later unrelated phase or the next fresh
    verify cycle; the caller just stops passing the escalated copy once the
    triggering attempt(s) are done.

    Default lever: bump models.<agent>_reasoning / <agent>_build_reasoning to
    "high" (or "max" if already "high") — cheap, always available, mirrors the
    one-knob-sets-both convention in _apply_phase_routing/_apply_role_routing.
    Full model swap only happens if the operator explicitly set
    runtime.escalation_model_override (extends the models.claude_integrator
    single-agent-override pattern to the claude coordinator only).

    gemini/ollama have no reasoning-effort lever in this engine (see the
    accepted-but-noop notes on run_claude/run_codex/_apply_phase_routing), so
    for those agents this is a no-op cfg-wise; the caller still logs/ledgers
    the escalation event for visibility even though there's no lever to pull.
    """
    ecfg = dict(cfg)
    models = dict(ecfg.get("models") or {})
    if coord == "codex":
        new = _escalated_effort(models.get("codex_reasoning")
                                or cget(cfg, "models.codex_reasoning", "low"))
        models["codex_reasoning"] = new
        models["codex_build_reasoning"] = new
    elif coord == "claude":
        new = _escalated_effort(models.get("claude_reasoning")
                                or cget(cfg, "models.claude_reasoning", ""))
        models["claude_reasoning"] = new
        models["claude_build_reasoning"] = new
        override = str(cget(cfg, "runtime.escalation_model_override", "") or "").strip()
        if override:
            tcxlib.TurnContext(ecfg).claude_model_override = override
    ecfg["models"] = models
    return ecfg


def _maybe_escalate(cfg, app, app_dir, key, coord, attempt_no, already_logged):
    """Shared by _verify_and_repair's repair loop and process_phase's quality-
    gate retry path: both are ONLY reachable via repeated failures on the same
    phase, so escalating here can never affect a normal successful run.

    ``attempt_no`` is 1-based (repair attempt number / quality-gate call
    number). Escalates from runtime.escalate_repair_after_attempt (default 2)
    onward. Returns (cfg_to_use, logged) — logged flips True the first time
    escalation actually triggers for this phase invocation, so the caller
    emits the log line + mistakes-ledger entry exactly once per escalation
    event, not once per subsequent (already-escalated) attempt.

    cls choice: a new mistakes.CLASSES value "escalation_triggered" rather
    than reusing "repair_queued" — repair_queued means "the release gate
    refused done and queued a whole repair phase", a different (and rarer)
    event than "this in-phase repair/quality-gate attempt got a stronger
    model". Keeping them distinct lets `--mistakes` answer "how often does
    escalation actually fire" without conflating it with release-gate queues.
    """
    if not bool(cget(cfg, "runtime.adaptive_escalation_enabled", True)):
        return cfg, already_logged
    threshold = int(cget(cfg, "runtime.escalate_repair_after_attempt", 2) or 2)
    if attempt_no < threshold:
        return cfg, already_logged
    ecfg = _apply_adaptive_escalation(cfg, coord)
    if not already_logged:
        emit("ADAPTIVE ESCALATION: phase '%s' attempt %d crossed "
             "escalate_repair_after_attempt=%d — bumping %s's reasoning effort "
             "for this and subsequent attempts in this loop only."
             % (key, attempt_no, threshold, DISPLAY.get(coord, coord)))
        mistklib.append_mistake(app_dir, {
            "app": app, "workflow": cfg.get("_workflow_name"), "phase": key,
            "agent": coord, "cls": "escalation_triggered",
            "summary": "adaptive escalation triggered at attempt %d "
                       "(threshold %d)" % (attempt_no, threshold)})
        already_logged = True
    return ecfg, already_logged


_CONV_ROUND_RE = re.compile(r"\n### Round \d+\n")


def _run_roster_turns(cfg, app, app_dir, phasedef, original_prompt,
                      prior_outputs, *,
                      rnd, round_agents, personas, extra, md_path,
                      transcript, ctx_transcript, _seen_chars,
                      last_substantive, unit, is_build,
                      independent_first, conversational):
    """One round of roster turns — the machinery BOTH loops share
    (V3 board 2.2d): concurrent turns against a frozen context,
    roster-order append with the PASS protocol and skipped-unavailable
    notes. The callers own everything around it (debate: step-in
    barrier, coordinator, consensus; chat: drains, waits, retry).
    ctx_transcript is the SEMANTIC seam: debate passes the round-open
    frozen view (empty on an independent-first round 1, so delta
    offsets deliberately restart at 0 and round 2 re-covers it);
    conversational passes the live post-drain transcript so the human
    block rides the claude delta. One string feeds all three uses —
    build_context, the delta slice, and the offset update.
    last_substantive collects in BOTH modes (chat hands a throwaway
    dict — no vote in a chat; a mode branch here isn't worth it).
    """
    key, folder, fname, _purpose = phasedef
    round_produced = 0
    parallel_rounds = bool(cget(cfg, "runtime.parallel_discussion_rounds", True)) \
        and len(round_agents) > 1

    def _roster_turn(agent):
        # Per-thread copy: session/health flags must not race.
        acfg = tcxlib.TurnContext(cfg).thread_copy(health_key=agent)
        ctx = build_context(acfg, app, phasedef, original_prompt, prior_outputs,
                            ctx_transcript)
        persona_text = roleslib.persona_preamble(personas.get(agent))
        prompt = prompt_discuss(acfg, agent, ctx, phasedef, rnd, extra=extra,
                                persona=persona_text,
                                independent_first=(independent_first and rnd == 1),
                                conversational=conversational)
        delta = _delta_discuss_prompt(
            acfg, agent, ctx_transcript[_seen_chars.get(agent, 0):],
            rnd, extra=extra, persona=persona_text) if agent == "claude" else None
        _seen_chars[agent] = len(ctx_transcript)
        return call_agent_sessioned(acfg, app, key, rnd, agent, prompt,
                                    delta_prompt=delta,
                                    session_key="%s:%s:discuss" % (key, agent))

    results_by_agent = {}
    if parallel_rounds:
        with concurrent.futures.ThreadPoolExecutor(
                max_workers=len(round_agents)) as ex:
            futs = {ex.submit(_roster_turn, a): a for a in round_agents}
            for fut in concurrent.futures.as_completed(futs):
                try:
                    results_by_agent[futs[fut]] = (fut.result(), None)
                except AgentError as exc:
                    results_by_agent[futs[fut]] = (None, str(exc))
                except Exception as exc:  # noqa: BLE001 - one turn must not kill the phase
                    results_by_agent[futs[fut]] = (None, "unexpected turn error: %s" % exc)
    else:
        for agent in round_agents:
            try:
                results_by_agent[agent] = (_roster_turn(agent), None)
            except AgentError as exc:
                results_by_agent[agent] = (None, str(exc))
    # Append in roster order so the transcript stays deterministic.
    for agent in round_agents:
        resp, aerr = results_by_agent.get(agent, (None, "no result"))
        plabel = roleslib.persona_label(personas.get(agent))
        hat = " (%s)" % plabel if plabel else ""
        block_kind = "turn"
        if aerr:
            # Resilient: an unavailable/logged-out agent is skipped with a note
            # so the phase keeps going with whoever IS available (they join
            # back the moment their CLI is logged in). Not fabricated — absent.
            emit("%s unavailable in %s %d: %s" % (DISPLAY[agent], unit, rnd, aerr))
            block = "**%s — %s %d (skipped: CLI unavailable)**\n\n_%s_\n" % (
                DISPLAY[agent], "Iteration" if is_build else "Round", rnd, aerr)
            block_kind = "skip"
        elif not is_build and rnd > 1 \
                and (resp or "").strip().upper().rstrip(".!").strip() == "PASS":
            # PASS protocol: an honest abstention is recorded as one line,
            # keeps the round alive (everyone passing signals convergence
            # for the coordinator), and shrinks every later turn's context.
            round_produced += 1
            block = "**%s%s — Round %d**\n\n_PASS — nothing new to add._\n" % (
                DISPLAY[agent], hat, rnd)
            block_kind = "pass"
            live_log(app_dir, key, agent, "agent_turn_completed", "PASS")
            emit("%s passed in round %d (nothing new)." % (DISPLAY[agent], rnd))
        else:
            round_produced += 1
            # Each agent's newest substantive (non-PASS) post is what a
            # deterministic vote tally adopts as the winning proposal.
            last_substantive[agent] = resp
            block = "**%s%s — %s %d**\n\n%s\n" % (DISPLAY[agent], hat,
                                                "Iteration" if is_build else "Round", rnd, resp)
            live_log(app_dir, key, agent, "agent_turn_completed", resp)
            emit("Appended %s response to %s/%s" % (DISPLAY[agent], folder, fname))
        append_md(md_path, "\n" + block)
        msglib.append_message(app_dir, key, block_kind, agent, md_path,
                              persona=plabel, rnd=rnd)
        transcript += "\n" + block
    return transcript, round_produced


def _run_conversational_phase(cfg, app, app_dir, phasedef, original_prompt,
                              prior_outputs, state, md_path, personas, active):
    """V3 board 1.1: human-paced chat phase — no coordinator/consensus/vote.

    Round shape: drain human_inbox -> every roster agent responds (concurrent,
    roster-order append, PASS protocol + unavailable-skip notes preserved) ->
    block in _await_inbox until the next human message or an explicit end
    command. Differences from the debate loop, each deliberate:

    - `rounds` is IGNORED (unbounded itertools.count): Phase.rounds defaults
      to 6 and the GUI WorkflowBuilder clamps rounds to 1..9 on save, so
      honoring it would silently kill a chat mid-conversation. The phase ends
      only on end command, idle timeout, shutdown, or sprint deadline.
    - Resume is APPEND-ONLY: _resume_round_state counts a round complete only
      when a '**Coordinator (' block follows it — a conversational transcript
      has none, so the stock heuristic would discard EVERY round. We recover
      the file verbatim and continue at the next round number.
    - independent_first is forced off: round 1's context must include the
      freshly drained human message, not a blank room.
    - final_output is a short closure note, NOT the transcript: the shared
      footer/state/docs plumbing would otherwise duplicate the entire chat
      three times (Final Output section, phase_outputs, docs render).
    - No "no enabled agent could produce output" raise: an all-unavailable
      round appends skip notes and waits — the human sees it and can end.
      Progress is human-gated, so there is no runaway loop to guard against.
    """
    key, folder, fname, _purpose = phasedef
    extra = phase_extra(cfg, key)
    end_file = os.path.join(app_dir, "approvals", "%s.ok" % key)

    # Append-only resume or fresh start. Same resume gate as the debate path
    # (state says THIS phase was in flight and at least one round was entered)
    # so a stale leftover file from an unrelated run is still truncated.
    resume_round, transcript, resuming = 1, "", False
    if state.get("current_phase") == key and int(state.get("current_round") or 0) > 0:
        try:
            with open(md_path, encoding="utf-8") as fh:
                _existing = fh.read()
        except OSError:
            _existing = ""
        if _existing.strip():
            resuming = True
            _m = _CONV_ROUND_RE.search(_existing)
            transcript = _existing[_m.start():] if _m else ""
            resume_round = len(_CONV_ROUND_RE.findall(_existing)) + 1
    if resuming:
        emit("Conversation '%s': resuming append-only at round %d (%d char(s) "
             "recovered; no rounds discarded)." % (key, resume_round, len(transcript)))
    else:
        write_md(md_path, phase_header(app, phasedef, original_prompt))
        msglib.reconcile_messages(app_dir, key, keep_below_round=1,
                                  drop_post_round=True)
        # A leftover end command is stale only on a FRESH start. On the resume
        # path above it is deliberately kept: the user ended a chat the engine
        # crashed out of, and that command must still be honored.
        try:
            os.remove(end_file)
        except OSError:
            pass

    state["current_phase"] = key
    save_state(app_dir, state)

    _retry_seq = {}   # (round, agent) -> count; ids stay unique on re-retry
    idle_timeout = _approval_timeout(cfg)
    end_reason = None
    rounds_run = resume_round - 1
    _seen_chars = {}
    _last_substantive = {}   # collected but unused — no vote in a chat
    # V3 board 1.11: a mid-chat model swap edits <chat>/model_routing.json;
    # the once-per-run routing cache would ignore it. Conversational phases
    # ONLY re-resolve routing at each round open when the file's stat
    # changes — non-conversational workflows keep the once-per-run cache.
    _routing_file = os.path.join(app_dir, mrlib.ROUTING_FILENAME)
    def _routing_stat():
        try:
            s = os.stat(_routing_file)
            return (s.st_mtime_ns, s.st_size)
        except OSError:
            return None
    _last_routing = _routing_stat()
    for rnd in itertools.count(resume_round):
        if cfg.get("_phase_deadline") and time.time() >= cfg["_phase_deadline"]:
            end_reason = "time budget reached"
            break
        if _SHUTDOWN.is_set():
            end_reason = "engine shutdown"
            break
        _cur_routing = _routing_stat()
        if _cur_routing != _last_routing:
            _last_routing = _cur_routing
            base = dict(cfg)
            # Force a fresh load (fleet + chat overlay).
            tcxlib.TurnContext(base).routing = None
            cfg = _apply_phase_routing(base, key)
            emit("Chat routing updated — new model assignments apply from "
                 "round %d." % rnd)
        state["current_round"] = rnd
        save_state(app_dir, state)
        unit_label = "Round %d" % rnd
        append_md(md_path, "\n### Round %d\n\n" % rnd)
        evlib.emit_event(app_dir, "conversation_round", project=app, phase=key,
                         round=rnd)
        transcript, _drained = _drain_inbox_message(
            app_dir, md_path, transcript, unit_label,
            phase_key=key, rnd=rnd, slot="open")
        # V3 4.6: an @<Section> line fires Quick-take / Deep-dive; anything
        # else is a pure no-op, so a chat that never @-mentions stays
        # transcript-byte-identical.
        transcript = _handle_chat_delegation(
            cfg, app, app_dir, phasedef, key, rnd, original_prompt,
            prior_outputs, personas, active, transcript, md_path, _drained,
            extra=extra)
        round_agents = ordered_agents(active)
        state["next_agent"] = "+".join(round_agents)
        save_state(app_dir, state)

        transcript, _produced = _run_roster_turns(
            cfg, app, app_dir, phasedef, original_prompt, prior_outputs,
            rnd=rnd, round_agents=round_agents, personas=personas, extra=extra,
            md_path=md_path, transcript=transcript, ctx_transcript=transcript,
            _seen_chars=_seen_chars, last_substantive=_last_substantive,
            unit="round", is_build=False, independent_first=False,
            conversational=True)
        rounds_run = rnd

        decision, _payload = _await_inbox(cfg, app, app_dir, key, state,
                                          timeout=idle_timeout)
        while decision == "retry":
            # Stateless re-run of ONE agent's turn on the requested model,
            # appended as an explicitly labeled block; the original message
            # is preserved and the round does not advance.
            _ragent = str(_payload.get("agent") or "")
            _rmodel = str(_payload.get("model") or "")
            if _ragent not in round_agents:
                emit("Retry ignored — %r is not in this chat's roster." % _ragent)
            else:
                acfg = tcxlib.TurnContext(cfg).thread_copy(
                    health_key=_ragent, stateless=True)
                ident = _ragent
                if _rmodel.startswith("local:"):
                    ident = _rmodel
                else:
                    _patch_agent_model(acfg, _ragent, _rmodel)
                rctx = build_context(acfg, app, phasedef, original_prompt,
                                     prior_outputs, transcript)
                rprompt = prompt_discuss(
                    acfg, _ragent, rctx, phasedef, rnd, extra=extra,
                    persona=roleslib.persona_preamble(personas.get(_ragent)),
                    conversational=True)
                try:
                    rresp = call_agent(acfg, app, key, rnd, ident, rprompt)
                    rblock = "**%s (retry on %s) — Round %d**\n\n%s\n" % (
                        DISPLAY.get(_ragent, _ragent), _rmodel, rnd, rresp)
                    append_md(md_path, "\n" + rblock)
                    _retry_seq[(rnd, _ragent)] = \
                        _retry_seq.get((rnd, _ragent), 0) + 1
                    msglib.append_message(app_dir, key, "retry", _ragent,
                                          md_path, rnd=rnd,
                                          seq=_retry_seq[(rnd, _ragent)])
                    transcript += "\n" + rblock
                    emit("Retry: %s re-answered round %d on %s."
                         % (DISPLAY.get(_ragent, _ragent), rnd, _rmodel))
                except AgentError as exc:
                    emit("Retry on %s failed: %s" % (_rmodel, exc))
            decision, _payload = _await_inbox(cfg, app, app_dir, key, state,
                                              timeout=idle_timeout)
        if decision == "message":
            continue
        if decision == "end":
            # End wins over a pending message, but the message must not rot in
            # the inbox forever (nothing drains it after the phase completes):
            # fold it in as a closing section, then finalize.
            transcript = drain_human_inbox(app_dir, md_path, transcript,
                                           "closing message", phase_key=key,
                                           rnd=rnd, slot="closing")
            end_reason = "ended by user"
        elif decision == "timeout":
            end_reason = "conversation idle timeout"
        elif decision == "deadline":
            end_reason = "time budget reached"
        else:   # shutdown
            end_reason = "engine shutdown"
        break

    # Finalize honestly: no coordinator, no consensus, no fabricated markers.
    closure = ("Conversation closed after %d round(s): %s. The transcript "
               "above is the record of this chat." % (rounds_run, end_reason))
    # The literal '## Coordinator Decision' heading is load-bearing: the GUI
    # transcript parser terminates the message stream at it and only renders
    # a Final Output section when it exists — so keep the heading and make
    # its BODY honest instead of dropping it.
    append_md(md_path, "\n## Coordinator Decision\n\n_No coordinator — "
                       "conversational phase; %s._\n" % end_reason)
    append_md(md_path, "\n## Final Output\n\n%s\n" % closure)
    append_md(md_path, "\n---\n\n%s\n" % ("ENDED BY USER"
              if end_reason == "ended by user"
              else "CONVERSATION CLOSED: %s" % end_reason.upper()))

    state["consensus_status"][key] = False
    # Explicit end-reason state (R4): 'ended by user' is a CLEAN close and
    # must not land in phase_resolutions (that dict means "closed without
    # clean resolution" and drives the GUI's amber badges + docs wording);
    # an idle timeout genuinely is an unresolved close, so it does.
    state.setdefault("conversation_end", {})[key] = end_reason
    if end_reason == "conversation idle timeout":
        state.setdefault("phase_resolutions", {})[key] = "idle_timeout"
    state["phase_outputs"][key] = closure
    if key not in state["completed_phases"]:
        state["completed_phases"].append(key)
    state["current_round"] = 0
    state["next_agent"] = None
    save_state(app_dir, state)
    # Deliberate SUBSET of end_phase(): the six other band-B keys are never
    # set on the conversational path; clearing them here would be silent
    # semantic drift, not hygiene.
    tctx = tcxlib.TurnContext(cfg)
    tctx.phase_playbook = ""
    tctx.knowledge = ""
    tctx.artifact_context = ""
    tctx.memory = ""
    tctx.verify_context = ""
    live_log(app_dir, key, "orchestrator", "phase_completed",
             "conversational phase '%s' complete (%s)" % (key, end_reason))
    evlib.emit_event(app_dir, "phase_completed", project=app, phase=key,
                     detail="conversational: %s" % end_reason)
    emit("Conversation '%s' complete for %s (%s)." % (key, app, end_reason))
    return closure


def _run_debate_rounds(cfg, app, app_dir, phasedef, original_prompt,
                       prior_outputs, state, *,
                       md_path, transcript, extra, personas, active, coord,
                       rounds_iter, resuming, unit, is_build, is_verify_repair,
                       unlimited_rounds, max_rounds, independent_first,
                       quality_repair_limit, step_in_marker,
                       consensus, final_output):
    """The sequential round-barrier debate loop, extracted from
    process_phase (V3 board 2.2a; roster-turn machinery further shared with
    the conversational loop via _run_roster_turns in 2.2d — the one
    post-extraction edit: any_agent_output is now derived from
    round_produced at the call site, provably equivalent because the two
    were incremented in lockstep). Behavior, save_state ordering, and
    transcript bytes are contract (tests/test_transcript_golden.py).
    Seam notes:
    - consensus/final_output arrive as IN/OUT seeds: an enabled parallel
      build has already produced them and flows through here with
      rounds_iter=[] (zero iterations) exactly as it flowed through the
      inline loop scaffolding before the extraction.
    - rounds_iter is BUILT BY THE CALLER and iterated exactly once here;
      process_phase still truthiness-checks the same object afterward
      (three shapes: [] for builds, range for budgeted phases — falsy when
      resume lands past the budget — and infinite itertools.count).
    - state/cfg cross by IDENTITY: current_round/next_agent/
      phase_resolutions and _noted_indep_grader must land in the caller's
      objects; the per-thread dict(cfg) copies inside stay put (2.3 relies
      on those exact copy points).
    - any_agent_output starts as `resuming`: a resumed phase has real
      output on disk even if every agent is down on the first resumed
      round — the caller's no-output guard must not fire then.
    """
    key, folder, fname, _purpose = phasedef
    quality_failures = 0
    _quality_escalation_logged = False   # see _maybe_escalate; scoped to this phase call
    # A resumed phase already has real agent output on disk (recovered into
    # `transcript` above) even before this loop produces anything new — the
    # "no enabled agent could produce output" guard below must not fire just
    # because every agent happens to be unavailable on the FIRST resumed round.
    any_agent_output = resuming
    empty_round_streak = 0   # consecutive rounds where NO agent spoke
    _seen_chars = {}   # per-agent transcript offset for session delta prompts
    last_substantive = {}   # per-agent newest non-PASS post (vote-tally source)
    for rnd in rounds_iter:
        # Sprint watchdog: if this phase's time slice is spent, finalize with the
        # best output so far instead of starting another round.
        if cfg.get("_phase_deadline") and time.time() >= cfg["_phase_deadline"]:
            emit("Sprint: time budget for phase '%s' reached — finalizing at round %d." % (key, rnd))
            break
        if _SHUTDOWN.is_set():
            break
        # V3 board 1.7 ("Step in"): the marker check + wait sit BEFORE the
        # round header/drain, deliberately — while waiting, the human's
        # message stays safely in the inbox, so a crash during the pause
        # loses nothing (resume re-enters this barrier and re-honors the
        # marker). Draining first and then waiting would let a crash truncate
        # the round (coordinator-less) AND find the inbox already emptied.
        stepping_in = os.path.exists(step_in_marker)
        if stepping_in:
            evlib.emit_event(app_dir, "step_in_requested", project=app,
                             phase=key, round=rnd)
            if not _read_nonblank(os.path.join(app_dir, "human_inbox.txt")):
                # The GUI writes the marker FIRST, then the message — this
                # wait only covers that one-file-write race, so its timeout
                # is seconds, never the 2h approval timeout.
                emit("Step-in requested — waiting briefly for your message "
                     "before %s %d." % ("iteration" if is_build else "round", rnd))
                _await_step_in(cfg, app_dir,
                               timeout=float(cget(cfg, "runtime.step_in_wait_seconds", 20) or 20))
        state["current_round"] = rnd
        save_state(app_dir, state)
        append_md(md_path, "\n### %s %d\n\n" % ("Iteration" if is_build else "Round", rnd))

        unit_label = "%s %d" % ("Iteration" if is_build else "Round", rnd)
        _pre_drain_len = len(transcript)
        transcript = drain_human_inbox(app_dir, md_path, transcript, unit_label,
                                       phase_key=key, rnd=rnd, slot="open")
        human_joined = len(transcript) > _pre_drain_len
        if stepping_in:
            try:
                os.remove(step_in_marker)
            except OSError:
                pass
            if human_joined:
                evlib.emit_event(app_dir, "step_in_joined", project=app,
                                 phase=key, round=rnd)
                emit("You joined the conversation at %s %d." %
                     ("iteration" if is_build else "round", rnd))
            else:
                # Stale marker / empty inbox after the bounded wait: proceed
                # honestly instead of stalling the debate (never a deadlock).
                evlib.emit_event(app_dir, "step_in_missed", project=app,
                                 phase=key, round=rnd, detail="no message arrived")
                emit("Step-in marker had no message — continuing %s %d."
                     % ("iteration" if is_build else "round", rnd))
        round_agents = ordered_agents(active)
        state["next_agent"] = "+".join(round_agents)
        save_state(app_dir, state)
        # Round-barrier debate: every agent in a round reacts to the transcript
        # as it stood when the round OPENED (rounds 2+ include everyone's last-
        # round posts and the coordinator's decision), so the turns run
        # CONCURRENTLY — round wall-clock is the slowest agent, not the sum.
        # A freshly drained human block overrides independent-first hiding:
        # round 1's blank-room rule must never hide the human the agents are
        # supposed to be reacting to (V3 board 1.7 review).
        round_ctx_transcript = "" if (independent_first and rnd == 1
                                      and not human_joined) else transcript
        transcript, round_produced = _run_roster_turns(
            cfg, app, app_dir, phasedef, original_prompt, prior_outputs,
            rnd=rnd, round_agents=round_agents, personas=personas, extra=extra,
            md_path=md_path, transcript=transcript,
            ctx_transcript=round_ctx_transcript, _seen_chars=_seen_chars,
            last_substantive=last_substantive, unit=unit, is_build=is_build,
            independent_first=independent_first, conversational=False)
        if round_produced:
            any_agent_output = True

        # Resilience: one empty round must NOT abort the phase — agents recover
        # from cooldowns and rate limits between rounds. Skip the coordinator
        # turn (nothing new to judge) and try the next round; only a persistent
        # streak of fully-empty rounds ends the phase (the any_agent_output
        # guard below then reports it honestly).
        if round_produced == 0:
            empty_round_streak += 1
            if empty_round_streak >= 3:
                emit("Phase '%s': %d consecutive empty %ss — stopping the loop."
                     % (key, empty_round_streak, unit))
                break
            emit("Phase '%s': no agent produced output in %s %d — retrying with "
                 "whoever recovers next %s." % (key, unit, rnd, unit))
            continue
        empty_round_streak = 0

        # Coordinator turn
        _pre_coord_len = len(transcript)
        transcript = drain_human_inbox(app_dir, md_path, transcript, unit_label,
                                       phase_key=key, rnd=rnd, slot="coord")
        # V3 board 1.7: a step-in message can land while agents were talking —
        # this pre-coordinator drain then folds it a round early. That IS the
        # join (the message is in the transcript; the marker is consumed), but
        # only the coordinator has seen it, so consensus THIS round is
        # suppressed (below) — the next round's agents must genuinely respond
        # before the phase may close. Exception: the final budgeted round
        # falls through to the forced vote rather than overrunning the budget.
        _step_joined_pre_coord = False
        if not is_build and len(transcript) > _pre_coord_len \
                and os.path.exists(step_in_marker):
            try:
                os.remove(step_in_marker)
            except OSError:
                pass
            _step_joined_pre_coord = unlimited_rounds or rnd < max_rounds
            evlib.emit_event(app_dir, "step_in_joined", project=app, phase=key,
                             round=rnd)
            emit("You joined the conversation at %s %d (agents respond next %s)."
                 % ("iteration" if is_build else "round", rnd, unit))
        state["next_agent"] = coord
        save_state(app_dir, state)
        ctx = build_context(cfg, app, phasedef, original_prompt, prior_outputs, transcript)
        cprompt = prompt_coordinate(cfg, coord, ctx, phasedef, rnd,
                                    is_build=is_build,
                                    final_round=(not unlimited_rounds and rnd == max_rounds))
        coord_delta = None
        if coord == "claude":
            # Resumed session: only the new blocks + the coordinate instructions
            # (prompt_coordinate with an empty ctx yields just the instructions).
            coord_delta = (
                "===== NEW MESSAGES SINCE YOUR LAST DECISION =====\n"
                + transcript[_seen_chars.get("__coord__", 0):]
                + "\n"
                + prompt_coordinate(cfg, coord, "", phasedef, rnd, is_build=is_build,
                                    final_round=(not unlimited_rounds and rnd == max_rounds)))
        _seen_chars["__coord__"] = len(transcript)
        # The decision turn must survive on ANY working model: try the preferred
        # coordinator, then every other live participant as a stand-in — local
        # models included, as the last resort when no cloud agent can decide.
        cand_order = _coordinator_candidates(cfg, active, preferred=coord)
        for a in ordered_agents(active):
            if a not in cand_order:
                cand_order.append(a)
        cresp, acting_coord = None, coord
        for cand in cand_order:
            try:
                if cand == coord:
                    cresp = call_agent_sessioned(dict(cfg), app, key, rnd, coord, cprompt,
                                                 delta_prompt=coord_delta,
                                                 session_key="%s:%s:coord" % (key, coord))
                else:
                    emit("Coordinator (%s) unavailable in %s %d — %s standing in."
                         % (DISPLAY[coord], unit, rnd, DISPLAY[cand]))
                    sprompt = prompt_coordinate(cfg, cand, ctx, phasedef, rnd,
                                                is_build=is_build,
                                                final_round=(not unlimited_rounds and rnd == max_rounds))
                    cresp = call_agent_sessioned(dict(cfg), app, key, rnd, cand, sprompt,
                                                 session_key="%s:%s:coord" % (key, cand))
                acting_coord = cand
                break
            except AgentError as exc:
                emit("%s could not coordinate %s %d: %s" % (DISPLAY[cand], unit, rnd, exc))
        if cresp is None:
            emit("No agent could coordinate %s %d in phase '%s' — continuing."
                 % (unit, rnd, key))
            continue
        cblock = "**Coordinator (%s) — decision after %s %d**\n\n%s\n" % (
            DISPLAY[acting_coord], "iteration" if is_build else "round", rnd, cresp)
        append_md(md_path, "\n" + cblock)
        msglib.append_message(app_dir, key, "coordinator", acting_coord,
                              md_path, rnd=rnd)
        transcript += "\n" + cblock
        live_log(app_dir, key, acting_coord, "agent_turn_completed", cresp)
        emit("Appended Coordinator (%s) decision to %s/%s" % (DISPLAY[acting_coord], folder, fname))

        # Always keep the latest coordinator recap as the working decision, so a
        # phase never ends with an empty result (matters for single-agent runs
        # where no forced vote runs).
        final_output = cresp
        if _step_joined_pre_coord and CONSENSUS_RE.search(cresp):
            # V3 board 1.7: the human's message arrived after this round's
            # agents spoke — only the coordinator saw it. A consensus now
            # would close the phase without any agent responding to the human;
            # defer it exactly one round (never past the round budget — the
            # guard above leaves _step_joined_pre_coord False on the final
            # round, falling through to the forced vote).
            emit("Consensus deferred for %s %d — you just joined; the agents "
                 "respond next %s before this phase can close." % (unit, rnd, unit))
            append_md(md_path, "\n_Consensus deferred one %s — the human just "
                               "joined and the agents respond first._\n" % unit)
            continue
        if CONSENSUS_RE.search(cresp):
            consensus = True
            if _phase_quality_gate_enabled(cfg, is_build or is_verify_repair):
                try:
                    # Same failure-triggered escalation as the repair loop:
                    # once this is the Nth-or-later quality-gate call for this
                    # phase (N = runtime.escalate_repair_after_attempt), the
                    # evaluator turn gets a stronger reasoning effort. `cfg`
                    # itself is untouched, so this never leaks past the phase.
                    # Independent grader: whoever ACTUALLY coordinated this
                    # round must not grade their own wrap-up. Escalation (a
                    # stronger reasoning effort on repeated failures) targets
                    # the grader, since that's who makes the repeated call.
                    _grader = pick_quality_evaluator(cfg, active, acting_coord)
                    if _grader != acting_coord and not cfg.get("_noted_indep_grader"):
                        tcxlib.TurnContext(cfg).noted_indep_grader = True
                        emit("Quality gate: %s grades the coordinator's wrap-up "
                             "(independent of %s)." % (DISPLAY[_grader], DISPLAY[acting_coord]))
                    qcfg, _quality_escalation_logged = _maybe_escalate(
                        cfg, app, app_dir, key, _grader,
                        quality_failures + 1, _quality_escalation_logged)
                    qctx = build_context(qcfg, app, phasedef, original_prompt,
                                         prior_outputs, transcript)
                    qpass, qresp, transcript = run_phase_quality_gate(
                        qcfg, app, app_dir, phasedef, rnd, coord, qctx,
                        cresp, md_path, transcript, evaluator=_grader)
                except AgentError as exc:
                    emit("Quality gate unavailable in phase '%s': %s — accepting coordinator decision."
                         % (key, exc))
                    break
                if qpass:
                    emit("Quality gate passed for phase '%s' at %s %d."
                         % (key, unit, rnd))
                    emit("CONSENSUS reached in phase '%s' at %s %d." % (key, unit, rnd))
                    break
                quality_failures += 1
                can_retry_round = unlimited_rounds or rnd < max_rounds
                can_retry_quality = quality_failures <= quality_repair_limit
                emit("Quality gate failed for phase '%s' at %s %d (%d/%d repair failures)."
                     % (key, unit, rnd, quality_failures, quality_repair_limit))
                if can_retry_round and can_retry_quality:
                    consensus = False
                    append_md(md_path, "\n_Quality gate requested another round before this phase can close._\n")
                    continue
                final_output = (
                    final_output.rstrip()
                    + "\n\n**Quality gate warning:** The evaluator still found gaps, "
                    + "but the phase could not run another repair round under the "
                    + "current settings.\n\n"
                    + _budget(qresp, 4000)
                )
                emit("Quality gate warning recorded for phase '%s'; closing under current limits."
                     % key)
                # Honest metadata: this phase closed WITHOUT a passing quality
                # gate. Downstream consumers (docs, GUI) can surface it instead
                # of the close reading as a clean consensus.
                state.setdefault("phase_resolutions", {})[key] = "quality_gate_warning"
                save_state(app_dir, state)
                mistklib.append_mistake(app_dir, {
                    "app": app, "workflow": cfg.get("_workflow_name"),
                    "phase": key, "agent": coord, "cls": "quality_gate_fail",
                    "summary": "phase closed with a failing quality gate after "
                               "%d repair failure(s)" % quality_failures})
                break
            emit("CONSENSUS reached in phase '%s' at %s %d." % (key, unit, rnd))
            break

    return consensus, final_output, transcript, last_substantive, any_agent_output


def _run_forced_vote(cfg, app, app_dir, phasedef, original_prompt,
                     prior_outputs, state, *,
                     md_path, transcript, unit, coord,
                     available_active, last_substantive, final_output):
    """Forced weighted vote: concurrent ballots, deterministic
    confidence-weighted tally, LLM-tally fallback — extracted VERBATIM
    from process_phase (V3 board 2.2b), dedented one level. Seam notes:
    - Returns (final_output, transcript, vote). transcript MUST be
      returned: the vote blocks are appended to it for downstream
      contract parsing, and losing the rebinding would stay byte-green
      (append_md writes the .md independently) while silently hiding
      ballots from _record_phase_contracts.
    - final_output arrives SEEDED with the coordinator's last recap:
      on the no-tally-agent path this function assigns nothing and the
      recap must survive to the return.
    - vote starts {} here AND at the call site's skeleton: the empty
      dict crossing back is load-bearing — `if vote:` gates
      state['vote_results'] and the undecided bookkeeping; the no-vote
      paths must keep writing nothing.
    - state crosses by identity (next_agent roster save happens BEFORE
      the ballots so a crash mid-vote shows who was voting).
    """
    key, folder, fname, _purpose = phasedef
    vote = {}
    emit("No consensus by max %s in phase '%s' — forcing a weighted vote." % (unit, key))
    append_md(md_path, "\n### Forced Vote (max %ss reached)\n\n" % unit)
    candidates = "the proposals advanced by " + ", ".join(DISPLAY[a] for a in available_active)
    voters = ordered_agents(available_active)
    state["next_agent"] = "+".join(voters)
    save_state(app_dir, state)
    # Ballots are independent — cast them concurrently (same round-barrier
    # pattern as discussion turns; wall-clock = slowest voter, not the sum).
    vote_ctx = build_context(cfg, app, phasedef, original_prompt, prior_outputs, transcript)

    def _vote_turn(agent):
        # Per-thread copy: session/health flags must not race.
        acfg = tcxlib.TurnContext(cfg).thread_copy(health_key=agent)
        return call_agent(acfg, app, key, "vote", agent,
                          prompt_vote(acfg, agent, vote_ctx, phasedef, candidates))

    vote_responses = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(voters)) as ex:
        futs = {ex.submit(_vote_turn, a): a for a in voters}
        for fut in concurrent.futures.as_completed(futs):
            try:
                vote_responses[futs[fut]] = fut.result()
            except AgentError as exc:
                emit("%s could not vote: %s" % (DISPLAY[futs[fut]], exc))
            except Exception as exc:  # noqa: BLE001 - one ballot must not kill the phase
                emit("%s vote failed unexpectedly: %s" % (DISPLAY[futs[fut]], exc))
    for agent in voters:   # transcript stays deterministic: roster order
        if agent in vote_responses:
            vblock = "**%s — vote**\n\n%s\n" % (DISPLAY[agent], vote_responses[agent])
            append_md(md_path, "\n" + vblock)
            msglib.append_message(app_dir, key, "vote", agent, md_path,
                                  token="final")
            transcript += "\n" + vblock
            emit("Appended %s vote to %s/%s" % (DISPLAY[agent], folder, fname))

    # Deterministic tally: parse the vote-json ballots and count them in
    # Python. The legacy LLM tally turn survives only as a fallback when
    # fewer than two ballots parse — a tally must never hinge on one
    # model's reading of prose.
    ballots = parse_vote_ballots(
        vote_responses, voters,
        on_error=lambda msg: emit("WARN BALLOT %s (phase '%s')" % (msg, key)))
    if len(ballots) >= 2:
        winner, weights = tally_votes(ballots, voters)
        weight_line = ", ".join("%s: %d" % (DISPLAY[a], w)
                                for a, w in sorted(weights.items(),
                                                   key=lambda kv: -kv[1]))
        tally_md = (
            "The orchestrator tallied %d valid ballot(s) (confidence-weighted): %s.\n\n"
            "**Decision: the group commits to %s's proposal.**\n\n"
            "## Final Output\n\n%s\n\nVOTE_DECISION: YES\n"
            % (len(ballots), weight_line, DISPLAY[winner],
               (last_substantive.get(winner) or
                "See %s's final position in the transcript above." % DISPLAY[winner])))
        append_md(md_path, "\n**Deterministic vote tally**\n\n" + tally_md)
        msglib.append_message(app_dir, key, "tally", "orchestrator", md_path,
                              token="final")
        final_output = tally_md
        vote = {"decided": True, "by": "ballot-tally", "winner": winner,
                "method": "ballots",
                "ballots": [{k: b[k] for k in ("voter", "choice", "confidence")}
                            for b in ballots]}
        emit("Vote tally complete for phase '%s' (deterministic; winner: %s; %s)."
             % (key, DISPLAY[winner], weight_line))
    else:
        emit("Only %d parseable ballot(s) in phase '%s' — falling back to an "
             "LLM tally." % (len(ballots), key))
        ctx = build_context(cfg, app, phasedef, original_prompt, prior_outputs, transcript)
        # Tally with the same failover as the decision turn: any working
        # participant may count the votes rather than losing the phase result.
        for cand in [coord] + [a for a in voters if a != coord]:
            try:
                tresp = call_agent(cfg, app, key, "tally", cand,
                                   prompt_tally(cfg, cand, ctx, phasedef))
                append_md(md_path, "\n**Coordinator (%s) — vote tally & decision**\n\n%s\n"
                          % (DISPLAY[cand], tresp))
                msglib.append_message(app_dir, key, "tally", cand, md_path,
                                      token="final")
                final_output = tresp
                vote = {"decided": bool(VOTE_RE.search(tresp)), "by": cand,
                        "method": "llm-fallback"}
                emit("Vote tally complete for phase '%s' (VOTE_DECISION: %s)."
                     % (key, "YES" if vote["decided"] else "UNCLEAR"))
                break
            except AgentError as exc:
                emit("%s could not tally the vote: %s" % (DISPLAY[cand], exc))
        else:
            emit("No agent could tally the vote in phase '%s' — keeping last recap." % key)
    return final_output, transcript, vote


# V3 board 2.2c: the phase-close hook sequence. Each hook is a VERBATIM
# lift of one tail block of process_phase (gating `if` kept INSIDE, so
# the runner stays a dumb loop and each hook is independently skippable).
# Every hook takes the full uniform signature and returns the
# (transcript, final_output) pair even when untouched — the uniformity is
# what makes the tuple callable in a loop; 2.3's TurnContext is the
# designated slimming pass. NOTE: _PHASE_CLOSE_HOOKS freezes function
# references at import — tests exercise hooks end-to-end, not by
# monkeypatching hook names. Order is CONTRACT: verify before signing
# (device settings enforced last), contracts after verify (transcript
# must include repair rounds), audit before the verification label (audit
# REPLACES final_output; the label APPENDS).
def _hook_sprint_verify_reserve(cfg, app, app_dir, phasedef, state, *,
        key, md_path, transcript, final_output, coord, active,
        is_build, is_verify_repair, allow_writes, _needs_vlabel,
        consensus=False):
    # Sprint: the build slice is now spent, but the run still has its verify
    # reserve. Hand the verify/repair pass the hard RUN deadline so compile +
    # repair can use that reserved tail.
    if cfg.get("_budget") and cfg.get("_deadline"):
        tcxlib.TurnContext(cfg).phase_deadline = cfg["_deadline"]
    return transcript, final_output


def _hook_verify_repair(cfg, app, app_dir, phasedef, state, *,
        key, md_path, transcript, final_output, coord, active,
        is_build, is_verify_repair, allow_writes, _needs_vlabel,
        consensus=False):
    # After an enabled build, actually compile it and run bounded repair
    # iterations on failure (the reliability gate). Runs while writes are still
    # allowed so repairs can edit files; before the signing fixup so device
    # settings are enforced last.
    verify_note = ""
    if (is_build or is_verify_repair) and allow_writes and cfg.get("_build_dir"):
        transcript, verify_note = _verify_and_repair(
            cfg, app, app_dir, phasedef, state, md_path, transcript, coord)
        if verify_note:
            final_output = (final_output.rstrip() + "\n\n**Build verification:** "
                            + verify_note) if final_output else verify_note
    return transcript, final_output


def _hook_ios_signing(cfg, app, app_dir, phasedef, state, *,
        key, md_path, transcript, final_output, coord, active,
        is_build, is_verify_repair, allow_writes, _needs_vlabel,
        consensus=False):
    # Deterministic safety net: after an enabled build, guarantee any generated
    # Xcode project is signable on a real device — regardless of what the build
    # agent wrote into the pbxproj.
    if (is_build or is_verify_repair) and allow_writes and cfg.get("_build_dir") \
            and bool(cget(cfg, "ios.enforce_signing", True)):
        try:
            fixed = fix_ios_signing(
                cfg["_build_dir"],
                team=str(cget(cfg, "ios.development_team", "") or ""),
                style=str(cget(cfg, "ios.code_sign_style", "Automatic") or "Automatic"),
                bundle_prefix=str(cget(cfg, "ios.bundle_id_prefix", "") or ""),
            )
            for p in fixed:
                emit("iOS signing fixup applied: %s" % p)
            if not fixed:
                emit("iOS signing check: no changes needed.")
        except OSError as exc:
            emit("WARN iOS signing fixup failed: %s" % exc)
    return transcript, final_output


def _hook_secret_scan(cfg, app, app_dir, phasedef, state, *,
        key, md_path, transcript, final_output, coord, active,
        is_build, is_verify_repair, allow_writes, _needs_vlabel,
        consensus=False):
    # V2 §17/§23: deterministic secret scan over the generated source, feeding
    # the secret_hardcoded launch-readiness gate. Runs after every build phase,
    # before docs render; each run replaces the previous secret_scan findings.
    if (is_build or is_verify_repair) and allow_writes and cfg.get("_build_dir"):
        sfinds = scan_build_secrets(cfg["_build_dir"])
        merge_secret_findings(app_dir, sfinds)
        for f in sfinds:
            emit("SECRET_SCAN: %s" % f["title"])
        emit("SECRET_SCAN: %d hardcoded secret(s) in app_build -> docs/findings.json."
             % len(sfinds))
        live_log(app_dir, key, "orchestrator", "secret_scan",
                 "%d hardcoded secret finding(s) in generated source" % len(sfinds))
    return transcript, final_output


def _hook_record_contracts(cfg, app, app_dir, phasedef, state, *,
        key, md_path, transcript, final_output, coord, active,
        is_build, is_verify_repair, allow_writes, _needs_vlabel,
        consensus=False):
    # V2 §19/§20: persist the machine contracts these phases emitted. Parsed from
    # the transcript + final output (last emission of an id/name wins, so the
    # coordinator's final revision beats any draft). Cycles are an error recorded
    # in tasks.json — never a crash.
    transcript, _unresolved_contract = _record_phase_contracts(
        cfg, app, app_dir, key, transcript, final_output,
        coord=coord, active=active, md_path=md_path,
        record_decisions=_decisions_contract_requested(cfg, phasedef))
    if _unresolved_contract:
        state.setdefault("phase_resolutions", {})[key] = _unresolved_contract
    return transcript, final_output


def _hook_flows_requirements_research(cfg, app, app_dir, phasedef, state, *,
        key, md_path, transcript, final_output, coord, active,
        is_build, is_verify_repair, allow_writes, _needs_vlabel,
        consensus=False):
    # Flows/requirements contracts: not part of _record_phase_contracts (that
    # covers tasks.json/interfaces.json/decisions.json/phase_summaries.json)
    # since these feed the UI-crawl and adherence gates specifically.
    if key == "task_assignments":
        blob = transcript + "\n" + (final_output or "")
        flows, ferrs = parse_flows_blocks(blob)
        for e in ferrs:
            emit("FLOWS: ERROR %s" % e)
        if flows:
            persist_flows(app_dir, flows)
            emit("FLOWS: %d user flow(s) -> flows.json (%d error(s)) — the "
                 "UI-crawl gate replays these against the built app."
                 % (len(flows), len(ferrs)))
    if key == "app_features":
        blob = transcript + "\n" + (final_output or "")
        reqs, rerrs = parse_requirements_blocks(blob)
        for e in rerrs:
            emit("REQUIREMENTS: ERROR %s" % e)
        if reqs:
            persist_requirements(app_dir, reqs)
            emit("REQUIREMENTS: %d requirement(s) -> requirements.json — the "
                 "adherence gate grades the build against exactly these."
                 % len(reqs))
    if key == "product_research" and bool(cget(cfg, "runtime.research_source_check", True)):
        _research_source_check(cfg, app, app_dir, final_output)
    return transcript, final_output


def _hook_library_mining(cfg, app, app_dir, phasedef, state, *,
        key, md_path, transcript, final_output, coord, active,
        is_build, is_verify_repair, allow_writes, _needs_vlabel,
        consensus=False):
    # library_mining: write the extraction plan to a convenient report file.
    if cfg.get("_workflow_target") == "library_mining" and key == "extraction_candidates":
        rep_dir = os.path.join(app_dir, "report")
        os.makedirs(rep_dir, exist_ok=True)
        header = ("# Reusable-Library Extraction Report — %s\n\n_Read-only analysis of "
                  "%d repos by the multi-agent orchestrator._\n\n" % (app, len(cfg.get("_target_paths") or [])))
        write_md(os.path.join(rep_dir, "LIBRARY_REPORT.md"), header + (final_output or ""))
        emit("LIBRARY_MINING: wrote report/LIBRARY_REPORT.md")
        # Follow-on (NEXT_MILESTONES): if the coordinator emitted an
        # extraction-json contract for its top candidate, scaffold it into a
        # real, COMPILABLE SPM package skeleton and prove it builds. The ENTIRE
        # block is exception-isolated — a scaffold/verify hiccup must never
        # fail the (read-only, already-complete) library_mining phase.
        try:
            pkg, perrs = parse_extraction_blocks(transcript + "\n" + (final_output or ""))
            for e in perrs:
                emit("LIBRARY_MINING extraction-json: %s" % e)
            if pkg:
                manifest = swiftscaffoldlib.scaffold_spm_package(
                    pkg, os.path.join(app_dir, "app_build"))
                if manifest.get("ok"):
                    emit("LIBRARY_MINING: scaffolded package '%s' (%d public API stub(s)) at %s"
                         % (pkg["package_name"], manifest["api_count"], manifest["package_dir"]))
                    if shutil.which("swift"):
                        res = verifylib.run_verification(manifest["package_dir"], {"type": "spm"})
                        verifylib.persist_verify_result(
                            app_dir, key, res, prompt_hash=state.get("prompt_hash"),
                            workflow=cfg.get("_workflow_name"))
                        emit("LIBRARY_MINING: swift build %s"
                             % verifylib.verification_status(res))
                else:
                    for e in manifest.get("errors", []):
                        emit("LIBRARY_MINING scaffold: %s" % e)
        except Exception as exc:  # never let the follow-on abort the phase
            emit("LIBRARY_MINING: package scaffold skipped (%s)" % exc)
    return transcript, final_output


def _hook_audit_report(cfg, app, app_dir, phasedef, state, *,
        key, md_path, transcript, final_output, coord, active,
        is_build, is_verify_repair, allow_writes, _needs_vlabel,
        consensus=False):
    # Audit report phase: synthesize ALL findings (from every audit phase, using the
    # FULL untruncated phase_outputs — not the char-budgeted context) into a ranked
    # findings.json + AUDIT_REPORT.md, and make the rendered report this phase's Final
    # Output so it persists to state + the GUI with no new plumbing.
    if cfg.get("_workflow_target") == "audit" and key == "report":
        blob = "\n".join(str(v) for v in state.get("phase_outputs", {}).values())
        blob += "\n" + (final_output or "")
        findings = _assign_ids(rank_findings(dedup_findings(parse_finding_blocks(blob))))
        rep_dir = os.path.join(app_dir, "report")
        os.makedirs(rep_dir, exist_ok=True)
        agents = ", ".join(DISPLAY[a] for a in active)
        summary = _FIND_STRIP_RE.sub("", final_output or "").strip()[:800]
        rendered = render_audit_report(findings, app, cfg.get("_target_path"),
                                       agents=agents, summary=summary)
        try:
            with open(os.path.join(rep_dir, "findings.json"), "w", encoding="utf-8") as fh:
                json.dump({"app": app, "target_path": cfg.get("_target_path"),
                           "count": len(findings), "findings": findings}, fh, indent=2)
        except OSError as exc:
            emit("WARN could not write findings.json: %s" % exc)
        write_md(os.path.join(rep_dir, "AUDIT_REPORT.md"), rendered)
        final_output = rendered
        emit("AUDIT: %d finding(s) -> report/findings.json + report/AUDIT_REPORT.md."
             % len(findings))
    return transcript, final_output


def _hook_verification_label(cfg, app, app_dir, phasedef, state, *,
        key, md_path, transcript, final_output, coord, active,
        is_build, is_verify_repair, allow_writes, _needs_vlabel,
        consensus=False):
    # Verification gate (§16): append the orchestrator-DERIVED VERIFICATION label to
    # a requires_verification phase's output, computed from the persisted structured
    # result — never trusting the agent's prose. verified|failed|unverified.
    if _needs_vlabel:
        _latest = verifylib.latest_verify_result(app_dir, prompt_hash=state.get("prompt_hash"))
        _vstatus = (_latest.get("status") if _latest else "unverified") or "unverified"
        _vlabel = "VERIFICATION: %s" % _vstatus.upper()
        if not _latest:
            _vlabel += "\n_(No structured verification result exists for this build.)_"
        final_output = final_output.rstrip() + "\n\n" + _vlabel
        emit("Phase '%s': %s (orchestrator-derived from verify_results.json)." % (key, _vlabel.splitlines()[0]))
    return transcript, final_output


def _hook_artifact_publish(cfg, app, app_dir, phasedef, state, *,
        key, md_path, transcript, final_output, coord, active,
        is_build, is_verify_repair, allow_writes, _needs_vlabel,
        consensus=False):
    # V3 4.2: materialize ```artifact-json``` blocks from the phase's
    # Final Output into the project artifact store, emitting one
    # artifact_published event per landed artifact (ids+paths only,
    # AFTER the durable publish — §13.2). This hook is LAST on purpose:
    # it must read the exact output being recorded, including the audit
    # replacement and the verification label, so a published body can
    # never diverge from the phase record. It scans final_output only —
    # a superseded draft in the transcript must not become an artifact.
    # (Consequence: an audit run's replaced report publishes nothing;
    # qa's finding_report artifacts arrive via 4.9's Promote.) The
    # ENTIRE body is exception-isolated — publication must never fail a
    # completed phase (§13.3).
    try:
        _project, section = _session_coords(cfg)
        def _warn(msg):
            emit("ARTIFACT: WARN %s" % msg)
        project_dir = os.path.join(cfg.get("root") or "", _project or "")
        if section and artifactslib.FENCE_TAG in (final_output or ""):
            registry = artifactslib.load_registry(
                HERE, on_error=lambda m: (
                    _warn(m),
                    evlib.emit_event(app_dir, "config_fallback",
                                     section=section,
                                     file=artifactslib.REGISTRY_BASENAME,
                                     error=str(m)[:300])))
            turn = ""
            for rec in msglib.read_messages(app_dir):
                tid = rec.get("turn_id") or ""
                if tid.startswith(key + ":"):
                    turn = tid
            src = {"section": section,
                   "session": os.path.basename(app_dir),
                   "phase": key, "turn": turn}
            # Crash-resume guard: a re-close of this same phase (publish
            # landed durably, skip-on-resume state didn't) must not mint
            # -2 duplicates of what it already published.
            seen = set()
            for m in artifactslib.list_artifacts(project_dir,
                                                 on_error=_warn):
                s = m.get("source") or {}
                if (s.get("session") == src["session"]
                        and s.get("phase") == key):
                    seen.add((m.get("type"), m.get("title"),
                              m.get("content_hash")))
            for aid in artifactslib.publish_from_output(
                    project_dir, final_output, src, registry,
                    on_error=_warn, dedupe_against=seen, consensus=consensus):
                meta = artifactslib.load_meta(project_dir, aid,
                                              on_error=_warn) or {}
                evlib.emit_event(app_dir, "artifact_published",
                                 artifact_id=aid,
                                 type=meta.get("type", ""),
                                 version=meta.get("version", 1),
                                 path="artifacts/%s" % aid,
                                 phase=key, project=app)
                emit("ARTIFACT: published '%s' (%s) to the project bus"
                     % (aid, meta.get("type", "?")))
        # V3 4.4 reply edge: if THIS session was delegated with a
        # reply_to, deliver an artifact-card reference back to the
        # originating session's inbox — exactly once, driven by the
        # DURABLE delegation ledger, not this run's publish list (a crash
        # between publish and append re-delivers on resume, where the
        # republish above returns no new ids). Non-delegated sessions have
        # no delegation.json, so this is a pure no-op → golden transcripts
        # are byte-unchanged.
        if _project:
            deleg = seslib.read_delegation(app_dir, on_error=_warn)
            # Only 'failed' is terminal here — NOT 'replied': a delegated
            # run can publish across multiple phase closes, and each new
            # artifact must still be delivered. deliver_reply's per-artifact
            # ledger makes redelivery of already-sent ones a no-op, so this
            # re-runs safely every close.
            if deleg and deleg.get("reply_to") and \
                    deleg.get("status") != "failed":
                sess = os.path.basename(app_dir)
                for m in artifactslib.list_artifacts(project_dir,
                                                     on_error=_warn):
                    if (m.get("source") or {}).get("session") != sess:
                        continue
                    card = {"id": m.get("id"),
                            "type": m.get("type", ""),
                            "version": m.get("version", 1),
                            "path": "%s/artifacts/%s"
                                    % (_project, m.get("id"))}
                    if seslib.deliver_reply(app_dir, card, on_error=_warn):
                        emit("ARTIFACT: replied '%s' to %s"
                             % (m.get("id"), deleg.get("reply_to")))
    except Exception as exc:  # never let publication fail the phase
        emit("ARTIFACT: publication skipped (%s)" % exc)
    return transcript, final_output


_PHASE_CLOSE_HOOKS = (
    _hook_sprint_verify_reserve,
    _hook_verify_repair,
    _hook_ios_signing,
    _hook_secret_scan,
    _hook_record_contracts,
    _hook_flows_requirements_research,
    _hook_library_mining,
    _hook_audit_report,
    _hook_verification_label,
    _hook_artifact_publish,
)


def process_phase(cfg, app, app_dir, phasedef, original_prompt, prior_outputs,
                  state, phase_index=0):
    key, folder, fname, _purpose = phasedef
    # V2 routing: everything below (roster, coordinator, build lanes, every
    # agent turn) sees this phase's model overrides through the scoped copy.
    cfg = _apply_phase_routing(cfg, key)
    # Band-B per-phase state rides this view over the ROUTED copy —
    # constructed any earlier it would write phase state onto the
    # original cfg and leak it across phases.
    tctx = tcxlib.TurnContext(cfg)
    # A phase that writes files is a "build" phase, regardless of its name — this
    # is what lets non-app workflows (e.g. productionize) have their own build.
    is_build = bool(phasedef.get("writes", False)) if hasattr(phasedef, "get") \
        else (key == "build_coordination")
    verify_spec = (phasedef.get("verify") if hasattr(phasedef, "get") else None)
    # A non-build phase with a verify spec is a verification/repair phase: it
    # discusses the current app_build output, then runs the same real verifier and
    # bounded repair loop without starting another parallel build iteration.
    is_verify_repair = bool(verify_spec) and not is_build
    # Per-phase round budget now lives in the workflow (GUI-editable); fall back
    # to the legacy config `rounds:` block, then a small default. A per-project
    # routing override (Plan tab) beats them all; its 0 = unlimited.
    raw_rounds = (phasedef.get("rounds") if hasattr(phasedef, "get") else None)
    if cfg.get("_routed_rounds") is not None:
        raw_rounds = cfg["_routed_rounds"]
    max_rounds = int(raw_rounds if raw_rounds is not None else cget(cfg, "rounds.%s" % key, 3))
    unlimited_rounds = max_rounds <= 0
    # V2 §7.2: completeness profile scales the round budget (min 1 for any included
    # phase — structurally-required phases must still get at least one round).
    _rmult = cfg.get("_round_multiplier")
    if _rmult and not unlimited_rounds:
        max_rounds = max(1, int(round(max_rounds * float(_rmult))))
    # Sprint/time-budget mode: tighten the per-turn timeout (short for chat, longer
    # for building) so a single hung turn can't eat the whole budget. Cleared to
    # None when the workflow declares no budget (every non-sprint workflow).
    _bud = cfg.get("_budget")
    tctx.turn_timeout = cfg.get("_routed_turn_timeout") or \
        (int(_bud.get("build_turn_timeout", 480)
             if (is_build or is_verify_repair)
             else _bud.get("chat_turn_timeout", 150)) if _bud else None)
    active = enabled_agents(cfg)
    if not active:
        raise AppError("No agents enabled in config.")
    # Prefer an installed agent as coordinator so the decision turn actually runs
    # even when some enabled CLIs aren't logged in yet.
    coord = _pick_coordinator(cfg, active)

    # Hand each active agent a (role, personality) for THIS phase. Personalities
    # rotate every phase so no agent is ever stuck to one voice.
    speaking = ordered_agents(active)
    phase_roles = phasedef.get("roles") if hasattr(phasedef, "get") else None
    _guests = []
    if phase_roles:
        # V3 3.5: "section:id" refs resolve to guest cast members from
        # that section's roles.json; dangling refs banner and DROP —
        # never a fabricated stand-in. Resolved once per phase, before
        # assign_personas, so resumed runs reproduce the identical cast.
        phase_roles, _guests = roleslib.resolve_phase_role_refs(
            phase_roles, os.path.join(HERE, "sections"),
            on_missing=lambda ref, why: evlib.emit_event(
                _event_app_dir(cfg, app), "config_fallback",
                ref=ref, phase=key, error=why))
    personas = roleslib.assign_personas(
        phase_index, speaking, cfg.get("_personalities", roleslib.DEFAULT_PERSONALITIES),
        list(cfg.get("_roles", roleslib.DEFAULT_ROLES)) + _guests, phase_roles,
        cfg.get("_agent_role_overrides", {}),
        None if _guests else cfg.get("_role_by_id"))
    if personas:
        emit("Personas — " + "; ".join(
            "%s: %s" % (DISPLAY[a], roleslib.persona_label(personas[a])) for a in speaking))

    # Verification gate (§16): a requires_verification phase (e.g. final review) is
    # fed the real persisted verification result as context. Set-or-CLEAR each phase.
    _needs_vlabel = (bool(phasedef.get("requires_verification", False)) if hasattr(phasedef, "get")
                     else False) or key == "final_review"
    if _needs_vlabel:
        _vr = verifylib.load_verify_results(app_dir)
        _latest = verifylib.latest_verify_result(app_dir, prompt_hash=state.get("prompt_hash"))
        tctx.verify_context = (
            "\n\n===== VERIFICATION RESULTS (structured, authoritative) =====\n"
            + verifylib.summarize_verify_results(_vr, _latest)
            + "\nThe final VERIFICATION label is derived by the orchestrator from this "
              "result, not chosen by you — explain it, don't override it.")
    else:
        tctx.verify_context = ""

    # Retrieve curated domain knowledge relevant to this phase and stash it so
    # build_context injects it into every turn this phase (the RAG "specialist").
    # Few-shot exemplar (fleet learning): a rated-good project's output for
    # THIS phase key, if one has been exported to knowledge/exemplars/.
    tctx.phase_exemplar = _load_phase_exemplar(key)
    tctx.phase_playbook = phaseruleslib.render_phase_playbook(
        HERE, cfg.get("_workflow_target", "app"), key,
        layers=_phase_rule_layers(cfg),
        on_fallback=_rules_fallback_banner(cfg))
    if cfg["_phase_playbook"]:
        emit("Injected phase playbook into phase '%s'." % key)
    tctx.knowledge = ""
    if knowlib.should_inject(key):
        domain = knowlib.domain_for(cget(cfg, "knowledge.domain", ""),
                                    cfg.get("_workflow_target", "app"),
                                    original_prompt, phasedef.purpose if hasattr(phasedef, "purpose") else _purpose)
        tctx.knowledge = knowlib.retrieve(
            HERE, domain,
            "%s %s" % (_purpose, original_prompt),
            max_chars=int(cget(cfg, "knowledge.max_chars", 6000)),
            top_k=int(cget(cfg, "knowledge.top_k", 3)))
        if cfg["_knowledge"]:
            emit("Injected %s knowledge (%d chars) into phase '%s'."
                 % (domain, len(cfg["_knowledge"]), key))

    # V3 4.7 PULL: passively retrieve relevant FINAL artifacts published by
    # OTHER sections of this project (self-echo excluded). Flat/legacy runs
    # have no project bus -> no-op, so unrouted transcripts stay identical.
    tctx.artifact_context = ""
    _a_project, _a_section = _session_coords(cfg)
    if _a_section:
        tctx.artifact_context = artifactslib.retrieve(
            os.path.join(cfg.get("root") or "", _a_project),
            "%s %s" % (_purpose, original_prompt),
            max_chars=int(cget(cfg, "runtime.max_artifact_context_chars",
                               6000)),
            top_k=int(cget(cfg, "runtime.artifact_context_top_k", 3)),
            sensitivity_filter=None,   # 8.5 swaps this one predicate
            on_error=lambda m: emit("ARTIFACT: WARN %s" % m),
            exclude_session=os.path.basename(app_dir),
            exclude_section=_a_section)
        if cfg["_artifact_context"]:
            emit("Injected %d chars of project artifacts into phase '%s'."
                 % (len(cfg["_artifact_context"]), key))

    # V3 4.13 FACTORY MEMORY: founder-pinned facts, at the same seam. Staged
    # UNCONDITIONALLY (global facts apply to flat/legacy runs too); a repo
    # with no memory/ dir retrieves '' -> injects nothing -> byte-identical.
    tctx.memory = memlib.retrieve(
        HERE, _a_project or "", _a_section or "",
        "%s %s" % (_purpose, original_prompt),
        max_chars=int(cget(cfg, "runtime.max_memory_chars", 3000)),
        on_error=lambda m: emit("MEMORY: WARN %s" % m))
    if cfg["_memory"]:
        emit("Injected %d chars of factory memory into phase '%s'."
             % (len(cfg["_memory"]), key))

    # During an enabled build phase, agents work (and write files) directly in a
    # persistent build folder; otherwise no writes are allowed.
    allow_writes = (is_build or is_verify_repair) and \
        bool(cget(cfg, "runtime.build_code_changes_enabled", False))
    tctx.allow_writes = allow_writes
    tctx.build_dir = os.path.join(app_dir, "app_build") if allow_writes else None
    if allow_writes and cfg.get("_workflow_target", "app") == "app" \
            and bool(cget(cfg, "runtime.golden_scaffold_enabled", True)):
        _seed_golden_scaffold(cfg["_build_dir"])
    # Session map must exist on the ORIGINAL cfg before any per-thread shallow
    # copies are taken, or each copy would grow its own map and lose sessions.
    tctx.session_map("claude")
    tctx.session_map("codex")
    # Stable per-app cwd for resumed claude sessions (sessions are keyed by cwd,
    # so the default ephemeral temp dir would orphan them each turn). Discussion
    # phases stay read-only regardless — acceptEdits is only granted in builds.
    tctx.session_cwd = None
    if not allow_writes and bool(cget(cfg, "runtime.claude_session_reuse", True)):
        tctx.session_cwd = os.path.join(app_dir, ".agent_cwd")
    # Trim the planning-transcript payload for build turns (see build_context).
    tctx.prior_disc_cap = int(cget(
        cfg, "runtime.build_max_prior_discussion_chars", 30000)) if allow_writes else None
    if allow_writes:
        os.makedirs(cfg["_build_dir"], exist_ok=True)
        if is_verify_repair:
            emit("VERIFY/REPAIR phase: agents may make bounded fixes in %s"
                 % cfg["_build_dir"])
        else:
            emit("BUILD phase: agents may write files in %s" % cfg["_build_dir"])

    # Audit read channel. When a phase reads the target, inject the read-only digest
    # into every turn; and (only if runtime.audit_live_read_cwd) additionally point
    # codex/claude's cwd at the target read-only. Set-or-CLEAR on every phase so an
    # audit read channel can never leak into a later phase or another app.
    reads = bool(phasedef.get("reads_target", False)) if hasattr(phasedef, "get") else False
    _portfolio = cfg.get("_target_paths") or []
    if reads and len(_portfolio) > 0:
        # library_mining: a combined digest of the whole portfolio.
        tctx.target_digest = tctx.target_digest or build_portfolio_digest(_portfolio)
        tctx.read_dir = None
        emit("PORTFOLIO phase '%s': %d repos, %d-char digest."
             % (key, len(_portfolio), len(cfg["_target_digest"])))
    elif reads and cfg.get("_target_path"):
        tctx.target_digest = tctx.target_digest or build_target_digest(cfg["_target_path"])
        live = (not allow_writes
                and bool(cget(cfg, "runtime.audit_live_read_cwd", False)))
        tctx.read_dir = cfg["_target_path"] if live else None
        emit("AUDIT phase '%s': read-only target %s (%d-char digest%s)."
             % (key, cfg["_target_path"], len(cfg["_target_digest"]),
                "; live cwd" if live else ""))
    else:
        tctx.target_digest = ""
        tctx.read_dir = None

    phase_dir = os.path.join(app_dir, folder)
    os.makedirs(phase_dir, exist_ok=True)
    md_path = os.path.join(phase_dir, fname)

    unit = "iteration" if is_build else "round"
    round_desc = "unlimited %ss until consensus" % unit if unlimited_rounds \
        else "up to %d %s(s)" % (max_rounds, unit)
    emit("=== Phase '%s' for %s — %s; agents=%s; coordinator=%s ==="
         % (key, app, round_desc, ",".join(active), coord))
    live_log(app_dir, key, "orchestrator", "phase_started",
             "phase '%s' started — %s, coordinator %s"
             % (key, round_desc, coord))
    evlib.emit_event(app_dir, "phase_started", project=app, phase=key,
                     agents=",".join(active), coordinator=coord,
                     rounds=(0 if unlimited_rounds else max_rounds))

    # Round-level crash resume: scoped to the SEQUENTIAL round loop (plain
    # discussion/verify-repair phases, and a "build" phase with code changes
    # disabled) — NOT the parallel build loop (is_build and allow_writes),
    # whose iterations already land in a git-versioned app_build repo (see
    # ensure_build_repo) with its own crash-recovery path via that history,
    # and whose concurrent per-lane worker/worktree state makes a safe
    # mid-iteration resume a materially harder problem than resuming a
    # strictly-sequential round.
    #
    # The pipeline is strictly sequential (see the phase while-loop in run():
    # a phase is only entered after every earlier one is in completed_phases,
    # and process_phase is never called concurrently for two phases), so a
    # single state["current_phase"] + state["current_round"] pair unambiguously
    # describes at most one in-flight phase — no per-phase-keyed dict needed.
    #
    # "Resuming" requires ALL of: this phase is not in completed_phases
    # (guaranteed by the caller — it never calls process_phase for a completed
    # phase), the loaded state says THIS phase was the one in flight
    # (current_phase == key: set at the start of an earlier, crashed attempt
    # at this exact phase and never cleared because that attempt didn't
    # finish — a genuinely fresh phase would see the PREVIOUS phase's key
    # here instead), and current_round > 0 (at least one round was entered).
    # The actual resume point is then derived from the .md file itself, not
    # from current_round directly — see _resume_round_state's docstring for
    # why the counter alone is not trustworthy.
    #
    # V3 board 1.1: conversational phases branch BEFORE the resume/truncate
    # block below — _resume_round_state's coordinator heuristic would return
    # resume_round=1 for a coordinator-less transcript, making the else-branch
    # write_md() TRUNCATE the entire conversation on every re-entry. The
    # conversational runner owns its own append-only resume.
    if bool(phasedef.get("conversational", False) if hasattr(phasedef, "get") else False):
        return _run_conversational_phase(
            cfg, app, app_dir, phasedef, original_prompt, prior_outputs,
            state, md_path, personas, active)
    resume_round, resuming = 1, False
    if not (is_build and allow_writes) and state.get("current_phase") == key \
            and int(state.get("current_round") or 0) > 0:
        try:
            with open(md_path, encoding="utf-8") as fh:
                _existing = fh.read()
        except OSError:
            _existing = ""
        if _existing.strip():
            resume_round, _kept, _header_end = _resume_round_state(_existing)
            resuming = resume_round > 1
    if resuming:
        # Preserve the on-disk transcript (write_md would TRUNCATE it) except
        # for a trailing incomplete round, which is dropped and redone.
        write_md(md_path, _kept)
        msglib.reconcile_messages(app_dir, key, keep_below_round=resume_round)
        transcript = _kept[_header_end:]
        emit("Phase '%s': resuming a crashed run at round %d (%d completed "
             "round(s), %d char(s) of transcript recovered)."
             % (key, resume_round, resume_round - 1, len(transcript)))
    else:
        write_md(md_path, phase_header(app, phasedef, original_prompt))
        msglib.reconcile_messages(app_dir, key, keep_below_round=1,
                                  drop_post_round=True)
        transcript = ""
    extra = phase_extra(cfg, key)

    state["current_phase"] = key
    save_state(app_dir, state)

    consensus = False
    final_output = ""
    quality_repair_limit = max(0, int(cget(cfg, "runtime.phase_quality_repair_rounds", 1) or 0))
    independent_first = _independent_first_enabled(cfg, is_build or is_verify_repair)
    if independent_first:
        emit("Phase '%s': independent first round enabled." % key)
    if _phase_quality_gate_enabled(cfg, is_build or is_verify_repair):
        emit("Phase '%s': quality gate enabled (%d repair round%s allowed)."
             % (key, quality_repair_limit, "" if quality_repair_limit == 1 else "s"))

    # An enabled build phase fans the agents out to build IN PARALLEL (each owns a
    # lane) with an integrator turn between iterations. Every other phase — and a
    # build phase with code changes off — stays a sequential, turn-by-turn chat.
    if is_build and allow_writes:
        consensus, final_output, transcript = _run_parallel_build(
            cfg, app, app_dir, phasedef, original_prompt, prior_outputs,
            state, md_path, max_rounds, transcript, extra, personas=personas)
    rounds_iter = [] if (is_build and allow_writes) else (
        itertools.count(resume_round) if unlimited_rounds
        else range(resume_round, max_rounds + 1))
    step_in_marker = os.path.join(app_dir, ".step_in")
    consensus, final_output, transcript, last_substantive, any_agent_output = \
        _run_debate_rounds(cfg, app, app_dir, phasedef, original_prompt,
                           prior_outputs, state,
                           md_path=md_path, transcript=transcript, extra=extra,
                           personas=personas, active=active, coord=coord,
                           rounds_iter=rounds_iter, resuming=resuming, unit=unit,
                           is_build=is_build, is_verify_repair=is_verify_repair,
                           unlimited_rounds=unlimited_rounds, max_rounds=max_rounds,
                           independent_first=independent_first,
                           quality_repair_limit=quality_repair_limit,
                           step_in_marker=step_in_marker,
                           consensus=consensus, final_output=final_output)

    # A discussion phase that produced nothing at all means every enabled CLI is
    # unavailable — surface that clearly rather than writing an empty decision.
    if rounds_iter and not is_build and not any_agent_output:
        raise AgentError("No enabled agent could produce output in phase '%s' — are "
                         "the agent CLIs installed and logged in? See logs/." % key)

    # V3 board 1.7: the phase is completing — a still-armed step-in can no
    # longer be honored HERE, and a stale marker would pause the NEXT phase's
    # first round against an empty inbox. Delete it and say so honestly; the
    # user's message (if any) stays in the inbox and the next phase's
    # round-open drain folds it in (§5.5/§6.2 — never silently lost). Crash
    # and error exits deliberately KEEP the marker: resume re-enters the
    # round barrier and honors the join there.
    if not is_build and os.path.exists(step_in_marker):
        try:
            os.remove(step_in_marker)
        except OSError:
            pass
        _pending = _read_nonblank(os.path.join(app_dir, "human_inbox.txt"))
        evlib.emit_event(app_dir, "step_in_missed", project=app, phase=key,
                         detail=("phase ended before the join; message preserved "
                                 "in inbox") if _pending else "phase ended before the join")
        emit("Phase '%s' ended before your step-in was honored — %s" %
             (key, "your message stays queued for whatever runs next."
              if _pending else "nothing was lost (the inbox was empty)."))

    vote = {}
    available_active = [a for a in active if _agent_available(a, cfg)]
    if not consensus and not unlimited_rounds and not is_build and len(available_active) >= 2:
        final_output, transcript, vote = _run_forced_vote(
            cfg, app, app_dir, phasedef, original_prompt, prior_outputs, state,
            md_path=md_path, transcript=transcript, unit=unit, coord=coord,
            available_active=available_active,
            last_substantive=last_substantive, final_output=final_output)
    elif not consensus and is_build:
        # Build ran out of iterations; keep the coordinator's last recap as the
        # result (final_output already holds it) and close the phase out.
        if not final_output:
            final_output = transcript[-2000:]
        consensus = True

    # One working model is enough for a phase to conclude: if agents produced
    # output but no coordinator or tally ever answered, adopt the final
    # discussion state as the working decision (clearly labeled, never
    # fabricated) instead of failing a phase that had a live participant.
    if not final_output and any_agent_output:
        emit("Phase '%s': no coordinator was reachable — adopting the final "
             "discussion state as the working decision." % key)
        final_output = ("**No coordinator was reachable — the final discussion "
                        "state below stands as this phase's working decision.**\n\n"
                        + transcript[-4000:])

    for _hook in _PHASE_CLOSE_HOOKS:
        transcript, final_output = _hook(
            cfg, app, app_dir, phasedef, state,
            key=key, md_path=md_path, transcript=transcript,
            final_output=final_output, coord=coord, active=active,
            is_build=is_build, is_verify_repair=is_verify_repair,
            allow_writes=allow_writes, _needs_vlabel=_needs_vlabel,
            consensus=consensus)

    # Final phase footer
    marker = "CONSENSUS: YES" if consensus else (
        "VOTE_DECISION: YES" if vote.get("decided") else "VOTE_DECISION: NO")
    append_md(md_path, "\n## Coordinator Decision\n\nSee the coordinator's message above.\n")
    append_md(md_path, "\n## Final Output\n\n%s\n" % final_output.strip())
    append_md(md_path, "\n---\n\n%s\n" % marker)

    # Update state
    state["consensus_status"][key] = bool(consensus)
    if vote:
        state["vote_results"][key] = vote
        # UNRESOLVED phase state: a forced vote that never actually decided
        # (no quorum of parseable ballots, no agent could tally) is exactly
        # as unresolved as a failing quality gate or a broken contract — a
        # phase closing on it must not read as a clean, ordinary close.
        if not vote.get("decided"):
            state.setdefault("phase_resolutions", {})[key] = "vote_undecided"
            mistklib.append_mistake(app_dir, {
                "app": app, "workflow": cfg.get("_workflow_name"), "phase": key,
                "cls": "vote_undecided",
                "summary": "forced vote closed phase '%s' without deciding" % key})
    state["phase_outputs"][key] = final_output.strip()
    if key not in state["completed_phases"]:
        state["completed_phases"].append(key)
    state["current_round"] = 0
    state["next_agent"] = None
    save_state(app_dir, state)
    tctx.end_phase()
    live_log(app_dir, key, "orchestrator", "phase_completed",
             "phase '%s' complete (%s)" % (key, marker))
    evlib.emit_event(app_dir, "phase_completed", project=app, phase=key,
                     detail=marker)
    emit("Phase '%s' complete for %s (%s)." % (key, app, marker))
    return final_output.strip()


def _prompt_contract_repair(kind, errors, instruction):
    """Ask an agent to re-emit a corrected machine-contract block after the
    first attempt came back malformed/incomplete. Quotes the parser's exact
    errors so the fix is targeted, not a guess, and repeats the schema
    reminder since the agent won't otherwise re-read its earlier instructions."""
    err_list = "\n".join("- %s" % e for e in errors[:15])
    return (
        "Your last ```%s-json``` block had %d problem(s) the parser rejected:\n%s\n\n"
        "Re-emit a SINGLE corrected ```%s-json``` fenced block that fixes every "
        "one of these — the whole block, not a diff or a partial patch.\n"
        % (kind, len(errors), err_list, kind)
        + instruction
    )


def _prompt_requirements_coverage_repair(gaps, instruction):
    """Ask an agent to extend the tasks-json backlog so every CORE requirement
    is covered by at least one task's requirement_ids. A coverage gap isn't a
    parse error — the block is well-formed, it just doesn't fully cover the
    product's core promises yet."""
    gap_list = "\n".join("- %s: %s" % (rid, txt) for rid, txt in gaps[:15])
    return (
        "These CORE requirements have no task covering them yet (a task "
        "covers a requirement by listing its id in requirement_ids):\n"
        + gap_list + "\n\n"
        "Re-emit a SINGLE corrected ```tasks-json``` fenced block — the "
        "whole backlog, not a diff — that adds or updates tasks (via "
        "requirement_ids) so every one of these is covered by at least one "
        "task.\n"
        + instruction
    )


def _uncovered_core_requirements(tasks, requirements):
    """Core requirement ids not referenced by any task's requirement_ids.
    Returns [(id, text), ...] in requirements order. Purely mechanical: the
    agents are expected to tie every task to the requirement(s) it fulfills,
    so an untagged task simply doesn't count toward coverage — no LLM judgment
    call about whether a task "really" satisfies a requirement."""
    covered = set()
    for t in tasks:
        for rid in t.get("requirement_ids") or []:
            covered.add(str(rid))
    return [(str(r["id"]), r.get("text", "")) for r in requirements
            if r.get("core", True) and str(r.get("id")) not in covered]


def _repair_contract(cfg, app, app_dir, key, coord, active, md_path, transcript,
                     kind, prompt):
    """One bounded repair turn for a machine-contract problem (a malformed
    block or a requirements-coverage gap): ask the best-available coordinator
    candidate to respond per `prompt`, append the exchange to the transcript.
    Never raises — an agent that can't run this turn just leaves things as
    they were (resp=None signals that to the caller, which stops retrying
    rather than looping on a dead agent)."""
    for cand in _coordinator_candidates(cfg, active, preferred=coord):
        try:
            resp = call_agent(cfg, app, key, "contract-repair", cand, prompt)
        except AgentError as exc:
            emit("%s could not repair the %s-json contract: %s" % (DISPLAY[cand], kind, exc))
            continue
        block = "**%s — %s-json repair**\n\n%s\n" % (DISPLAY[cand], kind, resp)
        append_md(md_path, "\n" + block)
        msglib.append_message(app_dir, key, "contract", cand, md_path,
                              token="final.%s" % kind)
        transcript += "\n" + block
        return transcript, resp
    return transcript, None


def _record_phase_contracts(cfg, app, app_dir, key, transcript, final_output,
                            coord=None, active=None, md_path=None,
                            record_decisions=False):
    """Post-phase contract recording: tasks.json / interfaces.json (§19/§20)
    and, when the phase requested it, the decisions-json log (decisions.json).

    Bounded, non-fatal by design: parse/cycle errors are persisted in the
    contract file, WARNed prominently, and ledgered (contract_error) — never a
    hard block, because a wrongly-strict gate here would brick runs."""
    # Phase-transition summarization (unconditional — every completed phase
    # needs an entry in phase_summaries.json, not just decision-bearing ones,
    # since build_context's hybrid policy replaces ANY older phase's raw
    # transcript with this once it ages out of the recency window). Parsed
    # from the same wrap-up turn's phase-summary-json block when the phase
    # requested it via _phase_contract(). Phases whose coordinator turn never
    # runs this contract (the parallel-build integrator prompt doesn't request
    # it — see _phase_contract's docstring) or that dropped the block under
    # format drift get a synthesized fallback from final_output instead of a
    # missing entry — correctness (never silently lose a phase's substance)
    # beats a perfectly-worded summary.
    blob_all = transcript + "\n" + (final_output or "")
    summary, serrs = parse_phase_summary_blocks(blob_all)
    for e in serrs:
        emit("PHASE_SUMMARY: ERROR %s" % e)
    if summary:
        summary = dict(summary)
        summary["phase"] = key   # never trust the agent's self-reported key
        summary.setdefault("key_decisions", [])
        summary.setdefault("open_risks", [])
    else:
        summary = {
            "phase": key,
            "one_paragraph_summary": (final_output or "").strip()[:800],
            "key_decisions": [],
            "open_risks": [],
        }
    merge_phase_summary(app_dir, key, summary)
    if record_decisions:
        blob = transcript + "\n" + (final_output or "")
        decisions, derrs = parse_decision_blocks(blob)
        for e in derrs:
            emit("DECISIONS: ERROR %s" % e)
        if decisions:
            merged = merge_decisions(app_dir, decisions)
            emit("DECISIONS: %d new/updated decision(s) -> decisions.json "
                 "(%d total, %d error(s))."
                 % (len(decisions), len(merged), len(derrs)))
            live_log(app_dir, key, "orchestrator", "decisions_recorded",
                     "%d decision(s) merged into decisions.json; %d error(s)"
                     % (len(decisions), len(derrs)))
    contract_repair_limit = int(cget(cfg, "runtime.contract_repair_limit", 2) or 0)
    can_repair = bool(coord and active and md_path is not None)
    unresolved = None   # NEXT_MILESTONES "UNRESOLVED phase state": the caller
    # records this on state.phase_resolutions[key] so a phase that closed on
    # an unrepaired contract problem isn't indistinguishable from a clean one.
    if key == "task_assignments":
        blob = transcript + "\n" + (final_output or "")
        tasks, terrs = parse_tasks_blocks(blob)
        for c in find_task_cycles(tasks):
            terrs.append("dependency cycle: %s" % c)
        for e in terrs:
            emit("TASKS: ERROR %s" % e)
        attempts = 0
        while terrs and can_repair and attempts < contract_repair_limit:
            attempts += 1
            emit("CONTRACT: tasks.json has %d error(s) — repair attempt %d/%d."
                 % (len(terrs), attempts, contract_repair_limit))
            prompt = _prompt_contract_repair("tasks", terrs, _contract_snippet("tasks"))
            transcript, resp = _repair_contract(cfg, app, app_dir, key, coord, active,
                                                md_path, transcript, "tasks", prompt)
            if resp is None:
                emit("CONTRACT: no agent could attempt the tasks.json repair.")
                break
            # Parse ONLY this repair turn's response, not the whole accumulated
            # transcript: the repair prompt asks for the complete corrected
            # block (not a diff), so the response is self-contained, and
            # re-scanning the full blob would keep re-finding the ORIGINAL
            # bad block alongside the fix — errors accumulate per block seen,
            # they don't get superseded just because a later block is clean.
            tasks, terrs = parse_tasks_blocks(resp)
            for c in find_task_cycles(tasks):
                terrs.append("dependency cycle: %s" % c)
        if terrs:
            emit("WARN CONTRACT: %d error(s) in tasks.json (malformed blocks / "
                 "unknown lanes / dependency cycles) after %d repair attempt(s) — "
                 "the build proceeds, but review tasks.json 'errors' and the "
                 "mistakes ledger." % (len(terrs), attempts))
            mistklib.append_mistake(app_dir, {
                "app": app, "workflow": cfg.get("_workflow_name"), "phase": key,
                "cls": "contract_error",
                "summary": "%d tasks.json contract error(s) (%d repair attempt(s))"
                          % (len(terrs), attempts),
                "detail": {"errors": terrs[:10]}})
            unresolved = "contract_error"
        # Requirements-coverage check (NEXT_MILESTONES #2): every CORE
        # requirement from requirements.json (app_features) needs ≥1 task
        # naming it in requirement_ids. Checked mechanically, and skipped if
        # the backlog itself is still malformed — no point covering a parse
        # that's about to be discarded.
        if not terrs:
            requirements = load_requirements(app_dir)
            gaps = _uncovered_core_requirements(tasks, requirements)
            cov_attempts = 0
            while gaps and can_repair and cov_attempts < contract_repair_limit:
                cov_attempts += 1
                emit("CONTRACT: %d core requirement(s) uncovered by any task — "
                     "coverage repair attempt %d/%d: %s"
                     % (len(gaps), cov_attempts, contract_repair_limit,
                        ", ".join(g[0] for g in gaps)))
                prompt = _prompt_requirements_coverage_repair(gaps, _contract_snippet("tasks"))
                transcript, resp = _repair_contract(cfg, app, app_dir, key, coord, active,
                                                    md_path, transcript, "tasks", prompt)
                if resp is None:
                    emit("CONTRACT: no agent could attempt the requirements-coverage repair.")
                    break
                new_tasks, new_terrs = parse_tasks_blocks(resp)
                for c in find_task_cycles(new_tasks):
                    new_terrs.append("dependency cycle: %s" % c)
                if new_terrs:
                    # A broken re-emission must not silently replace a working
                    # backlog — surface it like any other malformed contract.
                    emit("CONTRACT: coverage repair produced %d malformed block "
                         "error(s) — keeping the prior backlog." % len(new_terrs))
                    break
                tasks = new_tasks
                gaps = _uncovered_core_requirements(tasks, requirements)
            if gaps:
                emit("WARN CONTRACT: %d core requirement(s) still uncovered after "
                     "%d coverage repair attempt(s): %s"
                     % (len(gaps), cov_attempts, ", ".join(g[0] for g in gaps)))
                mistklib.append_mistake(app_dir, {
                    "app": app, "workflow": cfg.get("_workflow_name"), "phase": key,
                    "cls": "requirements_coverage_gap",
                    "summary": "%d core requirement(s) uncovered by any task"
                              % len(gaps),
                    "detail": {"uncovered": [{"id": rid, "text": txt}
                                             for rid, txt in gaps]}})
                unresolved = "requirements_coverage_gap"
        persist_tasks(app_dir, tasks, terrs)
        emit("TASKS: %d task(s) -> tasks.json (%d error(s))." % (len(tasks), len(terrs)))
        live_log(app_dir, key, "orchestrator", "tasks_recorded",
                 "%d task(s) persisted to tasks.json; %d error(s)"
                 % (len(tasks), len(terrs)))
    if key == "tech_specs":
        blob = transcript + "\n" + (final_output or "")
        ifaces, ierrs = parse_interface_blocks(blob)
        for e in ierrs:
            emit("INTERFACES: ERROR %s" % e)
        attempts = 0
        while ierrs and can_repair and attempts < contract_repair_limit:
            attempts += 1
            emit("CONTRACT: interfaces.json has %d error(s) — repair attempt %d/%d."
                 % (len(ierrs), attempts, contract_repair_limit))
            prompt = _prompt_contract_repair("interfaces", ierrs, _contract_snippet("interfaces"))
            transcript, resp = _repair_contract(cfg, app, app_dir, key, coord, active,
                                                md_path, transcript, "interfaces", prompt)
            if resp is None:
                emit("CONTRACT: no agent could attempt the interfaces.json repair.")
                break
            # See the matching comment in the tasks-json loop above: parse only
            # this attempt's response, not the ever-growing transcript.
            ifaces, ierrs = parse_interface_blocks(resp)
        if ierrs:
            emit("WARN CONTRACT: %d error(s) in interfaces.json after %d repair "
                 "attempt(s) — the build proceeds, but review interfaces.json "
                 "'errors' and the mistakes ledger." % (len(ierrs), attempts))
            mistklib.append_mistake(app_dir, {
                "app": app, "workflow": cfg.get("_workflow_name"), "phase": key,
                "cls": "contract_error",
                "summary": "%d interfaces.json contract error(s) (%d repair attempt(s))"
                          % (len(ierrs), attempts),
                "detail": {"errors": ierrs[:10]}})
            unresolved = "contract_error"
        persist_interfaces(app_dir, ifaces, ierrs)
        emit("INTERFACES: %d interface(s) -> interfaces.json (%d error(s))."
             % (len(ifaces), len(ierrs)))
        live_log(app_dir, key, "orchestrator", "interfaces_recorded",
                 "%d interface(s) persisted to interfaces.json; %d error(s)"
                 % (len(ifaces), len(ierrs)))
    return transcript, unresolved


# ---------------------------------------------------------------------------
# App processing
# ---------------------------------------------------------------------------
def _should_pause_after(cfg, phasedef):
    """V2 §3.1: decide if the run pauses for approval after this phase. Manual
    mode pauses after every phase; Semi-Autonomous only after checkpoint phases;
    Fully Autonomous (default / anything else) never pauses."""
    # V3 board 1.1: a conversational phase ends on an explicit human command —
    # that IS the human decision. A checkpoint pause here would double-ack the
    # same file name (approvals/<key>.ok) the end command just consumed,
    # forcing the user to "approve" a conversation they explicitly ended.
    if bool(phasedef.get("conversational", False) if hasattr(phasedef, "get") else False):
        return False
    autonomy = cfg.get("_autonomy") or "fully_autonomous"
    if autonomy == "manual":
        return True
    if autonomy == "semi_autonomous":
        return bool(phasedef.get("checkpoint", False)) if hasattr(phasedef, "get") else False
    return False


def _approval_timeout(cfg):
    """How long a checkpoint waits for a human decision before proceeding. In
    parallel-project mode this bounds how long a blocked thread is held, so it's
    configurable (runtime.approval_timeout_seconds); default 2h."""
    try:
        return int(cget(cfg, "runtime.approval_timeout_seconds", 7200) or 7200)
    except (TypeError, ValueError):
        return 7200


def _read_nonblank(path):
    """One content read of a watched inbox file: True when it holds a
    non-blank message (same strip() test as drain_human_inbox). Split out so
    tests can count how often the wait loop actually reads — the stat gate in
    _await_inbox must make idle ticks read-free."""
    try:
        with open(path, encoding="utf-8") as fh:
            return bool(fh.read().strip())
    except OSError:
        return False


def _await_step_in(cfg, app_dir, timeout=20.0, poll=0.25):
    """V3 board 1.7: the tiny wait between a step-in marker landing and the
    message it announces (the GUI writes the marker FIRST, so this covers a
    one-file-write race — seconds, never the 2h approval timeout).

    Deliberately NOT _await_inbox: that helper consumes approvals/<key>.ok,
    which in a debate phase is the CHECKPOINT-APPROVAL decision file — eating
    it here would swallow a real approval click racing a phase end. It also
    must not set state['awaiting_human'] (a debate is not a chat; the GUI's
    chat machinery keys on that marker). Watches only the inbox, stat-gated,
    with the standard shutdown/deadline escapes."""
    inbox = os.path.join(app_dir, "human_inbox.txt")
    deadline = time.time() + timeout
    _last_stat = ()
    while time.time() < deadline:
        if _SHUTDOWN.is_set():
            return "shutdown"
        if cfg.get("_phase_deadline") and time.time() >= cfg["_phase_deadline"]:
            return "deadline"
        try:
            _s = os.stat(inbox)
            _st = (_s.st_mtime_ns, _s.st_size)
        except OSError:
            _st = None
        if _st is not None and _st != _last_stat:
            _last_stat = _st
            if _read_nonblank(inbox):
                return "message"
        time.sleep(poll)
    return "timeout"


def _await_inbox(cfg, app, app_dir, phase_key, state, timeout=7200, poll=0.25):
    """Block a conversational phase until the human acts (V3 board 1.1).

    Wake sources, checked in precedence order every poll tick:

      approvals/<phase_key>.ok    -> ("end", None)      explicit end command
      human_inbox.txt (non-blank) -> ("message", None)  next human message

    plus ("shutdown", None) on _SHUTDOWN, ("deadline", None) once the sprint
    phase deadline passes, and ("timeout", None) at the idle timeout.

    Deliberately UNLIKE _await_approval: the .ok file is NEVER cleared at wait
    entry. Agent rounds take minutes; an end command written while they were
    still talking is a real command, not a stale decision — clearing it here
    would silently discard it and hold the chat open until idle timeout.
    (Stale-.ok cleanup happens exactly once, at fresh conversational phase
    start.) The inbox check mirrors drain_human_inbox's strip() test so a
    whitespace-only write can't wake a round that would then drain nothing.

    Records state['awaiting_human'] so the GUI can surface the waiting state;
    cleared on every exit path (crash-abandoned markers are cleared by the
    run re-entry and --resume stale-state paths).
    """
    appr_dir = os.path.join(app_dir, "approvals")
    os.makedirs(appr_dir, exist_ok=True)
    end_file = os.path.join(appr_dir, "%s.ok" % phase_key)
    retry_file = os.path.join(appr_dir, "%s.retry" % phase_key)
    inbox = os.path.join(app_dir, "human_inbox.txt")
    state["awaiting_human"] = phase_key
    save_state(app_dir, state)
    evlib.emit_event(app_dir, "awaiting_human", project=app, phase=phase_key)
    emit("Conversation '%s': waiting for your next message (end the chat with "
         "approvals/%s.ok)." % (phase_key, phase_key))
    deadline = time.time() + timeout
    # V3 board 1.2: ~250ms tick doing only cheap checks — 1 exists + 1 stat
    # per tick; the inbox CONTENT is read only when its (st_mtime_ns, st_size)
    # tuple changes (mtime_ns not seconds: survives coarse-mtime filesystems
    # and same-second rewrites). () is the never-matches sentinel so a file
    # that already exists at wait entry is read on the first tick.
    _last_stat = ()
    try:
        while True:
            if _SHUTDOWN.is_set():
                return "shutdown", None
            if cfg.get("_phase_deadline") and time.time() >= cfg["_phase_deadline"]:
                return "deadline", None
            if os.path.exists(end_file):
                try:
                    os.remove(end_file)
                except OSError:
                    pass
                return "end", None
            if os.path.exists(retry_file):
                # V3 board 1.11 "retry with…": rename-then-run — a crash
                # after the rename loses one retry rather than double-running
                # it (the deliberate side of the trade).
                consumed = retry_file + ".consumed"
                try:
                    os.replace(retry_file, consumed)
                    with open(consumed, encoding="utf-8") as fh:
                        req = json.load(fh)
                except (OSError, ValueError) as exc:
                    emit("Retry request unreadable (%s) — ignored." % exc)
                    req = None
                if isinstance(req, dict) and req.get("agent") and req.get("model"):
                    return "retry", req
                if req is not None:
                    emit("Retry request missing agent/model — ignored.")
            try:
                _s = os.stat(inbox)
                _st = (_s.st_mtime_ns, _s.st_size)
            except OSError:
                _st = None   # missing file == no message; never crashes the wait
            if _st is not None and _st != _last_stat:
                _last_stat = _st
                if _read_nonblank(inbox):
                    return "message", None
            if time.time() >= deadline:
                return "timeout", None
            time.sleep(poll)
    finally:
        state["awaiting_human"] = None
        save_state(app_dir, state)


def _await_approval(app_dir, phase_key, state, timeout=7200, poll=0.25):
    """Pause after a checkpoint phase until the GUI (or a human) drops one of
    three decision files under <app>/approvals/ (V2 §3.1 approval flow), or the
    timeout elapses (then proceed rather than hang forever):

      <phase>.ok       — Approve: continue as-is.
      <phase>.edit     — Edit & Approve: the file BODY replaces the phase's
                         final output, then the run continues.
      <phase>.changes  — Request Changes: the file body is human feedback; the
                         phase is re-run with the feedback in the conversation.

    Records state['awaiting_approval'] so the GUI can surface the buttons.
    Returns (decision, payload): ("approved"|"edited"|"changes_requested"|
    "timeout", file body or None)."""
    appr_dir = os.path.join(app_dir, "approvals")
    os.makedirs(appr_dir, exist_ok=True)
    decision_files = {
        "approved": os.path.join(appr_dir, "%s.ok" % phase_key),
        "edited": os.path.join(appr_dir, "%s.edit" % phase_key),
        "changes_requested": os.path.join(appr_dir, "%s.changes" % phase_key),
    }
    for p in decision_files.values():
        try:
            os.remove(p)   # clear any stale decision
        except OSError:
            pass
    state["awaiting_approval"] = phase_key
    save_state(app_dir, state)
    emit("Awaiting approval after phase '%s' (autonomy pause). Approve: %s | "
         "Edit & Approve: %s.edit | Request changes: %s.changes"
         % (phase_key, decision_files["approved"], phase_key, phase_key))
    deadline = time.time() + timeout
    while time.time() < deadline:
        for decision, path in decision_files.items():
            if not os.path.exists(path):
                continue
            payload = None
            if decision in ("edited", "changes_requested"):
                try:
                    with open(path, encoding="utf-8") as fh:
                        payload = fh.read()
                except OSError:
                    payload = ""
            try:
                os.remove(path)
            except OSError:
                pass
            state["awaiting_approval"] = None
            save_state(app_dir, state)
            emit("Approval decision for '%s': %s." % (phase_key, decision))
            return decision, payload
        time.sleep(poll)
    state["awaiting_approval"] = None
    save_state(app_dir, state)
    emit("Approval timed out for '%s' after %ds — continuing." % (phase_key, timeout))
    return "timeout", None


def find_apps(root):
    """Project discovery: flat direct children AND nested chat sessions.

    Flat (unchanged, forever):   <root>/<project>/initial_prompt/…
    Nested (V3 board 3.0):       <root>/<project>/<section>/<chat>/initial_prompt/…
    returned as the session id "project/section/chat" — the one string the
    engine, events, GUI and search all carry for a nested session.

    Recursion guards (0.2 back-compat rule): a dir with its OWN
    initial_prompt/ is a flat project, full stop; a dir with root-level
    agent_state.json is a legacy single-chat project and is NEVER recursed
    into; dot-prefixed and .orch_archived dirs are invisible at every level.
    """
    apps = []
    for name in sorted(os.listdir(root)):
        if name.startswith("."):
            continue
        if name in (".orchestrator", ".git"):
            continue
        full = os.path.join(root, name)
        if not os.path.isdir(full):
            continue
        if os.path.exists(os.path.join(full, ".orch_archived")):
            # Archived projects (GUI "Remove from list, keep folder") are
            # invisible to scan/watch/shepherd; deleting the marker restores.
            continue
        if os.path.exists(os.path.join(full, "initial_prompt", "initial_prompt.md")):
            apps.append(name)
            continue
        if os.path.exists(os.path.join(full, "agent_state.json")):
            continue   # legacy single-chat project — never recurse (0.2)
        if not valid_app_slug(name):
            continue   # unaddressable project segment — symmetric skip
        if not os.path.exists(os.path.join(full, SECTIONS_MARKER)):
            # A wrapper/batch folder a user happened to arrange two levels
            # deep must NOT suddenly become runnable (the flat contract
            # promised nested dirs are ignored). Only dirs the engine
            # itself marked as section containers are recursed —
            # create_session and the migration mint the marker.
            continue
        try:
            sections = sorted(os.listdir(full))
        except OSError:
            continue
        for section in sections:
            if section.startswith(".") or not valid_app_slug(section):
                continue
            sdir = os.path.join(full, section)
            if (not os.path.isdir(sdir)
                    or os.path.exists(os.path.join(sdir, ".orch_archived"))):
                continue
            try:
                chats = sorted(os.listdir(sdir))
            except OSError:
                continue
            for chat in chats:
                # Segment validation keeps discovery and addressing
                # symmetric: a dir the operator could never address
                # (e.g. "a..b") must not be discovered either.
                if chat.startswith(".") or not valid_app_slug(chat):
                    continue
                cdir = os.path.join(sdir, chat)
                if (not os.path.isdir(cdir)
                        or os.path.exists(os.path.join(cdir, ".orch_archived"))):
                    continue
                if os.path.exists(os.path.join(
                        cdir, "initial_prompt", "initial_prompt.md")):
                    apps.append("%s/%s/%s" % (name, section, chat))
    return apps


def read_initial_prompt(app_dir):
    p = os.path.join(app_dir, "initial_prompt", "initial_prompt.md")
    if not os.path.exists(p):
        return None
    with open(p, encoding="utf-8") as fh:
        return fh.read()


def valid_app_slug(name):
    """True when an operator-supplied app/project name is a plain folder name
    that stays INSIDE the workspace root when joined: no path separators, no
    '..' traversal, not hidden. (Discovered apps come from os.listdir and are
    safe by construction; this guards --app/--project/--resume input.)"""
    n = str(name or "")
    return bool(n) and ".." not in n and not n.startswith(".") \
        and "/" not in n and "\\" not in n and os.sep not in n


# Dirs the engine may recurse into for nested sessions carry this marker
# (minted by create_session / the migration) — see find_apps.
SECTIONS_MARKER = ".orch-sections"


def _nested_parent_conflict(root, project):
    """Why the <root>/<project> level cannot host nested sessions, or None.
    Nesting inside an existing FLAT project (own initial_prompt/) or a
    legacy project (root agent_state.json) would make the session
    permanently invisible to discovery — the never-recurse rules would
    hide it (verified: migration into a flat project erased the session
    from every discovery surface AND its search index). A plain file
    blocks makedirs outright."""
    pdir = os.path.join(root, project)
    if not os.path.exists(pdir):
        return None
    if not os.path.isdir(pdir):
        return "a file blocks the project dir"
    if os.path.exists(os.path.join(pdir, "initial_prompt",
                                   "initial_prompt.md")):
        return "existing flat project"
    if os.path.exists(os.path.join(pdir, "agent_state.json")):
        return "existing legacy project"
    if not os.path.exists(os.path.join(pdir, SECTIONS_MARKER)):
        try:
            occupied = any(not e.startswith(".") for e in os.listdir(pdir))
        except OSError:
            return "project dir unreadable"
        if occupied:
            # Minting the marker on a user's wrapper dir would convert
            # every depth-2 initial_prompt dir inside it into runnable
            # sessions (verified: an archived project became discoverable).
            return "existing unmarked directory"
    return None


def parse_session_id(value):
    """Validate operator-supplied --app/--project/--resume input for BOTH
    layouts (V3 board 3.0): a flat name (one segment) or a nested session
    id (exactly project/section/chat). Every segment must pass the
    UNCHANGED valid_app_slug rules, so traversal ('..'), absolute paths,
    separators inside a segment and hidden segments are all impossible.
    Returns the normalized id, or None when invalid."""
    s = str(value or "").strip()
    if s.startswith("/") or s.endswith("/"):
        return None
    parts = s.split("/")
    if len(parts) not in (1, 3):
        return None
    if not all(valid_app_slug(p) for p in parts):
        return None
    return "/".join(parts)


def create_session(root, session_id, prompt, workflow=None):
    """Mint a session dir (nested or flat) with the same per-dir file
    contract as flat apps: initial_prompt/initial_prompt.md (+ workflow.txt
    when given). Refuses an existing dir. Returns the app_dir path."""
    sid = parse_session_id(session_id)
    if sid is None:
        raise AppError("invalid session id %r" % session_id)
    app_dir = os.path.join(root, sid)
    if os.path.exists(app_dir):
        raise AppError("session %r already exists" % sid)
    if "/" in sid:
        conflict = _nested_parent_conflict(root, sid.split("/")[0])
        if conflict:
            raise AppError(
                "cannot nest %r under %r (%s) — the session would be "
                "invisible to discovery" % (sid, sid.split("/")[0], conflict))
    try:
        os.makedirs(os.path.join(app_dir, "initial_prompt"))
        if "/" in sid:
            open(os.path.join(root, sid.split("/")[0], SECTIONS_MARKER),
                 "w").close()
    except OSError as exc:
        raise AppError("cannot create session %r: %s" % (sid, exc))
    with open(os.path.join(app_dir, "initial_prompt", "initial_prompt.md"),
              "w", encoding="utf-8") as fh:
        fh.write(prompt if prompt.endswith("\n") else prompt + "\n")
    if workflow:
        with open(os.path.join(app_dir, "workflow.txt"), "w",
                  encoding="utf-8") as fh:
            fh.write(workflow + "\n")
    return app_dir


def _do_route_push(cfg, args):
    """CLI 'route' verb (V3 4.5): PUSH an admissible bus artifact into a
    target session's carryover context — minting the target session if it
    doesn't exist yet — and emit artifact_routed. This is the SAME
    artifacts.route_push the GUI drag (4.9) drives. Spawning the target is
    deliberately NOT done here: a plain route lands context for a chat the
    human runs when ready; the mint+push+SPAWN autonomous flow rides a real
    delegation request (4.6 composer / 7.2 Conductor)."""
    root = cfg["root"]
    if not (args.route_from and args.route_to):
        emit("--route-artifact needs both --route-from and --route-to")
        return 2
    source_project = parse_session_id(args.route_from).split("/")[0]
    source_project_dir = os.path.join(root, source_project)
    target_sid = parse_session_id(args.route_to)
    target_dir = os.path.join(root, target_sid)
    # Pre-check the artifact BEFORE minting a target: a refused route must
    # never leave an orphan (phantom) session dir on disk. route_push
    # re-validates under its lock (defense in depth), so this duplication
    # is only to gate the side-effectful mint.
    _meta = artifactslib.load_meta(source_project_dir, args.route_artifact,
                                   on_error=emit)
    if _meta is None:
        emit("--route: artifact %r not found under %s"
             % (args.route_artifact, source_project))
        return 2
    if not artifactslib.is_admissible(source_project_dir, _meta,
                                      on_error=emit):
        emit("--route refused: artifact not admissible")
        return 2
    if not os.path.isdir(target_dir):
        try:
            create_session(root, target_sid, "(routed artifacts)", None)
        except AppError as exc:
            emit("--route: cannot create target %r: %s" % (target_sid, exc))
            return 2
    elif _app_lock_has_live_owner(target_sid):
        # State surgery against a live engine would lose updates.
        emit("--route: target %r is running — stop it first" % target_sid)
        return 3
    budget = int(cget(cfg, "runtime.max_prior_output_chars", 4000))
    res = artifactslib.route_push(
        args.route_artifact, source_project_dir, target_dir,
        on_error=emit, budget=budget, default_state=load_state(target_dir))
    status = res.get("status")
    if status == "routed":
        evlib.emit_event(target_dir, "artifact_routed",
                         route_id=res["route_id"],
                         artifact_id=res["artifact_id"],
                         version=res["version"], target=res["target"],
                         target_session=target_sid,
                         route_kind="manual_push")
        print("ROUTED: %s v%s -> %s"
              % (res["artifact_id"], res["version"], target_sid))
        return 0
    if status == "noop":
        print("ALREADY-ROUTED: %s -> %s"
              % (res["artifact_id"], target_sid))
        return 0
    emit("--route refused: %s" % res.get("reason", "unknown"))
    return 2


def _do_finalize(cfg, args):
    """CLI 'finalize' verb (V3 4.8): flip a pending_review/draft artifact to
    'final', recording who did it in status_history. The thin pre-4.9 path
    for the requires_human gate (--by-human). Same artifacts.finalize the
    4.9 GUI Finalize button will drive."""
    root = cfg["root"]
    if not args.finalize_in:
        emit("--finalize-artifact needs --finalize-in <project|session id>")
        return 2
    project = parse_session_id(args.finalize_in).split("/")[0]
    project_dir = os.path.join(root, project)
    registry = artifactslib.load_registry(HERE, on_error=emit)
    meta = artifactslib.finalize(
        project_dir, args.finalize_artifact, args.finalize_by, registry,
        human=bool(args.by_human), on_error=emit)
    if meta is None:
        emit("--finalize refused")
        return 2
    print("FINALIZED: %s -> final (by %s)"
          % (args.finalize_artifact, args.finalize_by))
    return 0


def migrate_layout(root, apply=False, out=print):
    """One-shot flat->nested chat migration (V3 board 3.0). DRY-RUN by
    default — the confirmation channel is the --apply flag, never stdin
    (this runs headless under the GUI). A flat dir migrates only when its
    name splits as <slug>--<slug>--<slug> exactly AND it has
    initial_prompt/; anything else is reported, never silently skipped.
    A live-locked session is refused (SKIP (running)); a stale lock file
    is renamed with its dir so resume offers keep working. Apply is one
    os.rename per dir — atomic on the same volume; a failed rename leaves
    the source untouched. Idempotent: a second run matches nothing."""
    counts = {"migrate": 0, "skip": 0}
    for name in sorted(os.listdir(root)):
        if name.startswith(".") or "--" not in name:
            continue
        full = os.path.join(root, name)
        if not os.path.isdir(full):
            continue
        parts = name.split("--")
        if len(parts) != 3 or not all(valid_app_slug(p) for p in parts):
            out("SKIP (ambiguous): %s" % name)
            counts["skip"] += 1
            continue
        if not os.path.exists(os.path.join(full, "initial_prompt",
                                           "initial_prompt.md")):
            out("SKIP (not a session): %s" % name)
            counts["skip"] += 1
            continue
        dest_id = "/".join(parts)
        dest = os.path.join(root, dest_id)
        conflict = _nested_parent_conflict(root, parts[0])
        if conflict:
            out("SKIP (project-level conflict — %s): %s" % (conflict, name))
            counts["skip"] += 1
            continue
        if os.path.exists(dest):
            out("SKIP (destination exists): %s -> %s" % (name, dest_id))
            counts["skip"] += 1
            continue
        old_lock = os.path.join(root, ".orch-locks", "%s.lock" % name)
        if os.path.exists(old_lock) and _app_lock_has_live_owner(name):
            out("SKIP (running): %s" % name)
            counts["skip"] += 1
            continue
        out("MIGRATE: %s -> %s" % (name, dest_id))
        if os.path.exists(old_lock):
            out("LOCK: %s.lock -> %s.lock" % (name, encode_lock_name(dest_id)))
        counts["migrate"] += 1
        if not apply:
            continue
        try:
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            open(os.path.join(root, parts[0], SECTIONS_MARKER), "w").close()
            os.rename(full, dest)
        except OSError as exc:
            out("WARN could not migrate %s (%s) — source left untouched"
                % (name, exc))
            counts["migrate"] -= 1
            counts["skip"] += 1
            continue
        if os.path.exists(old_lock):
            try:
                os.rename(old_lock,
                          os.path.join(root, ".orch-locks",
                                       "%s.lock" % encode_lock_name(dest_id)))
            except OSError as exc:
                out("WARN could not rename stale lock for %s: %s" % (name, exc))
    out("migration %s: %d to migrate, %d skipped"
        % ("applied" if apply else "dry-run", counts["migrate"], counts["skip"]))
    return counts


def process_app(cfg, root, app):
    app_dir = os.path.join(root, app)
    prompt = read_initial_prompt(app_dir)
    if prompt is None:
        return  # not an orchestrator app yet
    if portfoliolib.child_autorun_disabled(app_dir) and not cfg.get("_explicit_app"):
        emit("App '%s': spec-only portfolio child — skipping auto-run (run it explicitly to process)." % app)
        return
    # runtime.require_git_repo: preflight warning (not fatal). run.sh's
    # commit/push step silently no-ops when the root isn't a repo, so surface
    # that up front instead of letting the user discover it after a long run.
    if bool(cget(cfg, "runtime.require_git_repo", False)) \
            and not cfg.get("_warned_no_git_repo") \
            and not os.path.isdir(os.path.join(root, ".git")):
        tcxlib.TurnContext(cfg).warned_no_git_repo = True
        emit("WARN runtime.require_git_repo=true but %s is not a git repo — "
             "run.sh will skip its commit/push step (run `git init` there to fix)." % root)
    # No agent runnable at all (every cloud CLI disabled/missing AND no local
    # model pulled) is otherwise discovered deep in phase 1's call_agent —
    # every turn fails one at a time with no upfront diagnosis. Check once per
    # run (not per app) and give a single clear pointer to --doctor.
    if not cfg.get("_checked_any_agent_runnable"):
        tcxlib.TurnContext(cfg).checked_any_agent_runnable = True
        if not any(_agent_available(a, cfg) for a in enabled_agents(cfg)):
            emit("WARN no agent is runnable: every enabled agent's CLI is "
                 "missing/logged-out, and no local Ollama model is enabled+pulled. "
                 "Every phase will fail immediately. Run `--doctor` to see what's "
                 "wired up, or enable/log in to at least one of codex/claude/"
                 "gemini, or pull+enable a local model.")
    # Per-app lock so different apps can run at the same time.
    stale = int(cget(cfg, "runtime.stale_lock_seconds", 5400))
    if not acquire_app_lock(app, stale):
        emit("App '%s': already running (locked) — skipping." % app)
        return
    heartbeat_stop = _start_run_heartbeat(app, app_dir)
    try:
        _run_app_pipeline(cfg, app, app_dir, prompt)
    finally:
        heartbeat_stop.set()
        release_app_lock(app)


def _portfolio_manifest_blob(state, latest_output=""):
    parts = []
    for value in (state.get("phase_outputs") or {}).values():
        if value:
            parts.append(str(value))
    if latest_output:
        parts.append(str(latest_output))
    return "\n\n".join(parts)


def _maybe_materialize_portfolio_children(cfg, root, app, app_dir, prompt, state,
                                          latest_output=""):
    """Parse the portfolio manifest, if present, and create sibling projects."""
    # Materialized once already (recorded on state): the children exist and
    # materialize_children would just re-skip them. Skip the re-parse + fs walk
    # this call site does at the top of every phase iteration.
    if state.get("portfolio_manifest"):
        return None
    if not portfoliolib.is_portfolio_parent_prompt(prompt):
        return None
    manifest, errors = portfoliolib.parse_portfolio_manifest(
        _portfolio_manifest_blob(state, latest_output))
    for err in errors:
        emit("PORTFOLIO: %s" % err)
    if not manifest.get("apps"):
        return None
    force_build_all = portfoliolib.requires_built_children(prompt)
    requested_build_count = len(manifest.get("apps") or []) if force_build_all else \
        sum(1 for child in manifest.get("apps") or [] if child.get("build"))
    build_limit = cget(cfg, "runtime.portfolio_build_limit", None)
    manifest = portfoliolib.apply_build_plan(
        manifest, build_limit=build_limit, force_build_all=force_build_all)
    actual_build_count = sum(1 for child in manifest.get("apps") or [] if child.get("build"))
    result = portfoliolib.materialize_children(
        root, app, app_dir, prompt, manifest, force_build_all=False)
    state["portfolio_manifest"] = {
        "path": portfoliolib.manifest_path(app_dir),
        "app_count": len(manifest.get("apps") or []),
        "build_count": actual_build_count,
        "force_build_all": force_build_all,
        "build_limit": build_limit,
        "created": result.get("created", []),
        "updated": result.get("updated", []),
        "skipped": result.get("skipped", []),
    }
    save_state(app_dir, state)
    summary = ("%d app(s); created=%d updated=%d skipped=%d"
               % (len(manifest.get("apps") or []), len(result.get("created", [])),
                  len(result.get("updated", [])), len(result.get("skipped", []))))
    if build_limit is not None and actual_build_count < requested_build_count:
        summary += "; build cap applied (%d requested -> %d built)" % (
            requested_build_count, actual_build_count)
    elif force_build_all:
        summary += "; build=true forced for every selected app"
    emit("PORTFOLIO: materialized sibling projects — %s." % summary)
    live_log(app_dir, "portfolio", "orchestrator", "portfolio_materialized", summary)
    return result


def _project_parallel_workers(cfg, app_count):
    try:
        workers = int(cget(cfg, "runtime.project_parallel_workers", 1) or 1)
    except (TypeError, ValueError):
        workers = 1
    if workers <= 0:
        workers = max(1, os.cpu_count() or 1)
    return max(1, min(app_count, workers))


def _record_skipped_phase(app, app_dir, phasedef, prompt, state, reason):
    key, folder, fname, _purpose = phasedef
    phase_dir = os.path.join(app_dir, folder)
    os.makedirs(phase_dir, exist_ok=True)
    md_path = os.path.join(phase_dir, fname)
    write_md(md_path, phase_header(app, phasedef, prompt))
    msglib.reconcile_messages(app_dir, key, keep_below_round=1,
                              drop_post_round=True)
    append_md(md_path, "\n## Final Output\n\n%s\n" % reason)
    append_md(md_path, "\n---\n\nSKIPPED\n")
    state.setdefault("phase_outputs", {})[key] = reason
    state.setdefault("consensus_status", {})[key] = True
    if key not in state.get("completed_phases", []):
        state.setdefault("completed_phases", []).append(key)
    state["current_phase"] = key
    state["current_round"] = 0
    state["next_agent"] = None
    save_state(app_dir, state)
    live_log(app_dir, key, "orchestrator", "phase_skipped", reason)
    emit("Phase '%s' skipped for %s: %s" % (key, app, reason))
    return reason


_PORTFOLIO_DELEGATED_PHASES = {
    "initial_discussion", "per_app_product_brief", "next_steps_small",
    "detailed_discussion", "app_features", "design_discussion",
    "design_handoff", "ios_architecture_review", "tech_specs", "project_plan",
    "task_assignments", "implementation_readiness_gate", "build_coordination",
    "build_verification", "human_qa_checklist", "app_store_readiness",
    "final_review",
}


def _run_timed_gate(app_dir, gate, fn):
    """Run one release gate, recording its wall-clock as a
    release_gate_timing event — the ground truth for deciding whether gates
    are worth parallelizing. Never interferes with the gate's own result."""
    t0 = time.time()
    try:
        return fn()
    finally:
        try:
            evlib.emit_event(app_dir, "release_gate_timing", gate=gate,
                             seconds=round(time.time() - t0, 1))
        except Exception:  # noqa: BLE001 - telemetry must never fail a gate
            pass


def _release_gate_failure(app_dir, phases, state, prompt, cfg=None):
    """A build workflow may only be marked done when the thing it claims to
    have built exists and compiles (the nickel lesson: 10 failed verifies and
    a NO final_review still ended in 'Marked done'). Returns a human reason
    string when the gate fails, else None. Spec/research workflows (no
    verify-bearing phase) and portfolio parents whose build is delegated to
    children are exempt.

    Test failures are opt-in here: runtime.tests_gate_release (default False)
    — a test run failing does NOT block release unless an operator explicitly
    turns this on, since generated test suites are new/unproven and shouldn't
    be able to strand an otherwise-compiling build."""
    has_verify = any((p.get("verify") if hasattr(p, "get") else None)
                     for p in phases)
    if not has_verify:
        return None
    if portfoliolib.is_portfolio_parent_prompt(prompt) and \
            portfoliolib.load_manifest(app_dir):
        return None
    latest = verifylib.latest_verify_result(app_dir)
    # No record, or verification couldn't run (no toolchain): keep the engine's
    # best-effort stance — unverified is reported, never fatal. The gate only
    # blocks when a real verifier RAN and said no.
    if not latest or not latest.get("ran"):
        return None
    if not latest.get("ok"):
        return "last verification failed (%s)" % latest.get("summary", "")
    if bool(cget(cfg or {}, "runtime.tests_gate_release", False)) \
            and latest.get("tests_ran") and not latest.get("tests_ok"):
        return "last test run failed (%s)" % latest.get("summary", "")
    return None


ADHERENCE_JSON_RE = re.compile(r"```adherence-json\s*(\{.*?\})\s*```", re.DOTALL)


def _adherence_gate(cfg, app, app_dir, phases, state, prompt, workflow):
    """Prompt-adherence gate (runtime.adherence_gate, default true): a compiled
    app is necessary but not sufficient — this asks one strong agent to grade
    the BUILT app against the ORIGINAL PROMPT's requirements before the done
    flag. Returns a human reason string when core requirements are unmet (the
    caller routes it into the same bounded iterate-repair loop the release
    gate uses), else None. Best-effort: any error means None — the gate can
    tighten quality but never take down a run. Verdict + per-requirement
    grades are persisted to docs/adherence.json for the GUI."""
    if not bool(cget(cfg, "runtime.adherence_gate", True)):
        return None
    if workflow.target != "app":
        return None
    has_verify = any((p.get("verify") if hasattr(p, "get") else None)
                     for p in phases)
    if not has_verify:
        return None
    build_dir = os.path.join(app_dir, "app_build")
    if not os.path.isdir(build_dir):
        return None
    try:
        tree = []
        for dp, dns, fns in os.walk(build_dir):
            dns[:] = [d for d in dns if d not in (".git", ".build", "DerivedData")]
            for fn in fns:
                tree.append(os.path.relpath(os.path.join(dp, fn), build_dir))
            if len(tree) > 400:
                break
        spec_bits = []
        for k in ("app_features", "tech_specs", "design"):
            v = (state.get("phase_outputs") or {}).get(k)
            if v:
                spec_bits.append("--- %s ---\n%s" % (k, str(v)[:3000]))
        latest_v = verifylib.latest_verify_result(app_dir) or {}
        # Real runtime evidence (not just a file listing): the UI crawl
        # actually tapped every element and replayed the declared flows —
        # dead taps and failed flows are the strongest signal available that
        # a screen LOOKS done but isn't wired up.
        func_evidence = ""
        try:
            with open(os.path.join(app_dir, "docs", "ui_crawl.json"),
                      encoding="utf-8") as fh:
                crawl = json.load(fh)
            dead = crawl.get("dead_taps") or []
            flows = crawl.get("flows") or []
            failed_flows = [f for f in flows if not f.get("passed")]
            if dead or flows:
                bits = ["===== FUNCTIONAL EVIDENCE (from actually tapping the "
                       "running app — weigh this over the file tree) ====="]
                if dead:
                    bits.append("%d element(s) did NOTHING when tapped: %s"
                               % (len(dead), "; ".join(
                                   (d.get("tapped") or {}).get("label")
                                   or (d.get("tapped") or {}).get("id") or "?"
                                   for d in dead[:8])))
                if flows:
                    bits.append("%d/%d declared user flows actually work end "
                               "to end." % (len(flows) - len(failed_flows),
                                            len(flows)))
                if failed_flows:
                    bits.append("Failed flows: " + "; ".join(
                        "%s (%s)" % (f.get("name"), f.get("failure", "")[:80])
                        for f in failed_flows[:5]))
                func_evidence = "\n".join(bits) + "\n\n"
        except (OSError, ValueError):
            pass
        reqs = load_requirements(app_dir)
        req_block = ""
        if reqs:
            req_block = ("===== NUMBERED REQUIREMENTS (requirements.json — "
                         "grade EACH ONE) =====\n"
                         + "\n".join("%s%s: %s"
                                      % (r["id"],
                                         " [CORE]" if r.get("core") else "",
                                         r["text"]) for r in reqs[:60])
                         + "\n\n")
        dod_block = complib.render_dod(
            HERE, str(cfg.get("_completeness") or "v1")) + "\n\n"
        grade_instruction = (
            "Grade EACH numbered requirement above MET / PARTIAL / UNMET"
            if reqs else
            "Extract the concrete requirements from the ORIGINAL PROMPT (not "
            "the spec — the spec may have drifted) and grade each MET / "
            "PARTIAL / UNMET")
        grader_prompt = (
            "You are the PROMPT ADHERENCE GATE for the app '%s'. Grade the "
            "built app against what the user actually asked for.\n\n"
            "CRITICAL RULE: this is NOT a cosmetic review. A screen that "
            "LOOKS finished but whose controls don't actually work (taps "
            "that do nothing, hardcoded/fake data standing in for real "
            "input, calculations never performed, nothing persisted) grades "
            "UNMET, not MET, no matter how polished it looks. A file simply "
            "existing (e.g. a 'TipCalculator.swift') is NOT evidence its "
            "logic is real or wired to the UI — look for actual "
            "implementation, not just plausible file names.\n\n"
            "===== ORIGINAL PROMPT =====\n%s\n\n"
            "%s%s%s"
            "===== SPEC DECISIONS =====\n%s\n\n"
            "===== BUILT FILE TREE (app_build/, %d files) =====\n%s\n\n"
            "===== VERIFICATION =====\n%s\n\n"
            "%s, judging from the functional evidence above first, then the "
            "file tree and spec decisions. ALSO grade the Definition of Done "
            "items — an unmet DoD item counts like a PARTIAL requirement. Be "
            "strict about core functionality actually working, lenient about "
            "visual polish. Then output exactly one fenced block:\n"
            "```adherence-json\n"
            "{\"score\": 0-100, \"requirements\": [{\"text\": \"...\", "
            "\"grade\": \"MET|PARTIAL|UNMET\"}], \"verdict\": \"PASS|FAIL\", "
            "\"reason\": \"one line\"}\n"
            "```\n"
            "Verdict FAIL when a CORE requirement is UNMET, INCLUDING when a "
            "requirement is only cosmetically present without real "
            "functionality behind it."
            % (app, str(prompt)[:6000],
               req_block, dod_block, func_evidence,
               "\n".join(spec_bits) or "(none recorded)",
               len(tree), "\n".join(sorted(tree)[:400]),
               "%s — %s" % (latest_v.get("status", "unverified"),
                            latest_v.get("summary", "")),
               grade_instruction))
        active = enabled_agents(cfg)
        coord = _pick_coordinator(cfg, active) if active else None
        if not coord:
            return None
        reply = call_agent(cfg, app, "adherence_gate", 1, coord, grader_prompt)
        m = ADHERENCE_JSON_RE.search(reply or "")
        if not m:
            return None
        data = json.loads(m.group(1))
        try:
            with open(os.path.join(app_dir, "docs", "adherence.json"), "w",
                      encoding="utf-8") as fh:
                json.dump(data, fh, indent=2)
        except OSError:
            os.makedirs(os.path.join(app_dir, "docs"), exist_ok=True)
            try:
                with open(os.path.join(app_dir, "docs", "adherence.json"), "w",
                          encoding="utf-8") as fh:
                    json.dump(data, fh, indent=2)
            except OSError:
                pass
        if str(data.get("verdict", "")).upper() == "FAIL":
            unmet = [str(r.get("text", ""))[:120]
                     for r in (data.get("requirements") or [])
                     if isinstance(r, dict)
                     and str(r.get("grade", "")).upper() == "UNMET"]
            reason = str(data.get("reason") or "core requirements unmet")
            if unmet:
                reason += " Unmet: " + "; ".join(unmet[:5])
            emit("App '%s': ADHERENCE GATE FAILED — %s" % (app, reason))
            return reason
        emit("App '%s': adherence gate PASS (score %s)."
             % (app, data.get("score", "?")))
        return None
    except Exception as exc:  # noqa: BLE001 - the gate must never kill a run
        emit("WARN adherence gate skipped (%s)." % exc)
        return None


def _seed_golden_scaffold(build_dir):
    """Seed an empty app_build from scaffold/ios_app so builds start from
    the blessed conventions (starter DesignSystem.swift + binding rules)
    instead of a blank folder. Only when no Swift source exists yet."""
    try:
        src = os.path.join(HERE, "scaffold", "ios_app")
        if not build_dir or not os.path.isdir(src):
            return
        os.makedirs(build_dir, exist_ok=True)
        for dp, dns, fns in os.walk(build_dir):
            dns[:] = [d for d in dns if d != ".git"]
            if any(fn.endswith(".swift") for fn in fns):
                return   # already a real build — never overwrite
        copied = []
        for fn in sorted(os.listdir(src)):
            dst = os.path.join(build_dir, fn)
            if not os.path.exists(dst):
                shutil.copy2(os.path.join(src, fn), dst)
                copied.append(fn)
        if copied:
            emit("Golden scaffold: seeded app_build with %s." % ", ".join(copied))
    except OSError as exc:
        emit("WARN golden scaffold skipped (%s)." % exc)


def _load_phase_exemplar(key, max_chars=4000):
    """Newest exported exemplar for this phase key ('' when none)."""
    d = os.path.join(HERE, "knowledge", "exemplars", str(key))
    try:
        files = sorted(
            (os.path.join(d, n) for n in os.listdir(d) if n.endswith(".md")),
            key=os.path.getmtime, reverse=True)
        if not files:
            return ""
        with open(files[0], encoding="utf-8") as fh:
            body = fh.read()[:max_chars]
        return ("\n===== EXEMPLAR — a previous run's output for this phase "
                "that the operator rated GOOD (match this bar) =====\n" + body)
    except OSError:
        return ""


def _queue_release_gate_repair(app, app_dir, state, reason, phases=None,
                               build_phase_key=None, max_repairs=2,
                               gate="release_gate", extra_note=""):
    """Refuse the done flag and route the app into the iterate repair flow
    (same mechanism the shepherd uses for hollow builds), capped so a build
    that never converges can't loop forever. Every call records a blamed
    incident (fleet learning) and extra_note rides into the repair prompt —
    e.g. the exact failing screenshot paths for the repairing agent to open."""
    fllib.record_incident(app_dir, gate, reason)
    n = int(state.get("release_gate_repairs") or 0)
    state["done"] = False
    state["error"] = "release gate: %s" % reason
    mistklib.append_mistake(app_dir, {
        "app": app, "phase": build_phase_key, "cls": "repair_queued",
        "summary": ("release gate failed: %s (repair %d/%d)"
                    % (reason, min(n + 1, max_repairs), max_repairs))
                   if n < max_repairs else
                   "release gate failed: %s (repair budget exhausted)" % reason})
    if n < max_repairs:
        state["release_gate_repairs"] = n + 1
        # The queued repair only does anything if build_coordination (and
        # everything after it) actually re-runs. Appending "## Change
        # requested" usually forces that by changing the prompt hash, but a
        # PRIOR repair may already have appended one — the guard below skips
        # re-appending, the hash stays put, and completed_phases (all marked
        # done from the failed attempt) would otherwise make the next pass
        # skip every remaining phase and burn the repair budget doing nothing.
        # Clear them directly so a repair is never a no-op.
        if phases and build_phase_key:
            redo_from = next((i for i, p in enumerate(phases)
                             if p.key == build_phase_key), None)
            if redo_from is not None:
                redo_keys = {p.key for p in phases[redo_from:]}
                state["completed_phases"] = [k for k in state.get("completed_phases", [])
                                             if k not in redo_keys]
                for k in redo_keys:
                    state.get("phase_outputs", {}).pop(k, None)
                    state.get("consensus_status", {}).pop(k, None)
                    state.get("vote_results", {}).pop(k, None)
        req = ("The app currently FAILS its release gate: %s. Fix every "
               "problem named until the gates pass; do not drop features "
               "unless unavoidable." % reason)
        if extra_note:
            req += "\n" + extra_note
        prompt_p = os.path.join(app_dir, "initial_prompt", "initial_prompt.md")
        try:
            with open(prompt_p, encoding="utf-8") as fh:
                existing = fh.read()
            # REPLACE any prior "## Change requested" block with the CURRENT
            # failure, don't just skip when one exists: a repair pass routinely
            # surfaces a DIFFERENT gate than the one that triggered it (fixing
            # UI-crawl reveals a design-lint letterbox error), and leaving the
            # stale block told the agents to fix the wrong, already-addressed
            # problem — so the new failure never converged. The block is always
            # the tail (this is the only writer), so truncate from its marker
            # and re-append the fresh reason. Changing the text also moves the
            # prompt hash, which is what forces the build phases to actually
            # re-run rather than resume as already-done.
            marker = "\n\n## Change requested\n"
            idx = existing.find(marker)
            base = existing[:idx] if idx != -1 else existing.rstrip("\n")
            with open(prompt_p, "w", encoding="utf-8") as fh:
                fh.write(base + "%s%s\n" % (marker, req))
            with open(os.path.join(app_dir, "workflow.txt"), "w",
                      encoding="utf-8") as fh:
                fh.write("iterate\n")
            open(os.path.join(app_dir, ".repair_pending"), "w").close()
            emit("App '%s': RELEASE GATE FAILED (%s) — queued repair %d/%d, "
                 "NOT marked done." % (app, reason, n + 1, max_repairs))
        except OSError as exc:
            emit("App '%s': release gate failed (%s) but repair queueing "
                 "errored: %s" % (app, reason, exc))
    else:
        emit("App '%s': RELEASE GATE FAILED (%s) — repair budget exhausted "
             "(%d attempts); left as needs_repair for a human."
             % (app, reason, max_repairs))
    save_state(app_dir, state)


def _repair_portfolio_manifest(cfg, app, app_dir, state):
    """One-shot self-heal when portfolio_selection closed without a parseable
    ```portfolio-json``` manifest (usually fence-format drift): ask the
    coordinator to re-emit ONLY the block from its own selection decision,
    append it to the stored phase output, and let the caller re-parse.
    Returns True only when the re-emitted block parses into selected apps."""
    sel = (state.get("phase_outputs") or {}).get("portfolio_selection") or ""
    # Cheap first: the phase transcript often still holds a valid fence from an
    # earlier round even when the recorded closing output lost it (exp7: a
    # provider limit banner was recorded as the final message while round 1's
    # transcript had the full manifest). No LLM call needed to recover that.
    md = os.path.join(app_dir, "portfolio_selection", "portfolio_selection.md")
    try:
        with open(md, encoding="utf-8") as fh:
            recovered, _ = portfoliolib.parse_portfolio_manifest(fh.read())
    except OSError:
        recovered = {}
    if recovered.get("apps"):
        state.setdefault("phase_outputs", {})["portfolio_selection"] = (
            sel.rstrip()
            + "\n\n===== MANIFEST (recovered from phase transcript) =====\n"
            + "```portfolio-json\n" + json.dumps(recovered) + "\n```")
        save_state(app_dir, state)
        emit("Portfolio manifest recovered from the phase transcript (%d app(s))."
             % len(recovered["apps"]))
        return True
    if not sel.strip():
        return False
    coord = _pick_coordinator(cfg, enabled_agents(cfg))
    prompt = (
        "Below is the closing decision of a portfolio_selection phase. It was "
        "supposed to include a fenced code block whose info string is exactly "
        "portfolio-json (```portfolio-json) holding the selected-apps manifest, "
        "but that block is missing or malformed. Re-emit ONLY that fenced "
        "block: one JSON object with the top-level key \"apps\" — a list of "
        "objects with name, slug, build, and any other fields already present "
        "in the text. Preserve the apps exactly as decided. No prose before or "
        "after the block.\n\n===== SELECTION DECISION =====\n"
        + _budget(sel, 20000))
    try:
        resp = call_agent(cfg, app, "portfolio_selection", "manifest-repair",
                          coord, prompt)
    except AgentError as exc:
        emit("Portfolio manifest repair failed: %s" % exc)
        return False
    manifest, errors = portfoliolib.parse_portfolio_manifest(resp)
    for err in errors:
        emit("PORTFOLIO repair: %s" % err)
    if not manifest.get("apps"):
        emit("Portfolio manifest repair produced no parseable apps — aborting.")
        return False
    state.setdefault("phase_outputs", {})["portfolio_selection"] = (
        sel.rstrip() + "\n\n===== MANIFEST (re-emitted by repair) =====\n"
        + resp.strip())
    save_state(app_dir, state)
    emit("Portfolio manifest repair: coordinator re-emitted %d app(s)."
         % len(manifest["apps"]))
    return True


def _portfolio_delegation_reason():
    return ("Delegated on portfolio parent: selected apps were materialized as "
            "sibling child projects from portfolio_manifest.json. Each child runs "
            "its own phase workflow separately: build=true children use app_build; "
            "build=false children use app_spec.")


def _apply_workflow_overrides(cfg, workflow, phases):
    """Apply the workflow's optional top-level "overrides" preset for this run:
    claude_model / codex_model swap the resolved models, rounds_scale multiplies
    each phase's rounds (round to int, min 1), and effort "fast" caps every
    phase at 2 rounds / "max" floors discussion (writes:false) phases at 3.
    Models are rebuilt from a pristine base each run so a preset can never leak
    into the next app (cfg and its models/_resolved dicts are shared across apps
    and --watch passes). A workflow without "overrides" behaves exactly as before."""
    tctx = tcxlib.TurnContext(cfg)
    # The containment check stays a raw `not in` — reading through the view
    # would be equivalent (getters never seed), but the latch IS the
    # one-time-snapshot contract, so keep it explicit on the dict.
    if "_base_models" not in cfg:
        tctx.base_models = dict(cfg.get("models") or {})
        tctx.base_resolved = dict(cfg.get("_resolved") or {})
    cfg["models"] = dict(cfg["_base_models"])
    tctx.resolved = dict(cfg["_base_resolved"])
    ov = getattr(workflow, "overrides", None) or {}
    if not ov:
        return
    if ov.get("claude_model"):
        cfg["models"]["claude"] = tctx.resolved["claude_model"] = ov["claude_model"]
    if ov.get("codex_model"):
        # The preset is authoritative: it also replaces the probed "preferred"
        # model so detect_codex_model can't reintroduce another one.
        cfg["models"]["codex"] = tctx.resolved["codex_model"] = ov["codex_model"]
        cfg["models"]["codex_preferred_if_available"] = ov["codex_model"]
    try:
        scale = float(ov.get("rounds_scale") or 0)
    except (TypeError, ValueError):
        scale = 0.0
    effort = str(ov.get("effort") or "standard").strip().lower()
    # Claude reasoning-effort parity: the preset's effort tier also maps onto
    # the `claude --effort` keys (fast -> low, max -> high), the same way the
    # tier already shapes rounds. "standard" leaves the config values (default
    # "" = flag omitted) untouched.
    _claude_effort = {"fast": "low", "max": "high"}.get(effort)
    if _claude_effort:
        cfg["models"]["claude_reasoning"] = _claude_effort
        cfg["models"]["claude_build_reasoning"] = _claude_effort
    for p in phases:
        r = int(p.get("rounds") or 0)
        if r > 0 and scale > 0:
            r = max(1, int(round(r * scale)))
        if effort == "fast":                      # cap AFTER rounds_scale
            r = 2 if r <= 0 else min(r, 2)        # (unlimited counts as capped)
        elif effort == "max" and not p.get("writes") and 0 < r < 3:
            r = 3                                 # floor discussion phases
        if r != int(p.get("rounds") or 0):
            p.rounds = r
    emit("Workflow '%s': overrides preset active (%s)."
         % (workflow.name, ", ".join("%s=%s" % kv for kv in sorted(ov.items()))))


def _prepare_url_context(cfg, app, app_dir, prompt):
    """Fetch every URL named in the initial prompt into <app>/docs/fetched/
    and stash the assembled ground-truth block on cfg["_url_context"]
    (build_context injects it into urlfetch.URL_CONTEXT_PHASES only).

    A fresh, fully-successful cache (<24h, every URL OK) is reused so resumes
    and --watch passes don't hammer the sites; anything missing, stale, or
    FAILED triggers a refetch of the lot. One log line per URL plus one
    url_fetched / url_fetch_failed event per URL on real fetches."""
    urls = urlfetchlib.extract_urls(prompt)
    if not urls:
        return
    cache_dir = os.path.join(app_dir, "docs", "fetched")
    if urlfetchlib.cache_is_fresh(cache_dir, urls):
        results = urlfetchlib.load_cached(cache_dir)
        emit("App '%s': reusing %d fresh fetched-URL result(s) from docs/fetched/."
             % (app, len(results)))
    else:
        emit("App '%s': prompt references %d URL(s) — fetching ground truth "
             "into docs/fetched/." % (app, len(urls)))
        results = urlfetchlib.fetch_all(prompt, cache_dir)
        for res in results:
            if res.get("ok"):
                emit("App '%s': fetched %s (%d chars)."
                     % (app, res["url"], len(res.get("text") or "")))
                evlib.emit_event(app_dir, "url_fetched", project=app,
                                 url=res["url"], title=res.get("title") or None,
                                 chars=len(res.get("text") or ""))
            else:
                reason = res.get("error") or "unknown error"
                emit("App '%s': URL fetch FAILED: %s (%s)."
                     % (app, res["url"], reason))
                evlib.emit_event(app_dir, "url_fetch_failed", project=app,
                                 url=res["url"], reason=reason)
    tcxlib.TurnContext(cfg).url_context = urlfetchlib.build_url_context(results)


# Directories excluded from the target-change signature: VCS/build/dependency
# churn shouldn't re-trigger a run, and skipping them keeps the walk cheap.
_TSIG_PRUNE_DIRS = {".git", "node_modules", "DerivedData", ".build", "build",
                    "Pods", ".gradle", "__pycache__", ".venv", "venv"}


def _run_app_pipeline(cfg, app, app_dir, prompt):
    state = load_state(app_dir)
    tctx = tcxlib.TurnContext(cfg)
    # Sinks for structured events (§6) + fallback-count aggregation: every
    # call_agent/_call_agent_once down-stack finds the project dir and the
    # live state dict on cfg (both per-app; cfg is copied per app/worker).
    tctx.app_dir = app_dir
    tctx.state = state
    if _is_stale_running_state(app_dir, state, app=app):
        emit("App '%s': detected stale 'running' state — recovering and resuming"
             % app)
        state.setdefault("phase_outputs", {})
        state.setdefault("consensus_status", {})
        state["error"] = None
        state["blocked_conflict"] = None
        state["awaiting_approval"] = None
        state["next_agent"] = None
        state["runner_pid"] = os.getpid()
        save_state(app_dir, state)
    # Nested sessions live two levels down — dirname(app_dir) is NOT the
    # workspace root for them; cfg["root"] is authoritative.
    root = cfg.get("root") or os.path.dirname(app_dir)
    tctx.original_prompt = prompt

    # Resolve the workflow FIRST (so an audit target folds into change detection).
    workflow = wflib.resolve_workflow_for_app(
        app_dir, cget(cfg, "runtime.default_workflow", wflib.DEFAULT_WORKFLOW), HERE)
    phases = workflow.phases
    # Per-run/per-app state must NOT leak across apps (cfg is reused across apps in
    # one pass and in --watch): reset the completeness multiplier, autonomy, and the
    # circuit-breaker health map before this app applies its own.
    tctx.round_multiplier = None
    tctx.autonomy = None
    tctx.reset_agent_health()
    # Workflow "overrides" preset (optional top-level JSON key): per-run model
    # swap + effort/rounds shaping. Also resets any previous run's model preset.
    _apply_workflow_overrides(cfg, workflow, phases)
    # V2 §7.2/§14: apply the per-project run config (completeness profile picks the
    # phase subset; stop_after_phase truncates at a target). Optional file; no file
    # => full workflow, unchanged behavior.
    _rc = complib.load_run_config(app_dir)
    if _rc.get("completeness"):
        _before = len(phases)
        phases = complib.filter_phases(phases, _rc["completeness"],
                                       on_warn=lambda m: emit("WARN " + m))
        tctx.round_multiplier = complib.round_multiplier(_rc["completeness"])
        tctx.completeness = _rc["completeness"]
        emit("Completeness '%s': %d of %d phases included."
             % (_rc["completeness"], len(phases), _before))
    if _rc.get("stop_after_phase"):
        _before = len(phases)
        phases = complib.apply_stop_target(phases, _rc["stop_after_phase"],
                                           on_warn=lambda m: emit("WARN " + m))
        if len(phases) < _before:
            emit("Stop target: run will stop after '%s'." % _rc["stop_after_phase"])
    if _rc.get("autonomy"):
        tctx.autonomy = _rc["autonomy"]
    tctx.workflow_name = workflow.name
    tctx.workflow_target = workflow.target
    # First verify spec any phase carries (usually build_verification's): the
    # per-iteration build verifier reuses it so both gates compile the same way.
    tctx.workflow_verify_spec = next(
        (p.get("verify") for p in phases if hasattr(p, "get") and p.get("verify")),
        None)
    _sdir = _section_dir(cfg)
    tctx.personalities, tctx.roles = roleslib.load_roles_layered(
        HERE, section_dir=_sdir, on_fallback=_rules_fallback_banner(cfg))
    tctx.agent_role_overrides = roleslib.load_agent_role_overrides_layered(
        HERE, section_dir=_sdir)
    # Precomputed once per run (not rebuilt on every process_phase call) — see
    # assign_personas' role_by_id parameter.
    tctx.role_by_id = {r.get("id"): r for r in cfg["_roles"]}

    # Audit target: the read-only pre-existing codebase this app analyzes.
    tctx.target_path = wflib.read_target_path(app_dir, HERE)
    tctx.target_digest = ""
    tctx.read_dir = None
    # library_mining analyzes MANY repos at once (a whole portfolio).
    tctx.target_paths = (wflib.read_target_paths(app_dir, HERE)
                            if workflow.target == "library_mining" else [])
    if workflow.target == "library_mining" and not cfg["_target_path"]:
        tctx.target_path = cfg["_target_paths"][0] if cfg["_target_paths"] else None
    if workflow.target == "audit" and not cfg["_target_path"]:
        msg = ("audit workflow needs <app>/target_path.txt (or a 'target:' line in "
               "initial_prompt.md) pointing at an existing codebase dir outside app_build.")
        emit("App '%s': %s" % (app, msg))
        state["error"] = msg
        save_state(app_dir, state)
        return
    if workflow.target == "library_mining" and not cfg["_target_paths"]:
        msg = ("library_mining needs <app>/target_path.txt with one repo dir per line "
               "(the portfolio to analyze for reusable code).")
        emit("App '%s': %s" % (app, msg))
        state["error"] = msg
        save_state(app_dir, state)
        return

    # Change detection: prompt + (for audit) the target path and a cheap mtime
    # signature, so editing the prompt OR the target's files re-triggers a run.
    _tgt = cfg.get("_target_path") or ""
    _tsig = ""
    if _tgt:
        try:
            # Cheap aggregate signature instead of a giant sorted relpath+mtime
            # string: prune heavy/irrelevant dirs and track running (count,
            # newest-mtime, total-size) in O(1) memory. Any file edit bumps the
            # mtime; adds/removes change the count/size; a dir mtime catches a
            # rename that doesn't touch a file. Much faster on large targets.
            count, newest, total = 0, 0.0, 0
            for dp, dns, fns in os.walk(_tgt):
                dns[:] = [d for d in dns if d not in _TSIG_PRUNE_DIRS]
                try:
                    newest = max(newest, os.path.getmtime(dp))
                except OSError:
                    pass
                for fn in fns:
                    try:
                        st = os.stat(os.path.join(dp, fn))
                    except OSError:
                        continue
                    count += 1
                    total += st.st_size
                    if st.st_mtime > newest:
                        newest = st.st_mtime
            _tsig = "%d|%.3f|%d" % (count, newest, total)
        except OSError:
            _tsig = _tgt
    phash = sha256_text(prompt + "\n#target:" + _tgt + "\n#tsig:" + sha256_text(_tsig))

    if state.get("prompt_hash") != phash:
        emit("App '%s': new/updated input detected — (re)starting pipeline." % app)
        reset_state_for_new_prompt(state, phash)
        save_state(app_dir, state)
    elif state.get("done"):
        emit("App '%s': unchanged and already done — skipping." % app)
        return
    else:
        emit("App '%s': resuming from incomplete pipeline." % app)
        if state.get("blocked_conflict") and not cfg.get("_explicit_app"):
            # A watch/auto pass must NOT re-enter a conflict-blocked build:
            # setup_lane_worktrees would wipe the preserved lane worktrees the
            # user was told to resolve. Explicit --app/--resume means "go".
            emit("App '%s': blocked on a merge conflict — resolve it, then run "
                 "this app explicitly (--app or --resume) to continue." % app)
            return
        pending_approval = state.get("awaiting_approval")
        if state.get("error") or state.get("blocked_conflict") or pending_approval \
                or state.get("awaiting_human"):
            # Re-entering makes the run active again: clear the previous
            # attempt's abort/conflict/approval markers so status re-derives as
            # running instead of pinning stale banners for the whole re-run.
            # awaiting_human included: a SIGTERM mid-_await_inbox sys.exit(0)s
            # without clearing it, and the resumed run would otherwise execute
            # a full roster round while claiming to be waiting for the human.
            state["error"] = None
            state["blocked_conflict"] = None
            state["awaiting_approval"] = None
            state["awaiting_human"] = None
            save_state(app_dir, state)
        if pending_approval:
            # The run died while paused at this checkpoint. The phase is already
            # in completed_phases, so re-arm the approval instead of silently
            # skipping the human decision.
            emit("Re-arming interrupted approval checkpoint after phase '%s'."
                 % pending_approval)
            decision, payload = _await_approval(app_dir, pending_approval, state,
                                                timeout=_approval_timeout(cfg))
            if decision == "changes_requested":
                if pending_approval in state.get("completed_phases", []):
                    state["completed_phases"].remove(pending_approval)
                state.get("phase_outputs", {}).pop(pending_approval, None)
                save_state(app_dir, state)
                if (payload or "").strip():
                    try:
                        with open(os.path.join(app_dir, "human_inbox.txt"), "a",
                                  encoding="utf-8") as fh:
                            fh.write("\n[change request after %s]\n%s\n"
                                     % (pending_approval, payload.strip()))
                    except OSError:
                        pass
            elif decision == "edited" and (payload or "").strip():
                state.setdefault("phase_outputs", {})[pending_approval] = payload
                save_state(app_dir, state)

    if state.get("workflow") != workflow.name:
        state["workflow"] = workflow.name
        save_state(app_dir, state)
    if workflow.target == "app" and not cfg.get("_tech_stack_block"):
        tctx.tech_stack_block = dlintlib.render_tech_stack(
            dlintlib.load_tech_stack(HERE))
    emit("App '%s': workflow '%s' (%d phases, target=%s)."
         % (app, workflow.name, len(phases), workflow.target))
    evlib.emit_event(app_dir, "run_started", project=app,
                     workflow=workflow.name, phases=len(phases))
    if cfg.get("_gemini_disabled_reason"):
        # Startup-probe verdict (§4.7): surface the auto-disable per project so
        # any UI tailing events.jsonl can badge the missing agent.
        evlib.emit_event(app_dir, "agent_disabled", project=app, agent="gemini",
                         reason=cfg["_gemini_disabled_reason"])
    if cfg["_target_path"]:
        emit("App '%s': audit target = %s" % (app, cfg["_target_path"]))

    # Docs backfill: when the user drops reference docs in <app>/docs/ and
    # touches <app>/.backfill_requested, distill those docs into the pending
    # discussion phases before the normal pipeline runs. Build/QA/verification
    # phases are never backfilled — they always run live. Best-effort: a broken
    # backfill must never take the run down with it.
    try:
        backfilllib.run_backfill(cfg, app, app_dir, phases, state, call_agent)
    except Exception as exc:
        emit("App '%s': docs backfill failed (%s) — continuing with normal run."
             % (app, exc))

    # URL ground truth (urlfetch.py): when the prompt references live URLs
    # ("build me something like X: https://..."), the ENGINE fetches them now —
    # agent CLIs have no web access, and a run once spent 3 rounds guessing what
    # a linked product was and built the wrong app. Fetched text lands in
    # <app>/docs/fetched/ and cfg["_url_context"]; build_context injects it into
    # the product-definition phases only. Best-effort: failures inject an
    # explicit UNVERIFIED warning, and nothing here may take the run down.
    tctx.url_context = ""
    if bool(cget(cfg, "runtime.fetch_prompt_urls", True)):
        try:
            _prepare_url_context(cfg, app, app_dir, prompt)
        except Exception as exc:  # noqa: BLE001 - prefetch is strictly best-effort
            emit("App '%s': URL prefetch failed (%s) — continuing without it."
                 % (app, exc))

    # Sprint / time-budget mode. When the workflow declares a budget, carve the
    # wall-clock into a planning slice, a build slice, and a verify tail, and stash
    # a hard run deadline. Everything else (process_phase, call_agent, the build and
    # verify loops) reads these cfg keys; a workflow without a budget leaves them
    # all None and behaves exactly as before.
    budget = getattr(workflow, "budget", None)
    tctx.budget = budget
    tctx.deadline = None
    tctx.phase_deadline = None
    tctx.turn_timeout = None
    plan_deadline = build_deadline = None
    build_idx = None
    if budget:
        _start = time.time()
        _total = float(budget.get("time_budget_minutes", 55)) * 60.0
        _build_res = float(budget.get("build_reserve_minutes", 42)) * 60.0
        _verify_res = float(budget.get("verify_reserve_minutes", 8)) * 60.0
        tctx.deadline = _start + _total
        plan_deadline = _start + max(60.0, _total - _build_res)
        build_deadline = cfg["_deadline"] - _verify_res
        build_idx = next((j for j, p in enumerate(phases)
                          if p.key == workflow.build_phase), None)
        emit("Sprint mode: %.0f-min hard ceiling — plan by +%.0fm, build by +%.0fm."
             % (_total / 60.0, (plan_deadline - _start) / 60.0,
                (build_deadline - _start) / 60.0))

    prior_outputs = []
    # Staged continuation (--continue-with): outputs from a PREVIOUS workflow's
    # phases ride along as context, ahead of this workflow's own outputs, so a
    # research/spec-only pass genuinely informs the later build pass.
    _cur_keys = {pk for (pk, *_rest) in phases}
    for ck, cv in (state.get("carryover_outputs") or {}).items():
        if cv and ck not in _cur_keys:
            prior_outputs.append((ck, cv))
    for key in [pk for (pk, *_rest) in phases if pk in state.get("completed_phases", [])]:
        out = state.get("phase_outputs", {}).get(key)
        if out:
            prior_outputs.append((key, out))

    try:
        i = 0
        manifest_repair_attempted = False
        while i < len(phases):
            phasedef = phases[i]
            key = phasedef[0]
            if key in state.get("completed_phases", []):
                i += 1
                continue
            is_portfolio_parent = portfoliolib.is_portfolio_parent_prompt(prompt)
            if is_portfolio_parent:
                _maybe_materialize_portfolio_children(cfg, root, app, app_dir,
                                                      prompt, state)
                has_manifest = bool(portfoliolib.load_manifest(app_dir))
                if has_manifest and key in _PORTFOLIO_DELEGATED_PHASES:
                    out = _record_skipped_phase(
                        app, app_dir, phasedef, prompt, state,
                        _portfolio_delegation_reason())
                    prior_outputs.append((key, out))
                    i += 1
                    continue
                if key in _PORTFOLIO_DELEGATED_PHASES and not has_manifest:
                    # Self-heal before aborting: the usual cause is format drift
                    # in portfolio_selection's closing message (manifest present
                    # but not in a strict ```portfolio-json``` fence). One
                    # coordinator retry re-emits just the block; the next loop
                    # pass re-parses and delegates normally.
                    if not manifest_repair_attempted:
                        manifest_repair_attempted = True
                        if _repair_portfolio_manifest(cfg, app, app_dir, state):
                            continue
                    raise AppError(
                        "portfolio parent reached '%s' without a portfolio-json "
                        "manifest. portfolio_selection must emit the selected-app "
                        "manifest before any per-app phase begins; refusing to "
                        "collapse multiple apps into one app_build folder." % key)
            tctx.prior_discussions = _select_prior_context(
                cfg, app_dir, phases, state.get("completed_phases", []), phasedef)
            if budget:
                _now = time.time()
                _is_build = (build_idx is not None and i == build_idx)
                _pre_build = (build_idx is not None and i < build_idx)
                if _pre_build and _now >= plan_deadline:
                    # Planning budget spent — skip remaining pre-build phases and get
                    # to the build with whatever plan we have.
                    emit("Sprint: plan budget spent — skipping pre-build phase '%s' "
                         "to reach the build." % key)
                    i += 1
                    continue
                if _is_build:
                    tctx.phase_deadline = build_deadline
                elif _pre_build:
                    tctx.phase_deadline = plan_deadline
                else:  # post-build (e.g. review): use the remaining time to the ceiling
                    tctx.phase_deadline = cfg["_deadline"]
            out = process_phase(cfg, app, app_dir, phasedef, prompt, prior_outputs,
                                state, phase_index=i)
            prior_outputs.append((key, out))
            if key in ("portfolio_selection", "app_features", "project_plan") and \
                    portfoliolib.is_portfolio_parent_prompt(prompt):
                _maybe_materialize_portfolio_children(cfg, root, app, app_dir,
                                                      prompt, state, out)
            # V2 §3: semi-autonomous / manual checkpoint pause. Fully-autonomous
            # (the default) never pauses. Not for the last phase (nothing follows).
            if i < len(phases) - 1 and _should_pause_after(cfg, phasedef):
                decision, payload = _await_approval(app_dir, key, state,
                                                    timeout=_approval_timeout(cfg))
                if decision == "edited" and (payload or "").strip():
                    # Edit & Approve: the human's text REPLACES the phase output
                    # everywhere downstream phases read it.
                    out = payload
                    state.setdefault("phase_outputs", {})[key] = out
                    prior_outputs[-1] = (key, out)
                    save_state(app_dir, state)
                    emit("Phase '%s' output replaced by human edit (%d chars)."
                         % (key, len(out)))
                elif decision == "changes_requested":
                    # Request Changes: drop the phase back to not-completed, put
                    # the feedback where the re-run's conversation will pick it
                    # up (human_inbox), and loop WITHOUT advancing.
                    if key in state.get("completed_phases", []):
                        state["completed_phases"].remove(key)
                    state.get("phase_outputs", {}).pop(key, None)
                    prior_outputs.pop()
                    save_state(app_dir, state)
                    if (payload or "").strip():
                        try:
                            with open(os.path.join(app_dir, "human_inbox.txt"), "a",
                                      encoding="utf-8") as fh:
                                fh.write("\n[change request after %s]\n%s\n"
                                         % (key, payload.strip()))
                        except OSError:
                            pass
                    emit("Changes requested on '%s' — re-running the phase." % key)
                    continue
            i += 1
        gate_reason = _release_gate_failure(app_dir, phases, state, prompt, cfg=cfg)
        if gate_reason:
            _queue_release_gate_repair(app, app_dir, state, gate_reason,
                                       phases=phases, build_phase_key=workflow.build_phase)
            evlib.emit_event(app_dir, "run_finished", project=app,
                             status="release_gate_repair", detail=gate_reason,
                             verification=state.get("verification"))
            return
        if workflow.target == "app":
            # Free and deterministic first: token/dependency lint.
            lint_reason = _run_timed_gate(
                app_dir, "design_lint",
                lambda: dlintlib.run_design_lint(cfg, cget, emit, app,
                                                 app_dir, HERE))
            if lint_reason:
                _queue_release_gate_repair(
                    app, app_dir, state, "design lint: " + lint_reason,
                    phases=phases, build_phase_key=workflow.build_phase,
                    gate="design_lint",
                    extra_note="Full findings with file:line are in "
                               "docs/design_lint.json — fix EVERY error entry.")
                evlib.emit_event(app_dir, "run_finished", project=app,
                                 status="design_lint_repair",
                                 detail=lint_reason)
                return
            # Compiles ≠ looks finished: boot the simulator, screenshot the
            # app in light+dark, grade with the LOCAL vision panel (free).
            vqa_reason = _run_timed_gate(
                app_dir, "visual_qa",
                lambda: vqalib.run_visual_qa(cfg, cget, emit, app, app_dir,
                                             state, prompt))
            if vqa_reason:
                _queue_release_gate_repair(
                    app, app_dir, state, vqa_reason,
                    phases=phases, build_phase_key=workflow.build_phase,
                    gate="visual_qa",
                    extra_note="OPEN THESE IMAGE FILES and fix exactly what "
                               "you see: docs/screenshots/main_light.png and "
                               "docs/screenshots/main_dark.png. Grader "
                               "verdict: docs/visual_qa.json.")
                evlib.emit_event(app_dir, "run_finished", project=app,
                                 status="visual_qa_repair", detail=vqa_reason)
                return
            # Compiles + looks right ≠ behaves right: crawl every screen, tap
            # everything, replay declared user journeys. Crashes learn back
            # into flows.json as permanent regression flows.
            crawl_reason = _run_timed_gate(
                app_dir, "ui_crawl",
                lambda: uicrawllib.run_ui_crawl(cfg, cget, emit, app,
                                                app_dir, state, prompt))
            if crawl_reason:
                _gate = "ui_crawl_crash" if "crash" in crawl_reason \
                    else "ui_crawl_flow"
                _queue_release_gate_repair(
                    app, app_dir, state, "UI crawl: " + crawl_reason,
                    phases=phases, build_phase_key=workflow.build_phase,
                    gate=_gate,
                    extra_note="Screen graph, per-screen screenshots, dead "
                               "taps, and flow results are in docs/ui_crawl/ "
                               "and docs/ui_crawl.json — open the failing "
                               "screen's screenshot before fixing.")
                evlib.emit_event(app_dir, "run_finished", project=app,
                                 status="ui_crawl_repair", detail=crawl_reason)
                return
        # Compiles ≠ does what was asked: grade the build against the original
        # prompt's requirements; unmet core requirements route into the same
        # bounded iterate-repair loop instead of a hollow "done".
        adh_reason = _run_timed_gate(
            app_dir, "adherence",
            lambda: _adherence_gate(cfg, app, app_dir, phases, state, prompt,
                                    workflow))
        if adh_reason:
            _queue_release_gate_repair(app, app_dir, state,
                                       "prompt adherence: " + adh_reason,
                                       phases=phases,
                                       build_phase_key=workflow.build_phase,
                                       gate="adherence",
                                       extra_note="Per-requirement grades are "
                                                  "in docs/adherence.json.")
            evlib.emit_event(app_dir, "run_finished", project=app,
                             status="adherence_repair", detail=adh_reason)
            return
        state["done"] = True
        state["error"] = None
        # A finished run can't still be blocked or awaiting anything — without
        # this, derive_run_status reports those over 'done' forever.
        state["blocked_conflict"] = None
        state["awaiting_approval"] = None
        save_state(app_dir, state)
        # V2 §24: deterministically render project docs from the phase outputs.
        try:
            ordered = [(p.key, (p.title if hasattr(p, "title")
                                else p.key.replace("_", " ").title())) for p in phases]
            _latest_v = verifylib.latest_verify_result(app_dir, prompt_hash=state.get("prompt_hash"))
            _vsum = ("%s (%s)" % (_latest_v.get("status", "").upper(), _latest_v.get("summary", ""))
                     if _latest_v else "")
            written = docslib.write_project_docs(
                app_dir, app, ordered, state.get("phase_outputs", {}),
                consensus_status=state.get("consensus_status", {}),
                workflow_name=workflow.name, verify_summary=_vsum,
                findings=load_docs_findings(app_dir),
                blocked_conflict=state.get("blocked_conflict"),
                conversation_end=state.get("conversation_end", {}))
            written += docslib.write_project_archive(
                app_dir, app, phases, prompt, state,
                workflow_name=workflow.name, verify_summary=_vsum,
                findings=load_docs_findings(app_dir))
            if written:
                emit("Rendered docs: %s" % ", ".join(written))
        except Exception as exc:  # noqa: BLE001 - docs are best-effort, never fatal
            emit("WARN docs render failed: %s" % exc)
        emit("App '%s': ALL phases complete. Marked done." % app)
        # Fleet learning: refresh the anti-pattern ledger after every
        # finished run (deterministic text only — no model call).
        # runtime.fleet_ledger_enabled=false opts a run out — used by tests
        # (the ledger lives in the ENGINE checkout, so an opted-in test run
        # would rewrite this repo's own tracked knowledge/anti_patterns.md)
        # and useful for throwaway/demo roots that shouldn't teach the fleet.
        if bool(cget(cfg, "runtime.fleet_ledger_enabled", True)):
            try:
                _lpath, _lclusters = fllib.build_ledger(
                    os.path.dirname(os.path.abspath(app_dir)), HERE)
                if _lpath:
                    emit("Anti-pattern ledger refreshed (%d cluster(s))." % _lclusters)
            except Exception:  # noqa: BLE001 - learning must never fail a run
                pass
        evlib.emit_event(app_dir, "run_finished", project=app, status="done",
                         verification=state.get("verification"))
    except AgentError as exc:
        state["error"] = str(exc)
        state["done"] = False
        save_state(app_dir, state)
        emit("App '%s': ABORTED — %s" % (app, exc))
        evlib.emit_event(app_dir, "run_finished", project=app,
                         status="aborted", detail=str(exc),
                         verification=state.get("verification"))
    except AppError as exc:
        state["error"] = str(exc)
        save_state(app_dir, state)
        emit("App '%s': skipped — %s" % (app, exc))
        evlib.emit_event(app_dir, "run_finished", project=app,
                         status="skipped", detail=str(exc),
                         verification=state.get("verification"))


# ---------------------------------------------------------------------------
# Doctor / preflight
# ---------------------------------------------------------------------------
def _tool_version(cmd):
    """Best-effort first-line version string for a CLI; '' if unavailable. Never raises."""
    try:
        out, err, _ = procutil.run_capture(cmd, timeout=15)
        lines = (out + err).strip().splitlines()
        return lines[0].strip() if lines else ""
    except (OSError, subprocess.SubprocessError):
        return ""


# Tools the V2 preflight checks. `build`-class tools gate whether the native macOS
# app can be built on this machine; `agent`-class tools are only needed to RUN a
# generated build later, not to build Orchestrator V2 itself.
_PREFLIGHT_TOOLS = [
    ("python3", ["python3", "--version"], "build"),
    ("git", ["git", "--version"], "build"),
    ("swift", ["swift", "--version"], "build"),
    ("xcodebuild", ["xcodebuild", "-version"], "build"),
    ("codex", ["codex", "--version"], "agent"),
    ("claude", ["claude", "--version"], "agent"),
    ("gemini", ["gemini", "--version"], "agent"),
    ("agy", ["agy", "--version"], "agent"),
    ("ollama", ["ollama", "--version"], "agent"),
]


def preflight_report(cfg):
    """Structured, machine-readable environment preflight (V2 spec §27, `--doctor
    --json`). Pure stdlib version probes only — never invokes an agent turn, so it
    spends no subscription tokens. Consumed by the GUI's first-run onboarding."""
    root = cfg.get("root", "")
    tools = {}
    for name, vercmd, klass in _PREFLIGHT_TOOLS:
        path = which(name)
        tools[name] = {
            "present": bool(path),
            "path": path or "",
            "version": _tool_version(vercmd) if path else "",
            "class": klass,
        }
    # simctl is reached through `xcrun`, not as a bare binary on PATH.
    simctl_ok = False
    if which("xcrun"):
        try:
            simctl_ok = procutil.run_capture(["xcrun", "simctl", "help"], timeout=15)[2] == 0
        except (OSError, subprocess.SubprocessError):
            simctl_ok = False
    tools["simctl"] = {"present": simctl_ok, "path": "xcrun simctl" if simctl_ok else "",
                       "version": "", "class": "build"}
    # §27: the available-simulator list (trimmed), so a consumer can pick a
    # concrete destination without shelling out again. Best-effort: [] whenever
    # simctl is absent or its JSON is unparsable.
    simulators = []
    if simctl_ok:
        try:
            out, _err, code = procutil.run_capture(
                ["xcrun", "simctl", "list", "devices", "available", "--json"], timeout=30)
            if code == 0:
                for runtime_id, devs in sorted((json.loads(out).get("devices") or {}).items()):
                    rt = runtime_id.rsplit(".", 1)[-1]
                    for d in devs or []:
                        if d.get("isAvailable"):
                            simulators.append({"name": d.get("name", ""), "runtime": rt,
                                               "udid": d.get("udid", "")})
        except Exception:  # noqa: BLE001 - report stays usable without the list
            simulators = []

    build_capable = all(tools[t]["present"] for t in ("python3", "git", "swift", "xcodebuild"))
    agents_present = [t for t in ("codex", "claude", "gemini", "agy") if tools[t]["present"]]
    leaked = [k for k in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY",
                          "GOOGLE_API_KEY") if os.environ.get(k)]
    return {
        "schema_version": 1,
        "timestamp": now_str(),
        "root": root,
        "root_exists": os.path.isdir(root) if root else False,
        "git_repo_at_root": os.path.isdir(os.path.join(root, ".git")) if root else False,
        "tools": tools,
        "available_simulators": simulators[:40],
        "build_capable": build_capable,
        "agent_clis_present": agents_present,
        "leaked_api_key_env_vars": leaked,
        "workflows": wflib.list_workflows(HERE),
        "resolved_models": cfg.get("_resolved", {}),
        # Local model manager (V2 spec §12/§27): server reachability, the selected
        # model + whether it's pulled, and the curated registry with installed
        # flags — this is what the GUI's Local Models settings render.
        "local_models": lmlib.report(HERE, cget(cfg, "models.ollama", "")),
        # Per-phase routing + cloud->local fallback (model_routing.json).
        "model_routing": mrlib.summary(mrlib.load_routing(HERE)),
    }


def mistakes_report(root, app=None):
    """Structured `--mistakes` report: the cross-run mistakes-ledger aggregation
    (per-class / per-phase / per-agent counts) plus the verification rollup per
    app. Pure disk reads — never invokes an agent turn. ``app`` filters to one
    project; JSON-serializable for `--mistakes --json`."""
    agg = mistklib.aggregate_mistakes(root)
    if app:
        names = [app]
    else:
        try:
            discovered = find_apps(root) if root and os.path.isdir(root) else []
        except OSError:
            discovered = []
        names = sorted(set(list(agg["apps"]) + discovered))
    apps = {}
    for name in names:
        per = dict(agg["apps"].get(name) or {"total": 0, "by_class": {},
                                             "by_phase": {}, "by_agent": {}})
        app_dir = os.path.join(root, name)
        st = load_state(app_dir)
        per["verification"] = st.get("verification") or derive_verification(app_dir, st)
        apps[name] = per
    if app:
        per = apps[app]
        totals = {k: per[k] for k in ("total", "by_class", "by_phase", "by_agent")}
    else:
        totals = {k: agg[k] for k in ("total", "by_class", "by_phase", "by_agent")}
    report = {"schema_version": 1, "root": root, "apps": apps}
    report.update(totals)
    return report


def print_mistakes_report(rep):
    """Human rendering of mistakes_report (the --mistakes default output)."""
    print("=== MISTAKES: cross-run ledger report ===")
    print("Root: %s" % rep["root"])
    print("Total recorded mistakes: %d" % rep["total"])
    for label, key in (("By class", "by_class"), ("By phase", "by_phase"),
                       ("By agent", "by_agent")):
        counts = rep.get(key) or {}
        if counts:
            print("%s:" % label)
            for k, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])):
                print("  %-28s %d" % (k, n))
    for name, per in sorted((rep.get("apps") or {}).items()):
        print("App %-24s: %d mistake(s); verification=%s"
              % (name, per.get("total", 0), per.get("verification", "unverified")))
    print("=== MISTAKES report complete ===")


def doctor(cfg):
    emit("=== DOCTOR: environment preflight ===")
    root = cfg["root"]
    emit("Root: %s (exists=%s)" % (root, os.path.isdir(root)))
    git_ok = os.path.isdir(os.path.join(root, ".git"))
    emit("Git repo initialized at root: %s" % git_ok)
    found = {}
    for cli in ("codex", "claude", "agy", "gemini", "ollama", "git", "python3"):
        path = which(cli)
        found[cli] = path
        emit("CLI %-8s : %s" % (cli, path or "NOT FOUND"))
    # Local model manager (V2 spec §12/§27): server, selection, curated registry.
    lm = lmlib.report(HERE, cget(cfg, "models.ollama", ""))
    emit("Ollama server running: %s" % lm["server_running"])
    if lm["selected"]:
        emit("Local model selected: %s (installed=%s)"
             % (lm["selected"], lm["selected_installed"]))
    else:
        emit("Local model selected: (none — set models.ollama + agents.ollama_enabled "
             "to add the local agent)")
    for m in lm["registry"]:
        emit("Local model %-20s: %s" % (m["id"],
             "installed" if m["installed"] else "not pulled (ollama pull %s)" % m["id"]))
    # API key guard
    leaked = [k for k in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY",
                          "GOOGLE_API_KEY") if os.environ.get(k)]
    if leaked:
        emit("WARNING: these API-key env vars are set and will be UNSET by run.sh: %s"
             % ", ".join(leaked))
    else:
        emit("No pay-as-you-go API key env vars detected. Good.")
    emit("Resolved models: %s" % json.dumps(cfg["_resolved"]))
    # Agent readiness
    if cget(cfg, "agents.claude_enabled", True) and not found["claude"]:
        emit("NOTE: claude_enabled but `claude` CLI missing — that agent will error.")
    if cget(cfg, "agents.codex_enabled", True) and not found["codex"]:
        emit("NOTE: codex_enabled but `codex` CLI missing — that agent will error.")
    if cget(cfg, "agents.gemini_enabled", True) and not (found["agy"] or found["gemini"]):
        emit("NOTE: gemini_enabled but neither `agy` nor `gemini` found — that agent will error.")
    if cfg.get("_gemini_disabled_reason"):
        emit("NOTE: gemini auto-disabled by the startup probe — %s"
             % cfg["_gemini_disabled_reason"])
    if cget(cfg, "agents.ollama_enabled", False):
        if not found["ollama"]:
            emit("NOTE: ollama_enabled but `ollama` CLI missing — the local agent will error.")
        elif not lm["selected"]:
            emit("NOTE: ollama_enabled but models.ollama is blank — the local agent is inactive.")
    apps = find_apps(root) if os.path.isdir(root) else []
    emit("Apps discovered: %s" % (", ".join(apps) if apps else "(none yet)"))
    emit("Workflows available: %s" % ", ".join(wflib.list_workflows(HERE)))
    pers, rls = roleslib.load_roles(HERE)
    emit("Sub-agent roles: %s" % ", ".join(r["name"] for r in rls))
    emit("Personalities (rotate per phase): %s" % ", ".join(
        p["name"].replace("the ", "").title() for p in pers))
    doms = knowlib.available_domains(HERE)
    emit("Knowledge domains: %s" % (", ".join(doms) if doms else "(none yet)"))
    emit("=== DOCTOR complete ===")
    return found


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def resolve_models(cfg):
    tcxlib.TurnContext(cfg).resolved = {
        "claude_model": cget(cfg, "models.claude", "sonnet") or "sonnet",
        "codex_model": detect_codex_model(cfg),
        "gemini_model": (cget(cfg, "models.gemini_fallback", "")
                         if valid_gemini_model(cget(cfg, "models.gemini_fallback", ""))
                         else ""),
        # Selected local model (V2 spec §12); blank = local agent disabled.
        "ollama_model": str(cget(cfg, "models.ollama", "") or "").strip(),
        # Optional local roster for multi-agent setups. String form keeps YAML
        # simple (comma/semicolon-separated), while config.json can use a list.
        "ollama_roster": _split_local_roster(cget(cfg, "models.ollama_roster", [])),
    }
    # Gemini startup probe (§4.7): one tiny headless call (4h disk cache, like
    # the codex model probe) decides whether gemini joins this run at all. On a
    # failed probe the agent is auto-disabled for the run — ONE clear line here
    # plus a per-project agent_disabled event — instead of every turn burning
    # its timeout on the same auth/TTY failure.
    if cget(cfg, "agents.gemini_enabled", True):
        ok, reason = detect_gemini_available(cfg)
        if not ok:
            tcxlib.TurnContext(cfg).gemini_disabled_reason = reason
            emit("Gemini auto-disabled for this run — %s. (Probe verdict cached "
                 "4h in .gemini_probe.json; fix the key/CLI and delete the cache "
                 "to retry sooner.)" % reason)


def prepare_resume(root, slug):
    """V2 spec §27: --resume <project_slug> restarts an EXISTING project from its
    persisted state. Unlike --app/--project (which also create-or-scan), resume
    refuses to run when there is nothing to resume, and clears a recorded abort
    error so the pipeline re-enters instead of skipping.

    Returns (exit_code, target_app). target_app is None when the caller should
    exit with exit_code instead of processing."""
    app_dir = os.path.join(root, slug)
    if not os.path.isdir(app_dir):
        emit("--resume: no project '%s' under %s" % (slug, root))
        return 2, None
    if not os.path.exists(state_path(app_dir)):
        emit("--resume: project '%s' has no saved state to resume "
             "(run it normally first)." % slug)
        return 2, None
    st = load_state(app_dir)
    if st.get("done") and not st.get("error"):
        emit("--resume: project '%s' is already complete — nothing to resume." % slug)
        return 0, None
    if _is_stale_running_state(os.path.join(root, slug), st, app=slug):
        emit("--resume: project '%s' had a stale 'running' state; clearing it for resume." % slug)
        st["error"] = None
        st["blocked_conflict"] = None
        st["awaiting_approval"] = None
        st["awaiting_human"] = None
        st["next_agent"] = None
        st["runner_pid"] = None
        save_state(app_dir, st)
    if st.get("error") or st.get("blocked_conflict"):
        if st.get("error"):
            emit("--resume: clearing recorded error so '%s' re-enters the pipeline: %s"
                 % (slug, st["error"]))
        if st.get("blocked_conflict"):
            emit("--resume: clearing blocked_conflict marker for '%s' (assuming "
                 "the conflict was resolved manually)." % slug)
        st["error"] = None
        st["blocked_conflict"] = None
        st["done"] = False
        save_state(app_dir, st)
    return 0, slug


def prepare_continue(root, slug, workflow_name):
    """--continue-with <workflow>: re-open an EXISTING project (typically done)
    under a DIFFERENT workflow — e.g. a research/docs-only project continued
    later into a full build. Generalizes the iterate/release-gate reopen
    pattern: rewrite workflow.txt, selectively clear completed_phases for the
    new workflow's phase keys (so they actually run), and stash the previous
    workflow's outputs in carryover_outputs so they keep informing the run as
    context. The prompt hash is untouched, so the pipeline enters its normal
    resume path instead of a from-scratch reset.

    Returns (exit_code, target_app); target_app None means exit exit_code."""
    app_dir = os.path.join(root, slug)
    if not os.path.isdir(app_dir):
        emit("--continue-with: no project '%s' under %s" % (slug, root))
        return 2, None
    if not os.path.exists(state_path(app_dir)):
        emit("--continue-with: project '%s' has never run — a first run already "
             "honors workflow.txt, so run it normally instead." % slug)
        return 2, None
    available = wflib.list_workflows(HERE)
    if workflow_name not in available:
        emit("--continue-with: unknown workflow '%s' (available: %s)"
             % (workflow_name, ", ".join(available)))
        return 2, None
    wf = wflib.load_workflow(workflow_name, HERE)
    new_keys = {p.key for p in wf.phases}
    st = load_state(app_dir)
    # Prior-workflow outputs whose phase keys the new workflow does NOT re-run
    # become carryover context (later continues stack on top of earlier ones).
    carry = dict(st.get("carryover_outputs") or {})
    for k, v in (st.get("phase_outputs") or {}).items():
        if v and k not in new_keys:
            carry[k] = v
    st["carryover_outputs"] = carry
    # Phase keys shared with the new workflow must re-run: drop them from
    # completed_phases and clear their per-phase records (same surgery the
    # release-gate repair does) so the pass is never a silent no-op.
    redo = [k for k in st.get("completed_phases", []) if k in new_keys]
    st["completed_phases"] = [k for k in st.get("completed_phases", [])
                              if k not in new_keys]
    for k in redo:
        st.get("phase_outputs", {}).pop(k, None)
        st.get("consensus_status", {}).pop(k, None)
        st.get("vote_results", {}).pop(k, None)
    st["done"] = False
    st["error"] = None
    st["blocked_conflict"] = None
    st["awaiting_approval"] = None
    st["workflow"] = workflow_name
    save_state(app_dir, st)
    try:
        with open(os.path.join(app_dir, "workflow.txt"), "w", encoding="utf-8") as fh:
            fh.write(workflow_name + "\n")
    except OSError as exc:
        emit("--continue-with: could not write workflow.txt for '%s': %s"
             % (slug, exc))
        return 2, None
    emit("--continue-with: '%s' re-opened under workflow '%s' — %d phase(s) will "
         "run, %d prior output(s) carried forward as context."
         % (slug, workflow_name, len(new_keys), len(carry)))
    return 0, slug


def fork_session(root, src_slug, new_slug=None):
    """V3 board 1.9: fork = a copy of a session dir with every continuity and
    coordination carrier stripped.

    Excluded from the copy, each for a concrete reason:
    - `.agent_cwd/` — the stable cwd the claude CLI keys its session store by:
      the on-disk agent-thread carrier. Cloning it would resume the SOURCE's
      threads from the fork; `codex exec resume` always runs workspace-write
      regardless of the original sandbox, so a cloned thread id can silently
      upgrade a read-only discussion's sandbox (GLOSSARY, agent thread).
    - `*.lock` files — locks name a RUN, never content.
    - `approvals/` — pending decision files would instantly answer the fork's
      first checkpoint (or end its first conversational round).
    - `human_inbox.txt` / `.step_in` — an undrained message must not deliver
      twice, once in each timeline.
    agent_state.json is then sanitized: runner_pid/next_agent/awaiting/error
    markers cleared; completed_phases and phase_outputs are content and stay.

    Returns (exit_code, new_slug); 3 = source is live (refused: a mid-write
    copy could tear a round in half)."""
    src = os.path.join(root, src_slug)
    if not os.path.isdir(src):
        emit("--fork: no project '%s' under %s" % (src_slug, root))
        return 2, None
    if _app_lock_has_live_owner(src_slug):
        emit("--fork: '%s' is running — stop it first (a fork of a mid-write "
             "dir could tear a round in half)." % src_slug)
        return 3, None
    base = new_slug or (src_slug + "-fork")
    name, n = base, 2
    while os.path.exists(os.path.join(root, name)):
        name = "%s-%d" % (base, n)
        n += 1
    dst = os.path.join(root, name)

    def _ignore(_dirpath, entries):
        return {e for e in entries
                if e in (".agent_cwd", "approvals", "human_inbox.txt", ".step_in")
                or e.endswith(".lock")}

    try:
        shutil.copytree(src, dst, ignore=_ignore)
    except OSError as exc:
        emit("--fork: copy failed: %s" % exc)
        return 2, None
    if os.path.exists(state_path(dst)):
        # Direct write, NOT save_state: save_state stamps runner_pid with the
        # CURRENT process (runtime bookkeeping for a live engine) — a fork has
        # no runner, and a dead-looking pid here would trip staleness probes.
        st = load_state(dst)
        st["runner_pid"] = None
        st["next_agent"] = None
        st["awaiting_approval"] = None
        st["awaiting_human"] = None
        st["error"] = None
        st["blocked_conflict"] = None
        tmp = state_path(dst) + ".fork.tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(st, fh, indent=2)
            os.replace(tmp, state_path(dst))
        except OSError as exc:
            emit("--fork: state sanitize failed: %s" % exc)
            return 2, None
    emit("--fork: '%s' forked to '%s' (transcripts/state copied; agent "
         "threads, locks, approvals and inbox deliberately NOT)." % (src_slug, name))
    print("FORKED: %s" % name)
    return 0, name


# 'Let them discuss' (V3 board 1.8): source chat workflow -> default debate
# workflow. Data, and only a DEFAULT — --to overrides.
_CHAT_PROMOTE_TARGETS = {"chat_ideas": "brainstorm", "chat_research": "research"}
# Bound on the transcript carried into state: recent-first tail-keep. The
# per-entry context budget (runtime.max_prior_output_chars) applies its own
# tail-keep at render time anyway — for a chat, the RECENT exchange is the
# right bias — so this only bounds agent_state.json growth, honestly labeled.
_PROMOTE_TRANSCRIPT_CAP = 60000


def promote_chat(root, slug, to_workflow=None, wait_seconds=600):
    """'Let them discuss' (V3 board 1.8): promote a conversational chat
    session into an auto debate workflow in the SAME dir.

    The seam is carryover_outputs, NOT phase_outputs/completed_phases: run()'s
    prior-output assembly only reads keys of the CURRENT workflow's phase
    list, and the conversational finalizer deliberately stores a one-line
    closure note as phase_outputs — so the transcript is read from the phase
    .md itself (the source of truth) and carried under a key no workflow
    declares ('chat_transcript'), which the carryover loop always injects.

    A LIVE session is ended via the 1.1 contract (approvals/<key>.ok) and the
    promotion then WAITS for the engine lock's owner to actually die — the
    .ok isn't consumed until the current round's replies finish, so 'bounded'
    genuinely means minutes; state surgery against a live engine would be
    clobbered by its later save_state calls.

    Returns (exit_code, target_app); target_app None means exit exit_code.
    Exit 3 = still running after the wait (try again)."""
    app_dir = os.path.join(root, slug)
    if not os.path.isdir(app_dir):
        emit("--promote: no project '%s' under %s" % (slug, root))
        return 2, None
    if not os.path.exists(state_path(app_dir)):
        emit("--promote: '%s' has never run — there is no chat to promote." % slug)
        return 2, None
    cur_wf = wflib.resolve_workflow_for_app(app_dir, orch_dir=HERE)
    conv_phases = [p for p in cur_wf.phases if p.get("conversational", False)]
    if not conv_phases:
        st = load_state(app_dir)
        if st.get("promoted_from_chat"):
            emit("--promote: '%s' was already promoted (from %s) — nothing to do."
                 % (slug, st["promoted_from_chat"].get("workflow")))
            return 0, None
        emit("--promote: '%s' runs workflow '%s', which has no conversational "
             "phase — only chats can be promoted." % (slug, cur_wf.name))
        return 2, None
    phasedef = conv_phases[0]
    phase_key = phasedef.key
    target = to_workflow or _CHAT_PROMOTE_TARGETS.get(cur_wf.name, "brainstorm")
    available = wflib.list_workflows(HERE)
    if target not in available:
        emit("--promote: unknown target workflow '%s' (available: %s)"
             % (target, ", ".join(available)))
        return 2, None
    target_wf = wflib.load_workflow(target, HERE)
    if any(p.get("conversational", False) for p in target_wf.phases):
        emit("--promote: target '%s' is itself conversational — promotion "
             "means handing the chat to an AUTO debate." % target)
        return 2, None

    if _app_lock_has_live_owner(slug):
        end_file = os.path.join(app_dir, "approvals", "%s.ok" % phase_key)
        os.makedirs(os.path.dirname(end_file), exist_ok=True)
        try:
            open(end_file, "w", encoding="utf-8").close()
        except OSError as exc:
            emit("--promote: could not write the end command: %s" % exc)
            return 2, None
        emit("--promote: '%s' is live — chat end requested; waiting up to %ds "
             "for the current round to finish…" % (slug, int(wait_seconds)))
        deadline = time.time() + wait_seconds
        while _app_lock_has_live_owner(slug) and time.time() < deadline:
            time.sleep(1.0)
        if _app_lock_has_live_owner(slug):
            emit("--promote: '%s' is still running after %ds — the end command "
                 "stays queued; run --promote again once the chat stops."
                 % (slug, int(wait_seconds)))
            return 3, None

    st = load_state(app_dir)
    md_path = os.path.join(app_dir, phasedef.folder, phasedef.file)
    transcript = ""
    try:
        with open(md_path, encoding="utf-8") as fh:
            transcript = fh.read().strip()
    except OSError:
        pass
    kept_note = ""
    if len(transcript) > _PROMOTE_TRANSCRIPT_CAP:
        transcript = ("[earlier chat trimmed — the most recent %d characters "
                      "follow]\n" % _PROMOTE_TRANSCRIPT_CAP) \
            + transcript[-_PROMOTE_TRANSCRIPT_CAP:]
        kept_note = " (tail-kept at %d chars)" % _PROMOTE_TRANSCRIPT_CAP
    if transcript:
        carry = dict(st.get("carryover_outputs") or {})
        carry["chat_transcript"] = transcript
        st["carryover_outputs"] = carry
    st["promoted_from_chat"] = {"workflow": cur_wf.name, "phase": phase_key,
                                "at": time.strftime("%Y-%m-%d %H:%M:%S")}
    st["done"] = False
    st["error"] = None
    st["blocked_conflict"] = None
    st["awaiting_approval"] = None
    st["awaiting_human"] = None
    st["workflow"] = target
    save_state(app_dir, st)
    try:
        with open(os.path.join(app_dir, "workflow.txt"), "w", encoding="utf-8") as fh:
            fh.write(target + "\n")
    except OSError as exc:
        emit("--promote: could not write workflow.txt for '%s': %s" % (slug, exc))
        return 2, None
    # A leftover end command must not linger: in the promoted project the
    # same file name is a checkpoint-approval decision.
    try:
        os.remove(os.path.join(app_dir, "approvals", "%s.ok" % phase_key))
    except OSError:
        pass
    emit("--promote: '%s' promoted %s -> %s; %d transcript char(s) carried as "
         "context%s. The agents take it from here."
         % (slug, cur_wf.name, target, len(transcript), kept_note))
    return 0, slug


def run_once(cfg):
    root = cfg["root"]
    if not os.path.isdir(root):
        emit("Root does not exist: %s" % root)
        return
    apps = find_apps(root)
    if not apps:
        emit("No apps found under %s (expected <app>/initial_prompt/initial_prompt.md)." % root)
        return
    workers = _project_parallel_workers(cfg, len(apps))
    if workers <= 1 or len(apps) <= 1:
        for app in apps:
            process_app(dict(cfg), root, app)
        return
    emit("Project parallelism: %d app(s), %d worker(s)." % (len(apps), workers))
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(process_app, dict(cfg), root, app): app for app in apps}
        for fut in concurrent.futures.as_completed(futs):
            app = futs[fut]
            try:
                fut.result()
            except Exception as exc:
                emit("App '%s': unexpected worker failure: %s" % (app, exc))


def main():
    os.makedirs(LOG_DIR, exist_ok=True)
    ap = argparse.ArgumentParser(description="Autonomous multi-agent orchestrator")
    ap.add_argument("--once", action="store_true", help="single scan/process pass (default)")
    ap.add_argument("--watch", type=int, metavar="SECONDS",
                    help="loop forever, sleeping SECONDS between passes")
    ap.add_argument("--app", metavar="NAME", help="process only this app")
    ap.add_argument("--project", metavar="SLUG",
                    help="process only this project (V2 spec §27 alias for --app)")
    ap.add_argument("--root", metavar="PATH",
                    help="workspace root, overriding config.yaml's root (V2 spec §27)")
    ap.add_argument("--resume", metavar="SLUG",
                    help="resume an existing project from its saved state, clearing "
                         "any recorded abort error first (V2 spec §27)")
    ap.add_argument("--new-session", metavar="ID",
                    help="mint a session dir (flat name or nested "
                         "project/section/chat); reads the prompt from stdin; "
                         "prints CREATED: <id>")
    ap.add_argument("--session-workflow", metavar="NAME",
                    help="workflow.txt content for --new-session")
    ap.add_argument("--new-section", metavar="NAME",
                    help="scaffold sections/<NAME>/ from sections/_template/")
    ap.add_argument("--lint-section", metavar="NAME",
                    help="lint a section dir; exit 0 clean / 1 warnings / "
                         "2 errors (would banner-fallback at runtime)")
    ap.add_argument("--lint-json", action="store_true",
                    help="emit the --lint-section report as JSON")
    ap.add_argument("--migrate-layout", action="store_true",
                    help="flat->nested chat-dir migration; DRY-RUN unless "
                         "--apply is also given")
    ap.add_argument("--migrate-apply", action="store_true",
                    help="execute the --migrate-layout plan (default: dry-run)")
    ap.add_argument("--fork", metavar="SLUG",
                    help="fork a session: copy the dir minus agent threads, "
                         "locks, approvals and inbox; prints FORKED: <name>")
    ap.add_argument("--promote", metavar="SLUG",
                    help="'Let them discuss': promote a conversational chat "
                         "session to an auto debate workflow in the same dir "
                         "(default target per chat workflow; see --to)")
    ap.add_argument("--to", dest="promote_to", metavar="WORKFLOW",
                    help="target workflow for --promote (default: "
                         "chat_ideas->brainstorm, chat_research->research)")
    ap.add_argument("--continue-with", dest="continue_with", metavar="WORKFLOW",
                    help="with --app/--project: re-open the project under this "
                         "workflow, carrying prior phase outputs forward as "
                         "context (staged continuation, e.g. research now, "
                         "full build later)")
    ap.add_argument("--route-artifact", dest="route_artifact", metavar="ID",
                    help="PUSH an admissible bus artifact into a target "
                         "session's carryover context (V3 4.5); needs "
                         "--route-from and --route-to")
    ap.add_argument("--route-from", dest="route_from", metavar="SID",
                    help="source session/project id whose artifacts/ holds "
                         "the --route-artifact")
    ap.add_argument("--route-to", dest="route_to", metavar="SID",
                    help="target session id to route into (created if absent)")
    ap.add_argument("--finalize-artifact", dest="finalize_artifact",
                    metavar="ID",
                    help="transition a pending_review/draft bus artifact to "
                         "'final' (V3 4.8); needs --finalize-in")
    ap.add_argument("--finalize-in", dest="finalize_in", metavar="SID",
                    help="project or session id whose artifacts/ holds the "
                         "--finalize-artifact")
    ap.add_argument("--by", dest="finalize_by", metavar="NAME", default="cli",
                    help="finalizer identity recorded in status_history")
    ap.add_argument("--by-human", dest="by_human", action="store_true",
                    help="mark the finalize as human-authored (required to "
                         "finalize a requires_human type)")
    ap.add_argument("--doctor", action="store_true", help="print environment report and exit")
    ap.add_argument("--mistakes", action="store_true",
                    help="print the cross-run mistakes-ledger report (per-class/"
                         "per-phase/per-agent counts + verification rollup per "
                         "app) and exit; combine with --app and/or --json")
    ap.add_argument("--postmortem", action="store_true",
                    help="print one correlated failure report for a project "
                         "(run status, phases, event failure chain, verify "
                         "attempts, mistakes ledger, turn telemetry) and exit; "
                         "requires --app/--project, supports --json")
    ap.add_argument("--json", action="store_true",
                    help="with --doctor/--mistakes/--postmortem: emit a "
                         "machine-readable JSON report (V2 spec §27)")
    ap.add_argument("--seed", action="store_true",
                    help="seed built-in workflow JSON files and exit (used by the GUI)")
    ap.add_argument("--fleet-report", action="store_true",
                    help="print the fleet-learning report (label confirmation "
                         "queue + per-phase scorecards) and rebuild the "
                         "anti-pattern ledger, then exit")
    ap.add_argument("--eval-report", nargs="*", metavar="SLUG", default=None,
                    help="print the eval-harness scorecard table over the "
                         "given project slugs (default: all projects) and exit")
    ap.add_argument("--save-exemplar", metavar="SLUG",
                    help="export a rated-good project's phase outputs into "
                         "knowledge/exemplars/ for few-shot splicing, then exit")
    ap.add_argument("--distill-doc", metavar="PATH_OR_URL",
                    help="distill a design/engineering document into a dense "
                         "knowledge/ cheatsheet via the claude CLI, then exit")
    ap.add_argument("--domain", metavar="NAME", default="ios",
                    help="with --distill-doc: knowledge domain folder "
                         "(default ios)")
    ap.add_argument("--search-models", metavar="QUERY",
                    help="search open-source local models (curated registry + "
                         "Hugging Face GGUF, pullable via ollama) and exit; "
                         "combine with --json for machine-readable output")
    args = ap.parse_args()

    # --json means stdout must be ONLY the JSON blob (any consumer — CI, the
    # GUI's onboarding flow — parses stdout directly). Silence emit()'s
    # terminal printing up front, before anything below (workflow seeding,
    # resolve_models' gemini probe, etc.) can print a log line ahead of it.
    # emit() still writes to orchestrator.log either way.
    if args.json or args.lint_json:
        global _QUIET
        _QUIET = True

    # Always materialize built-in workflows to workflows/*.json (never clobbers an
    # existing file), so the engine and the GUI both have editable definitions.
    wflib.ensure_seeded(HERE)
    # Same discipline for section manifests (V3 3.8: the GUI's section rail
    # "seed defaults" action just runs any engine command).
    seclib.ensure_seeded_sections(HERE)

    if args.seed:
        emit("Seeded workflows: %s" % ", ".join(wflib.list_workflows(HERE)))
        return 0

    if args.search_models:
        res = lmlib.search_remote(args.search_models, here=HERE)
        if args.json:
            print(json.dumps(res, indent=2))
        else:
            for m in res["results"]:
                print("%-52s %-12s %-16s %s"
                      % (m["id"][:52], m["source"], (m.get("license") or "?")[:16],
                         "installed" if m.get("installed") else ""))
            if res.get("note"):
                print("note: %s" % res["note"])
        return 0

    cfg = load_config()
    resolve_models(cfg)
    # runtime.stream_terminal_output=false: engine lines go only to the log file
    # (the GUI's live run log needs the default true). `_QUIET` was already
    # declared global above (for the --json case); Python forbids repeating a
    # `global` statement for a name once it's been assigned earlier in the
    # function, so this just assigns.
    if not bool(cget(cfg, "runtime.stream_terminal_output", True)):
        _QUIET = True
    # Log hygiene: prune old per-call logs + rotate orchestrator.log at ~5 MB.
    prune_logs(retention_days=cget(cfg, "runtime.log_retention_days", 14))
    # V2 spec §27: --root overrides config.yaml's workspace root (relative config
    # roots resolve against this repo); --project is an alias for --app.
    cfg["root"] = resolve_root(cfg, args.root)
    if cfg["root"]:
        _ensure_workspace_gitignore(cfg["root"])
    # Per-app locks live IN THE WORKSPACE, not next to the engine: the repo
    # checkout and the GUI's Application Support copy of the engine both target
    # the same root, and engine-local locks let them run one app twice.
    if cfg["root"]:
        global LOCKS_DIR
        LOCKS_DIR = os.path.join(cfg["root"], ".orch-locks")
    target_app = args.app or args.project
    tcxlib.TurnContext(cfg).explicit_app = bool(target_app or args.resume)
    # An app/project name is a single folder under the root — refuse anything
    # that would traverse out of it when joined (orchestrator.py never treats
    # these as paths, only as folder names).
    for _slug in (args.app, args.project, args.resume,
                  args.fork, args.promote, args.route_from, args.route_to):
        if _slug and parse_session_id(_slug) is None:
            ap.error("invalid project name %r — use a single folder name under "
                     "the workspace root, or a nested session id "
                     "project/section/chat (no '..', hidden or empty segments)"
                     % _slug)

    if args.continue_with:
        # Validate the combination before any slug is processed.
        if args.resume:
            ap.error("--continue-with conflicts with --resume; resume finishes "
                     "the CURRENT workflow, continue-with switches to a new one")
        if not target_app:
            ap.error("--continue-with requires --app/--project SLUG")

    if args.resume:
        if target_app and target_app != args.resume:
            ap.error("--resume conflicts with --app/--project; pass only one slug")
        rc, target_app = prepare_resume(cfg["root"], args.resume)
        if target_app is None:
            return rc

    if args.postmortem:
        if not target_app:
            ap.error("--postmortem requires --app/--project <name>")
        # Opt-in cost estimation (§ config.yaml cost.pricing): absent/empty by
        # default, so build_postmortem's pricing=None keeps output identical
        # to before this feature existed.
        _pricing = cget(cfg, "cost.pricing", None)
        rep = pmlib.build_postmortem(os.path.join(cfg["root"], target_app),
                                     app=target_app,
                                     pricing=_pricing if isinstance(_pricing, dict) else None)
        if args.json:
            # Same contract as --doctor --json: stdout is ONLY the JSON blob
            # (_QUIET was set above so no emit() line can precede it).
            print(json.dumps(rep, indent=2))
        else:
            print(pmlib.render_postmortem(rep))
        return 0

    if args.mistakes:
        rep = mistakes_report(cfg["root"], app=target_app)
        if args.json:
            # Same contract as --doctor --json: stdout is ONLY the JSON blob
            # (_QUIET was set above so no emit() line can precede it).
            print(json.dumps(rep, indent=2))
        else:
            print_mistakes_report(rep)
        return 0

    if args.new_section:
        # Thin hook by design (3.7): all validation lives in sections.py.
        try:
            dest = seclib.scaffold_section(args.new_section, HERE)
        except ValueError as exc:
            emit("--new-section: %s" % exc)
            return 2
        print("SCAFFOLDED: %s" % dest)
        return 0

    if args.lint_section:
        report = seclib.lint_section(args.lint_section, HERE)
        if args.lint_json:
            print(json.dumps({"section": args.lint_section,
                              "report": report}))
        else:
            for e in report:
                print("%s %s %s: %s" % (e["severity"].upper(), e["file"],
                                        e["field"], e["message"]))
            if not report:
                print("clean: sections/%s/" % args.lint_section)
        return seclib.lint_exit_code(report)

    if args.new_session:
        try:
            create_session(cfg["root"], args.new_session,
                           sys.stdin.read().strip() or "(no prompt)",
                           workflow=args.session_workflow)
        except AppError as exc:
            emit("--new-session: %s" % exc)
            return 2
        print("CREATED: %s" % parse_session_id(args.new_session))
        return 0

    if args.migrate_layout:
        migrate_layout(cfg["root"], apply=args.migrate_apply)
        return 0

    if args.route_artifact:
        return _do_route_push(cfg, args)

    if args.finalize_artifact:
        return _do_finalize(cfg, args)

    if args.fork:
        rc, _forked = fork_session(cfg["root"], args.fork)
        return rc

    if args.promote:
        rc, promoted = promote_chat(cfg["root"], args.promote,
                                    to_workflow=args.promote_to)
        if promoted is None:
            return rc
        target_app = promoted

    if args.continue_with:
        rc, target_app = prepare_continue(cfg["root"], target_app,
                                          args.continue_with)
        if target_app is None:
            return rc

    if args.fleet_report:
        path, clusters = fllib.build_ledger(
            cfg["root"], HERE,
            model=str(cget(cfg, "models.ollama", "") or ""))
        if path:
            emit("Anti-pattern ledger rebuilt: %s (%d cluster(s))."
                 % (path, clusters))
        print(fllib.render_fleet_report(cfg["root"], HERE))
        return 0

    if args.eval_report is not None:
        print(evallib.report(cfg["root"], args.eval_report or None))
        return 0

    if args.save_exemplar:
        if not valid_app_slug(args.save_exemplar):
            ap.error("invalid project name %r" % args.save_exemplar)
        written = fllib.export_exemplars(
            os.path.join(cfg["root"], args.save_exemplar), HERE,
            args.save_exemplar)
        emit("Exported %d exemplar file(s)%s."
             % (len(written), ": " + ", ".join(
                 os.path.relpath(w, HERE) for w in written) if written else
                " (no substantial phase outputs found)"))
        return 0

    if args.distill_doc:
        src = args.distill_doc
        slug = re.sub(r"[^a-z0-9-]+", "-",
                      os.path.basename(src.rstrip("/")).lower())[:60] \
            .strip("-") or "doc"
        emit("Distilling %s into knowledge/%s/ via claude…" % (src, args.domain))
        ask = ("Read the document at %s (a file path or URL — open/fetch it "
               "yourself). Distill it into a dense, actionable cheatsheet for "
               "agents building iOS apps: concrete rules, exact values, "
               "do/don'ts, checklists. No prose padding, no attribution "
               "boilerplate. Maximum ~120 lines of markdown. Output ONLY the "
               "cheatsheet." % src)
        out, err, code, _cmd = run_claude(cfg, ask, 900)
        if code != 0 or not (out or "").strip():
            emit("Distill failed: %s" % (err or "no output").strip()[:200])
            return 1
        dest_dir = os.path.join(HERE, "knowledge", args.domain)
        os.makedirs(dest_dir, exist_ok=True)
        dest = os.path.join(dest_dir, "distilled-%s.md" % slug)
        with open(dest, "w", encoding="utf-8") as fh:
            fh.write("<!-- distilled from %s on %s -->\n\n%s\n"
                     % (src, time.strftime("%Y-%m-%d"), out.strip()))
        emit("Wrote %s." % os.path.relpath(dest, HERE))
        return 0

    if args.doctor:
        if args.json:
            print(json.dumps(preflight_report(cfg), indent=2))
        else:
            doctor(cfg)
        return 0

    # Locking is per-app now (see process_app), so different apps run
    # concurrently. Clean up any locks this process holds on signal.
    def _cleanup(*_a):
        # Stop worker threads from launching NEW agent calls (call_agent checks
        # this) — otherwise they treat their killed CLI as a skippable failure
        # and keep starting rounds while we block in executor shutdown.
        _SHUTDOWN.set()
        # Agent CLIs run in their own sessions (procutil), so without this they
        # would outlive a stopped orchestrator and keep writing to the workspace.
        procutil.kill_live_groups()
        for _p in list(_HELD_LOCKS):
            _remove_lock_if_owned(_p)
        sys.exit(0)
    signal.signal(signal.SIGINT, _cleanup)
    signal.signal(signal.SIGTERM, _cleanup)

    try:
        emit("Orchestrator starting. Root=%s" % cfg["root"])
        doctor(cfg)
        if args.watch:
            emit("Watch mode: every %ds. Ctrl-C to stop." % args.watch)
            while True:
                if target_app:
                    process_app(cfg, cfg["root"], target_app)
                else:
                    run_once(cfg)
                time.sleep(args.watch)
        else:
            if target_app:
                process_app(cfg, cfg["root"], target_app)
            else:
                run_once(cfg)
        emit("Orchestrator pass complete.")
    finally:
        for _p in list(_HELD_LOCKS):
            _remove_lock_if_owned(_p)
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
