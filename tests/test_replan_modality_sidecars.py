"""replan's workspace scan was paying for image/audio digestion and
throwing the result away.

`scan_workspace` publishes `project_manifest`, and NOTHING in replan read
it — the step's resolver is an unconditional `true -> build_repomap`. The
file list it carries is genuinely redundant with build_repomap's
`repo_file_index`, which is why the step looked droppable.

But action_scan_project is not only a scan: at refinement_actions.py:440
it runs _digest_modality_sidecars, which mutates the manifest with VL/ASR
readings "so the model sees them". Those readings land as manifest VALUES
(manifest[<path>.vltext] = <text>), so they died with the manifest. A
vision- or audio-enabled mission that replanned paid for digestion its
decomposition never saw.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent.formatters import PRE_COMPUTE_FORMATTERS, format_modality_sidecars

ROOT = Path(__file__).resolve().parent.parent
STEPS = ("decompose_directive", "decompose_repair")


@pytest.fixture(scope="module")
def replan():
    return json.loads((ROOT / "flows" / "compiled.json").read_text())["replan"]


class TestTheFormatterCarriesDigestsNotFilenames:
    def test_it_renders_sidecar_readings(self):
        out = format_modality_sidecars(
            {
                "source": {
                    "src/main.py": "def main(): ...",
                    "assets/diagram.png.vltext": "A flowchart: user -> API -> db",
                    "notes/standup.m4a.transcript.txt": "[00:01] ship the parser",
                }
            },
            {},
        )
        assert "A flowchart: user -> API -> db" in out
        assert "[00:01] ship the parser" in out

    def test_it_omits_ordinary_source_files(self):
        """The whole point of a focused formatter — repo_file_index already
        carries the file list authoritatively, at up to 40k chars."""
        out = format_modality_sidecars(
            {"source": {"src/main.py": "def main(): ...", "a.py": "x = 1"}}, {}
        )
        assert out == ""

    def test_it_surfaces_failures_and_over_cap_notes(self):
        """Bracketed keys are how digestion reports trouble; silence there
        would make a skipped digest look like no media at all."""
        out = format_modality_sidecars(
            {
                "source": {
                    "[image sidecars]": "(12 image files present — skipped, limit 6)",
                    "[assets/x.png]": "(image digestion failed: exit 1)",
                }
            },
            {},
        )
        assert "limit 6" in out and "digestion failed" in out

    def test_empty_and_malformed_manifests_render_nothing(self):
        assert format_modality_sidecars({"source": {}}, {}) == ""
        assert format_modality_sidecars({"source": None}, {}) == ""
        assert format_modality_sidecars({"source": ["a", "b"]}, {}) == ""

    def test_it_is_registered(self):
        assert PRE_COMPUTE_FORMATTERS["format_modality_sidecars"]


class TestBothDecompositionStepsAreWired:
    @pytest.mark.parametrize("step", STEPS)
    def test_the_manifest_is_declared(self, replan, step):
        ctx = replan["steps"][step]["context"]
        declared = (ctx.get("required") or []) + (ctx.get("optional") or [])
        assert "project_manifest" in declared

    @pytest.mark.parametrize("step", STEPS)
    def test_the_formatter_runs_in_pre_compute(self, replan, step):
        pcs = replan["steps"][step]["pre_compute"]
        assert any(
            p.get("formatter") == "format_modality_sidecars"
            and p.get("output_key") == "modality_sidecars"
            for p in pcs
        )

    @pytest.mark.parametrize("step", STEPS)
    def test_the_result_reaches_the_prompt(self, replan, step):
        """Declaring it is not enough — the same shape of bug as
        design_gate_feedback, which was declared nowhere and rendered
        nowhere while the gate faithfully published it."""
        keys = replan["steps"][step]["prompt_template"]["context_keys"]
        assert "modality_sidecars" in keys

    @pytest.mark.parametrize(
        "template", ("decompose_directive", "decompose_directive_repair")
    )
    def test_the_template_has_a_conditional_section(self, template):
        import yaml

        doc = yaml.safe_load(
            (ROOT / "prompts" / "replan" / f"{template}.yaml").read_text()
        )
        secs = [s for s in doc["sections"] if s.get("id") == "modality_sidecars"]
        assert secs, f"{template} renders nothing for modality_sidecars"
        assert secs[0]["when"] == "context.modality_sidecars", (
            "must be conditional — a text-only project should not emit an "
            "empty heading"
        )
