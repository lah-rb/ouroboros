#!/bin/bash
# Overnight tier comparison — game_challenge_boss, top_phase=quality, 2h backstop each.
#
#   bash dev/overnight_tier_run.sh
#
# Sequential by necessity: one resident model at a time. ~2h per arm plus load,
# so ~8.5h for four. Each arm is INDEPENDENT — a model that fails to load is
# logged and skipped, it does not abort the batch. That matters because this
# runs unattended and gemma-4 @ 65536 is an unverified canary (1.68 MB/token
# KV geometry); if it cannot boot, the other three must still produce.
#
# NOTE ON SURVIVAL: background jobs started from a Claude Code session die when
# that session exits (dev memory: stranded a PAUSED mission and a DOWN server).
# Launch this under `setsid` so it leaves the session's process group:
#     setsid nohup bash dev/overnight_tier_run.sh > /tmp/tier/driver.log 2>&1 &
set -u
ROOT=/Users/lah-rb/Repos/ouroboros
cd "$ROOT"
export PATH="$HOME/.local/bin:$PATH" PYTHONPATH="$ROOT"

BASE=/tmp/tier
mkdir -p "$BASE"
WALL=2h
BOOT_TIMEOUT=600          # seconds to wait for a server to report healthy
ARMS=(laguna-s-2.1 gpt-oss-120b-a5-swarm-524k gemma-4-31b devstral-2-small-24b)

log(){ echo "[$(date '+%m-%d %H:%M:%S')] $*"; }

stop_server(){
  local pid
  pid=$(pgrep -f "[a]pi/main.py" | head -1) || true
  [ -n "${pid:-}" ] || return 0
  kill -TERM "$pid" 2>/dev/null || true
  for _ in $(seq 1 90); do
    pgrep -f "[a]pi/main.py" >/dev/null || return 0
    sleep 1
  done
  log "  WARN server $pid did not exit on SIGTERM after 90s"
  return 1
}

wait_healthy(){
  local deadline=$((SECONDS + BOOT_TIMEOUT))
  while [ $SECONDS -lt $deadline ]; do
    if curl -s -m 5 -X POST http://localhost:8008/graphql \
         -H 'Content-Type: application/json' \
         -d '{"query":"{ health { status } }"}' 2>/dev/null | grep -q '"ok"'; then
      return 0
    fi
    sleep 10
  done
  return 1
}

log "=== overnight tier run: ${#ARMS[@]} arms, ${WALL} each, top_phase=quality ==="

for cfg in "${ARMS[@]}"; do
  WORK="$BASE/$cfg"
  rm -rf "$WORK"; mkdir -p "$WORK"
  log "--- ARM $cfg ---"

  stop_server
  echo -n "$cfg" > "$ROOT/llmvp/active_config.txt"
  ( cd "$ROOT/llmvp" && nohup .venv/bin/python api/main.py \
      > "$BASE/${cfg}_server.log" 2>&1 & )

  if ! wait_healthy; then
    log "  SKIP $cfg — server not healthy within ${BOOT_TIMEOUT}s (see ${cfg}_server.log)"
    echo "SKIPPED_BOOT" > "$WORK/OUTCOME"
    continue
  fi
  log "  server up"

  if ! uv run ouroboros.py mission create --mission_config game_challenge_boss \
        --working-dir "$WORK" --top-phase quality > "$BASE/${cfg}_create.log" 2>&1; then
    log "  SKIP $cfg — mission create failed"
    echo "SKIPPED_CREATE" > "$WORK/OUTCOME"
    continue
  fi

  log "  mission running (backstop $WALL)"
  start=$SECONDS
  env OURO_LLMVP=http://localhost:8008/graphql OURO_REASONING_OFF=1 \
    uv run ouroboros.py start --working-dir "$WORK" \
      --max-wall-clock "$WALL" --trace-thinking \
      > "$BASE/${cfg}_run.log" 2>&1
  rc=$?
  mins=$(( (SECONDS - start) / 60 ))

  files=$(find "$WORK" -type f -not -path "*/.agent/*" -not -name "OUTCOME" 2>/dev/null | wc -l | tr -d ' ')
  ok=0; bad=0
  while IFS= read -r f; do
    if uv run python -c "import ast,sys;ast.parse(open(sys.argv[1]).read())" "$f" 2>/dev/null; then
      ok=$((ok+1)); else bad=$((bad+1)); fi
  done < <(find "$WORK" -name "*.py" -not -path "*/.agent/*" 2>/dev/null)

  status=$(uv run ouroboros.py mission status --working-dir "$WORK" 2>/dev/null \
             | grep -E "^  Status:|Goals \(" | tr '\n' ' ')
  log "  done rc=$rc ${mins}min files=$files py_ok=$ok py_fail=$bad | $status"
  printf 'rc=%s\nminutes=%s\nfiles=%s\npy_ok=%s\npy_fail=%s\n%s\n' \
    "$rc" "$mins" "$files" "$ok" "$bad" "$status" > "$WORK/OUTCOME"
done

stop_server
echo -n "gpt-oss-120b-a5-swarm-524k" > "$ROOT/llmvp/active_config.txt"
( cd "$ROOT/llmvp" && nohup .venv/bin/python api/main.py \
    > "$BASE/restore_server.log" 2>&1 & )
wait_healthy && log "restored production server (gpt-oss)" || log "WARN restore server unhealthy"

log "=== SUMMARY ==="
for cfg in "${ARMS[@]}"; do
  printf '%-34s ' "$cfg"
  if [ -f "$BASE/$cfg/OUTCOME" ]; then tr '\n' ' ' < "$BASE/$cfg/OUTCOME"; echo
  else echo "no outcome recorded"; fi
done
log "=== BATCH COMPLETE ==="
