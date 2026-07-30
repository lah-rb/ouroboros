"""The seam gate stops burning its budget on code that cannot run.

THE EVIDENCE (2026-07-30): the gate's fail-open WARNING fired 8 times across
the run corpus. Five were ONE devstral run re-reporting
``GameEngine._handle_flee`` — a real AttributeError sitting inside
``_handle_command``, a second dispatcher the model wrote while migrating to
helper style and then never wired up (``run()`` still called the original,
which handled flee inline). The seam could not execute; the fix loop spent
all three attempts on it; the phase exited claiming unresolved seams.

Three behaviours are pinned here:
  * a seam reachable only from dead code is REPORTED, never blocking
  * a missing DEFINITION is dispatched as "add it", not as "fix the caller" —
    the old directive pointed at the call site, which is why three identical
    attempts re-edited the caller and never wrote the method
  * dead duplicates and orphaned module-level methods are surfaced, since no
    other check sees them (an orphan is a definition, not an attribute
    access, so the typecheck half is structurally blind to it)

The reachability analysis is deliberately biased toward calling things LIVE:
a false "dead" suppresses a real seam.
"""

from __future__ import annotations

import pytest

from agent.actions.batch_structural_actions import _symbol_reachability

# ── the real artifact's shape, reduced ────────────────────────────────

DEVSTRAL = {
    "engine.py": '''
class GameEngine:
    def __init__(self):
        self.player = None

    def run(self):
        while True:
            response = self.handle_command({"command": "flee"})

    def handle_command(self, command):
        """Handle a parsed command."""
        cmd = command["command"]
        if cmd == "flee":
            return "You flee!"
        elif cmd == "talk":
            return self._talk_to_npc(command["npc"])

    def _talk_to_npc(self, npc):
        return "hi"

    def _use_item(self, item_name):
        return "used"

    def _handle_command(self, command):
        """Handle a parsed command."""
        cmd = command.get("command")
        if cmd == "flee":
            return self._handle_flee()
        elif cmd == "use":
            return self._use_item(command.get("item", ""))
        return ""


def _use_item(self, item_name):
    return "used"
''',
    "main.py": """
from engine import GameEngine

def main():
    GameEngine().run()
""",
}


class TestReachability:
    def test_finds_only_the_dead_dispatcher(self):
        r = _symbol_reachability(DEVSTRAL)
        assert r["dead"] == {"engine.py::GameEngine._handle_command"}, (
            "handle_command/run/_talk_to_npc are all reached; only the "
            "unwired second dispatcher is dead"
        )

    def test_the_seam_is_reachable_from_dead_code_only(self):
        r = _symbol_reachability(DEVSTRAL)
        assert r["access_sites"]["_handle_flee"] == {
            "engine.py::GameEngine._handle_command"
        }
        assert r["access_sites"]["_handle_flee"] <= r["dead"]

    def test_orphaned_module_level_method(self):
        """`def _use_item(self, ...)` at module level fell out of its class on
        a splice. No other check sees it — it is a definition, not an
        attribute access."""
        assert _symbol_reachability(DEVSTRAL)["orphans"] == ["engine.py::_use_item"]

    def test_dead_duplicate_pair(self):
        assert _symbol_reachability(DEVSTRAL)["dead_dupes"] == [
            (
                "engine.py::GameEngine._handle_command",
                "engine.py::GameEngine.handle_command",
            )
        ]

    def test_reachability_is_single_level_by_design(self):
        """`_use_item` is referenced ONLY from the dead dispatcher, and is
        still counted live. Transitive elimination would be more thorough and
        much riskier — one wrong 'dead' silences a real seam. The orphan check
        is what catches the module-level `_use_item` regardless, which is
        exactly how it was found in the real artifact."""
        r = _symbol_reachability(DEVSTRAL)
        assert "engine.py::GameEngine._use_item" not in r["dead"]
        assert "engine.py::_use_item" in r["orphans"]


