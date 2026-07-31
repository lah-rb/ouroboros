"""The server's cache/feature register, folded into the run's trace.

THE GAP (2026-07-30): flow_builds/hits/evicts/fallbacks, the strategy triple,
and context_refreshes lived ONLY in health and were never persisted. A finished
run therefore could not answer the questions it was most useful for — did the
flow cache actually fire, which substrate did this model land on, and did a
context refresh wipe the caches mid-run. The agent already queried health for
the watchdog and threw the answer away.

Two design rules are load-bearing and tested here:
  * an UNREPORTED register records NOTHING, never a zero — a zero is
    indistinguishable from "the flow cache never fired", which is the exact
    question this exists to answer
  * the summary reports the DELTA, because flowHits is cumulative over the
    server's lifetime and its absolute value says nothing about one run
"""

from __future__ import annotations

import pytest

from agent.trace import finalize_ledger, fold_event, new_ledger


def _ledger_with(*snaps: dict) -> dict:
    led = new_ledger()
    for s in snaps:
        fold_event(led, {"event_type": "health_sample", "health": s})
    return led


class TestTheDeltaIsThePoint:
    def test_counters_report_last_minus_first(self):
        led = _ledger_with(
            {"flowHits": 10, "flowBuilds": 2}, {"flowHits": 47, "flowBuilds": 9}
        )
        srv = finalize_ledger(led, 1000.0)["server"]
        assert srv["delta"]["flowHits"] == 37
        assert srv["delta"]["flowBuilds"] == 7

    def test_a_flag_that_is_on_but_never_fires_is_visible(self):
        """The finding this was built for: the band is allocated and unused."""
        led = _ledger_with({"flowHits": 5}, {"flowHits": 5})
        assert finalize_ledger(led, 1000.0)["server"]["delta"]["flowHits"] == 0

    def test_a_mid_run_refresh_is_flagged_as_a_comparability_warning(self):
        """A refresh wipes the flow band and demotes hot snapshots, so a run
        that took one is not measuring the same machine as one that did not."""
        led = _ledger_with({"contextRefreshes": 0}, {"contextRefreshes": 2})
        srv = finalize_ledger(led, 1000.0)["server"]
        assert srv["cache_wiped_mid_run"] is True

    def test_no_refresh_is_not_flagged(self):
        led = _ledger_with({"contextRefreshes": 3}, {"contextRefreshes": 3})
        assert finalize_ledger(led, 1000.0)["server"]["cache_wiped_mid_run"] is False


class TestUnsampledIsNotZero:
    def test_an_unsampled_run_reports_none(self):
        assert finalize_ledger(new_ledger(), 1000.0)["server"] is None

    def test_an_empty_snapshot_is_ignored_entirely(self):
        """cache_health() returns {} when the server does not report — that
        must not create a first/last pair of zeros."""
        led = _ledger_with({}, {})
        assert finalize_ledger(led, 1000.0)["server"] is None

    def test_one_sample_yields_the_triple_but_NO_delta(self):
        """A single sample makes first and last the same snapshot, so every
        counter differences to 0 — which reads as "the flow cache never fired"
        when the truth is "we never measured twice". The strategy triple is a
        constant and survives one sample; the delta must be withheld."""
        led = _ledger_with({"flowHits": 12, "sessionStrategy": "resident"})
        srv = finalize_ledger(led, 1000.0)["server"]
        assert srv["strategy"] == "resident"
        assert srv["delta"] is None, "a 1-sample delta would be a fabrication"
        assert srv["cache_wiped_mid_run"] is None
        assert srv["samples"] == 1


