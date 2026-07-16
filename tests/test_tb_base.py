"""Shared TB-agent helpers (adapters.tb.base) + a resolution regression pin.

The resolution test exists because a cleanup regex once deleted
_probe_container_cwd/_token_totals from both agents while their call sites
survived — a live AttributeError no test caught.
"""

import ast
import re
from pathlib import Path

from adapters.tb.base import extract_deps, per_task_cap, token_totals

_TB = Path(__file__).parent.parent / "adapters" / "tb"


def test_extract_deps_filters_flags_paths_and_vars(tmp_path):
    sh = tmp_path / "run-tests.sh"
    sh.write_text(
        "uv add pandas numpy\n"
        "pip install --quiet requests==2.0 ./local/pkg $VAR -e .\n"
        "pip3 install flask | tee log\n"
    )
    # version-pinned tokens (=), paths (/.), env vars ($), and flags are all
    # dropped by design — only bare package names survive.
    assert extract_deps([sh]) == ["flask", "numpy", "pandas"]


def test_extract_deps_tolerates_missing_files(tmp_path):
    assert extract_deps([tmp_path / "nope.sh"]) == []


def test_per_task_cap_global_override_wins():
    assert per_task_cap(
        1000, fraction=0.9, fallback=300, multiplier=1.0, global_override="200"
    ) == 180.0


def test_per_task_cap_task_value_scaled():
    assert per_task_cap(
        400, fraction=0.9, fallback=300, multiplier=2.0, global_override=None
    ) == 400 * 2.0 * 0.9


def test_per_task_cap_fallback_and_floor():
    assert per_task_cap(
        None, fraction=0.9, fallback=300, multiplier=1.0, global_override=None
    ) == 300.0
    assert per_task_cap(
        1, fraction=0.5, fallback=1, multiplier=1.0, global_override=None
    ) == 60.0  # floor


def test_token_totals_sums_trace_jsonl(tmp_path):
    tr = tmp_path / ".agent" / "traces"
    tr.mkdir(parents=True)
    (tr / "a.jsonl").write_text(
        '{"tokens_in": 10, "tokens_out": 3}\n'
        "not json\n"
        '{"tokens_in": 5}\n'
    )
    assert token_totals(str(tmp_path)) == (15, 3)


def _self_calls_resolve(path: Path) -> set[str]:
    """Return self._X( calls with no matching def _X in the module (methods
    or module-level via import are both fine — we check ast defs + names)."""
    src = path.read_text()
    tree = ast.parse(src)
    defined = {n.name for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    called = set(re.findall(r"self\.(_[a-z_]+)\(", src))
    return called - defined


def test_tb1_agent_self_calls_resolve():
    assert _self_calls_resolve(_TB / "agent.py") == set()


def test_harbor_agent_self_calls_resolve():
    assert _self_calls_resolve(_TB / "harbor_agent.py") == set()
