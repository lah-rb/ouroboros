#!/bin/bash
# A/B: legacy (session_full_replay) vs RESIDENT seq cache, on a real feature task.
#
# Same archived replay-point (the 43/43 patched game) + same added directive,
# run twice on gpt-oss-120b — legacy full-replay (A) vs resident seq cache (B).
# The only variable is the cache strategy; model behavior is otherwise identical
# (resident arm keeps full turn CoT, same as full_replay — strip stays OFF so the
# comparison isolates the prefill cost, not a behavior change). Per arm we capture
# the runtime trace, agent log, produced game, and wall-clock; ab_trace_compare.py
# reports time-vs-cycle, total time, and the session-step prefill that drives it.
#
# Resident wins by keeping the session seq live (flat per-turn prefill) instead of
# re-prefilling the growing transcript each turn (legacy full_replay).
#
# Usage: nohup bash dev/ab_resident_test.sh > /tmp/ab_resident.out 2>&1 &
#        bash dev/ab_resident_test.sh resident    # run only one arm

set -u

ROOT=/Users/lah-rb/Repos/ouroboros
LLMVP=$ROOT/llmvp
WORK=/tmp/ouroboros-challenge
AB=$HOME/ouroboros-overnight/$(date +%Y%m%d)-resident-ab
REPLAY=$AB/replay-point
LOG=$AB/orchestrator.log
PY=$ROOT/.venv/bin/python

DIRECTIVE="Add a new room to the game containing a boss enemy that is gated behind a puzzle the player must solve before they can enter and confront it. Wire it into the world, the parser, and game state, and make it playable from the command line."
CAP_CYCLES=100
CAP_WALL=2h

# name:resident_seq_cache flag. Resident FIRST — early signal on the new path.
ARMS=( "resident:true" "legacy:false" )

# Optional positional filter: run only the named arm(s).
if [ "$#" -gt 0 ]; then
  FILTERED=()
  for entry in "${ARMS[@]}"; do
    for want in "$@"; do
      [ "${entry%%:*}" = "$want" ] && FILTERED+=("$entry")
    done
  done
  ARMS=("${FILTERED[@]}")
fi

mkdir -p "$AB"
log(){ echo "[$(date '+%F %T')] $*" | tee -a "$LOG"; }
health_ok(){ curl -s -m5 -X POST http://localhost:8008/graphql \
  -H 'Content-Type: application/json' \
  -d '{"query":"query { health { status } }"}' 2>/dev/null | grep -q '"ok"'; }
wait_ready(){ for _ in $(seq 1 180); do health_ok && return 0; sleep 10; done; return 1; }

restart_server(){
  (cd "$LLMVP" && uv run llmvp.py --stop) >>"$LOG" 2>&1 || true
  sleep 5
  (cd "$LLMVP" && uv run llmvp.py --backend) >>"$LOG" 2>&1 || true
  wait_ready
}

# Seed the replay-point from the newest existing *-ab/replay-point if missing.
if [ ! -d "$REPLAY" ]; then
  SRC=$(ls -dt "$HOME"/ouroboros-overnight/*-ab/replay-point 2>/dev/null | head -1)
  if [ -n "$SRC" ] && [ -d "$SRC" ]; then
    log "seeding replay-point from $SRC"
    mkdir -p "$AB" && cp -R "$SRC" "$REPLAY"
  fi
fi
if [ ! -d "$REPLAY" ]; then
  log "ERROR: replay-point missing at $REPLAY — archive it first."
  exit 1
fi

log "███ RESIDENT session A/B starting — replay-point: $REPLAY ███"

for entry in "${ARMS[@]}"; do
  IFS=':' read -r name flag <<<"$entry"
  ARM=$AB/arm-$name
  mkdir -p "$ARM"
  log "════ arm: $name (resident_seq_cache=$flag) ════"

  # Identical starting state for every arm.
  rm -rf "$WORK" && cp -R "$REPLAY" "$WORK"
  rm -f "$LLMVP/logs/interactions.jsonl"

  # Reset to committed config, then apply this arm's resident flag.
  (cd "$ROOT" && git checkout llmvp/configs/gpt-oss-120b-a5.yaml) >>"$LOG" 2>&1
  "$PY" "$ROOT/dev/ab_set_resident.py" "$flag" >>"$LOG" 2>&1
  log "restarting server on gpt-oss (resident=$flag)…"
  if ! restart_server; then
    log "ERROR: server not ready for arm $name — skipping"
    continue
  fi
  # Confirm the resident path actually engaged for the resident arm.
  if [ "$flag" = "true" ]; then
    grep -m1 "Resident-seq cache ACTIVE\|resident_seq_cache requested but" \
      "$LLMVP/logs/llmvp_server.log" 2>/dev/null | tail -1 | tee -a "$LOG" || true
  fi

  "$PY" "$ROOT/dev/ab_inject_directive.py" "$WORK" "$DIRECTIVE" >>"$LOG" 2>&1

  start=$(date +%s)
  (cd "$ROOT" && uv run ouroboros.py start --working-dir "$WORK" \
    --max-cycles "$CAP_CYCLES" --max-wall-clock "$CAP_WALL" \
    --trace-thinking --trace-prompts) >"$ARM/run.log" 2>&1
  rc=$?
  end=$(date +%s)

  tr=$(ls -t "$WORK/.agent/traces/"*.jsonl 2>/dev/null | head -1)
  [ -n "$tr" ] && cp "$tr" "$ARM/trace.jsonl"
  cp "$LLMVP/logs/interactions.jsonl" "$ARM/interactions.jsonl" 2>>"$LOG" || true
  cp "$WORK/.agent/mission.json" "$ARM/mission.json" 2>>"$LOG" || true
  rsync -a --exclude .venv --exclude __pycache__ --exclude .agent \
    --exclude .ruff_cache "$WORK/" "$ARM/workspace/" 2>>"$LOG" || true
  printf '{"mode":"%s","resident":%s,"start_epoch":%s,"end_epoch":%s,"exit_code":%s}\n' \
    "$name" "$flag" "$start" "$end" "$rc" > "$ARM/meta.json"
  log "arm $name done (exit $rc, $((end - start))s, trace=$(basename "${tr:-none}"))"
done

# Restore the committed prod config + relaunch.
log "════ restoring prod config (committed gpt-oss) ════"
(cd "$ROOT" && git checkout llmvp/configs/gpt-oss-120b-a5.yaml) >>"$LOG" 2>&1
restart_server && log "prod restored"

log "███ RESIDENT A/B complete — analyze: $PY dev/ab_trace_compare.py $AB ███"
