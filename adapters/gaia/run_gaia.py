"""GAIA campaign CLI — mirrors adapters.swe.run_pilot's marathon ergonomics.

  uv run python -m adapters.gaia.run_gaia --run-id gaia-l1-1 --levels 1
  uv run python -m adapters.gaia.run_gaia --run-id gaia-full-1 --resume

Writes runs/gaia/<run-id>/predictions.jsonl (one row per question, appended as
each finishes — resume-safe) and score_report.json (validation split is scored
locally with the official quasi-exact matcher as it goes).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import random
import sys

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--split", default="validation",
                    help="validation (165, locally scorable) | test (300, leaderboard)")
    ap.add_argument("--levels", default="",
                    help="comma-separated levels to include, e.g. 1 or 1,2 (default: all)")
    ap.add_argument("--limit", type=int, default=0, help="cap to first N after shuffle (0=all)")
    ap.add_argument("--seed", type=int, default=0, help="shuffle seed (reproducible order)")
    ap.add_argument("--no-shuffle", action="store_true", help="keep dataset order")
    ap.add_argument("--resume", action="store_true",
                    help="skip task_ids already in predictions.jsonl")
    ap.add_argument("--wall", type=float, default=None, help="per-question wall clock (s)")
    ap.add_argument("--out-dir", default=os.path.join(_REPO_ROOT, "runs", "gaia"))
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-5s %(message)s")

    from adapters.gaia.loader import load_questions
    from adapters.gaia.runner import run_question
    from adapters.gaia.scorer import question_scorer

    levels = tuple(int(x) for x in args.levels.split(",") if x.strip()) or None
    questions = load_questions(split=args.split, levels=levels)
    if not questions:
        sys.exit("No questions loaded — is the gated GAIA dataset accessible "
                 "(huggingface-cli login + accept terms)?")
    if not args.no_shuffle:
        random.Random(args.seed).shuffle(questions)
    if args.limit:
        questions = questions[: args.limit]

    out_dir = os.path.join(args.out_dir, args.run_id)
    logs_dir = os.path.join(out_dir, "logs")
    os.makedirs(logs_dir, exist_ok=True)
    preds_path = os.path.join(out_dir, "predictions.jsonl")

    done: set[str] = set()
    rows: list[dict] = []
    if args.resume and os.path.exists(preds_path):
        with open(preds_path) as f:
            for line in f:
                row = json.loads(line)
                rows.append(row)
                done.add(row["task_id"])
        print(f"[resume] {len(done)} already done")

    gold = {q.task_id: q.final_answer for q in questions}
    todo = [q for q in questions if q.task_id not in done]
    scorable = args.split == "validation"

    for i, q in enumerate(todo, 1):
        print(f"[{i}/{len(todo)}] L{q.level} {q.task_id}", flush=True)
        row = run_question(q, logs_dir, wall_clock_s=args.wall)
        if scorable:
            row["correct"] = question_scorer(row["model_answer"], gold[q.task_id])
        rows.append(row)
        with open(preds_path, "a") as f:
            f.write(json.dumps(row) + "\n")
        if scorable:
            n_ok = sum(1 for r in rows if r.get("correct"))
            print(f"    -> {row['model_answer']!r}  "
                  f"{'✓' if row.get('correct') else '✗'}   running: {n_ok}/{len(rows)}", flush=True)

    if scorable:
        by_level: dict[int, list[dict]] = {}
        for r in rows:
            by_level.setdefault(int(r["level"]), []).append(r)
        report = {
            "run_id": args.run_id,
            "total": len(rows),
            "correct": sum(1 for r in rows if r.get("correct")),
            "by_level": {
                lvl: {"total": len(rs), "correct": sum(1 for r in rs if r.get("correct"))}
                for lvl, rs in sorted(by_level.items())
            },
        }
        with open(os.path.join(out_dir, "score_report.json"), "w") as f:
            json.dump(report, f, indent=2)
        print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
