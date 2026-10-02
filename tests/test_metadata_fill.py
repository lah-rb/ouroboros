"""tools/metadata_fill.py: missing bibliographic fields, filled with provenance (2026-10-02).

The closed-shelf books were catalogued from filenames and hundreds of accepted
papers lack authors or a venue. The tool fills only empty or provisional
fields, from Crossref, Open Library and the document's own front matter --
and a front-matter value is booked only when it can be found in that text.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from agent.effects.mock import MockEffects
from agent.effects.protocol import HttpResult

_spec = importlib.util.spec_from_file_location(
    "metadata_fill", Path(__file__).resolve().parents[1] / "tools" / "metadata_fill.py"
)
mf = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mf)

FRONT = """# PRINCIPLES OF INSTRUMENTAL ANALYSIS

FIFTH EDITION

Douglas A. Skoog
F. James Holler
Timothy A. Nieman

Saunders College Publishing

Copyright © 1998 by Harcourt Brace & Company
ISBN: 0-03-002078-6
"""


def _book(**kw) -> dict:
    return {
        "paper_key": "book_principals",
        "record_kind": "book",
        "title": "Principals Of Instrumental Analysis Fifth Edition Skoog Holler Nieman",
        "authors": [],
        "year": 0,
        "metadata_note": "title from the filename; verify against the title page",
        **kw,
    }


def test_only_empty_or_provisional_fields_are_fillable():
    assert mf.fillable_fields(_book()) == list(mf.BOOK_FIELDS)
    paper = {"paper_key": "p", "title": "T", "authors": ["A B"], "year": 2020}
    assert mf.fillable_fields(paper) == ["venue"]
    assert mf.fillable_fields({**paper, "venue": "J"}) == []


def test_isbns_are_found_by_label_and_checksum():
    assert mf.isbn_valid("0030020786") and mf.isbn_valid("9780030020780")
    assert not mf.isbn_valid("0030020787")
    assert mf.printed_isbns(FRONT) == ["0030020786"]
    assert mf.printed_isbns("call 0-03-002078-6 now") == [], "a label is required"


def test_a_front_matter_value_must_be_in_the_text():
    assert mf.verified("title", "Principles of Instrumental Analysis", FRONT)
    assert not mf.verified("title", "Principals of Instrumental Analysis", FRONT)
    assert mf.verified("authors", ["Douglas A. Skoog", "Timothy A. Nieman"], FRONT)
    assert not mf.verified("authors", ["Douglas A. Skoog", "Stanley R. Crouch"], FRONT)
    assert mf.verified("year", 1998, FRONT) and not mf.verified("year", 2007, FRONT)
    assert mf.verified("isbn", "0030020786", FRONT)


def _fx(tmp_path: Path, rec: dict, model: dict, http: dict | None = None):
    (tmp_path / "databank" / "markdown").mkdir(parents=True)
    (tmp_path / "databank" / "markdown" / f"{rec['paper_key']}.md").write_text(FRONT)
    return MockEffects(
        files={"databank/papers.jsonl": json.dumps(rec) + "\n"},
        inference_responses=[json.dumps(model)],
        http_responses=http or {},
    )


@pytest.mark.asyncio
async def test_a_book_is_filled_from_its_title_page_and_a_guess_is_dropped(tmp_path):
    model = {
        "title": "Principles of Instrumental Analysis",
        "authors": ["Douglas A. Skoog", "F. James Holler", "Timothy A. Nieman"],
        "year": "1998",
        "edition": "Fifth Edition",
        "publisher": "Saunders College Publishing",
        "isbn": "0-03-002078-6",
        "venue": "Brooks Cole",  # not printed: must be dropped
    }
    fx = _fx(tmp_path, _book(), model)
    p = await mf.plan_one(fx, tmp_path, _book(), llm=True, web=False)
    fill = {f: x["value"] for f, x in p["fill"].items()}
    assert fill == {
        "title": "Principles of Instrumental Analysis",
        "authors": ["Douglas A. Skoog", "F. James Holler", "Timothy A. Nieman"],
        "year": 1998,
        "edition": "Fifth Edition",
        "publisher": "Saunders College Publishing",
        "isbn": "0030020786",
    }
    assert {x["source"] for x in p["fill"].values()} == {"front_matter"}
    assert "venue" not in fill, "a book has no venue field to fill"


@pytest.mark.asyncio
async def test_a_paper_takes_crossref_first_and_never_overwrites(tmp_path):
    rec = {
        "paper_key": "p",
        "title": "Kept",
        "doi": "10.1/x",
        "authors": [],
        "year": 2020,
    }
    url = mf._CROSSREF + "10.1/x"
    msg = {
        "title": ["Crossref title"],
        "author": [{"given": "Ada", "family": "Lovelace"}],
        "issued": {"date-parts": [[2019]]},
        "container-title": ["Journal of Tests"],
    }
    fx = _fx(
        tmp_path,
        rec,
        {},
        http={url: HttpResult(status=200, url=url, json_data={"message": msg})},
    )
    p = await mf.plan_one(fx, tmp_path, rec, llm=False, web=True)
    assert {f: x["value"] for f, x in p["fill"].items()} == {
        "authors": ["Ada Lovelace"],
        "venue": "Journal of Tests",
    }, "title and year are present: never replaced"


@pytest.mark.asyncio
async def test_apply_books_the_plan_onto_a_fresh_row(tmp_path, monkeypatch):
    rec = _book()
    fx = _fx(tmp_path, rec, {})
    (tmp_path / "databank" / "papers.jsonl").write_text(
        json.dumps({**rec, "figtext_status": "figtext_done"}) + "\n"
    )
    plan = {
        "plans": [
            {
                "paper_key": rec["paper_key"],
                "fill": {
                    "title": {
                        "value": "Principles of Instrumental Analysis",
                        "source": "front_matter",
                    },
                    "year": {"value": 1998, "source": "front_matter"},
                },
            }
        ]
    }
    (tmp_path / mf.PLAN_FILE).write_text(json.dumps(plan))

    class A:
        root = str(tmp_path)

    assert await mf.cmd_apply(A()) == 0
    rows = (tmp_path / "databank" / "papers.jsonl").read_text().splitlines()
    last = json.loads(rows[-1])
    assert (
        last["title"] == "Principles of Instrumental Analysis" and last["year"] == 1998
    )
    assert (
        last["figtext_status"] == "figtext_done"
    ), "the row was re-read, not the plan's"
    assert last["metadata_sources"] == {"title": "front_matter", "year": "front_matter"}
    assert "title from the filename" not in last["metadata_note"]
    assert "title" not in mf.fillable_fields(last), "no longer provisional"
