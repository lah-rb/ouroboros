"""Who reads the figures, and whether the record says so truthfully.

fig_review moved from a private mlx_vlm.server child to LLMVP's own
/v1/vision on 2026-08-12. That changed TWO things at once and both are
pinned here:

  TRANSPORT — no VLM subprocess per dispatch; one HTTP call per figure to a
  server that is already resident.
  MODEL — the old FIG_MODEL (Qwen3-VL-8B-8bit) was a "mid-size default" that
  the 2026-08-11 bake-off had already beaten: its whole family lost the
  initial pass on speed and quality. The endpoint serves that bake-off's
  winner instead, and which model that is belongs to LLMVP's config rather
  than a constant here.

The provenance tests are the load-bearing ones. figtext_model must come
from the sidecar the run actually wrote, because under the llmvp backend
this code does not choose the model and a constant would record a guess —
including for papers processed before the backend changed.
"""

from __future__ import annotations

import json

import pytest

import agent.actions.curation_actions as ca
from agent.effects.mock import MockEffects


def _fig_cmd(fx) -> list[str]:
    """The argv fig_review was dispatched with."""
    runs = [c for c in fx._calls if c.method == "run_command"]
    assert runs, "fig_review was never dispatched"
    return list(runs[-1].args["command"])


def _si(inputs, effects):
    from agent.models import StepInput

    return StepInput(inputs=inputs, context={}, effects=effects)


def _bank(*keys) -> dict:
    return {
        "databank/papers.jsonl": "\n".join(
            json.dumps(
                {
                    "paper_key": k,
                    "access_status": "oa_pdf",
                    "extraction_status": "extracted",
                    "figure_count": 1,
                }
            )
            for k in keys
        )
    }


async def _dispatch(monkeypatch, backend: str | None):
    from agent.effects.protocol import CommandResult

    if backend is None:
        monkeypatch.delenv("OUROBOROS_FIG_BACKEND", raising=False)
    else:
        monkeypatch.setenv("OUROBOROS_FIG_BACKEND", backend)
    fx = MockEffects(files=_bank("a"))
    import os

    fx._commands[os.path.join(ca._repo_root(), ca._FIG_TOOL_PY)] = CommandResult(
        return_code=0,
        stdout=json.dumps({"paper_key": "a", "figtext_path": "x", "figs": 1}),
        stderr="",
        command="tool",
    )
    await ca.action_fig_review_batch(
        _si(
            {
                "paper_keys": ["a"],
                "working_directory": "/tmp/x",
                "mission_id": "m",
                "goal_id": "g",
                "flow_directive": "figs",
            },
            fx,
        )
    )
    return _fig_cmd(fx)


# ── which backend gets dispatched ─────────────────────────────────────


@pytest.mark.asyncio
async def test_the_default_dispatch_targets_llmvp_and_names_no_model(monkeypatch):
    """Under llmvp the caller does NOT choose the model — passing one would
    be a lie the server ignores."""
    cmd = await _dispatch(monkeypatch, None)
    assert cmd[cmd.index("--vl-backend") + 1] == "llmvp"
    assert "--model" not in cmd


@pytest.mark.asyncio
async def test_mlx_opt_in_names_no_model_unless_one_is_configured(monkeypatch):
    """The old default (Qwen3-VL-8B) was retired and its weights deleted.
    Keeping the name would make this path silently re-download 9.2GB of a
    beaten model; fig_review fails fast on a missing --model instead."""
    monkeypatch.delenv("OUROBOROS_FIG_MLX_MODEL", raising=False)
    cmd = await _dispatch(monkeypatch, "mlx")
    assert cmd[cmd.index("--vl-backend") + 1] == "mlx"
    assert "--model" not in cmd


@pytest.mark.asyncio
async def test_mlx_opt_in_carries_an_explicitly_configured_model(monkeypatch):
    """mlx_vlm.server loads per request, so the name IS the model."""
    monkeypatch.setenv("OUROBOROS_FIG_MLX_MODEL", "mlx-community/Some-VL-8bit")
    cmd = await _dispatch(monkeypatch, "mlx")
    assert cmd[cmd.index("--model") + 1] == "mlx-community/Some-VL-8bit"


