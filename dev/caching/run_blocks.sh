#!/bin/bash
# Cache experiment block runner — dev/caching/EXPERIMENT.md
#
# One row per cell into dev/caching/results/cells.jsonl (APPEND — never
# truncate). Per cell: write a temp config with the cell's EXACT flags,
# restart the server, HARD-ASSERT the intervention landed (strategy triple +
# n_ctx_seq from health against the cell spec — the nopersona
# byte-identical-null lesson), run the probe, append, clean up. Production
# config restored at exit whatever happens.
#
# Usage: bash dev/caching/run_blocks.sh [BLOCK ...]   (default: A B)
#   e.g. bash dev/caching/run_blocks.sh A B D
set -uo pipefail
cd "$(dirname "$0")/../.."
LLMVP=llmvp
OUT=dev/caching/results/cells.jsonl
mkdir -p "$(dirname "$OUT")"
RUN_ID="cells_$(date +%Y%m%d-%H%M%S)"
RESTORE=$(cat $LLMVP/active_config.txt)
BLOCKS="${*:-A B}"

health_ok(){ curl -s -m5 -X POST http://localhost:8008/graphql \
  -H 'Content-Type: application/json' \
  -d '{"query":"query { health { status } }"}' 2>/dev/null | grep -q '"ok"'; }
wait_ready(){ for _ in $(seq 1 120); do health_ok && return 0; sleep 10; done; return 1; }
stop_server(){ (cd "$LLMVP" && uv run llmvp.py --stop) >/dev/null 2>&1 || true; sleep 5; }
start_server(){ (cd "$LLMVP" && uv run llmvp.py --backend) >/dev/null 2>&1 || true; wait_ready; }

cleanup(){
  # VERIFY the stop took before restoring: on 2026-07-30 the trap's stop had
  # not completed before exit and the last CELL's server outlived the restore —
  # active_config said production while qwen3.6-27b was still serving.
  stop_server
  for _ in $(seq 1 12); do pgrep -f '[a]pi/main.py' >/dev/null || break; sleep 5; done
  pgrep -f '[a]pi/main.py' >/dev/null && echo "!! server still up after stop — NOT restarting production over it"
  echo "$RESTORE" > $LLMVP/active_config.txt
  rm -f $LLMVP/configs/cachecell-*.yaml
  if ! pgrep -f '[a]pi/main.py' >/dev/null; then
    start_server && echo "production config ($RESTORE) restored AND serving"                  || echo "production restored in active_config; server did not come up"
  fi
}
trap cleanup EXIT

# ── CELL TABLE ─────────────────────────────────────────────────────────
# cell_id | block | base_config | strategy | bands | swa | depth | turn_tokens
#   strategy: resident | replay
#   bands:    none (n_seq_max=2) | stock (12-ish) | unified (kv_unified) | na (replay)
#   swa:      inherit | on | off
#
# Block B: payout x band-layout at realistic turns. gemma resident cells live
# in Block C behind the KV re-check (can_shift=false at inherited flags).
# Block A: instrument validation (small depth past the fragmented window).
CELLS="
A1|A|glm-4.7-flash|resident|stock|inherit|14|900
A2|A|gpt-oss-120b-a5|resident|stock|inherit|14|900
B01|B|gpt-oss-120b-a5|replay|na|inherit|16|900
B02|B|gpt-oss-120b-a5|resident|none|inherit|16|900
B03|B|gpt-oss-120b-a5|resident|stock|inherit|16|900
B04|B|gpt-oss-120b-a5|resident|unified|inherit|16|900
B05|B|glm-4.7-flash|replay|na|inherit|16|900
B06|B|glm-4.7-flash|resident|none|inherit|16|900
B07|B|glm-4.7-flash|resident|stock|inherit|16|900
B08|B|glm-4.7-flash|resident|unified|inherit|16|900
B09|B|gemma-4-26b-a4b|replay|na|inherit|16|900
B10|B|gpt-oss-120b-a5|replay|na|inherit|12|0
B11|B|gpt-oss-120b-a5|resident|stock|inherit|12|0
B12|B|glm-4.7-flash|resident|stock|inherit|12|0
C1|C|gemma-4-26b-a4b|resident|none|on|16|900
C2|C|gemma-4-26b-a4b|resident|unified|on|16|900
D1|D|hy3-reap-200b-a21|resident|none|inherit|16|900
D2|D|hy3-reap-200b-a21|resident|unified|inherit|16|900
D3|D|hy3-reap-200b-a21|replay|na|inherit|16|900
RC1|RC|laguna-xs-2.1|resident|unified|on|16|900
RC2|RC|qwen3.5-122b-a10|resident|unified|on|16|900
F1|F|mistral-medium-3.5-128b|resident|none|inherit|16|900
F2|F|laguna-xs-2.1|replay|na|inherit|16|900
F3|F|qwen3.5-122b-a10|replay|na|inherit|16|900
F4|F|devstral-2-small-24b|resident|none|inherit|16|900
F5|F|qwen3.6-27b|replay|na|inherit|16|900
"

