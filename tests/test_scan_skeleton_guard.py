"""Guard G2 — workspace scan as a bounded, navigable skeleton.

Pins the contract: the scan excludes vendor/cache/build dirs, skips large/binary
files before reading, caps the file count and per-file signature, and the
rendered listing has a total budget — so a dataset/cache tree (the 29M-token
eval-mteb overflow) can't balloon the prompt.
"""

from __future__ import annotations

import pytest

from agent.actions.refinement_actions import (
    _MAX_FILE_SIZE,
    _MAX_SCAN_FILES,
    _SIGNATURE_MAX_CHARS,
    _excluded,
    action_scan_project,
)
from agent.effects.mock import MockEffects
from agent.formatters import _LISTING_MAX_CHARS, format_project_listing
from agent.models import FlowMeta, StepInput


def _si(files, patterns=("*.py", "*.js", "*.json")):
    return StepInput(
        context={},
        params={"root": ".", "include_patterns": list(patterns)},
        meta=FlowMeta(flow_name="x", step_id="y"),
        effects=MockEffects(files=files),
    )


def test_excluded_dirs_and_egg_info():
    assert _excluded("node_modules/lib/x.js")
    assert _excluded("a/__pycache__/x.pyc")
    assert _excluded(".venv/lib/site.py")
    assert _excluded("foo.egg-info/PKG-INFO")
    assert not _excluded("src/main.py")


def test_non_ast_signature_is_head_tail_bounded():
    # Prompt economy (plan_charter): non-AST files (shell/config/data) were the
    # projection's verbosity hotspot — first-50-lines verbatim. Now bound head+tail
    # so the entry point AND the tail (shell scripts execute at the bottom) survive
    # without the verbose middle. Short files still render whole.
    from agent.actions.refinement_actions import _extract_signature

    short = "\n".join(f"line{i}" for i in range(20))
    out = _extract_signature("run.sh", short, "full")
    assert out == short  # at/under the threshold → whole file, no omission marker

    long = "\n".join(f"line{i}" for i in range(100))
    out = _extract_signature("run.sh", long, "full")
    assert "line0" in out and "line11" in out  # head (first 12)
    assert "line99" in out and "line82" in out  # tail (last 18)
    assert "line50" not in out and "omitted" in out  # verbose middle dropped
    assert len(out) < len(long)  # net smaller


@pytest.mark.asyncio
async def test_scan_excludes_vendor_and_skips_large_files():
    files = {
        "src/main.py": "def f(): pass",
        "node_modules/dep.js": "x" * 10,
        "data.json": "y" * (_MAX_FILE_SIZE + 100),  # too big — data, not source
    }
    out = await action_scan_project(_si(files))
    m = out.context_updates["project_manifest"]
    assert list(m.keys()) == ["src/main.py"]


@pytest.mark.asyncio
async def test_scan_caps_file_count():
    files = {f"f{i}.py": "x = 1" for i in range(_MAX_SCAN_FILES + 50)}
    out = await action_scan_project(_si(files))
    assert len(out.context_updates["project_manifest"]) == _MAX_SCAN_FILES
    assert out.result["scan_omitted"] == 50


@pytest.mark.asyncio
async def test_scan_byte_caps_minified_signature():
    # A minified file UNDER the size limit but one giant line — the 50-line cap
    # alone wouldn't bound it; the byte-cap must.
    files = {"bundle.js": "var x=" + "9," * 50_000}  # ~100KB, under _MAX_FILE_SIZE
    out = await action_scan_project(_si(files))
    sig = out.context_updates["project_manifest"]["bundle.js"]
    assert len(sig) <= _SIGNATURE_MAX_CHARS + 80


def test_listing_total_budget_and_omit_note():
    big = {f"f{i}.py": "x" * 200 for i in range(1000)}
    out = format_project_listing({"source": big}, {})
    assert len(out) < _LISTING_MAX_CHARS + 500
    assert "more files omitted" in out


def test_listing_small_manifest_unchanged():
    out = format_project_listing({"source": {"main.py": "def f(): ..."}}, {})
    assert "main.py" in out and "omitted" not in out


# ── Modality sidecars: deterministic digestion at the scan position ─────────
# (GAIA adoption finding: digestion must be a flow position, not a model choice)

