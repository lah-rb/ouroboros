"""The shared validation-check-result dict.

`validation_results` entries — the verdict rows the ops task loop and the
code_core quality gate both accumulate — all have the same seven-key contract:
``{name, command, passed, required, stdout, stderr, return_code}``. That shape
was hand-built inline at seven sites (the run_validation_checks validator, the
ops verify-before-harvest record, the profile/sanity/format/boot-liveness oracle
rungs, and the asym property probe), each repeating the 500-char output cap and
the pass→return-code convention. One constructor here removes the drift risk;
callers pass raw values and this applies the cap + the return-code default.

Pure and dependency-free (imports nothing from agent.*), so any action module
can use it without cycle risk.
"""

from __future__ import annotations

# Output cap for a check row — keep stdout/stderr bounded in the mission state.
CHECK_OUTPUT_CAP = 500


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

    stdout/stderr are capped at ``CHECK_OUTPUT_CAP`` chars; ``return_code``
    defaults to ``0 if passed else 1`` when not given (the fail-only rungs rely
    on this — they pass only ``passed=False`` + a reason as ``stdout``).
    """
    return {
        "name": name,
        "command": command,
        "passed": passed,
        "required": required,
        "stdout": (stdout or "")[:CHECK_OUTPUT_CAP],
        "stderr": (stderr or "")[:CHECK_OUTPUT_CAP],
        "return_code": (0 if passed else 1) if return_code is None else return_code,
    }
