#!/bin/bash
# tb retest: gpt-oss-120b with RESIDENT seq cache + pre-warmed flow cache, on the
# terminal-bench 8-set. Compares to the legacy 37.5% (3/8) baseline. Hypothesis
# (memory framework-overhead-timeouts): all gpt-oss losses were agent_timeout from
# inference volume; ~2x faster session turns (AB result) should convert timeouts
# into completions. hello-world runs FIRST as a fast pipeline de-risk.
#
# Usage: nohup bash dev/tb_resident_gptoss.sh > /tmp/tb_resident.out 2>&1 &
set -u
ROOT=/Users/lah-rb/Repos/ouroboros
LLMVP=$ROOT/llmvp
LOG=/tmp/tb_resident_gptoss.log
SRVLOG=$LLMVP/logs/llmvp_server.log
MODEL=gpt-oss-120b-a5
RUNID=tb-resident-gptoss
TASKS="-t hello-world -t csv-to-parquet -t grid-pattern-transform -t count-dataset-tokens -t create-bucket -t processing-pipeline -t extract-safely -t fix-permissions"
cd "$ROOT"; : > "$LOG"
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

log "==== tb RESIDENT retest: $MODEL (baseline legacy 3/8 = 37.5%) ===="
git checkout "llmvp/configs/$MODEL.yaml" >>"$LOG" 2>&1
.venv/bin/python dev/ab_set_resident.py true >>"$LOG" 2>&1      # resident_seq_cache: true
printf '%s' "$MODEL" > "$LLMVP/active_config.txt"
restart_server || { log "ABORT: server load failed"; exit 1; }
grep -m1 "Resident-seq cache ACTIVE\|resident_seq_cache requested but" "$SRVLOG" | tail -1 | sed 's/^/  /' | tee -a "$LOG"

mark=$(wc -l < "$SRVLOG")
log "  warming flows (pre-warm)…"
.venv/bin/python dev/warm_flows.py 2>&1 | tail -1 | sed 's/^/  /' | tee -a "$LOG"
c3=$(tail -n +$((mark+1)) "$SRVLOG" | grep -ci "code -3"); log "  warm code-3 errors: ${c3:-0}"

log "  running tb 8-set -> $RUNID"
rm -rf "runs/$RUNID" "/tmp/$RUNID.log" 2>/dev/null
OURO_TRACE=1 .venv/bin/tb run -d terminal-bench-core==0.1.1 \
  --agent-import-path tb_adapter.agent:OuroborosAgent $TASKS \
  --n-concurrent 1 --run-id "$RUNID" --output-path runs > "/tmp/$RUNID.log" 2>&1
acc=$(grep -iE "Accuracy" "/tmp/$RUNID.log" | tail -1)
builds=$(grep -cE "resident flow BUILD|flow_kv_cache BUILD" "$SRVLOG")
hits=$(grep -cE "resident flow HIT|flow_kv_cache HIT" "$SRVLOG")
win=$(grep -c "🪟 windowed" "$SRVLOG")
log "  DONE | $acc | flow BUILD=$builds HIT=$hits | windowing events=$win"

f=$(find "runs/$RUNID" -name results.json -maxdepth 3 2>/dev/null | head -1)
[ -n "$f" ] && .venv/bin/python -c "
import json; r=json.load(open('$f')); rs=r.get('results') or r.get('trials') or []
n=sum(1 for t in rs if t.get('is_resolved'))
print('  RESIDENT 8-set: %d/%d = %.1f%%  (baseline legacy 3/8 = 37.5%%)'%(n,len(rs),100*n/len(rs) if rs else 0))
for t in rs: print('    %-26s %s'%(t.get('task_id') or t.get('instance_id') or '?', 'RESOLVED' if t.get('is_resolved') else 'failed'))
" 2>&1 | sed 's/^/ /' | tee -a "$LOG"

log "  restoring prod config"
git checkout "llmvp/configs/$MODEL.yaml" >>"$LOG" 2>&1
restart_server && log "  prod restored"
log "==== tb RESIDENT retest complete ===="
