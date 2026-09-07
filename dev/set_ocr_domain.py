#!/usr/bin/env python3
"""Point the mission's ocr lane at another fleet: set llmvp_domains["ocr"].

Operator-directed (2026-09-06): paddle must not be resident on this box's
CUDA1 at the 131,072-cell seat, so OCR runs on the remote fleet. The agent
builds the ocr lane as a REMOTE lane iff `llmvp_domains` carries an "ocr"
key (agent/scheduler/worker_pool._ocr_lane) and hands the OCR tool that
endpoint and model explicitly (extraction_actions._ocr_route_argv).

Mechanics follow dev/add_foundations_goals.py: mission.json is the canonical
READ path (the Loro journal re-bootstraps from it when the file changes), so
it is edited while the mission is STOPPED, atomically, with a backup.

PREFLIGHT, because a lane pointed at a cold or unreachable paddle fails every
round at the tool's own preflight ("cannot serve"): before writing, this asks
the target fleet's GraphQL registry and refuses unless the named model is
hot/active. Pass --no-preflight to write anyway (e.g. staging the config
before the remote is up; the mission must not be started until it is).

Usage:
  .venv/bin/python dev/set_ocr_domain.py                      # dry run
  .venv/bin/python dev/set_ocr_domain.py --apply
  .venv/bin/python dev/set_ocr_domain.py --remove --apply      # back to local paddle
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import urllib.request
from datetime import datetime, timezone

MISSION = os.path.expanduser("~/corpora/ouroboros-spectra/.agent/mission.json")
DEFAULT_ENDPOINT = "http://192.168.1.209:8008/graphql"
DEFAULT_MODEL = "paddle-ocr-vl-mac"


def mission_is_running() -> bool:
    try:
        out = subprocess.run(
            ["ps", "-Ao", "command="], capture_output=True, text=True, timeout=10
        ).stdout
    except Exception:  # noqa: BLE001
        return True
    return any(
        "ouroboros.py start" in ln for ln in out.splitlines() if "grep" not in ln
    )


def model_state(endpoint: str, model: str) -> str | None:
    """'active' | 'hot' | 'cold' | '' (unknown name) | None (unreachable)."""
    req = urllib.request.Request(
        endpoint,
        data=json.dumps({"query": "{ models { name state } }"}).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.load(r)
    except Exception:  # noqa: BLE001
        return None
    for m in (data.get("data") or {}).get("models") or []:
        if m.get("name") == model:
            return str(m.get("state") or "")
    return ""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--remove", action="store_true", help="delete the ocr domain")
    ap.add_argument("--endpoint", default=DEFAULT_ENDPOINT)
    ap.add_argument("--model", default=DEFAULT_MODEL)
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
        if "ocr" not in domains:
            print("no ocr domain set — nothing to remove")
            return 0
        domains.pop("ocr")
        print("→ ocr domain REMOVED (lane returns to local paddle)")
    else:
        if not args.no_preflight:
            state = model_state(args.endpoint, args.model)
            if state is None:
                print(f"REFUSING: {args.endpoint} not reachable")
                return 3
            if state == "":
                print(f"REFUSING: {args.model!r} is not in that fleet's registry")
                return 3
            if state not in ("hot", "active"):
                print(
                    f"REFUSING: {args.model!r} is {state!r} on {args.endpoint} — "
                    "load it there first (every OCR round would fail its preflight)"
                )
                return 3
            print(f"preflight: {args.model!r} is {state} on {args.endpoint}")
        domains["ocr"] = {"endpoint": args.endpoint, "model": args.model}
        print("→ ocr domain:", json.dumps(domains["ocr"]))

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
    print(
        "relaunch WITHOUT ocr in OUROBOROS_DISABLE_LANES; the first ocr round "
        "should book extraction_method paddleocr-vl-1.6-q8_0-llmvp with the "
        "local GPUs unchanged."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
