"""Contract-swarm actions: parse gate, review verdict, worker fan-out,
per-file assembly, doctest merge.

The behavioral spine: contracts are stubs that round-trip through
build_frame; workers run CONCURRENTLY under a semaphore with ONE
error-threaded retry; assembly is all-or-nothing PER FILE (failed files
stay missing for the serial sweep); doctest failures keep goals
incomplete."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from agent.actions.contract_swarm_actions import (
    _DATA_CONTRACT_MARKER,
    _ENTITY_REGISTRY_MARKER,
    _data_contracts,
    _data_digest,
    _enrich_data_goals,
    _project_digest,
    _pyi_view,
    _registry_digest,
    _validate_worker_body,
    _worker_prompt,
    action_apply_contract_review,
    action_assemble_contract_files,
    action_parse_contracts,
    action_run_contract_doctests,
    action_run_contract_typecheck,
    action_store_data_registry,
    action_swarm_generate_symbols,
)
from agent.models import FlowMeta, StepInput
from agent.persistence.models import (
    ArchitectureState,
    DataShapeContract,
    GoalRecord,
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


def _si(context, params=None, effects=None, inputs=None):
    return StepInput(
        context=context,
        params=params or {},
        meta=FlowMeta(flow_name="build_contracts", step_id="t"),
        effects=effects,
        inputs=inputs or {},
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
    """Scripted run_inference recording concurrency + prompts + overrides."""

    def __init__(self, responses, truncated=False):
        self.responses = list(responses)
        self.prompts: list[str] = []
        self.overrides: list[dict] = []
        self.truncated = truncated
        self.in_flight = 0
        self.max_in_flight = 0

    async def run_inference(self, prompt, config_overrides=None):
        self.prompts.append(prompt)
        self.overrides.append(dict(config_overrides or {}))
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        await asyncio.sleep(0.01)
        self.in_flight -= 1
        text = self.responses.pop(0) if self.responses else _GOOD_BODY
        return SimpleNamespace(
            text=text, error=None, tokens_generated=7, truncated=self.truncated
        )


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
            effects=eff,
        )
    )
    assert out.result["any_ok"] is True
    wr = out.context_updates["worker_results"]["engine.py"]["run"]
    assert wr["ok"] and wr["body"].startswith("def run()")
    assert out.context_updates["inference_tokens_generated"] == 107
    # Workers carry NO max_tokens cap — EOS ends generation (the removed
    # worker_max_tokens starved the biggest symbols, 2026-07-20).
    assert all("max_tokens" not in ov for ov in eff.overrides)


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


def _contract_set_n_symbols(n: int):
    cs = _contract_set_one_symbol()
    sym = cs["files"]["engine.py"]["symbols"]["run"]
    cs["files"]["engine.py"]["order"] = [f"run{i}" for i in range(n)]
    cs["files"]["engine.py"]["symbols"] = {f"run{i}": dict(sym) for i in range(n)}
    return cs


@pytest.mark.asyncio
async def test_fanout_max_workers_caps_concurrency():
    # Five symbols, max_workers=2 (admission ceiling) → max in-flight ≤ 2
    # even though the pool comfortably fits n=symbols.
    eff = _FanoutEffects(
        []
    )  # every response is the default _GOOD_BODY (wrong names → retries fail; fine)
    out = await action_swarm_generate_symbols(
        _si(
            {"contract_set": _contract_set_n_symbols(5)},
            params={"max_workers": 2},
            effects=eff,
        )
    )
    assert eff.max_in_flight <= 2
    assert out.context_updates["swarm_stats"]["symbols"] == 5


@pytest.mark.asyncio
async def test_fanout_defaults_to_n_symbols():
    # No params: sem = min(n_symbols, 32) and the gate reports "full".
    eff = _FanoutEffects([])
    out = await action_swarm_generate_symbols(
        _si({"contract_set": _contract_set_n_symbols(5)}, effects=eff)
    )
    stats = out.context_updates["swarm_stats"]
    assert stats["gate"] == "full" and stats["workers"] == 5


@pytest.mark.asyncio
async def test_fanout_pool_fit_gate_waves_when_pool_small():
    # A pool too small for 5 concurrent draws forces waves: the per-stream
    # draw (~prompt est + 2048 margin) exceeds half of 80% of pool_budget,
    # so sem collapses to 1 and in-flight never exceeds it.
    eff = _FanoutEffects([])
    out = await action_swarm_generate_symbols(
        _si(
            {"contract_set": _contract_set_n_symbols(5)},
            params={"pool_budget": 4096},
            effects=eff,
        )
    )
    stats = out.context_updates["swarm_stats"]
    assert stats["gate"] == "waved" and stats["workers"] == 1
    assert eff.max_in_flight <= 1


@pytest.mark.asyncio
async def test_fanout_truncated_fails_fast_no_retry():
    # A generation that hit the server ceiling is a ramble: the attempt
    # fails with an honest reason and is NOT retried (the AST gate must
    # never mislabel truncation as invalid code).
    eff = _FanoutEffects([_GOOD_BODY, _GOOD_BODY], truncated=True)
    out = await action_swarm_generate_symbols(
        _si({"contract_set": _contract_set_one_symbol()}, effects=eff)
    )
    wr = out.context_updates["worker_results"]["engine.py"]["run"]
    assert not wr["ok"] and wr["attempts"] == 1
    assert "ceiling" in wr["error"]
    assert len(eff.prompts) == 1


@pytest.mark.asyncio
async def test_fanout_writes_perf_sidecar(tmp_path):
    eff = _FanoutEffects([_GOOD_BODY])
    out = await action_swarm_generate_symbols(
        _si(
            {"contract_set": _contract_set_one_symbol()},
            effects=eff,
            inputs={"working_directory": str(tmp_path)},
        )
    )
    assert out.result["any_ok"] is True
    rows = [
        json.loads(line)
        for line in (tmp_path / ".agent" / "swarm_perf.jsonl").read_text().splitlines()
    ]
    events = [r["event"] for r in rows]
    assert events[0] == "burst" and events[-1] == "burst_done"
    worker_rows = [r for r in rows if r["event"] == "worker"]
    assert len(worker_rows) == 1
    w = worker_rows[0]
    assert w["symbol"] == "run" and w["ok"] and w["generated_tokens"] == 7
    assert w["t_done"] >= w["t_submit"] >= 0


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


# ── Round 1: dep-digest carries full type shapes ─────────────────────

_MODELS_RICH = '''"""Models."""

from dataclasses import dataclass


@dataclass
class GameState:
    player: "Player"
    rooms_state: dict
    monsters_state: dict

    def apply(self, cmd: "Command") -> str:
        """Apply a command."""
        ...


def load_world(path: str = "world.yaml") -> GameState:
    """Load."""
    ...
'''


def test_pyi_view_renders_fields_methods_and_functions():
    view = _pyi_view(_MODELS_RICH)
    # dataclass fields (the constructor) are present — the round-0 gap
    assert "player:" in view and "rooms_state: dict" in view
    # method + free-function signatures present, bodies/docstrings elided
    assert "def apply(self, cmd:" in view
    assert "def load_world(path: str=" in view
    assert '"""' not in view  # docstrings stripped
    # NOT the bare round-0 rendering
    assert view.strip() != "class GameState:"


