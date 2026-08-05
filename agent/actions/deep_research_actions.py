"""Deep-research loop v1 — the parallel, stateless generalization of deep_search.

deep_search (flows/shared/deep_search.cue) is a SERIAL reflect-and-refine loop:
one memoryful session, one query per round, the session IS the evidence ledger.
That shape wastes the batched engine — measured on the swarm-class study
(2026-07-24): the parallel fraction of a swarm code run was ~7%; everything
serial ran at the single-stream floor. Research is the workload that escapes
this: evidence gathering is seam-free and embarrassingly parallel.

deep_research therefore inverts the substrate decisions (v2 shape, Luke
2026-07-24: maximize swarm work per API call — search calls are the scarce
politeness-bound resource, GPU streams are abundant):

    decompose → [ select (proposer burst → panel vote within search budget)
                  → wave (B respectful searches → per-HIT extract burst)
                  → verify (adversarial per-finding skeptic burst)
                  → merge_reflect ]* → synthesize

  - NO memoryful session anywhere — every inference is a stateless completion
    (the batched engine's native currency; no seat pinning, per-request
    reasoning levels work);
  - the evidence ledger is explicit context data (a list of finding dicts),
    not session history — merges are data operations, seam-free by shape;
  - select spends cheap GPU work to make each search count: a proposer
    burst derives candidate queries through distinct lenses, then a small
    stateless panel votes the best B within the search budget;
  - the wave dispatches only those B searches (politeness semaphore), then
    fans out one extract completion PER HIT — single-source attribution,
    5x the GPU parallelism per API call — sized by the shared pool-fit
    gate (agent/actions/fanout.py);
  - verify is the adversarial pass (debate-AB lesson: grounded refutation,
    never persona debate): one skeptic completion per finding, handed the
    claim AND its source text, refute-minded, three-way verdict
    (supported / unsupported / contradicted — a single refute-biased
    kill vote would false-kill); contradicted findings are excluded from
    synthesis, unsupported ones survive flagged. Vacuous-skip: findings
    with no checkable claim are never "verified" (the curator trap);
  - merge_reflect is ONE inference per wave: dedup + gap analysis over the
    verdict-annotated ledger, feeding the next select's candidate pool;
  - synthesize grounds the final summary in surviving findings with
    inline citations.

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
from agent.loader import load_prompt_text

logger = logging.getLogger(__name__)

# Wave budget — keep in agreement with deep_research.cue's check_budget rule.
MAX_RESEARCH_WAVES = 3
MAX_ANGLES_PER_WAVE = 8  # hard cap on selected queries per wave
SEARCH_BUDGET_DEFAULT = 6  # API calls per wave (the panel selects within this)
N_PROPOSERS = 3  # query-derivation workers per select stage (one per lens)
PANEL_VOTERS = 3  # stateless voters picking the wave's searches
NUM_RESULTS = 5  # Exa hits per query
_EXA_CONCURRENCY = 3  # politeness bound on the search API (not the GPU)
# Extract/verify prompts are bounded by construction (one 1500-char hit +
# template) — the per-worker draw estimate the pool-fit gate sizes against.
_EXTRACT_DRAW_EST = estimate_draw("x" * 2600, gen_margin=384)
_EXTRACT_CFG = {"reasoning": "low", "temperature": "t*0.2", "max_tokens": 384}
_VERIFY_CFG = {"reasoning": "low", "temperature": "t*0.2", "max_tokens": 384}

DECOMPOSE_PROMPT = load_prompt_text("deep_research/decompose")

PROPOSE_PROMPT = load_prompt_text("deep_research/propose")

_PROPOSER_LENSES = (
    "mechanisms, definitions, and primary sources (specs, official docs, "
    "original papers)",
    "comparisons, failure modes, counter-evidence, and recent developments",
    # The soft lens (Luke 2026-07-24): gpt-oss is the engineer of our
    # models, not the content curator — without an explicit lens it
    # under-proposes the human side of a question.
    "community experience, popular usage, UX and ergonomics, adoption "
    "stories, practitioner opinions (forums, reviews, real-world reports)",
)

VOTE_PROMPT = load_prompt_text("deep_research/vote")

EXTRACT_PROMPT = load_prompt_text("deep_research/extract")

VERIFY_PROMPT = load_prompt_text("deep_research/verify")

MERGE_REFLECT_PROMPT = load_prompt_text("deep_research/merge_reflect")

SYNTHESIZE_PROMPT = load_prompt_text("deep_research/synthesize")


def _bounded(s: str, n: int) -> str:
    s = (s or "").strip()
    return s if len(s) <= n else s[:n] + " …[truncated]"


def _ledger_block(
    ledger: list[dict], limit: int = 60, include_contradicted: bool = True
) -> str:
    """Render findings with verification markers. Contradicted entries can
    be excluded (synthesize) or shown flagged (merge_reflect — a
    contradiction is gap-worthy signal)."""
    lines = []
    for f in ledger[-limit:]:
        verdict = f.get("verdict", "")
        if verdict == "contradicted" and not include_contradicted:
            continue
        mark = {
            "supported": "",
            "unsupported": " [UNVERIFIED — source does not fully support]",
            "contradicted": " [CONTRADICTED by its own source]",
        }.get(verdict, "")
        lines.append(
            f"- [{f.get('query', '')}] {_bounded(f.get('fact', ''), 900)}{mark}"
        )
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

    logger.info("deep_research: decomposed into %d candidate angles", len(angles))
    return StepOutput(
        result={"research_started": True, "n_angles": len(angles)},
        observations=f"deep_research: {len(angles)} candidate angles",
        context_updates={
            "research_brief": _bounded(brief, 3000),
            # Candidates for the select panel — not yet the wave's queries.
            "research_candidates": angles,
            "research_angles": [],
            "research_ledger": [],
            "research_wave_n": 0,
            "research_queries_run": [],
            "research_summary": "",
            "research_sufficient": False,
        },
    )


async def action_research_select(step_input: StepInput) -> StepOutput:
    """select: spend cheap GPU work to make each search count — a proposer
    burst derives candidate queries through distinct lenses, then a small
    stateless panel votes the best B within the search budget.

    Candidates = carried pool (decompose / reflect gaps) + proposer output,
    deduped against queries already run. Degrades gracefully: no effects or
    a fully-failed panel → first-B candidates in order.

    Context: research_brief, research_candidates, research_ledger,
    research_queries_run. Params: search_budget (default 6).
    Publishes: research_angles (the selected queries), research_candidates.
    """
    effects = step_input.effects
    brief = str(step_input.context.get("research_brief", "") or "")
    ledger = list(step_input.context.get("research_ledger", []) or [])
    queries_run = {
        q.strip().lower()
        for q in (step_input.context.get("research_queries_run") or [])
        if str(q).strip()
    }
    budget = min(
        int(step_input.params.get("search_budget", SEARCH_BUDGET_DEFAULT) or 0)
        or SEARCH_BUDGET_DEFAULT,
        MAX_ANGLES_PER_WAVE,
    )
    candidates: list[str] = []

    def _add(q: str) -> None:
        qs = str(q).strip()
        if qs and qs.lower() not in queries_run and qs not in candidates:
            candidates.append(qs)

    for q in step_input.context.get("research_candidates") or []:
        _add(q)

    if effects:
        ledger_digest = _ledger_block(ledger, limit=20)

        async def propose(lens: str) -> list[str]:
            try:
                res = await effects.run_inference(
                    PROPOSE_PROMPT.format(
                        lens=lens,
                        brief=brief,
                        queries_run="\n".join(sorted(queries_run)) or "(none)",
                        ledger=ledger_digest,
                        n=MAX_ANGLES_PER_WAVE,
                    ),
                    {"temperature": "t*0.6", "max_tokens": 768},
                )
                parsed = parse_llm_json(getattr(res, "text", None) or "")
                if isinstance(parsed, dict):
                    return [str(q) for q in (parsed.get("queries") or [])]
            except Exception as e:  # noqa: BLE001
                logger.warning("deep_research proposer failed: %s", e)
            return []

        for qs in await asyncio.gather(
            *(
                propose(_PROPOSER_LENSES[i % len(_PROPOSER_LENSES)])
                for i in range(N_PROPOSERS)
            )
        ):
            for q in qs:
                _add(q)

    selected: list[str] = []
    if effects and len(candidates) > budget:
        numbered = "\n".join(f"{i + 1}. {q}" for i, q in enumerate(candidates))
        ledger_digest = _ledger_block(ledger, limit=20)

        async def vote() -> list[int]:
            try:
                res = await effects.run_inference(
                    VOTE_PROMPT.format(
                        budget=budget,
                        brief=brief,
                        ledger=ledger_digest,
                        candidates=numbered,
                    ),
                    {"temperature": "t*0.3", "max_tokens": 256},
                )
                parsed = parse_llm_json(getattr(res, "text", None) or "")
                if isinstance(parsed, dict):
                    return [
                        int(i)
                        for i in (parsed.get("picks") or [])
                        if str(i).strip().isdigit() or isinstance(i, int)
                    ]
            except Exception as e:  # noqa: BLE001
                logger.warning("deep_research voter failed: %s", e)
            return []

        ballots = await asyncio.gather(*(vote() for _ in range(PANEL_VOTERS)))
        approvals: dict[int, int] = {}
        for ballot in ballots:
            for pick in ballot[:budget]:
                if 1 <= pick <= len(candidates):
                    approvals[pick - 1] = approvals.get(pick - 1, 0) + 1
        # Approval count, ties broken by candidate order (carried pool first).
        ranked = sorted(range(len(candidates)), key=lambda i: (-approvals.get(i, 0), i))
        selected = [candidates[i] for i in ranked[:budget]]
    if not selected:
        selected = candidates[:budget]

    obs = (
        f"deep_research select: {len(selected)}/{len(candidates)} queries "
        f"chosen (budget {budget})"
    )
    logger.info(obs)
    return StepOutput(
        result={"n_selected": len(selected)},
        observations=obs,
        context_updates={
            "research_angles": selected,
            "research_candidates": [],
        },
    )


async def action_research_wave(step_input: StepInput) -> StepOutput:
    """wave: dispatch the selected searches respectfully, then fan out one
    stateless extract completion PER HIT (single-source attribution — the
    granularity the adversarial verify pass needs) and append findings to
    the ledger.

    The GPU-side concurrency is sized by the shared pool-fit gate (server
    kvPoolTokens, static fallback); the search API gets its own small
    politeness semaphore. A failed search or extract degrades to a bounded
    note — one bad angle never fails the wave. Raw hit text is published
    TRANSIENTLY (research_wave_hits) for the verify pass and cleared there.

    Context: research_angles, research_ledger, research_wave_n.
    Publishes: research_ledger, research_wave_n, research_queries_run,
    research_wave_hits.
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
            context_updates={"research_wave_n": wave_n, "research_wave_hits": {}},
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
            context_updates={"research_wave_n": wave_n, "research_wave_hits": {}},
        )

    max_workers = int(step_input.params.get("max_workers", 32) or 32)
    pool_budget = int(step_input.params.get("pool_budget", 131072) or 131072)
    exa_sem = asyncio.Semaphore(_EXA_CONCURRENCY)

    working_directory = str(step_input.inputs.get("working_directory", "") or "")
    perf = FanoutPerf(working_directory)
    burst_t0 = time.monotonic()

    # Stage 1: the B respectful searches (API-bound, small semaphore).
    async def search(angle: str) -> tuple[str, list[dict], str]:
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
            return angle, hits, ""
        except Exception as e:  # noqa: BLE001
            return angle, [], f"(search failed: {_bounded(str(e), 200)})"

    searched = await asyncio.gather(*(search(a) for a in angles))

    # Stage 2: one extract completion PER HIT — the GPU burst.
    units: list[tuple[str, dict]] = []
    findings: list[dict] = []
    wave_hits: dict[str, str] = {}
    for angle, hits, err in searched:
        if not hits:
            findings.append(
                {
                    "query": angle,
                    "url": "",
                    "fact": err or "INSUFFICIENT (no usable hits)",
                    "ok": False,
                    "wave": wave_n,
                    "verdict": "",
                }
            )
            continue
        for h in hits:
            units.append((angle, h))

    decision = await pool_fit_width(
        effects,
        [_EXTRACT_DRAW_EST] * max(len(units), 1),
        max_workers=max_workers,
        pool_budget_fallback=pool_budget,
        label="deep_research",
    )
    infer_sem = asyncio.Semaphore(max(decision.width, 1))
    perf.row(
        {
            "event": "burst",
            "kind": "deep_research_wave",
            "wave": wave_n,
            "t0_epoch": round(time.time(), 3),
            "angles": len(angles),
            "hits": len(units),
            "max_workers": max_workers,
            **decision.perf_fields(),
        }
    )

    async def extract(angle: str, hit: dict) -> dict:
        t_submit = round(time.monotonic() - burst_t0, 2)
        content = _bounded(hit["content"], 1500)
        finding = {
            "query": angle,
            "url": hit["url"],
            "fact": "",
            "ok": False,
            "wave": wave_n,
            "verdict": "",
        }
        tokens = 0
        try:
            async with infer_sem:
                res = await effects.run_inference(
                    EXTRACT_PROMPT.format(
                        query=angle,
                        url=hit["url"],
                        title=hit["title"],
                        content=content,
                    ),
                    dict(_EXTRACT_CFG),
                )
            fact = (getattr(res, "text", None) or "").strip()
            tokens = int(getattr(res, "tokens_generated", 0) or 0)
            if fact:
                finding["fact"] = _bounded(fact, 1200)
                finding["ok"] = "INSUFFICIENT" not in fact[:64]
        except Exception as e:  # noqa: BLE001
            logger.warning("extract failed for %r/%s: %s", angle, hit["url"], e)
        if not finding["fact"]:
            finding["fact"] = "(extract unavailable) " + _bounded(hit["content"], 400)
        if finding["ok"]:
            wave_hits[f"{angle}\n{hit['url']}"] = content
        perf.row(
            {
                "event": "worker",
                "kind": "deep_research_hit",
                "wave": wave_n,
                "query": _bounded(angle, 120),
                "url": _bounded(hit["url"], 120),
                "ok": finding["ok"],
                "generated_tokens": tokens,
                "t_submit": t_submit,
                "t_done": round(time.monotonic() - burst_t0, 2),
            }
        )
        return finding

    findings.extend(await asyncio.gather(*(extract(a, h) for a, h in units)))
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
        f"{n_ok}/{len(findings)} hit-findings from {len(angles)} searches "
        f"({decision.width} concurrent [{decision.gate}], "
        f"budget={decision.budget_source})"
    )
    logger.info(obs)
    return StepOutput(
        result={"wave_ok": n_ok > 0, "n_findings": len(findings)},
        observations=obs,
        context_updates={
            "research_ledger": ledger,
            "research_wave_n": wave_n,
            "research_queries_run": queries_run,
            "research_wave_hits": wave_hits,
        },
    )


