#!/bin/bash
# Copy run artifacts out of /tmp before anything that reboots the machine.
#
#   bash dev/preserve_run_artifacts.sh [label]
#
# WHY. Every benchmark artifact this project produces lands in /tmp/tier — the
# judged game artifacts, agent traces, degeneration evidence, blind-panel
# directories and their label KEYs. macOS clears /tmp on boot, so a system
# update, a kernel panic, or a memory experiment that wedges the machine takes
# all of it. It is ~60 MB; there is no reason to ever lose it.
#
# Idempotent and additive: re-running after a later arm lands picks up the new
# workdirs without disturbing what is already archived.
set -u
RUNS=$HOME/ouroboros-runs
LABEL=${1:-$(date '+%Y%m%d-%H%M%S')}
DEST=$RUNS/$LABEL

mkdir -p "$DEST"
echo "archiving -> $DEST"

n=0
for d in /tmp/tier /tmp/qwen_panel /tmp/laguna_panel /tmp/blind_panel; do
  [ -d "$d" ] || continue
  if cp -R "$d" "$DEST/" 2>/dev/null; then
    echo "  ok   $d  ($(du -sh "$d" | cut -f1))"
    n=$((n+1))
  else
    echo "  FAIL $d — NOT SAFE TO REBOOT"
    exit 1
  fi
done

if [ "$n" -eq 0 ]; then
  # An EMPTY /tmp is the safest state there is — there is nothing to lose. This
  # path must still emit the safety token, because callers gate on it: the Hy3
  # ladder refused to start after a reboot had already cleared /tmp, reading
  # "nothing to archive" as "archive failed". A guard that blocks on the safe
  # case teaches people to bypass it.
  echo "  nothing in /tmp to archive — it is already empty"
  rmdir "$DEST" 2>/dev/null || true   # don't leave an empty dated dir behind
  echo "SAFE TO REBOOT"
  exit 0
fi

# A manifest, because six months from now the directory names alone will not
# say which arm was which.
{
  echo "archived $(date '+%F %H:%M:%S')"
  echo "git HEAD: $(cd "$(dirname "$0")/.." && git rev-parse --short HEAD 2>/dev/null)"
  echo
  echo "workdirs and their recorded OUTCOME line:"
  for w in "$DEST"/tier/*/; do
    [ -f "$w/OUTCOME" ] || continue
    echo "  $(basename "$w")"
    sed 's/^/      /' "$w/OUTCOME"
  done
  echo
  echo "blind-panel label keys (unblinding maps):"
  for k in "$DEST"/*/KEY.json; do
    [ -f "$k" ] && { echo "  $k"; sed 's/^/      /' "$k"; }
  done
} > "$DEST/MANIFEST.txt"

echo "  total $(du -sh "$DEST" | cut -f1)   manifest: $DEST/MANIFEST.txt"
echo "SAFE TO REBOOT"
