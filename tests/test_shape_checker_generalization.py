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


# ── empirical over-globalization guard (scalar-valued maps that VARY) ──
#
# The residual class above (scalar-valued multi-key maps) is only closed
# because the EXEMPLAR alone can't distinguish an open map from a struct.
# The DATA can: an open map's per-instance key-sets diverge across list
# siblings, a struct's stay consistent. The guard opens the former and
# leaves the latter closed (rename detection preserved). Fresh domains
# (i18n locale maps, contacts) — never the benchmark.


def test_i18n_locale_maps_open_empirically():
    # `labels` is a scalar map (locale->text) whose key-set varies across 3
    # screens -> empirically OPEN; es/de/ja are not undeclared keys.
    exemplar = {
        "screens": [{"name": "home", "labels": {"en": "Home", "fr": "Accueil"}}]
    }
    data = {
        "screens": [
            {"name": "home", "labels": {"en": "Home", "fr": "Accueil"}},
            {"name": "cart", "labels": {"en": "Cart", "es": "Carrito", "de": "Korb"}},
            {"name": "help", "labels": {"en": "Help", "ja": "herupu"}},
        ]
    }
    assert diff(data, exemplar) == []


def test_contact_field_rename_still_flags_across_records():
    # Contacts are scalar-only structs (no dict-valued field) -> the guard
    # never engages; a real key rename (phone->mobile) still flags.
    exemplar = {"contacts": [{"name": "a", "email": "a@x", "phone": "1"}]}
    data = {
        "contacts": [
            {"name": "a", "email": "a@x", "phone": "1"},
            {"name": "b", "email": "b@x", "phone": "2"},
            {"name": "c", "email": "c@x", "phone": "3"},
            {"name": "d", "email": "d@x", "mobile": "4"},
        ]
    }
    issues = diff(data, exemplar)
    assert any(
        i["kind"] == "undeclared_key" and "'mobile'" in i["detail"] for i in issues
    )
    assert any(
        i["kind"] == "missing_declared_key" and "'phone'" in i["detail"] for i in issues
    )


def test_dict_field_rename_stays_closed_by_majority():
    # `address` is a scalar-valued map, but a modal majority (3/4) share its
    # key-set -> stays CLOSED -> the zip->postcode rename still flags. This
    # exercises the helper's rename-safe path directly.
    exemplar = {
        "contacts": [{"name": "a", "address": {"street": "s", "city": "c", "zip": "z"}}]
    }
    data = {
        "contacts": [
            {"name": "a", "address": {"street": "s1", "city": "c1", "zip": "z1"}},
            {"name": "b", "address": {"street": "s2", "city": "c2", "zip": "z2"}},
            {"name": "c", "address": {"street": "s3", "city": "c3", "zip": "z3"}},
            {"name": "d", "address": {"street": "s4", "city": "c4", "postcode": "z4"}},
        ]
    }
    issues = diff(data, exemplar)
    assert any(
        i["kind"] == "undeclared_key" and "'postcode'" in i["detail"] for i in issues
    )
    assert any(
        i["kind"] == "missing_declared_key" and "'zip'" in i["detail"] for i in issues
    )


def test_two_divergent_map_instances_stay_closed():
    # n=2 is below the sample floor -> undecidable -> CLOSED -> es flags.
    exemplar = {
        "screens": [{"name": "home", "labels": {"en": "Home", "fr": "Accueil"}}]
    }
    data = {
        "screens": [
            {"name": "home", "labels": {"en": "Home", "fr": "Accueil"}},
            {"name": "cart", "labels": {"en": "Cart", "es": "Carrito"}},
        ]
    }
    issues = diff(data, exemplar)
    assert any(i["kind"] == "undeclared_key" and "'es'" in i["detail"] for i in issues)


def test_three_identical_map_instances_produce_no_noise():
    # modal 3/3 (union == modal) -> CLOSED, and conformant -> no issues.
    exemplar = {"screens": [{"name": "s", "labels": {"en": "x", "fr": "y"}}]}
    data = {
        "screens": [
            {"name": "a", "labels": {"en": "1", "fr": "2"}},
            {"name": "b", "labels": {"en": "3", "fr": "4"}},
            {"name": "c", "labels": {"en": "5", "fr": "6"}},
        ]
    }
    assert diff(data, exemplar) == []


def test_three_instance_majority_stays_closed():
    # 2/3 share {en,fr}; modal not < half -> CLOSED -> de flags. Pins the
    # modal_count*2 < n threshold.
    exemplar = {"screens": [{"name": "s", "labels": {"en": "x", "fr": "y"}}]}
    data = {
        "screens": [
            {"name": "a", "labels": {"en": "1", "fr": "2"}},
            {"name": "b", "labels": {"en": "3", "fr": "4"}},
            {"name": "c", "labels": {"en": "5", "fr": "6", "de": "7"}},
        ]
    }
    issues = diff(data, exemplar)
    assert any(i["kind"] == "undeclared_key" and "'de'" in i["detail"] for i in issues)


def test_empirically_open_map_values_still_shape_checked():
    # `labels` opens; a dict value where a scalar is declared still flags.
    exemplar = {
        "screens": [{"name": "home", "labels": {"en": "Home", "fr": "Accueil"}}]
    }
    data = {
        "screens": [
            {"name": "home", "labels": {"en": "Home", "fr": "Accueil"}},
            {"name": "cart", "labels": {"en": "Cart", "es": "Carrito", "de": "Korb"}},
            {"name": "help", "labels": {"en": {"text": "Help"}, "ja": "x"}},
        ]
    }
    issues = diff(data, exemplar)
    assert any(
        i["kind"] == "type_mismatch" and i["path"] == "f.screens[2].labels.en"
        for i in issues
    )


def test_map_field_is_empirically_open_unit():
    from agent.actions.research_actions import _map_field_is_empirically_open

    assert _map_field_is_empirically_open([{"a": 1}, {"b": 2}]) is False  # n<3
    assert (
        _map_field_is_empirically_open([{"a": 1}, {"a": 2}, {"a": 3}]) is False
    )  # homogeneous
    assert (
        _map_field_is_empirically_open([{"a": 1}, {"b": 2}, {"c": 3}]) is True
    )  # all divergent
    assert (
        _map_field_is_empirically_open([{"a": 1}, {"a": 2}, {"a": 3}, {"b": 4}])
        is False
    )  # modal majority
