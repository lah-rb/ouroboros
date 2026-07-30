#!/bin/bash
# Hy3 ceiling ladder, round 2 — find the wall, with a flight recorder running.
#
#   python3 dev/daemonize.py ~/ouroboros-runs/hy3_ladder2_console.log bash dev/hy3_ceiling_ladder2.sh
#
# ── WHAT ROUND 1 SETTLED, AND WHAT IT DID NOT ────────────────────────
# Round 1 ran 2048 -> 24576 and EVERY rung passed, including 24576 whose
# weights+KV compute to 116.4 GB — above iogpu.wired_limit_mb=116000. So the
# wired limit is not a hard allocation ceiling, and the KV formula is accurate
# for this arch (327,680 B/token measured vs 331,776 predicted, ratio 0.988).
#
# It did NOT find the ceiling, because the last rung passed. This one climbs
# until something breaks:
#
#   n_ctx    KV GB    weights+KV     note
#   24576      8.1      116.4        known good — also re-verifies the FSM fix
#   32768     10.7      119.0
#   49152     16.1      124.4
#   65536     21.5      129.8
#   81920     26.8      135.1        at physical (137.4 GB) — expect failure
#
# ── THE RECORDER IS THE POINT ────────────────────────────────────────
# One continuous wired-memory recording spans the whole ladder, fsync'd per
# sample, in ~/ouroboros-runs (never /tmp — the crash this watches for clears
# /tmp). If the machine dies, the last line is the high-water mark it died at,
# and rung boundaries correlate by wall-clock against this script's own log.
#
# Round 1 taught us WHY continuous sampling is necessary: at idle the server
# showed ~2.5 GB wired against ~105 GB RSS. Weights are not wired until Metal
# decodes, so a load-time snapshot measures the wrong thing entirely. Each rung
# therefore GENERATES, not merely loads.
#
# ── SECOND JOB: CONFIRM THE FSM FIX ON LIVE OUTPUT ───────────────────
# Round 1's every generation came back `:opensource>def reverse_string(s)...` —
# the family's think-tag suffix leaking into content, a SyntaxError as line 1.
# Fixed in core/fsm_labeller.py by deriving the tag tail from the spec. The
# 24576 rung re-verifies that against the real model, and the script FAILS LOUD
# if the residue is still there: a ceiling measured on corrupted extraction
# would be a wasted risk.
set -u
ROOT=/Users/lah-rb/Repos/ouroboros
RUNS=$HOME/ouroboros-runs
STAMP=$(date '+%Y%m%d-%H%M%S')
LOG=$RUNS/hy3_ladder2_$STAMP.log
CFG=hy3-reap-200b-a21
BOOT_TIMEOUT=1200
RESTORE_CFG=gpt-oss-120b-a5-swarm-524k

mkdir -p "$RUNS"
log(){ echo "[$(date '+%m-%d %H:%M:%S')] $*" | tee -a "$LOG"; sync; }
cd "$ROOT"
export PATH="$HOME/.local/bin:$PATH" PYTHONPATH="$ROOT"

bash "$ROOT/dev/preserve_run_artifacts.sh" "pre-hy3b-$STAMP" 2>&1 | tee -a "$LOG" \
  | grep -q "SAFE TO REBOOT" || { log "ABORT — archive failed"; exit 1; }

# Flight recorder for the WHOLE ladder: one continuous timeline, so a crash can
# be placed against the rung boundaries in this log by wall clock.
llmvp/.venv/bin/python dev/wired_mem_recorder.py --label hy3-ladder2 --hz 20 \
  > "$RUNS/hy3_recorder_$STAMP.out" 2>&1 &
REC_PID=$!
sleep 2
log "=== recorder pid $REC_PID -> $(ls -t "$RUNS"/wired_hy3-ladder2_*.csv | head -1)"

cleanup(){ kill -TERM "$REC_PID" 2>/dev/null || true; }
trap cleanup EXIT

stop_server(){
  local pid; pid=$(pgrep -f "[a]pi/main.py" | head -1) || true
  [ -n "${pid:-}" ] || return 0
  kill -TERM "$pid" 2>/dev/null || true   # never SIGKILL: it leaks the pool
  for _ in $(seq 1 900); do
    pgrep -f "[a]pi/main.py" >/dev/null || return 0; sleep 1
  done
  log "  ERROR server would not exit on SIGTERM"; return 1
}

