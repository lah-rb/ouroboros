#!/bin/bash
# Bisect the maximum ALLOCATABLE working-seat count for the batched engine,
# then run the throughput ladder only up to what proved allocatable.
#
# WHY BISECTION RATHER THAN A FIXED LADDER: as of 2026-07-26 we have zero
# evidence about what seat count actually allocates. The 128-seat config has
# never successfully loaded, and METHODS.md's assumption that "idle seats are
# ~free" is untested — each working seat may carry a static-prefix head
# (~1809 tokens), which at 128 seats would be ~231k tokens of KV before any
# real work. The max allocatable count is therefore a headline number in its
# own right: it bounds every swarm sizing decision.
#
# ACCEPTANCE IS NOT "IT LOADED". On 2026-07-25 a server came up healthy and
# then wedged on the first concurrent decode with a Metal graph failure, after
# which the sticky has_error latch made every later request fail. So each trial
# must (a) report status ok, (b) report the seat count we ASKED for, and
# (c) actually decode a concurrent wave at that width.
set -u
ROOT=/Users/lah-rb/Repos/ouroboros
LLMVP=$ROOT/llmvp
OUT=$HOME/ouroboros_artifacts/nightruns
LOG=$OUT/seat_bisect.log
TEMPLATE=$LLMVP/configs/gpt-oss-120b-a5-swarm-393k.yaml
CFG_NAME=gpt-oss-120b-a5-seatprobe
CFG=$LLMVP/configs/$CFG_NAME.yaml
RESTORE=$(cat $LLMVP/active_config.txt)
mkdir -p "$OUT"
log(){ echo "[$(date +%H:%M:%S)] $*" | tee -a "$LOG"; }

cleanup(){ log "restoring active_config -> $RESTORE"; printf '%s' "$RESTORE" > "$LLMVP/active_config.txt"; }
trap cleanup EXIT

health(){ curl -s -m 10 -X POST http://localhost:8008/graphql -H 'Content-Type: application/json' \
  -d '{"query":"{ health { status poolSize } }"}' 2>/dev/null; }

stop_server(){
  (cd "$LLMVP" && .venv/bin/python api/main.py --stop >>"$LOG" 2>&1)
  for i in $(seq 1 40); do lsof -nP -iTCP:8008 -sTCP:LISTEN >/dev/null 2>&1 || return 0; sleep 5; done
  log "  !! port still held after stop"; return 1
}

# Try to bring the server up at N seats and PROVE it decodes. 0 = ok.
try_seats(){
  local N=$1
  log "── trying $N seats"
  sed "s/^  max_concurrent_requests: .*/  max_concurrent_requests: $N/" "$TEMPLATE" > "$CFG"
  printf '%s' "$CFG_NAME" > "$LLMVP/active_config.txt"
  stop_server || return 1
  (cd "$LLMVP" && nohup .venv/bin/python api/main.py --backend >>"$LOG" 2>&1 &)
  local ok=0
  for i in $(seq 1 60); do
    local H; H=$(health)
    if echo "$H" | grep -q '"status": *"ok"'; then
      local S; S=$(echo "$H" | tr -d ' \n' | sed 's/.*"poolSize":\([0-9]*\).*/\1/')
      if [ "${S:-0}" = "$N" ]; then ok=1; break; fi
      log "  answering with poolSize=$S, wanted $N"
    fi
    sleep 10
  done
  if [ "$ok" != "1" ]; then log "  ✗ $N seats: never came up with $N seats"; return 1; fi
  log "  server up with $N seats — now proving it DECODES at that width"
  # (c) a real concurrent wave. A healthy-looking server that dies on first
  # decode is not an allocated server.
  if (cd "$ROOT" && timeout 900 uv run python dev/decode_ceiling/bench.py \
        --ladder "$N" --repeats 1 --gen 64 --out "$OUT/seatprobe_$N.json" \
        >> "$LOG" 2>&1); then
    local ERRS; ERRS=$(python3 -c "
import json;d=json.load(open('$OUT/seatprobe_$N.json'))
print(sum(r['errors'] for r in d['rows']))" 2>/dev/null || echo 999)
    if [ "${ERRS:-999}" = "0" ]; then log "  ✓ $N seats: allocated AND decoded clean"; return 0; fi
    log "  ✗ $N seats: came up but decode errored ($ERRS errors)"; return 1
  fi
  log "  ✗ $N seats: decode probe failed/timed out"; return 1
}

log "=== seat bisection: find the max allocatable width ==="
LO=0; HI=0
if try_seats 32; then LO=32; else log "!! 32 seats — the PRODUCTION width — failed. Stopping."; exit 1; fi
if try_seats 128; then LO=128; HI=128; log "128 seats works; no bisection needed"
else
  HI=128
  # binary search the boundary, 8-seat granularity
  while [ $((HI - LO)) -gt 8 ]; do
    MID=$(( (LO + HI) / 2 ))
    if try_seats "$MID"; then LO=$MID; else HI=$MID; fi
  done
fi
log "=== MAX ALLOCATABLE SEATS: $LO (first failing width ≤ $HI) ==="

# ── throughput ladder, only up to what proved allocatable ────────────────
LADDER=$(python3 -c "
lo=$LO
rungs=[n for n in (1,2,4,8,16,32,48,64,96,128) if n<=lo]
if lo not in rungs: rungs.append(lo)
print(','.join(str(n) for n in sorted(set(rungs))))")
log "=== throughput ladder at $LO seats: $LADDER ==="
if try_seats "$LO"; then
  (cd "$ROOT" && timeout 5400 uv run python dev/decode_ceiling/bench.py \
      --ladder "$LADDER" --repeats 2 >> "$OUT/decode_ceiling3.log" 2>&1)
  log "  bench exit=$?"
  (cd "$ROOT" && uv run python dev/decode_ceiling/plot.py >> "$OUT/decode_ceiling3.log" 2>&1)
  grep -E "^ *[0-9]+ |^PEAK|^batching|^sum-of-rates" "$OUT/decode_ceiling3.log" | tail -18 | tee -a "$LOG"
else
  log "!! could not re-establish $LO seats for the ladder"
fi
log "=== COMPLETE ==="
