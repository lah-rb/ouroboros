#!/bin/bash
# mem_ledger.sh — one CSV row per process of interest per invocation.
# Cheap differential sampler to pin down WHICH processes accumulate app memory
# over days (companion to mem_snapshot.sh, which is the heavyweight one-shot).
#
# Usage:  bash dev/mem_ledger.sh [label]
# Output: appends to dev/mem_ledger.csv
#   ts,label,scope,name,pid,rss_mb,phys_footprint_mb
# scope=proc rows: top processes by RSS + always-watched names.
# scope=sys  rows: vm_stat anon/wired/fileback/compressed totals (name=anon etc).

set -u
LEDGER="$(cd "$(dirname "$0")" && pwd)/mem_ledger.csv"
TS="$(date +%Y-%m-%dT%H:%M:%S)"
LABEL="${1:-tick}"

[ -f "$LEDGER" ] || echo "ts,label,scope,name,pid,rss_mb,phys_footprint_mb" > "$LEDGER"

# ---- system totals from vm_stat (16K pages) ----
vm_stat | awk -v ts="$TS" -v label="$LABEL" '
  /Anonymous pages/          {printf "%s,%s,sys,anon,,%d,\n",     ts,label,$3*16384/1048576}
  /Pages wired down/         {printf "%s,%s,sys,wired,,%d,\n",    ts,label,$4*16384/1048576}
  /File-backed pages/        {printf "%s,%s,sys,filebacked,,%d,\n",ts,label,$3*16384/1048576}
  /Pages occupied by compressor/ {printf "%s,%s,sys,compressor,,%d,\n",ts,label,$5*16384/1048576}
' | tr -d '.' >> "$LEDGER"

# ---- per-process: top 20 by RSS + always-watch list ----
# footprint(1) gives the accurate dirty+swap "app memory" per process; fall back
# to blank if it errors (needs same-user or sudo).
ps axo pid=,rss=,comm= | sort -k2 -rn | head -20 > /tmp/mem_ledger_top.$$
# always-watch: llmvp api/main.py, docker bits, VM, claude, run_pilot
for pat in "api/main.py" "com.apple.Virtualization" "com.docker.backend" \
           "Docker Desktop Helper (Renderer)" "run_pilot"; do
  pgrep -f "$pat" 2>/dev/null | while read -r p; do
    ps -o pid=,rss=,comm= -p "$p" 2>/dev/null
  done
done >> /tmp/mem_ledger_top.$$

sort -u -k1,1n /tmp/mem_ledger_top.$$ | while read -r pid rss comm; do
  [ -z "${pid:-}" ] && continue
  name="$(basename "$comm" | tr ',' '_')"
  # annotate the two ambiguous python processes by role
  case "$(ps -o command= -p "$pid" 2>/dev/null)" in
    *api/main.py*)  name="llmvp-server" ;;
    *run_pilot*)    name="swe-run_pilot" ;;
  esac
  fp="$(footprint -p "$pid" 2>/dev/null | awk '/^Physical footprint:/{print $3; exit}')"
  # normalize footprint like "1.2G"/"433.5M" to MB
  fp_mb=""
  case "$fp" in
    *G) fp_mb="$(echo "$fp" | sed 's/G//' | awk '{printf "%d",$1*1024}')" ;;
    *M) fp_mb="$(echo "$fp" | sed 's/M//' | awk '{printf "%d",$1}')" ;;
    *K) fp_mb=0 ;;
  esac
  echo "$TS,$LABEL,proc,$name,$pid,$((rss/1024)),$fp_mb" >> "$LEDGER"
done
rm -f /tmp/mem_ledger_top.$$

echo "ledger tick appended: $TS ($LABEL) -> $LEDGER"
