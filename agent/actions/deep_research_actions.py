"""Deep-research loop v1 — the parallel, stateless generalization of deep_search.

deep_search (flows/shared/deep_search.cue) is a SERIAL reflect-and-refine loop:
one memoryful session, one query per round, the session IS the evidence ledger.
That shape wastes the batched engine — measured on the swarm-class study
(2026-07-24): the parallel fraction of a swarm code run was ~7%; everything
serial ran at the single-stream floor. Research is the workload that escapes
this: evidence gathering is seam-free and embarrassingly parallel.

deep_research therefore inverts the substrate decisions:

    decompose → [ wave (FAN-OUT: search+condense per angle) → merge_reflect ]* → synthesize

  - NO memoryful session anywhere — every inference is a stateless completion
    (the batched engine's native currency; no seat pinning, per-request
    reasoning levels work);
  - the evidence ledger is explicit context data (a list of finding dicts),
    not session history — merges are data operations, seam-free by shape;
  - each wave fans out ALL open angles concurrently: per-angle Exa search
    (small politeness semaphore) then a stateless low-reasoning condense
    completion (the deep_search condense discipline, parallelized) — sized
    by the shared pool-fit gate (agent/actions/fanout.py);
  - merge_reflect is ONE inference per wave (not per query): dedup + gap
    analysis over the ledger, emitting the next wave's angles or sufficiency;
  - synthesize grounds the final summary in the ledger with inline citations.

Contract parity with deep_search: takes ``brief``, returns
``research_summary`` + ``research_sufficient`` + ``queries_run`` — callers can
choose depth (deep_search: one fact; deep_research: a braced survey).

Gating mirrors deep_search: ``web_research`` off or a missing ``~/.exa_key``
declines cleanly (empty summary, never errors the caller).
"""

from __future__ import annotations

import asyncio
import logging
import time

from agent.actions.fanout import FanoutPerf, estimate_draw, pool_fit_width
from agent.actions.refinement_actions import _extract_exa_hits
from agent.llm_json import parse_llm_json
from agent.models import StepInput, StepOutput

logger = logging.getLogger(__name__)

# Wave budget — keep in agreement with deep_research.cue's check_budget rule.
MAX_RESEARCH_WAVES = 3
MAX_ANGLES_PER_WAVE = 8  # fan width per wave (24 queries max across 3 waves)
NUM_RESULTS = 5  # Exa hits per query
_CONDENSE_HITS = 3  # top-K hits fed to each condense completion
_EXA_CONCURRENCY = 3  # politeness bound on the search API (not the GPU)
# Condense prompts are bounded by construction (3 hits x 1500 chars + template)
# — this is the per-worker draw estimate the pool-fit gate sizes against.
_CONDENSE_DRAW_EST = estimate_draw("x" * 7000, gen_margin=512)
_CONDENSE_CFG = {"reasoning": "low", "temperature": "t*0.2", "max_tokens": 512}

DECOMPOSE_PROMPT = (
    "You are planning a parallel research sweep. Decompose the brief below "
    "into independent, concretely searchable questions — each one answerable "
    "by a focused web search, none depending on another's answer. Prefer "
    "distinct ANGLES (mechanisms, comparisons, failure modes, primary "
    "sources, recent developments) over paraphrases of the whole brief.\n\n"
    "BRIEF:\n{brief}\n\n"
    "Return ONLY a fenced JSON object: "
    '{{"angles": ["question 1", "question 2", ...]}} '
    "with 3-{max_angles} questions."
)

CONDENSE_PROMPT = (
    "You are distilling raw web-search results into the answer-bearing "
    "fact(s) for a specific question. Be terse and literal — extract, do not "
    "reason.\n\n"
    "QUESTION (the gap being filled):\n{query}\n\n"
    "RAW RESULTS:\n{hits}\n\n"
    "Return 1-4 sentences capturing ONLY the concrete fact(s) that answer the "
    "question, each with its source URL in parentheses. If the results do NOT "
    "answer it, reply exactly: INSUFFICIENT. If sources conflict, say so and "
    "give each side with its URL. No preamble, no restating the question."
)

