#!/bin/bash
# Does thinking mode fix the deliberation tail? Two quants, three samples each.
#
#   python3 dev/daemonize.py /tmp/tier/think_probe.log bash dev/laguna_thinking_probe.sh
#
# macOS has no setsid — use daemonize.py and confirm PPID 1.
#
# WHY. Poolside benchmarks thinking mode far higher on our exact workload
# (DeepSWE 16.5% -> 40.4%, Terminal-Bench 2.1 60.4% -> 70.2%), and every laguna
# number we hold was taken with thinking OFF. The APEX replay also showed the
# model deliberating for 68% of a 60,138-token generation AFTER its artifact was
# complete — with no <think> channel, that deliberation lands in CONTENT where
# nothing downstream can strip it.
#
# PREDICTION: with thinking on, the same batch turn returns a SMALLER, cleaner
# POST-STRIP answer for the same delivered files, because the deliberation moves
# into a channel the FSM removes.
#
# This supersedes the earlier termination probe, whose hypothesis (that the
# close-only prefill was malformed and the token-19 ban blocked termination) was
# refuted by the model's own chat template: non-think renders exactly
# `<assistant></think>` and `</think>` is prefilled, never emitted.
#
# THREE SAMPLES PER ARM, deliberately. APEX's generation lengths this session
# were 3,079 / 3,685 / 8,020 / 8,039 / 17,880 / 32,974 / 60,138 tokens — a heavy
# tail, not a stable mean. One draw per arm would be noise, and a single
# "it terminated!" would prove nothing.
#
# BASELINES to compare against (thinking OFF, already measured):
#   APEX     60,138 tok, 11 files, 230,852 post-strip chars (68% deliberation)
#   poolside terminates reliably; longest generation across a 2h run was 15,146
#
# RISK: thinking-on was catastrophic on the UNSLOTH build (zero files in 22
# minutes, two >100k-char pure-CoT runaways). Bounded at 20k tokens per sample,
# so if that recurs it costs ten minutes rather than a run.
set -u
ROOT=/Users/lah-rb/Repos/ouroboros
cd "$ROOT"
export PATH="$HOME/.local/bin:$PATH" PYTHONPATH="$ROOT"

BASE=/tmp/tier
OUT=$BASE/thinking_probe.txt
MAXTOK=20000
SAMPLES=3
BOOT_TIMEOUT=900
RESTORE_CFG=gpt-oss-120b-a5-swarm-524k
ARMS=(laguna-s-2.1-apex-think laguna-s-2.1-poolside-think)

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
  log "  ERROR server did not exit on SIGTERM after 900s"
  return 1
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

while pgrep -f "[o]uroboros.py start" >/dev/null; do sleep 60; done
sleep 20

{
  echo "laguna THINKING-mode probe — $(date '+%F %H:%M')"
  echo "prompt: the archived === CODE EDITOR === batch turn"
  echo "max_tokens: $MAXTOK per sample, $SAMPLES samples per arm"
  echo
  echo "BASELINES (thinking OFF, measured):"
  echo "  APEX      60,138 tok | 11 files | 230,852 post-strip chars (68% deliberation)"
  echo "  poolside  terminates; longest generation across 2h was 15,146 tok"
  echo
} > "$OUT"

for cfg in "${ARMS[@]}"; do
  log "--- ARM $cfg ---"
  if ! stop_server; then
    echo "ARM $cfg: SKIPPED (server would not stop)" >> "$OUT"; continue
  fi
  echo -n "$cfg" > "$ROOT/llmvp/active_config.txt"
  ( cd "$ROOT/llmvp" && nohup .venv/bin/python api/main.py \
      > "$BASE/${cfg}_server.log" 2>&1 & )
  if ! wait_healthy; then
    log "  SKIP — server not healthy within ${BOOT_TIMEOUT}s"
    echo "ARM $cfg: SKIPPED (server unhealthy)" >> "$OUT"; continue
  fi
  log "  server up"
  echo "ARM $cfg" >> "$OUT"

  for i in $(seq 1 "$SAMPLES"); do
    res=$(uv run python dev/replay_kv_pressure.py --max-tokens "$MAXTOK" 2>&1)
    toks=$(echo "$res"  | awk -F': *' '/tokensGenerated/{print $2}')
    trunc=$(echo "$res" | awk -F': *' '/^truncated/{print $2}')
    files=$(echo "$res" | awk -F': *' '/FILE blocks/{print $2}')
    chars=$(echo "$res" | awk -F': *' '/response chars/{print $2}')
    if [ -n "${toks:-}" ] && [ "$toks" -lt "$MAXTOK" ] 2>/dev/null; then
      v="terminated"
    else
      v="ran to cap"
    fi
    log "  sample $i: ${toks:-?} tok, ${files:-?} files, ${chars:-?} chars — $v"
    printf '  sample %s: tokens=%-6s files=%-3s post_strip_chars=%-8s truncated=%-5s %s\n' \
      "$i" "${toks:-?}" "${files:-?}" "${chars:-?}" "${trunc:-?}" "$v" >> "$OUT"
  done
  echo >> "$OUT"
done

stop_server
echo -n "$RESTORE_CFG" > "$ROOT/llmvp/active_config.txt"
( cd "$ROOT/llmvp" && nohup .venv/bin/python api/main.py > "$BASE/restore_server.log" 2>&1 & )
wait_healthy && log "restored production server" || log "WARN restore server unhealthy"

log "=== THINKING PROBE COMPLETE ==="
cat "$OUT"
