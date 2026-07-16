#!/bin/bash
# Comprehensive host-memory snapshot (macOS / Apple silicon) for diagnosing the
# app-memory creep. One-shot, read-only, no sudo. Writes dev/mem_snapshot_<ts>.txt.
#
# Focus: WHAT is holding RAM. Ouroboros' wired agent process shows ~zero creep,
# so the culprit is elsewhere — Docker Desktop VM (guest page-cache + Rosetta
# amd64 translation cache under platform=linux/amd64), orphaned docker-exec/MCP
# relays, the LLMVP server, or the compressor/swap. Captures per-process DIRTY
# footprint (vmmap --summary) since top's "used" is inflated by file cache on
# unified memory (see memory: top-used-inflated-on-unified-memory).
set -u
TS=$(date +%Y%m%dT%H%M%S)
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUT="$ROOT/dev/mem_snapshot_${TS}.txt"

sec() { echo; echo "════════════════════════════════════════════════════════════"; echo "## $1"; echo "════════════════════════════════════════════════════════════"; }

{
echo "# HOST MEMORY SNAPSHOT — $TS"
echo "host: $(hostname) | uptime:$(uptime)"

sec "Hardware / total RAM"
echo "hw.memsize: $(sysctl -n hw.memsize 2>/dev/null) bytes ($(( $(sysctl -n hw.memsize 2>/dev/null) / 1073741824 )) GiB)"
sysctl -n hw.model machdep.cpu.brand_string 2>/dev/null

sec "memory_pressure (system pressure %)"
memory_pressure 2>/dev/null | head -25 || echo "(unavailable)"

sec "vm_stat (page breakdown — the real anon/wired/compressed picture)"
vm_stat 2>/dev/null
echo "-- swap --"; sysctl vm.swapusage 2>/dev/null

sec "TOP 35 PROCESSES BY MEMORY (top -l1 -o mem)"
top -l 1 -o mem -n 35 -stats pid,command,mem,rprvt,vprvt,vsize,threads 2>/dev/null | tail -n 42

sec "TOP 40 BY RSS with FULL command (spot orphans/leaks)"
ps -Ao rss,pid,ppid,%mem,etime,command -m 2>/dev/null | head -41

sec "PROCESS COUNTS by class (orphan accumulation)"
for pat in "com.docker" "docker" "vpnkit" "qemu" "rosetta" "Virtualization" "llmvp" "run_pilot" "python" "node" "npx" "exa-mcp" "mcp" "setsid" "/bin/sh -c"; do
  n=$(pgrep -fil "$pat" 2>/dev/null | wc -l | tr -d ' ')
  echo "  ${pat}: ${n}"
done

sec "ORPHAN RELAY HUNT (docker-exec / MCP / npx / setsid / PTY)"
ps aux 2>/dev/null | grep -iE "docker exec|exa-mcp|npx|mcp-server|terminal|setsid|pty|frotz" | grep -v grep || echo "(none)"

sec "ZOMBIE / DEFUNCT"
ps aux 2>/dev/null | grep -iE "<defunct>|[[:space:]]Z[[:space:]]" | grep -v grep || echo "(none)"

sec "DOCKER: system df -v (images/containers/volumes/build cache)"
docker system df -v 2>/dev/null | head -70 || echo "(docker unavailable)"

sec "DOCKER: info (mem limit + counts + storage + rosetta)"
docker info 2>/dev/null | grep -iE "name:|server version|containers|images|memory|cpus|storage driver|operating|kernel|rosetta|architecture" || echo "(docker unavailable)"

sec "DOCKER: containers + sizes"
docker ps -a --format '{{.Names}}\t{{.Status}}\t{{.Size}}\t{{.Image}}' 2>/dev/null || echo "(none)"

sec "DOCKER: image count + largest 20"
echo "image lines: $(docker images 2>/dev/null | wc -l)"
docker images --format '{{.Size}}\t{{.Repository}}:{{.Tag}}' 2>/dev/null | sort -rh | head -20

sec "DOCKER DESKTOP: configured VM memory/cpu/swap"
for f in "$HOME/Library/Group Containers/group.com.docker/settings-store.json" "$HOME/Library/Group Containers/group.com.docker/settings.json"; do
  [ -f "$f" ] && echo "-- $f --" && python3 -c "import json,sys;d=json.load(open('$f'));print('\n'.join(f'{k}: {d[k]}' for k in d if any(t in k.lower() for t in ('memory','cpu','swap','disk','rosetta','virtual'))))" 2>/dev/null
done

sec "PER-PROCESS DIRTY FOOTPRINT (vmmap --summary) — the accurate memory a process actually holds"
# Top RSS processes among the suspects: docker VM/backend + llmvp python + any agent python.
SUSPECT_PIDS=$(ps -Ao rss,pid,command -m 2>/dev/null | grep -iE "com.docker|docker|vpnkit|Virtualization|llmvp|run_pilot|python|node" | grep -v grep | head -8 | awk '{print $2}')
for p in $SUSPECT_PIDS; do
  cmd=$(ps -o command= -p "$p" 2>/dev/null | cut -c1-90)
  echo "──────── pid $p — $cmd ────────"
  vmmap --summary "$p" 2>/dev/null | grep -iE "Physical footprint|dirty|swapped|region|TOTAL" | head -12 || echo "  (vmmap unavailable for $p)"
done

sec "LLMVP server health (RSS + instances)"
LLMVP_PID=$(pgrep -fi "llmvp" | head -1)
[ -n "${LLMVP_PID:-}" ] && ps -o pid,rss,%mem,etime,command -p "$LLMVP_PID" 2>/dev/null
curl -s -m4 -X POST http://localhost:8008/graphql -H 'Content-Type: application/json' \
  -d '{"query":"{ health { availableInstances } }"}' 2>/dev/null | head -c 200; echo

sec "purgeable / cached files hint (df of the docker.raw disk image if present)"
ls -lh "$HOME/Library/Containers/com.docker.docker/Data/vms/0/data/Docker.raw" 2>/dev/null || \
ls -lh "$HOME/Library/Containers/com.docker.docker/Data/"*.raw 2>/dev/null || echo "(docker.raw not found at default path)"

echo
echo "# END SNAPSHOT $TS"
} 2>&1 | tee "$OUT"

echo
echo ">>> saved: $OUT"
