"""Phase 3 of the whole-if-it-fits rule (agent/context_fit.py): the context
blocks a prompt is assembled from never drop anything silently.

- ``tail_view`` is the shared tail fit for a reader that cannot read more;
- the repo map lists by name what its budget cannot hold, and the budget
  is the window's share unless a caller says otherwise;
- the schema appendix keeps whole sections and names the rest;
- the docs listing names what it does not show, with sizes;
- manifest signatures are whole indexes (every import, def, key, heading;
  the whole module docstring), byte-capped per LINE only;
- the localizer sends every symbol and sizes its evidence to the window.
"""

from __future__ import annotations

import pytest

from agent import context_fit as cf
from agent.actions.frame_actions import action_localize_fix_target
from agent.actions.refinement_actions import (
    _bound_minified_lines,
    _extract_markdown_signature,
    _extract_python_signature,
)
from agent.analysis_types import FileInfo, RepoMap, SymbolDef
from agent.effects.mock import MockEffects
from agent.formatters import format_project_docs
from agent.models import FlowMeta, StepInput
from agent.schema_extract import build_schema_context, build_schema_sections


class _Window(MockEffects):
    def __init__(self, n_ctx: int, **kw):
        super().__init__(**kw)
        self._n = n_ctx

    async def cache_health(self):
        return {"nCtxSeq": self._n}

    async def token_count(self, texts, model=""):
        return [len(t) // 4 for t in texts]


def _lines(n: int) -> str:
    return "\n".join(f"line {i} " + "x" * 50 for i in range(1, n + 1))


# ── tail_view ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_tail_view_returns_the_same_object_when_whole():
    text = _lines(20)
    out = await cf.tail_view(_Window(32768), text, used=1000)
    assert out is text


@pytest.mark.asyncio
async def test_tail_view_keeps_the_most_recent_part_under_a_marker():
    text = _lines(6000)
    out = await cf.tail_view(_Window(16384), text, used=2000, reserve=1024)
    marker, _, kept = out.partition("\n")
    assert marker.startswith("[… the first ") and "16,384-token window" in marker
    assert text.endswith(kept) and "line 6000" in kept
    assert (len(kept) // 4) + 2000 + 1024 <= 16384  # it fits what was free


# ── the repo map ────────────────────────────────────────────────────────


def _repo(n_files: int) -> RepoMap:
    files, ranks = {}, {}
    for i in range(n_files):
        path = f"pkg/mod{i:03d}.py"
        defs = [
            SymbolDef(
                name=f"f{i}_{k}",
                kind="function",
                file_path=path,
                line=k + 1,
                signature=f"def f{i}_{k}(a, b) -> int:",
            )
            for k in range(4)
        ]
        files[path] = FileInfo(path=path, definitions=defs)
        ranks[path] = 1.0 / (i + 1)  # rank order == file order
    return RepoMap(files=files, file_rankings=ranks)


def test_repo_map_names_every_file_its_budget_cannot_hold():
    out = _repo(60).format_for_prompt(max_chars=1200)
    assert (
        "pkg/mod000.py:" in out and "def f0_0(a, b) -> int:" in out
    )  # top-ranked, whole
    # Every file is present: in full, as a stub, or by name — never dropped.
    for i in range(60):
        assert f"pkg/mod{i:03d}.py" in out, i
    assert "more files with definitions, names only:" in out


def test_names_past_the_budget_roll_up_by_directory():
    out = _repo(60).format_for_prompt(max_chars=300)
    tail = out.split("names only: ")[1]
    assert tail.startswith("by directory: pkg/ (")  # accounted for, not a wall
    total = int(tail.split("(")[1].split(" ")[0])
    assert total + out.count("⋮...") + out.count("definitions)") == 60


def test_name_list_rolls_up_coarser_until_it_fits():
    paths = [f"data/raw/part{i:05d}.json" for i in range(5000)] + ["main.py"]
    assert cf.name_list(["a.py", "b.py"], 100) == "a.py, b.py"
    rolled = cf.name_list(paths, 200)
    assert rolled == "by directory: ./ (1 file), data/raw/ (5,000 files)"


def test_repo_map_default_budget_is_the_windows_share(monkeypatch):
    monkeypatch.setattr(cf, "_last_window", 262144)
    out = _repo(60).format_for_prompt()
    assert "names only" not in out and "definitions)" not in out  # all whole
    assert out.count("⋮...") == 60


# ── the schema appendix ─────────────────────────────────────────────────


def test_schema_sections_carry_their_file_and_join_to_the_context():
    files = {
        "world.json": '{"rooms": [{"id": "hall", "exits": {"north": "attic"}}]}',
        "engine.py": "def load(w):\n    return w['rooms'][0]['exits']\n",
    }
    sections = build_schema_sections(files)
    paths = [p for p, _ in sections]
    assert paths and paths[0] == "world.json"  # data skeletons first
    assert build_schema_context(files) == "\n\n".join(t for _, t in sections)


# ── the docs listing ────────────────────────────────────────────────────


def test_docs_beyond_the_share_are_named_with_their_size(monkeypatch):
    monkeypatch.setattr(cf, "_last_window", 4000)  # share ≈ 3,000 chars
    manifest = {
        "README.md": "# Play\n" + "Type north to go north.\n" * 100,  # ~2.4k
        "docs/manual.md": "# Manual\n" + "m" * 2000,
        "main.py": "def main(): ...",
    }
    out = format_project_docs({"source": manifest}, {})
    assert out.startswith("--- README.md ---")
    assert "Type north to go north." in out
    assert "not shown here (read them directly): docs/manual.md (2,009 chars)" in out
    assert "m" * 100 not in out  # named, not cut


# ── manifest signatures ─────────────────────────────────────────────────


def test_python_signature_is_a_whole_index():
    doc = ['"""Opening line.']
    doc += [f"Docstring line {i}." for i in range(2, 41)] + ['Last docstring line."""']
    imports = [f"import mod{i}" for i in range(30)]
    defs = [f"def fn{i}(x):" for i in range(45)]
    src = doc + imports + [""] + [d + "\n    return x" for d in defs]
    sig = _extract_python_signature("\n".join(src).splitlines(), "imports_and_exports")
    assert "Last docstring line." in sig  # a 41-line docstring is whole
    assert all(imp in sig for imp in imports)  # not 15
    assert all(d in sig for d in defs)  # not 20


def test_a_string_later_in_the_file_is_not_the_module_docstring():
    src = ["import os", "", "def f():", '    """Not the module doc."""', "    return 1"]
    sig = _extract_python_signature(src, "imports_and_exports")
    assert "Not the module doc" not in sig and "import os" in sig


def test_markdown_signature_lists_every_heading():
    heads = [f"## Section {i}" for i in range(25)]
    assert _extract_markdown_signature(heads).count("## Section") == 25


def test_only_a_minified_line_is_byte_capped():
    structured = "\n".join(f"def f{i}():" for i in range(400))  # ~5k chars, short lines
    assert _bound_minified_lines(structured) == structured
    minified = "var x=" + "9," * 5000
    out = _bound_minified_lines(minified)
    assert len(out) <= 1500 + 40 and out.endswith("…(line truncated)")


# ── the localizer's one-shot prompt ─────────────────────────────────────


class _Capture(_Window):
    def __init__(self, n_ctx: int, responses: list[str]):
        super().__init__(n_ctx, inference_responses=responses)
        self.prompts: list[str] = []

    async def run_inference(self, prompt, config_overrides=None, **kw):
        self.prompts.append(prompt)
        return await super().run_inference(prompt, config_overrides, **kw)


def _localize_input(effects, error_output: str, symbols: list[dict]) -> StepInput:
    return StepInput(
        context={"symbol_table": symbols},
        params={
            "target_file_path": "main.py",
            "error_output": error_output,
            "flow_directive": "Fix the startup crash",
        },
        effects=effects,
        meta=FlowMeta(flow_name="file_ops", step_id="run_localize"),
    )


@pytest.mark.asyncio
async def test_the_localizer_sends_every_symbol_and_fits_its_evidence():
    symbols = [
        {
            "name": f"Engine.m{i}",
            "kind": "method",
            "signature": f"def m{i}(self, a, b, c):",
        }
        for i in range(90)
    ]
    evidence = _lines(8000) + "\nAttributeError: 'str' object has no attribute 'x'\n"
    eff = _Capture(
        16384, ['```json\n{"target_symbol": "Engine.m3", "scope": "symbol"}\n```']
    )
    out = await action_localize_fix_target(_localize_input(eff, evidence, symbols))
    assert out.result.get("localized_symbol_in_ast") is True
    prompt = eff.prompts[0]
    assert (
        "Engine.m89" in prompt and "def m89(self, a, b, c):" in prompt
    )  # not 60 / 90 chars
    assert "[… the first " in prompt  # the evidence was fitted, not cut
    assert (
        "AttributeError: 'str' object has no attribute 'x'" in prompt
    )  # its end survives
    assert "line 1 " not in prompt  # the old head is what went
