"""Run one Ouroboros mission per GAIA question.

Shape mirrors adapters.swe.runner with the container swapped for a host scratch
workspace (LocalEffects): GAIA needs web + files + reasoning, not a repo
sandbox. web_research is ON — this is the deep_search loop's production
arena. The answer channel is deterministic: the mission must write the bare
final answer string to ./answer.txt in its workspace.
"""

from __future__ import annotations

import logging
import os
import shutil
import sys
import tempfile

from adapters.gaia.loader import GaiaQuestion
from adapters._common import llmvp_endpoint, preserve_agent_dir, seed_workspace_venv  # noqa: E402
from agent.mission_runner import (  # noqa: E402
    build_and_save_mission,
    run_mission_isolated,
)

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)  # sibling-adapter parity — don't rely on caller PYTHONPATH
_LLMVP = llmvp_endpoint()
# Same loose-backstop philosophy as SWE: wall-clock governs, cycles catch a
# degenerate fast-loop. GAIA questions are smaller than SWE instances.
_MAX_CYCLES = int(os.environ.get("OURO_MAX_CYCLES", "50"))
_WALL_CLOCK_S = float(os.environ.get("OURO_GAIA_WALL_S", "900") or "900")
_TRACE = bool(os.environ.get("OURO_TRACE"))

logger = logging.getLogger(__name__)

# GAIA's official answer contract, adapted to a file channel: the grader is a
# quasi-exact match, so formatting discipline is part of the task.
_ANSWER_CONTRACT = """
Deliverable: write the FINAL ANSWER — and nothing else — to a file named
answer.txt in the working directory. Formatting rules (the grader is an
exact match, so these matter):
- A number: digits only — no commas, no units ($, %, etc.) unless the
  question explicitly asks for them.
- A string: as few words as possible, no articles, no abbreviations,
  digits in plain text unless specified otherwise.
- A comma-separated list: apply the rules above to each element.
- The bare entity only — never append a category noun ("Berkshire",
  not "Berkshire locomotive").
- A quotation or decoded text: reproduce it VERBATIM from the source —
  preserve its exact spelling even if it looks like a typo; never
  "correct" it.
- COMPUTE, NEVER ESTIMATE: perform every count, sum, difference, base
  conversion, or statistic in python (or bash) in the terminal and read
  the printed result — never do arithmetic in your head. Ad-hoc mental
  math is the leading cause of wrong numeric answers; the terminal is
  your calculator.
- When counting items against prose criteria, first translate each
  criterion into an explicit predicate and check the boundary readings
  ("3+" means >= 3; "X or higher" includes X; a compound requirement
  like "a degree in A, B, or C" is ONE criterion). Print the per-item
  verdicts and take the final count from len(), not by eye.
answer.txt must contain just the answer string on one line — no
"FINAL ANSWER:" prefix, no explanation, no quotes.
""".strip()


# Modality tool notes: absolute tool-venv paths (GAIA workspaces have no
# .venv and the PTY's ambient PATH is unpinned — the extraction_actions
# pattern). Appended to the objective, which propagates to EVERY ops prompt
# via format_mission_meta, per-mission, below the charter's cache boundary.
_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
_AUDIO_EXTS = {".mp3", ".wav", ".m4a", ".flac"}

_VL_TOOL = os.path.join(_REPO_ROOT, "tools", "fig_review")
_PDF_TOOL = os.path.join(_REPO_ROOT, "tools", "pdf_extract")


def _tool_note(file_name: str) -> tuple[str, bool, bool]:
    """(objective note for the attachment's modality, needs_vision, needs_audio).

    Sidecars (the workspace scan auto-digests flagged modalities into
    ./<file>.vltext / ./<file>.transcript.txt) provide the BASELINE reading;
    the notes keep the focused-follow-up tools available on top."""
    ext = os.path.splitext(file_name)[1].lower()
    if ext in _IMAGE_EXTS:
        return (
            "\nYou CANNOT see this image yourself. A text digest is written"
            f" to ./{file_name}.vltext — read it. For focused follow-up"
            " questions about the image, run:\n"
            f"  {_VL_TOOL}/.venv/bin/python {_VL_TOOL}/vl_inspect.py"
            f" --image ./{file_name} --question \"<what you need to know>\"\n"
            "(each call takes a minute or two).",
            True,
            False,
        )
    if ext in _AUDIO_EXTS:
        return (
            "\nYou CANNOT hear this audio yourself. A transcript is written"
            f" to ./{file_name}.transcript.txt — read it.",
            False,
            True,
        )
    if ext == ".pdf":
        return (
            "\nYou CANNOT read the PDF's bytes directly — extract it first"
            " or your reading of it will be a guess. Run:\n"
            f"  {_PDF_TOOL}/.venv/bin/python {_PDF_TOOL}/pdf_extract_one.py"
            f" --pdf ./{file_name} --out ./{file_name}.md\n"
            "(then read the .md; extraction takes a few minutes).",
            False,
            False,
        )
    return "", False, False


