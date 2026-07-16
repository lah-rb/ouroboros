#!/bin/bash
# Server-keeper — keep the LLMVP daemon alive for an overnight sweep. If the
# server PROCESS dies, relaunch it (+ re-warm the flow cache); tb is resilient to
# a server disconnect (the in-flight task fails, tb moves on), so keeping the
# server up is enough to finish all 80 tasks. CONSERVATIVE: restarts only on
# actual process death (pgrep), never on a busy/slow health response, so it can't
# disrupt a live generation. Exits cleanly when the sweep (tb) finishes, or after
# too many restarts (a real, persistent failure worth a human look).
#   usage: dev/server_keeper.sh <config-name>
set -u
CONFIG="${1:?usage: server_keeper.sh <config>}"
ROOT=/Users/lah-rb/Repos/ouroboros
LLMVP=$ROOT/llmvp
cd "$ROOT"
log(){ echo "[$(date '+%m-%d %H:%M:%S')] $*"; }

health1(){ curl -s -m 5 -X POST http://localhost:8008/graphql -H 'Content-Type: application/json' \
  -d '{"query":"{ health { availableInstances } }"}' 2>/dev/null \
  | python3 -c "import json,sys;print(json.load(sys.stdin)['data']['health']['availableInstances'])" 2>/dev/null; }

restarts=0
saw_sweep=0
log "keeper started for $CONFIG"
while true; do
  # Exit once the sweep has run and then disappeared (finished).
  if pgrep -f "tb run -d terminal-bench" >/dev/null; then
    saw_sweep=1
  elif [ "$saw_sweep" = "1" ]; then
    log "sweep finished — keeper exiting"; exit 0
  fi

  if ! pgrep -f "api/main.py" >/dev/null; then
    restarts=$((restarts + 1))
    log "SERVER DOWN — relaunch #$restarts"
    lsof -ti:8008 2>/dev/null | xargs -r kill -9 2>/dev/null; sleep 1
    printf '%s' "$CONFIG" > "$LLMVP/active_config.txt"
    ( cd "$LLMVP" && nohup .venv/bin/python api/main.py >> logs/llmvp_server.log 2>&1 < /dev/null & )
    ready=0
    for i in $(seq 1 120); do
      [ "$(health1)" = "1" ] && { log "  ready (~$((i*3))s); warming flow cache"; \
        .venv/bin/python dev/warm_flows.py >/dev/null 2>&1; ready=1; break; }
      sleep 3
    done
    [ "$ready" = "1" ] || log "  WARN: server did not report ready within ~6min"
    if [ "$restarts" -ge 10 ]; then
      log "max relaunches (10) reached — giving up, needs a human"; exit 1
    fi
  fi
  sleep 30
done