def test_project_digest_broadcasts_all_modules_type_shapes():
    # Round-3 broadcast: a worker sees a module's full type shape even when
    # its own module does NOT import it (parser.py declares no imports).
    cs = {
        "files": {
            "models.py": {"stub_text": _MODELS_RICH, "symbols": {}, "imports": []},
            "parser.py": {
                "stub_text": "def parse(s: str) -> str:\n    ...\n",
                "symbols": {},
                "imports": [],
            },
        }
    }
    digest = _project_digest(cs, "parser.py")  # parser imports nothing
    assert "### models.py" in digest  # still broadcast to the parser worker
    assert "rooms_state: dict" in digest  # fields reach every worker
    assert "def apply(self" in digest
    assert "### parser.py" not in digest  # own module excluded


def test_pyi_view_falls_back_on_unparseable():
    assert _pyi_view("def broken(:\n  ...") == "def broken(:\n  ..."


# ── Round 1: worker validation closes the two holes ──────────────────


def _meta(kind, stub):
    return {"kind": kind, "stub": stub, "signature": "", "has_doctest": False}


def test_validate_rejects_nested_import():
    body = (
        "def run() -> int:\n"
        '    """Doc."""\n'
        "    from os import getcwd\n"  # nested import — round-0 blind spot
        "    return 0\n"
    )
    reason = _validate_worker_body(body, "run", _meta("function", "def run(): ..."))
    assert reason and "no imports" in reason


