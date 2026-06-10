"""data_ops — the data-file analogue of the AST patcher (v1: YAML).

These tests prove the load-bearing properties: round-trip + comment/order
preservation under a surgical edit (so a one-key fix never regenerates the
file), the op matrix (set/add/remove/move × object/list, append, predicate),
the JSONPointer grammar, the jsonpath-ng query + full_path→pointer adapter, and
the edge cases (anchors, merge keys, multi-doc, missing path, type mismatch).
``patch_text.ok`` is the strict gate the flow falls back on.
"""

from __future__ import annotations

import pytest

from agent.data_ops import (
    DataOp,
    DataOpsError,
    Document,
    Fmt,
    PathError,
    detect_fmt,
    jsonpath_to_pointer,
    patch_text,
    resolve_pointer,
)

WORLD = """\
# Blackwood Hall
rooms:
  - id: entrance_hall   # the start room
    exits:
      north: library
    items: [lantern]
  - id: bedroom
    exits: {}
npcs:
  martha:
    name: Martha the Caretaker
"""


def _y(content: str) -> Document:
    return Document.load(content, Fmt.YAML)


# ── format detection ───────────────────────────────────────────────────


def test_detect_fmt():
    assert detect_fmt("world.yaml") is Fmt.YAML
    assert detect_fmt("a.yml") is Fmt.YAML
    assert detect_fmt("c.json") is Fmt.JSON
    assert detect_fmt("d.toml") is Fmt.TOML
    assert detect_fmt("noext", '{"a":1}') is Fmt.JSON


def test_non_yaml_edit_rejected_in_v1():
    with pytest.raises(DataOpsError):
        Document.load('{"a":1}', Fmt.JSON)


# ── read-only loader (trace path: YAML/JSON/TOML, never writable) ──────


def test_read_json_slices_subtree():
    doc = Document.read('{"rooms": [{"id": "a"}], "npcs": []}', Fmt.JSON)
    assert doc.slice("/rooms").value == [{"id": "a"}]
    assert doc.slice("/rooms/0/id").value == "a"


def test_read_toml_slices_subtree():
    doc = Document.read('[server]\nhost = "x"\nport = 80\n', Fmt.TOML)
    assert doc.slice("/server/host").value == "x"
    assert doc.slice("/server/port").value == 80


def test_read_is_readonly_dumps_rejected():
    with pytest.raises(DataOpsError):
        Document.read('{"a": 1}', Fmt.JSON).dumps()


def test_read_yaml_still_readonly():
    """A read() Document is read-only even for YAML — the flag, not the format,
    gates the write path, so a non-YAML root can never be obtained writable."""
    with pytest.raises(DataOpsError):
        Document.read("a: 1\n", Fmt.YAML).dumps()


def test_apply_rejected_on_readonly():
    doc = Document.read('{"a": 1}', Fmt.JSON)
    res = doc.apply([DataOp(op="set", path="/a", value=2)])
    assert res.changed is False
    assert res.applied == [] and len(res.failed) == 1


# ── round-trip + comment/order preservation (THE win) ──────────────────


def test_roundtrip_stable():
    assert _y(WORLD).dumps() == WORLD


def test_set_preserves_comments_and_order():
    r = patch_text(
        WORLD,
        Fmt.YAML,
        [
            DataOp(
                op="set", path="/rooms/[id=bedroom]/exits/south", value="entrance_hall"
            )
        ],
    )
    assert r.ok
    assert "# Blackwood Hall" in r.text  # header comment survives
    assert "# the start room" in r.text  # sibling comment survives
    assert "south: entrance_hall" in r.text  # the edit landed
    # key order unchanged: rooms still before npcs, entrance before bedroom
    assert r.text.index("rooms:") < r.text.index("npcs:")
    assert r.text.index("entrance_hall") < r.text.index("bedroom")


# ── op matrix ──────────────────────────────────────────────────────────


def test_set_replaces_existing_scalar():
    r = patch_text(
        WORLD, Fmt.YAML, [DataOp(op="set", path="/rooms/0/id", value="hall")]
    )
    assert r.ok and "id: hall" in r.text


def test_add_appends_to_list():
    r = patch_text(
        WORLD,
        Fmt.YAML,
        [DataOp(op="add", path="/rooms/[id=entrance_hall]/items/-", value="key")],
    )
    assert r.ok and "[lantern, key]" in r.text


def test_add_inserts_at_index():
    r = patch_text(
        WORLD,
        Fmt.YAML,
        [DataOp(op="add", path="/rooms/[id=entrance_hall]/items/0", value="map")],
    )
    assert r.ok and "[map, lantern]" in r.text


