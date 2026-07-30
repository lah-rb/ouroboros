"""`cache_strategy:` — one key for the three deployable shapes.

Standing up a strategy meant getting four interacting flags right across two
config sections, and every way of getting it wrong is SILENT. The 2026-07-30
audit found, in a 20-config fleet: a flow cache enabled with nothing to pin
onto, a head-swap declared on a model that cannot host it, and a stock band
dividing a per-seq window by twelve. Naming the shape once removes the class.

The design decision worth protecting: it does NOT set `swa_full`. That is
architecture-determined — glm (MLA) and hy3 (dense) run resident with it FALSE
and are correct, while gemma and gpt-oss require it TRUE or their pinned
prefixes corrupt at the window boundary. A strategy-level answer would be
wrong for half the fleet.

See dev/caching/FEATURE_MATRIX.md §1.
"""

from __future__ import annotations

import pytest

from core.config import CACHE_STRATEGIES, expand_cache_strategy


class TestExpansion:
    def test_resident_sets_the_pool_shape(self):
        raw = {"cache_strategy": "resident"}
        applied = expand_cache_strategy(raw)
        assert raw["model"]["resident_seq_cache"] is True
        assert raw["model"]["session_full_replay"] is True
        assert raw["model"]["kv_unified"] is True
        assert raw["resources"]["decode_mode"] == "pool"
        assert applied  # reported for the boot log

    def test_batched_sets_batched_decode(self):
        raw = {"cache_strategy": "batched"}
        expand_cache_strategy(raw)
        assert raw["resources"]["decode_mode"] == "batched"
        assert raw["model"]["resident_seq_cache"] is True

    def test_replay_is_the_explicit_fallback_shape(self):
        raw = {"cache_strategy": "replay"}
        expand_cache_strategy(raw)
        assert raw["model"]["resident_seq_cache"] is False
        assert raw["model"]["session_full_replay"] is True
        assert raw["resources"]["decode_mode"] == "pool"

    def test_the_fallback_is_armed_in_every_strategy(self):
        """session_full_replay is the catch for a can_shift denial, so no
        strategy may leave it off — including the resident ones, where it is
        ignored while resident is live and costs nothing."""
        for name in CACHE_STRATEGIES:
            raw = {"cache_strategy": name}
            expand_cache_strategy(raw)
            assert raw["model"]["session_full_replay"] is True, name

    def test_absent_key_changes_nothing(self):
        raw = {"model": {"name": "x"}}
        assert expand_cache_strategy(raw) == []
        assert raw == {"model": {"name": "x"}}

    def test_unknown_strategy_is_refused_by_name(self):
        with pytest.raises(ValueError, match="not one of"):
            expand_cache_strategy({"cache_strategy": "s2"})

    def test_the_key_is_consumed(self):
        """Config has no such field — leaving it in the dict would fail
        construction."""
        raw = {"cache_strategy": "resident"}
        expand_cache_strategy(raw)
        assert "cache_strategy" not in raw


class TestSwaFullIsNotStrategyDetermined:
    """The subtlety that decides the whole design."""

    @pytest.mark.parametrize("name", sorted(CACHE_STRATEGIES))
    def test_no_strategy_touches_swa_full(self, name):
        raw = {"cache_strategy": name}
        expand_cache_strategy(raw)
        assert "swa_full" not in raw["model"], (
            "swa_full is architecture-determined: MLA and dense models run "
            "resident with it FALSE and are correct; SWA/iSWA models require "
            "it TRUE. A strategy-level value would be wrong for half the fleet."
        )

    def test_an_mla_shaped_config_survives_expansion(self):
        """glm: resident + swa_full false. The expansion must not overwrite it."""
        raw = {"cache_strategy": "resident", "model": {"swa_full": False}}
        expand_cache_strategy(raw)
        assert raw["model"]["swa_full"] is False
        assert raw["model"]["resident_seq_cache"] is True


