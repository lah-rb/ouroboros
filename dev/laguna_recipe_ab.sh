#!/bin/bash
# Laguna APEX recipe A/B: I-Balanced vs i-quality, both on the FIXED watchdog.
#
#   python3 dev/daemonize.py /tmp/tier/laguna_ab.log bash dev/laguna_recipe_ab.sh
#
# macOS has no setsid — use daemonize.py and confirm PPID 1 before walking away.
#
# ── WHY BOTH ARMS RERUN ──────────────────────────────────────────────
# The three qwen models that improved on 2026-07-28 are all APEX I-Balanced;
# laguna is APEX i-quality. That is the axis to test. But the i-quality data we
# hold is NOT a usable control: on 2026-07-27/28 the agent's health watchdog
# cancelled laguna generations at a blind 49,152-token ceiling — the
# apex-maxctx arm died on its FIRST inference and terminated after 0 cycles of
# a 2h budget. Comparing I-Balanced-now against i-quality-then would confound
# the recipe with the watchdog fix (4c103c9). So both arms rerun here.
#
# Both configs are `thinking: false` already, so this is a clean non-think A/B
# on ONE axis, the quant:
#
#   laguna-s-2.1-apex-balanced   APEX I-Balanced   5.800 bpw   79.4 GiB
#   laguna-s-2.1-apex            APEX i-quality    5.031 bpw   68.9 GiB
#
# ── WHY 1h, AND WHAT THAT COSTS THE POOLSIDE COMPARISON ──────────────
# 1h makes these DIRECTLY comparable to the 2026-07-28 qwen arms — laguna has
# never been measured against qwen on an equal clock, and that is the more
# valuable axis. The tells also surface early: the 2h poolside run hit its
# degeneration, its force-window and three of its four cancels inside hour one.
#
# Poolside is deliberately NOT rerun; it is compared from the existing 2026-07-28
# data. State the methodology gap whenever that comparison is made:
#   * poolside ran 2h, these run 1h — its 7 files / 6-of-42 goals are a
#     TWO-hour figure and must not be read against a one-hour one directly;
#   * poolside ran on the BROKEN watchdog: 4 cancels (2 of them collateral,
#     reading another request's token count) and one KV force-window. Its
#     result is a floor, not a like-for-like measurement;
#   * poolside is a different quant family entirely (Q4_K_M, ~7.0 GiB/token hot
#     path) — the widest gap of the three, not a near neighbour.
# A poolside rerun on fixed infra is cheap if the comparison ends up load-bearing.
#
# ── WHAT TO JUDGE ────────────────────────────────────────────────────
# NOT throughput. The headers already answer that and answer it against
# I-Balanced: its per-token hot path is 6.92 GiB against i-quality's 5.86
# (+18%), because the balanced recipe promotes the whole IQ4_XS expert tier to
# Q5_K and puts BF16 in the dense path. Expect ~25 tok/s vs ~30. That is the
# price; the question is what it buys.
#
# Judge on: (1) artifact quality, blind, against the i-quality arm; (2) whether
# the paragraph-orbit degeneration survives the higher-precision experts —
# poolside hit one that night, caught by the long-cycle guard at 4,096 tokens
# with the n-gram ratio named. Degeneration counts below come from the SERVER's
# guards, which is the reliable place to read them.
#
# CONFOUND: only the Uncensored finetune has the balanced recipe applied, so
# arm 1 varies recipe AND finetune. Refusal/tone/instruction-following
# differences belong to the finetune. A base-model I-Balanced build, if one
# appears, is what makes this clean.
set -u
ROOT=/Users/lah-rb/Repos/ouroboros
cd "$ROOT"
export PATH="$HOME/.local/bin:$PATH" PYTHONPATH="$ROOT"

BASE=/tmp/tier
SUMMARY=$BASE/laguna_ab_summary.txt
RESTORE_CFG=gpt-oss-120b-a5-swarm-524k
BOOT_TIMEOUT=900
WALL=1h   # matches the 2026-07-28 qwen arms; see the header on poolside

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
  stop_server || { log "  SKIP $cfg — previous server would not stop"; return 1; }
  echo -n "$cfg" > "$ROOT/llmvp/active_config.txt"
  ( cd "$ROOT/llmvp" && nohup .venv/bin/python api/main.py \
      > "$BASE/${cfg}_ab_server.log" 2>&1 & )
  wait_healthy || { log "  SKIP $cfg — server not healthy in ${BOOT_TIMEOUT}s"; return 1; }
  # The watchdog fix is the premise of this comparison. Without requestId the
  # agent silently reverts to the blind ceiling and the arm measures the old
  # world again — exactly the confound this chain exists to remove.
  local ident
  ident=$(curl -s -m 10 -X POST http://localhost:8008/graphql \
    -H 'Content-Type: application/json' \
    -d '{"query":"{ health { requestId decodeMode } }"}' 2>/dev/null)
  case "$ident" in
    *"Cannot query field"*)
      log "  SKIP $cfg — server lacks requestId; it is running OLD code"; return 1 ;;
  esac
  log "  server up ($cfg) $ident"
}

