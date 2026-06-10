"""Tests for the fix_target_menu projection (Batch B, Site #9).

Verifies the Pattern C materializer that replaces the legacy
`action_build_fix_target_menu` action. Produces a display-ready list
of `{id, description}` entries from `mission.architecture.modules` and
`.data_shapes`, with the same pre-composition rules the action used
(6-symbol defines cap, `(+N more)` suffix, data files folded in, dedup).
"""

from __future__ import annotations

from agent.persistence.models import (
    ArchitectureState,
    DataShapeContract,
    MissionConfig,
    MissionState,
    ModuleSpec,
)
from agent.projections import materialize


def _mission(arch: ArchitectureState | None = None) -> MissionState:
    """Build a minimal MissionState with just enough to satisfy the
    projection's reads."""
    state_kwargs: dict = {
        "mission_id": "m1",
        "objective": "test",
        "config": MissionConfig(working_directory="/tmp/test"),
    }
    if arch is not None:
        state_kwargs["architecture"] = arch
    return MissionState(**state_kwargs)


def test_empty_architecture_returns_empty_list() -> None:
    """No architecture → empty list. Turn renderer will route to
    no_answer via its own empty-options check."""
    result = materialize("project_fix_target_menu", _mission(), {})
    assert result == []


def test_modules_with_defines_get_symbols_in_description() -> None:
    arch = ArchitectureState(
        modules=[
            ModuleSpec(
                file="engine.py",
                responsibility="Game loop",
                defines=["GameEngine", "Room"],
            )
        ]
    )
    result = materialize("project_fix_target_menu", _mission(arch), {})
    assert len(result) == 1
    entry = result[0]
    assert entry["id"] == "engine.py"
    assert entry["description"] == "engine.py — Game loop [defines: GameEngine, Room]"


def test_defines_list_truncated_at_six_with_suffix() -> None:
    """More than 6 defined symbols → keep first 6, append `(+N more)`."""
    arch = ArchitectureState(
        modules=[
            ModuleSpec(
                file="big_module.py",
                responsibility="Everything",
                defines=[f"Symbol{i}" for i in range(10)],
            )
        ]
    )
    result = materialize("project_fix_target_menu", _mission(arch), {})
    desc = result[0]["description"]
    # First 6 names present
    for i in range(6):
        assert f"Symbol{i}" in desc
    # 7th onwards are NOT listed individually
    assert "Symbol7" not in desc
    assert "Symbol8" not in desc
    assert "Symbol9" not in desc
    # Suffix reports the remainder
    assert "(+4 more)" in desc


def test_module_without_defines_has_no_defines_block() -> None:
    arch = ArchitectureState(
        modules=[ModuleSpec(file="empty.py", responsibility="Nothing", defines=[])]
    )
    result = materialize("project_fix_target_menu", _mission(arch), {})
    desc = result[0]["description"]
    assert desc == "empty.py — Nothing"
    assert "[defines:" not in desc


def test_data_files_folded_in_after_modules() -> None:
    arch = ArchitectureState(
        modules=[ModuleSpec(file="engine.py", responsibility="Engine", defines=[])],
        data_shapes=[
            DataShapeContract(
                file="world.yaml",
                consumed_by="engine.py",
                structure="rooms: list",
            )
        ],
    )
    result = materialize("project_fix_target_menu", _mission(arch), {})
    assert len(result) == 2
    # Modules come first
    assert result[0]["id"] == "engine.py"
    assert result[1]["id"] == "world.yaml"
    assert "data file consumed by engine.py" in result[1]["description"]


def test_data_file_sharing_path_with_module_skipped() -> None:
    """A data_shape whose `file` matches a module file should NOT appear
    twice — first-wins dedup keeps the module entry."""
    arch = ArchitectureState(
        modules=[
            ModuleSpec(
                file="shared.py",
                responsibility="Code that also has data shape",
                defines=["Thing"],
            )
        ],
        data_shapes=[
            DataShapeContract(
                file="shared.py",  # same path as a module
                consumed_by="other.py",
                structure="...",
            )
        ],
    )
    result = materialize("project_fix_target_menu", _mission(arch), {})
    assert len(result) == 1
    # The module version wins
    assert "defines" in result[0]["description"]


def test_data_shape_without_file_path_skipped() -> None:
    """Defensive: a malformed data_shape with empty file is skipped."""
    arch = ArchitectureState(
        modules=[ModuleSpec(file="engine.py", responsibility="x", defines=[])],
        data_shapes=[DataShapeContract(file="", consumed_by="x", structure="")],
    )
    result = materialize("project_fix_target_menu", _mission(arch), {})
    assert len(result) == 1
    assert result[0]["id"] == "engine.py"


def test_module_order_preserved() -> None:
    """Architecture module list order is preserved in the menu."""
    arch = ArchitectureState(
        modules=[
            ModuleSpec(file="a.py", responsibility="A", defines=[]),
            ModuleSpec(file="b.py", responsibility="B", defines=[]),
            ModuleSpec(file="c.py", responsibility="C", defines=[]),
        ]
    )
    result = materialize("project_fix_target_menu", _mission(arch), {})
    assert [r["id"] for r in result] == ["a.py", "b.py", "c.py"]


def test_projection_returns_list_not_dict() -> None:
    """Sanity check on the shape contract."""
    arch = ArchitectureState(
        modules=[ModuleSpec(file="x.py", responsibility="x", defines=[])]
    )
    result = materialize("project_fix_target_menu", _mission(arch), {})
    assert isinstance(result, list)
    assert all(isinstance(e, dict) and "id" in e and "description" in e for e in result)


def test_materializer_registered() -> None:
    """The projection name must be registered so CUE's materializer
    reference resolves at runtime."""
    from agent.projections import registered_materializers

    assert "project_fix_target_menu" in registered_materializers()