def test_validate_rejects_off_contract_method():
    stub = 'class GameState:\n    """Doc."""\n    def apply(self) -> str: ...\n'
    body = (
        "class GameState:\n"
        '    """Doc."""\n'
        "    def apply(self) -> str:\n"
        '        return ""\n'
        "    def __init__(self, world_data=None):\n"  # not in the contract
        "        self._x = world_data\n"
    )
    reason = _validate_worker_body(body, "GameState", _meta("class", stub))
    assert reason and "__init__" in reason


def test_validate_passes_compliant_class():
    stub = 'class GameState:\n    """Doc."""\n    def apply(self) -> str: ...\n'
    body = (
        "class GameState:\n"
        '    """Doc."""\n'
        "    def apply(self) -> str:\n"
        '        return "ok"\n'
    )
    assert _validate_worker_body(body, "GameState", _meta("class", stub)) is None


# ── Round 1: import-completeness vs architecture imports_from ─────────


@pytest.mark.asyncio
async def test_parse_flags_missing_architecture_import(tmp_path):
    # Architecture says engine imports models; the engine stub omits it.
    mission = MissionState(
        objective="t",
        status="active",
        config=MissionConfig(working_directory=str(tmp_path)),
        architecture=ArchitectureState(
            run_command="python engine.py",
            creation_order=["models.py", "engine.py"],
            modules=[
                ModuleSpec(file="models.py", responsibility="r"),
                ModuleSpec(
                    file="engine.py",
                    responsibility="r",
                    imports_from={"models": ["make_card"]},
                ),
            ],
        ),
    )
    engine_no_import = (
        "```python\n# === FILE: engine.py ===\n"
        '"""Engine."""\n\n\ndef run() -> int:\n    """Doc.\n\n    >>> run()\n    0\n    """\n    ...\n```'
    )
    out = await action_parse_contracts(
        _si(
            {
                "inference_response": _MODELS_STUB + "\n" + engine_no_import,
                "mission": mission,
            }
        )
    )
    probs = [i["problem"] for i in out.context_updates["contract_set"]["issues"]]
    assert any("architecture-declared imports" in p and "models.py" in p for p in probs)


@pytest.mark.asyncio
async def test_parse_clean_when_imports_complete(tmp_path):
    mission = MissionState(
        objective="t",
        status="active",
        config=MissionConfig(working_directory=str(tmp_path)),
        architecture=ArchitectureState(
            run_command="python engine.py",
            creation_order=["models.py", "engine.py"],
            modules=[
                ModuleSpec(file="models.py", responsibility="r"),
                ModuleSpec(
                    file="engine.py",
                    responsibility="r",
                    imports_from={"models": ["make_card"]},
                ),
            ],
        ),
    )
    # _ENGINE_STUB imports models — completeness satisfied.
    out = await action_parse_contracts(
        _si(
            {
                "inference_response": _MODELS_STUB + "\n" + _ENGINE_STUB,
                "mission": mission,
            }
        )
    )
    probs = [i["problem"] for i in out.context_updates["contract_set"]["issues"]]
    assert not any("architecture-declared imports" in p for p in probs)


# ── Round 2: cross-module type-consistency gate ──────────────────────


class _ReadEffects:
    """Serves assembled file contents to action_run_contract_typecheck."""

    def __init__(self, files: dict):
        self._files = files

    async def read_file(self, path):
        content = self._files.get(path)
        return SimpleNamespace(exists=content is not None, content=content or "")