rung(){
  local n=$1 budget=$2
  local slog=$RUNS/hy3b_server_${n}.log
  log ""
  log "─── RUNG n_ctx=$n (budget ${budget} GB) ───"
  stop_server || return 1
  /usr/bin/sed -i '' -e "s/^  n_ctx: .*/  n_ctx: $n/" \
    -e "s/^  kv_preflight_gb: .*/  kv_preflight_gb: $budget/" \
    "$ROOT/llmvp/configs/$CFG.yaml"
  echo -n "$CFG" > "$ROOT/llmvp/active_config.txt"
  log "  loading… (a reboot here names this rung)"
  ( cd "$ROOT/llmvp" && nohup .venv/bin/python api/main.py > "$slog" 2>&1 & )

  local deadline=$((SECONDS + BOOT_TIMEOUT)) up=0
  while [ $SECONDS -lt $deadline ]; do
    curl -s -m 5 -X POST http://localhost:8008/graphql -H 'Content-Type: application/json' \
      -d '{"query":"{ health { status } }"}' 2>/dev/null | grep -q '"ok"' && { up=1; break; }
    grep -q "Startup failed" "$slog" 2>/dev/null && break
    sleep 10
  done
  if [ "$up" != "1" ]; then
    log "  DID NOT COME UP: $(grep -m1 -o 'KV preflight REFUSED.*\|Startup failed.*' "$slog" 2>/dev/null || echo "see $slog")"
    return 1
  fi

  local kvmib bpt
  kvmib=$(grep -o "KV buffer size = *[0-9.]*" "$slog" | grep -o "[0-9.]*$" | awk '{s+=$1} END{printf "%.0f", s}')
  bpt=$(awk -v k="${kvmib:-0}" -v n="$n" 'BEGIN{printf "%.0f", (n>0? k*1048576/n : 0)}')
  log "  UP. KV $kvmib MiB -> $bpt B/token"

  # GENERATE — this is what wires the weights. A load alone does not.
  local ans txt
  ans=$(curl -s -m 600 -X POST http://localhost:8008/graphql -H 'Content-Type: application/json' \
    -d '{"query":"query($p:String!,$m:Int!){completion(request:{prompt:$p,maxTokens:$m}){text tokensGenerated}}","variables":{"p":"Write a complete Python module implementing a priority queue with insert, extract_min, decrease_key and a heap-invariant check. Include docstrings. Return only the code.","m":1200}}' 2>/dev/null)
  txt=$(echo "$ans" | sed 's/.*"text": *"//; s/", *"tokensGenerated.*//' | head -c 120)
  log "  generated: $txt"
  case "$txt" in
    :opensource*|*"opensource>"*)
      log "  !! FSM RESIDUE STILL PRESENT — the tag suffix is leaking into content."
      log "     Stopping: a ceiling measured on corrupted extraction is a wasted risk."
      return 2 ;;
  esac

  local peak
  peak=$(awk -F, '/^S,/{if($4+0>m)m=$4+0} END{printf "%.1f", m}' \
         "$(ls -t "$RUNS"/wired_hy3-ladder2_*.csv | head -1)" 2>/dev/null)
  log "RUNG $n OK  kv_mib=$kvmib b_per_tok=$bpt  peak_wired_so_far=${peak}GB"
  return 0
}

log "=== Hy3 ceiling ladder round 2 ==="
log "  wired cap $(sysctl -n iogpu.wired_limit_mb) MB · physical $(sysctl -n hw.memsize | awk '{printf "%.1f", $1/1e9}') GB"

for pair in "24576 119" "32768 122" "49152 127" "65536 133" "81920 139"; do
  set -- $pair
  rung "$1" "$2"; rc=$?
  [ $rc -eq 2 ] && { log "halting on FSM residue"; break; }
  [ $rc -ne 0 ] && { log "=== CEILING FOUND: n_ctx $1 did not come up. Last good rung is the one above. ==="; break; }
done

log ""
log "=== restoring production ==="
stop_server
echo -n "$RESTORE_CFG" > "$ROOT/llmvp/active_config.txt"
( cd "$ROOT/llmvp" && nohup .venv/bin/python api/main.py > "$RUNS/hy3b_restore.log" 2>&1 & )
for _ in $(seq 1 120); do
  curl -s -m 5 -X POST http://localhost:8008/graphql -H 'Content-Type: application/json' \
    -d '{"query":"{ health { status } }"}' 2>/dev/null | grep -q '"ok"' && break
  sleep 10
done
log "restored $RESTORE_CFG"
cleanup
log ""
log "SUMMARY:"; grep -E "RUNG .* OK|CEILING FOUND|DID NOT COME UP" "$LOG" | sed 's/^/  /' | tee -a "$LOG"
log "  recorder: $(ls -t "$RUNS"/wired_hy3-ladder2_*.csv | head -1)"
