"""Data-aware symbol trace — make diagnose treat data as an equal suspect.

Live regression: an items-goal failure was a DATA bug (a room's empty
``items: []`` in ``world_data.yaml``), but the trace surfaces only CODE symbols,
so the model blamed ``loader.py``, failed, then re-diagnosed the YAML — wasting a
fix round. This feature surfaces the connected data file's parsed, key-scoped
content as trace evidence: detect the data a traced symbol reads (directly or
via ``self.X``), scope to the keys the code accesses, render so a nested
``courtyard.items: []`` is visible.
"""

from __future__ import annotations

import pytest

from agent.actions.diagnosis_session_actions import action_start_diagnosis_session
from agent.data_trace import _scope_to_keys, build_data_trace_evidence
from agent.effects.mock import MockEffects
from agent.models import FlowMeta, StepInput
from agent.schema_extract import (
    _find_data_loads_regex,
    extract_module_constants,
    find_data_loads,
    parses_data,
)

# ── Fixtures ───────────────────────────────────────────────────────────

LOADER_BODY = (
    "def load_world():\n"
    '    data = yaml.safe_load(open("world_data.yaml"))\n'
    '    rooms = data["rooms"]\n'
    "    for room in rooms.values():\n"
    '        items = room["items"]\n'
    "        process(items)\n"
    "    return rooms\n"
)
LOADER_FILE = "import yaml\n\n\n" + LOADER_BODY

WORLD_YAML = (
    "rooms:\n"
    "  courtyard:\n"
    "    name: Courtyard\n"
    "    description: A mossy courtyard\n"
    "    items: []\n"
    "  hall:\n"
    "    name: Hall\n"
    "    items: [torch, key]\n"
    "npcs:\n"
    "  elder:\n"
    "    name: Elder Sage\n"
)


def _sym(name, body, parent="", kind="function"):
    return {"name": name, "body": body, "parent": parent, "kind": kind}


def _fc(content=WORLD_YAML, path="world_data.yaml"):
    return {"data_file_contents": {path: content}}


# ── Detector units (pure, fast) ────────────────────────────────────────


def test_direct_yaml_load_detected():
    loads = find_data_loads(LOADER_BODY)
    paths = {d["path"] for d in loads}
    assert "world_data.yaml" in paths
    assert all(d["resolved"] for d in loads if d["path"])


def test_json_load_variant_detected():
    body = 'def load():\n    return json.load(open("save_data.json"))\n'
    assert any(d["path"] == "save_data.json" for d in find_data_loads(body))


def test_module_constant_path_resolved():
    src = 'DATA = "world_data.yaml"\n\ndef load():\n    return open(DATA)\n'
    consts = extract_module_constants(src)
    assert consts.get("DATA") == "world_data.yaml"
    # the load itself is unresolved (bare Name) until the constant is applied
    loads = find_data_loads("def load():\n    return open(DATA)\n")
    assert any(d["raw_arg"] == "DATA" and not d["resolved"] for d in loads)


def test_computed_path_unresolved():
    body = "def load():\n    return open(os.path.join(d, name))\n"
    assert all(d["path"] is None for d in find_data_loads(body))


def test_regex_fallback_finds_literals():
    body = 'x = yaml.safe_load(open("world_data.yaml"))'
    assert any(d["path"] == "world_data.yaml" for d in _find_data_loads_regex(body))


def test_slice_retains_entry_id():
    """Regression (structural): scoping slices the REAL subtree, so a shown entry
    can never lack its identifier. Dropping room ``id`` once made a model
    hallucinate "rooms are missing their id fields" and rewrite the whole data
    file to add ids that already existed; slicing makes that impossible."""
    content = (
        "rooms:\n"
        "  - id: entrance\n"
        "    name: Hall\n"
        "    items: []\n"
        "    exits:\n"
        "      n: library\n"
    )
    data = {
        "rooms": [
            {"id": "entrance", "name": "Hall", "items": [], "exits": {"n": "library"}},
        ]
    }
    scoped = _scope_to_keys(
        data, ["rooms", "items", "exits"], content, "world.yaml", 1200
    )
    assert "id: entrance" in scoped  # identifier always present — the real node
    assert "items: []" in scoped and "library" in scoped
    assert "name: Hall" in scoped  # real subtree: sibling content is shown, not pruned