class TestBiasTowardLive:
    """A false 'dead' silences a real seam, so each of these must stay live."""

    def test_dunders_are_live(self):
        r = _symbol_reachability(
            {"a.py": "class C:\n    def __init__(self):\n        pass\n"}
        )
        assert r["dead"] == set()

    def test_string_reference_counts_as_live(self):
        """getattr / dynamic dispatch — the name only ever appears in a string."""
        src = {
            "a.py": (
                "class C:\n"
                "    def handler(self):\n"
                "        return 1\n"
                "    def go(self, n):\n"
                "        return getattr(self, 'handler')()\n"
                "c = C()\n"
                "c.go(1)\n"
            )
        }
        assert "a.py::C.handler" not in _symbol_reachability(src)["dead"]

    def test_decorated_is_live(self):
        src = {
            "a.py": "import functools\n@functools.cache\ndef helper():\n    return 1\n"
        }
        assert _symbol_reachability(src)["dead"] == set()

    def test_recursion_alone_is_not_reachability(self):
        """A function that only calls itself is still unreachable."""
        src = {"a.py": "def loop(n):\n    return loop(n - 1)\n"}
        assert _symbol_reachability(src)["dead"] == {"a.py::loop"}

    def test_main_is_implicitly_live(self):
        assert (
            _symbol_reachability({"a.py": "def main():\n    return 1\n"})["dead"]
            == set()
        )

    def test_syntax_error_file_is_skipped_not_crashed(self):
        r = _symbol_reachability(
            {"bad.py": "def (:\n", "ok.py": "def main():\n    pass\n"}
        )
        assert r["dead"] == set()


# ── the gate's use of it ──────────────────────────────────────────────


class TestGateWiring:
    """Source guards for the seams a refactor could sever. The gate itself is
    async + effects-heavy; these pin the decisions, not the plumbing."""

    def _src(self) -> str:
        import inspect

        from agent.actions import mission_actions

        return inspect.getsource(mission_actions._phase_exit_seam_gate)

    def test_unreachable_seams_do_not_block(self):
        src = self._src()
        assert "if attr and holders and holders <= _dead:" in src
        assert "unreachable.append(v)" in src
        assert "if not blocking:" in src

    def test_unknown_access_sites_stay_blocking(self):
        """No sites found must NOT be read as 'dead' — the bias is toward
        blocking when reachability is unknown."""
        src = self._src()
        assert (
            "holders and holders <= _dead" in src
        ), "an empty holder set must fail the suppression test"

    def test_missing_definition_gets_an_add_directive(self):
        src = self._src()
        assert "are CALLED but never DEFINED" in src
        assert "do not edit them" in src

    def test_cruft_is_reported_without_spending_an_attempt(self):
        src = self._src()
        assert "seam_gate_advisory" in src
        assert "NOT blocking, no fix attempt spent" in src
        # The advisory note must NOT carry the seam_gate tag, which is what
        # the attempt counter sums.
        advisory = src[src.index("cruft = [") :]
        assert 'tags=["seam_gate_advisory"]' in advisory

    def test_the_attempt_counter_still_only_counts_blocking_notes(self):
        """`attempts` sums notes tagged seam_gate. The advisory note uses a
        different tag on purpose — otherwise reporting cruft would exhaust
        the budget that fixing real seams needs."""
        import inspect

        from agent.actions import mission_actions

        src = inspect.getsource(mission_actions._phase_exit_seam_gate)
        assert '"seam_gate" in (getattr(n, "tags", None) or [])' in src


@pytest.mark.parametrize(
    "attr,expected",
    [
        (
            "GameEngine._handle_flee — 'GameEngine' has no attribute/method '_handle_flee' (declared: run)",
            "_handle_flee",
        ),
        (
            "Combat.resolve — 'Combat' has no attribute/method 'resolve' (declared: none)",
            "resolve",
        ),
        ("some other typecheck output entirely", ""),
        ("transfer shape: .get('hp') not in produced keys", ""),
    ],
)
def test_missing_attr_extraction(attr, expected):
    """The direction decision keys off this parse; a miss means the seam is
    treated as a wrong-call and blocks, which is the safe direction."""
    import re

    m = re.search(r"has no attribute/method '([A-Za-z_]\w*)'", attr)
    assert (m.group(1) if m else "") == expected
