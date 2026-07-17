"""Contract-swarm wiring pins (compiled.json + registries).

The controller copy's contract: mission_control_swarm differs from
mission_control ONLY in its identity and the batch-create tail-call
target — every other dispatch must stay byte-identical so the swarm
set A/Bs the structural phase and nothing else."""

from __future__ import annotations

import json
from pathlib import Path

from agent.flow_sets import CODE_CORE_PHASES, FLOW_SETS

_REPO = Path(__file__).parent.parent


def _compiled():
    return json.loads((_REPO / "flows" / "compiled.json").read_text())


def test_director_return_routing():
    """Sub-flows 'return' to mission_control by name (shared templates);
    the loop must route that to the mission's own controller — found by
    the toy smoke when one return silently handed a swarm mission back
    to plain mission_control (which dispatched build_structure)."""
    import asyncio
    from types import SimpleNamespace

    from agent.loop import _route_director_return
    from agent.tail_call import FlowTailCall

    class _Effects:
        def __init__(self, flow_set):
            self._m = SimpleNamespace(config=SimpleNamespace(flow_set=flow_set))

        async def load_mission(self):
            return self._m

    # Swarm mission: mission_control → mission_control_swarm.
    out = asyncio.run(
        _route_director_return(
            FlowTailCall(target_flow="mission_control"), _Effects("contract_swarm")
        )
    )
    assert out.target_flow == "mission_control_swarm"

    # code_core mission: identity.
    out = asyncio.run(
        _route_director_return(
            FlowTailCall(target_flow="mission_control"), _Effects("code_core")
        )
    )
    assert out.target_flow == "mission_control"

    # Non-director targets never touched.
    out = asyncio.run(
        _route_director_return(
            FlowTailCall(target_flow="file_ops"), _Effects("contract_swarm")
        )
    )
    assert out.target_flow == "file_ops"


def test_flow_set_registered():
    spec = FLOW_SETS["contract_swarm"]
    assert spec.entry_flow == "mission_control_swarm"
    assert spec.phases is CODE_CORE_PHASES


def test_actions_registered():
    from agent.actions.registry import build_action_registry

    names = build_action_registry().registered_actions
    for a in (
        "parse_contracts",
        "apply_contract_review",
        "swarm_generate_symbols",
        "assemble_contract_files",
        "run_contract_doctests",
    ):
        assert a in names


def test_formatter_registered():
    from agent.renderers import RENDERER_REGISTRY

    assert "render_contract_digest" in RENDERER_REGISTRY


def test_controller_delta_is_exactly_the_batch_target():
    c = _compiled()
    base = c["mission_control"]["steps"]
    swarm = c["mission_control_swarm"]["steps"]
    assert set(base) == set(swarm)
    for name in base:
        b, s = base[name], swarm[name]
        if name == "dispatch_batch_create":
            assert s["tail_call"]["flow"] == "build_contracts"
            assert b["tail_call"]["flow"] == "build_structure"
        elif name == "idle":
            continue  # _self differs by design (controller identity)
        else:
            assert b == s, f"unexpected controller drift in step {name!r}"


def test_build_contracts_graph():
    steps = _compiled()["build_contracts"]["steps"]
    assert _compiled()["build_contracts"]["entry"] == "load_state"

    # Every transition target exists.
    names = set(steps)
    for name, step in steps.items():
        rules = (step.get("resolver") or {}).get("rules") or []
        for r in rules:
            assert r["transition"] in names, f"{name} → {r['transition']} missing"
        turn = step.get("turn") or {}
        for target in (turn.get("transitions") or {}).get("options", {}).values():
            assert target in names
        for key in ("default", "no_answer"):
            t = (turn.get("transitions") or {}).get(key)
            if t:
                assert t in names

    # The revision loop and the no_answer convergence edges.
    parse_rules = {
        r["transition"] for r in steps["parse_contracts"]["resolver"]["rules"]
    }
    assert {"apply_results", "review_contracts", "author_contracts"} <= parse_rules
    review_t = steps["review_contracts"]["turn"]["transitions"]
    assert review_t["no_answer"] == "fan_out_workers"
    apply_review_rules = {
        r["transition"] for r in steps["apply_review"]["resolver"]["rules"]
    }
    assert {"author_contracts", "fan_out_workers"} <= apply_review_rules

    # Reuse pins: bookkeeping and gates are the shared batch machinery.
    assert steps["apply_results"]["action"] == "apply_batch_results"
    assert steps["apply_results"]["params"]["flow_label"] == "build_contracts"
    assert steps["run_batch_checks"]["action"] == "run_batch_file_checks"
    assert (
        steps["run_batch_checks"]["resolver"]["rules"][0]["transition"]
        == "run_doctests"
    )

    # Boss-swappable turns expose config.model (default local).
    assert steps["author_contracts"]["turn"]["config"]["model"] == ""
    assert steps["review_contracts"]["turn"]["config"]["model"] == ""
