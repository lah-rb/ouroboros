"""Strict model routing for hot secondaries (Phase 2b).

THE FAILURE THIS GUARDS. ``resolve_local_backend`` returning None means
"serve with the primary". If an unknown, unloaded, or cosmetic model name
also returned None, a request naming model X would be answered by model Y,
correctly formatted, with no error at any layer — the silent-wrong-output
class. Every negative case here asserts a RAISE, not a fallback.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import remote_router, resident_models  # noqa: E402


@pytest.fixture(autouse=True)
def clean():
    resident_models._reset_for_tests()
    yield
    resident_models._reset_for_tests()


def _registry(monkeypatch, active="primary", known=("primary", "secondary")):
    """Patch ATTRIBUTES on the real module, not sys.modules.

    ``from core import model_registry`` inside a function reads the attribute
    off the already-imported ``core`` package — it never re-consults
    ``sys.modules["core.model_registry"]``. Replacing that key looks like it
    works and silently does nothing, which is how the first version of these
    tests "passed" against unpatched code.
    """
    from core import model_registry

    monkeypatch.setattr(model_registry, "active_name", lambda: active)
    monkeypatch.setattr(
        model_registry,
        "list_models",
        lambda: [types.SimpleNamespace(name=n) for n in known],
    )


def _make_hot(name, backend=None):
    entry = resident_models.ResidentEntry(
        name=name,
        backend=backend or object(),
        config=None,
        footprint_bytes=1,
    )
    resident_models._registry.entries[name] = entry
    return entry.backend


def test_empty_model_means_primary(monkeypatch):
    _registry(monkeypatch)
    assert remote_router.resolve_local_backend(None) is None
    assert remote_router.resolve_local_backend("") is None


def test_active_name_means_primary(monkeypatch):
    _registry(monkeypatch)
    assert remote_router.resolve_local_backend("primary") is None


def test_hot_secondary_returns_its_backend(monkeypatch):
    _registry(monkeypatch)
    monkeypatch.setattr(remote_router, "get_adapter", lambda n: None)
    sentinel = object()
    _make_hot("secondary", sentinel)
    assert remote_router.resolve_local_backend("secondary") is sentinel


def test_known_but_cold_raises_not_falls_back(monkeypatch):
    """The whole point: a config that exists but is not hot must NOT be
    quietly answered by the primary."""
    _registry(monkeypatch)
    monkeypatch.setattr(remote_router, "get_adapter", lambda n: None)
    with pytest.raises(remote_router.InactiveLocalModel, match="loadModel"):
        remote_router.resolve_local_backend("secondary")


def test_unknown_name_raises_keyerror(monkeypatch):
    _registry(monkeypatch)
    monkeypatch.setattr(remote_router, "get_adapter", lambda n: None)
    with pytest.raises(KeyError):
        remote_router.resolve_local_backend("PaddleOCR-VL-1.6")


def test_remote_entry_on_a_local_path_raises(monkeypatch):
    """A remote entry cannot serve vision/sessions — say so rather than
    handing the request to the primary."""
    _registry(monkeypatch)
    monkeypatch.setattr(remote_router, "get_adapter", lambda n: ("cfg", "adapter"))
    with pytest.raises(remote_router.InactiveLocalModel, match="REMOTE"):
        remote_router.resolve_local_backend("boss-claude")


# ── resolve_route: hot secondaries are LOCAL, not an error ────────────


def test_resolve_route_treats_hot_secondary_as_local(monkeypatch):
    _registry(monkeypatch)
    monkeypatch.setattr(remote_router, "get_adapter", lambda n: None)
    _make_hot("secondary")
    assert remote_router.resolve_route("secondary") is None


def test_resolve_route_still_rejects_cold_local(monkeypatch):
    _registry(monkeypatch)
    monkeypatch.setattr(remote_router, "get_adapter", lambda n: None)
    with pytest.raises(remote_router.InactiveLocalModel):
        remote_router.resolve_route("secondary")


def test_resolve_route_still_routes_remote(monkeypatch):
    _registry(monkeypatch)
    monkeypatch.setattr(remote_router, "get_adapter", lambda n: ("cfg", "adapter"))
    assert remote_router.resolve_route("boss") == ("cfg", "adapter")


# ── _get_backend dispatch ─────────────────────────────────────────────


def test_get_backend_returns_secondary_when_named(monkeypatch):
    import asyncio

    from core import inference

    _registry(monkeypatch)
    monkeypatch.setattr(remote_router, "get_adapter", lambda n: None)
    sentinel = object()
    _make_hot("secondary", sentinel)
    assert asyncio.run(inference._get_backend("secondary")) is sentinel


def test_get_backend_unnamed_is_untouched(monkeypatch):
    """The eight pre-existing call sites pass nothing and must keep their
    exact behaviour — the primary, via the factory."""
    import asyncio

    from core import inference

    primary = object()
    monkeypatch.setattr(inference, "get_backend", lambda: primary)
    monkeypatch.setattr(
        inference,
        "swap_in_progress" if hasattr(inference, "swap_in_progress") else "get_backend",
        lambda: primary,
        raising=False,
    )
    assert asyncio.run(inference._get_backend()) is primary
