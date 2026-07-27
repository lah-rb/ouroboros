#!/bin/bash
# Third arm: poolside re-run on CURRENT agent code. Chains after the quant A/B.
#
#   python3 dev/daemonize.py /tmp/tier/poolside_v3.log bash dev/poolside_v3_followon.sh
#
# WHY THIS ARM EXISTS. The poolside 2h run (/tmp/tier/poolside-2h-v2) started at
# 14:06 on the code at d3a9656. The repeat-target warning rewording (250be67)
# landed at ~15:30, WHILE IT WAS STILL RUNNING. So the APEX arm would be the
# first to carry that change, and "APEX vs poolside-v2" would be comparing the
# quant AND an agent-prompt change at the same time.
#
# The exposure is small — the warning only fires on goals with >=2 fix attempts
# against the same target, which was 1 goal of 32 in v2 — and it touches none of
# the primary quant signals (decode rate, JSON compliance, files authored, syntax
# validity, install_command collection). But compute is not the constraint here,
# and a clean apples-to-apples pairing is worth 2h.
#
# SECOND PURPOSE, independent of the confound: this is the FIRST live exercise of
# the reworded warning on a goal that actually repeats. v2 gave us a known repeat
# case to watch for — goal 1a7564ac hit engine.py:GameEngine._do_combat_flee
# three times with the same (correct) diagnosis. See dev/laguna/FINDINGS.md.
#
# KILLABLE. If the confound is not worth the block, kill this before it starts:
#     pkill -f poolside_v3_followon.sh
set -u
ROOT=/Users/lah-rb/Repos/ouroboros
cd "$ROOT"
export PATH="$HOME/.local/bin:$PATH" PYTHONPATH="$ROOT"

BASE=/tmp/tier
CFG=laguna-s-2.1-poolside
WORK=$BASE/poolside-2h-v3
WALL=2h
BOOT_TIMEOUT=900
RESTORE_CFG=gpt-oss-120b-a5-swarm-524k

log(){ echo "[$(date '+%m-%d %H:%M:%S')] $*"; }

stop_server(){
  local pid
  pid=$(pgrep -f "[a]pi/main.py" | head -1) || true
  [ -n "${pid:-}" ] || return 0
  kill -TERM "$pid" 2>/dev/null || true   # never SIGKILL: hard kill leaks the pool
  for _ in $(seq 1 360); do
    pgrep -f "[a]pi/main.py" >/dev/null || { log "  server $pid down"; return 0; }
    sleep 1
  done
  log "  ERROR server $pid did not exit on SIGTERM after 360s"
  return 1
}

wait_healthy(){
  local deadline=$((SECONDS + BOOT_TIMEOUT))
  while [ $SECONDS -lt $deadline ]; do
    if curl -s -m 5 -X POST http://localhost:8008/graphql \
         -H 'Content-Type: application/json' \
         -d '{"query":"{ health { status } }"}' 2>/dev/null | grep -q '"ok"'; then
      return 0
    fi
    sleep 10
  done
  return 1
}

# Wait for the quant A/B driver to finish both its arms.
if pgrep -f "[l]aguna_quant_ab.sh" >/dev/null; then
  log "waiting for the quant A/B driver (APEX 2h + unsloth 45m) to finish"
  while pgrep -f "[l]aguna_quant_ab.sh" >/dev/null; do sleep 60; done
  log "quant A/B driver exited"
fi
# Belt and braces: never race a mission for the GPU.
while pgrep -f "[o]uroboros.py start" >/dev/null; do sleep 30; done
sleep 20

log "=== ARM 3: $CFG on current agent code (backstop $WALL) ==="
rm -rf "$WORK"; mkdir -p "$WORK"

# A failed stop must ABORT: api/main.py refuses to start while another server
# holds the pidfile, so booting anyway guarantees failure and then burns the
# full BOOT_TIMEOUT before saying so.
if ! stop_server; then
  log "  ABORT — previous server would not stop; refusing to boot on top of it"
  echo "SKIPPED_STOP" > "$WORK/OUTCOME"
  exit 1
