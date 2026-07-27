#!/bin/bash
# Laguna quant A/B — APEX (2h) then the unsloth retry (45m, short leash).
#
#   python3 dev/daemonize.py /tmp/tier/quant_ab.log bash dev/laguna_quant_ab.sh
#
# SURVIVAL: background jobs started from a Claude Code session die when that
# session exits, and nohup alone does not save them (it stranded a PAUSED
# mission and a DOWN server once already). This has to leave the session's
# process group. An earlier version of this header said to use `setsid` —
# MACOS HAS NO setsid, so that line just failed and the job stayed in the
# session's process group, i.e. exactly the death it was meant to prevent.
# Verify before walking away: `ps -o pid,ppid -p $(pgrep -f laguna_quant_ab)`
# must show PPID 1.
#
# CHAINING: this script WAITS for any in-flight `ouroboros.py start` to exit
# before touching the server, so it can be armed while the poolside 2h run is
# still going. It will not race it for the GPU.
#
# ── ARM 1: APEX, 2h ──────────────────────────────────────────────────
# The comparison target is /tmp/tier/poolside-2h-v2 (same objective, same
# top_phase, same 2h backstop, same agent code). laguna-s-2.1-apex.yaml pins
# n_ctx 65536 to MATCH poolside so the quant is the only variable — free,
# because that run logged zero truncation across 262 flows.
#
# ── ARM 2: unsloth, 45m ──────────────────────────────────────────────
# NOT a quant A/B. This asks whether today's fixes changed the build that
# previously produced nothing usable. Its config keeps its own n_ctx 131072
# but now carries the two config-level fixes from today (max_tokens 32768 ->
# 65536, </think> ban), so it is not a half-fixed arm.
#
# 45m is the leash. The prior failures were all visible well inside it — zero
# files in 22 minutes with thinking on, and the tier arm's trouble showed by
# the first structural batch. If the fixes helped, 45m shows it; if they did
# not, we have not spent 2h finding out.
set -u
ROOT=/Users/lah-rb/Repos/ouroboros
cd "$ROOT"
export PATH="$HOME/.local/bin:$PATH" PYTHONPATH="$ROOT"

BASE=/tmp/tier
mkdir -p "$BASE"
BOOT_TIMEOUT=900          # APEX is 73.9GB off disk; poolside took ~6min cold
RESTORE_CFG=gpt-oss-120b-a5-swarm-524k

# cfg:workdir:wallclock
ARMS=(
  "laguna-s-2.1-apex:$BASE/apex-2h:2h"
  "laguna-s-2.1:$BASE/unsloth-retry:45m"
)

log(){ echo "[$(date '+%m-%d %H:%M:%S')] $*"; }

