#!/bin/bash
# shepherd.sh — drives the 51-app program to completion: keeps MAX_BUILDS child
# builds + MAX_PARENTS portfolio-parent ideations running at all times, resumes
# interrupted runs, retries aborted apps (max 3 each), and exits when every
# parent and child is done. Safe against duplicates: the engine's per-app
# workspace locks make a redundant launch exit with "already running".
# set -u (catch undefined-var typos) + pipefail. Deliberately NOT -e: this
# driver uses non-zero exit codes as control flow (is_done/has_error/retry_ok)
# and targets macOS's bash 3.2, so -e would abort the fleet loop on ordinary
# "not done yet" results. The one failure that must stop the run — not being
# able to cd to the script dir — is handled explicitly below.
set -uo pipefail
cd "$(dirname "$0")" || { echo "shepherd: cannot cd to script dir" >&2; exit 1; }
# Machine-specific config comes from the environment so this isn't pinned to one
# user. ORCH_PARENTS is a space-separated list of portfolio-parent app names.
ROOT="${ORCH_ROOT:-$HOME/Documents/iOS-App-Factory}"
read -ra PARENTS <<< "${ORCH_PARENTS:-}"
MAX_BUILDS="${ORCH_MAX_BUILDS:-3}"; MAX_PARENTS="${ORCH_MAX_PARENTS:-2}"
# GUI queue panel writes $ROOT/.orch-queue-order.json {"order":[...],"lanes":N}.
# lanes overrides MAX_BUILDS; order apps launch first (missing/done ones skip).
queue_lanes(){ python3 -c "import json;v=json.load(open('$ROOT/.orch-queue-order.json')).get('lanes');print(v if isinstance(v,int) and v>0 else '')" 2>/dev/null; }
queue_order_dirs(){ python3 -c "
import json
for a in (json.load(open('$ROOT/.orch-queue-order.json')).get('order') or []):
    if isinstance(a,str) and a and '/' not in a: print('$ROOT/%s/'%a)" 2>/dev/null; }
log(){ echo "[$(date '+%F %T')] $*"; }
is_parent(){ case " ${PARENTS[*]:-} " in *" $1 "*) return 0;; *) return 1;; esac; }
is_done(){ python3 -c "import json;print(1 if json.load(open('$ROOT/$1/agent_state.json')).get('done') else 0)" 2>/dev/null | grep -q 1; }
has_error(){ python3 -c "import json;s=json.load(open('$ROOT/$1/agent_state.json'));print(1 if (s.get('error') and not s.get('done')) else 0)" 2>/dev/null | grep -q 1; }
# Session id -> lock-file stem (V3 board 3.0). Flat ids (no '/') stay RAW, so a
# flat run's lock — and every check against it — is byte-identical to the
# pre-nesting behavior and costs no python. Nested ids ("project/section/chat")
# MUST match orchestrator.encode_lock_name BYTE-FOR-BYTE (percent-encode the NFC
# form + an 8-hex sha256 suffix over the NFC-lowercased id). bash 3.2 (which this
# script targets) cannot do NFC normalization or Unicode-aware lowercasing, so
# re-deriving the encoding here would be a THIRD implementation that could
# silently drift from the engine and GUI; instead delegate to the ONE canonical
# implementation. tests/fixtures/lock_encoding.json pins the sides, and
# `shepherd.sh --lock-name <id>` drives this very function against every case.
lock_stem(){
  case "$1" in
    */*) python3 -c 'import sys, orchestrator; sys.stdout.write(orchestrator.encode_lock_name(sys.argv[1]))' "$1" ;;
    *) printf '%s' "$1" ;;
  esac
}
# Locked = a LIVE process still holds the session's lock. A stale lock (its pid
# is dead — a crashed/killed run that never cleaned up on SIGTERM) must NOT count
# as running: the old `[ -f lock ]` file-existence check made a crashed build
# look forever-running, so shepherd never relaunched it and the fleet looped
# "N child app(s) still pending" indefinitely. This mirrors the engine's own
# _pid_alive reclaim check (acquire_app_lock), which steals the orphaned lock on
# the relaunch this unblocks — so a redundant launch still dedups safely. The
# lock stem is derived via lock_stem so a nested session's real encoded lock is
# consulted, not a raw "project/section/chat.lock" that no live run ever holds.
locked(){
  local f="$ROOT/.orch-locks/$(lock_stem "$1").lock" pid
  [ -f "$f" ] || return 1
  pid=$(grep -oE 'pid=[0-9]+' "$f" 2>/dev/null | head -1 | cut -d= -f2)
  [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null
}
# A user (or the GUI) disables autorun by dropping this marker in the app dir.
# EVERY launch path must honor it — including the repair path: a release-gate
# repair writes a .repair_pending marker, and the repair loop below used to
# launch on that alone, relaunching a disabled app the user had deliberately
# parked (and consuming a build lane). Shared helper so no launch path forgets.
autorun_disabled(){ [ -f "$ROOT/$1/.orchestrator_autorun_disabled" ]; }
# Log basename mirrors the lock stem: flat ids keep the historical "$1-run.log";
# a nested id ("p/s/c") would otherwise try to open a log under directories that
# don't exist relative to the engine dir (and its '/' isn't a legal flat file
# name), so it uses the percent-encoded stem — same string as its lock.
launch(){ log "launching $1"; local logf="$1"; case "$1" in */*) logf=$(lock_stem "$1");; esac; nohup bash run.sh --app "$1" >> "$logf-run.log" 2>&1 & disown; }
retry_ok(){ local f="$ROOT/$1/.shepherd_retries" n=0; [ -f "$f" ] && n=$(cat "$f"); if [ "$n" -ge 3 ]; then return 1; fi; echo $((n+1)) > "$f"; log "$1 aborted earlier — retry $((n+1))/3"; }