fi
echo -n "$CFG" > "$ROOT/llmvp/active_config.txt"
( cd "$ROOT/llmvp" && nohup .venv/bin/python api/main.py \
    > "$BASE/${CFG}_v3_server.log" 2>&1 & )

if ! wait_healthy; then
  log "  ABORT — server not healthy within ${BOOT_TIMEOUT}s"
  echo "SKIPPED_BOOT" > "$WORK/OUTCOME"
  exit 1
fi
log "  server up"

if ! uv run ouroboros.py mission create --mission_config game_challenge_boss \
      --working-dir "$WORK" --top-phase quality > "$BASE/${CFG}_v3_create.log" 2>&1; then
  log "  ABORT — mission create failed"
  echo "SKIPPED_CREATE" > "$WORK/OUTCOME"
  exit 1
fi

log "  mission running"
start=$SECONDS
env OURO_LLMVP=http://localhost:8008/graphql OURO_REASONING_OFF=1 \
  uv run ouroboros.py start --working-dir "$WORK" \
    --max-wall-clock "$WALL" --trace-thinking \
    > "$BASE/${CFG}_v3_run.log" 2>&1
rc=$?
mins=$(( (SECONDS - start) / 60 ))

PRUNE=( -not -path "*/.agent/*" -not -path "*/.venv/*"
        -not -path "*/__pycache__/*" -not -path "*/.ruff_cache/*"
        -not -path "*/.git/*" -not -name "OUTCOME" )
files=$(find "$WORK" -type f "${PRUNE[@]}" 2>/dev/null | wc -l | tr -d ' ')
ok=0; bad=0
while IFS= read -r f; do
  if uv run python -c "import ast,sys;ast.parse(open(sys.argv[1]).read())" "$f" 2>/dev/null; then
    ok=$((ok+1)); else bad=$((bad+1)); fi
done < <(find "$WORK" -name "*.py" "${PRUNE[@]}" 2>/dev/null)

L="$BASE/${CFG}_v3_run.log"
inst=$(grep -c "Collected install_command" "$L" 2>/dev/null || echo 0)
cyc=$(grep -c "long-cycle" "$L" 2>/dev/null || echo 0)
diag=$(grep -c "Starting flow 'diagnose" "$L" 2>/dev/null || echo 0)
venvpkgs=$(ls -d "$WORK"/.venv/lib/python*/site-packages/*.dist-info 2>/dev/null | wc -l | tr -d ' ')
# Did the reworded warning fire, and did the goal escape afterwards?
warn=$(grep -c "has failed to resolve the goal" "$L" 2>/dev/null || echo 0)
# The undefined-name class this run is also watching for (engine.py used
# random.choice with no import; ruff F821 finds it, we never run ruff).
f821=$(uv run ruff check --select F821 --no-cache "$WORK" 2>/dev/null | grep -c "F821" || echo 0)

status=$(uv run ouroboros.py mission status --working-dir "$WORK" 2>/dev/null \
           | grep -E "^  Status:|Goals \(" | tr '\n' ' ')
log "  done rc=$rc ${mins}min files=$files py_ok=$ok py_fail=$bad venv=${venvpkgs}pkgs install_cmd=$inst long_cycle=$cyc diagnose=$diag repeat_warn=$warn F821=$f821 | $status"
printf 'cfg=%s\nrc=%s\nminutes=%s\nfiles=%s\npy_ok=%s\npy_fail=%s\nvenv_pkgs=%s\ninstall_cmd=%s\nlong_cycle=%s\ndiagnose=%s\nrepeat_warn=%s\nF821=%s\n%s\n' \
  "$CFG-v3" "$rc" "$mins" "$files" "$ok" "$bad" "$venvpkgs" "$inst" "$cyc" "$diag" "$warn" "$f821" "$status" > "$WORK/OUTCOME"

stop_server
echo -n "$RESTORE_CFG" > "$ROOT/llmvp/active_config.txt"
( cd "$ROOT/llmvp" && nohup .venv/bin/python api/main.py \
    > "$BASE/restore_server.log" 2>&1 & )
wait_healthy && log "restored production server ($RESTORE_CFG)" || log "WARN restore server unhealthy"
log "=== ARM 3 COMPLETE — compare against /tmp/tier/apex-2h on identical agent code ==="
