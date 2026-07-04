"""Run Ouroboros over a SWE-bench pilot subset → a predictions JSONL.

    python -m swe_adapter.run_pilot --run-id swe-pilot-1 [--instances a,b,c]
                                    [--model ouroboros-gpt-oss] [--wall 1200]

Sequential (single LLMVP instance — no concurrency). The LLMVP server must be
up on OURO_LLMVP. Then feed the JSONL to dev/swe_eval.sh (or evaluate.py) for
grading. --gold writes the gold-patch oracle predictions instead of running
missions (no containers spun by us; the evaluator pulls its own).
"""

from __future__ import annotations

import argparse
import logging
import os
import sys

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from swe_adapter.evaluate import write_gold_predictions, write_predictions  # noqa: E402
from swe_adapter.instance import PILOT_INSTANCES, load_instances  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser(description="Run the SWE-bench pilot")
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--instances", default="", help="comma-separated ids (default: pilot)")
    ap.add_argument("--model", default="ouroboros-gpt-oss")
    ap.add_argument("--wall", type=float, default=None, help="per-instance wall clock (s)")
    ap.add_argument("--out-dir", default=os.path.join(_REPO_ROOT, "runs", "swe"))
    ap.add_argument("--gold", action="store_true", help="write gold-patch oracle predictions")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ids = [s.strip() for s in args.instances.split(",") if s.strip()] or PILOT_INSTANCES
    instances = load_instances(ids)
    if not instances:
        sys.exit("No instances loaded — check the ids / dataset access.")

    preds_dir = os.path.join(args.out_dir, args.run_id)
    preds_path = os.path.join(preds_dir, "predictions.jsonl")

    if args.gold:
        path = write_gold_predictions(instances, preds_path)
        print(f"gold predictions ({len(instances)}) → {path}")
        return

    from swe_adapter.runner import run_instance

    logs_dir = os.path.join(preds_dir, "logs")
    rows = []
    for i, inst in enumerate(instances, 1):
        print(f"[{i}/{len(instances)}] {inst.instance_id}", flush=True)
        row = run_instance(inst, args.model, logs_dir, wall_clock_s=args.wall)
        rows.append(row)
        write_predictions(rows, preds_path)  # incremental — survive a mid-run stop
    print(f"predictions ({len(rows)}) → {preds_path}")


if __name__ == "__main__":
    main()
