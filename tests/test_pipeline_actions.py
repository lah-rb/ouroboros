"""project_ops' dependency claim: it repairs the manifest, it does not merely
note it.

The check that finds an undeclared import was ADVISORY for good reason —
import-name to distribution-name is genuinely ambiguous. That reasoning was
sound about the mapping and wrong about the cost: measured 2026-08-14, the note
it wrote had no consumer anywhere in agent/ or flows/, while the install step
downstream derives its command from the very manifest the note says is wrong.
"""

from __future__ import annotations

import pytest

# ── the dependency claim repairs itself ───────────────────────────────


@pytest.mark.asyncio
async def test_undeclared_dependency_is_declared_not_just_noted():
    """Measured 2026-08-14: an arm imported `yaml` with no dependencies block.
    This check fired correctly and said exactly what was wrong — and nothing
    read it. `undeclared_dependencies` had no consumer anywhere, and the
    install step derives its command FROM the manifest, so it installed
    nothing. The model then diagnosed the true cause twice ("Declare PyYAML as
    a project dependency"), was unheard, and spent nine cycles moving the
    import between files until the wall."""
    from agent.actions.pipeline_actions import _declare_dependencies

    toml = '[project]\nname = "g"\nversion = "0.1.0"\n'
    written = {}

    class FX:
        async def read_file(self, p):
            class R:
                exists = True
                content = toml

            return R()

        async def write_file(self, p, c):
            written[p] = c

    got = await _declare_dependencies(FX(), ["pyproject.toml", "main.py"], ["yaml"])
    assert got == ["PyYAML"], "import name must map to the distribution name"
    assert 'dependencies = [\n    "PyYAML",\n]' in written["pyproject.toml"]
    assert 'name = "g"' in written["pyproject.toml"], "must not clobber the manifest"


@pytest.mark.asyncio
async def test_existing_dependencies_key_is_never_overwritten():
    """A populated dependencies list is a STATEMENT. Rewriting it needs a TOML
    parse this action deliberately does not do, and silently replacing one is
    how a correct manifest gets clobbered."""
    from agent.actions.pipeline_actions import _declare_dependencies

    class FX:
        async def read_file(self, p):
            class R:
                exists = True
                content = '[project]\nname = "g"\ndependencies = ["rich"]\n'

            return R()

        async def write_file(self, p, c):  # pragma: no cover - must not run
            raise AssertionError("must not write over an existing dependencies key")

    assert await _declare_dependencies(FX(), ["pyproject.toml"], ["yaml"]) == []


@pytest.mark.asyncio
async def test_unknown_import_falls_back_to_its_own_name():
    """The import->distribution map is genuinely ambiguous, which is why this
    was advisory for so long. The fallback is right more often than not, and a
    wrong guess fails LOUDLY at `uv pip install` in the very next step — where
    silence failed quietly and forever."""
    from agent.actions.pipeline_actions import _declare_dependencies

    written = {}

    class FX:
        async def read_file(self, p):
            class R:
                exists = True
                content = '[project]\nname = "g"\n'

            return R()

        async def write_file(self, p, c):
            written[p] = c

    got = await _declare_dependencies(FX(), ["pyproject.toml"], ["httpx"])
    assert got == ["httpx"]
    assert '"httpx"' in written["pyproject.toml"]


@pytest.mark.asyncio
async def test_no_pyproject_stays_advisory():
    from agent.actions.pipeline_actions import _declare_dependencies

    class FX:
        async def read_file(self, p):  # pragma: no cover
            raise AssertionError("nothing to read")

    assert await _declare_dependencies(FX(), ["main.py"], ["yaml"]) == []
