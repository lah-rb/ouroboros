#!/bin/bash
# qwen-mitigation validation + gpt-oss control, back-to-back.
#
# The point: qwen3-next-coder degenerated for Ouroboros (worked AROUND
# the framework / session-state poisoning). The fix landed llmvp-side —
# session_full_replay (full re-prefill per turn from a pristine snapshot,
# no per-turn state surgery) + temperature_floor 0.4 — both armed in
# qwen3-next-coder-80b-a3.yaml. This run validates them on a real
# game_challenge workload, then runs gpt-oss on the SAME config
# (game_challenge_overnight, serial) as a same-night control.
#
# Lifecycle is owned here (game_challenge_overnight has no hooks):
#   phase 0  archive the current paused working-dir mission (preserve,
#            no resume — it predates today's trap run and isn't archived)
#   run 1    qwen3-next-coder (mitigations) — fresh mission, archived
#   run 2    gpt-oss-120b-a5 (prod/control) — fresh mission, archived
# Ending on gpt-oss restores production automatically.
#
# Backstops (dogfooding the new run-termination flags): each run is
# capped --max-cycles 500 (allows completion — the strongest "qwen
# works again" signal) AND --max-wall-clock 4h (a degenerate qwen that
# stalls must not eat the night and starve the control). The mission
# parks as paused on either cap; the archive captures it either way.
#
# Usage: nohup bash dev/qwen_validate_then_gptoss.sh \
#            > /tmp/qwen_validate.out 2>&1 &

set -u

ROOT=/Users/lah-rb/Repos/ouroboros
LLMVP=$ROOT/llmvp
WORK=/tmp/ouroboros-challenge
ARCHIVE=$HOME/ouroboros-overnight/$(date +%Y%m%d)-validate
LOG=$ARCHIVE/orchestrator.log
PROD_CONFIG=gpt-oss-120b-a5

# name:config:cycles:wallclock
RUNS=(
  "qwen-validate:qwen3-next-coder-80b-a3:500:4h"
  "gpt-oss-control:gpt-oss-120b-a5:500:4h"
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
  local cycles=$1 wallclock=$2
  # Cap exit is code 1 by design — capture, don't abort the harness.
  (cd "$ROOT" && uv run ouroboros.py start --working-dir "$WORK" \
    --max-cycles "$cycles" --max-wall-clock "$wallclock" \
    --trace-thinking --trace-prompts) >>"$LOG" 2>&1
  log "agent run finished (exit $?)"
}

fresh_run() {
  local name=$1 cfg=$2 cycles=$3 wallclock=$4
  log "════ fresh run: $name ($cfg, ${cycles}c / ${wallclock}) ════"
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
  run_agent "$cycles" "$wallclock"
  archive_run "$name"
}

log "███ qwen-validation harness starting — archive: $ARCHIVE ███"

# ── Phase 0: preserve the current paused working-dir mission ─────────
# Snapshot only (no resume) — the fresh runs wipe WORK.
if [ -f "$WORK/.agent/mission.json" ]; then
  log "════ phase 0: preserving existing working-dir mission ════"
  archive_run "preexisting-paused"
else
  log "phase 0: no existing working-dir mission to preserve"
fi

# ── qwen validation, then gpt-oss control ────────────────────────────
for entry in "${RUNS[@]}"; do
  IFS=':' read -r name cfg cycles wallclock <<<"$entry"
  fresh_run "$name" "$cfg" "$cycles" "$wallclock"
done

# Run 2 (gpt-oss) leaves the server on production already.
log "═══ ending on $PROD_CONFIG (production) ═══"
if ! health_ok; then
  log "WARN: server not healthy at finish — restoring $PROD_CONFIG"
  switch_model "$PROD_CONFIG" || log "WARN: production restore failed — check server"
fi

log "███ qwen-validation harness complete ███"
