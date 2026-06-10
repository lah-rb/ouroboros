"""ArchitectureState.notes must tolerate structured model output.

Some models (e.g. Mistral) emit the architecture `notes` as a dict or list
instead of a plain string. Strict validation used to crash design_and_plan at
cycle 0. The field validator coerces any shape into a readable string.
"""

from agent.persistence.models import ArchitectureState


def test_notes_dict_is_coerced():
    a = ArchitectureState(
        notes={"design_rationale": "Split commands", "io": "non-blocking"}
    )
    assert isinstance(a.notes, str)
    assert "design_rationale: Split commands" in a.notes
    assert "io: non-blocking" in a.notes


def test_notes_list_is_coerced():
    a = ArchitectureState(notes=["a", "b"])
    assert a.notes == "a; b"


def test_notes_string_passthrough():
    assert ArchitectureState(notes="plain note").notes == "plain note"


def test_notes_none_and_default():
    assert ArchitectureState(notes=None).notes == ""
    assert ArchitectureState().notes == ""
