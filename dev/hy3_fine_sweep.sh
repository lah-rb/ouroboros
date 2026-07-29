#!/bin/bash
# Hy3 fine sweep 32768 -> 49152: the exact context ceiling, the kill switch,
# and the fixed config — one pass, three answers.
#
#   python3 dev/daemonize.py ~/ouroboros-runs/hy3_fine_console.log bash dev/hy3_fine_sweep.sh
#
# ── WHAT ROUND 2 LEFT ────────────────────────────────────────────────
# 32768 decoded. 49152 and 65536 LOADED, reported healthy, and failed every
# decode with llama_decode -3. 81920 rebooted the machine mid-load. So the
# functional wall sits somewhere in a 16k-token band nobody has looked inside.
# Rungs here step 4096 at a time through it.
#
# ── WHY EACH RUNG GENERATES FOUR TIMES ───────────────────────────────
# Round 2 generated ONCE per rung, which cannot exercise the new unservable
# escalation (8af50f4) — that fires on rebuild-then-fail-AGAIN, and a single
# request only ever produces the first failure. The heal runs on release and on
# acquire, so consecutive requests are what drive the cycle:
#
#   gen1  -3, instance flagged
#   (release -> heal: context rebuilt, latch cleared, _healed_since_failure)
#   gen2  -3 again -> unhealed=1
#   (heal)
#   gen3  -3 again -> unhealed=2 -> UNSERVABLE -> SIGTERM to self
#
# So a rung past the wall should end with the SERVER GONE, not with a rung
# that limps. That is the switch working, and this sweep is its first live
# test — it has only ever been exercised in unit tests.
#
# ── AND IT VALIDATES THE CONFIG FIX ──────────────────────────────────
# Round 2 ran with NO static tokens: prompt.persona_file was unset, so
# PersonaConfig failed validation and the server fell back to lightweight mode
# with no system block — which is why the model ignored reasoning_effort:no_think
# and returned empty content. Every rung below asserts the static buffer loaded
# AND that output is non-empty, so a repeat of that failure stops the sweep
# instead of being logged as OK. Round 2's rung criteria passed two rungs that
# could not decode at all; these do not.
set -u
ROOT=/Users/lah-rb/Repos/ouroboros
RUNS=$HOME/ouroboros-runs
STAMP=$(date '+%Y%m%d-%H%M%S')
LOG=$RUNS/hy3_fine_$STAMP.log
CFG=hy3-reap-200b-a21
BOOT_TIMEOUT=1200
RESTORE_CFG=gpt-oss-120b-a5-swarm-524k
GENS_PER_RUNG=4

mkdir -p "$RUNS"
log(){ echo "[$(date '+%m-%d %H:%M:%S')] $*" | tee -a "$LOG"; sync; }
cd "$ROOT"; export PATH="$HOME/.local/bin:$PATH" PYTHONPATH="$ROOT"

bash "$ROOT/dev/preserve_run_artifacts.sh" "pre-hy3fine-$STAMP" 2>&1 | tee -a "$LOG" \
  | grep -q "SAFE TO REBOOT" || { log "ABORT — archive failed"; exit 1; }

llmvp/.venv/bin/python dev/wired_mem_recorder.py --label hy3-fine --hz 20 \
  > "$RUNS/hy3_fine_recorder_$STAMP.out" 2>&1 &
REC_PID=$!; sleep 2
trap 'kill -TERM "$REC_PID" 2>/dev/null || true' EXIT
log "=== recorder pid $REC_PID"

server_up(){ curl -s -m 5 -X POST http://localhost:8008/graphql \
  -H 'Content-Type: application/json' -d '{"query":"{ health { status } }"}' \
  2>/dev/null | grep -q '"ok"'; }

stop_server(){
  local pid; pid=$(pgrep -f "[a]pi/main.py" | head -1) || true
  [ -n "${pid:-}" ] || return 0
  kill -TERM "$pid" 2>/dev/null || true   # never SIGKILL: it leaks the pool
  for _ in $(seq 1 900); do pgrep -f "[a]pi/main.py" >/dev/null || return 0; sleep 1; done
  log "  ERROR server would not exit on SIGTERM"; return 1
}

