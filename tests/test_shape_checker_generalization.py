"""Cross-domain generalization battery for the exemplar shape checker.

The checker must work on UNKNOWN task space, not just the benchmark.
Live failure (gpt-oss parallel run): 7 immortal findings on NPC dialogue
node names — re-refuted 48 times across 52 gate rounds — because
multi-key mappings were always treated as closed structs. This battery
diffs realistic data from foreign domains (web routes, CI pipelines,
package manifests, ML configs, state machines) and pins three rules:

  open-by-homogeneity   — exemplar mapping whose values all share one
                          dict shape is a keyed collection, not a struct
  variant-list elements — 2+ exemplar list elements with differing key
                          sets declare variants (union allowed,
                          intersection required)
  closed scalar maps    — scalar-valued multi-key maps stay CLOSED on
                          purpose: structurally indistinguishable from
                          scalar structs, and opening them would silence
                          real key renames (proven by the FN guards).
                          That residual class belongs to refuted-
                          signature suppression, not this checker.
"""

from __future__ import annotations

from agent.actions.research_actions import _shape_diff


def diff(data, exemplar):
    issues: list[dict] = []
    _shape_diff(data, exemplar, "f", issues)
    return issues


# ── open collections (must NOT flag) ─────────────────────────────────


def test_web_routes_path_keyed_collection_is_open():
    exemplar = {
        "routes": {
            "/": {"handler": "home", "methods": ["GET"]},
            "/login": {"handler": "auth", "methods": ["POST"]},
        }
    }
    data = {
        "routes": {
            "/": {"handler": "home", "methods": ["GET"]},
            "/about": {"handler": "about", "methods": ["GET"]},
            "/api/users": {"handler": "users", "methods": ["GET"]},
        }
    }
    assert diff(data, exemplar) == []


def test_dialogue_nodes_are_open_the_immortal_findings_class():
    # The live noise: node ids are instance names; every node shares the
    # {text, responses} shape, which is the exemplar declaring a
    # collection.
    exemplar = {
        "npcs": [
            {
                "id": "guide",
                "dialogue": {
                    "start": {"text": "Hello", "responses": []},
                    "ask_help": {"text": "Sure", "responses": []},
                    "end": {"text": "Bye", "responses": []},
                },
            }
        ]
    }
    data = {
        "npcs": [
            {
                "id": "guide",
                "dialogue": {
                    "start": {"text": "Hello", "responses": []},
                    "secret_path": {"text": "Psst", "responses": []},
                    "goodbye": {"text": "Farewell", "responses": []},
                },
            }
        ]
    }
    assert diff(data, exemplar) == []


def test_variant_ci_steps_union_allowed_intersection_required():
    exemplar = {
        "steps": [
            {"name": "checkout", "uses": "actions/checkout@v4"},
            {"name": "test", "run": "pytest", "env": {"CI": "true"}},
        ]
    }
    data = {
        "steps": [
            {"name": "lint", "run": "ruff check ."},
            {"name": "build", "uses": "docker/build@v5"},
        ]
    }
    assert diff(data, exemplar) == []
    # A key in NO variant still flags (true under-declaration)...
    flagged = diff({"steps": [{"name": "x", "with": {"a": 1}}]}, exemplar)
    assert any(i["kind"] == "undeclared_key" for i in flagged)
    # ...and dropping an every-variant key flags.
    missing = diff({"steps": [{"run": "pytest"}]}, exemplar)
    assert any(
        i["kind"] == "missing_declared_key" and "'name'" in i["detail"] for i in missing
    )


def test_state_machine_transitions_stay_open():
    exemplar = {
        "states": [
            {"id": "idle", "transitions": {"start": "running"}},
        ]
    }
    data = {
        "states": [
            {"id": "idle", "transitions": {"start": "running"}},
            {"id": "paused", "transitions": {"resume": "running", "stop": "idle"}},
        ]
    }
    assert diff(data, exemplar) == []


# ── deliberate residuals (documented closed behavior) ────────────────


def test_scalar_valued_maps_stay_closed_by_design():
    # Hyperparameter/dependency maps are indistinguishable from scalar
    # structs; they remain closed (false-positive risk accepted —
    # handled by refuted-signature suppression downstream).
    exemplar = {"params": {"lr": 0.001, "batch_size": 32}}
    data = {"params": {"lr": 0.01, "batch_size": 64, "optimizer": "adamw"}}
    issues = diff(data, exemplar)
    assert any(i["kind"] == "undeclared_key" for i in issues)


# ── false-negative guards (must STILL flag) ──────────────────────────


def test_required_struct_field_missing_still_flags():
    exemplar = {"services": [{"name": "web", "image": "nginx:1.25", "ports": [80]}]}
    data = {"services": [{"name": "web", "ports": [80]}]}
    issues = diff(data, exemplar)
    assert any(
        i["kind"] == "missing_declared_key" and "'image'" in i["detail"] for i in issues
    )


def test_struct_reshape_still_flags():
    exemplar = {"server": {"host": "0.0.0.0", "port": 8080}}
    issues = diff({"server": ["0.0.0.0", 8080]}, exemplar)
    assert any(i["kind"] == "type_mismatch" for i in issues)


def test_key_rename_in_scalar_struct_still_flags():
    # The next_id/text rename class — the reason scalar structs stay
    # closed even though scalar collections false-positive.
    exemplar = {"card": {"choices": [{"text": "4", "next_id": "n1"}]}}
    data = {"card": {"choices": [{"label": "4", "next_id": "n1"}]}}
    issues = diff(data, exemplar)
    kinds = {i["kind"] for i in issues}
    assert "undeclared_key" in kinds and "missing_declared_key" in kinds


def test_heterogeneous_collection_values_still_checked():
    # Open-by-homogeneity validates VALUES: a route whose value drops a
    # declared field still flags inside the open collection.
    exemplar = {
        "routes": {
            "/": {"handler": "home", "methods": ["GET"]},
            "/x": {"handler": "x", "methods": ["GET"]},
        }
    }
    data = {"routes": {"/new": {"methods": ["GET"]}}}  # handler missing
    issues = diff(data, exemplar)
    assert any(
        i["kind"] == "missing_declared_key" and "'handler'" in i["detail"]
        for i in issues
    )
