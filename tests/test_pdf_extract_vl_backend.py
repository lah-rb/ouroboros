"""The VL backend is an either/or, and its slot knob must not degrade output.

PaddleOCR-VL's layout pipeline is the same either way; only the engine
answering the per-region VLM calls changes (paddleocr 3.7 supports both
`mlx-vlm-server` and `llama-cpp-server` over one OpenAI-shaped client).

THE REGRESSION THIS FILE EXISTS FOR: llama-server's -c is the TOTAL KV cache
DIVIDED across --parallel slots. A fixed -c therefore shrinks every slot's
window as slots are added, and a region crop that no longer fits comes back
TRUNCATED with no error, exit code 0 and the same wall time. Measured
2026-08-12 on a 10-page paper: -c 8192 held numeric recall 0.935 / span 0.900
at 1, 4 and 8 slots, then at 16 slots (512 tokens each) fell to 0.910 / 0.850
and lost 4,482 characters. Speed alone could not see it — only the pipeline's
own truth-recall check did.
"""

from __future__ import annotations

import importlib.util
import os
import sys

import pytest

_TOOL = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "tools",
    "pdf_extract",
    "extract_batch.py",
)


def _load_tool():
    """Import the tool by path — it lives outside the package tree."""
    spec = importlib.util.spec_from_file_location("_extract_batch_vl_under_test", _TOOL)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


pytest.importorskip("PIL")  # the tool imports PIL at module scope
_MOD = _load_tool()


class _FakePopen:
    """Captures the argv instead of starting a server."""

    last: list[str] = []

    def __init__(self, cmd, **kwargs):
        type(self).last = list(cmd)


@pytest.fixture
def spawn(monkeypatch):
    monkeypatch.setattr(_MOD.subprocess, "Popen", _FakePopen)

    def _run(**kw):
        _MOD._spawn_vl_server(**kw)
        return _FakePopen.last

    return _run


def _flag(cmd: list[str], name: str) -> str:
    return cmd[cmd.index(name) + 1]


# ── the slot/context invariant ────────────────────────────────────────


@pytest.mark.parametrize("slots", [1, 2, 4, 8, 16])
def test_every_slot_gets_the_full_per_slot_window(spawn, slots):
    """-c must scale with --parallel, or extra slots silently truncate crops."""
    cmd = spawn(
        backend="llamacpp",
        model="m.gguf",
        mmproj="p.gguf",
        port=1,
        parallel=slots,
        ctx=2048,
    )
    assert int(_flag(cmd, "--parallel")) == slots
    assert int(_flag(cmd, "-c")) == 2048 * slots, (
        "-c is the TOTAL cache split across slots; a fixed value shrinks each "
        "slot's window and truncates region crops with no error at all"
    )


def test_zero_slots_does_not_zero_the_context(spawn):
    """A 0/negative slot count must not collapse -c to nothing."""
    cmd = spawn(
        backend="llamacpp",
        model="m.gguf",
        mmproj="p.gguf",
        port=1,
        parallel=0,
        ctx=2048,
    )
    assert int(_flag(cmd, "-c")) >= 2048


# ── backend selection ─────────────────────────────────────────────────


def test_mlx_spawns_the_mlx_server_and_nothing_llama(spawn):
    cmd = spawn(backend="mlx", model="/models/paddle-mlx", port=7)
    assert "mlx_vlm.server" in cmd
    assert _flag(cmd, "--port") == "7"
    assert "--mmproj" not in cmd and "-ngl" not in cmd


def test_llamacpp_passes_model_projector_and_full_offload(spawn):
    cmd = spawn(backend="llamacpp", model="m.gguf", mmproj="p.gguf", port=9)
    assert _flag(cmd, "-m") == "m.gguf"
    assert _flag(cmd, "--mmproj") == "p.gguf"
    assert _flag(cmd, "-ngl") == "99"
    assert _flag(cmd, "--port") == "9"


def test_llamacpp_without_a_projector_is_refused(spawn):
    """A VL model with no mmproj loads as text-only and OCRs nothing —
    fail at spawn rather than produce empty markdown for a whole batch."""
    with pytest.raises(ValueError, match="mmproj"):
        spawn(backend="llamacpp", model="m.gguf", port=1)


def test_unknown_backend_is_refused(spawn):
    with pytest.raises(ValueError, match="unknown VL backend"):
        spawn(backend="vllm", model="m", port=1)


# ── the default is the fleet server, deliberately ─────────────────────


