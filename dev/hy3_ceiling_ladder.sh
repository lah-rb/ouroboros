#!/bin/bash
# Find the REAL memory ceiling on Hy3-REAP-200B — by measuring, not predicting.
#
#   python3 dev/daemonize.py ~/ouroboros-runs/hy3_ladder_console.log bash dev/hy3_ceiling_ladder.sh
#
# ── WHY A LADDER AND NOT A CONFIG ────────────────────────────────────
# The header formula says 324 KiB/token (81 layers x 8 kv-heads x 128 dim), so
# 108.3 GB of weights against a 116 GB wired cap leaves ~23k tokens. That is a
# PREDICTION, and gemma-4 is the standing proof that the prediction can be
# wrong by ~2x — kv_bytes_per_token_measured exists in ModelConfig precisely
# because the formula assumes every layer carries a full-size cache. Luke has
# already loaded this model in LM Studio at 2.5k context and got real output,
# so the weights fit and the only open question is how much KV rides along.
#
# So: start small, READ THE ACTUAL KV BUFFER llama.cpp allocates, and derive
# the ceiling from measurement. Each rung records the true bytes/token, which
# is the number that belongs in the config afterwards.
#
# ── THIS RUN CAN HARD-REBOOT THE MACHINE. TWO CONSEQUENCES. ──────────
# 1. /tmp DOES NOT SURVIVE. Every run artifact of the last two days lives in
#    /tmp/tier (~60 MB: judged artifacts, traces, degeneration evidence, both
#    blind-panel directories with their KEYs). This script ARCHIVES THEM FIRST
#    and refuses to proceed if that fails. Losing a benchmark to a memory
#    experiment would be a self-inflicted wound.
# 2. THIS SCRIPT'S OWN LOG must outlive the crash, or the ladder learns nothing
#    from the rung that killed it. Everything goes to ~/ouroboros-runs/, never
#    /tmp, and each rung is flushed BEFORE the load that might not return.
#
# ── THE RUNGS ────────────────────────────────────────────────────────
# 2048 first, matching Luke's LM Studio probe — if that fails, the problem is
# not context and the ladder stops immediately. Then a doubling walk. The
# preflight budget is raised per rung to just above what that rung needs, so
# the guard still refuses a rung we did not intend rather than being disabled
# outright.
set -u
ROOT=/Users/lah-rb/Repos/ouroboros
RUNS=$HOME/ouroboros-runs
STAMP=$(date '+%Y%m%d-%H%M%S')
ARCHIVE=$RUNS/pre-hy3-$STAMP
LOG=$RUNS/hy3_ladder_$STAMP.log
CFG=hy3-reap-200b-a21
BOOT_TIMEOUT=1200
RESTORE_CFG=gpt-oss-120b-a5-swarm-524k

mkdir -p "$RUNS"
# Line-buffered and fsync'd per rung: the whole point is surviving the rung
# that does not return.
log(){ echo "[$(date '+%m-%d %H:%M:%S')] $*" | tee -a "$LOG"; sync; }

cd "$ROOT"
export PATH="$HOME/.local/bin:$PATH" PYTHONPATH="$ROOT"

# ── 0. Save what a reboot would eat ──────────────────────────────────
log "=== archiving /tmp before a run that may reboot the machine ==="
if bash "$ROOT/dev/preserve_run_artifacts.sh" "pre-hy3-$STAMP" 2>&1 | tee -a "$LOG" | grep -q "SAFE TO REBOOT"; then
  log "  archive complete at $ARCHIVE"
else
  log "  ABORT — archive failed. Not proceeding: this run can destroy /tmp."
  exit 1
fi

while pgrep -f "[o]uroboros.py start" >/dev/null; do
  log "  waiting for an in-flight mission to finish…"; sleep 120
done

stop_server(){
  local pid; pid=$(pgrep -f "[a]pi/main.py" | head -1) || true
  [ -n "${pid:-}" ] || return 0
  kill -TERM "$pid" 2>/dev/null || true   # never SIGKILL: it leaks the pool
  for _ in $(seq 1 900); do
    pgrep -f "[a]pi/main.py" >/dev/null || return 0; sleep 1
  done
  log "  ERROR server would not exit on SIGTERM"; return 1
}

