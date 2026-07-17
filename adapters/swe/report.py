"""Unified SWE-bench report: the agent's self-verdict beside the official grade.

Ouroboros is deliberately unyielding — a mission PARKS (budget / wall-clock)
with its goal still marked ``incomplete`` rather than declaring a soft win. Yet
the patch it left on disk can still RESOLVE the instance: the flask
should-raise case parked ``0/1`` but graded ✅. The runner already grades every
run regardless of how the mission ended (``run_instance`` extracts the patch in
a ``finally``); this report puts the two verdicts side by side so the
divergences are visible instead of being hidden behind the agent's own
(intentionally conservative) self-assessment:

  - UNDERCLAIM — parked / self-incomplete BUT graded resolved. The agent
    under-reported; the fix worked. (flask-5014.)
  - OVERCLAIM  — self-complete BUT graded unresolved. The agent believed it
    solved the task; the held-out test disagrees. The higher-signal failure.

Usage:
    python -m adapters.swe.report --run-id swe-loc-1 [--out-dir runs/swe]
                                 [--report PATH] [--json]
"""

from __future__ import annotations

import argparse
import json
import logging
import os

logger = logging.getLogger(__name__)

# Grade buckets in a harness report, in precedence order (an id in resolved_ids
# is resolved; the remaining buckets are mutually exclusive in practice).
_GRADE_ORDER = [
    ("resolved_ids", "resolved"),
    ("unresolved_ids", "unresolved"),
    ("empty_patch_ids", "empty_patch"),
    ("error_ids", "error"),
]

_GRADE_MARK = {
    "resolved": "✅ resolved",
    "unresolved": "❌ unresolved",
    "empty_patch": "∅ empty-patch",
    "error": "⚠ grader-error",
    "ungraded": "· ungraded",
}


def _self_verdict(logs_dir: str, instance_id: str) -> dict:
    """Read the preserved mission.json for an instance and summarize the agent's
    own verdict. Returns {found, status, goals_complete, goals_total}."""
    path = os.path.join(logs_dir, instance_id, "ouroboros-mission", "mission.json")
    verdict = {"found": False, "status": "", "goals_complete": 0, "goals_total": 0}
    try:
        with open(path, encoding="utf-8") as f:
            m = json.load(f)
    except Exception:
        return verdict
    goals = m.get("goals", []) or []
    verdict.update(
        found=True,
        status=str(m.get("status", "") or ""),
        goals_total=len(goals),
        goals_complete=sum(1 for g in goals if g.get("status") == "complete"),
    )
    return verdict


def _grade_for(instance_id: str, report: dict) -> str:
    for key, label in _GRADE_ORDER:
        if instance_id in (report.get(key, []) or []):
            return label
    return "ungraded"


def _divergence(grade: str, v: dict) -> str:
    """Classify the self-verdict vs grade mismatch (the informative signal)."""
    if not v["found"]:
        return "no-mission"  # the run left no mission.json (crash before persist)
    agent_solved = v["goals_total"] > 0 and v["goals_complete"] == v["goals_total"]
    if grade == "resolved" and not agent_solved:
        return "UNDERCLAIM"
    if grade == "unresolved" and agent_solved:
        return "OVERCLAIM"
    return ""


def build_rows(report: dict, logs_dir: str) -> list[dict]:
    """One row per instance THIS run actually attempted — the union of the four
    grade buckets only. ``submitted_ids`` / ``incomplete_ids`` are deliberately
    excluded: a harness report lists every dataset instance not in the
    predictions as incomplete, which would flood a subset run with hundreds of
    never-run instances. Each row carries grade + self-verdict + divergence;
    sorted so divergences (UNDERCLAIM / OVERCLAIM) surface first."""
    ids: list[str] = []
    seen: set[str] = set()
    for key, _label in _GRADE_ORDER:
        for iid in report.get(key, []) or []:
            if iid not in seen:
                seen.add(iid)
                ids.append(iid)

    rows = []
    for iid in ids:
        grade = _grade_for(iid, report)
        v = _self_verdict(logs_dir, iid)
        rows.append(
            {
                "instance_id": iid,
                "grade": grade,
                "self_status": v["status"] or ("—" if not v["found"] else ""),
                "goals_complete": v["goals_complete"],
                "goals_total": v["goals_total"],
                "self_found": v["found"],
                "divergence": _divergence(grade, v),
            }
        )
    # Divergences first (UNDERCLAIM, OVERCLAIM), then by grade, then id.
    _grade_rank = {
        "resolved": 0,
        "unresolved": 1,
        "empty_patch": 2,
        "error": 3,
        "ungraded": 4,
    }
    rows.sort(
        key=lambda r: (
            0 if r["divergence"] in ("UNDERCLAIM", "OVERCLAIM") else 1,
            _grade_rank.get(r["grade"], 9),
            r["instance_id"],
        )
    )
    return rows


