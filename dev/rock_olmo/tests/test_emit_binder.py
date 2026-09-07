"""The binder set: foundational papers presented at the reference rate."""

import json

from emit import SOURCE_WEIGHT, binder_keys, relabel_binder


def test_binder_sources_are_presented_more_than_once():
    """A binder that is shown once is not a binder."""
    assert SOURCE_WEIGHT["binder_text"] >= 2
    assert SOURCE_WEIGHT["binder_text"] == SOURCE_WEIGHT["binder_markdown"]
    assert SOURCE_WEIGHT["paper_text"] == 1


def test_relabel_touches_only_flagged_papers():
    recs = [
        {"source": "paper_text", "provenance": {"paper_key": "a"}},
        {"source": "paper_text", "provenance": {"paper_key": "b"}},
        {"source": "paper_markdown", "provenance": {}},
    ]
    n = relabel_binder(recs, {"a"}, "binder_text")
    assert n == 1
    assert [r["source"] for r in recs] == [
        "binder_text",
        "paper_text",
        "paper_markdown",
    ]


def test_binder_keys_honour_last_row_wins(tmp_path):
    """A later full row that clears the flag removes the paper; a later
    row that keeps it retains it. That is how un-flagging works without
    editing history."""
    rows = [
        {"paper_key": "keep", "binder": True},
        {"paper_key": "drop", "binder": True},
        {"paper_key": "never"},
        {"paper_key": "drop", "title": "rebooked without the flag"},
        {"paper_key": "keep", "binder": True, "review_status": "accepted"},
    ]
    path = tmp_path / "papers.jsonl"
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))
    assert binder_keys(str(path)) == {"keep"}


def test_binder_keys_missing_file_is_empty(tmp_path):
    assert binder_keys(str(tmp_path / "absent.jsonl")) == set()
