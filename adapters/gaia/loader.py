"""Load GAIA questions (validation split has gold answers for local scoring).

GAIA is a GATED HF dataset (`gaia-benchmark/GAIA`, gated=auto): the operator
must `huggingface-cli login` and accept the terms on the dataset page once.
Attachment files ride along in the dataset repo; rows carry `file_name` and
(with the official loading script) a resolved local `file_path`.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

_DATASET = "gaia-benchmark/GAIA"
_CONFIG = "2023_all"


@dataclass
class GaiaQuestion:
    task_id: str
    question: str
    level: int
    final_answer: str  # gold ("?" on the test split — not locally scorable)
    file_name: str = ""
    file_path: str = ""  # resolved local path to the attachment (if any)
    annotator_metadata: dict = field(default_factory=dict)

    @property
    def has_file(self) -> bool:
        return bool(self.file_name)


def _resolve_file(row: dict, split: str) -> str:
    """Best-effort local path for a row's attachment. Prefer the path the
    dataset loader resolved; fall back to hf_hub_download from the repo."""
    p = str(row.get("file_path", "") or "")
    if p and os.path.exists(p):
        return p
    name = str(row.get("file_name", "") or "")
    if not name:
        return ""
    try:
        from huggingface_hub import hf_hub_download

        return hf_hub_download(
            repo_id=_DATASET,
            repo_type="dataset",
            filename=f"2023/{split}/{name}",
        )
    except Exception as e:  # noqa: BLE001 — a missing attachment shouldn't kill the run
        logger.warning(
            "attachment fetch failed for %s (%s): %s", name, row.get("task_id"), e
        )
        return ""


def load_questions(
    split: str = "validation",
    levels: tuple[int, ...] | None = None,
    limit: int = 0,
) -> list[GaiaQuestion]:
    """Load GAIA questions. validation = 165 rows WITH gold answers (the local
    benchmark); test = 300 rows, answers withheld (leaderboard only)."""
    from datasets import load_dataset

    ds = load_dataset(_DATASET, _CONFIG, split=split)
    out: list[GaiaQuestion] = []
    for row in ds:
        level = int(row["Level"])
        if levels and level not in levels:
            continue
        out.append(
            GaiaQuestion(
                task_id=str(row["task_id"]),
                question=str(row["Question"]),
                level=level,
                final_answer=str(row.get("Final answer", "") or ""),
                file_name=str(row.get("file_name", "") or ""),
                file_path=_resolve_file(row, split),
                annotator_metadata=dict(row.get("Annotator Metadata") or {}),
            )
        )
        if limit and len(out) >= limit:
            break
    logger.info(
        "loaded %d GAIA %s questions%s",
        len(out),
        split,
        f" (levels {levels})" if levels else "",
    )
    return out
