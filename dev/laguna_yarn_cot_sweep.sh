#!/bin/bash
# YaRN x CoT sweep — does the RoPE stretch explain laguna's unconverged thinking?
#
#   python3 dev/daemonize.py /tmp/tier/yarn_sweep.log bash dev/laguna_yarn_cot_sweep.sh
#
# macOS has no setsid — use daemonize.py and confirm PPID 1 before walking away.
#
# ── THE QUESTION ─────────────────────────────────────────────────────
# On 2026-07-27 the CoT difficulty ladder returned VERDICT (b): laguna scales
# its reasoning with the ASK, not the budget. Trivial prompts answer in 52
# tokens; "hard" (one complete module) converges at ~11.3k; "very hard" (six
# files in one turn) ran to the 20k cap TWICE with zero files emitted.
#
# One untested explanation is geometric rather than behavioural. The GGUF asks
# for a 128x YaRN stretch (original_context_length 8192 -> 1,048,576, factor
# 128.0) and we run n_ctx 65536, where the real stretch is 8x. Every position
# therefore sits in a band the model was tuned to treat as ~16x more distant
# than it is. It also declares yarn_attn_factor 1.4852 while poolside's own
# guidance for this model is 1.0. Until the RoPE plumbing landed (a8ccaeb) we
# could not set either, or even log which was in force.
#
# Three arms, one variable each, i-quality weights throughout (the balanced
# quant scored 18/50 blind against i-quality's 35 and its weights are gone):
#
#   1. think        GGUF default        yarn_attn_factor 1.4852, freq_scale 1/128
#   2. think-yarn1  poolside's value    yarn_attn_factor 1.0
#   3. think-yarn8x stretch = actual    rope_freq_scale 0.125 (1/8)
#
# ── WHAT COUNTS AS AN ANSWER ─────────────────────────────────────────
# The ladder's own verdict line, plus the "very hard" rung's ran-to-cap count.
# If an arm converges "very hard" under the cap where arm 1 did not, the
# geometry was the problem. If all three run to cap, it is not, and the lever
# is reasoning_budget or decomposing the batch.
#
# max_tokens stays at 20000 so arm 1 is directly comparable to the 2026-07-27
# ladder — that run is effectively a fourth sample of the same condition.
#
# ── DEGENERATION IS THE SECOND READOUT ───────────────────────────────
# The i-quality 1h agent run took THREE long-cycle aborts (ratios 0.0686 and
# 0.1021 among them), all on temp-0.4 code-emission steps. The ladder calls the
# completion endpoint directly, so the server guards are the only detector in
# play and their captures are counted per arm below. If degeneration tracks a
# yarn arm, that is a finding; if it is flat across all three, the next lever is
# a temperature floor (laguna's own general-use recommendation is 0.7, and all
# three observed aborts sat at 0.4 — below it).
set -u
ROOT=/Users/lah-rb/Repos/ouroboros
cd "$ROOT"
export PATH="$HOME/.local/bin:$PATH" PYTHONPATH="$ROOT"

BASE=/tmp/tier
SUMMARY=$BASE/yarn_sweep_summary.txt
RESTORE_CFG=gpt-oss-120b-a5-swarm-524k
BOOT_TIMEOUT=900
MAXTOK=20000
SAMPLES=2

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

arm(){
  local cfg=$1 label=$2
  log "=== $label ($cfg) ==="
  stop_server || { echo "$label SKIPPED (stop)" >> "$SUMMARY"; return 1; }
  echo -n "$cfg" > "$ROOT/llmvp/active_config.txt"
  ( cd "$ROOT/llmvp" && nohup .venv/bin/python api/main.py \
      > "$BASE/${cfg}_yarn_server.log" 2>&1 & )
  if ! wait_healthy; then
    log "  SKIP — not healthy in ${BOOT_TIMEOUT}s"
    grep -m1 "Startup failed" "$BASE/${cfg}_yarn_server.log" | while read -r l; do log "  $l"; done
    echo "$label SKIPPED (boot)" >> "$SUMMARY"; return 1
  fi
  # The whole point of the sweep is WHICH RoPE values are in force. The backend
  # logs its resolved kwargs at load; echo them so each arm's record carries the
  # geometry it actually ran under rather than the one the config intended.
  log "  server up. RoPE in force:"
  grep -iE "rope|yarn" "$BASE/${cfg}_yarn_server.log" | grep -vi "warning" | head -3 \
    | while read -r l; do log "    ${l#*INFO }"; done

  local out=$BASE/cot_ladder_${label}.txt
  uv run python dev/laguna_cot_difficulty.py --max-tokens $MAXTOK --samples $SAMPLES \
    > "$out" 2>&1
  tail -9 "$out" | while read -r l; do log "    $l"; done

  local S=$BASE/${cfg}_yarn_server.log
  cnt(){ local v; v=$(grep -c "$1" "$2" 2>/dev/null); echo "${v:-0}"; }
  local verdict capped med
  verdict=$(grep -oE "VERDICT \([ab]\)" "$out" | head -1)
  capped=$(grep -E "^  very hard" "$out" | grep -oE "ran-to-cap [0-9]+/[0-9]+")
  med=$(grep -o 'speed=[0-9.]* tok/s' "$S" 2>/dev/null | sed 's/[^0-9.]//g' | sort -n \
        | awk '{v[NR]=$1} END{if(NR)printf "%.1f", v[int(NR/2)+1]}')
  local line="$label | ${verdict:-no-verdict} | very-hard ${capped:-?} | GUARDS captures=$(cnt 'Runaway capture' "$S") longcycle=$(cnt 'long-cycle' "$S") | DECODE ${med:-?} tok/s"
  log "  done $line"
  echo "$line" >> "$SUMMARY"
  # Per-rung medians, so the SHAPE of the curve is in the record, not just the verdict.
  grep -E "^  (trivial|easy|moderate|hard|very hard)" "$out" >> "$SUMMARY"
  echo "" >> "$SUMMARY"
}

while pgrep -f "[o]uroboros.py start" >/dev/null; do sleep 60; done
: > "$SUMMARY"
echo "laguna YaRN x CoT sweep $(date '+%F %H:%M') — i-quality weights, thinking on" >> "$SUMMARY"
echo "max_tokens=$MAXTOK samples=$SAMPLES; arm 1 is comparable to the 2026-07-27 ladder" >> "$SUMMARY"
echo "" >> "$SUMMARY"

arm laguna-s-2.1-apex-think        gguf-default
arm laguna-s-2.1-apex-think-yarn1  attn-factor-1.0
arm laguna-s-2.1-apex-think-yarn8x freq-scale-8x

stop_server
echo -n "$RESTORE_CFG" > "$ROOT/llmvp/active_config.txt"
( cd "$ROOT/llmvp" && nohup .venv/bin/python api/main.py > "$BASE/yarn_restore_server.log" 2>&1 & )
wait_healthy && log "restored production server" || log "WARN restore server unhealthy"

log "=== YARN SWEEP COMPLETE ==="
cat "$SUMMARY"
