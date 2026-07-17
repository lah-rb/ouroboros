#!/bin/bash
# Contract-swarm structural A/B: code_core's single-completion batch build
# (arm A) vs the contract_swarm flow set (arm B) on the SAME objective
# (missions/game_challenge_swarm.yaml). SEQUENTIAL arms — the two shapes
# want different server seat configs, swapped live via the swapModel
# mutation (no restart):
#   arm A: gpt-oss-120b-a5-gameab  (W=3, n_ctx 131072 — the batch build
#          emits one huge completion)
#   arm B: gpt-oss-120b-a5-swarm   (W=6, n_ctx 98304 — many short worker
#          streams)
#
# Compare afterwards (both arms book a batch_structural note via
# apply_batch_results): structural-phase wall clock, generation tokens,
# gate pass-rate, files left for serial fallback, downstream functional
# convergence. Run while the server is otherwise QUIET (pause any other
# missions first); restore the gameab config at the end if round-2 arms
# are still running, else production a5.
#
# Usage: bash dev/ab_contract_swarm.sh
set -u
ROOT=/Users/lah-rb/Repos/ouroboros
cd "$ROOT"; export PATH="$HOME/.local/bin:$PATH" PYTHONPATH="$ROOT"
BASE=/tmp/swarmab
mkdir -p "$BASE"
log(){ echo "[$(date +%H:%M:%S)] $*"; }

swap(){
  log "swapModel -> $1"
  .venv/bin/python ouroboros.py llmvp swap "$1" --drain-s 30 || exit 1
}

# NOTE: OURO_FLOW_SET is read at mission CREATE time (highest precedence)
# and persisted into mission.json — export it around create, not start.
create_and_run(){
  local arm=$1 flowset=$2
  local WORK="$BASE/arm_${arm}"
  rm -rf "$WORK"; mkdir -p "$WORK"
  ( cd "$ROOT" && env ${flowset:+OURO_FLOW_SET=$flowset} \
      uv run ouroboros.py mission create \
      --mission_config game_challenge_swarm --working-dir "$WORK" ) \
      > "$BASE/${arm}_create.log" 2>&1 || { log "create failed ($arm)"; exit 1; }
  log "arm=$arm flow_set=${flowset:-code_core} workdir=$WORK"
  date +%s > "$BASE/${arm}.t0"
  ( cd "$ROOT" && env OURO_LLMVP=http://localhost:8008/graphql \
      uv run ouroboros.py start --working-dir "$WORK" \
      --trace-thinking --trace-prompts \
      > "$BASE/${arm}_run.log" 2>&1
    echo $? > "$BASE/${arm}.exit" )
  date +%s > "$BASE/${arm}.t1"
  log "arm=$arm done (exit $(cat "$BASE/${arm}.exit"))"
}

swap gpt-oss-120b-a5-gameab
create_and_run code_core ""

swap gpt-oss-120b-a5-swarm
create_and_run swarm contract_swarm

swap gpt-oss-120b-a5-gameab
log "AB SWARM COMPLETE — compare the batch_structural notes in each arm's mission.json"