class TestTheStrategyTriple:
    def test_strategy_is_taken_from_health_not_config(self):
        """OPEN_TASKS §12: resident is a REQUEST the architecture can refuse,
        so the substrate a run actually used is knowable only from health."""
        led = _ledger_with(
            {
                "sessionStrategy": "full_replay",
                "sessionCanShift": False,
                "residentRequested": True,
                "flowHits": 0,
            },
            {
                "sessionStrategy": "full_replay",
                "sessionCanShift": False,
                "residentRequested": True,
                "flowHits": 0,
            },
        )
        srv = finalize_ledger(led, 1000.0)["server"]
        assert srv["strategy"] == "full_replay"
        assert srv["can_shift"] is False
        assert (
            srv["resident_requested"] is True
        ), "requested-and-refused must stay distinguishable from never-requested"

    def test_late_sample_wins_for_gauges(self):
        led = _ledger_with(
            {"nCtxSeq": 1024, "flowHits": 0}, {"nCtxSeq": 4096, "flowHits": 0}
        )
        assert finalize_ledger(led, 1000.0)["server"]["n_ctx_seq"] == 4096


class TestSamplingCadence:
    @pytest.mark.parametrize(
        "call,expected",
        [(1, True), (2, False), (19, False), (20, True), (21, False), (40, True)],
    )
    def test_first_call_then_every_nth(self, call, expected):
        from agent.effects.local import _HEALTH_SAMPLE_EVERY

        fires = call == 1 or call % _HEALTH_SAMPLE_EVERY == 0
        assert fires is expected

    def test_the_sampler_never_raises(self):
        """A telemetry read must not be able to fail a run."""
        import inspect

        from agent.effects.local import LocalEffects

        src = inspect.getsource(LocalEffects._maybe_sample_server_health)
        assert "except Exception" in src
        assert "telemetry never breaks a run" in src

    def test_cache_health_swallows_a_bad_server(self):
        import inspect

        from agent.effects.inference import InferenceEffect

        src = inspect.getsource(InferenceEffect.cache_health)
        assert "return {}" in src
        assert "except Exception" in src


class TestTheEmissionPathItself:
    """THE TEST THAT WAS MISSING, and its absence cost a sweep.

    The first version of this module only fed `fold_event` a hand-built dict
    — `{"event_type": "health_sample", "health": {...}}` — which passes
    happily while the real emit site is broken. It WAS broken: it constructed
    `TraceEvent(..., payload={...})`, and TraceEvent has no `payload` field,
    so every call raised TypeError into a broad `except Exception` and the
    register was silently absent from every trace. Fixture and production
    disagreed, and only the fixture was tested.

    So: build the event the way the emit site builds it, and require that a
    real fold of a real serialized event reaches the ledger.
    """

    def test_healthsample_constructs_and_carries_the_register(self):
        from agent.trace import HealthSample

        ev = HealthSample(mission_id="m1", health={"flowHits": 3})
        assert ev.event_type == "health_sample"
        assert ev.health == {"flowHits": 3}

    def test_a_serialized_event_folds_into_the_ledger(self):
        """to_dict() is what the trace writer persists — fold what it emits,
        not what the test imagines it emits."""
        from agent.trace import HealthSample

        led = new_ledger()
        for hits in (4, 11):
            fold_event(led, HealthSample(health={"flowHits": hits}).to_dict())
        assert finalize_ledger(led, 1000.0)["server"]["delta"]["flowHits"] == 7

    def test_the_emit_site_uses_the_dataclass_not_a_payload_kwarg(self):
        import inspect

        from agent.effects.local import LocalEffects

        src = inspect.getsource(LocalEffects._maybe_sample_server_health)
        assert "HealthSample(" in src
        assert "payload=" not in src, (
            "TraceEvent has no payload field — that kwarg raises TypeError and "
            "the handler below swallows it"
        )

    def test_a_sampling_failure_is_LOUD(self):
        """It logged at debug, so a construction bug was invisible for a whole
        sweep. Telemetry must not break a run; it must also not fail quietly."""
        import inspect

        from agent.effects.local import LocalEffects

        src = inspect.getsource(LocalEffects._maybe_sample_server_health)
        assert "logger.warning" in src
        assert "exc_info=True" in src
