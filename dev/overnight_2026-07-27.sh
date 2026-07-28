#!/bin/bash
# Overnight: laguna CoT characterisation, then the qwen APEX degeneration retest.
#
#   python3 dev/daemonize.py /tmp/tier/overnight.log bash dev/overnight_2026-07-27.sh
#
# macOS has no setsid — use daemonize.py and confirm PPID 1 before walking away.
#
# ── SHAPE (Luke's plan) ──────────────────────────────────────────────
#   1. laguna CoT difficulty ladder      — does CoT scale with the ask?
#   2. APEX max-context UNCAPPED CoT 2h  — GATED on rung 1's answer
#   3. poolside non-think 2h             — the comparison arm
#   4. qwen x3, 1h each                  — does the orbit survive in APEX quants?
#
# ── WHY RUNG 2 IS GATED ──────────────────────────────────────────────
# If even "what is the capital of France?" runs to the cap, laguna deliberates
# to whatever budget it is handed and a BIGGER budget cannot help — spending 2h
# to confirm that is waste, so the arm is skipped and the run moves on. If
# trivial prompts terminate, then CoT scales with the ask, the batch turn is
# simply beyond what it will commit to, and an uncapped max-context arm is the
# honest test. The gate reads the ladder's own verdict line.
#
# ── WHAT EACH ARM IS FOR ─────────────────────────────────────────────
# APEX max-ctx uncapped: 6/6 bounded samples hit the 20k cap with zero files,
#   but a cap is not a refutation — the CoT was NOT degenerate (distinct
#   200-char windows 384/384 = 1.000), just unconverged when cut. 131072 ctx is
#   99.7 GB total, 9 GB below the poolside config that already runs stable.
# poolside non-think: the only laguna configuration that has ever delivered a
#   complete artifact reliably (longest generation across 2h: 15,146 tok, ~23%
#   of its budget — natural stops, not cuts).
# qwen x3: the paragraph-ORBIT degeneration was established in this family;
#   these configs were repointed to APEX quants today. The archived runaway
#   prompt is GONE (/Users/lah-rb/ouroboros-overnight/ no longer exists) and a
#   400MB scan of the current log found no degenerate response to mine, so this
#   has to be a live run. Degeneration shows as long-cycle/run-length kills.
set -u
ROOT=/Users/lah-rb/Repos/ouroboros
cd "$ROOT"
export PATH="$HOME/.local/bin:$PATH" PYTHONPATH="$ROOT"

BASE=/tmp/tier
SUMMARY=$BASE/overnight_summary.txt
BOOT_TIMEOUT=900
RESTORE_CFG=gpt-oss-120b-a5-swarm-524k

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

# boot <config> ; returns non-zero and logs if it cannot come up
boot(){
  local cfg=$1
  stop_server || { log "  SKIP $cfg — previous server would not stop"; return 1; }
  echo -n "$cfg" > "$ROOT/llmvp/active_config.txt"
  ( cd "$ROOT/llmvp" && nohup .venv/bin/python api/main.py \
      > "$BASE/${cfg}_ovn_server.log" 2>&1 & )
  wait_healthy || { log "  SKIP $cfg — server not healthy in ${BOOT_TIMEOUT}s"; return 1; }
  log "  server up ($cfg)"
}