async def action_research_verify(step_input: StepInput) -> StepOutput:
    """verify: the adversarial pass — one grounded skeptic completion per
    fresh finding, handed the claim AND the source text it was extracted
    from, prompted to REFUTE. Three-way verdict: supported (quote the
    span) / unsupported (flagged, survives) / contradicted (excluded from
    synthesis). Vacuous-skip: findings with no checkable claim (search
    failures, INSUFFICIENT extracts) are never verified — a gate must fail
    on zero checkable items, not pass.

    Grounded refutation, not debate: the skeptic checks extraction
    fidelity against provided text (mechanical), which is also what keeps
    same-model verification honest.

    Context: research_ledger, research_wave_n, research_wave_hits.
    Publishes: research_ledger, research_wave_hits (cleared).
    """
    effects = step_input.effects
    ledger = list(step_input.context.get("research_ledger", []) or [])
    wave_n = int(step_input.context.get("research_wave_n", 0) or 0)
    wave_hits = dict(step_input.context.get("research_wave_hits", {}) or {})
    fresh = [
        f
        for f in ledger
        if f.get("wave") == wave_n
        and f.get("ok")
        and not f.get("verdict")
        and f"{f.get('query', '')}\n{f.get('url', '')}" in wave_hits
    ]
    if not effects or not fresh:
        return StepOutput(
            result={"n_verified": 0, "n_contradicted": 0},
            observations="deep_research verify: no checkable findings this wave",
            context_updates={"research_wave_hits": {}},
        )

    max_workers = int(step_input.params.get("max_workers", 32) or 32)
    pool_budget = int(step_input.params.get("pool_budget", 131072) or 131072)
    decision = await pool_fit_width(
        effects,
        [_EXTRACT_DRAW_EST] * len(fresh),
        max_workers=max_workers,
        pool_budget_fallback=pool_budget,
        label="deep_research_verify",
    )
    sem = asyncio.Semaphore(max(decision.width, 1))
    working_directory = str(step_input.inputs.get("working_directory", "") or "")
    perf = FanoutPerf(working_directory)
    burst_t0 = time.monotonic()
    perf.row(
        {
            "event": "burst",
            "kind": "deep_research_verify",
            "wave": wave_n,
            "t0_epoch": round(time.time(), 3),
            "findings": len(fresh),
            **decision.perf_fields(),
        }
    )

    async def skeptic(finding: dict) -> None:
        key = f"{finding.get('query', '')}\n{finding.get('url', '')}"
        verdict, note = "unsupported", "verifier unavailable"
        try:
            async with sem:
                res = await effects.run_inference(
                    VERIFY_PROMPT.format(
                        claim=finding.get("fact", ""),
                        url=finding.get("url", ""),
                        content=wave_hits.get(key, ""),
                    ),
                    dict(_VERIFY_CFG),
                )
            parsed = parse_llm_json(getattr(res, "text", None) or "")
            if isinstance(parsed, dict):
                v = str(parsed.get("verdict", "") or "").strip().lower()
                if v in ("supported", "unsupported", "contradicted"):
                    verdict = v
                    note = _bounded(str(parsed.get("note", "") or ""), 400)
        except Exception as e:  # noqa: BLE001
            logger.warning("verify failed for %s: %s", finding.get("url", ""), e)
        finding["verdict"] = verdict
        finding["verify_note"] = note
        perf.row(
            {
                "event": "worker",
                "kind": "deep_research_verdict",
                "wave": wave_n,
                "url": _bounded(finding.get("url", ""), 120),
                "ok": verdict == "supported",
                "verdict": verdict,
                "t_done": round(time.monotonic() - burst_t0, 2),
            }
        )

    await asyncio.gather(*(skeptic(f) for f in fresh))
    n_contra = sum(1 for f in fresh if f["verdict"] == "contradicted")
    n_unsup = sum(1 for f in fresh if f["verdict"] == "unsupported")
    perf.row(
        {
            "event": "burst_done",
            "kind": "deep_research_verify",
            "wave": wave_n,
            "t_done": round(time.monotonic() - burst_t0, 2),
            "ok": len(fresh) - n_contra - n_unsup,
            "failed": n_contra,
        }
    )
    obs = (
        f"deep_research verify: {len(fresh)} findings — "
        f"{len(fresh) - n_contra - n_unsup} supported, {n_unsup} unsupported, "
        f"{n_contra} contradicted ({decision.width} concurrent, "
        f"budget={decision.budget_source})"
    )
    logger.info(obs)
    return StepOutput(
        result={"n_verified": len(fresh), "n_contradicted": n_contra},
        observations=obs,
        context_updates={
            "research_ledger": ledger,
            "research_wave_hits": {},  # raw text never travels further
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
            # Gaps feed the next select stage's candidate pool (the panel
            # re-judges them against fresh proposer output).
            "research_candidates": gaps,
            "research_angles": [],
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
                SYNTHESIZE_PROMPT.format(
                    brief=brief,
                    # Contradicted findings are excluded outright; unsupported
                    # ones arrive flagged so the summary weights them honestly.
                    ledger=_ledger_block(ledger, include_contradicted=False),
                ),
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
