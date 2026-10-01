#!/usr/bin/env python3
"""Point the mission's repack lanes at an engine: set llmvp_domains["repack_remote"].

Operator ruling 2026-10-01: the remote lane repacks on gemma-4-12b on the 3060
box. The agent builds repack_r1.. iff `llmvp_domains` carries "repack_remote"
AND OUROBOROS_REMOTE_REPACK_LANES > 0 (agent/scheduler/worker_pool._repack_lanes).
`seat_tokens` is the engine's per-stream window: the lane declines any paper
with a pack window that would not fit it, leaving it for the muse lanes.

Same mechanics and safeguards as dev/set_ocr_domain.py: the mission must be
STOPPED (mission.json is the canonical read path), the named model must be
hot/active on the target, the edit is atomic with a backup.

Usage:
  .venv/bin/python dev/set_repack_domain.py                    # dry run
  .venv/bin/python dev/set_repack_domain.py --apply
  .venv/bin/python dev/set_repack_domain.py --remove --apply   # no repack lane
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from set_ocr_domain import MISSION, mission_is_running, model_state  # noqa: E402

DOMAIN = "repack_remote"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--remove", action="store_true")
    ap.add_argument("--endpoint", default="http://192.168.1.76:8008/graphql")
    ap.add_argument("--model", default="gemma-4-12b-3060")
    ap.add_argument("--seat-tokens", type=int, default=49152)
    ap.add_argument("--no-preflight", action="store_true")
    args = ap.parse_args()

    if mission_is_running():
        print("REFUSING: mission process is running — stop it first.")
        return 2
    with open(MISSION, encoding="utf-8") as fh:
        m = json.load(fh)
    cfg = m.setdefault("config", {})
    domains = dict(cfg.get("llmvp_domains") or {})
    print("current llmvp_domains:", json.dumps(domains, indent=1))

    if args.remove:
        if DOMAIN not in domains:
            print(f"no {DOMAIN} domain set — nothing to remove")
            return 0
        domains.pop(DOMAIN)
        print(f"→ {DOMAIN} REMOVED (no repack lane will be built)")
    else:
        if not args.no_preflight:
            state = model_state(args.endpoint, args.model)
            if state is None:
                print(f"REFUSING: {args.endpoint} not reachable")
                return 3
            if state not in ("hot", "active"):
                print(
                    f"REFUSING: {args.model!r} is {state or 'unknown'!r} on {args.endpoint}"
                )
                return 3
            print(f"preflight: {args.model!r} is {state} on {args.endpoint}")
        domains[DOMAIN] = {
            "endpoint": args.endpoint,
            "model": args.model,
            "seat_tokens": args.seat_tokens,
        }
        print(f"→ {DOMAIN}:", json.dumps(domains[DOMAIN]))

    if not args.apply:
        print("DRY RUN — pass --apply to write.")
        return 0
    cfg["llmvp_domains"] = domains
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    backup = MISSION + f".bak-{stamp}"
    shutil.copy2(MISSION, backup)
    tmp = MISSION + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(m, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, MISSION)
    print(f"written; backup at {backup}")
    print("start with OUROBOROS_REMOTE_REPACK_LANES=1 for the lane to exist.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
