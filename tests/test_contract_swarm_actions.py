"""Contract-swarm actions: parse gate, review verdict, worker fan-out,
per-file assembly, doctest merge.

The behavioral spine: contracts are stubs that round-trip through
build_frame; workers run CONCURRENTLY under a semaphore with ONE
error-threaded retry; assembly is all-or-nothing PER FILE (failed files
stay missing for the serial sweep); doctest failures keep goals
incomplete."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from agent.actions.contract_swarm_actions import (
    action_apply_contract_review,
    action_assemble_contract_files,
    action_parse_contracts,
    action_run_contract_doctests,
    action_swarm_generate_symbols,
)
from agent.models import FlowMeta, StepInput
from agent.persistence.models import (
    ArchitectureState,
    MissionConfig,
    MissionState,
    ModuleSpec,
)


def _mission(tmp_path, files=("models.py", "engine.py", "world.yaml")):
    return MissionState(
        objective="t",
        status="active",
        config=MissionConfig(working_directory=str(tmp_path)),
        architecture=ArchitectureState(
            run_command="python engine.py",
            creation_order=list(files),
            modules=[ModuleSpec(file=f, responsibility="r") for f in files],
        ),
    )


def _si(context, params=None, effects=None):
    return StepInput(
        context=context,
        params=params or {},
        meta=FlowMeta(flow_name="build_contracts", step_id="t"),
        effects=effects,
    )


_MODELS_STUB = '''```python
# === FILE: models.py ===
"""Data models."""


def make_card(front: str, back: str) -> dict:
    """Build a card dict.

    Args:
        front: Question text.
        back: Answer text.

    Returns:
        {"front": front, "back": back}.

    >>> make_card("a", "b")["back"]
    'b'
    """
    ...
```'''

_ENGINE_STUB = '''```python
# === FILE: engine.py ===
"""Engine."""

from models import make_card


def run() -> int:
    """Run one round.

    Returns:
        0 always.

    >>> run()
    0
    """
    ...
```'''


# ── parse_contracts ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_parse_contracts_happy_path(tmp_path):
    out = await action_parse_contracts(
        _si(
            {
                "inference_response": _MODELS_STUB + "\n" + _ENGINE_STUB,
                "mission": _mission(tmp_path),
            }
        )
    )
    assert out.result["parse_ok"] is True
    cs = out.context_updates["contract_set"]
    assert set(cs["files"]) == {"models.py", "engine.py"}  # world.yaml excluded
    eng = cs["files"]["engine.py"]
    assert "⟦OUROBOROS-SYMBOL run⟧" in eng["skeleton"]
    assert eng["order"] == ["run"]
    assert eng["symbols"]["run"]["has_doctest"] is True
    assert eng["imports"] == ["models.py"]
    # Failure-path manifest defaults are published for apply_results.
    assert out.context_updates["batch_manifest"]["written"] == []


@pytest.mark.asyncio
async def test_parse_contracts_issues_thread_back_bounded(tmp_path):
    bad = "```python\n# === FILE: models.py ===\ndef broken(:\n    ...\n```"
    out = await action_parse_contracts(
        _si({"inference_response": bad, "mission": _mission(tmp_path)})
    )
    assert out.result["parse_ok"] is False
    assert out.result["revisions_left"] > 0
    assert "does not parse" in out.context_updates["contract_feedback"]
    assert out.context_updates["contract_revision"] == 1

    # Revisions exhausted: proceed with the valid subset, no feedback loop.
    out2 = await action_parse_contracts(
        _si(
            {
                "inference_response": bad,
                "mission": _mission(tmp_path),
                "contract_revision": 2,
            }
        )
    )
    assert out2.result["revisions_left"] == 0
    assert out2.context_updates["contract_feedback"] == ""


@pytest.mark.asyncio
async def test_parse_contracts_empty_response(tmp_path):
    out = await action_parse_contracts(
        _si({"inference_response": "", "mission": _mission(tmp_path)})
    )
    assert out.result["no_files"] is True
    assert out.context_updates["batch_manifest"]["missing"] == [
        "models.py",
        "engine.py",
        "world.yaml",
    ]


@pytest.mark.asyncio
async def test_parse_contracts_missing_docstring_is_issue(tmp_path):
    stub = (
        "```python\n# === FILE: models.py ===\n"
        "def make_card(front, back):\n    ...\n```"
    )
    out = await action_parse_contracts(
        _si({"inference_response": stub, "mission": _mission(tmp_path)})
    )
    assert any(
        "docstring" in i["problem"]
        for i in out.context_updates["contract_set"]["issues"]
    )


# ── apply_contract_review ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_review_revise_then_bounded():
    verdict = (
        '```json\n{"passed": false, "issues": '
        '[{"file": "engine.py", "symbol": "run", "instruction": "fix X"}]}\n```'
    )
    out = await action_apply_contract_review(
        _si({"inference_response": verdict, "contract_set": {"files": {}}})
    )
    assert out.result["revise"] is True
    assert "fix X" in out.context_updates["contract_feedback"]

    out2 = await action_apply_contract_review(
        _si(
            {
                "inference_response": verdict,
                "contract_set": {"files": {}},
                "contract_revision": 2,
            }
        )
    )
    assert out2.result["approved"] is True  # bound reached


@pytest.mark.asyncio
async def test_review_garbage_fails_open():
    out = await action_apply_contract_review(
        _si({"inference_response": "no json here", "contract_set": {"files": {}}})
    )
    assert out.result["approved"] is True
    assert "fail-open" in out.observations


# ── swarm_generate_symbols ───────────────────────────────────────────


_GOOD_BODY = '''```python
def run() -> int:
    """Run one round.

    Returns:
        0 always.

    >>> run()
    0
    """
    return 0
```'''


class _FanoutEffects:
    """Scripted run_inference recording concurrency + prompts."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.prompts: list[str] = []
        self.in_flight = 0
        self.max_in_flight = 0

    async def run_inference(self, prompt, config_overrides=None):
        self.prompts.append(prompt)
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        await asyncio.sleep(0.01)
        self.in_flight -= 1
        text = self.responses.pop(0) if self.responses else _GOOD_BODY
        return SimpleNamespace(text=text, error=None, tokens_generated=7)


