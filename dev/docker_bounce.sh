#!/bin/bash
# docker_bounce.sh — conditionally restart Docker Desktop to reclaim the two
# genuine memory accumulators: com.docker.backend heap growth and the VM's
# Rosetta translation cache (grows with every amd64 sweb binary a marathon runs).
#
# Self-gating — safe to call blindly from a cron/health check:
#   * SKIPS if a benchmark harness is running (run_pilot / tb / harbor) — a
#     bounce mid-run would kill the task container.
#   * SKIPS if com.docker.backend RSS is below threshold (nothing to reclaim).
#   * Otherwise: quit Docker Desktop, relaunch, wait for engine health, verify
#     the nextcloud stack came back (its containers auto-restart with the VM).
#
# Usage: bash dev/docker_bounce.sh [--force]
#   OURO_BOUNCE_THRESHOLD_MB (default 2800) — backend RSS trigger.

set -u
LOG="$(cd "$(dirname "$0")" && pwd)/docker_bounce.log"
THRESH_MB="${OURO_BOUNCE_THRESHOLD_MB:-2800}"
TS="$(date +%Y-%m-%dT%H:%M:%S)"

say() { echo "$TS $*" | tee -a "$LOG"; }

# ---- gate 1: never bounce under a live benchmark run ----
if [ "${1:-}" != "--force" ]; then
  BUSY="$(pgrep -f 'swe_adapter.run_pilot|terminal_bench|tb_adapter|harbor' | head -1 || true)"
  if [ -n "$BUSY" ]; then
    say "SKIP: benchmark harness active (pid $BUSY) — no bounce mid-run"
    exit 0
  fi
fi

# ---- gate 2: only bounce when the backend OR the VM has actually bloated ----
# The VM process (com.apple.Virtualization) is where Rosetta translation cache
# + guest page cache accumulate after container-heavy work (a 140-container
# grading pass took it to ~15GB while backend sat at 888MB — backend alone is
# a blind gate).
VM_THRESH_MB="${OURO_BOUNCE_VM_THRESHOLD_MB:-10000}"
BACKEND_RSS_MB="$(ps axo rss=,command= | awk '/com\.docker\.backend/ && !/awk/ {if ($1>m) m=$1} END {print int(m/1024)}')"
VM_RSS_MB="$(ps axo rss=,command= | awk '/com\.apple\.Virtualization\.VirtualMachine/ && !/awk/ {if ($1>m) m=$1} END {print int(m/1024)}')"
if [ "${1:-}" != "--force" ] \
   && [ "${BACKEND_RSS_MB:-0}" -lt "$THRESH_MB" ] \
   && [ "${VM_RSS_MB:-0}" -lt "$VM_THRESH_MB" ]; then
  say "SKIP: backend ${BACKEND_RSS_MB}MB < ${THRESH_MB}MB and VM ${VM_RSS_MB}MB < ${VM_THRESH_MB}MB"
  exit 0
fi

say "BOUNCE: backend=${BACKEND_RSS_MB}MB VM=${VM_RSS_MB}MB (thresholds ${THRESH_MB}/${VM_THRESH_MB}MB) — restarting Docker Desktop"

# ---- restart via the Docker Desktop CLI (the supported path; the osascript
# quit AppleEvent is ignored when the dashboard window is closed) ----
if docker desktop restart >/dev/null 2>&1; then
  say "restart issued via docker desktop CLI"
else
  # fallback: quit + relaunch by hand
  osascript -e 'quit app "Docker Desktop"' 2>/dev/null || osascript -e 'quit app "Docker"' 2>/dev/null
  for i in $(seq 1 30); do
    pgrep -f 'com.docker.backend' >/dev/null || break
    sleep 2
  done
  if pgrep -f 'com.docker.backend' >/dev/null; then
    say "WARN: backend still alive after 60s quit wait — aborting (no force-kill)"
    exit 1
  fi
  open -a Docker
fi
READY=""
for i in $(seq 1 60); do
  if docker info >/dev/null 2>&1; then READY=1; break; fi
  sleep 3
done
if [ -z "$READY" ]; then
  say "FAIL: docker engine not healthy 180s after relaunch — check Docker Desktop"
  exit 1
fi

# ---- verify the nextcloud stack came back ----
sleep 10
NC_UP="$(docker ps --format '{{.Names}}' | grep -c nextcloud || true)"
NEW_RSS_MB="$(ps axo rss=,command= | awk '/com\.docker\.backend/ && !/awk/ {if ($1>m) m=$1} END {print int(m/1024)}')"
say "OK: bounced — backend ${BACKEND_RSS_MB}MB -> ${NEW_RSS_MB}MB, nextcloud containers up: ${NC_UP}"
if [ "${NC_UP:-0}" -lt 9 ]; then
  say "WARN: only ${NC_UP} nextcloud containers up (expect >=9) — may still be starting; re-check in a minute"
fi
