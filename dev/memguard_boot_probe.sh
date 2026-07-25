#!/bin/bash
# Memory-guarded boot probe: boot a config while sampling wired memory every
# 2s; KILL the server the moment wired crosses the ceiling — characterize
# allocation cost without risking a reboot. The runtime backstop behind the
# server's KV preflight (which refuses computable swa_full oversizes before
# allocation; this probe catches everything else). Promoted from the session
# scratchpad after reboot #3's /tmp wipe deleted it mid-campaign.
# Usage: memguard_boot_probe.sh <config> [ceiling_gb]
set -u
CONFIG=$1
CEILING_GB=${2:-100}
ROOT=/Users/lah-rb/Repos/ouroboros
OUT=/tmp/memprobe_${CONFIG}.log
cd "$ROOT"
echo "== memguard probe: $CONFIG ceiling=${CEILING_GB}GB ==" | tee "$OUT"

pkill -TERM -f "api/main.py"; sleep 8; pkill -KILL -f "api/main.py" 2>/dev/null; sleep 2
printf '%s' "$CONFIG" > llmvp/active_config.txt
( cd llmvp && nohup .venv/bin/python api/main.py >> logs/server_stdout.log 2>&1 & )

START=$(date +%s)
verdict="TIMEOUT"
while true; do
  sleep 2
  WIRED_GB=$(vm_stat | awk '/wired/ {gsub(/\./,"",$4); printf "%.1f", $4*16384/1073741824}')
  RSS_GB=$(ps -o rss= -p $(pgrep -f "api/main.py" | head -1) 2>/dev/null | awk '{printf "%.1f", $1/1048576}')
  ELAPSED=$(( $(date +%s) - START ))
  echo "[+${ELAPSED}s] wired=${WIRED_GB}GB rss=${RSS_GB:-0}GB" >> "$OUT"
  # ceiling breach -> kill server immediately
  if awk "BEGIN{exit !(${WIRED_GB} > ${CEILING_GB})}"; then
    echo "CEILING BREACH at +${ELAPSED}s: wired=${WIRED_GB}GB — killing server" | tee -a "$OUT"
    pkill -KILL -f "api/main.py"; verdict="BREACH"; break
  fi
  # server died on its own (validator/architecture/preflight rejection)
  pgrep -f "api/main.py" >/dev/null || { echo "SERVER EXITED at +${ELAPSED}s" | tee -a "$OUT"; verdict="EXITED"; break; }
  # pool ready = successful boot
  READY=$(curl -s -m3 -X POST http://localhost:8008/graphql -H 'content-type: application/json' \
    -d '{"query":"{ health { availableInstances } }"}' 2>/dev/null | grep -oE "\"availableInstances\": *[0-9]+" | cut -d: -f2)
  if [ "${READY:-0}" -ge 1 ] 2>/dev/null; then
    echo "POOL READY at +${ELAPSED}s: wired=${WIRED_GB}GB rss=${RSS_GB}GB" | tee -a "$OUT"; verdict="READY"; break
  fi
  [ "$ELAPSED" -ge 1200 ] && { echo "TIMEOUT at +${ELAPSED}s (wired=${WIRED_GB}GB) — killing" | tee -a "$OUT"; pkill -KILL -f "api/main.py"; break; }
done
echo "verdict=$verdict" | tee -a "$OUT"
# steady-state sample if ready
if [ "$verdict" = "READY" ]; then
  sleep 5
  vm_stat | awk '/wired/ {gsub(/\./,"",$4); printf "steady wired=%.1fGB\n", $4*16384/1073741824}' | tee -a "$OUT"
fi
