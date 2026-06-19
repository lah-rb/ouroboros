#!/bin/bash
# Terminal-Bench 2.0 (Harbor, 89 tasks) vs gpt-oss-120b, RESIDENT seq cache +
# pre-warmed flow cache. The 1:1 yardstick: gpt-oss-120b has a published verified
# TB2 score of 18.7% under the neutral Terminus-2 agent — this measures OUROBOROS
# (our scaffold) on the same model + same tasks. Mirrors dev/tb_resident_gptoss.sh
# (the legacy tb harness) but drives Harbor with tb_adapter.harbor_agent.
#
# Requires: Docker on Apple Virtualization Framework + Rosetta (the alexgshaw/*
# images are linux/amd64; QEMU segfaults the pytest/selenium verifiers — Rosetta
# runs them clean). harbor + ouroboros share .venv; PYTHONPATH lets Harbor's
# multiprocess workers import tb_adapter/agent.
#
# Usage: nohup bash dev/tb2_harbor_gptoss.sh > /tmp/tb2_harbor.out 2>&1 &
set -u
ROOT=/Users/lah-rb/Repos/ouroboros
LLMVP=$ROOT/llmvp
LOG=/tmp/tb2_harbor_gptoss.log
SRVLOG=$LLMVP/logs/llmvp_server.log
MODEL=gpt-oss-120b-a5
RUNID=tb2-harbor-gptoss
OUT=$ROOT/runs/$RUNID
cd "$ROOT"; : > "$LOG"
export PATH="$HOME/.local/bin:$PATH"
export PYTHONPATH="$ROOT"
export OURO_TRACE=1
export OURO_LLMVP=http://localhost:8008/graphql
# Per-task self-cap reads each task.toml [agent].timeout_sec × this × 0.9 so the
# mission parks before Harbor's wait_for cancels run(). 1.0 = Harbor's own budget.
export OURO_TIMEOUT_MULTIPLIER=1.0

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

log "==== TB2 (Harbor, 89 tasks): $MODEL  (published gpt-oss verified = 18.7%) ===="

# Docker backend sanity: Rosetta must be on, else amd64 verifiers segfault.
ROS=$(grep -c '"UseVirtualizationFrameworkRosetta": true' \
  "$HOME/Library/Group Containers/group.com.docker/settings-store.json" 2>/dev/null)
[ "${ROS:-0}" = "1" ] && log "  docker: Rosetta ON" || log "  WARNING: docker Rosetta OFF — amd64 verifiers may segfault"

# Resident-seq cache + warm flows (the validated fast-session config).
git checkout "llmvp/configs/$MODEL.yaml" >>"$LOG" 2>&1
.venv/bin/python dev/ab_set_resident.py true >>"$LOG" 2>&1
printf '%s' "$MODEL" > "$LLMVP/active_config.txt"
restart_server || { log "ABORT: server load failed"; exit 1; }
grep -m1 "Resident-seq cache ACTIVE\|resident_seq_cache requested but" "$SRVLOG" | tail -1 | sed 's/^/  /' | tee -a "$LOG"

mark=$(wc -l < "$SRVLOG")
log "  warming flows (pre-warm)…"
.venv/bin/python dev/warm_flows.py 2>&1 | tail -1 | sed 's/^/  /' | tee -a "$LOG"
c3=$(tail -n +$((mark+1)) "$SRVLOG" | grep -ci "code -3"); log "  warm code-3 errors: ${c3:-0}"

log "  running TB2 89-set -> $RUNID (n-concurrent 1; per-task caps from task.toml)"
rm -rf "$OUT" "/tmp/$RUNID.log" 2>/dev/null
.venv/bin/harbor run -d terminal-bench@2.0 \
  --agent-import-path tb_adapter.harbor_agent:OuroborosHarborAgent \
  -m "openai/$MODEL" -n 1 -o "$OUT" > "/tmp/$RUNID.log" 2>&1
log "  harbor run finished"

# Score: count reward==1.0 across the eval's reward_stats.
f=$(find "$OUT" -name 'result.json' -maxdepth 2 2>/dev/null | head -1)
[ -n "$f" ] && .venv/bin/python -c "
import json
r=json.load(open('$f'))
evals=r.get('stats',{}).get('evals',{})
for name,e in evals.items():
    rs=(e.get('reward_stats') or {}).get('reward') or {}
    total=sum(len(v) for v in rs.values())
    passed=sum(len(v) for k,v in rs.items() if float(k)>=1.0)
    n_err=e.get('n_errors',0)
    print('  %s'%name)
    print('  OUROBOROS TB2: %d/%d = %.1f%%   (gpt-oss verified Terminus-2 = 18.7%%)'%(passed,total,100*passed/total if total else 0))
    print('  errors(non-graded): %d'%n_err)
    fails=sorted(t for k,v in rs.items() if float(k)<1.0 for t in v)
    if fails: print('  failed:', ', '.join(fails[:40]))
" 2>&1 | tee -a "$LOG"

log "  restoring prod config"
git checkout "llmvp/configs/$MODEL.yaml" >>"$LOG" 2>&1
restart_server && log "  prod restored"
log "==== TB2 Harbor run complete -> $OUT ===="
