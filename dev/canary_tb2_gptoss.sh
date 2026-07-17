#!/bin/bash
# CANARY — 8-task TB2 subset vs gpt-oss-120b, for fast before/after comparison of
# scaffold changes (here: prompt Observation framing for run_session + fresh-tail
# de-dup, commit 3644df6). Same setup as dev/tb2_harbor_gptoss.sh (resident-seq
# cache + warm flows) but scoped to the 8 tasks via `-i`, so turn-count / prefill
# move directly against the prior canary (runs/canary-tb2/2026-06-19__10-50-20:
# 456 plan_interaction turns over the 5 traced tasks).
#
# The framing change is agent-side — Harbor fresh-imports adapters.tb each run, so no
# server restart is needed to pick it up; the server only needs the prefill/decode
# telemetry (already live). Resident + warm are reset here to match the prior run.
#
# Usage: nohup bash dev/canary_tb2_gptoss.sh > /tmp/canary_tb2_v2.out 2>&1 &
set -u
ROOT=/Users/lah-rb/Repos/ouroboros
LLMVP=$ROOT/llmvp
MODEL=gpt-oss-120b-a5
# Run id is the first arg (default canary-tb2-v2) so successive batches don't
# clobber each other's results — each is preserved under runs/<RUNID> for
# before/after comparison.
RUNID=${1:-canary-tb2-v2}
LOG=/tmp/${RUNID//-/_}.log
SRVLOG=$LLMVP/logs/llmvp_server.log
OUT=$ROOT/runs/$RUNID
cd "$ROOT"; : > "$LOG"
export PATH="$HOME/.local/bin:$PATH"
export PYTHONPATH="$ROOT"
export OURO_TRACE=1
export OURO_LLMVP=http://localhost:8008/graphql
export OURO_TIMEOUT_MULTIPLIER=1.0

# The 8 canary tasks (same subset as the prior canary).
TASKS=(chess-best-move compile-compcert mteb-retrieve multi-source-data-merger \
       path-tracing portfolio-optimization regex-chess winning-avg-corewars)

log(){ echo "[$(date +%H:%M:%S)] $*" | tee -a "$LOG"; }
health(){ curl -s -m5 -X POST http://localhost:8008/graphql -H 'Content-Type: application/json' \
  -d '{"query":"{ health { availableInstances } }"}' 2>/dev/null \
  | python3 -c "import json,sys;print(json.load(sys.stdin)['data']['health']['availableInstances'])" 2>/dev/null; }
restart_server(){
  (cd "$LLMVP" && uv run llmvp.py --stop) >>"$LOG" 2>&1 || true; sleep 5
  (cd "$LLMVP" && uv run llmvp.py --backend) >>"$LOG" 2>&1 || true
  for i in $(seq 1 150); do [ "$(health)" = "1" ] && { log "  server ready (~$((i*3))s)"; return 0; }; sleep 3; done
  log "  SERVER LOAD TIMEOUT"; return 1
}

log "==== CANARY v2 (8-task TB2 subset): $MODEL  — framing + fresh-tail de-dup ===="

ROS=$(grep -c '"UseVirtualizationFrameworkRosetta": true' \
  "$HOME/Library/Group Containers/group.com.docker/settings-store.json" 2>/dev/null)
[ "${ROS:-0}" = "1" ] && log "  docker: Rosetta ON" || log "  WARNING: docker Rosetta OFF — amd64 verifiers may segfault"

git checkout "llmvp/configs/$MODEL.yaml" >>"$LOG" 2>&1
.venv/bin/python dev/ab_set_resident.py true >>"$LOG" 2>&1
printf '%s' "$MODEL" > "$LLMVP/active_config.txt"
restart_server || { log "ABORT: server load failed"; exit 1; }
grep -m1 "Resident-seq cache ACTIVE\|resident_seq_cache requested but" "$SRVLOG" | tail -1 | sed 's/^/  /' | tee -a "$LOG"

mark=$(wc -l < "$SRVLOG")
log "  warming flows (pre-warm)…"
.venv/bin/python dev/warm_flows.py 2>&1 | tail -1 | sed 's/^/  /' | tee -a "$LOG"
c3=$(tail -n +$((mark+1)) "$SRVLOG" | grep -ci "code -3"); log "  warm code-3 errors: ${c3:-0}"

# Build the -i include flags.
INC=(); for t in "${TASKS[@]}"; do INC+=(-i "$t"); done
log "  running ${#TASKS[@]}-task canary -> $RUNID (n-concurrent 1; per-task caps from task.toml)"
rm -rf "$OUT" "/tmp/$RUNID.log" 2>/dev/null
.venv/bin/harbor run -d terminal-bench@2.0 \
  --agent-import-path adapters.tb.harbor_agent:OuroborosHarborAgent \
  -m "openai/$MODEL" -n 1 "${INC[@]}" -o "$OUT" > "/tmp/$RUNID.log" 2>&1
log "  harbor run finished"

f=$(find "$OUT" -name 'result.json' -maxdepth 2 2>/dev/null | head -1)
[ -n "$f" ] && .venv/bin/python -c "
import json
r=json.load(open('$f'))
evals=r.get('stats',{}).get('evals',{})
for name,e in evals.items():
    rs=(e.get('reward_stats') or {}).get('reward') or {}
    total=sum(len(v) for v in rs.values())
    passed=sum(len(v) for k,v in rs.items() if float(k)>=1.0)
    print('  CANARY v2: %d/%d passed   errors(non-graded): %d'%(passed,total,e.get('n_errors',0)))
    fails=sorted(t for k,v in rs.items() if float(k)<1.0 for t in v)
    if fails: print('  failed:', ', '.join(fails[:40]))
" 2>&1 | tee -a "$LOG"

log "  restoring prod config"
git checkout "llmvp/configs/$MODEL.yaml" >>"$LOG" 2>&1
restart_server && log "  prod restored"
log "==== CANARY v2 complete -> $OUT ===="
