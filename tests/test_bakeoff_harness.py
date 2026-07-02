"""Bake-off harness: curator templates render, paper picking, needle.

The harness imports the REAL prompts and REAL gates — these tests pin
the template contracts (placeholders resolve, retry section skips when
empty) and the deterministic harness helpers, without any server.
"""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path

from agent.actions.curation_actions import format_key_registry, update_key_registry
from agent.loader import PromptRenderer

_ROOT = Path(__file__).parent.parent


def _renderer() -> PromptRenderer:
    return PromptRenderer(_ROOT / "prompts")


def _bakeoff_module():
    spec = importlib.util.spec_from_file_location(
        "bakeoff_text", _ROOT / "dev" / "bakeoff_text.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_review_template_renders_clean():
    out = _renderer().render(
        "curator/review_paper", {"input": {}, "context": {}, "meta": {}}
    )
    assert "sceptical materials-science data curator" in out
    assert '"verdict": "accept"' in out  # JSON exemplar braces intact
    assert "{context." not in out  # no unresolved placeholders


def test_pack_template_renders_registry_and_skips_empty_feedback():
    registry = {}
    update_key_registry(registry, {"yield_strength_mpa": 759}, "p1")
    ns = {
        "input": {},
        "context": {
            "key_registry_block": format_key_registry(registry),
            "gate_feedback": "",
        },
        "meta": {},
    }
    out = _renderer().render("curator/pack_data", ns)
    assert "yield_strength_mpa" in out
    assert "previous pack attempt FAILED" not in out  # when: skipped

    ns["context"]["gate_feedback"] = "ungrounded: data.creep_rate token 3.1e-7"
    out2 = _renderer().render("curator/pack_data", ns)
    assert "previous pack attempt FAILED" in out2
    assert "3.1e-7" in out2


def test_pick_papers_prefers_mixed_sizes_with_largest_tail(tmp_path):
    bk = _bakeoff_module()
    databank = tmp_path / "databank"
    (databank / "markdown").mkdir(parents=True)
    lines = []
    for i in range(8):
        key = f"p{i}"
        (databank / "markdown" / f"{key}.md").write_text("x" * (1000 * (i + 1)))
        lines.append(
            json.dumps(
                {
                    "paper_key": key,
                    "extraction_status": "extracted",
                    "md_path": f"databank/markdown/{key}.md",
                }
            )
        )
    lines.append(
        json.dumps({"paper_key": "failed", "extraction_status": "extract_failed"})
    )
    (databank / "papers.jsonl").write_text("\n".join(lines) + "\n")

    picked = bk._pick_papers(databank, 5)
    keys = [p["paper_key"] for p in picked]
    assert len(keys) == 5
    assert "failed" not in keys
    # The two largest are always in (needle hosts).
    assert {"p6", "p7"} <= set(keys)


def test_needle_insertion_lands_deep():
    bk = _bakeoff_module()
    doc = "\n\n".join(f"paragraph {i}" for i in range(10))
    out = bk._insert_needle(doc)
    assert bk.NEEDLE in out
    assert out.index(bk.NEEDLE) > out.index("paragraph 6")


def test_gold_file_parses():
    gold = json.loads((_ROOT / "dev" / "bakeoff_gold.json").read_text())
    assert "__instructions__" in gold


def test_vision_harness_importable_and_samples(tmp_path):
    spec = importlib.util.spec_from_file_location(
        "bakeoff_vision", _ROOT / "dev" / "bakeoff_vision.py"
    )
    bv = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bv)

    databank = tmp_path / "databank"
    for key in ("pA", "pB"):
        (databank / "figures" / key).mkdir(parents=True)
        (databank / "markdown").mkdir(exist_ok=True, parents=True)
        for j in range(2):
            (databank / "figures" / key / f"fig_0{j}.png").write_bytes(b"png")
        (databank / "markdown" / f"{key}.md").write_text(
            f"See ../figures/{key}/fig_00.png here."
        )
    figs = bv._sample_figures(databank, 3)
    # Round-robin: one from each paper first, then seconds.
    assert [f[0] for f in figs] == ["pA", "pB", "pA"]
    assert figs[0][2] != ""  # caption located for referenced figure


def test_mtmd_candidates_have_expected_paths():
    spec = importlib.util.spec_from_file_location(
        "bakeoff_vision", _ROOT / "dev" / "bakeoff_vision.py"
    )
    bv = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bv)
    for name, paths in bv.MTMD_MODELS.items():
        assert os.path.isfile(os.path.expanduser(paths["gguf"])), name
        assert os.path.isfile(os.path.expanduser(paths["mmproj"])), name
