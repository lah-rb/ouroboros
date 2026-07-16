"""Run Ouroboros over a SWE-bench pilot subset → a predictions JSONL.

    python -m adapters.swe.run_pilot --run-id swe-pilot-1 [--instances a,b,c]
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

from adapters.swe.evaluate import write_gold_predictions, write_predictions  # noqa: E402
from adapters.swe.instance import PILOT_INSTANCES, load_instances  # noqa: E402
from adapters._common import IncrementalPredictions


def main() -> None:
    ap = argparse.ArgumentParser(description="Run the SWE-bench pilot / full set")
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--instances", default="", help="comma-separated ids (default: pilot)")
    ap.add_argument("--all", action="store_true",
                    help="run the ENTIRE Verified set (overrides --instances)")
    ap.add_argument("--shuffle", action="store_true",
                    help="deterministically shuffle the id order (representative "
                    "prefix for a partial run); on by default with --all")
    ap.add_argument("--no-shuffle", action="store_true", help="keep dataset order under --all")
    ap.add_argument("--seed", type=int, default=0, help="shuffle seed (reproducible order)")
    ap.add_argument("--limit", type=int, default=0, help="cap to the first N instances (0=all)")
    ap.add_argument("--resume", action="store_true",
                    help="skip instances already present in predictions.jsonl")
    ap.add_argument("--model", default="ouroboros-gpt-oss")
    ap.add_argument("--wall", type=float, default=None, help="per-instance wall clock (s)")
    ap.add_argument("--out-dir", default=os.path.join(_REPO_ROOT, "runs", "swe"))
    ap.add_argument("--gold", action="store_true", help="write gold-patch oracle predictions")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if args.all:
        from adapters.swe.instance import all_instance_ids

        ids = all_instance_ids()
        if not args.no_shuffle:  # representative prefix for the partial overnight run
            import random

            random.Random(args.seed).shuffle(ids)
    else:
        ids = [s.strip() for s in args.instances.split(",") if s.strip()] or PILOT_INSTANCES
        if args.shuffle:
            import random

            random.Random(args.seed).shuffle(ids)
    if args.limit and args.limit > 0:
        ids = ids[: args.limit]

    preds_dir = os.path.join(args.out_dir, args.run_id)
    preds_path = os.path.join(preds_dir, "predictions.jsonl")

    if args.gold:
        instances = load_instances(ids)
        if not instances:
            sys.exit("No instances loaded — check the ids / dataset access.")
        path = write_gold_predictions(instances, preds_path)
        print(f"gold predictions ({len(instances)}) → {path}")
        return

    # Resume: seed rows with what's already graded-out, skip those ids.
    # Shared resume loader (adapters._common); SWE keeps its own writer
    # (write_predictions — the official predictions format, full rewrite).
    preds = IncrementalPredictions(preds_path, id_key="instance_id", load=args.resume)
    rows, done = preds.rows, preds.done
    if args.resume:
        if done:
            print(f"resume: {len(done)} instance(s) already done — skipping", flush=True)
        ids = [i for i in ids if i not in done]

    instances = load_instances(ids)
    if not instances:
        sys.exit("No instances loaded — check the ids / dataset access.")

    from adapters.swe.runner import (
        _PRUNE_MODE,
        _docker_client,
        _remove_image,
        run_instance,
    )

    logs_dir = os.path.join(preds_dir, "logs")
    # rows already seeded from the resume scan above (empty otherwise) — do NOT
    # reset, or a resumed run would drop the completed instances from the file.
    client = _docker_client()  # ONE client reused across instances (was leaked per-instance)
    pulled: list[str] = []  # images this run freshly pulled → run_end prune target
    try:
        for i, inst in enumerate(instances, 1):
            print(f"[{i}/{len(instances)}] {inst.instance_id}", flush=True)
            row, prune_img = run_instance(
                inst, args.model, logs_dir, wall_clock_s=args.wall, client=client
            )
            rows.append(row)
            if prune_img:
                pulled.append(prune_img)
            write_predictions(rows, preds_path)  # incremental — survive a mid-run stop
    finally:
        if _PRUNE_MODE == "run_end":
            for img in dict.fromkeys(pulled):  # dedupe, preserve order
                _remove_image(client, img)
        try:
            client.close()
        except Exception:
            pass
    print(f"predictions ({len(rows)}) → {preds_path} (prune={_PRUNE_MODE})")


if __name__ == "__main__":
    main()
