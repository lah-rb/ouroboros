"""The turn record carries the temperature LLMVP actually sampled at.

LLMVP owns the model's parameters: the agent asks for a temperature (often a
relative one, "t*0.1"), and the server may floor it, deepen it on a long
session, or re-drive a degenerate turn at the recovery recipe's temperature.
The history store recorded `float(cfg["temperature"])`, which failed on every
relative spec and wrote 0.0 — for all 54 derive_acceptance turns and 1,179
tester turns of tier_20260924-191710, while the server floored them at 0.7 —
and `history replay` re-issued turns at that 0.0.

Now the response carries the served temperature, the row records it (falling
back to what the client sent), the request is kept verbatim in
`temperature_requested`, and replay re-asks the original request.
"""

from __future__ import annotations

import os

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from agent.effects.inference import InferenceEffect
from agent.effects.protocol import InferenceResult
from agent.history import reader
from agent.history.replay import _config
from agent.history.store import HistoryStore
from agent.trace import InferenceCall, build_inference_call, step_context

_OLD_SERVER = (
    "GraphQL errors: Cannot query field 'temperature' on type 'CompletionResponse'."
)


# ── the client reads and asks for it ─────────────────────────────────


class _Resp:
    def __init__(self, data):
        self._data = data

    def raise_for_status(self):
        return None

    def json(self):
        return self._data


class _Client:
    def __init__(self, completion: dict):
        self.completion = completion
        self.bodies: list[dict] = []

    async def post(self, url, json=None, **kw):
        self.bodies.append(json)
        return _Resp({"data": {"completion": self.completion}})


@pytest.mark.asyncio
async def test_the_parser_reads_the_served_temperature():
    eff = InferenceEffect(endpoint="http://unused")
    client = _Client({"text": "ok", "tokensGenerated": 1, "temperature": 0.7})
    result = await eff._request_once_with_watchdog(client, {"query": "q"})
    assert result.text == "ok" and result.temperature == 0.7


@pytest.mark.asyncio
async def test_a_completion_asks_for_it_and_records_what_it_sent():
    eff = InferenceEffect(endpoint="http://unused", model_default_temperature=0.7)
    bodies: list[dict] = []

    async def fake(client, body, **kw):
        bodies.append(body)
        return InferenceResult(text="ok", tokens_generated=1, temperature=0.7)

    async def fake_client():
        return object()

    eff._request_with_health_watchdog = fake
    eff._get_client = fake_client
    result = await eff.run_inference("hi", config_overrides={"temperature": "t*0.1"})
    assert "temperature" in bodies[0]["query"]
    assert result.temperature == 0.7
    assert result.temperature_sent == pytest.approx(0.07)


def _old_server(bodies: list[str], *, knows_turn_fields: bool = True):
    async def fake(client, body, **kw):
        bodies.append(body["query"])
        if "        temperature" in body["query"]:
            return InferenceResult(text="", tokens_generated=0, error=_OLD_SERVER)
        if not knows_turn_fields and "sessionTurnId" in body["query"]:
            return InferenceResult(
                text="",
                tokens_generated=0,
                error="GraphQL errors: Cannot query field 'sessionTurnId' on type",
            )
        return InferenceResult(text="ok", tokens_generated=1)

    return fake


async def _no_client():
    return object()


@pytest.mark.asyncio
async def test_an_older_server_is_asked_once_then_never_again():
    eff = InferenceEffect(endpoint="http://unused")
    bodies: list[str] = []
    eff._request_with_health_watchdog = _old_server(bodies)
    eff._get_client = _no_client
    assert (await eff.run_inference("a")).text == "ok"
    assert (await eff.run_inference("b")).text == "ok"
    assert eff._temperature_field_supported is False
    assert ["        temperature" in q for q in bodies] == [True, False, False]


@pytest.mark.asyncio
async def test_a_session_drops_only_the_field_the_server_lacks():
    """A server that knows turn ids but not temperature (the one serving the
    09-24 run) must keep turn ids — rewind depends on them."""
    eff = InferenceEffect(endpoint="http://unused")
    bodies: list[str] = []
    eff._request_with_health_watchdog = _old_server(bodies)
    eff._get_client = _no_client
    assert (await eff.session_turn("s1", "p")).text == "ok"
    assert eff._temperature_field_supported is False
    assert eff._session_turn_fields_supported is not False
    assert "sessionTurnId" in bodies[-1] and "        temperature" not in bodies[-1]


