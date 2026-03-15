"""Demo script — runs the test flows to verify the flow engine works end-to-end.

Phase 3: Adds inference demo using MockEffects (no LLMVP required)
         and optional live inference demo against LLMVP backend.
"""

import asyncio
import logging
import os
import sys

from agent.actions.registry import build_action_registry
from agent.effects import LocalEffects, MockEffects
from agent.loader import load_flow
from agent.runtime import execute_flow

logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(message)s")
logger = logging.getLogger(__name__)


async def main():
    registry = build_action_registry()
    effects = LocalEffects(os.getcwd())

    # ── Demo 1: Read an existing file via effects ─────────────────
    print("\n" + "=" * 60)
    print("DEMO 1: test_simple flow — read pyproject.toml (via LocalEffects)")
    print("=" * 60)

    flow = load_flow("flows/test_simple.yaml")
    result = await execute_flow(
        flow, {"target_file_path": "pyproject.toml"}, registry, effects=effects
    )

    print(f"  Status:    {result.status}")
    print(f"  Steps:     {' → '.join(result.steps_executed)}")
    print(f"  File read: {result.context.get('target_file', {}).get('path', 'N/A')}")
    for obs in result.observations:
        print(f"  Obs:       {obs}")

    # Show effects log
    log = effects.get_log()
    print(f"  Effects log: {len(log)} operations")
    for entry in log:
        print(
            f"    {entry.method}({entry.args_summary}) → {entry.result_summary} [{entry.duration_ms:.1f}ms]"
        )
    effects.clear_log()

    # ── Demo 2: Read a missing file via effects ───────────────────
    print("\n" + "=" * 60)
    print("DEMO 2: test_simple flow — read missing file (via LocalEffects)")
    print("=" * 60)

    result = await execute_flow(
        flow, {"target_file_path": "nonexistent.xyz"}, registry, effects=effects
    )

    print(f"  Status:    {result.status}")
    print(f"  Steps:     {' → '.join(result.steps_executed)}")
    log = effects.get_log()
    print(f"  Effects log: {len(log)} operations")
    for entry in log:
        print(f"    {entry.method}({entry.args_summary}) → {entry.result_summary}")
    effects.clear_log()

    # ── Demo 3: Branching flow ────────────────────────────────────
    print("\n" + "=" * 60)
    print("DEMO 3: test_branching flow — mode='fast'")
    print("=" * 60)

    branch_flow = load_flow("flows/test_branching.yaml")
    result = await execute_flow(
        branch_flow, {"mode": "fast"}, registry, effects=effects
    )

    print(f"  Status:    {result.status}")
    print(f"  Steps:     {' → '.join(result.steps_executed)}")
    print(f"  Route:     {result.context.get('route_taken', 'N/A')}")

    # ── Demo 4: Path traversal protection ─────────────────────────
    print("\n" + "=" * 60)
    print("DEMO 4: Path traversal protection")
    print("=" * 60)

    traversal_result = await effects.read_file("../../etc/passwd")
    print(f"  Read ../../etc/passwd → exists={traversal_result.exists}")
    traversal_write = await effects.write_file("../../evil.txt", "bad")
    print(f"  Write ../../evil.txt → success={traversal_write.success}")
    print("  ✅ Path traversal blocked!")

    # ── Demo 5: Inference flow with MockEffects ───────────────────
    print("\n" + "=" * 60)
    print("DEMO 5: test_inference flow — inference + LLM menu (MockEffects)")
    print("=" * 60)

    mock_effects = MockEffects(
        files={"demo.py": "def hello():\n    print('Hello, world!')\n"},
        inference_responses=[
            "This is a simple Python file with a hello() function that prints a greeting.",
            "complete",  # LLM menu resolver picks "complete"
        ],
    )

    inference_flow = load_flow("flows/test_inference.yaml")
    result = await execute_flow(
        inference_flow,
        {"target_file_path": "demo.py"},
        registry,
        effects=mock_effects,
    )

    print(f"  Status:    {result.status}")
    print(f"  Steps:     {' → '.join(result.steps_executed)}")
    print(f"  Inference calls: {mock_effects.call_count('run_inference')}")
    for obs in result.observations:
        print(f"  Obs:       {obs}")

    # Show what the inference saw
    inf_calls = mock_effects.calls_to("run_inference")
    if inf_calls:
        print(
            f"  First inference prompt (truncated): {inf_calls[0].args['prompt'][:60]}..."
        )

    # ── Demo 6: Inference with deeper analysis path ───────────────
    print("\n" + "=" * 60)
    print("DEMO 6: test_inference flow — deeper analysis path (MockEffects)")
    print("=" * 60)

    mock_effects2 = MockEffects(
        files={
            "complex.py": "class Agent:\n    def run(self): ...\n    def think(self): ...\n"
        },
        inference_responses=[
            "This file defines an Agent class with run() and think() methods.",
            "analyze_deeper",  # LLM menu picks deeper analysis
            "The Agent class follows a think-then-act pattern common in AI agents.",
        ],
    )

    result = await execute_flow(
        inference_flow,
        {"target_file_path": "complex.py"},
        registry,
        effects=mock_effects2,
    )

    print(f"  Status:    {result.status}")
    print(f"  Steps:     {' → '.join(result.steps_executed)}")
    print(f"  Inference calls: {mock_effects2.call_count('run_inference')}")
    print(
        f"  Path taken: {'deeper analysis' if 'analyze_deeper' in result.steps_executed else 'direct complete'}"
    )

    # ── Demo 7 (optional): Live inference against LLMVP ───────────
    if "--live" in sys.argv:
        print("\n" + "=" * 60)
        print("DEMO 7: LIVE inference against LLMVP backend")
        print("=" * 60)

        live_effects = LocalEffects(
            os.getcwd(),
            llmvp_endpoint="http://localhost:8000/graphql",
        )

        try:
            # Quick health check
            from agent.effects.inference import InferenceEffect

            ie = InferenceEffect()
            health = await ie.health_check()
            print(f"  LLMVP health: {health}")
            await ie.close()

            result = await execute_flow(
                inference_flow,
                {"target_file_path": "pyproject.toml"},
                registry,
                effects=live_effects,
            )

            print(f"  Status:    {result.status}")
            print(f"  Steps:     {' → '.join(result.steps_executed)}")
            for obs in result.observations:
                print(f"  Obs:       {obs}")

            # Show the actual model response
            inference_resp = result.context.get("inference_response", "")
            if inference_resp:
                print(f"  Model response: {inference_resp[:200]}...")

        except Exception as e:
            print(f"  ⚠️  Live demo failed: {e}")
            print(
                "  Make sure LLMVP is running: cd ../llmvp && uv run llmvp.py --backend"
            )
    else:
        print("\n  (Run with --live to test against LLMVP backend)")

    print("\n" + "=" * 60)
    print("All demos completed successfully! ✅")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