_COMBAT_ASM = (
    "class CombatEngine:\n"
    '    """C."""\n'
    "    def __init__(self, player, monster):\n"
    "        self.player = player\n"
    "        self.monster = monster\n"
    "    def resolve(self) -> str:\n"
    '        """r."""\n'
    "        return ''\n"
)
_PARSER_ASM = (
    "from dataclasses import dataclass\n\n\n"
    "@dataclass\nclass Command:\n    verb: str\n    arg: str\n"
)
_ENGINE_BAD = (
    "from combat import CombatEngine\nfrom parser import Command\n\n\n"
    "class GameEngine:\n"
    '    """E."""\n'
    "    def __init__(self, world):\n        self._w = world\n"
    "    def handle(self, cmd: Command) -> str:\n"
    '        """h."""\n'
    "        name = cmd.name\n"  # Command has verb, not name
    "        ce = CombatEngine()\n"  # missing player, monster
    "        return ce.fight()\n"  # CombatEngine has resolve, not fight
)
_ENGINE_GOOD = (
    "from combat import CombatEngine\nfrom parser import Command\n\n\n"
    "class GameEngine:\n"
    '    """E."""\n'
    "    def __init__(self, world):\n        self._w = world\n"
    "    def handle(self, cmd: Command) -> str:\n"
    '        """h."""\n'
    "        ce = CombatEngine(self._w, None)\n"
    "        return ce.resolve() + cmd.verb\n"
)


@pytest.mark.asyncio
async def test_typecheck_flags_cross_module_drift():
    eff = _ReadEffects(
        {"combat.py": _COMBAT_ASM, "parser.py": _PARSER_ASM, "engine.py": _ENGINE_BAD}
    )
    out = await action_run_contract_typecheck(
        _si({"files_changed": ["combat.py", "parser.py", "engine.py"]}, effects=eff)
    )
    assert out.result["typecheck_failed"] == 1
    eng = out.context_updates["batch_check_results"]["engine.py"]
    assert eng["passed"] is False
    assert "typecheck: engine.py" in eng["checks_failed"]
    blob = eng["output"]
    assert "no attribute/method 'name'" in blob  # Command.name
    assert "missing required argument" in blob  # CombatEngine()
    assert "'fight'" in blob  # CombatEngine.fight


@pytest.mark.asyncio
async def test_typecheck_clean_on_consistent_code():
    eff = _ReadEffects(
        {"combat.py": _COMBAT_ASM, "parser.py": _PARSER_ASM, "engine.py": _ENGINE_GOOD}
    )
    out = await action_run_contract_typecheck(
        _si({"files_changed": ["combat.py", "parser.py", "engine.py"]}, effects=eff)
    )
    assert out.result["typecheck_failed"] == 0
    assert (
        out.context_updates["batch_check_results"]
        .get("engine.py", {})
        .get("passed", True)
    )


@pytest.mark.asyncio
async def test_typecheck_skips_dynamic_and_untyped():
    dyn = 'class Bag:\n    """B."""\n    def __init__(self, **kw):\n        self.kw = kw\n'
    consumer = (
        "from bag import Bag\n\n\n"
        "def f():\n    b = Bag(anything=1)\n    return b.whatever\n"  # **kwargs → skip
        "def g(items):\n    for x in items:\n        return x.foo\n"  # untyped → skip
    )
    eff = _ReadEffects({"bag.py": dyn, "consumer.py": consumer})
    out = await action_run_contract_typecheck(
        _si({"files_changed": ["bag.py", "consumer.py"]}, effects=eff)
    )
    assert out.result["typecheck_failed"] == 0


# ── Round-4: data-shape broadcast (code↔data cohesion) ───────────────


class _SaveEffects:
    """Minimal effects that count save_mission calls."""

    def __init__(self):
        self.saves = 0

    async def save_mission(self, mission):
        self.saves += 1
        return True


def _mission_with_data(tmp_path):
    m = _mission(tmp_path)
    m.architecture.data_shapes = [
        DataShapeContract(
            file="world.yaml",
            consumed_by="loader.py",
            structure="mapping with keys rooms, monsters",
            example="rooms:\n  - id: entrance\n    name: Hall\nmonsters:\n  - id: rat\n    health: 5",
        )
    ]
    m.goals = [
        GoalRecord(
            description="Create world.yaml with content: design a world...",
            type="structural",
            associated_files=["world.yaml"],
        ),
        GoalRecord(
            description="Load the world",
            type="structural",
            associated_files=["loader.py"],
        ),
    ]
    return m