def _contract_set_one_symbol():
    return {
        "files": {
            "engine.py": {
                "stub_text": _ENGINE_STUB.split("===\n", 1)[1],
                "skeleton": '"""Engine."""\n\nfrom models import make_card\n\n'
                "# ⟦OUROBOROS-SYMBOL run⟧ def run() -> int: ...\n",
                "order": ["run"],
                "symbols": {
                    "run": {
                        "stub": "def run() -> int:\n    ...\n",
                        "kind": "function",
                        "signature": "def run() -> int",
                        "has_doctest": True,
                    }
                },
                "imports": ["models.py"],
            }
        },
        "issues": [],
    }


@pytest.mark.asyncio
async def test_fanout_happy_path_and_tokens():
    eff = _FanoutEffects([_GOOD_BODY])
    out = await action_swarm_generate_symbols(
        _si(
            {"contract_set": _contract_set_one_symbol(), "swarm_token_base": 100},
            params={"workers": 2},
            effects=eff,
        )
    )
    assert out.result["any_ok"] is True
    wr = out.context_updates["worker_results"]["engine.py"]["run"]
    assert wr["ok"] and wr["body"].startswith("def run()")
    assert out.context_updates["inference_tokens_generated"] == 107


@pytest.mark.asyncio
async def test_fanout_error_threaded_retry():
    wrong_name = '```python\ndef sprint() -> int:\n    """D."""\n    return 0\n```'
    eff = _FanoutEffects([wrong_name, _GOOD_BODY])
    out = await action_swarm_generate_symbols(
        _si({"contract_set": _contract_set_one_symbol()}, effects=eff)
    )
    wr = out.context_updates["worker_results"]["engine.py"]["run"]
    assert wr["ok"] and wr["attempts"] == 2
    assert "failed validation" in eff.prompts[1]
    assert "named 'run'" in eff.prompts[1]


@pytest.mark.asyncio
async def test_fanout_rejections_and_all_failed():
    with_import = (
        '```python\nimport os\n\ndef run() -> int:\n    """D."""\n    return 0\n```'
    )
    eff = _FanoutEffects([with_import, with_import])
    out = await action_swarm_generate_symbols(
        _si({"contract_set": _contract_set_one_symbol()}, effects=eff)
    )
    assert out.result["any_ok"] is False
    wr = out.context_updates["worker_results"]["engine.py"]["run"]
    assert not wr["ok"] and "no imports" in wr["error"]


