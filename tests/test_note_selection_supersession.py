"""Note selection for fix contexts — supersession + target scoping.

Earned by the 2026-08-01 laguna Q6_K degeneration study: a stale
failure_analysis note ("line 29 calls prompt() with no arguments" — accurate
at diagnosis time, fixed since) fed 4 long-cycle orbits because the model was
handed the aged claim beside the corrected file with nothing marking the
diagnosis as superseded. Operator intent: last 5 notes, only for the current
thing being fixed, newer diagnoses supersede older ones.
"""

from agent.persistence.models import MissionConfig, MissionState, NoteRecord
from agent.projections import _filter_notes_for_file


def _mission(notes):
    return MissionState(
        objective="t",
        status="active",
        config=MissionConfig(working_directory="/tmp/x"),
        goals=[],
        notes=notes,
    )


def _note(content, category="failure_analysis", tags=(), ts="2026-08-01T00:00:00"):
    return NoteRecord(content=content, category=category, tags=list(tags), timestamp=ts)


class TestSupersession:
    def test_older_diagnoses_marked_superseded(self):
        m = _mission(
            [
                _note(
                    "line 29 calls prompt() with no args",
                    tags=["engine.py"],
                    ts="2026-08-01T01:00:00",
                ),
                _note(
                    "input() consumes the first command",
                    tags=["engine.py"],
                    ts="2026-08-01T02:00:00",
                ),
            ]
        )
        out = _filter_notes_for_file(m, "engine.py")
        assert len(out) == 2
        # Newest first, unmarked; older carries the supersession label.
        assert out[0].startswith("[failure_analysis]")
        assert "input() consumes" in out[0]
        assert "SUPERSEDED" in out[1]
        assert "line 29" in out[1]

    def test_single_diagnosis_unmarked(self):
        m = _mission([_note("root cause X", tags=["engine.py"])])
        out = _filter_notes_for_file(m, "engine.py")
        assert out == ["[failure_analysis] root cause X"]


class TestScoping:
    def test_other_files_notes_excluded(self):
        m = _mission(
            [
                _note("ui.py note", tags=["ui.py"]),
                _note("engine note", tags=["engine.py"]),
                _note(
                    "stray observation about parser.py",
                    category="codebase_observation",
                    tags=["parser.py"],
                ),
            ]
        )
        out = _filter_notes_for_file(m, "engine.py")
        assert out == ["[failure_analysis] engine note"]

    def test_architecture_blueprint_stays_global(self):
        m = _mission([_note("the blueprint", category="architecture_blueprint")])
        out = _filter_notes_for_file(m, "engine.py")
        assert out == ["[architecture_blueprint] the blueprint"]

    def test_cap_five(self):
        notes = [
            _note(f"n{i}", tags=["engine.py"], ts=f"2026-08-01T0{i}:00:00")
            for i in range(7)
        ]
        out = _filter_notes_for_file(_mission(notes), "engine.py")
        assert len(out) == 5
        assert "n6" in out[0]  # newest kept


class TestLintWarningChannel:
    """2026-08-02 wire-up: lint_warning was WRITE-ONLY (two writers, zero
    readers). Target-tagged notes now surface through the same filter."""

    def test_on_target_lint_warning_surfaces(self):
        m = _mission(
            [
                _note(
                    "Validation issues (non-blocking):\n  - lint: engine.py: F841",
                    category="lint_warning",
                    tags=["lint", "non_blocking", "engine.py"],
                )
            ]
        )
        out = _filter_notes_for_file(m, "engine.py")
        assert len(out) == 1 and out[0].startswith("[lint_warning]")

    def test_untagged_lint_warning_stays_out_of_other_targets(self):
        m = _mission([_note("F841 in parser", category="lint_warning", tags=["lint"])])
        assert _filter_notes_for_file(m, "engine.py") == []


class TestRetiredCategories:
    """Pruned enum values must LOAD (archives carry them), coerced to general."""

    def test_retired_category_coerces_to_general(self):
        n = NoteRecord(content="x", category="task_learning")
        assert n.category == "general"
        n2 = NoteRecord(content="y", category="dependency_identified")
        assert n2.category == "general"

    def test_unknown_category_coerces_to_general(self):
        n = NoteRecord.model_validate({"content": "z", "category": "made_up"})
        assert n.category == "general"

    def test_live_categories_untouched(self):
        assert (
            NoteRecord(content="a", category="lint_warning").category == "lint_warning"
        )
        assert (
            NoteRecord(content="b", category="failure_analysis").category
            == "failure_analysis"
        )
