#!/bin/bash
# Does Hy3 honour reasoning_effort, and does it give three distinct levels?
#
#   python3 dev/daemonize.py ~/ouroboros-runs/hy3_reason_console.log bash dev/hy3_reasoning_probe.sh
#
# ── THE QUESTION ─────────────────────────────────────────────────────
# With <｜reasoning_mode:opensource｜>reasoning_effort:no_think VERIFIABLY in the
# rendered system block, the model opened a think block on 4/4 generations
# (2026-07-28). So the directive reaches the prompt and is ignored. Two
# candidate explanations, and they are distinguishable:
#
#   POSITION  — Hy3's own template APPENDS the line to the end of the system
#               prompt; our renderer places it at the FRONT. The hunyuan3post
#               family + hy3-postsystem config move it, changing nothing else.
#   THE MODEL — a REAP prune drops experts, and instruction-following on a
#               rarely-exercised control line is exactly the kind of behaviour
#               pruning can cost. If BOTH placements ignore it, that is the
#               answer and Hy3 is an always-thinking model here.
#
# ── AND THE PRIZE IF IT WORKS ────────────────────────────────────────
# no_think / low / high would give a THIRD granularity level, which no model in
# this fleet currently offers — everything else is bimodal (on/off) or a single
# depth dial. So the probe measures not just "did it stop thinking" but how much
# it thought at each level: three levels that collapse onto two are worth
# knowing before the router is pointed at them.
#
# Measured from the server's OWN raw/stripped lengths, not the API response:
# CoT size is (raw - stripped), and the stripped text alone cannot show it.
set -u
ROOT=/Users/lah-rb/Repos/ouroboros
RUNS=$HOME/ouroboros-runs
STAMP=$(date '+%Y%m%d-%H%M%S')
LOG=$RUNS/hy3_reason_$STAMP.log
RESTORE_CFG=gpt-oss-120b-a5-swarm-524k
BOOT_TIMEOUT=1200
REPS=3

mkdir -p "$RUNS"
log(){ echo "[$(date '+%m-%d %H:%M:%S')] $*" | tee -a "$LOG"; sync; }
cd "$ROOT"; export PATH="$HOME/.local/bin:$PATH" PYTHONPATH="$ROOT"

server_up(){ curl -s -m 5 -X POST http://localhost:8008/graphql \
  -H 'Content-Type: application/json' -d '{"query":"{ health { status } }"}' \
  2>/dev/null | grep -q '"ok"'; }

stop_server(){
  local pid; pid=$(pgrep -f "[a]pi/main.py" | head -1) || true
  [ -n "${pid:-}" ] || return 0
  kill -TERM "$pid" 2>/dev/null || true   # never SIGKILL: it leaks the pool
  for _ in $(seq 1 900); do pgrep -f "[a]pi/main.py" >/dev/null || return 0; sleep 1; done
  return 1
}

# arm <config> <label>
arm(){
  local cfg=$1 label=$2
  local slog=$RUNS/hy3r_${label}_server.log
  log ""; log "═══ $label ($cfg) ═══"
  stop_server || { log "  server would not stop"; return 1; }
  echo -n "$cfg" > "$ROOT/llmvp/active_config.txt"
  ( cd "$ROOT/llmvp" && nohup .venv/bin/python api/main.py > "$slog" 2>&1 & )
  local deadline=$((SECONDS + BOOT_TIMEOUT)) up=0
  while [ $SECONDS -lt $deadline ]; do
    server_up && { up=1; break; }
    grep -q "Startup failed" "$slog" 2>/dev/null && break
    sleep 10
  done
  [ "$up" = "1" ] || { log "  DID NOT COME UP — $(grep -m1 -o 'Startup failed.*\|KV preflight REFUSED.*' "$slog" | head -c 100)"; return 1; }
  grep -q "Could not load static tokens" "$slog" && { log "  !! static tokens failed"; return 1; }
  log "  up · static tokens loaded"

  local level
  for level in no_think low high; do
    local mark; mark=$(wc -l < "$slog")
    local i
    for i in $(seq 1 $REPS); do
      curl -s -m 600 -X POST http://localhost:8008/graphql -H 'Content-Type: application/json' \
        -d "{\"query\":\"query(\$p:String!,\$m:Int!,\$r:String){completion(request:{prompt:\$p,maxTokens:\$m,reasoning:\$r}){text tokensGenerated}}\",\"variables\":{\"p\":\"A farmer has 17 sheep. All but 9 run away. How many are left? Answer with just the number.\",\"m\":2000,\"r\":\"$level\"}}" \
        >/dev/null 2>&1
    done
    # Read the server's own accounting for the lines this level produced.
    local stats
    stats=$(tail -n +$((mark+1)) "$slog" | awk '
      /run_completion: raw answer len=/ { match($0,/len=[0-9]+/); raw=substr($0,RSTART+4,RLENGTH-4)+0;
                                          think += ($0 ~ /first100=.<think:opensource>/) ? 1 : 0; nraw++ ; sraw+=raw }
      /run_completion: after strip len=/ { match($0,/len=[0-9]+/); st=substr($0,RSTART+4,RLENGTH-4)+0; sstr+=st }
      END { if(nraw) printf "n=%d think=%d raw_avg=%d out_avg=%d cot_avg=%d", nraw, think, sraw/nraw, sstr/nraw, (sraw-sstr)/nraw;
            else printf "no generations recorded" }')
    log "  reasoning=$level  ->  $stats"
  done
}

log "=== Hy3 reasoning-effort probe (position × level) ==="
arm hy3-reap-200b-a21 "PREFIX (shipped: directive at the FRONT)"
arm hy3-postsystem    "APPEND (Hy3's own placement: directive at the END)"

log ""; log "=== restoring production ==="
stop_server
echo -n "$RESTORE_CFG" > "$ROOT/llmvp/active_config.txt"
( cd "$ROOT/llmvp" && nohup .venv/bin/python api/main.py > "$RUNS/hy3r_restore.log" 2>&1 & )
for _ in $(seq 1 120); do server_up && break; sleep 10; done
log "restored $RESTORE_CFG"
log ""; log "SUMMARY:"; grep -E "═══|reasoning=" "$LOG" | sed 's/^\[[^]]*\] /  /' | tee -a "$LOG"
log ""
log "READ IT AS: think=0 at no_think means the dial works. cot_avg separating"
log "across no_think/low/high means three real granularities; two clusters mean"
log "it is bimodal like everything else in the fleet."