class TestTwoSourcesOfTruthAreRefused:
    def test_a_contradicting_flag_raises(self):
        raw = {"cache_strategy": "resident", "model": {"resident_seq_cache": False}}
        with pytest.raises(ValueError, match="two sources of truth"):
            expand_cache_strategy(raw)

    def test_a_contradicting_decode_mode_raises(self):
        raw = {"cache_strategy": "resident", "resources": {"decode_mode": "batched"}}
        with pytest.raises(ValueError, match="two sources of truth"):
            expand_cache_strategy(raw)

    def test_the_error_names_both_values(self):
        raw = {"cache_strategy": "batched", "resources": {"decode_mode": "pool"}}
        with pytest.raises(ValueError) as e:
            expand_cache_strategy(raw)
        assert "decode_mode=" in str(e.value) and "'pool'" in str(e.value)

    def test_restating_an_agreeing_flag_is_allowed(self):
        """Verbosity is not an error — only disagreement is."""
        raw = {"cache_strategy": "resident", "model": {"resident_seq_cache": True}}
        expand_cache_strategy(raw)
        assert raw["model"]["resident_seq_cache"] is True

    def test_the_report_names_only_what_it_actually_SET(self):
        """The returned list becomes the boot log, and the whole reason that
        line exists is 'a setting you cannot see is a setting you cannot
        debug'. Reporting a flag the config already stated makes the log claim
        authorship it does not have — and would hide which half of a
        half-written config the strategy supplied."""
        raw = {"cache_strategy": "resident", "model": {"resident_seq_cache": True}}
        applied = expand_cache_strategy(raw)
        assert not any("resident_seq_cache" in a for a in applied), applied
        # ...while the ones it really did supply are still reported.
        assert any("kv_unified" in a for a in applied), applied

    def test_unrelated_keys_in_the_same_section_survive(self):
        raw = {
            "cache_strategy": "resident",
            "model": {"name": "m", "n_ctx": 4096},
            "resources": {"cpu_threads": 8},
        }
        expand_cache_strategy(raw)
        assert raw["model"]["name"] == "m"
        assert raw["model"]["n_ctx"] == 4096
        assert raw["resources"]["cpu_threads"] == 8


class TestEndToEnd:
    BASE = """
cache_strategy: %s
app: {host: 0.0.0.0, port: 8008, log_level: info, cors_origins: ['*'], backend_timeout: 180}
model:
  name: t
  family: chatml
  path: /tmp/t.gguf
  n_ctx: 4096
  n_gpu_layers: -1
  seed: -1
  verbose: false
  thinking: false
  swa_full: true
prompt: {system_prompt: hello}
generation: {max_tokens_default: 512, temperature_default: 1.0}
knowledge: {tokens_bin: ./data/t.tokens.bin, token_limit: 1024}
resources: {cpu_threads: 2}
logging: {enabled: true, directory: ./logs}
"""

    def _load(self, tmp_path, monkeypatch, name):
        import core.config as cfgmod
        from core.config import load_config

        p = tmp_path / "t.yaml"
        p.write_text(self.BASE % name)
        monkeypatch.setattr(cfgmod, "CONFIGS_DIR", tmp_path)
        return load_config(p)

    def test_resident_loads_through_the_real_validators(self, tmp_path, monkeypatch):
        c = self._load(tmp_path, monkeypatch, "resident")
        assert c.model.resident_seq_cache is True
        assert c.model.kv_unified is True
        assert c.resources.decode_mode == "pool"

    def test_batched_satisfies_its_own_precondition_validator(self, tmp_path, monkeypatch):
        """decode_mode: batched raises unless resident + swa_full + kv_unified
        are all set. The strategy supplies two of the three; swa_full is in the
        fixture because it is the model's business."""
        c = self._load(tmp_path, monkeypatch, "batched")
        assert c.resources.decode_mode == "batched"
        assert c.model.resident_seq_cache is True

    def test_replay_loads(self, tmp_path, monkeypatch):
        c = self._load(tmp_path, monkeypatch, "replay")
        assert c.model.resident_seq_cache is False
        assert c.model.session_full_replay is True