from agent.actions.refinement_actions import (  # noqa: E402
    _ASR_TOOL_PY,
    _VL_TOOL_PY,
    _sidecar_repo_root,
)
from agent.effects.mock import CommandResult  # noqa: E402
from agent.persistence.models import MissionConfig, MissionState  # noqa: E402

import os  # noqa: E402
import asyncio  # noqa: E402

_VL_KEY = os.path.join(_sidecar_repo_root(), _VL_TOOL_PY)
_ASR_KEY = os.path.join(_sidecar_repo_root(), _ASR_TOOL_PY)


def _vision_mission(**cfg):
    base = dict(working_directory="/w", vision=True)
    base.update(cfg)
    return MissionState(objective="Count the red squares in the chart.",
                        status="active", config=MissionConfig(**base))


def _run(si):
    return asyncio.run(action_scan_project(si))


def _si_m(files, mission, commands=None, patterns=("*.py", "*.vltext", "*.transcript.txt"),
          host_tools=True):
    return StepInput(
        context={},
        params={"root": ".", "include_patterns": list(patterns)},
        meta=FlowMeta(flow_name="x", step_id="y"),
        effects=MockEffects(files=files, mission=mission, commands=commands or {},
                            supports_host_tools=host_tools),
    )


def test_image_digested_with_conditioned_prompt_and_sidecar_written():
    files = {"src/main.py": "print(1)", "chart.png": "\x89PNG"}
    cmds = {_VL_KEY: CommandResult(return_code=0, stdout="Red squares: 4, blue: 2",
                                   stderr="", command="vl")}
    si = _si_m(files, _vision_mission(), cmds)
    out = _run(si)
    fx = si.effects
    # sidecar written + surfaced in the manifest this same scan
    assert "chart.png.vltext" in fx.written_files
    assert "Red squares" in out.context_updates["project_manifest"]["chart.png.vltext"]
    # the invocation used the OBJECTIVE-CONDITIONED prompt (the AB winner)
    vl_calls = [c for c in fx.calls if c.method == "run_command"
                and c.args and _VL_KEY in " ".join(map(str, c.args))]
    joined = " ".join(map(str, vl_calls[0].args)) if vl_calls else str(
        [(c.method, c.args) for c in fx.calls])
    assert "pre-reading an image" in joined and "red squares" in joined.lower()


def test_existing_sidecar_skips_digestion():
    files = {"chart.png": "x", "chart.png.vltext": "already digested"}
    si = _si_m(files, _vision_mission())
    _run(si)
    # no NEW digestion: the vl tool was never invoked
    assert not [c for c in si.effects.calls if c.method == "run_command"]


def test_count_gate_skips_with_note():
    files = {f"img{i}.png": "x" for i in range(9)}
    si = _si_m(files, _vision_mission(modality_sidecar_max_images=6))
    out = _run(si)
    assert not [k for k in si.effects.written_files if k.endswith(".vltext")]
    note = out.context_updates["project_manifest"].get("[image sidecars]", "")
    assert "skipped" in note and "9" in note


def test_vision_off_and_container_effects_skip():
    files = {"chart.png": "x"}
    m_off = _vision_mission(vision=False)
    si = _si_m(files, m_off)
    _run(si)
    assert not [k for k in si.effects.written_files if k.endswith(".vltext")]
    si2 = _si_m(files, _vision_mission(), host_tools=False)
    _run(si2)
    assert not [k for k in si2.effects.written_files if k.endswith(".vltext")]


def test_audio_transcript_sidecar():
    files = {"talk.mp3": "x"}
    m = _vision_mission(vision=False, audio=True)
    cmds = {_ASR_KEY: CommandResult(return_code=0, stdout="[0:00] hello world",
                                    stderr="", command="asr")}
    si = _si_m(files, m, cmds)
    out = _run(si)
    assert "talk.mp3.transcript.txt" in si.effects.written_files
    assert "hello world" in out.context_updates["project_manifest"]["talk.mp3.transcript.txt"]


def test_tool_failure_never_fails_the_scan():
    files = {"chart.png": "x", "src/a.py": "pass"}
    cmds = {_VL_KEY: CommandResult(return_code=1, stdout="", stderr="mlx exploded",
                                   command="vl")}
    si = _si_m(files, _vision_mission(), cmds)
    out = _run(si)
    assert out.result["file_count"] >= 1  # scan succeeded
    assert "chart.png.vltext" not in si.effects.written_files
    assert "digestion failed" in out.context_updates["project_manifest"].get("[chart.png]", "")