# All runnable session ids under ROOT — flat AND nested (V3 board 3.0) — from the
# engine's ONE discovery implementation (find_apps), so shepherd never re-derives
# the .orch-sections recursion rules in bash. One id per line; nested ids contain
# '/'. Any failure (no python3, import error, unreadable root) emits nothing, so
# the fleet degrades to the flat-only "$ROOT"/*/ glob — never a crash.
list_sessions(){
  python3 -c 'import sys, os, orchestrator
r = sys.argv[1]
if os.path.isdir(r):
    for a in orchestrator.find_apps(r):
        print(a)' "$ROOT" 2>/dev/null
}

# Nested chat sessions are human-paced today, so shepherd does NOT auto-relaunch
# them by default (doing so would resurrect a chat a person is mid-conversation
# in, and never let the fleet declare completion). Set ORCH_SHEPHERD_NESTED=1 to
# let the fleet loop manage autonomous nested sections (M7 Conductor) with the
# same guards and MAX_BUILDS lane budget it uses for flat builds.
nested_autorun_enabled(){ [ -n "${ORCH_SHEPHERD_NESTED:-}" ]; }

# Diagnostic/test hooks: report a guard's verdict via exit status WITHOUT
# entering the fleet loop, so the real functions can be exercised in a test.
if [ "${1:-}" = "--check-lock" ]; then locked "${2:-}"; exit $?; fi
if [ "${1:-}" = "--check-disabled" ]; then autorun_disabled "${2:-}"; exit $?; fi
# Print the lock-file stem for an id and exit — the fixture-parity test drives
# this over every case in tests/fixtures/lock_encoding.json.
if [ "${1:-}" = "--lock-name" ]; then lock_stem "${2:-}"; echo; exit 0; fi
# Print discovered runnable session ids (flat + nested), one per line, and exit.
if [ "${1:-}" = "--list-sessions" ]; then list_sessions; exit 0; fi

