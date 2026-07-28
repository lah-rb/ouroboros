#!/bin/bash
# Does laguna ever close its turn? Three arms, one archived prompt.
#
#   python3 dev/daemonize.py /tmp/tier/term_probe.log bash dev/laguna_termination_probe.sh
#
# macOS has no setsid — use daemonize.py and confirm PPID 1.
#
# THE QUESTION. The APEX replay generated 60,138 tokens and NEVER sampled a stop
# token. The deliverable — 11 files, all parsing — was complete at 32%; the
# other 68% was deliberation about code already written. Both stops are
# correctly registered (eos=2, eot=24 in the GGUF, both folded into llama.cpp's
# EOG set), so this is not a detection miss: the model never tried to stop.
#
# THE HYPOTHESIS. laguna's template is
# `<assistant><think>...</think>...</assistant>`. With thinking off we prefill
# the CLOSE tag alone and BAN token 19, so a model that drifts into deliberating
# can never emit the close tag — and `</assistant>` naturally follows closing
# thinking. Unable to close its thinking, it never closes its turn.
#
#   A  baseline        thinking off, ban {25,19}   <- known: no stop in 60,138 tok
#   B  no-think-ban    thinking off, ban {25}      <- can it close and stop?
#   C  think-on        thinking ON,  ban {25}      <- let it think properly
#
# COUNTER-EVIDENCE to weigh against a positive result: poolside runs with the
# SAME bans and format DID terminate (longest generation 15,146 tokens, well
# under budget). So the ban is not sufficient on its own — either the effect is
# quant-conditional, or poolside never entered the state. A single arm flipping
# is suggestive, not conclusive.
#
# BOUNDED. The artifact completes around 14.3k tokens, so max_tokens=20000
# discriminates cleanly: terminate below it, or run to it. Ten minutes an arm
# rather than thirty — and a thinking-on runaway (which cost 22 minutes and zero
# files on unsloth) costs ten.
set -u
ROOT=/Users/lah-rb/Repos/ouroboros
cd "$ROOT"
export PATH="$HOME/.local/bin:$PATH" PYTHONPATH="$ROOT"

BASE=/tmp/tier
OUT=$BASE/termination_probe.txt
MAXTOK=20000
BOOT_TIMEOUT=900
RESTORE_CFG=gpt-oss-120b-a5-swarm-524k
ARMS=(laguna-s-2.1-apex-nothinkban laguna-s-2.1-apex-think)

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

# Never race the arm chain for the GPU.
while pgrep -f "[o]uroboros.py start" >/dev/null; do sleep 60; done
sleep 20

: > "$OUT"
{
  echo "laguna termination probe — $(date '+%F %H:%M')"
  echo "prompt: the archived === CODE EDITOR === batch turn"
  echo "max_tokens: $MAXTOK  (artifact completes ~14.3k, so this discriminates)"
  echo
  echo "ARM A baseline (thinking off, ban {25,19}) — KNOWN:"
  echo "  60,138 tokens, no stop token ever sampled, artifact done at 32%"
  echo
} >> "$OUT"

for cfg in "${ARMS[@]}"; do
  log "--- ARM $cfg ---"
  if ! stop_server; then
    echo "ARM $cfg: SKIPPED (server would not stop)" >> "$OUT"; continue
  fi
  echo -n "$cfg" > "$ROOT/llmvp/active_config.txt"
  ( cd "$ROOT/llmvp" && nohup .venv/bin/python api/main.py \
      > "$BASE/${cfg}_server.log" 2>&1 & )
  if ! wait_healthy; then
    log "  SKIP — server not healthy"
    echo "ARM $cfg: SKIPPED (server unhealthy)" >> "$OUT"; continue
  fi
  log "  server up; replaying"

  res=$(uv run python dev/replay_kv_pressure.py --max-tokens "$MAXTOK" 2>&1)
  echo "$res" | sed 's/^/    /' | tee -a /dev/stderr >> /dev/null
  toks=$(echo "$res" | awk -F': *' '/tokensGenerated/{print $2}')
  trunc=$(echo "$res" | awk -F': *' '/^truncated/{print $2}')
  files=$(echo "$res" | awk -F': *' '/FILE blocks/{print $2}')

  # Terminated == stopped BELOW the cap. That is the whole question.
  if [ -n "${toks:-}" ] && [ "$toks" -lt "$MAXTOK" ] 2>/dev/null; then
    verdict="TERMINATED at $toks tokens — it closed its own turn"
  else
    verdict="ran to the $MAXTOK cap — still no stop"
  fi
  log "  $verdict"
  {
    echo "ARM $cfg"
    echo "  tokensGenerated: ${toks:-?}   truncated: ${trunc:-?}   FILE blocks: ${files:-?}"
    echo "  VERDICT: $verdict"
    echo
  } >> "$OUT"
done

stop_server
echo -n "$RESTORE_CFG" > "$ROOT/llmvp/active_config.txt"
( cd "$ROOT/llmvp" && nohup .venv/bin/python api/main.py > "$BASE/restore_server.log" 2>&1 & )
wait_healthy && log "restored production server" || log "WARN restore server unhealthy"

log "=== PROBE COMPLETE ==="
cat "$OUT"