def test_set_creates_intermediate_containers():
    r = patch_text(
        WORLD,
        Fmt.YAML,
        [DataOp(op="set", path="/rooms/1/exits/down/cellar", value="dark")],
    )
    assert r.ok
    doc = _y(r.text)
    assert doc.slice("/rooms/1/exits/down/cellar").value == "dark"


def test_remove_key_and_list_item():
    r = patch_text(
        WORLD,
        Fmt.YAML,
        [
            DataOp(op="remove", path="/npcs/martha"),
            DataOp(op="remove", path="/rooms/[id=entrance_hall]/items/0"),
        ],
    )
    assert r.ok
    assert "npcs: {}" in r.text and "items: []" in r.text


def test_move_preserves_subtree():
    src = "a:\n  x:\n    k: 1   # keep me\nb: {}\n"
    r = patch_text(src, Fmt.YAML, [DataOp(op="move", path="/b/y", from_="/a/x")])
    assert r.ok
    assert "# keep me" in r.text  # comment on the moved subtree survives
    doc = _y(r.text)
    assert doc.slice("/b/y/k").value == 1
    assert doc.slice("/a/x").value is None  # source gone


# ── JSONPointer grammar ────────────────────────────────────────────────


def test_pointer_escaping():
    doc = _y('weird~key:\n  "a/b": 1\n')
    # ~0 = ~, ~1 = /
    assert doc.slice("/weird~0key/a~1b").value == 1


def test_predicate_addresses_by_identity():
    parent, key, exists = resolve_pointer(_y(WORLD).root, "/rooms/[id=bedroom]")
    assert exists and key == 1  # resolved to concrete index


def test_missing_path_slice_is_empty_not_error():
    s = _y(WORLD).slice("/rooms/[id=cellar]/exits")
    assert s.value is None and s.text == ""


def test_remove_missing_fails_cleanly():
    r = patch_text(WORLD, Fmt.YAML, [DataOp(op="remove", path="/rooms/9/id")])
    assert not r.ok and r.failed


def test_type_mismatch_is_typed_error():
    with pytest.raises(PathError):
        resolve_pointer(_y(WORLD).root, "/npcs/martha/name/nope")


# ── jsonpath-ng query + adapter ────────────────────────────────────────


def test_query_returns_addressable_pointers():
    doc = _y(WORLD)
    ms = doc.query("$.rooms[*].id")
    assert [(m.pointer, m.value) for m in ms] == [
        ("/rooms/0/id", "entrance_hall"),
        ("/rooms/1/id", "bedroom"),
    ]


def test_query_filter_then_patch_roundtrips():
    doc = _y(WORLD)
    hits = doc.query("$.rooms[?(@.id=='bedroom')]")
    assert len(hits) == 1
    ptr = hits[0].pointer  # addressable
    r = patch_text(
        WORLD, Fmt.YAML, [DataOp(op="set", path=ptr + "/name", value="Master Bedroom")]
    )
    assert r.ok and "name: Master Bedroom" in r.text


def test_query_recursive_descent():
    doc = _y(WORLD)
    names = {m.value for m in doc.query("$..name")}
    assert "Martha the Caretaker" in names


def test_jsonpath_to_pointer_unit():
    from jsonpath_ng.ext import parse as jparse

    (m,) = jparse("$.rooms[1].id").find(_y(WORLD).root)
    assert jsonpath_to_pointer(m.full_path) == "/rooms/1/id"


# ── YAML edge cases ────────────────────────────────────────────────────


def test_anchors_preserved_on_unrelated_edit():
    src = "base: &b\n  hp: 10\nhero:\n  <<: *b\n  name: Knight\n"
    r = patch_text(
        src, Fmt.YAML, [DataOp(op="set", path="/hero/name", value="Paladin")]
    )
    assert r.ok
    assert "&b" in r.text and "<<: *b" in r.text  # anchor + merge survive
    assert "name: Paladin" in r.text


def test_merge_key_read_sees_merged_value():
    src = "base: &b\n  hp: 10\nhero:\n  <<: *b\n  name: Knight\n"
    # ruamel exposes merged values on read
    assert _y(src).slice("/hero/hp").value == 10


def test_empty_doc_set_creates_root():
    r = patch_text("", Fmt.YAML, [DataOp(op="set", path="/title", value="Hello")])
    assert r.ok and "title: Hello" in r.text


def test_patch_text_ok_false_on_bad_op():
    # set into a scalar → fails; ok must be False and text unchanged-ish
    r = patch_text(
        WORLD, Fmt.YAML, [DataOp(op="set", path="/npcs/martha/name/x", value=1)]
    )
    assert not r.ok and r.failed
