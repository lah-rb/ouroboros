"""Multilingual discovery: a mission flag, off by default.

Extra languages are a real cost multiplier — each one is another full pass over
every query — so the corpus that wants them says so, and a corpus that does not
pays nothing. English is never listed: a mission that named it would run the
English pass twice and, downstream, queue English papers for translation into
English.
"""

from __future__ import annotations

import pytest

from agent.actions.refinement_actions import action_extract_search_queries
from agent.actions.scholarly_actions import corpus_languages
from agent.models import FlowMeta, StepInput
from agent.persistence.models import MissionConfig, MissionState


def _mission(langs):
    return MissionState(
        objective="corpus",
        status="active",
        config=MissionConfig(
            working_directory="/tmp/x", flow_set="scraper", corpus_languages=langs
        ),
    )


def _si(mission=None, params=None, context=None):
    return StepInput(
        context={**({"mission": mission} if mission else {}), **(context or {})},
        inputs={},
        params=params or {},
        meta=FlowMeta(flow_name="discover", step_id="t"),
        effects=None,
    )


def test_default_is_english_only():
    assert corpus_languages(_si(_mission([]))) == []
    assert corpus_languages(_si()) == []


def test_languages_come_from_the_mission_flag():
    assert corpus_languages(_si(_mission(["zh", "es"]))) == ["zh", "es"]


@pytest.mark.parametrize("spelling", ["en", "EN", "eng", "English"])
def test_english_is_always_stripped(spelling):
    """Listing English would run that pass twice and ask for English papers to
    be translated into English."""
    assert corpus_languages(_si(_mission([spelling, "de"]))) == ["de"]


def test_codes_are_normalized_and_deduped():
    got = corpus_languages(_si(_mission([" ZH ", "zh", "Pt"])))
    assert got == ["zh", "pt"]


def test_a_string_is_accepted_as_well_as_a_list():
    """Mission YAML and CLI both hand this through as free text."""
    assert corpus_languages(
        _si(_mission([]), params={"corpus_languages": "zh, es"})
    ) == [
        "zh",
        "es",
    ]


def test_params_override_the_mission():
    """So one flow step can scope a pass without changing the mission."""
    si = _si(_mission(["zh"]), params={"corpus_languages": ["fr"]})
    assert corpus_languages(si) == ["fr"]


# ── the cap has to scale, or it defeats the feature ───────────────────


_RESPONSE = """```json
["english one", "english two", "english three", "english four",
 "espectroscopia raman pigmentos", "espectros infrarrojos minerales"]
```"""


@pytest.mark.asyncio
async def test_query_cap_scales_with_languages():
    """Models emit the English queries FIRST, so a fixed cap truncates exactly
    the native-language phrasings the language pass exists to produce — and
    silently, leaving a multilingual mission running English-only queries while
    reporting success."""
    si = _si(
        context={"inference_response": _RESPONSE},
        params={"max_queries": 4, "corpus_languages": ["es"]},
    )
    out = await action_extract_search_queries(si)
    queries = out.context_updates["search_queries"]
    # The cap rises to 8, so all six survive — the point is that the Spanish
    # pair is NOT cut, which a fixed cap of 4 would have done silently.
    assert len(queries) == 6
    assert "espectroscopia raman pigmentos" in queries
    assert "espectros infrarrojos minerales" in queries


@pytest.mark.asyncio
async def test_query_cap_unchanged_without_languages():
    si = _si(
        context={"inference_response": _RESPONSE},
        params={"max_queries": 4},
    )
    out = await action_extract_search_queries(si)
    assert len(out.context_updates["search_queries"]) == 4
