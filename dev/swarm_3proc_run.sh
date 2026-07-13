#!/bin/bash
# 3-process swarm probe: boot three llmvp processes (ports 8008/8018/8028,
# batched W=8 each, 32k ctx, weights shared via mmap page cache), run the
# 24-stream bench, tear down, restore production a5.
#
# SWARM_ENV controls residency: empty (default) = residency sets ON — if
# macOS shares the wiring of the mmap'd weight pages across processes,
# wired stays ~79G and the bench is fair; if NOT shared, boot 2 trips the
# 100G wired guard and aborts safely. SWARM_ENV=GGML_METAL_NO_RESIDENCY=1
# reproduces the unwired variant (measured: catastrophic paging contention,
# aggregate <11 tok/s across 24 streams).
set -u
ROOT=/Users/lah-rb/Repos/ouroboros
PTR="$ROOT/llmvp/active_config.txt"
LOG="$ROOT/llmvp/logs/llmvp_server.log"

wired_gb() { vm_stat | awk '/wired/ {printf "%.0f", $NF * 16384 / 1e9}'; }

pkill -TERM -f api/main.py 2>/dev/null; sleep 8

for PORT in 8008 8018 8028; do
  printf "gpt-oss-swarm-p$PORT" > "$PTR"
  ( cd "$ROOT/llmvp" && env ${SWARM_ENV:-} nohup .venv/bin/python api/main.py >> "$LOG" 2>&1 & )
  ok=""
  for i in $(seq 1 90); do
    curl -s -m 3 -X POST "http://127.0.0.1:$PORT/graphql" -H 'Content-Type: application/json' \
      -d '{"query":"query{health{availableInstances}}"}' 2>/dev/null | grep -q '"availableInstances": 8' && { ok=1; break; }
    sleep 5
  done
  W=$(wired_gb)
  echo "port $PORT ready=$ok wired=${W}G"
  if [ -z "$ok" ]; then echo "BOOT_FAIL port $PORT — aborting"; break; fi
  if [ "$W" -gt 100 ]; then echo "WIRED_GUARD tripped (${W}G > 100G) — aborting"; break; fi
done

if [ -n "${ok:-}" ]; then
  "$ROOT/llmvp/.venv/bin/python" "$ROOT/dev/swarm_3proc_bench.py" 2
fi

pkill -TERM -f api/main.py 2>/dev/null; sleep 8
printf 'gpt-oss-120b-a5' > "$PTR"
( cd "$ROOT/llmvp" && nohup .venv/bin/python api/main.py >> "$LOG" 2>&1 & )
echo "swarm probe done — a5 restoring"
