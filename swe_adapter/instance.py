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


# Curated pilot: small-repo instances first (editing-competence read — the
# langcodes/marshmallow/flask-scale repos where the repair loop is proven),
# then a few large-repo SCOUTS (django/sympy/matplotlib) so the first run maps
# the localization/scale wall explicitly rather than hiding it. These ids are
# real SWE-bench_Verified instances; adjust after the first dataset load
# confirms availability (load_instances warns on any id not in the dataset).
PILOT_SMALL = [
    "marshmallow-code__marshmallow-1359",
    "pvlib__pvlib-python-1854",
    "pydicom__pydicom-1256",
    "sqlfluff__sqlfluff-1625",
    "pylint-dev__astroid-1866",
    "pallets__flask-4045",
]
PILOT_LARGE_SCOUTS = [
    "django__django-11099",
    "sympy__sympy-20154",
    "matplotlib__matplotlib-23913",
]
PILOT_INSTANCES = PILOT_SMALL + PILOT_LARGE_SCOUTS


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
            "swe_adapter.load_instances needs the `datasets` package "
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
