#!/bin/bash
# Empirical robustness boundary for dual-context gpt-oss: sweep per-context
# n_ctx, soak each point with the interleaved dual-persona harness, record
# errors + wired floor + KV size. Finds where "survivable" becomes "robust".
#
# Points ascend so the boundary is bracketed from the safe side. Each point:
# edit duo yaml n_ctx → bounce server → wait 2 slots → duo_soak.py R rounds →
# append CSV row. Restores 32768 + the a5 production server at the end.
#
# Usage: bash dev/duo_ctx_sweep.sh   (results: dev/duo_ctx_sweep.csv)
set -u
ROOT=/Users/lah-rb/Repos/ouroboros
YAML="$ROOT/llmvp/configs/gpt-oss-120b-a5-duo.yaml"
CSV="$ROOT/dev/duo_ctx_sweep.csv"
SOAK_ROUNDS="${SOAK_ROUNDS:-25}"
POINTS=(8192 16384 32768 49152 65536)

[ -f "$CSV" ] || echo "n_ctx,rounds,turns,errors,exit,kv_mib_per_cache,wired_min_gb,wired_max_gb,ts" > "$CSV"

set_nctx() {
  /usr/bin/sed -i '' -E "s/^  n_ctx: [0-9]+/  n_ctx: $1/" "$YAML"
}

bounce() {
  pkill -TERM -f api/main.py 2>/dev/null; sleep 8
  printf 'gpt-oss-120b-a5-duo' > "$ROOT/llmvp/active_config.txt"
  ( cd "$ROOT/llmvp" && nohup .venv/bin/python api/main.py >> logs/llmvp_server.log 2>&1 & )
  for i in $(seq 1 80); do
    curl -s -m 3 -X POST http://127.0.0.1:8008/graphql -H 'Content-Type: application/json' \
      -d '{"query":"query{health{availableInstances}}"}' 2>/dev/null | grep -q '"availableInstances": 2' && return 0
    pgrep -f api/main.py >/dev/null || return 1
    sleep 5
  done
  return 1
}

for N in "${POINTS[@]}"; do
  echo "════ sweep point n_ctx=$N ════"
  set_nctx "$N"
  if ! bounce; then
    echo "$N,$SOAK_ROUNDS,0,BOOT_FAIL,-,-,-,-,$(date +%H:%M)" >> "$CSV"
    continue
  fi
  KV=$(grep "ggml:" "$ROOT/llmvp/logs/llmvp_server.log" | grep "KV buffer size" | tail -1 | grep -oE '[0-9.]+ MiB' | head -1)
  OUT=$("$ROOT/llmvp/.venv/bin/python" "$ROOT/dev/duo_soak.py" "$SOAK_ROUNDS" 2>&1)
  RC=$?
  ERRS=$(echo "$OUT" | grep -cE "ERR:")
  WMIN=$(echo "$OUT" | grep -oE 'wired=[0-9.]+' | cut -d= -f2 | sort -n | head -1)
  WMAX=$(echo "$OUT" | grep -oE 'wired=[0-9.]+' | cut -d= -f2 | sort -n | tail -1)
  TURNS=$(echo "$OUT" | grep -cE "ok \(|ERR:")
  echo "$N,$SOAK_ROUNDS,$TURNS,$ERRS,$RC,${KV:-?},${WMIN:-?},${WMAX:-?},$(date +%H:%M)" >> "$CSV"
  echo "→ n_ctx=$N: $ERRS errors in $TURNS turns (exit $RC, wired ${WMIN:-?}-${WMAX:-?}G)"
  # A dead slot (exit 1) at this size means bigger sizes are hopeless — stop.
  [ "$RC" = "1" ] && { echo "slot death at $N — stopping ascent"; break; }
done

# Restore the intended duo default + production server.
set_nctx 32768
pkill -TERM -f api/main.py 2>/dev/null; sleep 8
printf 'gpt-oss-120b-a5' > "$ROOT/llmvp/active_config.txt"
( cd "$ROOT/llmvp" && nohup .venv/bin/python api/main.py >> logs/llmvp_server.log 2>&1 & )
echo "════ sweep complete — production a5 restoring ════"
column -t -s, "$CSV"