def test_data_contracts_extracts_exemplars(tmp_path):
    dc = _data_contracts(_mission_with_data(tmp_path))
    assert len(dc) == 1
    assert dc[0]["file"] == "world.yaml"
    assert dc[0]["consumed_by"] == "loader.py"
    assert dc[0]["example"].startswith("rooms:")
    assert "health: 5" in dc[0]["example"]
    # No architecture / no data_shapes → empty, no crash.
    assert _data_contracts(None) == []
    assert _data_contracts(SimpleNamespace(architecture=None)) == []


def test_data_digest_renders_shape_and_exemplar(tmp_path):
    dig = _data_digest(_data_contracts(_mission_with_data(tmp_path)))
    assert "### world.yaml (read by loader.py)" in dig
    assert "index ONLY these" in dig
    assert "health: 5" in dig
    assert _data_digest([]) == ""
    # An empty exemplar+structure entry is dropped.
    assert _data_digest([{"file": "x.json", "structure": "", "example": ""}]) == ""


@pytest.mark.asyncio
async def test_enrich_data_goals_idempotent_and_scoped(tmp_path):
    m = _mission_with_data(tmp_path)
    eff = _SaveEffects()
    n = await _enrich_data_goals(eff, m, _data_contracts(m))
    assert n == 1 and eff.saves == 1
    world_goal = next(g for g in m.goals if g.associated_files == ["world.yaml"])
    code_goal = next(g for g in m.goals if g.associated_files == ["loader.py"])
    assert _DATA_CONTRACT_MARKER in world_goal.description
    assert "health: 5" in world_goal.description  # exemplar reached the generator
    assert _DATA_CONTRACT_MARKER not in code_goal.description  # code goal untouched
    # Idempotent across contract revisions: no duplicate block, no re-save.
    n2 = await _enrich_data_goals(eff, m, _data_contracts(m))
    assert n2 == 0 and eff.saves == 1
    assert world_goal.description.count(_DATA_CONTRACT_MARKER) == 1


@pytest.mark.asyncio
async def test_parse_contracts_publishes_and_enriches_data(tmp_path):
    m = _mission_with_data(tmp_path)
    eff = _SaveEffects()
    out = await action_parse_contracts(
        _si(
            {"inference_response": _MODELS_STUB + "\n" + _ENGINE_STUB, "mission": m},
            effects=eff,
        )
    )
    cs = out.context_updates["contract_set"]
    assert cs["data_contracts"][0]["file"] == "world.yaml"
    world_goal = next(g for g in m.goals if g.associated_files == ["world.yaml"])
    assert _DATA_CONTRACT_MARKER in world_goal.description
    assert eff.saves >= 1


def test_worker_prompt_broadcasts_data_vocabulary(tmp_path):
    dc = _data_contracts(_mission_with_data(tmp_path))
    cs = {
        "files": {
            "loader.py": {
                "stub_text": "def load(p): ...",
                "skeleton": "def load(p): ...",
                "symbols": {"load": {"stub": "def load(p): ...", "kind": "function"}},
                "order": ["load"],
                "imports": [],
            }
        },
        "data_contracts": dc,
    }
    wp = _worker_prompt(cs, "loader.py", "load", "PERSONA", "INSTRUCTION")
    assert "SHARED DATA VOCABULARY" in wp
    assert "world.yaml" in wp and "health: 5" in wp
    # Absent when there are no data contracts (no empty section).
    cs["data_contracts"] = []
    assert "SHARED DATA VOCABULARY" not in _worker_prompt(
        cs, "loader.py", "load", "PERSONA", "INSTRUCTION"
    )


# ── Round-5: shared entity-id registry (data↔data cohesion) ──────────


