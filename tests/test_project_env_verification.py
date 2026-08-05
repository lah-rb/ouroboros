"""project_ops must verify its own work, not report success on exit 0.

"The install commands exited 0" is not "the dependencies are installed". The
empty-venv run reported success on EVERY cycle while nothing was installed, and
that false success propagated into the workspace ledger ("[provision] … —
success") where the next diagnosis read it as settled
(dev/POOLSIDE_TRAP_ROOTCAUSE.md).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent.actions.pipeline_actions import (
    _declared_distributions,
    action_verify_project_env,
)
from agent.effects.mock import MockEffects
from agent.effects.protocol import CommandResult
from agent.models import FlowMeta, StepInput

COMPILED = Path(__file__).resolve().parents[1] / "flows" / "compiled.json"


def _si(effects):
    return StepInput(
        context={},
        params={},
        meta=FlowMeta(flow_name="project_ops", step_id="verify_env", attempt=1),
        effects=effects,
    )


class TestDeclaredDistributions:
    def test_reads_pyproject_dependencies(self):
        toml = '[project]\nname = "x"\ndependencies = ["PyYAML>=6.0", "rich"]\n'
        assert _declared_distributions(toml, "") == ["PyYAML", "rich"]

    def test_strips_specifiers_extras_and_markers(self):
        toml = (
            '[project]\nname = "x"\n'
            "dependencies = [\"requests[socks]>=2 ; python_version>'3.8'\"]\n"
        )
        assert _declared_distributions(toml, "") == ["requests"]

    def test_reads_requirements_and_skips_flags_and_comments(self):
        req = "PyYAML>=6.0\n# a comment\n-r other.txt\n--index-url http://x\n\nrich\n"
        assert _declared_distributions("", req) == ["PyYAML", "rich"]

    def test_deduplicates_across_both_sources(self):
        toml = '[project]\nname = "x"\ndependencies = ["PyYAML"]\n'
        assert _declared_distributions(toml, "PyYAML>=6.0\n") == ["PyYAML"]

    def test_malformed_manifest_is_not_our_error(self):
        assert _declared_distributions("this is not toml {{{", "") == []

    def test_distribution_names_not_module_names(self):
        """Deliberate: PyYAML->yaml, beautifulsoup4->bs4. A manifest declares
        the DISTRIBUTION, and importlib.metadata resolves exactly that, so no
        name-mapping guesswork is needed."""
        toml = '[project]\nname = "x"\ndependencies = ["PyYAML", "beautifulsoup4"]\n'
        assert _declared_distributions(toml, "") == ["PyYAML", "beautifulsoup4"]


class TestVerifyAction:
    @pytest.mark.asyncio
    async def test_nothing_declared_verifies_trivially(self):
        eff = MockEffects(files={})
        out = await action_verify_project_env(_si(eff))
        assert out.result["env_verified"] is True
        assert out.result["missing"] == []

    @pytest.mark.asyncio
    async def test_all_present_verifies(self):
        eff = MockEffects(
            files={"pyproject.toml": '[project]\nname="x"\ndependencies=["PyYAML"]\n'},
            commands={
                "python": CommandResult(
                    return_code=0, stdout="", stderr="", command="python"
                )
            },
        )
        out = await action_verify_project_env(_si(eff))
        assert out.result["env_verified"] is True

    @pytest.mark.asyncio
    async def test_declared_but_absent_fails_verification(self):
        eff = MockEffects(
            files={"pyproject.toml": '[project]\nname="x"\ndependencies=["PyYAML"]\n'},
            commands={
                "python": CommandResult(
                    return_code=0, stdout="PyYAML", stderr="", command="python"
                )
            },
        )
        out = await action_verify_project_env(_si(eff))
        assert out.result["env_verified"] is False
        assert out.result["missing"] == ["PyYAML"]

    @pytest.mark.asyncio
    async def test_a_probe_that_cannot_RUN_is_unverified_not_a_pass(self):
        """A broken interpreter is exactly what this is looking for, so a failed
        probe must never be read as success."""
        eff = MockEffects(
            files={"pyproject.toml": '[project]\nname="x"\ndependencies=["PyYAML"]\n'},
            commands={
                "python": CommandResult(
                    return_code=1, stdout="", stderr="boom", command="python"
                )
            },
        )
        out = await action_verify_project_env(_si(eff))
        assert out.result["env_verified"] is False


class TestWiring:
    @pytest.fixture(scope="class")
    def po(self):
        return json.loads(COMPILED.read_text())["project_ops"]["steps"]

    def test_a_successful_install_is_verified_before_being_believed(self, po):
        rules = po["run_installs"]["resolver"]["rules"]
        assert rules[0]["transition"] == "verify_env"

    def test_failed_verification_escalates_rather_than_reporting_success(self, po):
        assert po["verify_env"]["resolver"]["rules"][-1]["transition"] == "escalate_env"

    def test_escalation_result_is_re_verified_not_taken_on_trust(self, po):
        """'resolved' is the escalation's own claim about its own work — the
        exact class of claim that produced this trap."""
        assert po["escalate_env"]["resolver"]["rules"][0]["transition"] == (
            "verify_env_after_escalation"
        )

    def test_the_escalate_verify_loop_terminates(self, po):
        """A persistently-missing dependency must not cycle
        escalate -> verify -> escalate forever."""
        after = [
            r["transition"]
            for r in po["verify_env_after_escalation"]["resolver"]["rules"]
        ]
        assert "escalate_env" not in after
        assert after[-1] == "build_report_failure"
