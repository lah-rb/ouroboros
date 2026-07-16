#!/bin/bash
# Persistent process-footprint sampler (survives reboot, unlike /tmp). Logs the
# OWNED memory (phys_footprint + graphics-owned) that RSS/Activity-Monitor hide,
# every 120s, for the server process. Usage: footprint_sampler.sh [out.csv]
OUT=${1:-/tmp/thrash_footprint.csv}
echo "ts,iso,rss_gb,phys_footprint,graphics_owned,server_pid" > "$OUT"
while true; do
  pid=$(pgrep -f 'api/main.py' | head -1)
  if [ -z "$pid" ]; then echo "$(date +%s),$(date +%H:%M:%S),,,,gone" >> "$OUT"; sleep 20; continue; fi
  fp=$(footprint "$pid" 2>/dev/null)
  phys=$(echo "$fp" | grep -iE "^[[:space:]]*phys_footprint:" | head -1 | sed -E 's/.*phys_footprint:[[:space:]]*//')
  gfx=$(echo "$fp" | grep -iE "graphics" | head -1 | grep -oE "[0-9.]+ ?[KMG]?B" | head -1)
  rss=$(ps -o rss= -p "$pid" 2>/dev/null | awk '{printf "%.2f",$1/1048576}')
  echo "$(date +%s),$(date +%H:%M:%S),$rss,${phys:-?},${gfx:-?},$pid" >> "$OUT"
  sleep 120
done
