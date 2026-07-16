#!/bin/bash
# A/B spike: greenfield game missions with the full adaptive-reasoning config
# (TF-IDF router + nine static-high steps) vs pre-feature baseline (fixed
# medium everywhere, via the OURO_REASONING_OFF kill-switch).
#
# ARMS RUN IN PARALLEL per game on the batched W=3 server (gameab config, one
# spare seat for stateless completions) — both arms share decode conditions,
# so arm-vs-arm comparisons are fair; individual wall times are slower by
# design (accepted: higher total output). Pairs (games) run sequentially.
#
# Server must already be up on gpt-oss-120b-a5-gameab. Usage:
#   bash dev/ab_reasoning_games.sh
set -u
ROOT=/Users/lah-rb/Repos/ouroboros
cd "$ROOT"; export PATH="$HOME/.local/bin:$PATH" PYTHONPATH="$ROOT"
BASE=/tmp/gameab; mkdir -p "$BASE"
CYCLES=40
log(){ echo "[$(date +%H:%M:%S)] $*"; }

GAME2_OBJ="Build a deck-building card battle game engine in Python. The engine should load card definitions from YAML files (cost, attack, defense, special effects with triggers). Implement a turn-based battle loop against a scripted AI opponent: draw, play cards under a mana budget, resolve attacks and effects, track health. Manage deck, hand, discard pile, and effect state. Support save and load of a run in progress to JSON. Create a playable demo with at least 12 distinct cards, 3 AI opponents of increasing difficulty, and a 3-battle gauntlet. Runnable from the command line with clear battle narration."

run_pair() {
  local game=$1; shift
  local extra_create_args=("$@")
  log "════════ GAME $game: launching BOTH arms in parallel ════════"
  local pids=() arms=(adaptive baseline)
  wc -l < "$ROOT/llmvp/logs/llmvp_server.log" > "$BASE/${game}_srvmark.txt"
  for arm in "${arms[@]}"; do
    local WORK="$BASE/${game}_${arm}"
    rm -rf "$WORK"; mkdir -p "$WORK"
    (cd "$ROOT" && uv run ouroboros.py mission create \
        --mission_config game_challenge_overnight --working-dir "$WORK" \
        ${extra_create_args[@]+"${extra_create_args[@]}"}) > "$BASE/${game}_${arm}_create.log" 2>&1
    if [ "$arm" = "adaptive" ]; then
      ENVV=(OURO_ADAPTIVE_REASONING=1 OURO_ROUTER_THR=0.4)
    else
      ENVV=(OURO_REASONING_OFF=1)
    fi
    log "  arm=$arm workdir=$WORK env=${ENVV[*]}"
    ( cd "$ROOT" && env "${ENVV[@]}" OURO_LLMVP=http://localhost:8008/graphql \
        uv run ouroboros.py start --working-dir "$WORK" --max-cycles $CYCLES \
        --trace-thinking --trace-prompts \
        > "$BASE/${game}_${arm}_run.log" 2>&1
      echo $? > "$BASE/${game}_${arm}.exit" ) &
    pids+=($!)
    echo $SECONDS > "$BASE/${game}_${arm}.t0"
  done
  for i in "${!pids[@]}"; do
    wait "${pids[$i]}"
    local arm=${arms[$i]}
    local t0; t0=$(cat "$BASE/${game}_${arm}.t0")
    log "  GAME $game arm=$arm FINISHED wall=$((SECONDS - t0))s exit=$(cat "$BASE/${game}_${arm}.exit" 2>/dev/null)"
  done
  local MARK; MARK=$(tr -d ' ' < "$BASE/${game}_srvmark.txt")
  log "  GAME $game reasoning activity (server-wide, both arms):"
  tail -n +"$MARK" "$ROOT/llmvp/logs/llmvp_server.log" \
    | grep -oE "head-swap → [a-z]+|head-splice → [a-z]+|completion head-swap → [a-z]+" \
    | sort | uniq -c
}

run_pair adventure
run_pair cardgame --objective "$GAME2_OBJ"
log "AB SPIKE COMPLETE"
