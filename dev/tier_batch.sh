#!/bin/bash
# SUPERSEDED 2026-07-29 by `ouroboros.py tier` (agent/tier/). Use that:
#
#   ouroboros.py tier run --models glm-4.7-flash,gemma-4-26b-a4b
#   ouroboros.py tier status
#   ouroboros.py tier skip | stop | force-stop
#
# Kept because it is the artifact that produced run tier_20260729-010118, and
# because its comments are where the lessons were first written down. The Python
# version fixes three things this one taught, each by costing something:
#   * SKIP was the only verb — ending the chain after a GOOD arm meant killing
#     the script mid-flight and cleaning up orphans by hand
#   * stop_server waited 900s and then gave up, so an abandoned generation
#     blocking uvicorn's shutdown cost the gemma-4-26b arm entirely
#   * the heartbeat's degeneration count grepped three patterns and reported 5
#     for a single abort
#
# ── ORIGINAL HEADER ──────────────────────────────────────────────────
# Tier batch — serial arms, per-arm staging, judge-while-the-next-one-runs.
#
#   python3 dev/daemonize.py ~/ouroboros-runs/tier_driver.log bash dev/tier_batch.sh
#   python3 dev/daemonize.py ~/ouroboros-runs/tier_driver.log bash dev/tier_batch.sh glm-4.7-flash gemma-4-26b-a4b
#
# Verify before walking away: `ps -o pid,ppid -p $(pgrep -f tier_batch)` must
# show PPID 1. Background jobs started from a Claude Code session die when that
# session exits — this has stranded a PAUSED mission and left the server DOWN.
# `setsid` DOES NOT EXIST ON MACOS; use daemonize.py.
#
# ── WHAT IS DIFFERENT FROM overnight_tier_run.sh ─────────────────────
# 1. STAGING IS PER-ARM AND IMMEDIATE. The old script left artifacts in
#    /tmp/tier and staged after the batch. TIER_RUBRIC v1.0 judges one artifact
#    alone, so judging can start the moment an arm finishes and overlap the next
#    arm's 2h — but only if the artifact is stripped and moved somewhere durable
#    before the next arm touches /tmp. That is the whole reason judging no
#    longer adds to the batch's wall clock.
# 2. NEW CONFIGS RUN FIRST, and each arm writes a HEARTBEAT every 60s so a
#    15-minute check is one file read rather than a shell expedition. glm-4.7,
#    gemma-4-26b-a4b and laguna-xs have never booted; a bad leg gets skipped
#    rather than burning 2h.
# 3. SKIP SENTINEL. `touch $BASE/SKIP` ends the current arm cleanly and moves to
#    the next. Do not pkill by hand — the LLMVP pool leaks on hard-kill.
# 4. REASONING IS NOT FORCED OFF. The old run pinned OURO_REASONING_OFF=1; the
#    tier stage says "settings optimized to the model's full advantage", and
#    forcing reasoning off handicaps every thinking model in the roster. Each
#    config's own settings govern. This breaks comparability with the
#    2026-07-27 /50 scores, which the rubric already prohibits anyway.
# 5. NO run_until. game_challenge_tier deliberately does not force completion —
#    the instrument is a cheap 2h slice (TIER_RUBRIC v1.0 §1).
set -u
ROOT=/Users/lah-rb/Repos/ouroboros
cd "$ROOT"
export PATH="$HOME/.local/bin:$PATH" PYTHONPATH="$ROOT"

STAMP=$(date '+%Y%m%d-%H%M%S')
RUNS=$HOME/ouroboros-runs
BASE=$RUNS/tier_$STAMP           # durable: logs, heartbeats, manifest
STAGE=$BASE/staged               # durable: stripped artifacts for judging
WORKROOT=/tmp/tier               # scratch: cleared per arm
MISSION=game_challenge_tier
WALL=2h                          # per-arm backstop
BATCH_BUDGET_H=${BATCH_BUDGET_H:-11}   # stop STARTING arms past this
BOOT_TIMEOUT=1200                # gemma-4/step-3.7 class weights load slowly
RESTORE_CFG=gpt-oss-120b-a5-swarm-524k

