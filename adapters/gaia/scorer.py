"""GAIA official scoring — a faithful port of the leaderboard's
``question_scorer`` (quasi-exact match with type-aware normalization):

  * gold parses as a number  → strip $ % , from the model answer, float-compare
  * gold contains , or ;     → element-wise compare after splitting, each
                               element scored as number-or-string
  * otherwise                → normalized string equality (whitespace removed,
                               punctuation removed, lower-cased)

Kept dependency-free and pure so it is trivially unit-testable and can never
drift with harness updates mid-campaign.
"""

from __future__ import annotations

import re
import string


def _is_float(x) -> bool:
    try:
        float(x)
        return True
    except (TypeError, ValueError):
        return False


def normalize_number_str(s: str) -> float:
    for ch in ("$", "%", ","):
        s = s.replace(ch, "")
    try:
        return float(s)
    except ValueError:
        return float("inf")  # never equal to a real gold number


def split_string(s: str, chars: str = ",;") -> list[str]:
    return re.split(f"[{chars}]", s)


def normalize_str(s: str, remove_punct: bool = True) -> str:
    s = re.sub(r"\s", "", s)
    if remove_punct:
        s = s.translate(str.maketrans("", "", string.punctuation))
    return s.lower()


def question_scorer(model_answer: str, ground_truth: str) -> bool:
    """True iff the model answer matches gold under GAIA's normalization."""
    model_answer = str(model_answer or "")
    ground_truth = str(ground_truth or "")

    if _is_float(ground_truth):
        return normalize_number_str(model_answer) == float(ground_truth)

    if any(c in ground_truth for c in (",", ";")):
        gt_elems = split_string(ground_truth)
        ma_elems = split_string(model_answer)
        if len(gt_elems) != len(ma_elems):
            return False
        ok = []
        for ma, gt in zip(ma_elems, gt_elems):
            if _is_float(gt):
                ok.append(normalize_number_str(ma) == float(gt))
            else:
                # official scorer keeps punctuation inside list elements
                ok.append(normalize_str(ma, remove_punct=False)
                          == normalize_str(gt, remove_punct=False))
        return all(ok)

    return normalize_str(model_answer) == normalize_str(ground_truth)
