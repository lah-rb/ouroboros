#!/bin/zsh
# Does muse's dflash drafter actually speed up decode on THIS machine?
#
# THE TRAP THIS PROBE IS BUILT AROUND. llama.cpp issue #25345: with the
# unified KV cache (the default since ~2026-07-05, and what our muse config
# sets), llama-server SILENTLY IGNORES -md. The draft model loads, occupies
# ~1.6GB, and never engages — no init line, no acceptance stats, timings
# identical to baseline. A naive A/B would show "no speedup" and the wrong
# conclusion ("speculative decoding does not help on M1 Ultra") would look
# measured. So every arm below is checked for n_drafted/n_accept, and the
# no-kv-unified workaround is a THIRD arm rather than an afterthought.
#
# Published for comparison (meta-models card): M4 Max 23.7 -> 37.8 tok/s
# (1.5x), M5 Max 1.8x, RTX 5090 3.1x. M1 Ultra has MORE bandwidth and less
# compute per unit of it than an M4 Max, and speculative decoding trades
# bandwidth-bound decode for compute-bound verify — the direction that pays
# least here. The card's number is not a prediction for this box.
#
# Also: --mmproj + --model-draft makes the server terminate (issue #19712),
# so no vision arm exists and none can.
set -u
M=/Users/lah-rb/.lmstudio/models/meta-models/Muse-Glimmer-30B-GGUF/muse-glimmer-30B-kquant-dynamic.gguf
D=/Users/lah-rb/.lmstudio/models/meta-models/Muse-Glimmer-30B-GGUF/dflash-kquant.gguf
PORT=8077
PROMPT='Explain, in about 200 words, why speculative decoding speeds up autoregressive generation.'
N=200

run_arm () {
  local label="$1"; shift
  local log=/private/tmp/claude-501/sd_${label}.log
  llama-server -m "$M" --host 127.0.0.1 --port $PORT -ngl 99 -c 4096 \
      --no-webui "$@" > "$log" 2>&1 &
  local pid=$!
  # READINESS IS HTTP 200, NOT A CONNECTION. llama-server binds the port
  # immediately and answers /health with 503 "loading model" for as long as
  # the 18GB target takes. A plain `curl && break` returns 0 on that 503, so
  # the first version of this probe fired every request mid-load, got
  # nothing, and reported 0.00 tok/s for all three arms — which reads as a
  # result rather than a broken harness.
  local ready=0
  for i in $(seq 1 150); do
    if [[ "$(curl -s -o /dev/null -w '%{http_code}' -m 3 \
             "http://127.0.0.1:$PORT/health" 2>/dev/null)" == "200" ]]; then
      ready=1; break
    fi
    sleep 2
  done
  if [[ $ready -eq 0 ]]; then
    printf "  %-26s SERVER NEVER BECAME READY\n" "$label"
    kill $pid 2>/dev/null; wait $pid 2>/dev/null; return
  fi
  # greedy + batch 1: the card's stated requirement for dflash
  local out
  out=$(curl -s -m 600 -X POST "http://127.0.0.1:$PORT/completion" \
        -H 'Content-Type: application/json' \
        -d "{\"prompt\":$(printf '%s' "$PROMPT" | python3 -c 'import json,sys;print(json.dumps(sys.stdin.read()))'),\"n_predict\":$N,\"temperature\":0,\"top_k\":1,\"cache_prompt\":false}")
  local tps
  tps=$(printf '%s' "$out" | python3 -c '
import json,sys
try:
    t=json.load(sys.stdin).get("timings",{})
    print(f'"'"'{t.get("predicted_per_second",0):.2f} {int(t.get("predicted_n",0))}'"'"')
except Exception as e: print("0 0")')
  # THE ENGAGEMENT CHECK — absent stats mean the arm proved nothing.
  local spec
  spec=$(grep -icE "n_drafted|n_accept|draft acceptance|spec.*draft" "$log" 2>/dev/null || echo 0)
  printf "  %-26s %8s tok/s (%s tok)   draft-stats-lines=%s\n" "$label" "${tps%% *}" "${tps##* }" "$spec"
  kill $pid 2>/dev/null; wait $pid 2>/dev/null
  sleep 4
}

echo "=== dflash speculative-decoding probe, M1 Ultra ==="
echo "    greedy (temp 0, top_k 1), batch 1, n_predict=$N"
echo
# --spec-type DEFAULTS TO none IN THIS BUILD, so `-md` alone is inert — the
# model card's command (`-md ... -ngld 99`) predates the explicit selector and
# silently does nothing here. Confirmed: -md without it gave 20.78 tok/s on
# every arm, byte-identical to baseline, with
# "[spec] failed to measure draft model memory: failed to create
# llama_context from model" in the log.
run_arm baseline
run_arm dflash                 -md "$D" -ngld 99 --spec-type draft-dflash
run_arm dflash_no_kv_unified   -md "$D" -ngld 99 --spec-type draft-dflash --no-kv-unified
echo
echo "  draft-stats-lines=0 on a draft arm => -md was IGNORED, not slow."
echo "  logs: /private/tmp/claude-501/sd_*.log"