while true; do
  _l=$(queue_lanes); [ -n "$_l" ] && MAX_BUILDS=$_l
  parents_running=0; builds_running=0
  for L in "$ROOT"/.orch-locks/*.lock; do
    [ -f "$L" ] || continue
    b=$(basename "$L" .lock)
    if is_parent "$b"; then parents_running=$((parents_running+1)); else builds_running=$((builds_running+1)); fi
  done

  # Repairs first: apps that finished but failed compile verification carry a
  # .repair_pending marker (+ iterate workflow with a change request). They are
  # 'done' in state so the normal pending scan skips them — launch once here;
  # the prompt change resets their pipeline into the iterate flow.
  for d in "$ROOT"/*/; do
    [ "$builds_running" -ge "$MAX_BUILDS" ] && break
    a=$(basename "$d")
    [ -f "$d/.repair_pending" ] || continue
    autorun_disabled "$a" && continue   # a parked app stays parked, even for repair
    locked "$a" && continue
    rm -f "$d/.repair_pending"
    launch "$a"; builds_running=$((builds_running+1)); sleep 3
  done

  # Children next — builds are the bottleneck. The GUI queue file
  # (.orch-queue-order.json) sets which apps launch first; everything else
  # follows in directory order. IFS=newline for this loop so a workspace path
  # containing spaces doesn't word-split the command-substitution output (the
  # "$ROOT"/*/ glob is already space-safe; command sub is not).
  _oldifs=$IFS; IFS=$'\n'
  for d in $(queue_order_dirs) "$ROOT"/*/; do
    IFS=$_oldifs
    [ "$builds_running" -ge "$MAX_BUILDS" ] && break
    a=$(basename "$d")
    [ -f "$d/initial_prompt/initial_prompt.md" ] || continue
    is_parent "$a" && continue
    autorun_disabled "$a" && continue
    locked "$a" && continue
    is_done "$a" && continue
    if has_error "$a"; then retry_ok "$a" || continue; fi
    launch "$a"; builds_running=$((builds_running+1)); sleep 3
  done
  IFS=$_oldifs

  # Nested chat sessions (V3 board 3.0) live at
  # <root>/<project>/<section>/<chat> — the flat "$ROOT"/*/ glob above cannot
  # see them. When autonomous nested sections are enabled, enumerate them via
  # the engine's find_apps (the one discovery implementation) and drive each
  # through the SAME per-session guards and the SAME MAX_BUILDS lane budget as
  # flat builds. Off by default (nested chats are human-paced). IFS=newline so a
  # segment containing a space can't word-split the command-substitution output.
  if nested_autorun_enabled; then
    _oldifs=$IFS; IFS=$'\n'
    for a in $(list_sessions); do
      IFS=$_oldifs
      [ "$builds_running" -ge "$MAX_BUILDS" ] && break
      case "$a" in */*) : ;; *) continue ;; esac   # nested ids only; flat done above
      [ -f "$ROOT/$a/initial_prompt/initial_prompt.md" ] || continue
      autorun_disabled "$a" && continue
      locked "$a" && continue
      is_done "$a" && continue
      if has_error "$a"; then retry_ok "$a" || continue; fi
      launch "$a"; builds_running=$((builds_running+1)); sleep 3
    done
    IFS=$_oldifs
  fi

  # Parents next, in program order. Guard the array expansion: with no parents
  # configured, "${PARENTS[@]}" under set -u errors on bash 3.2.
  if [ "${#PARENTS[@]}" -gt 0 ]; then
    for p in "${PARENTS[@]}"; do
      [ "$parents_running" -ge "$MAX_PARENTS" ] && break
      [ -d "$ROOT/$p" ] || continue
      autorun_disabled "$p" && continue   # a parked parent stays parked (like children)
      locked "$p" && continue
      is_done "$p" && continue
      if [ -f "$ROOT/$p/agent_state.json" ] && has_error "$p"; then retry_ok "$p" || continue; fi
      launch "$p"; parents_running=$((parents_running+1)); sleep 3
    done
  fi

  # Auto-queue ONE repair for any finished app whose compile verification
  # failed ('built to completion' means it actually builds). ok=None (no
  # verify record, e.g. web apps) is left alone.
  python3 - "$ROOT" <<'PY'
import json,os,sys
root=sys.argv[1]
REQ_PROJ=("The build produced sources but NO buildable Xcode project. Generate a complete "
          "working project wiring in all existing sources, then make it compile cleanly "
          "for the iOS Simulator.")
REQ_FAIL=("The app currently FAILS to compile. Fix every compiler error until the build "
          "succeeds cleanly; do not drop features unless unavoidable.")
for a in sorted(os.listdir(root)):
    d=os.path.join(root,a)
    if a.startswith(('.','batch-','multi-app-exp')) or not os.path.isdir(d): continue
    st=os.path.join(d,'agent_state.json')
    if not os.path.isfile(st): continue
    try: s=json.load(open(st))
    except Exception: continue
    if not s.get('done'): continue
    if os.path.exists(os.path.join(d,'.repair_attempted')) or os.path.exists(os.path.join(d,'.repair_pending')): continue
    rec={}
    try:
        r=json.load(open(os.path.join(d,'verify_results.json')))
        rec=(r if isinstance(r,list) else r.get('results',[]))[-1] or {}
    except Exception: pass
    if rec.get('ok') is not False: continue
    req=REQ_PROJ if 'no .xcodeproj' in str(rec.get('summary','')) else REQ_FAIL
    p=os.path.join(d,'initial_prompt','initial_prompt.md')
    try: t=open(p).read()
    except Exception: continue
    if 'Change requested' not in t:
        open(p,'a').write('\n\n## Change requested\n%s\n'%req)
    open(os.path.join(d,'workflow.txt'),'w').write('iterate\n')
    open(os.path.join(d,'.repair_pending'),'w').close()
    open(os.path.join(d,'.repair_attempted'),'w').close()
    print('[auto] queued verify repair:',a)
PY

  # Exit when every parent is done and no non-disabled child is unfinished.
  all=1
  if [ "${#PARENTS[@]}" -gt 0 ]; then
    for p in "${PARENTS[@]}"; do is_done "$p" || all=0; done
  fi
  if [ "$all" = "1" ]; then
    pending=0
    for d in "$ROOT"/*/; do
      a=$(basename "$d")
      [ -f "$d/initial_prompt/initial_prompt.md" ] || continue
      is_parent "$a" && continue
      autorun_disabled "$a" && continue
      # A hollow build (done but a queued repair) still counts as pending, so
      # the shepherd can't declare completion before the repair actually runs.
      if ! is_done "$a"; then pending=$((pending+1))
      elif [ -f "$d/.repair_pending" ]; then pending=$((pending+1)); fi
    done
    # Nested sessions keep the fleet alive too, but only when shepherd manages
    # them (repair is flat-only, so a nested session is pending iff not done).
    if nested_autorun_enabled; then
      _oldifs=$IFS; IFS=$'\n'
      for a in $(list_sessions); do
        IFS=$_oldifs
        case "$a" in */*) : ;; *) continue ;; esac
        autorun_disabled "$a" && continue
        is_done "$a" || pending=$((pending+1))
      done
      IFS=$_oldifs
    fi
    if [ "$pending" = "0" ]; then log "ALL APPS COMPLETE"; break; fi
    log "parents done; $pending child app(s) still pending"
  fi
  sleep 120
done
