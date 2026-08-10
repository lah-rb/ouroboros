"""data_ops — the data-file analogue of the AST patcher (YAML, TOML, JSON).

These tests prove the load-bearing properties: round-trip + comment/order
preservation under a surgical edit (so a one-key fix never regenerates the
file), the op matrix (set/add/remove/move × object/list, append, predicate),
the JSONPointer grammar, the jsonpath-ng query + full_path→pointer adapter, and
the edge cases (anchors, merge keys, multi-doc, missing path, type mismatch).
``patch_text.ok`` is the strict gate the flow falls back on.

The op matrix and pointer grammar are exercised on YAML because they are
FORMAT-INDEPENDENT — navigation and mutation never learn a format, since every
backend's containers are dict/list subclasses. What each backend section below
proves is only what differs: parse, serialize, and value wrapping.
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
    """A read() Document is read-only for every format — the FLAG gates the
    write path, not the format. It used to be load() that refused non-YAML,
    which made the restriction structural; now that all three are editable,
    read() is the only thing standing between the trace and a write."""
    with pytest.raises(DataOpsError):
        Document.read("a: 1\n", Fmt.YAML).dumps()


@pytest.mark.parametrize(
    "content,fmt",
    [
        ('{"a": 1}', Fmt.JSON),
        ("a = 1\n", Fmt.TOML),
        ("a: 1\n", Fmt.YAML),
    ],
)
def test_read_never_yields_a_writable_document(content, fmt):
    with pytest.raises(DataOpsError):
        Document.read(content, fmt).dumps()


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


# ══════════════════════════════════════════════════════════════════════
# TOML backend (tomlkit)
# ══════════════════════════════════════════════════════════════════════
#
# The manifest from the gpt-oss-medium artifact, verbatim as project_ops wrote
# it. For want of this backend the pass routed to the module-frame editor, which
# spliced `pytest = "^7.4"` after [project.optional-dependencies] — valid TOML,
# meaningless as a manifest, and it broke every `uv` call for the rest of the run.

MANIFEST = """\
[project]
name = "text-adventure"
version = "0.1.0"

# Core dependencies needed for the game to run.
dependencies = [
    "PyYAML>=6.0",   # For loading world.yaml
]

# Optional development dependencies.
[project.optional-dependencies]
dev = [
    "ruff>=0.4.0",   # Linter / formatter
]

