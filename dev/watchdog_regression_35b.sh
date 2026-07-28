#!/bin/bash
# End-to-end regression for the watchdog identity change.
#
#   python3 dev/daemonize.py /tmp/tier/wd_regression.log bash dev/watchdog_regression_35b.sh
#
# macOS has no setsid — use daemonize.py and confirm PPID 1 before walking away.
#
# ── WHAT THIS RE-RUNS AND WHY ────────────────────────────────────────
# The 2026-07-28 qwen3.6-35b arm lost 28 of 61 minutes to the health watchdog.
# Its batch turn generated 57,003 tokens — ~46k of chain-of-thought then a
# complete, correct eleven-file batch — and was cancelled at 49,987 against a
# blind 49,152 ceiling, 200s before the server delivered it. The orphan then
# kept decoding, and six queued requests read ITS counter off the single-slot
# health endpoint (52,120 four times, then 54,250) and cancelled themselves.
#
# Same config, same mission, same wall clock. The arm is the assertion:
#   * zero watchdog cancels           (six were collateral, one was a long CoT)
#   * the batch turn DELIVERS         (files from build_structure, not the
#                                      serial create fallback)
#   * first write well before +29 min (that arm's, vs the 122b's +11.8)
#
# A degeneration abort by LLMVP's own guards is NOT a regression here — that is
# the server doing the job the ceiling was approximating, and it now arrives
# named ("long-cycle repetition: …") rather than as a generic failure.
set -u
ROOT=/Users/lah-rb/Repos/ouroboros
cd "$ROOT"
export PATH="$HOME/.local/bin:$PATH" PYTHONPATH="$ROOT"

BASE=/tmp/tier
CFG=qwen3.6-35b-a3
WORK=$BASE/wd-regress-$CFG
RESTORE_CFG=gpt-oss-120b-a5-swarm-524k
BOOT_TIMEOUT=900

log(){ echo "[$(date '+%m-%d %H:%M:%S')] $*"; }

stop_server(){
  local pid
  pid=$(pgrep -f "[a]pi/main.py" | head -1) || true
  [ -n "${pid:-}" ] || return 0
  kill -TERM "$pid" 2>/dev/null || true   # never SIGKILL: it leaks the pool
  for _ in $(seq 1 900); do
    pgrep -f "[a]pi/main.py" >/dev/null || { log "  server $pid down"; return 0; }
    sleep 1
  done
  log "  ERROR server did not exit on SIGTERM after 900s"; return 1
}

wait_healthy(){
  local deadline=$((SECONDS + BOOT_TIMEOUT))
  while [ $SECONDS -lt $deadline ]; do
    curl -s -m 5 -X POST http://localhost:8008/graphql \
      -H 'Content-Type: application/json' \
      -d '{"query":"{ health { status } }"}' 2>/dev/null | grep -q '"ok"' && return 0
    sleep 10
  done
  return 1
}

boot(){
  local cfg=$1
  stop_server || { log "  ABORT — previous server would not stop"; return 1; }
  echo -n "$cfg" > "$ROOT/llmvp/active_config.txt"
  ( cd "$ROOT/llmvp" && nohup .venv/bin/python api/main.py \
      > "$BASE/${cfg}_wd_server.log" 2>&1 & )
  wait_healthy || { log "  ABORT — server not healthy in ${BOOT_TIMEOUT}s"; return 1; }
  log "  server up ($cfg)"
}

while pgrep -f "[o]uroboros.py start" >/dev/null; do sleep 60; done

log "=== booting $CFG on the reworked watchdog ==="
boot "$CFG" || exit 1

# Precondition: the identity fields must actually be served, or the watchdog
# silently runs in fallback mode and this run measures nothing.
IDENT=$(curl -s -m 10 -X POST http://localhost:8008/graphql \
  -H 'Content-Type: application/json' \
  -d '{"query":"{ health { requestId thinkingComplete decodeMode } }"}' 2>/dev/null)
log "  identity probe: $IDENT"
case "$IDENT" in
  *"Cannot query field"*)
    log "  ABORT — server does not expose requestId; it is running OLD code."
    exit 1 ;;
esac

log "=== mission (1h backstop) ==="
rm -rf "$WORK"; mkdir -p "$WORK"
uv run ouroboros.py mission create --mission_config game_challenge_boss \
  --working-dir "$WORK" --top-phase quality > "$BASE/wd_regress_create.log" 2>&1 \
  || { log "  ABORT — mission create failed"; exit 1; }

env OURO_LLMVP=http://localhost:8008/graphql OURO_REASONING_OFF=1 \
  uv run ouroboros.py start --working-dir "$WORK" \
    --max-wall-clock 1h --trace-thinking > "$BASE/wd_regress_run.log" 2>&1
log "  mission finished (rc=$?)"

L=$BASE/wd_regress_run.log
S=$BASE/${CFG}_wd_server.log
cnt(){ local v; v=$(grep -c "$1" "$2" 2>/dev/null); echo "${v:-0}"; }

log "=== VERDICT ==="
log "  watchdog cancels ....... $(cnt 'cancelled by health watchdog' "$L")   (was 7)"
log "  runaway cancels ........ $(cnt 'runaway generation' "$L")   (was 7)"
log "  long-gen advisories .... $(cnt 'advisory ceiling' "$L")   (the ceiling, no longer fatal)"
log "  server degen verdicts .. $(cnt 'aborted the generation as degenerate' "$L")"
log "  instances-busy ......... $(cnt 'instances are busy' "$L")   (was 4)"
log "  batch files written .... $(cnt 'Structural batch' "$L")"
log "  largest generation ..... $(grep -o 'generated=[0-9]* tok' "$S" | sed 's/[^0-9]//g' | sort -n | tail -1)"
find "$WORK" -type f -not -path "*/.agent/*" -not -path "*/.venv/*" \
  -not -path "*/__pycache__/*" -not -path "*/.ruff_cache/*" \
  | sed "s|$WORK/|    |" | sort | while read -r f; do log "  file $f"; done
uv run ouroboros.py mission status --working-dir "$WORK" 2>/dev/null \
  | grep -E "^  Status:|Goals \(" | while read -r l; do log "  $l"; done

log "=== restoring production server ==="
stop_server
echo -n "$RESTORE_CFG" > "$ROOT/llmvp/active_config.txt"
( cd "$ROOT/llmvp" && nohup .venv/bin/python api/main.py > "$BASE/wd_restore_server.log" 2>&1 & )
wait_healthy && log "restored ($RESTORE_CFG)" || log "WARN restore server unhealthy"
log "=== REGRESSION RUN COMPLETE ==="