def test_parses_data_detector():
    assert parses_data("    data = yaml.safe_load(f)\n")
    assert parses_data("return json.loads(text)")
    assert parses_data("cfg = tomllib.load(fp)")
    assert not parses_data('with open(path, "w") as f:\n    f.write(x)\n')
    assert not parses_data('return data["rooms"]')


# ── build_data_trace_evidence integration ──────────────────────────────


# The single most common loader shape: path is a PARAMETER (literal at the
# call site, not the body). The body-literal detector misses it — the
# parses_data + project-data fallback catches it.
PARAM_LOADER_BODY = (
    "def load_world_data(filepath):\n"
    '    with open(filepath, "r") as f:\n'
    "        data = yaml.safe_load(f)\n"
    '    for room in data["rooms"].values():\n'
    '        items = room["items"]\n'
    "    return data\n"
)


@pytest.mark.asyncio
async def test_param_path_loader_surfaces_project_data():
    """The live gap: `def load(filepath): yaml.safe_load(open(filepath))` has no
    body-literal, so direct/module-const resolution fails — but the symbol
    PARSES data, so the fallback surfaces the project's data file, key-scoped."""
    target = _sym("load_world_data", PARAM_LOADER_BODY)
    fc = {
        "data_file_contents": {"world_data.yaml": WORLD_YAML},
        "data_shapes": [{"file": "world_data.yaml"}],
    }
    ev = await build_data_trace_evidence(
        target_sym=target,
        symbol_table=[target],
        file_content="import yaml\n\n\n" + PARAM_LOADER_BODY,
        file_context=fc,
        effects=MockEffects(files={}),
    )
    assert "## Connected data: world_data.yaml" in ev
    assert "runtime" in ev  # the via note for a dynamic path
    assert "items: []" in ev  # the bug, still surfaced + scoped
    assert "Elder Sage" not in ev  # npcs scoped out


@pytest.mark.asyncio
async def test_generic_open_without_parse_no_fallback():
    """A function that opens a file but does NOT parse data (e.g. writing a log)
    must NOT trigger the project-data fallback — no false positives."""
    writer = _sym(
        "save_log",
        'def save_log(path, msg):\n    with open(path, "w") as f:\n        f.write(msg)\n',
    )
    ev = await build_data_trace_evidence(
        target_sym=writer,
        symbol_table=[writer],
        file_content="def save_log(path, msg):\n    open(path)\n",
        file_context={"data_file_contents": {"world_data.yaml": WORLD_YAML}},
        effects=MockEffects(files={}),
    )
    assert ev == ""


# ── build_data_trace_evidence integration ──────────────────────────────


@pytest.mark.asyncio
async def test_direct_load_surfaces_scoped_data():
    """HEADLINE regression: tracing the loader surfaces the empty courtyard
    items list. Scope = the REAL subtree of the accessed container (``rooms``):
    sibling fields like ``description`` are shown as-is (no reconstruction), while
    unrelated top-level containers (``npcs``) are never sliced in."""
    target = _sym("load_world", LOADER_BODY)
    ev = await build_data_trace_evidence(
        target_sym=target,
        symbol_table=[target],
        file_content=LOADER_FILE,
        file_context=_fc(),
        effects=MockEffects(files={}),
    )
    assert "## Connected data: world_data.yaml" in ev
    assert "loaded by load_world" in ev
    assert "items: []" in ev  # the bug, now visible
    assert "courtyard" in ev
    assert "Elder Sage" not in ev  # npcs: an unrelated top-level container, not sliced
    assert "mossy courtyard" in ev  # real subtree shown — sibling field NOT pruned away


@pytest.mark.asyncio
async def test_multihop_self_rooms_connects():
    """A method reading self.rooms (populated by __init__'s load) connects to
    the data via the sibling-method scan — the key correctness detail."""
    init = _sym(
        "GameEngine.__init__",
        "    def __init__(self):\n"
        '        self.rooms = yaml.safe_load(open("world_data.yaml"))["rooms"]\n',
        parent="GameEngine",
        kind="method",
    )
    check = _sym(
        "GameEngine.check_items",
        "    def check_items(self):\n"
        "        for room in self.rooms.values():\n"
        '            items = room["items"]\n'
        "            return items\n",
        parent="GameEngine",
        kind="method",
    )
    file_src = (
        "import yaml\n\n\nclass GameEngine:\n" + init["body"] + "\n" + check["body"]
    )
    ev = await build_data_trace_evidence(
        target_sym=check,
        symbol_table=[init, check],
        file_content=file_src,
        file_context=_fc(),
        effects=MockEffects(files={}),
    )
    assert "world_data.yaml" in ev
    assert "via self.rooms" in ev
    assert "items: []" in ev


