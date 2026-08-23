"""scraper_v2: the same pipeline, scheduled differently.

v2 exists to remove the parallel barrier — the drains move to continuous
workers — so what these tests care about is that the SHAPE of the
pipeline is unchanged while the wiring is correct, and above all that v1
and v2 cannot leak into each other. v1 is running a live mission.
"""

from __future__ import annotations

import json
import pathlib

import pytest

from agent.flow_sets import FLOW_SETS, get_flow_set

COMPILED = json.loads(
    (
        pathlib.Path(__file__).resolve().parents[1] / "flows" / "compiled.json"
    ).read_text()
)


def test_v2_is_registered_with_its_own_controller():
    spec = get_flow_set("scraper_v2")
    assert spec.name == "scraper_v2"
    assert spec.entry_flow == "research_control_v2"
    # Same phases: v2 changes HOW work is scheduled, not what the
    # pipeline does.
    assert spec.phases == get_flow_set("scraper").phases


def test_v2_dispatches_the_v2_work_flows():
    rc = COMPILED["research_control_v2"]
    targets = {
        (s.get("tail_call") or {}).get("flow")
        for s in rc["steps"].values()
        if (s.get("tail_call") or {}).get("flow")
    }
    assert "discover_v2" in targets and "acquire_catalog_v2" in targets
    # The parallel WRAPPERS are v1-only; v2 must not reach them.
    assert "discover" not in targets and "acquire_catalog" not in targets


def test_v2_work_flows_return_to_the_v2_controller():
    """A flow that ends WITHOUT a tail_call ends the whole agent run —
    loop.py turns it into FlowTermination rather than returning to the
    controller. Under v1 these were parallel branches and the wrapper
    owned the return; v2 has no wrapper, so they must return themselves."""
    for flow in ("discover_v2", "acquire_catalog_v2"):
        steps = COMPILED[flow]["steps"]
        returns = [
            (s.get("tail_call") or {}).get("flow")
            for s in steps.values()
            if (s.get("tail_call") or {}).get("flow")
        ]
        assert returns == ["research_control_v2"], flow
        terminal_without_return = [
            n
            for n, s in steps.items()
            if s.get("terminal") and not (s.get("tail_call") or {}).get("flow")
        ]
        assert not terminal_without_return, (
            f"{flow} has a terminal step with no tail_call: "
            f"{terminal_without_return} — that would END THE RUN"
        )


def test_v2_has_no_parallel_step_anywhere():
    """The barrier is the thing v2 removes. If a parallel step survives
    into v2, the restructure did not happen."""
    for flow in ("research_control_v2", "discover_v2", "acquire_catalog_v2"):
        actions = {s.get("action") for s in COMPILED[flow]["steps"].values()}
        assert "parallel" not in actions, flow


def test_v2_reuses_the_drain_flows_rather_than_forking_them():
    """The drains are identical in both worlds — the worker pool looks a
    lane's flow up in the GLOBAL registry, and a flow does not belong to
    the set that runs it. Forking them would be four files to keep in
    sync for no behavioural difference."""
    for drain in ("ocr_drain", "figtext_drain", "translate_drain", "curate_drain"):
        assert drain in COMPILED
        assert f"{drain}_v2" not in COMPILED


def test_a_shared_flows_hardcoded_return_is_routed_to_the_owning_controller():
    """plan_research is shared and hardcodes `research_control`, and
    v2's own idle step does too. Both would hand a v2 mission to the v1
    controller if _route_director_return did not re-resolve them — which
    it only does because scraper_v2 is REGISTERED in FLOW_SETS."""
    directors = {spec.entry_flow for spec in FLOW_SETS.values()}
    assert "research_control" in directors and "research_control_v2" in directors


@pytest.mark.asyncio
async def test_director_return_actually_reroutes_for_a_v2_mission():
    from agent.loop import _route_director_return
    from agent.tail_call import FlowTailCall

    class _Cfg:
        flow_set = "scraper_v2"

    class _Mission:
        config = _Cfg()

    class _Fx:
        async def load_mission(self):
            return _Mission()

    out = await _route_director_return(
        FlowTailCall(target_flow="research_control", inputs={}), _Fx()
    )
    assert out.target_flow == "research_control_v2"


@pytest.mark.asyncio
async def test_a_v1_mission_is_never_routed_into_v2():
    """The live mission runs v1. Nothing about v2 may move it."""
    from agent.loop import _route_director_return
    from agent.tail_call import FlowTailCall

    class _Cfg:
        flow_set = "scraper"

    class _Mission:
        config = _Cfg()

    class _Fx:
        async def load_mission(self):
            return _Mission()

    out = await _route_director_return(
        FlowTailCall(target_flow="research_control", inputs={}), _Fx()
    )
    assert out.target_flow == "research_control"
