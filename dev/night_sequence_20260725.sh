#!/bin/bash
# Night sequence, 2026-07-25:
#   0. wait for the cf-regen to finish (already running)
#   1. decode-ceiling experiment on gpt-oss (128 seats) -> results + graph
#   2. mistral-medium pressure probe -> MEASURED safe n_ctx
#   3. mistral-medium game_challenge_boss, 7h backstop, quality top phase
#
# Safety: every stage checks the server is healthy before proceeding and
# aborts the chain rather than stacking a failure into the next stage.
set -u
ROOT=/Users/lah-rb/Repos/ouroboros
LLMVP=$ROOT/llmvp
BASE=$HOME/ouroboros_artifacts/nightruns
LOG=$BASE/night_sequence.log
mkdir -p "$BASE"
log(){ echo "[$(date +%H:%M:%S)] $*" | tee -a "$LOG"; }

health(){ curl -s -m 10 -X POST http://localhost:8008/graphql \
  -H 'Content-Type: application/json' \
  -d '{"query":"{ health { status poolSize inFlight } }"}' 2>/dev/null; }

wait_ready(){  # $1 = timeout seconds
  local t=0
  while [ $t -lt "${1:-900}" ]; do
    if health | grep -q '"status": *"ok"'; then return 0; fi
    sleep 10; t=$((t+10))
  done
  return 1
}

restart_server(){  # $1 = config name
  log "  switching config -> $1"
  printf '%s' "$1" > "$LLMVP/active_config.txt"
  (cd "$LLMVP" && .venv/bin/python api/main.py --stop >>"$LOG" 2>&1)
  sleep 20
  # --backend (NOT --background); it blocks until the pool is ready, so the
  # wait_ready loop below is belt-and-braces rather than the primary signal.
  (cd "$LLMVP" && nohup .venv/bin/python api/main.py --backend >>"$LOG" 2>&1 &)
  if wait_ready 1200; then log "  server READY on $1"; return 0; fi
  log "  !! server did NOT come ready on $1 — aborting chain"; return 1
}

# ── 0. wait for the regen ────────────────────────────────────────────────
log "=== stage 0: waiting for cf-regen to finish ==="
while pgrep -f "cf_regen_swarm.py --run" >/dev/null 2>&1; do sleep 60; done
log "cf-regen finished"
tail -14 /private/tmp/claude-501/-Users-lah-rb-Repos-ouroboros/dcb3e0ab-de34-4f55-8466-b40c922afeed/scratchpad/regen.log >> "$LOG"

# ── 1. decode ceiling ────────────────────────────────────────────────────
log "=== stage 1: decode-ceiling experiment (128 seats) ==="
if restart_server gpt-oss-120b-a5-decodeceiling; then
  (cd "$ROOT" && timeout 5400 uv run python dev/decode_ceiling/bench.py \
      --ladder 1,2,4,8,16,32,48,64,96,128 --repeats 2 \
      >> "$BASE/decode_ceiling.log" 2>&1)
  log "  bench exit=$?"
  (cd "$ROOT" && uv run python dev/decode_ceiling/plot.py >> "$BASE/decode_ceiling.log" 2>&1)
  log "  graph written"
  grep -E "^PEAK|^batching|^sum-of-rates" "$BASE/decode_ceiling.log" | tee -a "$LOG"
else
  log "  SKIPPING stage 1"
fi

# ── 2. mistral pressure probe ────────────────────────────────────────────
log "=== stage 2: mistral-medium pressure probe (measure real MB/token) ==="
SAFE_CTX=65536
if restart_server mistral-medium-3.5-128b-boss; then
  (cd "$ROOT" && timeout 3600 uv run python dev/context_pressure_probe.py \
      --kill-gb 104 >> "$BASE/mistral_pressure.log" 2>&1)
  log "  probe exit=$?"
  tail -8 "$BASE/mistral_pressure.log" | tee -a "$LOG"
  # Raise n_ctx ONLY on measured evidence: the probe extrapolates max fill at
  # a wired ceiling. Conservative rule — take the next rung up only if the
  # extrapolation clears it with >=8GB of headroom.
  EXTRA=$(grep -oE "extrapolated max fill at [0-9]+GB wired ≈ [0-9,]+" \
          "$BASE/mistral_pressure.log" | tail -1 | grep -oE "[0-9,]+$" | tr -d ,)
  if [ -n "${EXTRA:-}" ]; then
    log "  probe extrapolates max fill ≈ ${EXTRA} tokens"
    if [ "$EXTRA" -ge 110000 ]; then SAFE_CTX=98304
    elif [ "$EXTRA" -ge 75000 ]; then SAFE_CTX=65536
    fi
  fi
  log "  chosen n_ctx for the boss run: $SAFE_CTX"
  if [ "$SAFE_CTX" != "65536" ]; then
    sed -i '' "s/^  n_ctx: 65536$/  n_ctx: $SAFE_CTX/" \
      "$LLMVP/configs/mistral-medium-3.5-128b-boss.yaml"
    restart_server mistral-medium-3.5-128b-boss || log "  !! reload failed"
  fi
else
  log "  probe stage failed — proceeding at the safe default $SAFE_CTX"
fi

# ── 3. mistral boss run ──────────────────────────────────────────────────
log "=== stage 3: mistral-medium game_challenge_boss (7h, quality top phase) ==="
W=$BASE/mistral_boss
rm -rf "$W"; mkdir -p "$W"
if health | grep -q '"status": *"ok"'; then
  (cd "$ROOT" && uv run ouroboros.py mission create --mission_config game_challenge_boss \
      --working-dir "$W" --top-phase quality) > "$W/create.log" 2>&1 \
      && log "  mission created" || { log "  !! create FAILED"; tail -5 "$W/create.log" | tee -a "$LOG"; }
  # thinking OFF by default (config thinking:false); explicit cue-authored
  # `reasoning: high` steps still fire — that is the point of NOT setting
  # OURO_REASONING_OFF, which would kill those too. Router stays off.
  ( cd "$ROOT" && env OURO_NOOP=1 OURO_LLMVP=http://localhost:8008/graphql \
      uv run ouroboros.py start --working-dir "$W" --max-wall-clock 7h \
      --trace-thinking --trace-prompts > "$W/run.log" 2>&1 ) &
  RUNPID=$!
  log "  mistral boss RUNNING pid=$RUNPID (7h cap)"
  T0=$SECONDS
  while kill -0 $RUNPID 2>/dev/null; do
    sleep 300
    [ $((SECONDS-T0)) -gt 27000 ] && { log "  7.5h hard cut"; pkill -f "ouroboros.py start --working-dir $W"; break; }
  done
  log "  mistral boss FINISHED wall=$((SECONDS-T0))s"
  (cd "$ROOT" && uv run python -c "
import json,collections
d=json.load(open('$W/.agent/mission.json'))
print('RESULT mistral_boss: status=',d.get('status'),
      dict(collections.Counter((g['type'],g['status']) for g in d['goals'])))" | tee -a "$LOG")
else
  log "  !! server not healthy — mistral boss NOT started"
fi
log "=== night sequence COMPLETE ==="