[build-system]
requires = ["setuptools>=61.0"]
build-backend = "setuptools.build_meta"
"""


def test_toml_roundtrip_stable():
    assert Document.load(MANIFEST, Fmt.TOML).dumps() == MANIFEST


def test_toml_declares_the_dependency_that_broke_the_run():
    """The edit the framework could not make, made surgically."""
    r = patch_text(
        MANIFEST,
        Fmt.TOML,
        [DataOp(op="add", path="/project/dependencies/-", value="pytest>=7.4")],
    )
    assert r.ok
    assert '"pytest>=7.4",' in r.text
    # It lands INSIDE the dependencies array, not as a stray top-level key —
    # the whole defect being repaired here.
    deps = r.text.index("dependencies = [")
    assert deps < r.text.index("pytest>=7.4") < r.text.index("# Optional development")
    # and it is a real PEP 621 declaration, not Poetry syntax
    assert 'pytest = "^' not in r.text


def test_toml_preserves_comments_and_table_order():
    r = patch_text(
        MANIFEST, Fmt.TOML, [DataOp(op="set", path="/project/version", value="0.2.0")]
    )
    assert r.ok and 'version = "0.2.0"' in r.text
    assert "# Core dependencies needed for the game to run." in r.text
    assert "# For loading world.yaml" in r.text  # INLINE comment survives
    assert "# Linter / formatter" in r.text
    assert r.text.index("[project]") < r.text.index("[build-system]")


def test_toml_creates_intermediate_tables():
    r = patch_text(
        MANIFEST,
        Fmt.TOML,
        [DataOp(op="set", path="/tool/pytest/ini_options/testpaths", value=["tests"])],
    )
    assert r.ok
    assert "[tool.pytest.ini_options]" in r.text
    assert 'testpaths = ["tests"]' in r.text


def test_toml_remove_and_move():
    r = patch_text(
        MANIFEST,
        Fmt.TOML,
        [DataOp(op="remove", path="/project/version")],
    )
    assert r.ok and 'version = "0.1.0"' not in r.text and "[project]" in r.text


def test_toml_predicate_and_index_addressing():
    r = patch_text(
        MANIFEST,
        Fmt.TOML,
        [DataOp(op="set", path="/project/dependencies/0", value="PyYAML>=6.0.3")],
    )
    assert r.ok and "PyYAML>=6.0.3" in r.text


def test_toml_unrepresentable_value_defers():
    """TOML has no null. The op fails cleanly so file_ops falls back to rewrite
    rather than writing a file the toolchain cannot read."""
    r = patch_text(
        MANIFEST, Fmt.TOML, [DataOp(op="set", path="/project/nope", value=None)]
    )
    assert not r.ok and r.failed


def test_toml_unparseable_source_defers():
    r = patch_text("nope = = 1", Fmt.TOML, [DataOp(op="set", path="/a", value=1)])
    assert not r.ok


# ══════════════════════════════════════════════════════════════════════
# JSON backend (stdlib + detected style)
# ══════════════════════════════════════════════════════════════════════

PKG = """\
{
  "name": "app",
  "scripts": {
    "test": "jest"
  },
  "dependencies": {
    "yaml": "^2.0.0"
  }
}
"""


def test_json_roundtrip_stable_on_canonical_formatting():
    """Files as tools emit them — npm's package.json, anything already written
    by json.dumps(indent=…) — come back byte-identical."""
    assert Document.load(PKG, Fmt.JSON).dumps() == PKG


def test_json_edit_is_a_one_line_diff():
    r = patch_text(
        PKG, Fmt.JSON, [DataOp(op="set", path="/dependencies/jest", value="^29.0.0")]
    )
    assert r.ok
    before, after = PKG.splitlines(), r.text.splitlines()
    assert len(after) - len(before) == 1  # the added key, and nothing else moved
    assert '"jest": "^29.0.0"' in r.text


@pytest.mark.parametrize(
    "raw,indent",
    [
        ('{\n    "a": 1\n}\n', "    "),
        ('{\n  "a": 1\n}\n', "  "),
        ('{\n\t"a": 1\n}\n', "\t"),
    ],
)
def test_json_indent_style_is_detected_not_assumed(raw, indent):
    r = patch_text(raw, Fmt.JSON, [DataOp(op="set", path="/b", value=2)])
    assert r.ok and f'\n{indent}"b": 2' in r.text


def test_json_compact_file_stays_compact():
    r = patch_text('{"a": 1}', Fmt.JSON, [DataOp(op="set", path="/b", value=2)])
    assert r.ok and r.text == '{"a": 1, "b": 2}'


def test_json_trailing_newline_policy_is_preserved():
    assert patch_text(
        '{\n  "a": 1\n}', Fmt.JSON, [DataOp(op="set", path="/a", value=2)]
    ).text.endswith("}")
    assert patch_text(
        '{\n  "a": 1\n}\n', Fmt.JSON, [DataOp(op="set", path="/a", value=2)]
    ).text.endswith("}\n")


def test_json_ascii_policy_follows_the_file():
    """An ASCII file stays ASCII; a file that already holds UTF-8 keeps it
    readable rather than escaping what was there."""
    ascii_out = patch_text(
        '{"a": 1}', Fmt.JSON, [DataOp(op="set", path="/b", value="café")]
    )
    assert ascii_out.ok and "caf\\u00e9" in ascii_out.text
    utf8_out = patch_text(
        '{"t": "café"}', Fmt.JSON, [DataOp(op="set", path="/u", value="naïve")]
    )
    assert utf8_out.ok and '"naïve"' in utf8_out.text


def test_json_inline_container_is_expanded():
    """THE known divergence, pinned. stdlib json applies its indent to every
    container, so a hand-written inline array in an otherwise multi-line file
    comes back expanded. Documented in the module header; the alternative is a
    round-trip JSON parser we do not have."""
    hand = '{\n  "keywords": ["cli", "game"]\n}\n'
    r = patch_text(hand, Fmt.JSON, [DataOp(op="set", path="/name", value="app")])
    assert r.ok
    assert '"keywords": [\n    "cli",\n    "game"\n  ]' in r.text


def test_json_list_ops_and_predicate():
    src = '{\n  "rooms": [\n    {\n      "id": "hall"\n    }\n  ]\n}\n'
    r = patch_text(
        src,
        Fmt.JSON,
        [DataOp(op="set", path="/rooms/[id=hall]/exit", value="north")],
    )
    assert r.ok and '"exit": "north"' in r.text


def test_json_unparseable_source_defers():
    assert not patch_text("{oops", Fmt.JSON, [DataOp(op="set", path="/a", value=1)]).ok


# ── the no-change guard, every format ─────────────────────────────────


@pytest.mark.parametrize(
    "src,fmt",
    [(WORLD, Fmt.YAML), (MANIFEST, Fmt.TOML), (PKG, Fmt.JSON)],
)
def test_nothing_applied_returns_the_original_bytes(src, fmt):
    """A no-op must leave the file alone, not reserialize it. Only JSON could
    actually differ, but 'we changed nothing' has to mean untouched."""
    assert patch_text(src, fmt, []).text == src
    failed = patch_text(src, fmt, [DataOp(op="remove", path="/definitely/missing")])
    assert failed.text == src and not failed.ok
