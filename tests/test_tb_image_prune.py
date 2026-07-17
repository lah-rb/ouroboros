"""adapters.tb image-prune policy (OURO_TB_PRUNE_IMAGES) — the durable patch for
the never-pruned TB2 task-image sink that drove the host-memory creep.

The tb/Harbor harness owns the container; we prune the IMAGE that persists after
teardown. per_instance = lag-remove the prior task's image at the next task's
start; run_end = atexit sweep of every touched image; off = no-op.
"""

from __future__ import annotations

import types

import adapters.tb.image_prune as ip


class _FakeImage:
    def __init__(self, tag=None, iid="sha256:deadbeef"):
        self.tags = [tag] if tag else []
        self.id = iid


class _FakeClient:
    def __init__(self):
        self.images = types.SimpleNamespace(removed=[])
        self.images.remove = lambda image, force=False: self.images.removed.append(
            image
        )
        self.closed = False

    def close(self):
        self.closed = True


def _container(tag, client):
    return types.SimpleNamespace(image=_FakeImage(tag), client=client)


def _reset(mode):
    ip._PRUNE_MODE = mode
    ip._touched.clear()
    ip._prev.clear()
    ip._atexit_registered = True  # suppress real atexit registration in tests


def test_image_ref_prefers_tag_then_id():
    assert (
        ip._image_ref(types.SimpleNamespace(image=_FakeImage("alexgshaw/x:1")))
        == "alexgshaw/x:1"
    )
    assert (
        ip._image_ref(types.SimpleNamespace(image=_FakeImage(None, "sha256:abc")))
        == "sha256:abc"
    )
    assert ip._image_ref(types.SimpleNamespace(image=None)) == ""


def test_per_instance_lag_removes_prior_task_image():
    _reset("per_instance")
    c = _FakeClient()
    ip.note_task_image(_container("alexgshaw/taskA:1", c))
    assert c.images.removed == []  # first task: nothing to remove yet
    ip.note_task_image(_container("alexgshaw/taskB:1", c))
    assert c.images.removed == ["alexgshaw/taskA:1"]  # prior task's image pruned
    assert ip._prev == ["alexgshaw/taskB:1"]  # last image awaits atexit


def test_run_end_records_but_defers_to_atexit(monkeypatch):
    _reset("run_end")
    c = _FakeClient()
    ip.note_task_image(_container("alexgshaw/taskA:1", c))
    ip.note_task_image(_container("alexgshaw/taskB:1", c))
    assert c.images.removed == []  # run_end defers all removal to exit
    assert ip._touched == {"alexgshaw/taskA:1", "alexgshaw/taskB:1"}
    # the atexit sweep prunes every touched image via a fresh client
    sweep_client = _FakeClient()
    monkeypatch.setattr(
        ip,
        "docker",
        types.SimpleNamespace(from_env=lambda: sweep_client),
        raising=False,
    )
    import sys

    monkeypatch.setitem(
        sys.modules, "docker", types.SimpleNamespace(from_env=lambda: sweep_client)
    )
    ip._run_end_sweep()
    assert set(sweep_client.images.removed) == {
        "alexgshaw/taskA:1",
        "alexgshaw/taskB:1",
    }
    assert sweep_client.closed is True


def test_off_is_a_noop():
    _reset("off")
    c = _FakeClient()
    ip.note_task_image(_container("alexgshaw/taskA:1", c))
    assert c.images.removed == [] and not ip._touched and not ip._prev


def test_note_never_raises_on_bad_container():
    _reset("run_end")
    ip.note_task_image(types.SimpleNamespace(image=None))  # no image → no-op, no raise
    ip.note_task_image(object())  # missing attrs → swallowed