def build_mission(question: GaiaQuestion, workspace: str):
    """Construct the MissionState for a GAIA question (pure, testable).

    Forces the ops flow set — one task goal worked until the completion judge
    passes — with web research ON, and vision ON when the attachment is an
    image (deterministic config-time modality routing). Returns
    (mission, entry_flow).
    """
    from agent.flow_sets import get_flow_set

    file_note, vision, audio = "", False, False
    if question.has_file:
        tool_note, vision, audio = _tool_note(question.file_name)
        file_note = (
            f"\nAn attached file accompanies the question: ./{question.file_name}"
            " (already present in the working directory)."
            f"{tool_note}"
        )
    mission = build_and_save_mission(
        workspace,
        f"{question.question}{file_note}\n\n{_ANSWER_CONTRACT}",
        flow_set="ops",
        task_profile="task",
        llmvp_endpoint=_LLMVP,
        web_research=True,  # GAIA IS a web-research benchmark
        vision=vision,
        audio=audio,
    )
    return mission, get_flow_set("ops").entry_flow


def extract_answer(workspace: str) -> str:
    """Read the deterministic answer channel. Defensive against the model
    prefixing 'FINAL ANSWER:' despite the contract."""
    path = os.path.join(workspace, "answer.txt")
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            ans = f.read().strip()
    except OSError:
        return ""
    if ans.upper().startswith("FINAL ANSWER"):
        ans = ans.split(":", 1)[-1].strip()
    return ans.strip().strip('"').strip()


def _preserve(workspace: str, logs_dir: str, task_id: str) -> None:
    """Copy the mission .agent (mission.json + traces) + answer.txt for audit."""
    dst = os.path.join(logs_dir, task_id)
    try:
        preserve_agent_dir(workspace, os.path.join(dst, "ouroboros-mission"))
        ans = os.path.join(workspace, "answer.txt")
        if os.path.isfile(ans):
            os.makedirs(dst, exist_ok=True)
            shutil.copy2(ans, os.path.join(dst, "answer.txt"))
    except Exception:
        logger.warning("%s: preservation failed", task_id)


def run_question(
    question: GaiaQuestion,
    logs_dir: str,
    wall_clock_s: float | None = None,
    max_cycles: int | None = None,
) -> dict:
    """Run one mission; return the predictions row. Never raises for a
    mission-level failure — a parked/crashed mission yields whatever
    answer.txt holds (usually empty → scored wrong, honestly)."""
    from agent.effects.local import LocalEffects

    wall = wall_clock_s if wall_clock_s is not None else _WALL_CLOCK_S
    cycles = max_cycles if max_cycles is not None else _MAX_CYCLES

    workspace = tempfile.mkdtemp(prefix="ouro-gaia-")
    answer = ""
    try:
        # ISOLATION: give the workspace its own venv. Without one, LocalEffects'
        # venv_env_overrides() falls through to the ambient PATH — i.e. the REPO
        # venv — and mission `pip install`s mutate it (a campaign mission
        # downgraded httpx 0.28→0.13 and broke the terminal MCP server for every
        # subsequent question). With workspace/.venv present, the PTY uses it and
        # installs die with the workspace.
        seed_workspace_venv(workspace)
        if question.has_file and question.file_path:
            shutil.copy2(question.file_path, os.path.join(workspace, question.file_name))
        mission, entry_flow = build_mission(question, workspace)
        effects = LocalEffects(
            working_directory=workspace,
            llmvp_endpoint=_LLMVP,
            trace_thinking=_TRACE,
            trace_prompts=_TRACE,
        )

        # Shared isolated harness (agent/mission_runner.py).
        outcome = run_mission_isolated(
            effects,
            mission_id=mission.id,
            entry_flow=entry_flow,
            max_cycles=cycles,
            max_wall_clock_s=wall,
        )
        if outcome.parked:
            logger.info("%s: budget stop (parked)", question.task_id)
        elif outcome.error is not None:
            logger.warning(
                "%s: mission failed: %s: %s",
                question.task_id,
                type(outcome.error).__name__,
                outcome.error,
            )
    finally:
        answer = extract_answer(workspace)
        _preserve(workspace, logs_dir, question.task_id)
        shutil.rmtree(workspace, ignore_errors=True)

    return {
        "task_id": question.task_id,
        "model_answer": answer,
        "level": question.level,
    }
