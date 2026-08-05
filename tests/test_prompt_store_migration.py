"""The prompt store is the auditable surface — these keep it that way.

19,975 chars of prompt text (~10% of the project's total) lived as Python
string constants: invisible to review, unparseable by
check_prompt_parser_contracts, undiffable. They were there for a real
reason — all 22 sites are plain `action:` steps driving an inference
effect directly, and several pin their text as a static_prefix KV-cache
head keyed on md5(text)[:10] — so the move had to be byte-exact.

THE MD5 IS A CORRECTNESS DEVICE, not a checksum for its own sake: a cache
HIT means the pinned KV corresponds to the prefix being sent (the same
discipline the runtime states at runtime.py:1034-1039). One byte of drift
in a migrated prompt silently repins a stale KV against new text. That is
what the frozen hashes below defend.
"""

from __future__ import annotations

import ast
import hashlib
import importlib
import re
from pathlib import Path

import pytest

from agent.errors import FlowRuntimeError
from agent.loader import load_prompt_text, set_prompt_text_dir

ROOT = Path(__file__).resolve().parent.parent

# (template_id, module, constant, chars, md5[:10]) — frozen at migration.
MIGRATED = [
    (
        "personas/diagnosis",
        "diagnosis_session_actions",
        "SYSTEM_PROMPT",
        979,
        "3e34525acf",
    ),
    (
        "diagnose/conclude",
        "diagnosis_session_actions",
        "CONCLUDE_PROMPT",
        5489,
        "a4c17e5e2b",
    ),
    (
        "diagnose/systemic_scan",
        "diagnosis_session_actions",
        "SCAN_PROMPT",
        1621,
        "699e31463e",
    ),
    ("personas/router", "router_actions", "SYSTEM_PROMPT", 580, "2ea8a46ce8"),
    (
        "classify/conclude_route",
        "router_actions",
        "CONCLUDE_ROUTE_PROMPT",
        1479,
        "03fb4207f6",
    ),
    (
        "personas/escalation_seed",
        "escalation_actions",
        "SYSTEM_PROMPT",
        922,
        "9583884a24",
    ),
    ("escalate/conclude", "escalation_actions", "CONCLUDE_PROMPT", 521, "878ba1a0f6"),
    (
        "personas/operator",
        "interactive_actions",
        "OPERATOR_PERSONA",
        1511,
        "11a52d54a8",
    ),
    (
        "personas/deep_search_seed",
        "deep_search_actions",
        "SEARCH_SYSTEM_PROMPT",
        602,
        "9b89214b6b",
    ),
    (
        "deep_search/condense",
        "deep_search_actions",
        "CONDENSE_PROMPT",
        503,
        "bb764a3b5c",
    ),
    (
        "deep_search/conclude",
        "deep_search_actions",
        "CONCLUDE_SEARCH_PROMPT",
        477,
        "9b4ae455f1",
    ),
    (
        "deep_research/decompose",
        "deep_research_actions",
        "DECOMPOSE_PROMPT",
        471,
        "4742abf9fe",
    ),
    (
        "deep_research/propose",
        "deep_research_actions",
        "PROPOSE_PROMPT",
        365,
        "f4e3eb6220",
    ),
    ("deep_research/vote", "deep_research_actions", "VOTE_PROMPT", 456, "12099e604b"),
    (
        "deep_research/extract",
        "deep_research_actions",
        "EXTRACT_PROMPT",
        459,
        "57b2ee2d85",
    ),
    (
        "deep_research/verify",
        "deep_research_actions",
        "VERIFY_PROMPT",
        598,
        "b5d0a2508b",
    ),
    (
        "deep_research/merge_reflect",
        "deep_research_actions",
        "MERGE_REFLECT_PROMPT",
        667,
        "1afb189cc7",
    ),
    (
        "deep_research/synthesize",
        "deep_research_actions",
        "SYNTHESIZE_PROMPT",
        553,
        "6a95ba6633",
    ),
    (
        "patch_module/frame_instruction",
        "frame_actions",
        "_FRAME_INSTRUCTION",
        563,
        "0775a613d8",
    ),
    ("file_ops/localize", "frame_actions", "LOCALIZE_PROMPT", 822, "6cb436ae67"),
    ("data_patch/translate", "data_ops_actions", "DATA_OPS_PROMPT", 1201, "bf2e057d88"),
    (
        "diagnose_batch/worker",
        "contract_swarm_actions",
        "_DIAGNOSE_WORKER_PROMPT",
        647,
        "8417ae2048",
    ),
]


