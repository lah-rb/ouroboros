#!/bin/bash
# Restart LLMVP on a given model config, stability-check the KV cache, and warm
# the cross-task flow cache. Extracted from cross_val_campaign.sh's restart_server
# so a single model can be brought up cleanly (SIGTERM, not pkill — pool leaks on
# hard-kill; SIGKILL only as a wedged-server fallback; free port 8008).
#   usage: dev/start_model.sh <config-name-without-.yaml>
set -u
CONFIG="${1:?usage: start_model.sh <config>}"
ROOT=/Users/lah-rb/Repos/ouroboros
LLMVP=$ROOT/llmvp
SRVLOG=$LLMVP/logs/llmvp_server.log
cd "$ROOT"
log(){ echo "[$(date +%H:%M:%S)] $*"; }

health_inst(){ curl -s -m 5 -X POST http://localhost:8008/graphql -H 'Content-Type: application/json' \
  -d '{"query":"{ health { availableInstances } }"}' 2>/dev/null \
  | python3 -c "import json,sys;print(json.load(sys.stdin)['data']['health']['availableInstances'])" 2>/dev/null; }

# arm cache flags for this config
for f in swa_full kv_unified flow_kv_cache; do
  .venv/bin/python dev/patch_cache_cfg.py "llmvp/configs/$CONFIG.yaml" "$f" true >/dev/null 2>&1
done
printf '%s' "$CONFIG" > "$LLMVP/active_config.txt"

# stop the current server (SIGTERM, wait up to 20s, SIGKILL fallback)
pid=$(pgrep -f "api/main.py" | head -1)
if [ -n "$pid" ]; then
  log "SIGTERM server pid=$pid"
  kill -TERM "$pid" 2>/dev/null
  for i in $(seq 1 20); do kill -0 "$pid" 2>/dev/null || break; sleep 1; done
  if kill -0 "$pid" 2>/dev/null; then log "  stuck -> SIGKILL"; kill -9 "$pid" 2>/dev/null; sleep 2; fi
fi
lsof -ti:8008 2>/dev/null | xargs -r kill -9 2>/dev/null; sleep 1

# relaunch
log "relaunch on $CONFIG"
( cd "$LLMVP" && nohup .venv/bin/python api/main.py >> logs/llmvp_server.log 2>&1 & )
ready=0
for i in $(seq 1 120); do
  [ "$(health_inst)" = "1" ] && { log "server ready (~$((i*3))s)"; ready=1; break; }
  if tail -n 80 "$SRVLOG" 2>/dev/null | grep -qiE "out of memory|Failed to load|Could not load|insufficient memory"; then
    log "LOAD ERROR:"; tail -n 80 "$SRVLOG" | grep -iE "out of memory|Failed to load|error" | tail -3; exit 1
  fi
  sleep 3
done
[ "$ready" = "1" ] || { log "SERVER LOAD TIMEOUT"; exit 1; }

# stability: 10 BUILD/HIT/plain cycles; count new code-3 errors
mark=$(wc -l < "$SRVLOG")
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
c3=$(tail -n +$((mark+1)) "$SRVLOG" | grep -ci "code -3"); c3=${c3:-0}
if [ "$c3" -gt 0 ]; then
  log "STABILITY FAILED ($c3 code-3) -> disabling flow_kv_cache"
  .venv/bin/python dev/patch_cache_cfg.py "llmvp/configs/$CONFIG.yaml" flow_kv_cache false >/dev/null 2>&1
  printf '%s' "$CONFIG" > "$LLMVP/active_config.txt"
  log "  (re-run start_model.sh to reload without cache)"; exit 2
fi
log "stability OK (0 code-3) -> warming flows"
.venv/bin/python dev/warm_flows.py 2>&1 | tail -2 | sed 's/^/  /'
log "DONE: $CONFIG up, cache warm"