@pytest.mark.asyncio
async def test_a_server_lacking_both_loses_both_in_one_retry():
    eff = InferenceEffect(endpoint="http://unused")
    bodies: list[str] = []

    async def fake(client, body, **kw):
        bodies.append(body["query"])
        missing = [
            f for f in ("sessionTurnId", "        temperature") if f in body["query"]
        ]
        if missing:
            return InferenceResult(
                text="",
                tokens_generated=0,
                error="GraphQL errors: "
                + " ".join(
                    f"Cannot query field '{m.strip()}' on type" for m in missing
                ),
            )
        return InferenceResult(text="ok", tokens_generated=1)

    eff._request_with_health_watchdog = fake
    eff._get_client = _no_client
    assert (await eff.session_turn("s1", "p")).text == "ok"
    assert len(bodies) == 2
    assert eff._temperature_field_supported is False
    assert eff._session_turn_fields_supported is False


# ── the row records it ───────────────────────────────────────────────


def _call(result, cfg):
    with step_context("m1", 3, "interact", "derive_acceptance"):
        return build_inference_call(
            result,
            prompt="p",
            response_text="r",
            thinking="",
            config_overrides=cfg,
            wall_ms=1.0,
            purpose="step_inference",
        )


def test_the_served_temperature_is_what_the_row_records():
    r = InferenceResult(text="r", tokens_generated=1, temperature=0.7)
    r.temperature_sent = 0.07
    call = _call(r, {"temperature": "t*0.1"})
    assert call.temperature == 0.7
    assert call.temperature_requested == "t*0.1"


def test_without_a_served_value_the_row_records_what_was_sent():
    r = InferenceResult(text="r", tokens_generated=1)
    r.temperature_sent = 0.07
    assert _call(r, {"temperature": "t*0.1"}).temperature == pytest.approx(0.07)


def test_a_relative_request_is_never_coerced_to_zero():
    """The bug: float("t*0.1") failed and the row said 0.0."""
    call = _call(
        InferenceResult(text="r", tokens_generated=1), {"temperature": "t*0.1"}
    )
    assert call.temperature is None
    assert call.temperature_requested == "t*0.1"


def test_a_plain_numeric_request_still_records():
    call = _call(InferenceResult(text="r", tokens_generated=1), {"temperature": 0.3})
    assert call.temperature == 0.3 and call.temperature_requested == "0.3"
    assert _call(InferenceResult(text="r", tokens_generated=1), {}).temperature is None


@pytest.mark.asyncio
async def test_the_store_round_trips_both_and_reads_older_parts(tmp_path):
    (tmp_path / ".agent").mkdir()
    agent = str(tmp_path / ".agent")
    store = HistoryStore(str(tmp_path), "m1", "full")
    store.ingest(
        InferenceCall(
            mission_id="m1",
            flow="interact",
            step="derive_acceptance",
            temperature=0.7,
            temperature_requested="t*0.1",
        )
    )
    await store.close("completed")
    # A part written before the column existed (the 09-24 run's store).
    from agent.history.schema import TURNS_SCHEMA

    old = pa.schema([f for f in TURNS_SCHEMA if f.name != "temperature_requested"])
    part = os.path.join(agent, "history", "turns", "run=old-run")
    os.makedirs(part)
    pq.write_table(
        pa.Table.from_pylist(
            [
                {
                    "turn_id": "aa",
                    "event_id": "aa",
                    "run_id": "old-run",
                    "seq": 1,
                    "ts_ms": 1,
                    "schema_version": 1,
                    "flow": "f",
                    "step": "s",
                    "temperature": 0.0,
                }
            ],
            schema=old,
        ),
        os.path.join(part, "part-00000001-00000001-aaaaaa.parquet"),
    )
    rows = {t["step"]: t for t in reader.load_turns(agent, with_content=False)}
    assert rows["derive_acceptance"]["temperature"] == 0.7
    assert rows["derive_acceptance"]["temperature_requested"] == "t*0.1"
    assert rows["s"].get("temperature_requested") is None


# ── replay re-asks the original request ──────────────────────────────


def test_replay_re_asks_the_original_request():
    assert (
        _config(
            {"temperature_requested": "t*0.1", "temperature": 0.7}, None, None, None
        )["temperature"]
        == "t*0.1"
    )
    assert (
        _config({"temperature_requested": "0.3", "temperature": 0.7}, None, None, None)[
            "temperature"
        ]
        == 0.3
    )
    assert _config({"temperature": 0.7}, None, None, None)["temperature"] == 0.7
    assert (
        _config({"temperature_requested": "t*0.1"}, None, 0.9, None)["temperature"]
        == 0.9
    )