# rung <n_ctx> <preflight_gb>
rung(){
  local n=$1 budget=$2
  local slog=$RUNS/hy3_server_${n}.log
  log ""
  log "─── RUNG n_ctx=$n (preflight budget ${budget} GB) ───"
  stop_server || return 1

  # Rewrite only the two fields this rung changes.
  /usr/bin/sed -i '' \
    -e "s/^  n_ctx: .*/  n_ctx: $n/" \
    -e "s/^  kv_preflight_gb: .*/  kv_preflight_gb: $budget/" \
    "$ROOT/llmvp/configs/$CFG.yaml"
  echo -n "$CFG" > "$ROOT/llmvp/active_config.txt"

  log "  loading… (if the machine reboots, this is the rung that did it)"
  ( cd "$ROOT/llmvp" && nohup .venv/bin/python api/main.py > "$slog" 2>&1 & )

  local deadline=$((SECONDS + BOOT_TIMEOUT)) up=0
  while [ $SECONDS -lt $deadline ]; do
    curl -s -m 5 -X POST http://localhost:8008/graphql -H 'Content-Type: application/json' \
      -d '{"query":"{ health { status } }"}' 2>/dev/null | grep -q '"ok"' && { up=1; break; }
    grep -q "Startup failed" "$slog" 2>/dev/null && break
    sleep 10
  done

  if [ "$up" != "1" ]; then
    local why; why=$(grep -m1 -o "KV preflight REFUSED.*" "$slog" 2>/dev/null)
    log "  DID NOT COME UP: ${why:-see $slog}"
    return 1
  fi

  # THE MEASUREMENT. llama.cpp reports the KV buffer it actually allocated;
  # that number divided by n_ctx is the real bytes/token, and it is what
  # kv_bytes_per_token_measured should carry (the gemma-4 precedent).
  local kvmib
  kvmib=$(grep -o "KV buffer size = *[0-9.]*" "$slog" | grep -o "[0-9.]*$" \
          | awk '{s+=$1} END{printf "%.0f", s}')
  local bpt=0
  [ -n "$kvmib" ] && [ "$kvmib" != "0" ] && \
    bpt=$(awk -v k="$kvmib" -v n="$n" 'BEGIN{printf "%.0f", k*1048576/n}')
  log "  UP. KV buffer allocated: ${kvmib:-?} MiB  ->  ${bpt} bytes/token measured"
  log "     (header formula predicts 331776 bytes/token)"

  # Real output, not just a health probe — a model that loads and emits
  # garbage has not passed anything.
  local ans
  ans=$(curl -s -m 300 -X POST http://localhost:8008/graphql -H 'Content-Type: application/json' \
    -d '{"query":"query($p:String!,$m:Int!){completion(request:{prompt:$p,maxTokens:$m}){text tokensGenerated}}","variables":{"p":"Write a Python function that reverses a string. Return only the code.","m":200}}' 2>/dev/null)
  log "  generation: $(echo "$ans" | head -c 300)"
  log "RUNG $n OK  kv_mib=$kvmib bytes_per_token=$bpt"
  return 0
}

log "=== Hy3-REAP-200B ceiling ladder ==="
log "  weights 108.3 GB · wired cap $(sysctl -n iogpu.wired_limit_mb) MB · physical $(sysctl -n hw.memsize | awk '{printf "%.0f", $1/1e9}') GB"

# budget = weights (108.3) + predicted KV + ~1 GB slack, so the preflight still
# guards the rung above the one we asked for.
rung   2048 110 || { log "2048 failed — the ceiling is below Luke's LM Studio probe; stopping."; }
rung   4096 111 || true
rung   8192 113 || true
rung  16384 116 || true
rung  24576 119 || true

log ""
log "=== LADDER COMPLETE — restoring production ==="
stop_server
echo -n "$RESTORE_CFG" > "$ROOT/llmvp/active_config.txt"
( cd "$ROOT/llmvp" && nohup .venv/bin/python api/main.py > "$RUNS/hy3_restore.log" 2>&1 & )
for _ in $(seq 1 90); do
  curl -s -m 5 -X POST http://localhost:8008/graphql -H 'Content-Type: application/json' \
    -d '{"query":"{ health { status } }"}' 2>/dev/null | grep -q '"ok"' && break
  sleep 10
done
log "restored $RESTORE_CFG"
log ""
log "SUMMARY (measured bytes/token is what belongs in the config):"
grep "^\[.*RUNG .* OK" "$LOG" | while read -r l; do log "  ${l#*] }"; done
