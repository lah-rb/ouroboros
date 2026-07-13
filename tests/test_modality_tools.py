"""Modality tools: vl_inspect payload contract, pdf_extract_one pymupdf engine,
MissionConfig.vision additivity. The Paddle engine + live VL answers are
covered by dev/ab_pdf_extract.sh and manual smokes (too heavy for CI)."""

from __future__ import annotations

import base64
import os
import subprocess
import sys

import pytest

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_FIG_TOOL = os.path.join(_REPO, "tools", "fig_review")
_PDF_TOOL = os.path.join(_REPO, "tools", "pdf_extract")
_PDF_VENV_PY = os.path.join(_PDF_TOOL, ".venv", "bin", "python")


# ── vl_inspect: payload builder (stdlib-only module import) ─────────────────

def _import_vl_inspect():
    sys.path.insert(0, _FIG_TOOL)
    try:
        import vl_inspect
        return vl_inspect
    finally:
        sys.path.remove(_FIG_TOOL)


def test_vl_payload_shape_and_question_placement():
    vi = _import_vl_inspect()
    b64 = base64.b64encode(b"fakeimg").decode()
    p = vi.build_payload("m", b64, "image/jpeg", "What does the sign say?", max_tokens=123)
    assert p["model"] == "m" and p["max_tokens"] == 123
    parts = p["messages"][0]["content"]
    text = next(x for x in parts if x["type"] == "text")
    img = next(x for x in parts if x["type"] == "image_url")
    assert text["text"] == "What does the sign say?"
    assert img["image_url"]["url"] == f"data:image/jpeg;base64,{b64}"


def test_vl_mime_inference_covers_gaia_image_exts():
    vi = _import_vl_inspect()
    for ext, mime in ((".png", "image/png"), (".jpg", "image/jpeg"), (".jpeg", "image/jpeg")):
        assert vi._MIME[ext] == mime


# ── pdf_extract_one: pymupdf engine round-trip (gated on the tool venv) ─────

@pytest.mark.skipif(not os.path.exists(_PDF_VENV_PY),
                    reason="tools/pdf_extract/.venv not provisioned")
def test_pdf_extract_one_pymupdf_roundtrip(tmp_path):
    pdf = tmp_path / "fixture.pdf"
    # Build a 1-page fixture with the SAME venv's pymupdf, then extract it.
    mk = (
        "import fitz; d=fitz.open(); p=d.new_page(); "
        "p.insert_text((72,72),'GAIA fixture 12345'); "
        f"d.save({str(pdf)!r})"
    )
    subprocess.run([_PDF_VENV_PY, "-c", mk], check=True, cwd=_PDF_TOOL)
    out = subprocess.run(
        [_PDF_VENV_PY, os.path.join(_PDF_TOOL, "pdf_extract_one.py"),
         "--pdf", str(pdf), "--engine", "pymupdf"],
        capture_output=True, text=True, cwd=_PDF_TOOL,
    )
    assert out.returncode == 0, out.stderr
    assert "=== page 1 ===" in out.stdout
    assert "GAIA fixture 12345" in out.stdout


@pytest.mark.skipif(not os.path.exists(_PDF_VENV_PY),
                    reason="tools/pdf_extract/.venv not provisioned")
def test_pdf_extract_one_missing_pdf_fails_cleanly():
    out = subprocess.run(
        [_PDF_VENV_PY, os.path.join(_PDF_TOOL, "pdf_extract_one.py"),
         "--pdf", "/nonexistent.pdf", "--engine", "pymupdf"],
        capture_output=True, text=True, cwd=_PDF_TOOL,
    )
    assert out.returncode == 2
    assert "no such pdf" in out.stderr


# ── MissionConfig.vision: additive, defaults off ─────────────────────────────

def test_mission_config_vision_defaults_false_and_loads_old_configs():
    from agent.persistence.models import MissionConfig

    cfg = MissionConfig(working_directory="/tmp/x")
    assert cfg.vision is False
    # Old mission.json (no vision key) must load unchanged.
    old = MissionConfig.model_validate({"working_directory": "/tmp/x", "web_research": False})
    assert old.vision is False and old.web_research is False

# ── audio_transcribe: structure + rendering (stdlib-safe imports) ────────────

_AT_TOOL = os.path.join(_REPO, "tools", "audio_transcribe")


def _import_audio_transcribe():
    sys.path.insert(0, _AT_TOOL)
    try:
        import audio_transcribe
        return audio_transcribe
    finally:
        sys.path.remove(_AT_TOOL)


def test_audio_render_segments_plain_and_timestamped():
    at = _import_audio_transcribe()
    segs = [(0.0, " hello"), (65.5, "world "), (3661.0, "deep")]
    assert at.render_segments(segs, timestamps=False) == "hello world deep"
    ts = at.render_segments(segs, timestamps=True)
    assert "[0:00] hello" in ts and "[1:05] world" in ts and "[1:01:01] deep" in ts


def test_audio_models_pinned():
    at = _import_audio_transcribe()
    assert "parakeet-tdt-0.6b-v2" in at.PARAKEET_MODEL
    assert "whisper-large-v3-turbo" in at.WHISPER_MODEL


def test_mission_config_audio_and_sidecar_gates_default():
    from agent.persistence.models import MissionConfig

    cfg = MissionConfig(working_directory="/tmp/x")
    assert cfg.audio is False
    assert cfg.modality_sidecar_max_images == 6
    assert cfg.modality_sidecar_max_audio == 2
