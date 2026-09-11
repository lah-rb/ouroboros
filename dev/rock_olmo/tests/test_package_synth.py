"""package_synth's pure helpers (root venv; the packer run itself needs the rock venv)."""

import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from package_synth import (
    parse_mix,
    pick_to_budget,
    plan_budgets,
    source_tag,
    tagged,
)  # noqa: E402


def test_budgets_follow_the_mix():
    b = plan_budgets(10_000_000, (60, 20, 20))
    assert b["total"] == 16_666_666 and b["carry"] == b["replay"] == 3_333_333
    assert parse_mix("60,20,20") == (60, 20, 20)


def test_source_tags_and_normalised_carry():
    assert source_tag({"source": "synthetic/raman"}) == "synthetic/raman"
    assert (
        source_tag({"source": "reference", "provenance": {"source": "NIST ASD"}})
        == "reference/nist_asd"
    )
    assert (
        source_tag({"source": "paper_markdown"}) == "paper"
        and source_tag({"source": "binder_markdown"}) == "paper"
    )
    assert (
        source_tag({"source": "replay/pes2o"}) == "replay/pes2o"
        and source_tag({"source": "hom"}) == "hom"
    )
    d = {"source": "paper_markdown", "text": "Magnetite (Fe_{3}O_{4}) was measured."}
    assert (
        tagged(d, normalise=True) == "[source: paper]\nMagnetite (Fe3O4) was measured."
    )
    assert tagged(d, normalise=False).endswith("Fe_{3}O_{4}) was measured.")


def test_pick_to_budget_takes_whole_docs():
    docs = [{"doc_id": str(i), "text": "x" * 350, "tokens_est": 100} for i in range(50)]
    out = pick_to_budget(docs, 1000, random.Random(0))
    assert 9 <= len(out) <= 10 and len({d["doc_id"] for d in out}) == len(out)
    assert pick_to_budget(docs, 0, random.Random(0)) == []
