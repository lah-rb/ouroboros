"""fig_review banks every figure as it lands (2026-10-01).

The bank used to be written only after the paper's loop, so one failed or timed-out
figure discarded every reading of the run: in run v50g, 30-40 minute figtext rounds
banked nothing. Now each reading is saved at once and the report still carries the
progress, so the next round resumes where this one stopped.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys

_TOOL = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "..",
    "tools",
    "fig_review",
    "fig_review.py",
)


def _load():
    spec = importlib.util.spec_from_file_location("_fig_review_bank_test", _TOOL)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


_M = _load()


def _paper(tmp_path, n=3):
    figs = tmp_path / "figures" / "k1"
    figs.mkdir(parents=True)
    for i in range(n):
        (figs / f"fig_{i}.png").write_bytes(b"png")
    md = tmp_path / "markdown"
    md.mkdir()
    (md / "k1.md").write_text("Raman bands at 1086 and 282 cm-1.")
    return str(tmp_path / "figures"), str(md), str(tmp_path / "out")


def test_a_failure_mid_paper_keeps_the_figures_already_read(tmp_path, monkeypatch):
    figures_root, md_dir, out_dir = _paper(tmp_path)
    calls = []

    def chat(endpoint, model, image_path, caption, send_model):
        calls.append(os.path.basename(image_path))
        if len(calls) == 3:
            raise RuntimeError("All inference instances are busy [vision]")
        return f"reading of {calls[-1]}", "muse"

    monkeypatch.setattr(_M, "_chat_figure", chat)
    rep = _M.review_paper(
        "http://x/graphql", "muse", "k1", figures_root, md_dir, out_dir
    )
    assert "busy" in rep["error"]
    assert rep["described"] == 2 and rep["remaining"] == 1 and rep["figs"] == 2
    bank = json.load(open(os.path.join(out_dir, "k1.json")))
    assert [e["fig"] for e in bank["figs"]] == ["fig_0.png", "fig_1.png"]

    # The next round resumes: only the missing figure is asked for.
    calls.clear()
    monkeypatch.setattr(_M, "_chat_figure", lambda *a: ("third", "muse"))
    rep = _M.review_paper(
        "http://x/graphql", "muse", "k1", figures_root, md_dir, out_dir
    )
    assert rep["error"] == "" and rep["described"] == 3 and rep["remaining"] == 0


def test_the_fleet_request_outlasts_the_server_seat_wait():
    import inspect

    timeout = inspect.signature(_M._graphql_vision).parameters["timeout"].default
    assert timeout > 900  # LLMVP queues a vision stream up to 900 s for a seat
