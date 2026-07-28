#!/bin/bash
# Re-run the laguna I-Balanced arm, which the A/B chain skipped on 2026-07-28.
#
#   python3 dev/daemonize.py /tmp/tier/laguna_balanced.log bash dev/laguna_balanced_retry.sh
#
# WHY IT WAS SKIPPED. Not a model or hardware problem — a units bug in the
# config I wrote. The GGUF headers give 79.4 GiB of weights; _kv_preflight
# works in DECIMAL GB, where the same weights are 85.2 GB. kv_preflight_gb was
# set to 96 from the binary figure, so the check refused a load that needs
# 98.1 GB (85.2 weights + 12.9 KV at 192 KiB/token x 65536). Budget is now 102,
# which is still 10.8 GB below the poolside config that boots and runs stable
# at 108.9 GB. The refusal was the preflight doing exactly its job: that
# allocation would hard-reboot the machine before any tripwire reacted.
#
# Waits for the in-flight i-quality control arm to finish so the two arms never
# contend for the box, then runs the same 1h non-think mission for a like-for-
# like comparison against it.
set -u
ROOT=/Users/lah-rb/Repos/ouroboros
cd "$ROOT"
export PATH="$HOME/.local/bin:$PATH" PYTHONPATH="$ROOT"

BASE=/tmp/tier
CFG=laguna-s-2.1-apex-balanced
WORK=$BASE/ab-$CFG
SUMMARY=$BASE/laguna_ab_summary.txt
RESTORE_CFG=gpt-oss-120b-a5-swarm-524k
BOOT_TIMEOUT=900
WALL=1h

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

# Wait out the A/B chain AND any mission it is still running.
while pgrep -f "[l]aguna_recipe_ab" >/dev/null || pgrep -f "[o]uroboros.py start" >/dev/null; do
  sleep 60
done
log "=== chain drained; retrying $CFG ($WALL) ==="

stop_server || exit 1
echo -n "$CFG" > "$ROOT/llmvp/active_config.txt"
( cd "$ROOT/llmvp" && nohup .venv/bin/python api/main.py > "$BASE/${CFG}_ab_server.log" 2>&1 & )
if ! wait_healthy; then
  log "  ABORT — still not healthy in ${BOOT_TIMEOUT}s"
  grep -m1 "Startup failed" "$BASE/${CFG}_ab_server.log" | while read -r l; do log "  $l"; done
  exit 1
fi
ident=$(curl -s -m 10 -X POST http://localhost:8008/graphql \
  -H 'Content-Type: application/json' \
  -d '{"query":"{ health { requestId decodeMode } }"}' 2>/dev/null)
case "$ident" in
  *"Cannot query field"*) log "  ABORT — server lacks requestId; OLD code"; exit 1 ;;
esac
log "  server up ($CFG) $ident"

rm -rf "$WORK"; mkdir -p "$WORK"
uv run ouroboros.py mission create --mission_config game_challenge_boss \
  --working-dir "$WORK" --top-phase quality > "$BASE/ab-$CFG-create.log" 2>&1 \
  || { log "  ABORT — mission create failed"; exit 1; }
log "  mission running (backstop $WALL)"
start=$SECONDS
env OURO_LLMVP=http://localhost:8008/graphql OURO_REASONING_OFF=1 \
  uv run ouroboros.py start --working-dir "$WORK" \
    --max-wall-clock "$WALL" --trace-thinking > "$BASE/ab-$CFG-run.log" 2>&1
rc=$?; mins=$(( (SECONDS - start) / 60 ))

L=$BASE/ab-$CFG-run.log; S=$BASE/${CFG}_ab_server.log
cnt(){ local v; v=$(grep -c "$1" "$2" 2>/dev/null); echo "${v:-0}"; }
files=$(find "$WORK" -type f -not -path "*/.agent/*" -not -path "*/.venv/*" \
        -not -path "*/__pycache__/*" -not -path "*/.ruff_cache/*" 2>/dev/null | wc -l | tr -d ' ')
ok=0; bad=0
while IFS= read -r f; do
  if uv run python -c "import ast,sys;ast.parse(open(sys.argv[1]).read())" "$f" 2>/dev/null
    then ok=$((ok+1)); else bad=$((bad+1)); fi
done < <(find "$WORK" -name "*.py" -not -path "*/.venv/*" -not -path "*/__pycache__/*" 2>/dev/null)
degen=$(( $(cnt 'long-cycle' "$S") + $(cnt 'cycle period' "$S") + $(cnt 'run-length' "$S") ))
maxgen=$(grep -o 'generated=[0-9]* tok' "$S" | sed 's/[^0-9]//g' | sort -n | tail -1)
tps=$(grep -o 'speed=[0-9.]* tok/s' "$S" | sed 's/[^0-9.]//g' | sort -n \
      | awk '{v[NR]=$1} END{if(NR)printf "%.1f", v[int(NR/2)+1]}')
status=$(uv run ouroboros.py mission status --working-dir "$WORK" 2>/dev/null \
           | grep -E "^  Status:|Goals \(" | tr '\n' ' ')
line="$CFG rc=$rc ${mins}min files=$files py_ok=$ok py_fail=$bad | GUARDS degen=$degen captures=$(cnt 'Runaway capture' "$S") | WATCHDOG cancels=$(cnt 'cancelled by health watchdog' "$L") advisories=$(cnt 'advisory ceiling' "$L") busy=$(cnt 'instances are busy' "$L") | DECODE median=${tps:-?} tok/s maxgen=${maxgen:-0} | $status"
log "  done $line"
echo "$line" >> "$SUMMARY"
printf '%s\n' "$line" > "$WORK/OUTCOME"

stop_server
echo -n "$RESTORE_CFG" > "$ROOT/llmvp/active_config.txt"
( cd "$ROOT/llmvp" && nohup .venv/bin/python api/main.py > "$BASE/balanced_restore_server.log" 2>&1 & )
wait_healthy && log "restored production server" || log "WARN restore server unhealthy"
log "=== BALANCED RETRY COMPLETE ==="
cat "$SUMMARY"