for line in $CELLS; do
  IFS='|' read -r CID BLK CFG STRAT BANDS SWA DEPTH TT <<< "$line"
  [ -z "$CID" ] && continue
  case " $BLOCKS " in *" $BLK "*) ;; *) continue;; esac
  echo "── cell $CID ($BLK): $CFG strat=$STRAT bands=$BANDS swa=$SWA depth=$DEPTH turn_tokens=$TT ──"

  CFG_PATH=$(cd "$LLMVP" && .venv/bin/python -c "
import sys; sys.path.insert(0,'.')
from core.config import resolve_config_path
try: print(resolve_config_path('$CFG'))
except Exception: pass")
  [ -n "$CFG_PATH" ] || { echo "!! cannot resolve $CFG"; continue; }

  # Temp config with the cell's exact flags. Refresh pinned high; strip off.
  CFG_PATH="$CFG_PATH" CID="$CID" STRAT="$STRAT" BANDS="$BANDS" SWA="$SWA" \
  uv run python - <<'PYEOF'
import os, yaml
d = yaml.safe_load(open(os.environ["CFG_PATH"]))
m = d["model"]
strat, bands, swa = os.environ["STRAT"], os.environ["BANDS"], os.environ["SWA"]
m["resident_seq_cache"] = strat == "resident"
m["session_full_replay"] = True                      # always-armed fallback
m["resident_strip_reasoning"] = False                # KV-content confound held
m["context_refresh_interval"] = 100000               # M16 pinned out of the way
m["context_refresh_seconds"] = 360000
if bands == "none":
    m["resident_session_flow_fork"] = False
    m["session_snapshot_max"] = 0
    m["flow_kv_cache"] = False
    m["reasoning_head_swap"] = False
elif bands == "stock":
    m["resident_session_flow_fork"] = True
    m["session_snapshot_max"] = 2
elif bands == "unified":
    m["kv_unified"] = True
    m["resident_session_flow_fork"] = False
    m["session_snapshot_max"] = 0
    m["reasoning_head_swap"] = False
if swa == "on":
    m["swa_full"] = True
    m["kv_unified"] = True if bands == "unified" else m.get("kv_unified", False)
    # KV re-check: drop n_ctx to a conservative window; the preflight refuses
    # if even this is unaffordable (that refusal IS the Block C step-1 result).
    m["n_ctx"] = min(int(m.get("n_ctx", 32768)), 65536)
    m.pop("probe_verified_n_ctx", None)              # measured under OTHER flags
    m.pop("kv_bytes_per_token_measured", None)
d["resources"]["decode_mode"] = "pool"
d.pop("tier", None)
yaml.safe_dump(d, open(f"llmvp/configs/cachecell-{os.environ['CID']}.yaml", "w"),
               sort_keys=False)
PYEOF

  echo "cachecell-$CID" > $LLMVP/active_config.txt
  stop_server
  if ! start_server; then
    echo "{\"cell\":\"$CID\",\"run_id\":\"$RUN_ID\",\"void\":\"server failed to start\"}" >> "$OUT"
    rm -f $LLMVP/configs/cachecell-$CID.yaml; continue
  fi

  # HARD ASSERT the intervention landed before any workload.
  EXPECT_STRAT=$([ "$STRAT" = resident ] && echo resident || echo full_replay)
  CHECK=$(curl -s -m10 -X POST http://localhost:8008/graphql -H 'Content-Type: application/json' \
    -d '{"query":"query { health { sessionStrategy sessionCanShift residentRequested nCtxSeq } }"}')
  GOT_STRAT=$(echo "$CHECK" | uv run python -c "import sys,json;print(json.load(sys.stdin)['data']['health']['sessionStrategy'])" 2>/dev/null)
  GOT_SEQ=$(echo "$CHECK" | uv run python -c "import sys,json;print(json.load(sys.stdin)['data']['health']['nCtxSeq'])" 2>/dev/null)
  echo "   landed: strategy=$GOT_STRAT n_ctx_seq=$GOT_SEQ (expected $EXPECT_STRAT)"
  if [ "$GOT_STRAT" != "$EXPECT_STRAT" ]; then
    # NOT voided silently: a denial IS a result (e.g. gemma can_shift=false
    # without swa) — record it with the health payload and move on.
    echo "{\"cell\":\"$CID\",\"run_id\":\"$RUN_ID\",\"landed\":false,\"expected\":\"$EXPECT_STRAT\",\"health\":$CHECK}" >> "$OUT"
    rm -f $LLMVP/configs/cachecell-$CID.yaml; continue
  fi

  uv run python dev/cache_compat_matrix.py --label "cell:$CID:$CFG" \
    --run-id "$RUN_ID" --depth "$DEPTH" --turn-tokens "$TT" \
    | uv run python -c "
import sys, json
row = json.loads(sys.stdin.read())
row.update(cell='$CID', block='$BLK', strategy_expected='$EXPECT_STRAT',
           bands='$BANDS', swa='$SWA', n_ctx_seq_health=int('$GOT_SEQ' or 0))
print(json.dumps(row))" | tee -a "$OUT"

  rm -f $LLMVP/configs/cachecell-$CID.yaml
done
echo "cells appended to $OUT (run_id $RUN_ID)"
