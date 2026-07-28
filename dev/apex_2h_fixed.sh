#!/bin/bash
# APEX 2h on the fixed engine.
#
#   python3 dev/daemonize.py /tmp/tier/apex_fixed.log bash dev/apex_2h_fixed.sh
#
# macOS has no setsid — use daemonize.py and confirm PPID 1 before walking away.
#
# WHY THIS RUN. The previous APEX arm never measured the quant: its batch
# structural generation was EVICTED after producing 15 complete files, so it
# fell back to the per-file create path while poolside-v2 ran a completed batch.
# That comparison is batch-vs-serial. This arm runs on the fixed engine
# (admission sizes against free KV cells; KV pressure force-windows instead of
# discarding; every engine-side cut is announced as truncated) so the batch path
# can actually complete.
#
# PRE-FLIGHT GATE. The server must be carrying the truncation fix or the run
# silently loses the severed-block protection — the agent only drops a severed
# trailing block when the response is FLAGGED truncated. A 16-token completion
# is asked for and `truncated` must come back true. Cheap, and it refuses to
# spend 2h on a half-fixed stack.
set -u
ROOT=/Users/lah-rb/Repos/ouroboros
cd "$ROOT"
export PATH="$HOME/.local/bin:$PATH" PYTHONPATH="$ROOT"

BASE=/tmp/tier
CFG=laguna-s-2.1-apex
WORK=$BASE/apex-2h-fixed
WALL=2h
BOOT_TIMEOUT=900
RESTORE_CFG=gpt-oss-120b-a5-swarm-524k

log(){ echo "[$(date '+%m-%d %H:%M:%S')] $*"; }

stop_server(){
  local pid
  pid=$(pgrep -f "[a]pi/main.py" | head -1) || true
  [ -n "${pid:-}" ] || return 0
  kill -TERM "$pid" 2>/dev/null || true   # never SIGKILL: it leaks the pool
  # 900s from measurement: a 74GB teardown took 560s once and 310s another time.
  for _ in $(seq 1 900); do
    pgrep -f "[a]pi/main.py" >/dev/null || { log "  server $pid down"; return 0; }
    sleep 1
  done
  log "  ERROR server $pid did not exit on SIGTERM after 900s"
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

# Ask for 16 tokens on a trivial prompt; the response must say it was cut.
truncation_gate(){
  local out
  out=$(curl -s -m 120 -X POST http://localhost:8008/graphql \
    -H 'Content-Type: application/json' \
    -d '{"query":"query($p:String!,$m:Int!){completion(request:{prompt:$p,maxTokens:$m}){tokensGenerated truncated}}","variables":{"p":"Count slowly from one to one hundred, one number per line.","m":16}}' \
    2>/dev/null)
  log "  truncation gate: $out"
  echo "$out" | grep -q '"truncated": *true'
}

while pgrep -f "[o]uroboros.py start" >/dev/null; do sleep 30; done

log "=== APEX 2h on the fixed engine -> $WORK ==="
rm -rf "$WORK"; mkdir -p "$WORK"

if ! stop_server; then
  log "  ABORT — previous server would not stop; refusing to boot on top of it"
  echo "SKIPPED_STOP" > "$WORK/OUTCOME"; exit 1
fi
echo -n "$CFG" > "$ROOT/llmvp/active_config.txt"
( cd "$ROOT/llmvp" && nohup .venv/bin/python api/main.py \
    > "$BASE/${CFG}_fixed_server.log" 2>&1 & )

if ! wait_healthy; then
  log "  ABORT — server not healthy within ${BOOT_TIMEOUT}s"
  echo "SKIPPED_BOOT" > "$WORK/OUTCOME"; exit 1
fi
log "  server up"

if ! truncation_gate; then
  log "  ABORT — the server is NOT carrying the truncation fix. Without it the"
  log "          agent cannot tell a severed batch from a complete one and the"
  log "          run would quietly lose the severed-block protection."
  echo "SKIPPED_STALE_ENGINE" > "$WORK/OUTCOME"; exit 1
fi
log "  truncation fix confirmed live"

if ! uv run ouroboros.py mission create --mission_config game_challenge_boss \
      --working-dir "$WORK" --top-phase quality > "$BASE/${CFG}_fixed_create.log" 2>&1; then
  log "  ABORT — mission create failed"
  echo "SKIPPED_CREATE" > "$WORK/OUTCOME"; exit 1
fi

log "  mission running (backstop $WALL)"
start=$SECONDS
env OURO_LLMVP=http://localhost:8008/graphql OURO_REASONING_OFF=1 \
  uv run ouroboros.py start --working-dir "$WORK" \
    --max-wall-clock "$WALL" --trace-thinking \
    > "$BASE/${CFG}_fixed_run.log" 2>&1
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

L="$BASE/${CFG}_fixed_run.log"
S="$BASE/${CFG}_fixed_server.log"
n(){ local v; v=$(grep -c "$1" "$2" 2>/dev/null); echo "${v:-0}"; }
venvpkgs=$(ls -d "$WORK"/.venv/lib/python*/site-packages/*.dist-info 2>/dev/null | wc -l | tr -d ' ')
f821=$(uv run ruff check --select F821 --no-cache "$WORK" 2>/dev/null | grep -c "F821"); f821=${f821:-0}

status=$(uv run ouroboros.py mission status --working-dir "$WORK" 2>/dev/null \
           | grep -E "^  Status:|Goals \(" | tr '\n' ' ')
log "  done rc=$rc ${mins}min files=$files py_ok=$ok py_fail=$bad venv=${venvpkgs}pkgs"
log "    AGENT  install_cmd=$(n 'Collected install_command' "$L") diagnose=$(n "Starting flow 'diagnose" "$L") lint_ask=$(n 'lint review for\|diagnose-batching' "$L") frame_edit=$(n 'splice_frame: module frame edited' "$L") F821=$f821"
log "    ENGINE evictions=$(n 'KV cell pool exhausted' "$S") windowed=$(n 'force-windowing' "$S") shrunk=$(n 'stream admitted at' "$S") queued=$(n 'stream queued' "$S") clamped=$(n 'max_tokens .* →' "$S")"
log "    $status"
printf 'cfg=%s\nrc=%s\nminutes=%s\nfiles=%s\npy_ok=%s\npy_fail=%s\nvenv_pkgs=%s\ninstall_cmd=%s\ndiagnose=%s\nlint_ask=%s\nframe_edit=%s\nF821=%s\nevictions=%s\nforce_windowed=%s\nadmit_shrunk=%s\nadmit_queued=%s\n%s\n' \
  "$CFG-fixed" "$rc" "$mins" "$files" "$ok" "$bad" "$venvpkgs" \
  "$(n 'Collected install_command' "$L")" "$(n "Starting flow 'diagnose" "$L")" \
  "$(n 'lint review for\|diagnose-batching' "$L")" "$(n 'splice_frame: module frame edited' "$L")" \
  "$f821" "$(n 'KV cell pool exhausted' "$S")" "$(n 'force-windowing' "$S")" \
  "$(n 'stream admitted at' "$S")" "$(n 'stream queued' "$S")" "$status" > "$WORK/OUTCOME"

stop_server
echo -n "$RESTORE_CFG" > "$ROOT/llmvp/active_config.txt"
( cd "$ROOT/llmvp" && nohup .venv/bin/python api/main.py \
    > "$BASE/restore_server.log" 2>&1 & )
wait_healthy && log "restored production server ($RESTORE_CFG)" || log "WARN restore server unhealthy"
log "=== APEX 2h COMPLETE ==="
