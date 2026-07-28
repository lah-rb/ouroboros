"""Config layout + `extends:` inheritance.

WHY THIS SHAPE. 37 configs lived in one flat directory, most of them one-question
variants of a handful of base models, each a full ~120-line copy. The split is
root (one servable config per model) / boss (remote provider entries) /
experiments (variants), and a variant now carries only its DIFF.

The dangerous part is not the directories, it is the merge, because this schema
encodes meaning in ``None`` and the meaning is not uniform:

    temperature_floor: null        -> disabled
    repetition_guard_enabled: null -> ENABLED
    rope/yarn fields: null         -> not passed to llama.cpp at all
    kv_preflight_gb: null          -> 100

So a value-based merge could never let a child take a field BACK to its default,
and what it got instead would differ per field. Key PRESENCE is therefore the
override signal, which makes ``null`` mean "reset to this field's default"
everywhere and uniformly.

The second trap is dict-valued FIELDS. ``laguna-s-2.1-apex`` bans token 19
(``</think>``) via ``generation.logit_bias`` because thinking is off there; a
thinking variant that inherited that mapping key-by-key would silently ban the
token it depends on. Sections merge, values replace.
"""

from __future__ import annotations

import textwrap

import pytest
import yaml

from core.config import (
    Config,
    _load_raw_with_inheritance,
    _merge_over,
    resolve_config_path,
    section_fields,
)

BASE = """
app: {host: 0.0.0.0, port: 8008, log_level: info, cors_origins: ['*'], backend_timeout: 180}
model:
  name: base-model
  family: chatml
  path: /tmp/base.gguf
  n_ctx: 65536
  n_gpu_layers: -1
  seed: -1
  verbose: false
  thinking: false
  context_refresh_drain_s: 300
prompt: {system_prompt: hello}
generation:
  max_tokens_default: 4096
  temperature_default: 1.0
  logit_bias: {19: -100.0, 25: -100.0}
knowledge: {tokens_bin: ./data/base.tokens.bin, token_limit: 32768}
resources: {decode_mode: pool, cpu_threads: 8}
logging: {enabled: true, directory: ./logs}
"""


@pytest.fixture
def configs(tmp_path, monkeypatch):
    """A catalog rooted at tmp_path with root/, boss/ and experiments/."""
    import core.config as cfg

    root = tmp_path / "configs"
    for d in ("", "boss", "experiments"):
        (root / d).mkdir(parents=True, exist_ok=True)
    (root / "base-model.yaml").write_text(BASE)
    monkeypatch.setattr(cfg, "CONFIGS_DIR", root)
    return root


def write(path, body: str):
    path.write_text(textwrap.dedent(body))


class TestResolution:
    def test_finds_configs_in_every_search_dir(self, configs):
        write(configs / "boss" / "b.yaml", "provider: claude_cli\nmodel: x\n")
        write(configs / "experiments" / "e.yaml", "extends: base-model\n")
        assert resolve_config_path("base-model").parent == configs
        assert resolve_config_path("b").parent.name == "boss"
        assert resolve_config_path("e").parent.name == "experiments"

    def test_archive_is_never_resolved(self, configs):
        """Retiring a config must mean it stops answering to its name —
        otherwise archive/ is just another namespace."""
        (configs / "archive").mkdir()
        write(configs / "archive" / "old.yaml", "model: {}\n")
        assert resolve_config_path("old") is None

    def test_root_shadows_a_variant_of_the_same_name(self, configs):
        write(configs / "experiments" / "base-model.yaml", "extends: base-model\n")
        assert resolve_config_path("base-model").parent == configs

    def test_a_name_in_two_subdirs_is_an_error_not_a_guess(self, configs):
        """boss/ and experiments/ mean different things. Silently picking one
        is how a run measures a config nobody chose."""
        write(configs / "boss" / "dup.yaml", "provider: claude_cli\nmodel: x\n")
        write(configs / "experiments" / "dup.yaml", "extends: base-model\n")
        with pytest.raises(ValueError, match="ambiguous"):
            resolve_config_path("dup")

    def test_an_unknown_name_resolves_to_nothing(self, configs):
        assert resolve_config_path("nope") is None