rung(){
  local n=$1 budget=$2
  local slog=$RUNS/hy3f_server_${n}.log
  log ""; log "─── RUNG n_ctx=$n (budget ${budget} GB) ───"
  stop_server || return 1
  /usr/bin/sed -i '' -e "s/^  n_ctx: .*/  n_ctx: $n/" \
    -e "s/^  kv_preflight_gb: .*/  kv_preflight_gb: $budget/" \
    "$ROOT/llmvp/configs/$CFG.yaml"
  echo -n "$CFG" > "$ROOT/llmvp/active_config.txt"
  ( cd "$ROOT/llmvp" && nohup .venv/bin/python api/main.py > "$slog" 2>&1 & )

  local deadline=$((SECONDS + BOOT_TIMEOUT)) up=0
  while [ $SECONDS -lt $deadline ]; do
    server_up && { up=1; break; }
    grep -q "Startup failed" "$slog" 2>/dev/null && break
    sleep 10
  done
  [ "$up" = "1" ] || { log "  DID NOT COME UP: $(grep -m1 -o 'KV preflight REFUSED.*\|Startup failed.*' "$slog" 2>/dev/null || echo see "$slog")"; return 1; }

  # The config fix, asserted rather than assumed.
  if grep -q "Could not load static tokens" "$slog"; then
    log "  !! STATIC TOKENS FAILED — the persona regression is back. Stopping."
    return 3
  fi
  local kvmib; kvmib=$(grep -o "KV buffer size = *[0-9.]*" "$slog" | grep -o "[0-9.]*$" | awk '{s+=$1} END{printf "%.0f", s}')
  log "  UP. KV ${kvmib} MiB · static tokens loaded"

  local ok=0 fail=0 empty=0 i
  for i in $(seq 1 $GENS_PER_RUNG); do
    if ! server_up; then
      log "  gen$i: SERVER GONE — the unservable kill switch fired (this is the wall)"
      return 2
    fi
    local ans txt
    ans=$(curl -s -m 600 -X POST http://localhost:8008/graphql -H 'Content-Type: application/json' \
      -d '{"query":"query($p:String!,$m:Int!){completion(request:{prompt:$p,maxTokens:$m}){text tokensGenerated}}","variables":{"p":"Write a Python function that merges two sorted lists. Return only the code.","m":300}}' 2>/dev/null)
    case "$ans" in
      *"code -3"*|*"Fatal Decode"*) fail=$((fail+1)); log "  gen$i: DECODE -3" ;;
      *'"text": ""'*)               empty=$((empty+1)); log "  gen$i: EMPTY (decoded, nothing extracted)" ;;
      *'"text"'*)                   ok=$((ok+1));   txt=$(echo "$ans" | sed 's/.*"text": *"//; s/", *"tokensGenerated.*//' | head -c 70); log "  gen$i: OK  $txt" ;;
      *)                            fail=$((fail+1)); log "  gen$i: no usable response: $(echo "$ans" | head -c 90)" ;;
    esac
  done

  local h; h=$(curl -s -m 5 -X POST http://localhost:8008/graphql -H 'Content-Type: application/json' \
    -d '{"query":"{ health { decodeFailures unhealedDecodeFailures unservable } }"}' 2>/dev/null | head -c 160)
  local peak; peak=$(awk -F, '/^S,/{if($4+0>m)m=$4+0} END{printf "%.1f", m}' "$(ls -t "$RUNS"/wired_hy3-fine_*.csv | head -1)" 2>/dev/null)
  log "RUNG $n  ok=$ok fail=$fail empty=$empty  kv=${kvmib}MiB peak_wired=${peak}GB  health=$h"
  [ "$ok" -gt 0 ] || return 4   # loaded but never produced usable output
  return 0
}

log "=== Hy3 fine sweep (4 generations per rung — the kill switch needs repeats) ==="
for pair in "32768 122" "36864 124" "40960 126" "45056 128" "49152 130"; do
  set -- $pair
  rung "$1" "$2"; rc=$?
  case $rc in
    0) ;;
    2) log "=== WALL at n_ctx $1 — kill switch fired. Last good rung is below. ==="; break ;;
    3) log "=== halting: config regression ==="; break ;;
    4) log "=== WALL at n_ctx $1 — loads, never produces usable output ==="; break ;;
    *) log "=== WALL at n_ctx $1 — would not come up ==="; break ;;
  esac
done

log ""; log "=== restoring production ==="
stop_server
/usr/bin/sed -i '' -e "s/^  n_ctx: .*/  n_ctx: 32768/" -e "s/^  kv_preflight_gb: .*/  kv_preflight_gb: 122/" \
  "$ROOT/llmvp/configs/$CFG.yaml"   # never leave the config at a failing rung
echo -n "$RESTORE_CFG" > "$ROOT/llmvp/active_config.txt"
( cd "$ROOT/llmvp" && nohup .venv/bin/python api/main.py > "$RUNS/hy3f_restore.log" 2>&1 & )
for _ in $(seq 1 120); do server_up && break; sleep 10; done
log "restored $RESTORE_CFG (hy3 config reset to 32768)"
log ""; log "SUMMARY:"; grep -E "^\[.*(RUNG [0-9]+ |WALL|halting)" "$LOG" | sed 's/^/  /' | tee -a "$LOG"
