#!/bin/bash
# Reasoning-effort spike (control): does the CANONICAL harmony system dial
# (thinking_mode low/medium/high) actually move gpt-oss's CoT length on our
# setup? Pure config flip — edit thinking_mode, restart (auto-rebuilds the static
# SOUL via cache_is_stale), ask a fixed reasoning-heavy question via raw_completion
# (raw output incl. the analysis channel), record tokens_generated as the CoT
# proxy. If CoT(low) << CoT(high), the dial works and the lever is real.
#
# Usage: bash dev/reasoning_spike.sh    (server must be free; restores config at end)
set -u
ROOT=/Users/lah-rb/Repos/ouroboros
LLMVP=$ROOT/llmvp
CFG=$LLMVP/configs/gpt-oss-120b-a5.yaml
GQL=http://localhost:8008/graphql
cd "$ROOT"
cp "$CFG" "/tmp/gptoss_cfg.bak"

# A fixed, moderately hard multi-step problem — CoT length should scale with effort.
Q='You have three boxes. Box A holds twice as many marbles as Box B. Box C holds 5 more than Box A. Altogether there are 47 marbles. How many marbles are in each box? Show your reasoning step by step and then verify the total.'

restart() {
  (cd "$LLMVP" && uv run llmvp.py --stop) >/dev/null 2>&1 || true; sleep 4
  (cd "$LLMVP" && nohup uv run llmvp.py --backend >/tmp/spike_srv.log 2>&1 &)
  for i in $(seq 1 150); do
    [ "$(curl -s -m4 -X POST "$GQL" -H 'Content-Type: application/json' \
      -d '{"query":"{ health { availableInstances } }"}' 2>/dev/null \
      | python3 -c 'import json,sys;print(json.load(sys.stdin)["data"]["health"]["availableInstances"])' 2>/dev/null)" = "1" ] \
      && return 0
    sleep 3
  done
  echo "  SERVER TIMEOUT"; return 1
}

ask() {  # -> tokens_generated + analysis char count, via raw_completion
  python3 - "$Q" <<'PY'
import json, sys, urllib.request
q=sys.argv[1]
body=json.dumps({"query":"query($r: CompletionRequest!){ rawCompletion(request:$r){ rawText tokensGenerated } }",
                 "variables":{"r":{"prompt":q,"maxTokens":4000,"temperature":0.0}}}).encode()
req=urllib.request.Request("http://localhost:8008/graphql", body, {"Content-Type":"application/json"})
d=json.load(urllib.request.urlopen(req, timeout=180))["data"]["rawCompletion"]
raw=d["rawText"]; tg=d["tokensGenerated"]
# analysis-channel length (the CoT) — between 'analysis' marker and 'final'
import re
m=re.search(r'analysis(.*?)(?:<\|channel\|>final|<\|end\|>|$)', raw, re.DOTALL)
cot=len(m.group(1)) if m else len(raw)
print(f"{tg}\t{cot}")
PY
}

echo "=== Reasoning dial control spike (CoT vs thinking_mode) ==="
printf '%-10s %-14s %-12s\n' "level" "tokens_gen" "analysis_chars"
for level in low medium high; do
  sed -i '' "s/^\( *thinking_mode:\) .*/\1 $level/" "$CFG"
  restart || { echo "abort"; break; }
  out=$(ask 2>/dev/null)
  printf '%-10s %-14s %-12s\n' "$level" "${out%%	*}" "${out##*	}"
done

echo "  restoring config…"
cp "/tmp/gptoss_cfg.bak" "$CFG"
restart && echo "  prod restored (thinking_mode=$(grep thinking_mode "$CFG" | sed 's/.*: //; s/ .*//'))"
