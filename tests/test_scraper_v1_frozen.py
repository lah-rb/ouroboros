"""flows/scraper/ is FROZEN. Change flows/scraper_v2/ instead.

WHY A HASH MANIFEST AND NOT THE `version:` FIELD. Every #FlowDefinition
carries a `version: int`, and nothing anywhere reads it — not the
compiler, not the loader, not the runtime, not the lint. Bumping it
communicates intent to a human and changes no behaviour whatsoever. The
only mechanisms that actually separate two flow sets in this codebase are
the directory, the globally-unique flow names the compiler enforces, and
the FlowSetSpec registration. This test is the fourth: it makes an edit
to the frozen set FAIL rather than silently succeed.

WHY FREEZE AT ALL. v1 is running a live mission with ~1,100 extracted
papers behind it. The v2 restructure removes the parallel barrier and
moves the drains to continuous workers — a change to how everything is
scheduled. Those two should not share files while v2 is being proven,
and a mission already running v1 must be unable to be altered by v2 work
(its flow_set is per-mission config, so it keeps resolving v1 by name).

TO CHANGE A FROZEN FLOW: don't. Add the change to flows/scraper_v2/. If
a fix genuinely belongs to v1 — a live incident on the running mission —
update MANIFEST below in the same commit, so the edit is a deliberate,
reviewed act rather than an accident.
"""

from __future__ import annotations

import hashlib
import pathlib

FROZEN_DIR = pathlib.Path(__file__).resolve().parents[1] / "flows" / "scraper"

# sha256 of each frozen flow, as of the freeze.
MANIFEST = {
    "acquire_catalog.cue": "f2125a1288af4865a8b6ff740a5e90a167f6317837d1234da0c019763f200a80",
    "catalog_work.cue": "e5faf3a1925dd3c289a710aea8cf427836f03c80c5dec837dc3af091e55a8bca",
    "curate_drain.cue": "a2ea2fc70cc6d0d3b98663127b03cbf2560b00ab65e941652817a61653e57d5b",
    "discover.cue": "279811d62addcb2fe4783ba94765a1f76b89625ad7437ced7c237aca71a0d609",
    "discover_work.cue": "15b000265b405ee6a3085bca2a9502c539104e69f41b309add674bbd896a246e",
    "figtext_drain.cue": "aaf4ab27f729af01bbadd97ab0e3d33463dd1d26e89f80288ff153d7bc264b89",
    "ocr_drain.cue": "421d93144f7cc891027caf6e139efdb7d22ab780f650567915aa2b93ae55d727",
    "plan_research.cue": "8af6fb7ff6dde166fb22b89c14311fc2dacf9ab50948ea3a454c3d9e37da06a3",
    "research_control.cue": "086cd8695302405374f1d37b6ebbf23a492c9a07c98c11e1e810e9181cac4f2b",
    "research_gate.cue": "b1c697cdd15c045c3ce00edb10da51ee7a3eee1ccbfbc05d3d0ed7f526e9e552",
    "translate_drain.cue": "084a9f767c21b13caa172547a9185d30510e991b09afa5713a9aa6a78a362de0",
}


def _digest(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_manifest_covers_every_frozen_flow():
    """A new file in the frozen set is itself a freeze violation — it
    would otherwise be an unreviewed addition to v1."""
    on_disk = {p.name for p in FROZEN_DIR.glob("*.cue")}
    assert on_disk == set(MANIFEST), (
        "flows/scraper/ membership changed.\n"
        f"  added:   {sorted(on_disk - set(MANIFEST))}\n"
        f"  removed: {sorted(set(MANIFEST) - on_disk)}\n"
        "scraper v1 is FROZEN — put new flows in flows/scraper_v2/."
    )


def test_no_frozen_flow_has_been_edited():
    changed = [
        name
        for name, want in MANIFEST.items()
        if (FROZEN_DIR / name).exists() and _digest(FROZEN_DIR / name) != want
    ]
    assert not changed, (
        f"scraper v1 is FROZEN and these were edited: {changed}\n"
        "Put the change in flows/scraper_v2/. If it truly belongs to the "
        "running v1 mission, update MANIFEST in the same commit so the "
        "edit is deliberate and reviewed."
    )


def test_the_guard_actually_detects_an_edit(tmp_path):
    """A freeze test that only ever passes is indistinguishable from no
    freeze test. This proves the comparison catches a changed byte."""
    f = tmp_path / "x.cue"
    f.write_text("version: 1\n")
    before = _digest(f)
    f.write_text("version: 2\n")
    assert _digest(f) != before


def test_every_manifest_entry_is_a_real_sha256():
    """A truncated or placeholder hash would silently match nothing and
    make the guard pass for the wrong reason."""
    for name, h in MANIFEST.items():
        assert len(h) == 64 and set(h) <= set("0123456789abcdef"), name