arm(){
  local cfg=$1 work=$BASE/ab-$1
  log "=== $cfg ($WALL) ==="
  boot "$cfg" || { echo "$cfg SKIPPED (boot)" >> "$SUMMARY"; return 1; }
  rm -rf "$work"; mkdir -p "$work"
  if ! uv run ouroboros.py mission create --mission_config game_challenge_boss \
        --working-dir "$work" --top-phase quality > "$BASE/ab-$cfg-create.log" 2>&1; then
    log "  SKIP — mission create failed"; echo "$cfg SKIPPED (create)" >> "$SUMMARY"; return 1
  fi
  log "  mission running (backstop $WALL)"
  local start=$SECONDS
  env OURO_LLMVP=http://localhost:8008/graphql OURO_REASONING_OFF=1 \
    uv run ouroboros.py start --working-dir "$work" \
      --max-wall-clock "$WALL" --trace-thinking > "$BASE/ab-$cfg-run.log" 2>&1
  local rc=$? mins=$(( (SECONDS - start) / 60 ))

  local L=$BASE/ab-$cfg-run.log S=$BASE/${cfg}_ab_server.log
  cnt(){ local v; v=$(grep -c "$1" "$2" 2>/dev/null); echo "${v:-0}"; }
  local files ok=0 bad=0
  files=$(find "$work" -type f -not -path "*/.agent/*" -not -path "*/.venv/*" \
          -not -path "*/__pycache__/*" -not -path "*/.ruff_cache/*" 2>/dev/null | wc -l | tr -d ' ')
  while IFS= read -r f; do
    if uv run python -c "import ast,sys;ast.parse(open(sys.argv[1]).read())" "$f" 2>/dev/null
      then ok=$((ok+1)); else bad=$((bad+1)); fi
  done < <(find "$work" -name "*.py" -not -path "*/.venv/*" -not -path "*/__pycache__/*" 2>/dev/null)
  # Degeneration from the SERVER's guards (authoritative), not the agent's view.
  local degen tps maxgen status
  degen=$(( $(cnt 'long-cycle' "$S") + $(cnt 'cycle period' "$S") + $(cnt 'run-length' "$S") ))
  maxgen=$(grep -o 'generated=[0-9]* tok' "$S" | sed 's/[^0-9]//g' | sort -n | tail -1)
  tps=$(grep -o 'speed=[0-9.]* tok/s' "$S" | sed 's/[^0-9.]//g' | sort -n \
        | awk '{v[NR]=$1} END{if(NR)printf "%.1f", v[int(NR/2)+1]}')
  status=$(uv run ouroboros.py mission status --working-dir "$work" 2>/dev/null \
             | grep -E "^  Status:|Goals \(" | tr '\n' ' ')
  local line="$cfg rc=$rc ${mins}min files=$files py_ok=$ok py_fail=$bad | GUARDS degen=$degen captures=$(cnt 'Runaway capture' "$S") | WATCHDOG cancels=$(cnt 'cancelled by health watchdog' "$L") advisories=$(cnt 'advisory ceiling' "$L") busy=$(cnt 'instances are busy' "$L") | DECODE median=${tps:-?} tok/s maxgen=${maxgen:-0} | $status"
  log "  done $line"
  echo "$line" >> "$SUMMARY"
  printf '%s\n' "$line" > "$work/OUTCOME"
}

while pgrep -f "[o]uroboros.py start" >/dev/null; do sleep 60; done
: > "$SUMMARY"
echo "laguna recipe A/B $(date '+%F %H:%M') — both arms on the fixed watchdog" >> "$SUMMARY"

arm laguna-s-2.1-apex-balanced   # the new recipe (+ uncensored finetune)
arm laguna-s-2.1-apex            # i-quality control, rerun on fixed infra

stop_server
echo -n "$RESTORE_CFG" > "$ROOT/llmvp/active_config.txt"
( cd "$ROOT/llmvp" && nohup .venv/bin/python api/main.py > "$BASE/ab_restore_server.log" 2>&1 & )
wait_healthy && log "restored production server" || log "WARN restore server unhealthy"

log "=== LAGUNA RECIPE A/B COMPLETE ==="
cat "$SUMMARY"