MERGE_REFLECT_PROMPT = (
    "You are auditing a research ledger mid-sweep. The brief and the findings "
    "so far are below. Judge coverage and name what is still missing.\n\n"
    "BRIEF:\n{brief}\n\n"
    "FINDINGS ({n} so far):\n{ledger}\n\n"
    "Return ONLY a fenced JSON object:\n"
    '{{"sufficient": true|false, "gaps": ["searchable question", ...]}}\n\n'
    "sufficient = true when the findings already answer the brief (gaps then "
    "empty). Otherwise list up to {max_angles} NEW independent searchable "
    "questions targeting the actual gaps — never repeat a question already "
    "answered, and drop angles that returned INSUFFICIENT twice."
)

SYNTHESIZE_PROMPT = (
    "Conclude the research sweep. Ground every claim in the findings below — "
    "cite source URLs inline.\n\n"
    "BRIEF:\n{brief}\n\n"
    "FINDINGS:\n{ledger}\n\n"
    "Return ONLY a fenced JSON object:\n"
    '{{"research_summary": "...", "sufficient": true|false}}\n\n'
    "research_summary answers the brief in a structured, dense form "
    "(paragraphs or a compact list; cite URLs inline; note conflicts and "
    "open questions honestly). sufficient = true only if the findings "
    "actually answer the brief."
)


def _bounded(s: str, n: int) -> str:
    s = (s or "").strip()
    return s if len(s) <= n else s[:n] + " …[truncated]"


def _ledger_block(ledger: list[dict], limit: int = 40) -> str:
    lines = []
    for f in ledger[-limit:]:
        lines.append(f"- [{f.get('query', '')}] {_bounded(f.get('fact', ''), 900)}")
    return "\n".join(lines) if lines else "(none)"


async def action_research_decompose(step_input: StepInput) -> StepOutput:
    """decompose: one stateless inference — brief → independent angle questions.

    Declines cleanly (research_started=False) when web_research is off, there
    are no effects, or the brief is empty — the flow routes to unavailable.

    Inputs: brief. Publishes: research_brief, research_angles, research_ledger,
    research_wave_n, research_queries_run, research_summary, research_sufficient.
    """
    effects = step_input.effects
    empty = {
        "research_summary": "",
        "research_sufficient": False,
        "queries_run": [],
    }
    if not effects:
        return StepOutput(
            result={"research_started": False},
            observations="No effects interface — cannot research",
            context_updates=dict(empty),
        )
    try:
        mission = await effects.load_mission()
        web_ok = bool(getattr(getattr(mission, "config", None), "web_research", True))
    except Exception:  # noqa: BLE001 — no mission → assume enabled, key backstop later
        web_ok = True
    if not web_ok:
        return StepOutput(
            result={"research_started": False},
            observations="web_research disabled — deep_research unavailable",
            context_updates=dict(empty),
        )
    brief = str((step_input.inputs or {}).get("brief", "") or "").strip()
    if not brief:
        return StepOutput(
            result={"research_started": False},
            observations="No brief provided — nothing to research",
            context_updates=dict(empty),
        )

    angles: list[str] = []
    try:
        res = await effects.run_inference(
            DECOMPOSE_PROMPT.format(
                brief=_bounded(brief, 3000), max_angles=MAX_ANGLES_PER_WAVE
            ),
            {"temperature": "t*0.4", "max_tokens": 1024},
        )
        parsed = parse_llm_json(getattr(res, "text", None) or "")
        if isinstance(parsed, dict):
            angles = [
                str(a).strip() for a in (parsed.get("angles") or []) if str(a).strip()
            ][:MAX_ANGLES_PER_WAVE]
    except Exception as e:  # noqa: BLE001
        logger.warning("deep_research decompose failed: %s", e)
    if not angles:
        # Degraded start: research the brief itself as a single angle rather
        # than dying on a parse miss.
        angles = [_bounded(brief, 300)]

    logger.info("deep_research: decomposed into %d angles", len(angles))
    return StepOutput(
        result={"research_started": True, "n_angles": len(angles)},
        observations=f"deep_research: {len(angles)} independent angles",
        context_updates={
            "research_brief": _bounded(brief, 3000),
            "research_angles": angles,
            "research_ledger": [],
            "research_wave_n": 0,
            "research_queries_run": [],
            "research_summary": "",
            "research_sufficient": False,
        },
    )