@pytest.mark.asyncio
async def test_fanout_semaphore_caps_concurrency():
    cs = _contract_set_one_symbol()
    # Five symbols in one file, workers=2 → max in-flight must be ≤ 2.
    sym = cs["files"]["engine.py"]["symbols"]["run"]
    cs["files"]["engine.py"]["order"] = [f"run{i}" for i in range(5)]
    cs["files"]["engine.py"]["symbols"] = {f"run{i}": dict(sym) for i in range(5)}
    eff = _FanoutEffects(
        []
    )  # every response is the default _GOOD_BODY (wrong names → retries fail; fine)
    out = await action_swarm_generate_symbols(
        _si({"contract_set": cs}, params={"workers": 2}, effects=eff)
    )
    assert eff.max_in_flight <= 2
    assert out.context_updates["swarm_stats"]["symbols"] == 5


# ── assemble_contract_files ──────────────────────────────────────────


class _WriteEffects:
    def __init__(self):
        self.writes: dict[str, str] = {}

    async def write_file(self, path, content):
        self.writes[path] = content
        return SimpleNamespace(success=True)

    async def read_file(self, path):
        return SimpleNamespace(exists=False, content="")


@pytest.mark.asyncio
async def test_assemble_writes_complete_files_only(tmp_path):
    cs = _contract_set_one_symbol()
    cs["files"]["models.py"] = dict(
        cs["files"]["engine.py"],
        skeleton='"""M."""\n\n# ⟦OUROBOROS-SYMBOL make_card⟧ stub\n',
        order=["make_card"],
        symbols={
            "make_card": {
                "stub": "",
                "kind": "function",
                "signature": "",
                "has_doctest": False,
            }
        },
    )
    worker_results = {
        "engine.py": {
            "run": {
                "ok": True,
                "body": 'def run() -> int:\n    """D.\n\n    >>> run()\n    0\n    """\n    return 0\n',
            }
        },
        "models.py": {"make_card": {"ok": False, "body": "", "error": "x"}},
    }
    eff = _WriteEffects()
    out = await action_assemble_contract_files(
        _si(
            {
                "contract_set": cs,
                "worker_results": worker_results,
                "mission": _mission(tmp_path),
            },
            effects=eff,
        )
    )
    assert out.result["files_written"] == 1
    m = out.context_updates["batch_manifest"]
    assert m["written"] == ["engine.py"]
    assert "models.py" in m["missing"] and "world.yaml" in m["missing"]
    assert "def run() -> int:" in eff.writes["engine.py"]
    assert "⟦OUROBOROS-SYMBOL" not in eff.writes["engine.py"]
    assert out.context_updates["primary_code_file"] == "engine.py"


@pytest.mark.asyncio
async def test_assemble_splice_mismatch_leaves_file_unwritten(tmp_path):
    cs = _contract_set_one_symbol()
    # Body for a symbol the skeleton has no sentinel for → splice fails.
    cs["files"]["engine.py"]["skeleton"] = '"""Engine."""\n'
    worker_results = {
        "engine.py": {"run": {"ok": True, "body": "def run():\n    return 0\n"}}
    }
    eff = _WriteEffects()
    out = await action_assemble_contract_files(
        _si(
            {
                "contract_set": cs,
                "worker_results": worker_results,
                "mission": _mission(tmp_path),
            },
            effects=eff,
        )
    )
    assert out.result["files_written"] == 0
    assert eff.writes == {}


# ── run_contract_doctests ────────────────────────────────────────────


class _CmdEffects:
    def __init__(self, rc):
        self.rc = rc
        self.commands: list[str] = []

    async def run_command(self, cmd, timeout=60):
        self.commands.append(cmd)
        return SimpleNamespace(
            return_code=self.rc, stdout="", stderr="boom" if self.rc else ""
        )


@pytest.mark.asyncio
async def test_doctests_merge_failures_into_checks():
    cs = _contract_set_one_symbol()
    eff = _CmdEffects(rc=1)
    out = await action_run_contract_doctests(
        _si(
            {
                "files_changed": ["engine.py"],
                "contract_set": cs,
                "batch_check_results": {
                    "engine.py": {"passed": True, "checks_failed": [], "output": ""}
                },
            },
            effects=eff,
        )
    )
    assert out.result["doctests_failed"] == 1
    entry = out.context_updates["batch_check_results"]["engine.py"]
    assert entry["passed"] is False
    assert "doctest: engine.py" in entry["checks_failed"]
    assert eff.commands == ["python -m doctest engine.py"]


@pytest.mark.asyncio
async def test_doctests_skip_files_without_doctests():
    cs = _contract_set_one_symbol()
    cs["files"]["engine.py"]["symbols"]["run"]["has_doctest"] = False
    eff = _CmdEffects(rc=0)
    out = await action_run_contract_doctests(
        _si({"files_changed": ["engine.py"], "contract_set": cs}, effects=eff)
    )
    assert eff.commands == []
    assert "none declared" in out.observations
