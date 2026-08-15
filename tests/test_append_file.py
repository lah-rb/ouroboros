"""append_file: the true-append effects primitive.

The databank JSONLs were appended via read-whole-file → write_file, whose
read-modify-write window drops concurrent appenders' lines — safe only while
every booking happened to be serial. append_file closes that window
(per-path lock + append mode) and heals a missing trailing newline so a
JSONL record can never concatenate onto a partial last line.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from agent.effects.local import LocalEffects
from agent.effects.mock import MockEffects


@pytest.mark.asyncio
async def test_concurrent_appends_lose_nothing(tmp_path):
    fx = LocalEffects(working_directory=str(tmp_path))

    async def one(i: int):
        return await fx.append_file("bank/records.jsonl", f'{{"k": {i}}}\n')

    results = await asyncio.gather(*(one(i) for i in range(50)))
    assert all(r.success for r in results)
    lines = (tmp_path / "bank" / "records.jsonl").read_text().splitlines()
    assert sorted(json.loads(ln)["k"] for ln in lines) == list(range(50))


@pytest.mark.asyncio
async def test_append_heals_missing_trailing_newline(tmp_path):
    (tmp_path / "bank").mkdir()
    (tmp_path / "bank" / "records.jsonl").write_text('{"k": "partial"}')  # no \n
    fx = LocalEffects(working_directory=str(tmp_path))
    out = await fx.append_file("bank/records.jsonl", '{"k": "next"}\n')
    assert out.success
    lines = (tmp_path / "bank" / "records.jsonl").read_text().splitlines()
    assert [json.loads(ln)["k"] for ln in lines] == ["partial", "next"]


@pytest.mark.asyncio
async def test_append_creates_file_and_dirs(tmp_path):
    fx = LocalEffects(working_directory=str(tmp_path))
    out = await fx.append_file("deep/new/dir/x.jsonl", '{"k": 1}\n')
    assert out.success
    assert (tmp_path / "deep" / "new" / "dir" / "x.jsonl").read_text() == '{"k": 1}\n'


@pytest.mark.asyncio
async def test_mock_append_matches_local_semantics():
    fx = MockEffects(files={"bank/records.jsonl": '{"k": "partial"}'})
    await fx.append_file("bank/records.jsonl", '{"k": "next"}\n')
    lines = fx._files["bank/records.jsonl"].splitlines()
    assert [json.loads(ln)["k"] for ln in lines] == ["partial", "next"]


@pytest.mark.asyncio
async def test_append_jsonl_routes_through_append_file():
    """The databank booker must use the append primitive, not write_file."""
    from agent.actions.scholarly_actions import append_records

    fx = MockEffects()
    await append_records(fx, [{"paper_key": "p1"}, {"paper_key": "p2"}])
    ops = [c.method for c in fx.calls]
    assert "append_file" in ops
    assert "write_file" not in ops
    lines = fx._files["databank/papers.jsonl"].strip().splitlines()
    assert [json.loads(ln)["paper_key"] for ln in lines] == ["p1", "p2"]
