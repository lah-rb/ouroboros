"""Official SWE-bench (Verified) harness adapter for Ouroboros.

Runs one code_core repair mission per instance against the instance's own
container, extracts a git-diff model_patch, and produces the official
predictions JSONL — reusing ContainerEffects + run_agent + the code_core
brownfield entry verbatim. See swe_adapter/runner.py.
"""

from swe_adapter.patch import extract_model_patch, prediction_row

__all__ = ["extract_model_patch", "prediction_row"]
