#!/bin/bash
# Overnight multi-model game_challenge harness.
#
# Phase 0: resume the current (paused) gpt-oss mission in-place for more cycles.
# Then, per model: switch LLMVP config, restart the server, run a FRESH
# game_challenge mission (game_challenge_overnight.yaml — no hooks; this
# script owns lifecycle), and archive everything for morning review.
# Finally: restore the production config and leave the server running on it.
#
# Archives land in ~/ouroboros-overnight/<date>/<run-name>/:
#   agent-state/   — .agent (mission.json, traces incl. prompts+thinking)
#   workspace/     — the produced game (sans .venv/__pycache__)
#   interactions.jsonl — LLMVP-side prompt/response log for the run
#
# Usage: nohup bash dev/overnight_models.sh > /tmp/overnight.out 2>&1 &

set -u

ROOT=/Users/lah-rb/Repos/ouroboros
LLMVP=$ROOT/llmvp
WORK=/tmp/ouroboros-challenge
ARCHIVE=$HOME/ouroboros-overnight/$(date +%Y%m%d)
LOG=$ARCHIVE/orchestrator.log
PROD_CONFIG=gpt-oss-120b-a5

# name:config:cycles for the fresh runs (phase 0 handled separately)
RUNS=(
  "qwen3.5-122b:qwen3.5-122b-a10:100"
  "gemma-4:gemma-4-31b:100"
  "qwen3-next-coder:qwen3-next-coder-80b-a3:100"
  "mistral-small-4:mistral-small-4-119b-a6:100"
)

mkdir -p "$ARCHIVE"

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOG"; }

health_ok() {
  curl -s -m 5 -X POST http://localhost:8008/graphql \
    -H 'Content-Type: application/json' \
    -d '{"query":"query { health { status } }"}' 2>/dev/null | grep -q '"ok"'
}

wait_ready() {
  # Model load + static eval for the big MoEs can take a while.
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
  # Cycle-cap exit is code 1 by design — capture, don't abort the harness.
  (cd "$ROOT" && uv run ouroboros.py start --working-dir "$WORK" \
    --max-cycles "$cycles" --trace-thinking --trace-prompts) >>"$LOG" 2>&1
  log "agent run finished (exit $?)"
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
  run_agent "$cycles"
  archive_run "$name"
}

log "███ overnight harness starting — archive: $ARCHIVE ███"

# ── Phase 0: continue the paused gpt-oss mission (+50 cycles) ────────
log "════ phase 0: gpt-oss continuation (+50 cycles) ════"
if ! health_ok; then
  switch_model "$PROD_CONFIG" || log "WARN: gpt-oss server not ready; trying anyway"
fi
# Preserve the completed run-3 interaction log before the continuation
# overwrites context, then clear so the archive is continuation-only.
cp "$LLMVP/logs/interactions.jsonl" "$ARCHIVE/gpt-oss-run3-interactions.jsonl" 2>/dev/null || true
rm -f "$LLMVP/logs/interactions.jsonl"
(cd "$ROOT" && uv run ouroboros.py mission resume --working-dir "$WORK") >>"$LOG" 2>&1 || true
run_agent 50
archive_run "gpt-oss-continued"

# ── Fresh runs per model ─────────────────────────────────────────────
for entry in "${RUNS[@]}"; do
  IFS=':' read -r name cfg cycles <<<"$entry"
  fresh_run "$name" "$cfg" "$cycles"
done

# ── Restore production config ────────────────────────────────────────
log "════ restoring production config ($PROD_CONFIG) ════"
switch_model "$PROD_CONFIG" || log "WARN: production restore failed — check server"

log "███ overnight harness complete ███"
