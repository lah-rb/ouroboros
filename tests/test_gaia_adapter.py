"""adapters.gaia: official scorer semantics, mission construction, answer channel."""

from __future__ import annotations


from adapters.gaia.loader import GaiaQuestion
from adapters.gaia.runner import _ANSWER_CONTRACT, build_mission, extract_answer
from adapters.gaia.scorer import question_scorer

# ── scorer: the official quasi-exact-match semantics ────────────────────────


def test_scorer_numbers_normalize_currency_percent_commas():
    assert question_scorer("$1,234.5", "1234.5")
    assert question_scorer("34%", "34")
    assert question_scorer("1,000,000", "1000000")
    assert not question_scorer("1234.6", "1234.5")
    assert not question_scorer("not a number", "42")


def test_scorer_strings_ignore_case_space_punct():
    assert question_scorer("Right Whale", "right whale")
    assert question_scorer("  right   whale ", "right whale")
    assert question_scorer("right-whale!", "right whale")
    assert not question_scorer("blue whale", "right whale")


def test_scorer_lists_elementwise_with_mixed_types():
    assert question_scorer("a, b, 3", "a,b,3")
    assert question_scorer("A; B; 3.0", "a,b,3")  # ; and , both split
    assert not question_scorer("a, b", "a,b,3")  # length mismatch
    assert not question_scorer("a, x, 3", "a,b,3")


def test_scorer_empty_answer_never_matches():
    assert not question_scorer("", "42")
    assert not question_scorer("", "right whale")
    # ...unless gold is itself empty-normalized — not a real GAIA case, but
    # the guard should be deliberate: empty vs empty string-compares equal.
    assert question_scorer("", "")


# ── mission construction ─────────────────────────────────────────────────────


def _q(**kw) -> GaiaQuestion:
    base = dict(
        task_id="t-1",
        question="How many moons does Mars have?",
        level=1,
        final_answer="2",
    )
    base.update(kw)
    return GaiaQuestion(**base)


def test_build_mission_forces_ops_with_web_on(tmp_path):
    mission, entry = build_mission(_q(), str(tmp_path))
    assert mission.config.flow_set == "ops"
    assert mission.config.web_research is True  # GAIA is the web benchmark
    assert mission.config.working_directory == str(tmp_path)
    assert entry  # ops entry flow resolved from the registry
    assert "answer.txt" in mission.objective  # the deliverable contract rides along
    assert "How many moons" in mission.objective


def test_build_mission_notes_attachment(tmp_path):
    mission, _ = build_mission(_q(file_name="data.xlsx"), str(tmp_path))
    assert "./data.xlsx" in mission.objective
    m2, _ = build_mission(_q(), str(tmp_path))
    assert "attached file" not in m2.objective


def test_image_attachment_sets_vision_and_tool_note(tmp_path):
    # Deterministic config-time modality routing: image → vision flag + the
    # vl_inspect invocation line (absolute tool-venv path) in the objective.
    for name in ("chart.png", "photo.JPG"):
        mission, _ = build_mission(_q(file_name=name), str(tmp_path))
        assert mission.config.vision is True
        assert "vl_inspect.py" in mission.objective
        assert f"--image ./{name}" in mission.objective
        assert "tools/fig_review/.venv/bin/python" in mission.objective


def test_pdf_attachment_gets_extract_note_not_vision(tmp_path):
    mission, _ = build_mission(_q(file_name="paper.pdf"), str(tmp_path))
    assert mission.config.vision is False  # PDF text path needs no VL
    assert "pdf_extract_one.py" in mission.objective
    assert "--pdf ./paper.pdf" in mission.objective
    assert "tools/pdf_extract/.venv/bin/python" in mission.objective


def test_data_and_fileless_questions_get_no_tool_note(tmp_path):
    for q in (_q(file_name="data.xlsx"), _q()):
        mission, _ = build_mission(q, str(tmp_path))
        assert mission.config.vision is False
        assert "vl_inspect" not in mission.objective
        assert "pdf_extract_one" not in mission.objective


def test_answer_contract_bans_prefix_and_units():
    # The contract text itself must carry the graded formatting rules.
    for needle in ("answer.txt", "no commas", "FINAL ANSWER"):
        assert needle in _ANSWER_CONTRACT


# ── answer channel ───────────────────────────────────────────────────────────


def test_extract_answer_reads_and_strips(tmp_path):
    (tmp_path / "answer.txt").write_text("  right whale \n")
    assert extract_answer(str(tmp_path)) == "right whale"


def test_extract_answer_strips_defensive_prefix_and_quotes(tmp_path):
    (tmp_path / "answer.txt").write_text('FINAL ANSWER: "42"\n')
    assert extract_answer(str(tmp_path)) == "42"


def test_extract_answer_missing_file_is_empty(tmp_path):
    assert extract_answer(str(tmp_path)) == ""
