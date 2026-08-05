"""reference.yaml must document every schema field.

README.LLM.md makes the pydantic schema the source of truth and requires
config.py changes to be mirrored here — but nothing enforced it, and the
2026-07-30 audit found **22 of 122 fields undocumented**, including the whole
memory-budget group (`kv_preflight_gb`, `model_max_context`,
`probe_verified_n_ctx`, `probe_verified_weights_bytes`,
`kv_bytes_per_token_measured`) that the context probe writes back and that the
new-model procedure depends on.

A reference that silently drifts is worse than no reference: it reads as
complete.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from pydantic import BaseModel

import core.config as C

REFERENCE = Path(__file__).resolve().parents[1] / "configs" / "reference.yaml"


def _schema_fields() -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for name in dir(C):
        obj = getattr(C, name)
        if (
            isinstance(obj, type)
            and issubclass(obj, BaseModel)
            and obj is not BaseModel
        ):
            for field in obj.model_fields:
                out.append((obj.__name__, field))
    return sorted(set(out))


@pytest.mark.parametrize("model_name,field", _schema_fields(), ids=lambda v: str(v))
def test_every_schema_field_is_documented(model_name, field):
    text = REFERENCE.read_text()
    assert re.search(rf"(^|[\s#]){re.escape(field)}\s*:", text, re.M), (
        f"{model_name}.{field} is missing from configs/reference.yaml — "
        "README.LLM.md requires config.py changes to be mirrored there"
    )


def test_reference_still_parses_as_yaml():
    import yaml

    assert isinstance(yaml.safe_load(REFERENCE.read_text()), dict)


class TestTheNewModelGuide:
    """The procedure is the part that prevents repeat failures, so pin that it
    is present and keeps the steps whose omission already cost us something."""

    def _text(self) -> str:
        return REFERENCE.read_text()

    def test_the_guide_exists(self):
        assert "SETTING UP A NEW MODEL" in self._text()

    def test_it_precedes_the_field_reference(self):
        """It is the first thing a new-model author needs; below the fields it
        would not be read."""
        t = self._text()
        assert t.index("SETTING UP A NEW MODEL") < t.index("\nmodel:")

    @pytest.mark.parametrize(
        "step,why",
        [
            (
                "gguf_geometry.py",
                "geometry before allocation — three reboots were precomputable",
            ),
            (
                "WEB-SEARCH THE MODEL CARD",
                "glm's first arm ran a sampling profile from nowhere",
            ),
            ("fsm_labeller", "olmo shipped unmapped and corrupted a judged artifact"),
            (
                "cache_strategy",
                "the strategy decides which features are even available",
            ),
            ("memory_can_shift", "resident is a request the arch can refuse"),
            ("--probe-context", "the ceiling must be measured, not guessed"),
            ("FEATURE_MATRIX", "feature selection is strategy-dependent"),
        ],
    )
    def test_each_hard_won_step_is_present(self, step, why):
        assert step in self._text(), f"missing step: {step} — {why}"

    def test_it_warns_that_swa_full_is_not_strategy_determined(self):
        t = self._text()
        assert "architecture-determined" in t or "architecture determined" in t