def format_report(rows: list[dict], run_id: str = "") -> str:
    """Render the unified table: self-verdict beside the official grade."""
    if not rows:
        return "No graded instances found."
    id_w = max(len(r["instance_id"]) for r in rows)
    id_w = max(id_w, len("instance"))
    lines = []
    if run_id:
        lines.append(f"SWE unified report — {run_id}")
    header = f"{'instance':<{id_w}}  {'agent self-verdict':<22}  {'grade':<15}  note"
    lines.append(header)
    lines.append("-" * len(header))
    n_under = n_over = 0
    for r in rows:
        if r["self_found"]:
            self_col = f"{r['self_status']} {r['goals_complete']}/{r['goals_total']}"
        else:
            self_col = "no mission.json"
        note = r["divergence"]
        if note == "UNDERCLAIM":
            note = "◀ UNDERCLAIM (parked but patch resolved)"
            n_under += 1
        elif note == "OVERCLAIM":
            note = "▶ OVERCLAIM (self-complete, grader failed)"
            n_over += 1
        elif note == "no-mission":
            note = "(no self-verdict)"
        lines.append(
            f"{r['instance_id']:<{id_w}}  {self_col:<22}  "
            f"{_GRADE_MARK.get(r['grade'], r['grade']):<15}  {note}"
        )

    resolved = sum(1 for r in rows if r["grade"] == "resolved")
    self_solved = sum(
        1
        for r in rows
        if r["self_found"]
        and r["goals_total"] > 0
        and r["goals_complete"] == r["goals_total"]
    )
    lines.append("-" * len(header))
    lines.append(
        f"grade: {resolved}/{len(rows)} resolved   "
        f"agent self-solved: {self_solved}/{len(rows)}   "
        f"divergence: {n_under} underclaim, {n_over} overclaim"
    )
    return "\n".join(lines)


def unified_report(
    report: dict, logs_dir: str, run_id: str = ""
) -> tuple[list[dict], str]:
    """Build rows + render the table. Returns (rows, text)."""
    rows = build_rows(report, logs_dir)
    return rows, format_report(rows, run_id)


def main() -> None:
    ap = argparse.ArgumentParser(description="Unified SWE report: self-verdict + grade")
    ap.add_argument("--run-id", required=True)
    ap.add_argument(
        "--out-dir",
        default=os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "runs", "swe"
        ),
    )
    ap.add_argument(
        "--report",
        default="",
        help="path to the harness report JSON "
        "(default: locate <model>.<run_id>.json next to predictions)",
    )
    ap.add_argument("--json", action="store_true", help="emit rows as JSON")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    run_dir = os.path.join(args.out_dir, args.run_id)
    logs_dir = os.path.join(run_dir, "logs")

    if args.report:
        with open(args.report, encoding="utf-8") as f:
            report = json.load(f)
    else:
        from adapters.swe.evaluate import _load_report

        report = _load_report(os.path.join(run_dir, "predictions.jsonl"), args.run_id)
    if not report:
        raise SystemExit(
            f"No harness report for run_id={args.run_id}. Grade first "
            "(dev/swe_eval.sh / evaluate.run_evaluation), or pass --report PATH."
        )

    rows, text = unified_report(report, logs_dir, args.run_id)
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
    else:
        print(text)


if __name__ == "__main__":
    main()