# mission <config> <workdir> <wallclock> <mission_config>
mission(){
  local cfg=$1 work=$2 wall=$3 mcfg=$4
  rm -rf "$work"; mkdir -p "$work"
  if ! uv run ouroboros.py mission create --mission_config "$mcfg" \
        --working-dir "$work" --top-phase quality > "$BASE/$(basename "$work")_create.log" 2>&1; then
    log "  SKIP — mission create failed"; echo "SKIPPED_CREATE" > "$work/OUTCOME"; return 1
  fi
  log "  mission running (backstop $wall)"
  local start=$SECONDS
  env OURO_LLMVP=http://localhost:8008/graphql OURO_REASONING_OFF=1 \
    uv run ouroboros.py start --working-dir "$work" \
      --max-wall-clock "$wall" --trace-thinking \
      > "$BASE/$(basename "$work")_run.log" 2>&1
  local rc=$? mins=$(( (SECONDS - start) / 60 ))

  local L="$BASE/$(basename "$work")_run.log" S="$BASE/${cfg}_ovn_server.log"
  cnt(){ local v; v=$(grep -c "$1" "$2" 2>/dev/null); echo "${v:-0}"; }
  local files ok=0 bad=0
  files=$(find "$work" -type f -not -path "*/.agent/*" -not -path "*/.venv/*" \
          -not -path "*/__pycache__/*" -not -path "*/.ruff_cache/*" -not -name OUTCOME 2>/dev/null | wc -l | tr -d ' ')
  while IFS= read -r f; do
    if uv run python -c "import ast,sys;ast.parse(open(sys.argv[1]).read())" "$f" 2>/dev/null
      then ok=$((ok+1)); else bad=$((bad+1)); fi
  done < <(find "$work" -name "*.py" -not -path "*/.venv/*" -not -path "*/__pycache__/*" 2>/dev/null)
  local status
  status=$(uv run ouroboros.py mission status --working-dir "$work" 2>/dev/null \
             | grep -E "^  Status:|Goals \(" | tr '\n' ' ')
  local line="$cfg rc=$rc ${mins}min files=$files py_ok=$ok py_fail=$bad | DEGEN long_cycle=$(cnt 'long-cycle' "$L") run_length=$(cnt 'run-length' "$L") cycle_period=$(cnt 'cycle period' "$L") | ENGINE evict=$(cnt 'KV cell pool exhausted' "$S") windowed=$(cnt 'force-windowing' "$S") | $status"
  log "  done $line"
  echo "$line" >> "$SUMMARY"
  printf '%s\n' "$line" > "$work/OUTCOME"
}

while pgrep -f "[o]uroboros.py start" >/dev/null; do sleep 60; done
: > "$SUMMARY"
echo "overnight $(date '+%F %H:%M')" >> "$SUMMARY"

# ── 1. laguna CoT difficulty ladder ──────────────────────────────────
log "=== 1/4 laguna CoT difficulty ladder (APEX, thinking) ==="
LADDER_OUT=$BASE/cot_ladder.txt
if boot laguna-s-2.1-apex-think; then
  uv run python dev/laguna_cot_difficulty.py --max-tokens 20000 --samples 2 \
    > "$LADDER_OUT" 2>&1
  tail -6 "$LADDER_OUT" | while read -r l; do log "    $l"; done
  cat "$LADDER_OUT" >> "$SUMMARY"
else
  echo "LADDER SKIPPED (boot)" >> "$SUMMARY"
fi

# ── 2. APEX max-context uncapped CoT — GATED on the ladder ───────────
if grep -q "VERDICT (b)" "$LADDER_OUT" 2>/dev/null; then
  log "=== 2/4 APEX max-ctx UNCAPPED CoT 2h (ladder says CoT scales with the ask) ==="
  boot laguna-s-2.1-apex-maxctx-think && \
    mission laguna-s-2.1-apex-maxctx-think "$BASE/ovn-apex-maxctx" 2h game_challenge_boss
else
  log "=== 2/4 SKIPPED — even trivial prompts ran to the cap, so a bigger"
  log "        budget cannot help. See VERDICT (a) in cot_ladder.txt. ==="
  echo "APEX max-ctx arm SKIPPED — ladder verdict (a): deliberates to any budget" >> "$SUMMARY"
fi

# ── 3. poolside non-think 2h — the comparison arm ────────────────────
log "=== 3/4 poolside non-think 2h ==="
boot laguna-s-2.1-poolside && \
  mission laguna-s-2.1-poolside "$BASE/ovn-poolside" 2h game_challenge_boss

# ── 4. qwen x3, 1h each — does the orbit survive the APEX quants? ────
# 122b uses a probe variant at n_ctx 131072: at 262144 it computes to
# 107.5 GiB and the default 100 GiB preflight would refuse the load.
for q in qwen3.6-35b-a3 qwen3-next-coder-80b-a3 qwen3.5-122b-a10-probe; do
  log "=== 4/4 qwen $q (1h) ==="
  boot "$q" && mission "$q" "$BASE/ovn-$q" 1h game_challenge_boss
done

stop_server
echo -n "$RESTORE_CFG" > "$ROOT/llmvp/active_config.txt"
( cd "$ROOT/llmvp" && nohup .venv/bin/python api/main.py > "$BASE/restore_server.log" 2>&1 & )
wait_healthy && log "restored production server" || log "WARN restore server unhealthy"

log "=== OVERNIGHT COMPLETE ==="
cat "$SUMMARY"
