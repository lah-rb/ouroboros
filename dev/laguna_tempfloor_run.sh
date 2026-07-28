#!/bin/bash
# Does a 0.7 temperature floor stop laguna's degeneration?
#
#   python3 dev/daemonize.py /tmp/tier/tempfloor.log bash dev/laguna_tempfloor_run.sh
#
# macOS has no setsid — use daemonize.py and confirm PPID 1 before walking away.
#
# ── WHY THIS ARM, AND WHY IT IS THE LAST HYPOTHESIS STANDING ─────────
# The 2026-07-28 i-quality 1h run took THREE long-cycle aborts, every one on a
# short mechanical turn sampled at temp 0.4 (generate_new_symbol x2,
# generate_rewrite x1) while its 18,466-token batch and a 20,480-token
# generation ran clean. So the failure tracks the LOW-TEMPERATURE band, not
# generation length.
#
# The yarn sweep ruled out the geometric explanation first: three RoPE arms all
# returned VERDICT (b), and the overrides made convergence worse (freq-scale-8x
# took `hard` from 2/2 converging to 2/2 capped). That leaves sampling.
#
# 0.7 is poolside's own general-use recommendation for laguna, so this asks
# whether honouring the vendor's operating point removes the failure — not
# whether an arbitrary knob suppresses a symptom. The three qwen configs already
# carry temperature_floor: 0.7 and took zero aborts across 618 generations.
#
# ── READ IT ON TWO AXES, NOT ONE ─────────────────────────────────────
# The floor lifts ~190 calls per run that currently sample at 0.0 —
# investigate, plan_interaction, conclude, evaluate_outcome. None of those ever
# degenerated. So:
#
#   degenerations 3 -> 0 AND artifact holds  = the floor is right, ship it
#   degenerations 3 -> 0 AND artifact worse  = trading diagnosis quality for
#                                              stability; scope the floor to the
#                                              code-emission steps instead
#   degenerations unchanged                  = sampling is not the mechanism
#                                              either, and the next lever is the
#                                              penalty window / DRY, not temp
#
# Counts alone cannot separate the first two — the artifact needs the blind
# panel, same protocol as the 35/50 the baseline scored.
#
# BASELINE (laguna-s-2.1-apex, 1h, same mission, same fixed watchdog):
#   3 degenerations · 17 files · 21/45 goals · 22.4 tok/s · panel 35/50
set -u
ROOT=/Users/lah-rb/Repos/ouroboros
cd "$ROOT"
export PATH="$HOME/.local/bin:$PATH" PYTHONPATH="$ROOT"

BASE=/tmp/tier
CFG=laguna-s-2.1-apex-tempfloor
WORK=$BASE/tempfloor-$CFG
SUMMARY=$BASE/tempfloor_summary.txt
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

while pgrep -f "[o]uroboros.py start" >/dev/null; do sleep 60; done

log "=== $CFG ($WALL) ==="
stop_server || exit 1
echo -n "$CFG" > "$ROOT/llmvp/active_config.txt"
( cd "$ROOT/llmvp" && nohup .venv/bin/python api/main.py > "$BASE/${CFG}_server.log" 2>&1 & )
if ! wait_healthy; then
  log "  ABORT — not healthy in ${BOOT_TIMEOUT}s"
  grep -m1 "Startup failed" "$BASE/${CFG}_server.log" | while read -r l; do log "  $l"; done
  exit 1
fi
# The floor is the whole arm. If the config resolved without it — a bad extends,
# a typo'd key — the run would look like a clean baseline and prove nothing.
log "  server up. inheritance + floor in force:"
grep -E "🧬|temperature floor" "$BASE/${CFG}_server.log" | head -2 \
  | while read -r l; do log "    ${l#*INFO }"; done
grep -q "🧬 Config $CFG extends" "$BASE/${CFG}_server.log" \
  || { log "  ABORT — config did not resolve through extends"; exit 1; }
ident=$(curl -s -m 10 -X POST http://localhost:8008/graphql -H 'Content-Type: application/json' \
  -d '{"query":"{ health { requestId decodeMode } }"}' 2>/dev/null)