class TestMergeSemantics:
    def _resolved(self, configs, body: str) -> dict:
        p = configs / "experiments" / "child.yaml"
        write(p, body)
        raw, base, overridden = _load_raw_with_inheritance(p, configs)
        return raw

    def test_an_omitted_key_is_inherited(self, configs):
        raw = self._resolved(configs, "extends: base-model\nmodel: {name: child}\n")
        assert raw["model"]["n_ctx"] == 65536
        assert raw["model"]["name"] == "child"

    def test_an_explicit_null_resets_to_the_field_default(self, configs):
        """The escape hatch. Under a value-based merge this is indistinguishable
        from 'not mentioned', and a child could never undo a base's setting."""
        raw = self._resolved(
            configs, "extends: base-model\nmodel: {context_refresh_drain_s: null}\n"
        )
        assert raw["model"]["context_refresh_drain_s"] is None

    def test_a_falsy_override_is_honoured(self, configs):
        """0 and false are values, not absence."""
        raw = self._resolved(configs, "extends: base-model\nmodel: {seed: 0}\n")
        assert raw["model"]["seed"] == 0

    def test_a_value_dict_REPLACES_rather_than_merging(self, configs):
        """The logit_bias trap, and the reason this file exists. The base bans
        19 (</think>) because thinking is off; a thinking child that inherited
        the mapping per-key would ban the token it needs."""
        raw = self._resolved(
            configs,
            "extends: base-model\nmodel: {thinking: true}\ngeneration: {logit_bias: {25: -100.0}}\n",
        )
        assert raw["generation"]["logit_bias"] == {25: -100.0}
        assert 19 not in raw["generation"]["logit_bias"]

    def test_a_section_merges_key_by_key(self, configs):
        """Sections are the one thing that DOES merge — otherwise every child
        would restate the whole model block and we are back to copies."""
        raw = self._resolved(configs, "extends: base-model\ngeneration: {temperature_default: 0.4}\n")
        assert raw["generation"]["temperature_default"] == 0.4
        assert raw["generation"]["max_tokens_default"] == 4096

    def test_a_list_replaces(self, configs):
        raw = self._resolved(configs, "extends: base-model\napp: {cors_origins: ['a']}\n")
        assert raw["app"]["cors_origins"] == ["a"]

    def test_the_overridden_paths_are_reported(self, configs):
        """Boot logs these. A config used to be self-contained and greppable;
        inheritance trades that away, and the resolved diff is what buys it
        back — an unlogged override is an undebuggable one."""
        p = configs / "experiments" / "c.yaml"
        write(p, "extends: base-model\nmodel: {name: kid, n_ctx: 4096}\n")
        _raw, base, overridden = _load_raw_with_inheritance(p, configs)
        assert base == "base-model"
        assert set(overridden) == {"model.name", "model.n_ctx"}


class TestGuardrails:
    def test_inheritance_is_one_level_only(self, configs):
        """A chain means you cannot answer 'what is this config' without
        walking a graph — the exact property the flat directory had."""
        write(configs / "experiments" / "mid.yaml", "extends: base-model\n")
        write(configs / "experiments" / "leaf.yaml", "extends: mid\n")
        with pytest.raises(ValueError, match="one level only"):
            _load_raw_with_inheritance(configs / "experiments" / "leaf.yaml", configs)

    def test_extending_something_unresolvable_fails_loudly(self, configs):
        write(configs / "experiments" / "x.yaml", "extends: ghost\n")
        with pytest.raises(FileNotFoundError, match="ghost"):
            _load_raw_with_inheritance(configs / "experiments" / "x.yaml", configs)

    def test_a_config_without_extends_is_untouched(self, configs):
        """The safety property for every root config: inheritance must be
        strictly opt-in."""
        raw, base, overridden = _load_raw_with_inheritance(
            configs / "base-model.yaml", configs
        )
        assert base is None and overridden == []
        assert raw == yaml.safe_load(BASE)


class TestSectionDetection:
    def test_nested_models_are_sections(self):
        s = section_fields(Config)
        assert "model" in s and "generation" in s

    def test_a_mapping_of_models_is_a_value_not_a_section(self):
        """Dict[str, PersonaConfig]. Merging per-key would let a child ADD a
        persona but never remove one; 'declare the set you want' is the more
        predictable rule and matches how lists behave."""
        assert "personas" not in section_fields(Config)

    def test_merging_without_a_model_treats_everything_as_a_value(self):
        merged, _ = _merge_over({"a": {"x": 1}}, {"a": {"y": 2}}, None)
        assert merged["a"] == {"y": 2}


class TestDocOnlyKeys:
    def test_results_and_notes_do_not_reach_validation(self, configs):
        """Config is extra='forbid', which is right. The experiment's question
        and what it answered still belong WITH the experiment, so they are
        stripped explicitly rather than by loosening the model."""
        from core.config import DOC_ONLY_KEYS, load_config

        p = configs / "experiments" / "documented.yaml"
        write(
            p,
            """
            extends: base-model
            results: |
              3 degenerations, 0 after the change.
            notes: superseded by the balanced arm
            model: {name: documented}
            """,
        )
        assert "results" in DOC_ONLY_KEYS and "notes" in DOC_ONLY_KEYS
        cfg = load_config(p)
        assert cfg.model.name == "documented"


class TestTheDiffIsActuallyVisible:
    """load_config runs at MODULE IMPORT, before the server calls
    logging.basicConfig — so a log call inside it reaches a handlerless logger
    at WARNING and vanishes. The first live boot on an inherited config found
    exactly that: the config resolved correctly and the run aborted anyway,
    because the evidence it resolved was nowhere in the server log.

    Recording the resolution and re-emitting it from the startup path is what
    makes the mitigation real, so it needs a test that does not depend on log
    capture at import time."""

    def test_an_inherited_config_reports_its_base_and_overrides(self, configs):
        from core.config import describe_resolution, load_config

        p = configs / "experiments" / "kid.yaml"
        write(p, "extends: base-model\nmodel: {name: kid, n_ctx: 4096}\n")
        load_config(p)
        line = describe_resolution()
        assert "kid extends base-model" in line
        assert "model.name" in line and "model.n_ctx" in line

    def test_a_self_contained_config_reports_nothing(self, configs):
        """Silence is correct here — a root config has nothing to disclose, and
        a line saying so on every boot is noise."""
        from core.config import describe_resolution, load_config

        load_config(configs / "base-model.yaml")
        assert describe_resolution() is None
