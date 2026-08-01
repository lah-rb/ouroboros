"""The post-splice gate must not throw away a repair it did not cause.

THE TRAP (gemma-4-31b, tier_20260731-050209 arm07). `game_engine.py` shipped
with FIVE syntax errors: three inside `GameEngine.process_command`, and two that
WERE the `def` lines of two sibling helpers (`def 8_world_rooms_get`, an
identifier starting with a digit). Every one of 51 repair attempts targeted
process_command — correctly, that is where the diagnosis pointed.

Fourteen of those attempts REPAIRED it: the post-splice error line moved off 75
and on to 140/152/153/155, i.e. the siblings. The gate parsed the WHOLE file,
saw a remaining error, and reverted the splice — so 37 later attempts restarted
from a file an earlier attempt had already partially fixed. Two hours, zero
progress, and the residual symbols were UNNAMEABLE, so no symbol-scoped editor
could ever have reached them.

The guard's real job — "do not let a splice corrupt a WORKING file" — is
unchanged and is the first class of test below. What changes is the
already-broken case: an error OUTSIDE the body just written is pre-existing
damage this edit was never scoped to fix.
"""

from __future__ import annotations

import ast as stdlib_ast


def _parses(src: str) -> bool:
    try:
        stdlib_ast.parse(src)
        return True
    except SyntaxError:
        return False


# The decision the gate now makes, expressed exactly as the code does.
def _keeps_splice(pre: str, post: str, body_start: int, body_end: int) -> bool:
    if _parses(post):
        return True
    try:
        stdlib_ast.parse(post)
    except SyntaxError as e:
        pre_broken = not _parses(pre)
        inside = e.lineno is not None and body_start <= e.lineno <= body_end
        return pre_broken and not inside
    return True


class TestAWorkingFileIsStillProtected:
    """Unchanged behaviour: if it parsed before, a bad splice always reverts."""

    def test_splice_that_breaks_a_clean_file_is_rejected(self):
        pre = "def a():\n    return 1\n\ndef b():\n    return 2\n"
        post = "def a():\n    return 1 +\n\ndef b():\n    return 2\n"  # broke line 2
        assert _keeps_splice(pre, post, body_start=1, body_end=2) is False

    def test_clean_file_clean_splice_is_kept(self):
        pre = "def a():\n    return 1\n"
        post = "def a():\n    return 2\n"
        assert _keeps_splice(pre, post, body_start=1, body_end=2) is True


class TestAnAlreadyBrokenFileKeepsRealProgress:
    """THE GEMMA CASE."""

    # three errors in the edited symbol, two in an unreachable sibling
    PRE = (
        "class E:\n"
        "    def process(self):\n"
        "        x = self.8_get(1)\n"      # line 3 - in scope, broken
        "        return x\n"
        "    def 8_get(self, i): return i\n"  # line 5 - SIBLING, unnameable
    )

    def test_repairing_the_edited_symbol_is_KEPT_though_the_file_still_fails(self):
        post = (
            "class E:\n"
            "    def process(self):\n"
            "        x = self.get(1)\n"    # repaired
            "        return x\n"
            "    def 8_get(self, i): return i\n"  # line 5 still broken, OUT of scope
        )
        assert not _parses(post), "file is still broken overall — that is the point"
        assert _keeps_splice(post=post, pre=self.PRE, body_start=2, body_end=4) is True

    def test_a_splice_that_leaves_its_OWN_body_broken_is_still_rejected(self):
        post = (
            "class E:\n"
            "    def process(self):\n"
            "        x = self.9_get(1)\n"  # still broken, INSIDE the edited body
            "        return x\n"
            "    def 8_get(self, i): return i\n"
        )
        assert _keeps_splice(post=post, pre=self.PRE, body_start=2, body_end=4) is False

    def test_progress_can_accumulate_across_attempts(self):
        """The property the old gate destroyed: attempt N+1 starts from the
        file attempt N improved, instead of from the original."""
        after_first = (
            "class E:\n"
            "    def process(self):\n"
            "        x = self.get(1)\n"
            "        return x\n"
            "    def 8_get(self, i): return i\n"
        )
        assert _keeps_splice(post=after_first, pre=self.PRE, body_start=2, body_end=4)
        # a later module-frame edit can now fix the sibling, and the file parses
        final = after_first.replace("def 8_get", "def get")
        assert _parses(final)


class TestTheGuardIsWiredThatWay:
    def test_source_checks_pre_state_and_body_span(self):
        import inspect

        from agent.actions import ast_actions

        src = inspect.getsource(ast_actions)
        assert "pre_broken" in src
        assert "inside_new_body" in src
        assert "_body_start" in src and "_body_end" in src