case "$ident" in *"Cannot query field"*)
  log "  ABORT — server lacks requestId; OLD code"; exit 1 ;; esac

rm -rf "$WORK"; mkdir -p "$WORK"
uv run ouroboros.py mission create --mission_config game_challenge_boss \
  --working-dir "$WORK" --top-phase quality > "$BASE/tempfloor-create.log" 2>&1 \
  || { log "  ABORT — mission create failed"; exit 1; }
log "  mission running (backstop $WALL)"
start=$SECONDS
env OURO_LLMVP=http://localhost:8008/graphql OURO_REASONING_OFF=1 \
  uv run ouroboros.py start --working-dir "$WORK" \
    --max-wall-clock "$WALL" --trace-thinking > "$BASE/tempfloor-run.log" 2>&1
rc=$?; mins=$(( (SECONDS - start) / 60 ))

L=$BASE/tempfloor-run.log; S=$BASE/${CFG}_server.log
cnt(){ local v; v=$(grep -c "$1" "$2" 2>/dev/null); echo "${v:-0}"; }
files=$(find "$WORK" -type f -not -path "*/.agent/*" -not -path "*/.venv/*" \
        -not -path "*/__pycache__/*" -not -path "*/.ruff_cache/*" 2>/dev/null | wc -l | tr -d ' ')
ok=0; bad=0
while IFS= read -r f; do
  if uv run python -c "import ast,sys;ast.parse(open(sys.argv[1]).read())" "$f" 2>/dev/null
    then ok=$((ok+1)); else bad=$((bad+1)); fi
done < <(find "$WORK" -name "*.py" -not -path "*/.venv/*" -not -path "*/__pycache__/*" 2>/dev/null)
maxgen=$(grep -o 'generated=[0-9]* tok' "$S" | sed 's/[^0-9]//g' | sort -n | tail -1)
tps=$(grep -o 'speed=[0-9.]* tok/s' "$S" | sed 's/[^0-9.]//g' | sort -n \
      | awk '{v[NR]=$1} END{if(NR)printf "%.1f", v[int(NR/2)+1]}')
floored=$(cnt 'temperature floor' "$S")
status=$(uv run ouroboros.py mission status --working-dir "$WORK" 2>/dev/null \
           | grep -E "^  Status:|Goals \(" | tr '\n' ' ')

{
  echo "laguna temperature-floor arm $(date '+%F %H:%M')"
  echo "  baseline (no floor): 3 degens · 17 files · 21/45 goals · 22.4 tok/s · panel 35/50"
  echo "  this arm: rc=$rc ${mins}min files=$files py_ok=$ok py_fail=$bad"
  echo "    GUARD captures=$(cnt 'Runaway capture' "$S")  (baseline 3)"
  echo "    relayed verdicts=$(cnt 'aborted the generation as degenerate' "$L")"
  echo "    floor applied to $floored requests"
  echo "    WATCHDOG cancels=$(cnt 'cancelled by health watchdog' "$L") busy=$(cnt 'instances are busy' "$L")"
  echo "    batch $(cnt "'build_structure': step 'report_success'" "$L") success / $(cnt "'build_structure': step 'report_failed'" "$L") failed, serial creates=$(cnt 'file_ops → create' "$L")"
  echo "    PTY $(cnt "'interact': step 'report_success'" "$L") ok / $(cnt "'interact': step 'report_with_issues'" "$L") issues  (baseline 15/5)"
  echo "    DECODE median=${tps:-?} tok/s maxgen=${maxgen:-0}"
  echo "    $status"
} | tee "$SUMMARY" | while read -r l; do log "$l"; done
cp "$SUMMARY" "$WORK/OUTCOME"

stop_server
echo -n "$RESTORE_CFG" > "$ROOT/llmvp/active_config.txt"
( cd "$ROOT/llmvp" && nohup .venv/bin/python api/main.py > "$BASE/tempfloor_restore.log" 2>&1 & )
wait_healthy && log "restored production server" || log "WARN restore server unhealthy"
log "=== TEMPFLOOR RUN COMPLETE ==="