@pytest.mark.asyncio
async def test_the_backend_is_read_per_call_not_at_import(monkeypatch):
    """A module-level constant would ignore the env of an already-imported
    module, which is exactly how a station override silently does nothing."""
    monkeypatch.setenv("OUROBOROS_FIG_BACKEND", "mlx")
    assert ca._fig_backend() == "mlx"
    monkeypatch.setenv("OUROBOROS_FIG_BACKEND", "llmvp")
    assert ca._fig_backend() == "llmvp"


# ── provenance: the record must name the model that actually read ─────


@pytest.mark.asyncio
async def test_figtext_model_comes_from_the_sidecar_the_run_wrote():
    fx = MockEffects(
        files={
            f"{ca.FIGTEXT_DIR}/p1.json": json.dumps(
                {"paper_key": "p1", "model": "muse-glimmer-30b", "figs": []}
            )
        }
    )
    assert await ca._figtext_model(fx, "p1") == "muse-glimmer-30b"


@pytest.mark.asyncio
async def test_a_paper_read_by_the_old_model_keeps_saying_so():
    """Provenance is a historical claim. Re-labelling an old paper with
    today's model would silently rewrite what happened to it."""
    fx = MockEffects(
        files={
            f"{ca.FIGTEXT_DIR}/old.json": json.dumps(
                {"paper_key": "old", "model": "Qwen3-VL-8B-Instruct-8bit", "figs": []}
            )
        }
    )
    assert await ca._figtext_model(fx, "old") == "Qwen3-VL-8B-Instruct-8bit"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "files",
    [
        {},  # no sidecar at all
        {f"{ca.FIGTEXT_DIR}/p1.json": json.dumps({"paper_key": "p1", "figs": []})},
        {f"{ca.FIGTEXT_DIR}/p1.json": json.dumps({"model": "  ", "figs": []})},
        {f"{ca.FIGTEXT_DIR}/p1.json": "not json"},
    ],
)
async def test_missing_or_blank_provenance_falls_back_rather_than_empty(files):
    """An empty figtext_model in a dataset envelope is worse than a stale
    one — it reads as 'no VLM involved'."""
    got = await ca._figtext_model(MockEffects(files=files), "p1")
    assert got and got.strip()


# ── the batch is budgeted in FIGURES, because figures are the cost ────


def _pending(**counts) -> dict:
    return {
        k: {
            "paper_key": k,
            "extraction_status": "extracted",
            "figure_count": n,
            "md_path": f"m/{k}.md",
        }
        for k, n in counts.items()
    }


def test_figure_heavy_papers_do_not_stack_past_the_budget():
    """Three 50-figure papers at the endpoint's ~37s/figure is ~90 minutes
    against a 3600s timeout — the dispatch would die having done the work,
    and every paper in it would book figtext_failed."""
    batch = ca._fig_batch(_pending(a=54, b=53, c=40))
    assert batch == ["a"], "a 54-figure paper must not be batched with more"
    assert sum(54 for _ in batch) <= ca.FIG_BATCH_FIGURES + 54


def test_small_papers_still_batch_up_to_the_paper_cap():
    """The figure budget must not turn every batch into one paper."""
    assert ca._fig_batch(_pending(a=2, b=3, c=2, d=2)) == ["a", "b", "c"]
    assert len(ca._fig_batch(_pending(a=2, b=3, c=2, d=2))) == ca.FIG_BATCH_SIZE


def test_a_paper_over_the_whole_budget_is_dispatched_ALONE_not_skipped():
    """Skipping it would hang the sweep forever on a paper it refuses to do."""
    assert ca._fig_batch(_pending(huge=500, small=1)) == ["huge"]


def test_the_budget_counts_figures_not_papers():
    batch = ca._fig_batch(_pending(a=30, b=30, c=30))
    assert batch == ["a", "b"], "third paper would exceed the figure budget"
