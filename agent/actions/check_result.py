"""The shared validation-check-result dict.

`validation_results` entries — the verdict rows the ops task loop and the
code_core quality gate both accumulate — all have the same seven-key contract:
``{name, command, passed, required, stdout, stderr, return_code}``. That shape
was hand-built inline at seven sites (the run_validation_checks validator, the
ops verify-before-harvest record, the profile/sanity/format/boot-liveness oracle
rungs, and the asym property probe), each repeating an output cap and
the pass→return-code convention. One constructor here removes the drift risk;
callers pass raw values and this applies the return-code default.

Pure and dependency-free (imports nothing from agent.*), so any action module
can use it without cycle risk.
"""

from __future__ import annotations

# No output cap (2026-09-26). The rows were cut to their first 500 chars,
# which kept pytest's header and lost its failures — the part every judge
# and repair turn reads. They are not persisted in the mission state; a
# prompt that renders them sizes them to the serving window (the step's
# `fit` map, agent/context_fit.py).


def check_result(
    name: str,
    command: str,
    passed: bool,
    *,
    required: bool = True,
    stdout: str = "",
    stderr: str = "",
    return_code: int | None = None,
) -> dict:
    """Build a validation_results row in the shared contract shape.

    stdout/stderr are kept whole; ``return_code``
    defaults to ``0 if passed else 1`` when not given (the fail-only rungs rely
    on this — they pass only ``passed=False`` + a reason as ``stdout``).
    """
    return {
        "name": name,
        "command": command,
        "passed": passed,
        "required": required,
        "stdout": stdout or "",
        "stderr": stderr or "",
        "return_code": (0 if passed else 1) if return_code is None else return_code,
    }