@pytest.mark.asyncio
async def test_key_scoping_limits_output():
    """Only the accessed key's slice appears; unrelated top-level keys don't."""
    body = (
        "def load():\n"
        '    data = json.load(open("conf.json"))\n'
        '    return data["servers"]\n'
    )
    conf = (
        '{"servers": ["a", "b"], "secrets": ["x"], "limits": {"n": 1}, '
        '"flags": {"f": true}, "meta": {"v": 2}}'
    )
    target = _sym("load", body)
    ev = await build_data_trace_evidence(
        target_sym=target,
        symbol_table=[target],
        file_content="import json\n\n\n" + body,
        file_context=_fc(conf, "conf.json"),
        effects=MockEffects(files={}),
    )
    assert "servers" in ev
    assert "secrets" not in ev and "flags" not in ev  # scoped out


def test_head_sample_on_overflow_keeps_ids():
    """Overflow head-samples COMPLETE entries: shown rooms keep their ids and a
    '… (N more items)' note appears — never a mid-entry char-truncation that could
    sever an identifier (the failure the retired _prune/_IDENTITY_FIELDS guarded)."""
    rooms = [{"id": f"room_{i}", "name": f"Room {i}", "items": []} for i in range(30)]
    lines = ["rooms:"]
    for r in rooms:
        lines += [f"  - id: {r['id']}", f"    name: {r['name']}", "    items: []"]
    content = "\n".join(lines) + "\n"

    scoped = _scope_to_keys({"rooms": rooms}, ["rooms"], content, "world.yaml", 200)

    assert "id: room_0" in scoped  # the head entry's id is present
    assert "more items" in scoped  # overflow is announced, entry-wise
    # Every shown room is COMPLETE (carries both id and name) — no partial entry.
    n_ids = scoped.count("id: room_")
    n_names = scoped.count("name: Room")
    assert n_ids >= 1 and n_ids == n_names


def test_toml_read_surfaces_scoped_data():
    """TOML read path: Document.read(..., Fmt.TOML) slices the accessed table's
    real subtree (rendered as YAML); an unrelated table is not sliced in."""
    import tomllib

    content = '[server]\nhost = "h"\nport = 8080\n\n[db]\nname = "main"\n'
    data = tomllib.loads(content)

    scoped = _scope_to_keys(data, ["server"], content, "config.toml", 1200)

    assert "host: h" in scoped and "port: 8080" in scoped
    assert "name: main" not in scoped  # unrelated [db] table not sliced in


@pytest.mark.asyncio
async def test_no_keys_falls_back_to_shape():
    """A loader with no data[...] access still surfaces the file's shape."""
    body = 'def load():\n    return yaml.safe_load(open("world_data.yaml"))\n'
    target = _sym("load", body)
    ev = await build_data_trace_evidence(
        target_sym=target,
        symbol_table=[target],
        file_content="import yaml\n\n\n" + body,
        file_context=_fc(),
        effects=MockEffects(files={}),
    )
    assert "## Connected data: world_data.yaml" in ev
    assert "rooms" in ev  # shape/skeleton shows top-level structure


@pytest.mark.asyncio
async def test_graceful_noop_when_no_data_connection():
    body = "def helper(x):\n    return x + 1\n"
    target = _sym("helper", body)
    ev = await build_data_trace_evidence(
        target_sym=target,
        symbol_table=[target],
        file_content=body,
        file_context=None,
        effects=MockEffects(files={}),
    )
    assert ev == ""


@pytest.mark.asyncio
async def test_data_file_not_found_emits_note():
    target = _sym("load_world", LOADER_BODY)
    ev = await build_data_trace_evidence(
        target_sym=target,
        symbol_table=[target],
        file_content=LOADER_FILE,
        file_context=None,  # no data_file_contents
        effects=MockEffects(files={}),  # not on disk either
    )
    assert "world_data.yaml" in ev and "not found" in ev


