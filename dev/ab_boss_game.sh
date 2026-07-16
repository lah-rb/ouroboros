#!/bin/bash
# Overnight A/B: "fun-tier" boss-fight game challenge, adaptive reasoning vs
# kill-switch baseline, RUN UNTIL COMPLETION (game_challenge_boss.yaml sets
# run_until: completed; no --max-cycles, no wall clock — the mission ends at
# a terminal status only). Arms run in parallel on the gameab batched W=3
# server. Waits for the cardgame pair (dev/ab_reasoning_games.sh) to finish
# first so the server is not oversubscribed.
#
# Usage: bash dev/ab_boss_game.sh <path-to-ab_games.log>
set -u
ROOT=/Users/lah-rb/Repos/ouroboros
cd "$ROOT"; export PATH="$HOME/.local/bin:$PATH" PYTHONPATH="$ROOT"
PRIOR_LOG=${1:?path to the running ab_games.log}
BASE=/tmp/gameab
log(){ echo "[$(date +%H:%M:%S)] $*"; }

log "waiting for cardgame pair to complete (marker in $PRIOR_LOG)..."
while ! grep -q "AB SPIKE COMPLETE" "$PRIOR_LOG"; do sleep 60; done
log "cardgame pair done — launching bossgame pair (UNBOUNDED)"

wc -l < "$ROOT/llmvp/logs/llmvp_server.log" > "$BASE/bossgame_srvmark.txt"
pids=(); arms=(adaptive baseline)
for arm in "${arms[@]}"; do
  WORK="$BASE/bossgame_${arm}"
  rm -rf "$WORK"; mkdir -p "$WORK"
  (cd "$ROOT" && uv run ouroboros.py mission create \
      --mission_config game_challenge_boss --working-dir "$WORK") \
      > "$BASE/bossgame_${arm}_create.log" 2>&1
  if [ "$arm" = "adaptive" ]; then
    ENVV=(OURO_ADAPTIVE_REASONING=1 OURO_ROUTER_THR=0.4)
  else
    ENVV=(OURO_REASONING_OFF=1)
  fi
  log "  arm=$arm workdir=$WORK env=${ENVV[*]}"
  ( cd "$ROOT" && env "${ENVV[@]}" OURO_LLMVP=http://localhost:8008/graphql \
      uv run ouroboros.py start --working-dir "$WORK" \
      --trace-thinking --trace-prompts \
      > "$BASE/bossgame_${arm}_run.log" 2>&1
    echo $? > "$BASE/bossgame_${arm}.exit" ) &
  pids+=($!)
  echo $SECONDS > "$BASE/bossgame_${arm}.t0"
done
for i in "${!pids[@]}"; do
  wait "${pids[$i]}"
  arm=${arms[$i]}
  t0=$(cat "$BASE/bossgame_${arm}.t0")
  log "  BOSSGAME arm=$arm FINISHED wall=$((SECONDS - t0))s exit=$(cat "$BASE/bossgame_${arm}.exit" 2>/dev/null)"
done
MARK=$(tr -d ' ' < "$BASE/bossgame_srvmark.txt")
log "  bossgame reasoning activity (server-wide, both arms):"
tail -n +"$MARK" "$ROOT/llmvp/logs/llmvp_server.log" \
  | grep -oE "head-swap → [a-z]+|head-splice → [a-z]+|completion head-swap → [a-z]+" \
  | sort | uniq -c
log "BOSSGAME AB COMPLETE"
