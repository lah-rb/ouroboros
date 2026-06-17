#!/bin/bash
# Redo of the qwen3-next-coder serial baseline. The first attempt hit a
# loader/models/world contract-drift boot failure (the exemplar never
# reached per-file creation prompts — fixed in 8b84578) and burned 40+
# cycles in a diagnose loop that couldn't see the undeclared field; not
# a comparable baseline. Same lifecycle as overnight_vbh.sh, single run.
#
# Usage: nohup bash dev/restart_qwen_vbh.sh > /tmp/restart_qwen_vbh.out 2>&1 &

set -u

ROOT=/Users/lah-rb/Repos/ouroboros
LLMVP=$ROOT/llmvp
WORK=/tmp/ouroboros-challenge
ARCHIVE=$HOME/ouroboros-overnight/$(date +%Y%m%d)-vbh
LOG=$ARCHIVE/orchestrator.log
PROD_CONFIG=gpt-oss-120b-a5

RUNS=(
  "qwen3-trap30:qwen3-next-coder-80b-a3:30"
)

mkdir -p "$ARCHIVE"

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOG"; }

health_ok() {
  curl -s -m 5 -X POST http://localhost:8008/graphql \
    -H 'Content-Type: application/json' \
    -d '{"query":"query { health { status } }"}' 2>/dev/null | grep -q '"ok"'
}

wait_ready() {
  for _ in $(seq 1 180); do
    health_ok && return 0
    sleep 10
  done
  return 1
}

switch_model() {
  local cfg=$1
  log "switching LLMVP to $cfg"
  printf "%s" "$cfg" > "$LLMVP/active_config.txt"
  (cd "$LLMVP" && uv run llmvp.py --stop) >>"$LOG" 2>&1 || true
  sleep 5
  (cd "$LLMVP" && uv run llmvp.py --backend) >>"$LOG" 2>&1 || true
  if wait_ready; then
    log "server ready on $cfg"
    return 0
  fi
  log "ERROR: server failed to become ready on $cfg"
  return 1
}

archive_run() {
  local name=$1
  local dest=$ARCHIVE/$name
  mkdir -p "$dest"
  if [ -d "$WORK/.agent" ]; then
    cp -R "$WORK/.agent" "$dest/agent-state"
  else
    log "WARN: no .agent state to archive for $name"
  fi
  rsync -a --exclude .venv --exclude __pycache__ --exclude .agent \
    --exclude .ruff_cache "$WORK/" "$dest/workspace/" 2>>"$LOG" || true
  cp "$LLMVP/logs/interactions.jsonl" "$dest/interactions.jsonl" 2>>"$LOG" || true
  log "archived $name -> $dest"
}

run_agent() {
  local cycles=$1
  (cd "$ROOT" && uv run ouroboros.py start --working-dir "$WORK" \
    --max-cycles "$cycles" --trace-thinking --trace-prompts) >>"$LOG" 2>&1
  local rc=$?
  log "agent run finished (exit $rc)"
  return $rc
}

fresh_run() {
  local name=$1 cfg=$2 cycles=$3
  log "════ fresh run: $name ($cfg, $cycles cycles) ════"
  if ! switch_model "$cfg"; then
    log "SKIPPING $name — server unavailable"
    return 1
  fi
  rm -rf "$WORK" && mkdir -p "$WORK"
  rm -f "$LLMVP/logs/interactions.jsonl"
  (cd "$ROOT" && uv run ouroboros.py cue-compile) >>"$LOG" 2>&1
  if ! (cd "$ROOT" && uv run ouroboros.py mission create \
      --mission_config game_challenge_overnight) >>"$LOG" 2>&1; then
    log "ERROR: mission create failed for $name"
    return 1
  fi
  if run_agent "$cycles"; then
    log "🎉 $name COMPLETED within budget"
  else
    log "$name parked at cycle cap (or errored) — see log"
  fi
  archive_run "$name"
}

log "███ qwen trap-30 harness starting — archive: $ARCHIVE ███"

for spec in "${RUNS[@]}"; do
  IFS=':' read -r name cfg cycles <<< "$spec"
  fresh_run "$name" "$cfg" "$cycles"
done

log "restoring production config ($PROD_CONFIG)"
switch_model "$PROD_CONFIG" || log "WARN: production restore failed — check server"

log "███ qwen trap-30 harness done ███"
