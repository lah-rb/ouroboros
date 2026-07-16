"""SWE-bench evaluation wrapper + gold-patch oracle.

Two entry points:
  - write_gold_predictions: emit a predictions JSONL whose model_patch is each
    instance's GOLD patch. Running the evaluator on it must resolve 100% — the
    go/no-go that validates the eval loop (images, grader, our JSONL shape)
    before trusting any model run. The vacuous-verification discipline applied
    to the harness itself: never trust a rate gate you haven't proven can fail.
  - run_evaluation: shell out to the official
    `python -m swebench.harness.run_evaluation` and parse its report.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess

from adapters.swe.instance import DATASET, SweInstance
from adapters.swe.patch import prediction_row

logger = logging.getLogger(__name__)

GOLD_MODEL = "gold"


def write_predictions(rows: list[dict], path: str) -> str:
    """Write predictions rows as JSONL (one object per line). Returns path."""
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    return path


def write_gold_predictions(instances: list[SweInstance], path: str) -> str:
    """Predictions whose model_patch is the gold patch — the oracle input."""
    rows = [
        prediction_row(inst.instance_id, GOLD_MODEL, inst.patch)
        for inst in instances
    ]
    return write_predictions(rows, path)


def run_evaluation(
    predictions_path: str,
    run_id: str,
    dataset: str = DATASET,
    max_workers: int = 4,
) -> dict:
    """Invoke the official evaluator; return the parsed report dict.

    The harness writes a `<model>.<run_id>.json` report in the cwd; we locate
    and load it. Raises on a non-zero evaluator exit (an infra failure — image
    build, docker — distinct from an unresolved instance, which is a normal
    report entry).
    """
    cmd = [
        "python", "-m", "swebench.harness.run_evaluation",
        "--dataset_name", dataset,
        "--predictions_path", predictions_path,
        "--run_id", run_id,
        "--max_workers", str(max_workers),
    ]
    logger.info("running evaluator: %s", " ".join(cmd))
    subprocess.run(cmd, check=True)
    return _load_report(predictions_path, run_id)


def _load_report(predictions_path: str, run_id: str) -> dict:
    """Find + parse the harness report JSON for this run."""
    import glob

    # The harness names it <model_name_or_path>.<run_id>.json. Derive the model
    # from the predictions (all rows share it); fall back to a glob.
    model = ""
    try:
        with open(predictions_path, encoding="utf-8") as f:
            first = json.loads(f.readline() or "{}")
            model = str(first.get("model_name_or_path", "") or "")
    except Exception:
        pass
    candidates = []
    if model:
        candidates.append(f"{model}.{run_id}.json")
    candidates.extend(glob.glob(f"*.{run_id}.json"))
    for path in candidates:
        if os.path.isfile(path):
            with open(path, encoding="utf-8") as f:
                return json.load(f)
    logger.warning("no evaluator report found for run_id=%s", run_id)
    return {}


def summarize(report: dict) -> dict:
    """Compact resolve summary from a harness report."""
    resolved = report.get("resolved_ids", []) or report.get("resolved_instances", [])
    total = (
        report.get("total_instances")
        or report.get("submitted_instances")
        or len(report.get("completed_ids", []) or [])
    )
    return {
        "resolved": len(resolved),
        "total": total,
        "resolved_ids": list(resolved),
        "unresolved_ids": list(
            report.get("unresolved_ids", [])
            or report.get("error_ids", [])
        ),
    }
