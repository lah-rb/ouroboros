"""The dependency claim is checkable where it is made.

THE FAILURE: `plan_setup` emits config-file BODIES as fences — correct, since
TOML inside JSON means escaped newlines and a second parse — but the same step
also decides DEPENDENCIES, and that is structured data smuggled inside a file
it happens to write. One run emitted `requires = []` alongside the claim
"PyYAML is stdlib". Both halves were file content, so nothing could validate
either.

`yaml` is not in `sys.stdlib_module_names`. That makes the claim a fact rather
than an opinion, so the check costs no inference call.

Split by KIND, not wholesale: bodies stay fences, the claim gets cross-checked
against the code. ADVISORY — import-name to distribution-name is genuinely
ambiguous (`import yaml` <- PyYAML, `import bs4` <- beautifulsoup4), so a
mismatch is reported, never used to fail a phase.
"""

from __future__ import annotations

import pytest

from agent.actions.pipeline_actions import _declared_names, _undeclared_imports


class TestTheOriginalFalsehood:
    def test_pyyaml_is_stdlib_is_caught(self):
        """The exact shape that shipped: an empty requires list plus a claim
        that a third-party module is stdlib."""
        sources = {"main.py": "import yaml\nimport os\n\nprint(yaml, os)\n"}
        manifest = '[project]\nname = "game"\nrequires = []\n'
        assert _undeclared_imports(sources, manifest, set()) == ["yaml"]

    def test_declaring_it_clears_the_finding(self):
        sources = {"main.py": "import yaml\n"}
        manifest = '[project]\ndependencies = ["PyYAML>=6"]\n'
        assert _undeclared_imports(sources, manifest, set()) == []


class TestNoFalseAlarms:
    """Every finding costs an operator a note; the bar is real coverage."""

    def test_stdlib_never_reported(self):
        sources = {
            "a.py": "import os, sys, json, sqlite3, dataclasses\nfrom pathlib import Path\n"
        }
        assert _undeclared_imports(sources, "[project]\n", set()) == []

    def test_local_modules_never_reported(self):
        sources = {
            "engine.py": "from models import Room\nimport parser\n",
            "models.py": "class Room: pass\n",
            "parser.py": "def parse(): pass\n",
        }
        local = {"engine", "models", "parser"}
        assert _undeclared_imports(sources, "[project]\n", local) == []

    def test_relative_imports_never_reported(self):
        sources = {"pkg/a.py": "from . import sibling\nfrom .deep import thing\n"}
        assert _undeclared_imports(sources, "[project]\n", set()) == []

    @pytest.mark.parametrize(
        "imported,declared",
        [
            ("yaml", "PyYAML"),
            ("dateutil", "python-dateutil"),
            ("dotenv", "python_dotenv"),
            ("requests", "requests==2.31.0"),
        ],
    )
    def test_distribution_name_variants_resolve(self, imported, declared):
        """Normalized substring match in EITHER direction — the naming gap is
        real and a false alarm here is noise on every run."""
        sources = {"a.py": f"import {imported}\n"}
        assert (
            _undeclared_imports(sources, f"dependencies = ['{declared}']", set()) == []
        )

    def test_syntax_error_file_is_skipped(self):
        sources = {"bad.py": "def (:\n", "ok.py": "import yaml\n"}
        assert _undeclared_imports(sources, "[project]\n", set()) == ["yaml"]

    def test_non_python_sources_ignored(self):
        sources = {"app.js": "import yaml from 'yaml'\n"}
        assert _undeclared_imports(sources, "[project]\n", set()) == []


class TestDeclaredNames:
    def test_normalizes_and_splits(self):
        got = _declared_names('dependencies = ["PyYAML>=6.0", "python-dateutil"]')
        assert "pyyaml" in got
        assert "pythondateutil" in got

    def test_empty_manifest_declares_nothing(self):
        assert _declared_names("") == set()


# ── the three recorded project_ops defects ────────────────────────────


class TestWriteFilesParseFailure:
    """A fence-parse failure used to be indistinguishable from success: the
    resolver was `{condition: "true"}` and both cases leave files_written at 0.
    """

    def test_action_marks_a_parse_failure(self):
        import inspect

        from agent.actions import file_ops_actions

        src = inspect.getsource(file_ops_actions.action_apply_multi_file_changes)
        assert '"parse_failed": True' in src
        assert '"parse_failed": False' in src, (
            "the success path must set it too, or a resolver testing == true "
            "reads a missing key on the happy path"
        )

    def test_flow_routes_parse_failure_to_failure(self):
        import json
        from pathlib import Path

        compiled = json.loads(Path("flows/compiled.json").read_text())
        rules = compiled["project_ops"]["steps"]["write_files"]["resolver"]["rules"]
        assert rules[0]["condition"] == "result.parse_failed == true"
        assert rules[0]["transition"] == "build_report_failure"

    def test_zero_written_alone_does_not_fail_the_phase(self):
        """protect_existing: true means a legitimate re-run writes zero files;
        routing on the count would fail the phase for doing nothing wrong."""
        import json
        from pathlib import Path

        compiled = json.loads(Path("flows/compiled.json").read_text())
        rules = compiled["project_ops"]["steps"]["write_files"]["resolver"]["rules"]
        assert not any("files_written" in r.get("condition", "") for r in rules)


def test_phantom_setup_complete_return_is_gone():
    """`returns.setup_complete` read context.setup_result, whose only publisher
    was the deleted run_setup_commands step — so it could never be present."""
    import json
    from pathlib import Path

    compiled = json.loads(Path("flows/compiled.json").read_text())
    assert "setup_complete" not in (compiled["project_ops"].get("returns") or {})


def test_test_install_command_is_no_longer_forbidden_by_its_own_schema():
    """collect_test_installs reads the field and detect_tooling_rules asks for
    it at length, but LanguageCommands set additionalProperties: false — so a
    schema-conforming response could never contain it and the step always fell
    through."""
    import json
    from pathlib import Path

    schema = json.loads(Path("schemas/validation_env_config.json").read_text())
    lang = schema["$defs"]["LanguageCommands"]
    assert lang["additionalProperties"] is False, "the closed shape is the point"
    assert "test_install_command" in lang["properties"]


def test_the_new_step_is_registered_and_reachable():
    import json
    from pathlib import Path

    from agent.actions.registry import build_action_registry

    assert build_action_registry().get("check_declared_dependencies") is not None
    compiled = json.loads(Path("flows/compiled.json").read_text())
    steps = compiled["project_ops"]["steps"]
    assert steps["check_declared_deps"]["action"] == "check_declared_dependencies"
    assert (
        steps["write_files"]["resolver"]["rules"][-1]["transition"]
        == "check_declared_deps"
    )