class TestTheBytesDidNotMove:
    @pytest.mark.parametrize(
        "tid,module,const,chars,md5_10",
        MIGRATED,
        ids=[m[0] for m in MIGRATED],
    )
    def test_store_text_matches_the_frozen_hash(
        self, tid, module, const, chars, md5_10
    ):
        text = load_prompt_text(tid)
        assert len(text) == chars, f"{tid} length drifted"
        assert hashlib.md5(text.encode("utf-8")).hexdigest()[:10] == md5_10, (
            f"{tid} content drifted — any site pinning this as a "
            f"static_prefix would repin a STALE KV against new text"
        )

    @pytest.mark.parametrize(
        "tid,module,const,chars,md5_10",
        MIGRATED,
        ids=[m[0] for m in MIGRATED],
    )
    def test_the_constant_still_binds_that_exact_text(
        self, tid, module, const, chars, md5_10
    ):
        """The action modules keep module-level constants with the same
        names, so every downstream use — md5 keying, static_prefix, seed
        lists, .format() — is unchanged. This pins that equivalence."""
        mod = importlib.import_module(f"agent.actions.{module}")
        assert getattr(mod, const) == load_prompt_text(tid)


class TestNoPromptTextCreptBackIntoPython:
    def test_the_migrated_constants_are_loads_not_literals(self):
        """A regression here means someone pasted prompt text back into a
        module, which is how the drift started."""
        offenders = []
        for tid, module, const, _chars, _md5 in MIGRATED:
            src = (ROOT / "agent" / "actions" / f"{module}.py").read_text()
            tree = ast.parse(src)
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.Assign)
                    and isinstance(node.value, ast.Constant)
                    and isinstance(node.value.value, str)
                    and any(
                        isinstance(t, ast.Name) and t.id == const for t in node.targets
                    )
                ):
                    offenders.append(f"{module}.{const}")
        assert not offenders, f"prompt text back in Python: {offenders}"


class TestEveryReferencedIdResolves:
    """Implements PRM-001. flows/shared/lint.cue:161-167 has declared
    'Prompt template file not found on disk' as an ERROR since the rule
    catalogue was written, and nothing ever implemented it."""

    def test_every_load_prompt_text_id_exists(self):
        pattern = re.compile(r"""load_prompt_text\(\s*["']([^"']+)["']""")
        missing = []
        for path in sorted((ROOT / "agent").rglob("*.py")):
            if "__pycache__" in str(path):
                continue
            for tid in pattern.findall(path.read_text()):
                if not (ROOT / "prompts" / f"{tid}.yaml").exists():
                    missing.append(f"{path.name}: {tid}")
        assert not missing, f"load_prompt_text ids with no file: {missing}"


class TestItFailsLoud:
    """The loader this replaces (contract_swarm_actions._load_prompt)
    swallowed every error and returned "" — a typo'd id silently produced
    an EMPTY prompt, which no test could catch."""

    def test_missing_id_raises_and_names_it(self):
        with pytest.raises(FlowRuntimeError) as exc:
            load_prompt_text("no_such/prompt_xyz")
        assert "no_such/prompt_xyz" in str(exc.value)

    def test_a_sections_format_file_is_rejected(self):
        """These must be the flat turn format. A `sections:` file would
        render as "" through this path — fail instead."""
        with pytest.raises(FlowRuntimeError):
            load_prompt_text("design_and_plan/design_architecture")

    def test_empty_content_raises(self, tmp_path):
        (tmp_path / "x").mkdir()
        (tmp_path / "x" / "empty.yaml").write_text('id: x/empty\ncontent: "  "\n')
        try:
            set_prompt_text_dir(tmp_path)
            with pytest.raises(FlowRuntimeError):
                load_prompt_text("x/empty")
        finally:
            set_prompt_text_dir(None)


class TestTheSeedPersonasAreNotTheTurnRolePersonas:
    """A near-miss worth pinning.

    `personas/deep_search` and `personas/escalation` ALREADY existed in the
    store — rendered as the per-turn role section by
    flows/shared/deep_search.cue:81 and escalate.cue:82 — while the Python
    constants held DIFFERENT text for the same personas, used as the session
    seed and static prefix. The first migration pass wrote the constants
    straight over those files, which would have silently changed what every
    per-turn menu renders.

    The two texts are both live and have drifted. Reconciling them changes
    model input, so it is a measured decision, not a tidy-up. These tests
    keep them apart until someone makes that decision deliberately.
    """

    PAIRS = [
        ("personas/deep_search", "personas/deep_search_seed"),
        ("personas/escalation", "personas/escalation_seed"),
    ]

    @pytest.mark.parametrize("turn_id,seed_id", PAIRS)
    def test_both_exist_and_differ(self, turn_id, seed_id):
        assert load_prompt_text(turn_id) != load_prompt_text(seed_id), (
            f"{turn_id} and {seed_id} converged — if that was deliberate, "
            f"delete one and repoint its reader; if not, a migration just "
            f"overwrote a live file"
        )

    @pytest.mark.parametrize("turn_id,seed_id", PAIRS)
    def test_the_turn_role_file_is_still_referenced_by_a_flow(self, turn_id, seed_id):
        """The reason overwriting it would have mattered."""
        cues = list((ROOT / "flows").rglob("*.cue"))
        assert any(
            f'"{turn_id}"' in c.read_text() for c in cues
        ), f"{turn_id} is no longer referenced by any flow"