@pytest.mark.asyncio
async def test_parse_error_surfaced():
    target = _sym("load_world", LOADER_BODY)
    ev = await build_data_trace_evidence(
        target_sym=target,
        symbol_table=[target],
        file_content=LOADER_FILE,
        file_context=_fc("rooms:\n  hall:\n    items: [a, b\n"),  # unclosed seq
        effects=MockEffects(files={}),
    )
    assert "world_data.yaml" in ev and "parse error" in ev


# ── Seed pointer ───────────────────────────────────────────────────────


def _seed_step(effects, **ctx):
    return StepInput(
        context=dict(ctx),
        params={},
        meta=FlowMeta(flow_name="diagnose_issue", step_id="start_session", attempt=1),
        effects=effects,
    )


@pytest.mark.asyncio
async def test_seed_lists_data_files_from_data_shapes():
    effects = MockEffects()
    out = await action_start_diagnosis_session(
        _seed_step(
            effects,
            flow_directive="diagnose",
            file_context={
                "data_shapes": [
                    {"file": "world_data.yaml"},
                    {"file": "save_data.json"},
                ]
            },
        )
    )
    seed = out.context_updates.get("session_injections", [""])[0]
    assert "## Data files" in seed
    assert "world_data.yaml" in seed and "save_data.json" in seed


@pytest.mark.asyncio
async def test_seed_enumerates_via_listdir_backstop():
    effects = MockEffects(files={"world.yaml": "rooms: {}", "main.py": "x = 1"})
    out = await action_start_diagnosis_session(
        _seed_step(effects, flow_directive="diagnose", file_context={})
    )
    seed = out.context_updates.get("session_injections", [""])[0]
    assert "## Data files" in seed and "world.yaml" in seed
    assert "main.py" not in seed  # code file excluded


@pytest.mark.asyncio
async def test_seed_omits_section_when_no_data_files():
    effects = MockEffects(files={"main.py": "x = 1"})
    out = await action_start_diagnosis_session(
        _seed_step(effects, flow_directive="diagnose", file_context={})
    )
    seed = out.context_updates.get("session_injections", [""])[0]
    assert "## Data files" not in seed


# ── Fix 1: subdir data-key reconciliation (bare literal → world/ key) ──

_SUBDIR_LOADER = (
    "def load_world():\n"
    '    data = yaml.safe_load(open("rooms.yaml"))\n'  # bare literal; world/ dropped
    '    rooms = data["rooms"]\n'
    "    return rooms\n"
)


@pytest.mark.asyncio
async def test_subdir_data_key_reconciled_from_bare_literal():
    # The loader's `world/` prefix is dropped by static detection; the stored
    # key is `world/rooms.yaml`. Reconciliation must surface the real content
    # instead of the "not found" note.
    target = _sym("load_world", _SUBDIR_LOADER)
    ev = await build_data_trace_evidence(
        target_sym=target,
        symbol_table=[target],
        file_content="import yaml\n\n\n" + _SUBDIR_LOADER,
        file_context={"data_file_contents": {"world/rooms.yaml": WORLD_YAML}},
        effects=MockEffects(files={}),
    )
    assert "courtyard" in ev and "items: []" in ev
    assert "not found" not in ev.lower()


@pytest.mark.asyncio
async def test_ambiguous_basename_degrades_to_note():
    # Same basename in two dirs → ambiguous → reconciliation returns None →
    # the "not found" note (never the wrong file).
    target = _sym("load_world", _SUBDIR_LOADER)
    ev = await build_data_trace_evidence(
        target_sym=target,
        symbol_table=[target],
        file_content="import yaml\n\n\n" + _SUBDIR_LOADER,
        file_context={
            "data_file_contents": {
                "world/rooms.yaml": WORLD_YAML,
                "dungeon/rooms.yaml": WORLD_YAML,
            }
        },
        effects=MockEffects(files={}),
    )
    assert "not found" in ev.lower()


@pytest.mark.asyncio
async def test_toplevel_data_file_still_exact_matches():
    # No regression: a genuine top-level `rooms.yaml` (key == query) still
    # exact-matches without going through reconciliation.
    target = _sym("load_world", _SUBDIR_LOADER)
    ev = await build_data_trace_evidence(
        target_sym=target,
        symbol_table=[target],
        file_content="import yaml\n\n\n" + _SUBDIR_LOADER,
        file_context={"data_file_contents": {"rooms.yaml": WORLD_YAML}},
        effects=MockEffects(files={}),
    )
    assert "courtyard" in ev and "not found" not in ev.lower()