def _mission_two_data(tmp_path):
    m = _mission(tmp_path)
    m.architecture.data_shapes = [
        DataShapeContract(file="rooms.yaml", consumed_by="loader.py"),
        DataShapeContract(file="items.yaml", consumed_by="loader.py"),
    ]
    m.goals = [
        GoalRecord(
            description="Create rooms.yaml with content: a world",
            type="structural",
            associated_files=["rooms.yaml"],
        ),
        GoalRecord(
            description="Create items.yaml with content: some items",
            type="structural",
            associated_files=["items.yaml"],
        ),
    ]
    return m


# rooms references torch (defined in items) AND phantom (defined nowhere).
_REGISTRY_RESP = (
    '```json\n{"files": ['
    '{"file": "rooms.yaml", "defines": ["entrance", "hall"], '
    '"references": {"items.yaml": ["torch", "phantom"]}}, '
    '{"file": "items.yaml", "defines": ["torch"], "references": {}}]}\n```'
)


@pytest.mark.asyncio
async def test_store_data_registry_cleans_and_drops_dangling(tmp_path):
    out = await action_store_data_registry(
        _si(
            {
                "inference_response": _REGISTRY_RESP,
                "mission": _mission_two_data(tmp_path),
            }
        )
    )
    reg = {e["file"]: e for e in out.context_updates["data_registry"]}
    assert set(reg) == {"rooms.yaml", "items.yaml"}
    # 'phantom' is not in items.yaml's defines → dropped; 'torch' kept.
    assert reg["rooms.yaml"]["references"]["items.yaml"] == ["torch"]
    assert reg["rooms.yaml"]["defines"] == ["entrance", "hall"]
    assert "dropped" in out.observations


@pytest.mark.asyncio
async def test_store_data_registry_degrades_on_garbage(tmp_path):
    for resp in ("junk", '```json\n{"nope": 1}\n```', ""):
        out = await action_store_data_registry(
            _si({"inference_response": resp, "mission": _mission_two_data(tmp_path)})
        )
        assert out.context_updates["data_registry"] == []


@pytest.mark.asyncio
async def test_store_data_registry_drops_undeclared_file(tmp_path):
    # A registry entry for a file that isn't a declared data file is ignored.
    resp = (
        '```json\n{"files": ['
        '{"file": "rooms.yaml", "defines": ["entrance"], "references": {}}, '
        '{"file": "not_a_file.yaml", "defines": ["x"], "references": {}}]}\n```'
    )
    out = await action_store_data_registry(
        _si({"inference_response": resp, "mission": _mission_two_data(tmp_path)})
    )
    files = {e["file"] for e in out.context_updates["data_registry"]}
    assert files == {"rooms.yaml"}


@pytest.mark.asyncio
async def test_enrich_data_goals_appends_id_block_scoped_idempotent(tmp_path):
    m = _mission_two_data(tmp_path)
    registry = [
        {
            "file": "rooms.yaml",
            "defines": ["entrance", "hall"],
            "references": {"items.yaml": ["torch"]},
        },
        {"file": "items.yaml", "defines": ["torch"], "references": {}},
    ]
    eff = _SaveEffects()
    n = await _enrich_data_goals(eff, m, [], registry)
    assert n == 2 and eff.saves == 1
    rooms = next(g for g in m.goals if g.associated_files == ["rooms.yaml"])
    assert _ENTITY_REGISTRY_MARKER in rooms.description
    assert "entrance" in rooms.description and "torch" in rooms.description
    # Idempotent: second pass appends nothing, no re-save.
    n2 = await _enrich_data_goals(eff, m, [], registry)
    assert n2 == 0 and eff.saves == 1
    assert rooms.description.count(_ENTITY_REGISTRY_MARKER) == 1


@pytest.mark.asyncio
async def test_enrich_data_goals_registry_none_is_round4(tmp_path):
    # No registry → only the round-4 shape block; no id block (degrade).
    m = _mission_with_data(tmp_path)
    await _enrich_data_goals(_SaveEffects(), m, _data_contracts(m), None)
    world = next(g for g in m.goals if g.associated_files == ["world.yaml"])
    assert _DATA_CONTRACT_MARKER in world.description
    assert _ENTITY_REGISTRY_MARKER not in world.description