def test_llmvp_is_the_default_backend():
    """The fleet server, not a spawned subprocess (2026-08-13).

    NOT for speed — the A/B was a wash: verification rates identical to
    sixteen decimal places on both papers, paper time +4.6% resident, wall
    +1.0% once the ~6.5s spawn it no longer pays is netted out. It is for
    MANAGEMENT: model choice lives in LLMVP's config instead of a constant
    here, the OCR stage is visible to fleet telemetry, and it travels to
    CUDA with the fleet. Flipping it back is a decision, not a typo.
    """
    assert _MOD._DEFAULT_VL_BACKEND == "llmvp"
    assert _MOD._VL_BACKENDS[0] == "llmvp"


def test_the_spawned_backends_survive_the_default_flip():
    """llamacpp is the fallback when no fleet server runs, and mlx is still
    the station opt-in — neither may be dropped by the flip."""
    assert set(_MOD._VL_BACKENDS) == {"llmvp", "llamacpp", "mlx"}


def test_llmvp_needs_no_local_weights_and_spawns_nothing():
    """`model` is a registry NAME there, not a path — and asking the tool to
    spawn a fleet server is a caller error, not a silent second copy of
    paddle beside the hot one."""
    kwargs = _MOD._vl_pipe_kwargs("llmvp", "", 8008)
    assert kwargs["vl_rec_api_model_name"] == _MOD._LLMVP_MODEL
    # paddlex's own OpenAI client never reaches the fleet: it is built against
    # an address nothing listens on and then REPLACED by the GraphQL
    # recognizer (_build_pipe). The name is still supplied because paddlex
    # only contacts the server at construction when the name is missing.
    assert kwargs["vl_rec_server_url"] == _MOD._PLACEHOLDER_VL_URL
    assert "/v1/" not in kwargs["vl_rec_server_url"]
    assert "8008" not in kwargs["vl_rec_server_url"], "the fleet is never the target"
    with pytest.raises(ValueError, match="nothing to spawn"):
        _MOD._spawn_vl_server("llmvp", "m", "p", 1)


def test_the_llamacpp_default_resolves_to_a_gguf_AND_a_projector():
    """A GGUF without its projector loads as a text model and OCRs nothing,
    so the default must never supply half a pair."""
    model, mmproj = _MOD._default_vl_model("llamacpp")
    assert model.endswith(".gguf")
    assert mmproj.endswith(".gguf")
    assert "mmproj" in os.path.basename(mmproj)


def test_the_mlx_default_is_a_model_dir_and_carries_no_projector():
    model, mmproj = _MOD._default_vl_model("mlx")
    assert not model.endswith(".gguf")
    assert mmproj == "", "mlx_vlm has no separate projector to pass"


def test_default_weights_live_under_the_tools_models_dir():
    """models/ is gitignored and operator-placed — the same convention the
    MLX model has always used, so a station swaps weights without a diff."""
    for path in _MOD._default_vl_model("llamacpp") + _MOD._default_vl_model("mlx"):
        if path:
            assert os.path.basename(os.path.dirname(path)) == "models"


# ── paddleocr wiring ──────────────────────────────────────────────────


def test_pipe_kwargs_map_to_paddleocrs_own_backend_names():
    mlx = _MOD._vl_pipe_kwargs("mlx", "/models/paddle-mlx", 123)
    llama = _MOD._vl_pipe_kwargs("llamacpp", "m.gguf", 123)
    assert mlx["vl_rec_backend"] == "mlx-vlm-server"
    assert llama["vl_rec_backend"] == "llama-cpp-server"
    assert mlx["vl_rec_server_url"] == "http://127.0.0.1:123/"


def test_api_model_name_is_sent_only_where_it_selects_the_model():
    """mlx_vlm.server loads per request, so the name IS the model. llama-server
    fixed its model at spawn; naming it there can only disagree with reality,
    so the client discovers it from /v1/models instead."""
    assert "vl_rec_api_model_name" in _MOD._vl_pipe_kwargs("mlx", "/m", 1)
    assert "vl_rec_api_model_name" not in _MOD._vl_pipe_kwargs("llamacpp", "m.gguf", 1)


def test_concurrency_is_only_sent_when_asked_for():
    assert "vl_rec_max_concurrency" not in _MOD._vl_pipe_kwargs("mlx", "/m", 1)
    assert (
        _MOD._vl_pipe_kwargs("mlx", "/m", 1, concurrency=8)["vl_rec_max_concurrency"]
        == 8
    )
