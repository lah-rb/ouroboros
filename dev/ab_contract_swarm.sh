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
# STOPPING POINT — each arm halts at the STRUCTURAL→ENVIRONMENT boundary:
# it runs plan → structural build → environment setup (deps installed so
# the prototype is runnable), then PAUSES before the functional phase
# rewrites any source. This isolates the paradigm: everything after
# structural is identical code_core flow, so letting arms run into
# functional repair would launder the paradigm's quality difference. The
# workspace left behind IS each paradigm's raw structural deliverable,
# set up to run — exactly what the blind panel reviews. A hard wall-clock
# backstop (AB_WALL, default 90m) catches an arm whose structural phase
# never completes (a file the sweep can't repair in budget — itself a
# paradigm signal).
#
# Compare afterwards (both arms book a batch_structural note via
# apply_batch_results): structural-phase wall clock, generation tokens,
# gate pass-rate, files left for serial fallback. Run while the server is
# otherwise QUIET (pause any other missions first); restore the gameab
# config at the end if round-2 arms are still running, else production a5.
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
  local RUNLOG="$BASE/${arm}_run.log"
  date +%s > "$BASE/${arm}.t0"
  # Start the agent; AB_WALL is only a BACKSTOP — the arm normally stops
  # earlier, at the structural→environment boundary (below).
  ( cd "$ROOT" && env OURO_LLMVP=http://localhost:8008/graphql \
      uv run ouroboros.py start --working-dir "$WORK" \
      --max-wall-clock "${AB_WALL:-90m}" \
      --trace-thinking --trace-prompts \
      > "$RUNLOG" 2>&1 ) &
  local PID=$!

  # Watch for the structural→environment transition. The controller
  # dispatches environment setup (project_ops) ONLY after every structural
  # goal is complete; that step's appearance means the structural
  # deliverable is finished. Pause the mission there — the in-flight
  # environment step completes (deps installed) and the agent parks at the
  # next cycle boundary, before functional_sweep_next edits anything.
  local paused=0
  while kill -0 "$PID" 2>/dev/null; do
    if [ "$paused" = 0 ] && grep -q "step 'dispatch_environment_setup'" "$RUNLOG" 2>/dev/null; then
      log "arm=$arm structural complete → pausing before functional phase"
      ( cd "$ROOT" && uv run ouroboros.py mission pause --working-dir "$WORK" ) \
        >/dev/null 2>&1
      paused=1
    fi
    sleep 5
  done
  wait "$PID" 2>/dev/null
  echo $? > "$BASE/${arm}.exit"
  date +%s > "$BASE/${arm}.t1"
  log "arm=$arm done (exit $(cat "$BASE/${arm}.exit"), paused_at_structural=$paused)"
}

swap gpt-oss-120b-a5-gameab
create_and_run code_core ""

swap gpt-oss-120b-a5-swarm
create_and_run swarm contract_swarm

swap gpt-oss-120b-a5-gameab
log "AB SWARM COMPLETE — compare the batch_structural notes in each arm's mission.json"