def test_registry_digest_and_worker_broadcast(tmp_path):
    registry = [
        {
            "file": "rooms.yaml",
            "defines": ["entrance", "hall"],
            "references": {"items.yaml": ["torch"]},
        },
        {"file": "items.yaml", "defines": ["torch"], "references": {}},
    ]
    dig = _registry_digest(registry)
    assert "### rooms.yaml" in dig and "defines" in dig
    assert "may reference in items.yaml: torch" in dig
    assert _registry_digest([]) == ""
    cs = {
        "files": {
            "loader.py": {
                "stub_text": "def load(): ...",
                "skeleton": "def load(): ...",
                "symbols": {"load": {"stub": "def load(): ...", "kind": "function"}},
                "order": ["load"],
                "imports": [],
            }
        },
        "data_contracts": [],
        "data_registry": registry,
    }
    wp = _worker_prompt(cs, "loader.py", "load", "P", "I")
    assert "SHARED ID NAMESPACE" in wp and "torch" in wp
    cs["data_registry"] = []
    assert "SHARED ID NAMESPACE" not in _worker_prompt(
        cs, "loader.py", "load", "P", "I"
    )


@pytest.mark.asyncio
async def test_parse_contracts_threads_registry(tmp_path):
    m = _mission_two_data(tmp_path)
    registry = [
        {
            "file": "rooms.yaml",
            "defines": ["entrance"],
            "references": {"items.yaml": ["torch"]},
        },
        {"file": "items.yaml", "defines": ["torch"], "references": {}},
    ]
    out = await action_parse_contracts(
        _si(
            {
                "inference_response": _MODELS_STUB + "\n" + _ENGINE_STUB,
                "mission": m,
                "data_registry": registry,
            },
            effects=_SaveEffects(),
        )
    )
    cs = out.context_updates["contract_set"]
    assert cs["data_registry"] == registry
    rooms = next(g for g in m.goals if g.associated_files == ["rooms.yaml"])
    assert _ENTITY_REGISTRY_MARKER in rooms.description


# ── data-boundary gate at contract time ───────────────────────────────


def _mission_with_boundary_data(tmp_path):
    m = _mission(tmp_path)
    m.architecture.data_shapes = [
        DataShapeContract(
            file="world.yaml",
            consumed_by="engine.py",
            structure="rooms: list",
            example="rooms:\n  - id: r1\n",
        )
    ]
    return m


_DRIFTED_ENGINE_STUB = '''```python
# === FILE: engine.py ===
"""Engine.

Loads world.yaml from the 'data' directory at startup.
"""

from models import make_card


def run() -> int:
    """Run one round reading data/world.yaml.

    >>> isinstance(run, object)
    True
    """
    ...
```'''


@pytest.mark.asyncio
async def test_parse_contracts_boundary_drift_books_revision(tmp_path):
    out = await action_parse_contracts(
        _si(
            {
                "inference_response": _MODELS_STUB + "\n" + _DRIFTED_ENGINE_STUB,
                "mission": _mission_with_boundary_data(tmp_path),
            }
        )
    )
    assert out.result["parse_ok"] is False
    assert out.result["revisions_left"] > 0
    fb = out.context_updates["contract_feedback"]
    assert "declares it at 'world.yaml'" in fb or "'data' directory" in fb


@pytest.mark.asyncio
async def test_parse_contracts_threads_state_contracts_to_workers(tmp_path):
    m = _mission(tmp_path)
    m.architecture.state_shapes = [
        {
            "name": "load_world() return / Engine input",
            "owner": "loader.py",
            "consumed_by": "engine.py",
            "structure": "{rooms: dict, items: dict} — exactly these keys",
        }
    ]
    out = await action_parse_contracts(
        _si(
            {
                "inference_response": _MODELS_STUB + "\n" + _ENGINE_STUB,
                "mission": m,
            }
        )
    )
    cs = out.context_updates["contract_set"]
    assert cs["state_contracts"][0]["name"] == "load_world() return / Engine input"
    prompt = _worker_prompt(cs, "engine.py", "run", "persona text", "instruction text")
    assert "State & transfer contracts" in prompt
    assert "exactly these keys" in prompt
