"""Tests for the schema registry (agent/schema_registry.py).

Step C migration, Phase 2.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent.schema_registry import (
    SchemaNotFoundError,
    SchemaRegistry,
    get_default_registry,
    get_schema,
    set_default_registry,
)


@pytest.fixture(autouse=True)
def reset_default_registry():
    """Each test starts with the default registry cleared, re-resolves
    at end. Prevents cross-test state leakage through the singleton."""
    set_default_registry(None)
    yield
    set_default_registry(None)


# ──────────────────────────────────────────────────────────────────────
# SchemaRegistry class
# ──────────────────────────────────────────────────────────────────────


def test_empty_registry_returns_empty_ids() -> None:
    reg = SchemaRegistry()
    assert reg.ids() == []


def test_register_and_get() -> None:
    reg = SchemaRegistry()
    schema = {"type": "object", "properties": {"x": {"type": "string"}}}
    reg.register("my_schema", schema)
    assert reg.get("my_schema") is schema


def test_register_replaces_existing() -> None:
    reg = SchemaRegistry()
    reg.register("s", {"v": 1})
    reg.register("s", {"v": 2})
    assert reg.get("s") == {"v": 2}


def test_get_unknown_raises_schema_not_found() -> None:
    reg = SchemaRegistry()
    reg.register("known", {})
    with pytest.raises(SchemaNotFoundError, match="not in the registry"):
        reg.get("unknown")


def test_schema_not_found_message_lists_available() -> None:
    reg = SchemaRegistry({"alpha": {}, "beta": {}, "gamma": {}})
    try:
        reg.get("missing")
    except SchemaNotFoundError as e:
        assert "alpha" in str(e)
        assert "beta" in str(e)
        assert "gamma" in str(e)


def test_has_returns_true_for_registered() -> None:
    reg = SchemaRegistry()
    reg.register("x", {})
    assert reg.has("x") is True
    assert reg.has("y") is False


def test_ids_returns_sorted_list() -> None:
    reg = SchemaRegistry({"charlie": {}, "alpha": {}, "bravo": {}})
    assert reg.ids() == ["alpha", "bravo", "charlie"]


def test_register_rejects_non_dict() -> None:
    reg = SchemaRegistry()
    with pytest.raises(TypeError, match="must be a dict"):
        reg.register("bad", "not a dict")  # type: ignore[arg-type]


def test_constructor_copies_input_dict() -> None:
    """Mutating the source dict after construction must not affect
    the registry."""
    source = {"s1": {"v": 1}}
    reg = SchemaRegistry(source)
    source["s2"] = {"v": 2}  # should not leak in
    assert reg.ids() == ["s1"]


# ──────────────────────────────────────────────────────────────────────
# from_dir loader
# ──────────────────────────────────────────────────────────────────────


def test_from_dir_loads_json_files(tmp_path: Path) -> None:
    (tmp_path / "alpha.json").write_text('{"type": "string"}')
    (tmp_path / "beta.json").write_text('{"type": "number"}')
    reg = SchemaRegistry.from_dir(tmp_path)
    assert reg.ids() == ["alpha", "beta"]
    assert reg.get("alpha") == {"type": "string"}
    assert reg.get("beta") == {"type": "number"}


def test_from_dir_uses_stem_as_schema_id(tmp_path: Path) -> None:
    (tmp_path / "my_complex_name.json").write_text('{"x": 1}')
    reg = SchemaRegistry.from_dir(tmp_path)
    assert reg.has("my_complex_name")


def test_from_dir_skips_non_json_files(tmp_path: Path) -> None:
    (tmp_path / "alpha.json").write_text('{"k": "v"}')
    (tmp_path / "readme.txt").write_text("not a schema")
    (tmp_path / "config.yaml").write_text("also not")
    reg = SchemaRegistry.from_dir(tmp_path)
    assert reg.ids() == ["alpha"]


def test_from_dir_handles_empty_directory(tmp_path: Path) -> None:
    reg = SchemaRegistry.from_dir(tmp_path)
    assert reg.ids() == []


def test_from_dir_raises_when_directory_missing(tmp_path: Path) -> None:
    missing = tmp_path / "does_not_exist"
    with pytest.raises(FileNotFoundError, match="not found"):
        SchemaRegistry.from_dir(missing)


def test_from_dir_rejects_non_object_json(tmp_path: Path) -> None:
    (tmp_path / "bad.json").write_text("[1, 2, 3]")
    with pytest.raises(ValueError, match="JSON object"):
        SchemaRegistry.from_dir(tmp_path)


def test_from_dir_accepts_string_path(tmp_path: Path) -> None:
    (tmp_path / "x.json").write_text('{"a": 1}')
    reg = SchemaRegistry.from_dir(str(tmp_path))
    assert reg.has("x")


# ──────────────────────────────────────────────────────────────────────
# Default-registry singleton
# ──────────────────────────────────────────────────────────────────────


def test_default_registry_lazy_loads(tmp_path: Path, monkeypatch) -> None:
    """Default registry reads from the repo `schemas/` dir.
    We verify it loads at all — contents vary with repo state."""
    reg = get_default_registry()
    assert isinstance(reg, SchemaRegistry)


def test_default_registry_cached() -> None:
    r1 = get_default_registry()
    r2 = get_default_registry()
    assert r1 is r2


def test_set_default_registry_overrides() -> None:
    custom = SchemaRegistry({"test_schema": {"type": "string"}})
    set_default_registry(custom)
    assert get_default_registry() is custom
    assert get_schema("test_schema") == {"type": "string"}


def test_set_default_registry_to_none_resets() -> None:
    set_default_registry(SchemaRegistry({"a": {}}))
    set_default_registry(None)
    # Next get_default_registry call reloads from disk
    reg = get_default_registry()
    # We can't assert much about contents without knowing repo state,
    # but it must be a fresh object (not the one we cleared)
    assert "a" not in reg.ids()


def test_get_schema_convenience_uses_default() -> None:
    custom = SchemaRegistry({"x": {"v": 42}})
    set_default_registry(custom)
    assert get_schema("x") == {"v": 42}


def test_get_schema_unknown_raises() -> None:
    set_default_registry(SchemaRegistry())
    with pytest.raises(SchemaNotFoundError):
        get_schema("nonexistent")


# ──────────────────────────────────────────────────────────────────────
# The actual validation_env_config schema loads and is usable
# ──────────────────────────────────────────────────────────────────────


def test_validation_env_config_schema_loads_from_default_registry() -> None:
    """The schemas/validation_env_config.json file shipped in this
    commit must be loadable via the default registry."""
    set_default_registry(None)  # force re-resolution from disk
    reg = get_default_registry()
    assert reg.has("validation_env_config"), (
        f"validation_env_config missing from registry. " f"Available: {reg.ids()}"
    )


def test_validation_env_config_has_expected_structure() -> None:
    """Sanity-check the schema's shape matches Site #19's design."""
    set_default_registry(None)
    schema = get_schema("validation_env_config")
    assert schema["type"] == "object"
    assert "interactive_prompt" in schema["properties"]
    assert "patternProperties" in schema
    # Pattern excludes interactive_prompt (it's a string, not a language commands obj)
    pattern_keys = list(schema["patternProperties"].keys())
    assert len(pattern_keys) == 1
    pattern = pattern_keys[0]
    assert "interactive_prompt" in pattern, (
        f"Pattern must exclude interactive_prompt to avoid shape collision. "
        f"Got pattern: {pattern!r}"
    )


def test_validation_env_config_example_is_present() -> None:
    """The x-example field supports envelope rendering downstream."""
    set_default_registry(None)
    schema = get_schema("validation_env_config")
    assert (
        "x-example" in schema
    ), "Expected x-example on validation_env_config for envelope rendering"
    example = schema["x-example"]
    assert isinstance(example, dict)
    assert "py" in example
    assert "syntax" in example["py"]


def test_validation_env_config_example_round_trips_as_json() -> None:
    """The example must serialize cleanly — the renderer will show it
    to the model as a concrete instance of the shape."""
    set_default_registry(None)
    schema = get_schema("validation_env_config")
    example = schema["x-example"]
    encoded = json.dumps(example)
    assert json.loads(encoded) == example
