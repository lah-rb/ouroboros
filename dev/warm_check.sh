#!/bin/bash
# Orchestration-only: wait for an ALREADY-LAUNCHED LLMVP server to come up, run
# the KV-cache stability check, and warm the cross-task flow cache. Does NOT
# launch the daemon (the server runs as its own background job) — so this script
# exits cleanly when done and never hangs a pipe (the start_model.sh footgun).
#   usage: dev/warm_check.sh <config-name>   # config only used for the disable-on-fail path
set -u
CONFIG="${1:?usage: warm_check.sh <config>}"
ROOT=/Users/lah-rb/Repos/ouroboros
SRVLOG=$ROOT/llmvp/logs/llmvp_server.log
cd "$ROOT"
log(){ echo "[$(date +%H:%M:%S)] $*"; }

health_inst(){ curl -s -m 5 -X POST http://localhost:8008/graphql -H 'Content-Type: application/json' \
  -d '{"query":"{ health { availableInstances } }"}' 2>/dev/null \
  | python3 -c "import json,sys;print(json.load(sys.stdin)['data']['health']['availableInstances'])" 2>/dev/null; }

# wait for server ready (up to ~6min; model load + first-call)
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
  log "STABILITY FAILED ($c3 code-3) -> disabling flow_kv_cache for $CONFIG (restart needed)"
  .venv/bin/python dev/patch_cache_cfg.py "llmvp/configs/$CONFIG.yaml" flow_kv_cache false >/dev/null 2>&1
  exit 2
fi
log "stability OK (0 code-3) -> warming flows"
.venv/bin/python dev/warm_flows.py 2>&1 | tail -2 | sed 's/^/  /'
log "READY: $CONFIG up, stable, cache warm"