async def action_research_wave(step_input: StepInput) -> StepOutput:
    """wave: fan out ALL open angles concurrently — per-angle Exa search then
    a stateless condense completion; append findings to the ledger.

    The GPU-side concurrency is sized by the shared pool-fit gate (server
    kvPoolTokens, static fallback); the search API gets its own small
    politeness semaphore. A failed search or condense degrades to a bounded
    note — one bad angle never fails the wave.

    Context: research_angles, research_ledger, research_wave_n.
    Publishes: research_ledger, research_wave_n, research_queries_run.
    """
    effects = step_input.effects
    angles = [
        str(a).strip()
        for a in (step_input.context.get("research_angles") or [])
        if str(a).strip()
    ][:MAX_ANGLES_PER_WAVE]
    ledger = list(step_input.context.get("research_ledger", []) or [])
    wave_n = int(step_input.context.get("research_wave_n", 0) or 0) + 1
    queries_run = list(step_input.context.get("research_queries_run", []) or [])

    if not effects or not angles:
        return StepOutput(
            result={"wave_ok": False, "n_findings": 0},
            observations="deep_research wave: no angles to run",
            context_updates={"research_wave_n": wave_n},
        )

    # One Exa connection for the wave; a missing key degrades the whole wave
    # cleanly (the synthesize step then reports on whatever the ledger holds).
    conn_id = None
    try:
        conn_id = await effects.mcp_connect("exa")
    except Exception as e:  # noqa: BLE001
        logger.warning("deep_research wave %d: search unavailable (%s)", wave_n, e)
        return StepOutput(
            result={"wave_ok": False, "n_findings": 0},
            observations=f"deep_research wave {wave_n}: search unavailable",
            context_updates={"research_wave_n": wave_n},
        )

    max_workers = int(step_input.params.get("max_workers", 32) or 32)
    pool_budget = int(step_input.params.get("pool_budget", 131072) or 131072)
    decision = await pool_fit_width(
        effects,
        [_CONDENSE_DRAW_EST] * len(angles),
        max_workers=max_workers,
        pool_budget_fallback=pool_budget,
        label="deep_research",
    )
    infer_sem = asyncio.Semaphore(max(decision.width, 1))
    exa_sem = asyncio.Semaphore(_EXA_CONCURRENCY)

    working_directory = str(step_input.inputs.get("working_directory", "") or "")
    perf = FanoutPerf(working_directory)
    burst_t0 = time.monotonic()
    perf.row(
        {
            "event": "burst",
            "kind": "deep_research_wave",
            "wave": wave_n,
            "t0_epoch": round(time.time(), 3),
            "angles": len(angles),
            "max_workers": max_workers,
            **decision.perf_fields(),
        }
    )

    async def run_angle(angle: str) -> dict:
        t_submit = round(time.monotonic() - burst_t0, 2)
        finding = {"query": angle, "fact": "", "ok": False, "wave": wave_n}
        hits: list[dict] = []
        try:
            async with exa_sem:
                mcp_result = await effects.mcp_call_tool(
                    conn_id,
                    "web_search_exa",
                    {"query": angle, "numResults": NUM_RESULTS},
                )
            hits = [
                {
                    "url": h.get("url", ""),
                    "title": h.get("title", ""),
                    "content": h.get("content", ""),
                }
                for h in _extract_exa_hits(mcp_result)
                if h.get("content", "").strip()
            ]
        except Exception as e:  # noqa: BLE001
            finding["fact"] = f"(search failed: {_bounded(str(e), 200)})"
        tokens = 0
        if hits:
            hits_block = "\n\n---\n\n".join(
                f"[{h['url']}] {h['title']}\n{_bounded(h['content'], 1500)}"
                for h in hits[:_CONDENSE_HITS]
            )
            prompt = CONDENSE_PROMPT.format(query=angle, hits=hits_block)
            try:
                async with infer_sem:
                    res = await effects.run_inference(prompt, dict(_CONDENSE_CFG))
                fact = (getattr(res, "text", None) or "").strip()
                tokens = int(getattr(res, "tokens_generated", 0) or 0)
                if fact:
                    finding["fact"] = _bounded(fact, 2000)
                    finding["ok"] = "INSUFFICIENT" not in fact[:64]
            except Exception as e:  # noqa: BLE001
                logger.warning("condense failed for %r: %s", angle, e)
            if not finding["fact"]:
                finding["fact"] = "(condense unavailable) " + _bounded(
                    hits[0]["content"], 600
                )
        elif not finding["fact"]:
            finding["fact"] = "INSUFFICIENT (no usable hits)"
        perf.row(
            {
                "event": "worker",
                "kind": "deep_research_angle",
                "wave": wave_n,
                "query": _bounded(angle, 120),
                "ok": finding["ok"],
                "hits": len(hits),
                "generated_tokens": tokens,
                "t_submit": t_submit,
                "t_done": round(time.monotonic() - burst_t0, 2),
            }
        )
        return finding

    findings = await asyncio.gather(*(run_angle(a) for a in angles))
    ledger.extend(findings)
    queries_run.extend(angles)
    n_ok = sum(1 for f in findings if f["ok"])
    perf.row(
        {
            "event": "burst_done",
            "kind": "deep_research_wave",
            "wave": wave_n,
            "t_done": round(time.monotonic() - burst_t0, 2),
            "ok": n_ok,
            "failed": len(findings) - n_ok,
        }
    )
    obs = (
        f"deep_research wave {wave_n}/{MAX_RESEARCH_WAVES}: "
        f"{n_ok}/{len(angles)} angles answered ({decision.width} concurrent "
        f"[{decision.gate}], budget={decision.budget_source})"
    )
    logger.info(obs)
    return StepOutput(
        result={"wave_ok": n_ok > 0, "n_findings": len(findings)},
        observations=obs,
        context_updates={
            "research_ledger": ledger,
            "research_wave_n": wave_n,
            "research_queries_run": queries_run,
        },
    )


