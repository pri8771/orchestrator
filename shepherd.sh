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
locked(){ [ -f "$ROOT/.orch-locks/$1.lock" ]; }
launch(){ log "launching $1"; nohup bash run.sh --app "$1" >> "$1-run.log" 2>&1 & disown; }
retry_ok(){ local f="$ROOT/$1/.shepherd_retries" n=0; [ -f "$f" ] && n=$(cat "$f"); if [ "$n" -ge 3 ]; then return 1; fi; echo $((n+1)) > "$f"; log "$1 aborted earlier — retry $((n+1))/3"; }

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
    [ -f "$d/.orchestrator_autorun_disabled" ] && continue
    locked "$a" && continue
    is_done "$a" && continue
    if has_error "$a"; then retry_ok "$a" || continue; fi
    launch "$a"; builds_running=$((builds_running+1)); sleep 3
  done
  IFS=$_oldifs

  # Parents next, in program order. Guard the array expansion: with no parents
  # configured, "${PARENTS[@]}" under set -u errors on bash 3.2.
  if [ "${#PARENTS[@]}" -gt 0 ]; then
    for p in "${PARENTS[@]}"; do
      [ "$parents_running" -ge "$MAX_PARENTS" ] && break
      [ -d "$ROOT/$p" ] || continue
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
      [ -f "$d/.orchestrator_autorun_disabled" ] && continue
      # A hollow build (done but a queued repair) still counts as pending, so
      # the shepherd can't declare completion before the repair actually runs.
      if ! is_done "$a"; then pending=$((pending+1))
      elif [ -f "$d/.repair_pending" ]; then pending=$((pending+1)); fi
    done
    if [ "$pending" = "0" ]; then log "ALL APPS COMPLETE"; break; fi
    log "parents done; $pending child app(s) still pending"
  fi
  sleep 120
done