# New and unbooted FIRST (2026-07-29): a format bug should surface at 00:15,
# not at 03:00. Then the reference and the incumbent champion, then the field.
ARMS=(
  glm-4.7-flash                # NEW — first glm4 family, MLA/deepseek2 arch
  gemma-4-26b-a4b              # NEW — first MoE gemma; KV formula over-predicts
  laguna-xs-2.1                # NEW — first UNCONFOUNDED I-Balanced laguna
  gpt-oss-120b-a5-swarm-524k   # reference
  step37-flash-196b-a11        # incumbent champion
  gemma-4-31b
  laguna-s-2.1-apex
  mistral-medium-3.5-128b
  qwen3.5-122b-a10
  hy3-reap-200b-a21
  qwen3.6-35b-a3
  qwen3-next-coder-80b-a3
  olmo-3.1-32b-think
  olmo-3.1-32b-instruct
  qwen3.6-27b
  devstral-2-small-24b
)
[ $# -gt 0 ] && ARMS=("$@")

mkdir -p "$BASE" "$STAGE"
LOG=$BASE/batch.log
log(){ echo "[$(date '+%m-%d %H:%M:%S')] $*" | tee -a "$LOG"; sync; }

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

PRUNE=( -not -path "*/.agent/*" -not -path "*/.venv/*" -not -path "*/__pycache__/*"
        -not -path "*/.ruff_cache/*" -not -path "*/.git/*" -not -name "OUTCOME" )

# The 15-minute check reads THIS, and nothing else.
heartbeat(){
  local cfg=$1 work=$2 slog=$3 rlog=$4 started=$5 idx=$6
  local hb=$BASE/HEARTBEAT.txt
  local mins=$(( (SECONDS - started) / 60 ))
  local files; files=$(find "$work" -type f "${PRUNE[@]}" 2>/dev/null | wc -l | tr -d ' ')
  local py;    py=$(find "$work" -name "*.py" "${PRUNE[@]}" 2>/dev/null | wc -l | tr -d ' ')
  local health; health=$(curl -s -m 5 -X POST http://localhost:8008/graphql \
    -H 'Content-Type: application/json' \
    -d '{"query":"{ health { status decodeFailures unhealedDecodeFailures unservable tokensGenerated } }"}' \
    2>/dev/null | head -c 240)
  local degen; degen=$(grep -ciE "degenerat|long-cycle|repetition guard" "$slog" 2>/dev/null || echo 0)
  local dec3;  dec3=$(grep -c "code -3" "$slog" 2>/dev/null || echo 0)
  {
    echo "arm            : $idx/${#ARMS[@]}  $cfg"
    echo "elapsed_min    : $mins   (backstop $WALL)"
    echo "authored_files : $files  (py $py)"
    echo "server_health  : ${health:-UNREACHABLE}"
    echo "server_degen   : $degen   decode_-3: $dec3"
    echo "stage_dir      : $BASE"
    echo "skip_with      : touch $BASE/SKIP"
    echo "--- mission status ---"
    timeout 60 uv run ouroboros.py mission status --working-dir "$work" 2>/dev/null \
      | grep -E "Status:|Goals|Cycle|Phase" | head -8
    echo "--- run.log tail ---"
    tail -n 6 "$rlog" 2>/dev/null
  } > "$hb.tmp" 2>&1
  mv "$hb.tmp" "$hb"
  cp "$hb" "$BASE/heartbeat_${cfg}.txt"
}

# Strip, blind-scan, and move somewhere /tmp cannot eat. Runs the INSTANT an arm
# ends so judging can begin against a durable copy while the next arm boots.
stage_arm(){
  local cfg=$1 work=$2 idx=$3
  local dest=$STAGE/arm$(printf '%02d' "$idx")     # opaque: the dir name must
  local slog=$BASE/stage_${cfg}.log                # not identify the model
  if [ ! -d "$work" ] || [ -z "$(find "$work" -type f "${PRUNE[@]}" 2>/dev/null | head -1)" ]; then
    log "  STAGE SKIPPED — no authored files (tier 3 candidate)"
    echo "$dest  $cfg  NO_ARTIFACT" >> "$BASE/MANIFEST.txt"
    return 0
  fi
  if uv run python dev/blind_panel/stage.py --judges 1 --out "$dest" "$work" > "$slog" 2>&1; then
    rm -f "$dest/KEY.json"        # redundant here, and it names the config
    echo "$dest  $cfg  staged" >> "$BASE/MANIFEST.txt"
    if grep -q "POSSIBLE IDENTIFIER LEAKS" "$slog"; then
      log "  !! STAGED WITH LEAKS — review $slog before judging"
    else
      log "  staged -> $dest (identifier scan clean)"
    fi
  else
    log "  STAGE FAILED — see $slog"
    echo "$dest  $cfg  STAGE_FAILED" >> "$BASE/MANIFEST.txt"
  fi
}

log "=== tier batch v1 · ${#ARMS[@]} arms · $WALL each · budget ${BATCH_BUDGET_H}h ==="
log "    mission=$MISSION  rubric=TIER_RUBRIC_v1.0  stage=$STAGE"
: > "$BASE/MANIFEST.txt"
BATCH_START=$SECONDS
IDX=0

for cfg in "${ARMS[@]}"; do
  IDX=$((IDX+1))
  elapsed_h=$(( (SECONDS - BATCH_START) / 3600 ))
  if [ "$elapsed_h" -ge "$BATCH_BUDGET_H" ]; then
    log "=== BUDGET REACHED (${elapsed_h}h) — not starting $cfg. Remaining: ${ARMS[*]:$((IDX-1))}"
    break
  fi

  WORK=$WORKROOT/$cfg
  SLOG=$BASE/${cfg}_server.log
  RLOG=$BASE/${cfg}_run.log
  rm -rf "$WORK"; mkdir -p "$WORK"
  log ""; log "─── ARM $IDX/${#ARMS[@]}: $cfg ───"

  stop_server || { log "  SKIP — server would not stop"; continue; }
  echo -n "$cfg" > "$ROOT/llmvp/active_config.txt"
  ( cd "$ROOT/llmvp" && nohup .venv/bin/python api/main.py > "$SLOG" 2>&1 & )

  deadline=$((SECONDS + BOOT_TIMEOUT)); up=0
  while [ $SECONDS -lt $deadline ]; do
    server_up && { up=1; break; }
    grep -q "Startup failed" "$SLOG" 2>/dev/null && break
    sleep 10
  done
  if [ "$up" != "1" ]; then
    reason=$(grep -m1 -o 'KV preflight REFUSED.*\|Startup failed.*' "$SLOG" 2>/dev/null | head -c 120)
    log "  UNSUPPORTED — no healthy server in ${BOOT_TIMEOUT}s: ${reason:-see $SLOG}"
    printf 'UNSUPPORTED\n%s\n' "${reason:-boot timeout}" > "$WORK/OUTCOME"
    echo "-  $cfg  UNSUPPORTED" >> "$BASE/MANIFEST.txt"
    continue
  fi
  grep -q "Could not load static tokens" "$SLOG" && log "  !! static tokens FAILED — no system block this arm"
  log "  server up"

  if ! timeout 900 uv run ouroboros.py mission create --mission_config "$MISSION" \
        --working-dir "$WORK" --top-phase quality > "$BASE/${cfg}_create.log" 2>&1; then
    log "  TIER 3 (create) — mission create failed"
    echo "TIER3_CREATE_FAILED" > "$WORK/OUTCOME"
    echo "-  $cfg  TIER3_CREATE" >> "$BASE/MANIFEST.txt"
    continue
  fi

  log "  mission running"
  ARM_START=$SECONDS
  env OURO_LLMVP=http://localhost:8008/graphql \
    uv run ouroboros.py start --working-dir "$WORK" \
      --max-wall-clock "$WALL" --trace-thinking > "$RLOG" 2>&1 &
  RUNPID=$!

  while kill -0 "$RUNPID" 2>/dev/null; do
    heartbeat "$cfg" "$WORK" "$SLOG" "$RLOG" "$ARM_START" "$IDX"
    if [ -f "$BASE/SKIP" ]; then
      rm -f "$BASE/SKIP"
      log "  SKIP requested at $(( (SECONDS-ARM_START)/60 ))min — ending this arm"
      kill -TERM "$RUNPID" 2>/dev/null || true
      pkill -TERM -f "ouroboros.py start --working-dir $WORK" 2>/dev/null || true
      sleep 20
      break
    fi
    sleep 60
  done
  wait "$RUNPID" 2>/dev/null; rc=$?
  mins=$(( (SECONDS - ARM_START) / 60 ))

  files=$(find "$WORK" -type f "${PRUNE[@]}" 2>/dev/null | wc -l | tr -d ' ')
  ok=0; bad=0
  while IFS= read -r f; do
    if uv run python -c "import ast,sys;ast.parse(open(sys.argv[1]).read())" "$f" 2>/dev/null
      then ok=$((ok+1)); else bad=$((bad+1)); fi
  done < <(find "$WORK" -name "*.py" "${PRUNE[@]}" 2>/dev/null)
  status=$(timeout 60 uv run ouroboros.py mission status --working-dir "$WORK" 2>/dev/null \
             | grep -E "^  Status:|Goals \(" | tr '\n' ' ')
  log "  done rc=$rc ${mins}min files=$files py_ok=$ok py_fail=$bad | $status"
  printf 'rc=%s\nminutes=%s\nfiles=%s\npy_ok=%s\npy_fail=%s\n%s\n' \
    "$rc" "$mins" "$files" "$ok" "$bad" "$status" > "$WORK/OUTCOME"

  stage_arm "$cfg" "$WORK" "$IDX"
  cp -a "$WORK/.agent" "$BASE/${cfg}_agent" 2>/dev/null || true   # telemetry for
done                                                              # the OBSERVED half

log ""; log "=== restoring production ==="
stop_server
echo -n "$RESTORE_CFG" > "$ROOT/llmvp/active_config.txt"
( cd "$ROOT/llmvp" && nohup .venv/bin/python api/main.py > "$BASE/restore_server.log" 2>&1 & )
for _ in $(seq 1 150); do server_up && break; sleep 10; done
server_up && log "restored $RESTORE_CFG" || log "WARN restore server unhealthy"

log ""; log "=== MANIFEST (judge these; the map lives HERE, not in the staged tree) ==="
cat "$BASE/MANIFEST.txt" | tee -a "$LOG"
log "=== BATCH COMPLETE ==="
