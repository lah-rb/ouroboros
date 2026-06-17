#!/bin/bash
# Cross-validation campaign: run the terminal-bench 8-set on 4 models with the
# pre-warm cross-task KV cache. Per model: restart server -> stability check
# (disable flow_kv_cache if it corrupts) -> warm flows -> run 8-set. Robust:
# SIGKILL-fallback on a wedged server, continue past any single-model failure.
set -u
ROOT=/Users/lah-rb/Repos/ouroboros
LLMVP=$ROOT/llmvp
LOG=/tmp/cross_val_campaign.log
SRVLOG=$LLMVP/logs/llmvp_server.log
TASKS="-t csv-to-parquet -t grid-pattern-transform -t count-dataset-tokens -t create-bucket -t processing-pipeline -t extract-safely -t fix-permissions -t hello-world"
# model<TAB>shortname
MODELS=(
  "gpt-oss-120b-a5:gptoss"
  "step37-flash-196b-a11:step37"
  "qwen3.6-35b-a3:qwen36"
  "gemma-4-31b:gemma4"
)
cd "$ROOT"
: > "$LOG"
log(){ echo "[$(date +%H:%M:%S)] $*" | tee -a "$LOG"; }

health_inst(){ curl -s -m 5 -X POST http://localhost:8008/graphql -H 'Content-Type: application/json' \
  -d '{"query":"{ health { availableInstances } }"}' 2>/dev/null \
  | python3 -c "import json,sys;print(json.load(sys.stdin)['data']['health']['availableInstances'])" 2>/dev/null; }

restart_server(){
  local pid; pid=$(pgrep -f "api/main.py" | head -1)
  if [ -n "$pid" ]; then
    kill -TERM "$pid" 2>/dev/null
    for i in $(seq 1 20); do kill -0 "$pid" 2>/dev/null || break; sleep 1; done
    if kill -0 "$pid" 2>/dev/null; then log "  server stuck -> SIGKILL"; kill -9 "$pid" 2>/dev/null; sleep 2; fi
  fi
  lsof -ti:8008 2>/dev/null | xargs -r kill -9 2>/dev/null; sleep 1
  local startline; startline=$(wc -l < "$SRVLOG")
  ( cd "$LLMVP" && nohup .venv/bin/python api/main.py >> logs/llmvp_server.log 2>&1 & )
  for i in $(seq 1 100); do
    [ "$(health_inst)" = "1" ] && { log "  server ready (~$((i*3))s)"; return 0; }
    if tail -n +"$startline" "$SRVLOG" 2>/dev/null | grep -qiE "out of memory|Failed to load|Could not load|insufficient memory"; then
      log "  LOAD ERROR:"; tail -n +"$startline" "$SRVLOG" | grep -iE "out of memory|Failed to load|error" | tail -2 | tee -a "$LOG"; return 1
    fi
    sleep 3
  done
  log "  SERVER LOAD TIMEOUT"; return 1
}

stability_c3(){
  # 10 BUILD/HIT/plain cycles; print the number of new code-3 errors
  local mark; mark=$(wc -l < "$SRVLOG")
  .venv/bin/python - <<'PYEOF' 2>/dev/null
import httpx
SP="fixture "+("alpha beta gamma delta epsilon zeta eta theta "*14)
q="query C($r: CompletionRequest!){ completion(request:$r){ text } }"
try:
    with httpx.Client(timeout=90.0) as c:
        for k in range(10):
            for key in (f"st:{k}", f"st:{k}"):
                c.post("http://localhost:8008/graphql", json={"query":q,"variables":{"r":{"prompt":"x","staticPrefix":SP,"flowCacheKey":key,"maxTokens":2,"temperature":0}}})
            c.post("http://localhost:8008/graphql", json={"query":q,"variables":{"r":{"prompt":"What is 2+2? Just the number.","maxTokens":40,"temperature":0}}})
except Exception: pass
PYEOF
  tail -n +$((mark+1)) "$SRVLOG" | grep -ci "code -3"
}

log "==== CROSS-VAL CAMPAIGN START (pre-warm cross-task KV cache) ===="
for entry in "${MODELS[@]}"; do
  model="${entry%%:*}"; short="${entry##*:}"
  log "==== MODEL: $model ($short) ===="
  for f in swa_full kv_unified flow_kv_cache; do
    .venv/bin/python dev/patch_cache_cfg.py "llmvp/configs/$model.yaml" "$f" true >/dev/null 2>&1
  done
  printf '%s' "$model" > "$LLMVP/active_config.txt"
  if ! restart_server; then log "  SKIP $model (server load failed)"; continue; fi

  c3=$(stability_c3); c3=${c3:-0}
  cache="warm"
  if [ "$c3" -gt 0 ]; then
    log "  stability FAILED ($c3 code-3) -> disabling flow_kv_cache for $model"
    .venv/bin/python dev/patch_cache_cfg.py "llmvp/configs/$model.yaml" flow_kv_cache false >/dev/null 2>&1
    printf '%s' "$model" > "$LLMVP/active_config.txt"
    if ! restart_server; then log "  SKIP $model (reload failed)"; continue; fi
    cache="nocache"
  else
    log "  stability OK (0 code-3) -> warming flows"
    .venv/bin/python dev/warm_flows.py 2>&1 | tail -1 | sed 's/^/  /' | tee -a "$LOG"
  fi

  runid="cmp-${short}-${cache}"
  log "  running 8-set -> $runid"
  rm -rf "runs/$runid" "/tmp/$runid.log" 2>/dev/null
  OURO_TRACE=1 .venv/bin/tb run -d terminal-bench-core==0.1.1 \
    --agent-import-path tb_adapter.agent:OuroborosAgent $TASKS \
    --n-concurrent 1 --run-id "$runid" --output-path runs > "/tmp/$runid.log" 2>&1
  acc=$(grep -iE "Accuracy" "/tmp/$runid.log" | tail -1 | tr -s ' ')
  builds=$(grep -c "flow_kv_cache BUILD" "$SRVLOG"); hits=$(grep -c "flow_kv_cache HIT" "$SRVLOG")
  log "  $short DONE | cache=$cache | $acc | (cumulative server BUILD=$builds HIT=$hits)"
done

log "==== CAMPAIGN COMPLETE — summary ===="
for entry in "${MODELS[@]}"; do
  short="${entry##*:}"
  for mode in warm nocache; do
    f=$(find "runs/cmp-${short}-${mode}" -name results.json -maxdepth 2 2>/dev/null | head -1)
    [ -n "$f" ] && .venv/bin/python -c "
import json
r=json.load(open('$f')); rs=r.get('results') or r.get('trials') or []
n=sum(1 for t in rs if t.get('is_resolved'))
print('  ${short} (${mode}): %d/%d = %.1f%%'%(n,len(rs),100*n/len(rs) if rs else 0))" 2>/dev/null | tee -a "$LOG"
  done
done
