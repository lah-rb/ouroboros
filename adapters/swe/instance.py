"""SWE-bench Verified instance access + the curated pilot subset.

Thin wrapper over the dataset so the runner needs only the fields it uses.
Dataset load is import-guarded (datasets/swebench are heavy, optional deps):
importing this module never requires them; only load_instances() does.
"""

from __future__ import annotations

from dataclasses import dataclass

DATASET = "princeton-nlp/SWE-bench_Verified"


@dataclass(frozen=True)
class SweInstance:
    instance_id: str
    repo: str
    base_commit: str
    problem_statement: str
    patch: str  # the GOLD patch (oracle mode only — never shown to the agent)
    test_patch: str

    @property
    def image_key(self) -> str:
        # The official per-instance eval image. SWE-bench normalizes the
        # instance id (double underscore) into the tag.
        norm = self.instance_id.replace("__", "_1776_")
        return f"swebench/sweb.eval.x86_64.{norm}:latest"

    @classmethod
    def from_row(cls, row: dict) -> "SweInstance":
        return cls(
            instance_id=row["instance_id"],
            repo=row.get("repo", ""),
            base_commit=row.get("base_commit", ""),
            problem_statement=row.get("problem_statement", ""),
            patch=row.get("patch", ""),
            test_patch=row.get("test_patch", ""),
        )


# Curated pilot, from the SMALLEST CODEBASES in Verified first (the
# editing-competence read — Verified has no langcodes-tiny repos, so requests/
# flask/pylint/pytest/astropy are the floor), then large-repo SCOUTS
# (django/sympy) so the first run maps the localization/scale wall explicitly.
# All ids verified present in princeton-nlp/SWE-bench_Verified (2026-07-04);
# load_instances warns + skips any id later absent.
# EVERY id below is GOLD-ORACLE-VERIFIED on this machine (2026-07-04: gold
# patch resolves under the official grader). The oracle rejected
# django-10097 + requests-1724/1766/2317 — psf/requests gold-fails
# systematically here (3 of 4 tried; ancient test infra under Rosetta), so
# exactly one requests instance made the cut. Never add a pilot id without a
# gold pass first (dev/swe_eval.sh --gold RUNID id1,id2).
PILOT_SMALL = [
    "psf__requests-1142",
    "pallets__flask-5014",
    "mwaskom__seaborn-3069",
    "pylint-dev__pylint-4551",
    "pylint-dev__pylint-4604",
    "pytest-dev__pytest-10051",
    "pytest-dev__pytest-10081",
    "astropy__astropy-12907",
    "astropy__astropy-13033",
]
PILOT_LARGE_SCOUTS = [
    "django__django-10554",
    "django__django-10880",
    "sympy__sympy-11618",
]
PILOT_INSTANCES = PILOT_SMALL + PILOT_LARGE_SCOUTS


def all_instance_ids(dataset: str = DATASET) -> list[str]:
    """Every instance_id in the dataset split, in dataset order.

    Cheap — reads only the id column, no SweInstance construction — so a full
    marathon launcher can enumerate + shuffle the 500 Verified ids without
    materializing every row. Raises ImportError (like load_instances) if the
    `datasets` dep is missing.
    """
    try:
        from datasets import load_dataset
    except ImportError as e:  # pragma: no cover - only without the dep
        raise ImportError(
            "adapters.swe.all_instance_ids needs the `datasets` package."
        ) from e
    ds = load_dataset(dataset, split="test")
    return [r["instance_id"] for r in ds]


def load_instances(
    instance_ids: list[str] | None = None, dataset: str = DATASET
) -> list[SweInstance]:
    """Load the named instances (default: PILOT_INSTANCES) from the dataset.

    Raises ImportError with a clear message if `datasets` isn't installed.
    Warns (does not fail) on any requested id absent from the dataset, so a
    stale pilot id degrades to "fewer instances", not a crash.
    """
    try:
        from datasets import load_dataset
    except ImportError as e:  # pragma: no cover - exercised only without the dep
        raise ImportError(
            "adapters.swe.load_instances needs the `datasets` package "
            "(added as a project dep; run `uv sync`)."
        ) from e

    wanted = instance_ids if instance_ids is not None else PILOT_INSTANCES
    wanted_set = set(wanted)
    ds = load_dataset(dataset, split="test")
    by_id = {r["instance_id"]: r for r in ds if r["instance_id"] in wanted_set}
    out: list[SweInstance] = []
    for iid in wanted:  # preserve requested order
        row = by_id.get(iid)
        if row is None:
            import logging

            logging.getLogger(__name__).warning(
                "pilot instance %s not found in %s — skipping", iid, dataset
            )
            continue
        out.append(SweInstance.from_row(row))
    return out