stop_server(){
  local pid
  pid=$(pgrep -f "[a]pi/main.py" | head -1) || true
  [ -n "${pid:-}" ] || return 0
  # SIGTERM, never SIGKILL: a hard kill leaks the instance pool and the next
  # launch finds availableInstances short.
  kill -TERM "$pid" 2>/dev/null || true
  for _ in $(seq 1 120); do
    pgrep -f "[a]pi/main.py" >/dev/null || { log "  server $pid down"; return 0; }
    sleep 1
  done
  log "  WARN server $pid did not exit on SIGTERM after 120s"
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

# ── Wait out whatever is already running ─────────────────────────────
if pgrep -f "[o]uroboros.py start" >/dev/null; then
  log "waiting for the in-flight mission to park before touching the server"
  while pgrep -f "[o]uroboros.py start" >/dev/null; do sleep 30; done
  log "in-flight mission exited"
  sleep 20   # let its final writes land
fi

log "=== laguna quant A/B: ${#ARMS[@]} arms ==="

for entry in "${ARMS[@]}"; do
  cfg="${entry%%:*}"; rest="${entry#*:}"
  WORK="${rest%%:*}"; WALL="${rest#*:}"
  rm -rf "$WORK"; mkdir -p "$WORK"
  log "--- ARM $cfg (backstop $WALL) -> $WORK ---"

  stop_server
  echo -n "$cfg" > "$ROOT/llmvp/active_config.txt"
  ( cd "$ROOT/llmvp" && nohup .venv/bin/python api/main.py \
      > "$BASE/${cfg}_server.log" 2>&1 & )

  if ! wait_healthy; then
    log "  SKIP $cfg — server not healthy within ${BOOT_TIMEOUT}s (see ${cfg}_server.log)"
    echo "SKIPPED_BOOT" > "$WORK/OUTCOME"
    continue
  fi
  log "  server up"

  if ! uv run ouroboros.py mission create --mission_config game_challenge_boss \
        --working-dir "$WORK" --top-phase quality > "$BASE/${cfg}_create.log" 2>&1; then
    log "  SKIP $cfg — mission create failed (see ${cfg}_create.log)"
    echo "SKIPPED_CREATE" > "$WORK/OUTCOME"
    continue
  fi

  log "  mission running"
  start=$SECONDS
  env OURO_LLMVP=http://localhost:8008/graphql OURO_REASONING_OFF=1 \
    uv run ouroboros.py start --working-dir "$WORK" \
      --max-wall-clock "$WALL" --trace-thinking \
      > "$BASE/${cfg}_run.log" 2>&1
  rc=$?
  mins=$(( (SECONDS - start) / 60 ))

  # Authored artifacts only. Counting raw files once reported devstral at 622
  # against gpt-oss's 80 — a 7x "productivity gap" that was .venv and caches.
  PRUNE=( -not -path "*/.agent/*" -not -path "*/.venv/*"
          -not -path "*/__pycache__/*" -not -path "*/.ruff_cache/*"
          -not -path "*/.git/*" -not -name "OUTCOME" )
  files=$(find "$WORK" -type f "${PRUNE[@]}" 2>/dev/null | wc -l | tr -d ' ')
  ok=0; bad=0
  while IFS= read -r f; do
    if uv run python -c "import ast,sys;ast.parse(open(sys.argv[1]).read())" "$f" 2>/dev/null; then
      ok=$((ok+1)); else bad=$((bad+1)); fi
  done < <(find "$WORK" -name "*.py" "${PRUNE[@]}" 2>/dev/null)

  # The signals today's fixes were supposed to move, counted from the run log
  # so the comparison does not depend on re-reading it by hand later.
  L="$BASE/${cfg}_run.log"
  inst=$(grep -c "Collected install_command" "$L" 2>/dev/null || echo 0)
  cyc=$(grep -c "long-cycle" "$L" 2>/dev/null || echo 0)
  diag=$(grep -c "Starting flow 'diagnose" "$L" 2>/dev/null || echo 0)
  trunc=$(grep -c "truncat" "$L" 2>/dev/null || echo 0)
  venvpkgs=$(ls -d "$WORK"/.venv/lib/python*/site-packages/*.dist-info 2>/dev/null | wc -l | tr -d ' ')

  status=$(uv run ouroboros.py mission status --working-dir "$WORK" 2>/dev/null \
             | grep -E "^  Status:|Goals \(" | tr '\n' ' ')
  log "  done rc=$rc ${mins}min files=$files py_ok=$ok py_fail=$bad venv=${venvpkgs}pkgs install_cmd=$inst long_cycle=$cyc diagnose=$diag trunc=$trunc | $status"
  printf 'cfg=%s\nrc=%s\nminutes=%s\nfiles=%s\npy_ok=%s\npy_fail=%s\nvenv_pkgs=%s\ninstall_cmd=%s\nlong_cycle=%s\ndiagnose=%s\ntruncations=%s\n%s\n' \
    "$cfg" "$rc" "$mins" "$files" "$ok" "$bad" "$venvpkgs" "$inst" "$cyc" "$diag" "$trunc" "$status" > "$WORK/OUTCOME"
done

stop_server
echo -n "$RESTORE_CFG" > "$ROOT/llmvp/active_config.txt"
( cd "$ROOT/llmvp" && nohup .venv/bin/python api/main.py \
    > "$BASE/restore_server.log" 2>&1 & )
wait_healthy && log "restored production server ($RESTORE_CFG)" || log "WARN restore server unhealthy"

log "=== SUMMARY ==="
for entry in "${ARMS[@]}"; do
  cfg="${entry%%:*}"; rest="${entry#*:}"; WORK="${rest%%:*}"
  printf '%-22s ' "$cfg"
  if [ -f "$WORK/OUTCOME" ]; then tr '\n' ' ' < "$WORK/OUTCOME"; echo
  else echo "no outcome recorded"; fi
done
log "  compare against /tmp/tier/poolside-2h-v2 (same objective, same 2h backstop)"
log "=== QUANT A/B COMPLETE ==="