async def action_research_merge_reflect(step_input: StepInput) -> StepOutput:
    """merge_reflect: ONE stateless inference per wave — audit the ledger
    against the brief, emit sufficiency + the next wave's gap questions.

    Context: research_brief, research_ledger. Publishes: research_angles
    (the gaps), research_sufficient.
    """
    effects = step_input.effects
    brief = str(step_input.context.get("research_brief", "") or "")
    ledger = list(step_input.context.get("research_ledger", []) or [])
    sufficient = False
    gaps: list[str] = []
    if effects and ledger:
        try:
            res = await effects.run_inference(
                MERGE_REFLECT_PROMPT.format(
                    brief=brief,
                    n=len(ledger),
                    ledger=_ledger_block(ledger),
                    max_angles=MAX_ANGLES_PER_WAVE,
                ),
                {"temperature": "t*0.3", "max_tokens": 1024},
            )
            parsed = parse_llm_json(getattr(res, "text", None) or "")
            if isinstance(parsed, dict):
                sufficient = bool(parsed.get("sufficient", False))
                gaps = [
                    str(g).strip() for g in (parsed.get("gaps") or []) if str(g).strip()
                ][:MAX_ANGLES_PER_WAVE]
        except Exception as e:  # noqa: BLE001
            logger.warning("deep_research merge_reflect failed: %s", e)
    if sufficient:
        gaps = []
    return StepOutput(
        result={"sufficient": sufficient, "n_gaps": len(gaps)},
        observations=(
            f"deep_research reflect: sufficient={sufficient}, {len(gaps)} gaps"
        ),
        context_updates={
            "research_angles": gaps,
            "research_sufficient": sufficient,
        },
    )


async def action_research_synthesize(step_input: StepInput) -> StepOutput:
    """synthesize: one stateless inference — ledger → grounded research_summary.

    Context: research_brief, research_ledger, research_queries_run.
    Publishes: research_summary, research_sufficient, queries_run.
    """
    effects = step_input.effects
    brief = str(step_input.context.get("research_brief", "") or "")
    ledger = list(step_input.context.get("research_ledger", []) or [])
    queries_run = list(step_input.context.get("research_queries_run", []) or [])
    summary = ""
    sufficient = False
    if effects and ledger:
        try:
            res = await effects.run_inference(
                SYNTHESIZE_PROMPT.format(brief=brief, ledger=_ledger_block(ledger)),
                {"temperature": "t*0.3", "max_tokens": 4096},
            )
            parsed = parse_llm_json(getattr(res, "text", None) or "")
            if isinstance(parsed, dict):
                summary = str(parsed.get("research_summary", "") or "").strip()
                sufficient = bool(parsed.get("sufficient", False))
        except Exception as e:  # noqa: BLE001
            logger.warning("deep_research synthesize failed: %s", e)
    return StepOutput(
        result={"sufficient": sufficient},
        observations=(
            f"deep_research concluded: {len(queries_run)} queries over "
            f"{len(ledger)} findings, sufficient={sufficient}, "
            f"{len(summary)} chars"
        ),
        context_updates={
            "research_summary": summary,
            "research_sufficient": sufficient,
            "queries_run": queries_run,
        },
    )
