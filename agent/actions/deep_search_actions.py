"""Deep-search loop v1 — a reusable reflect-and-refine web-search primitive.

The 5-agent field survey (memory: deep-search-loop-design) concluded that
open-ended web problem-solving wants a single-thread reflect-and-refine ReAct
loop (NOT Tree-of-Thoughts), with condense-before-fold-back and a
sufficiency-gated stop. This module is the action layer for that loop
(`flows/shared/deep_search.cue`):

    open_session → [ reflect → search → condense ]* → synthesize

  - reflect names the SPECIFIC missing fact and emits a focused query (or done);
  - search runs one Exa query (reusing the refinement_actions MCP path);
  - condense distills raw hits to the answer-bearing fact BEFORE folding it into
    the session — raw pages never enter the session (the research's #1 borrow for
    our bounded-context / over-reasoning risk);
  - synthesize produces the final research_summary the caller consumes.

The loop takes a generic ``brief`` and returns ``research_summary`` — no
caller-specific coupling, so escalation (the first wiring), the scraper, and the
design phase can all invoke it. Routing (internal-first) and parallel best-of-N
are deferred until the shape proves out.

Gating: ``web_research`` off (hermetic SWE) or a missing ``~/.exa_key`` → the
session declines cleanly to an empty summary (never errors the caller).
"""

from __future__ import annotations

import logging

from agent.actions.refinement_actions import _extract_exa_hits
from agent.llm_json import parse_llm_json
from agent.models import StepInput, StepOutput
from agent.session_injections import queue as queue_injection
from agent.loader import load_prompt_text

logger = logging.getLogger(__name__)

# Own round budget — a SEPARATE constant from escalation's turn cap. A round =
# one reflect→search→condense cycle. Kept in agreement with deep_search.cue's
# check_budget rule and the reflect_instruction template ("round N of 4").
MAX_SEARCH_ROUNDS = 4
MAX_SEARCH_CORRECTIONS = 3
NUM_RESULTS = 5  # Exa hits per query
_CONDENSE_HITS = 3  # top-K hits fed to the condense inference

SEARCH_SYSTEM_PROMPT = load_prompt_text("personas/deep_search_seed")

# Condense: distill raw hits to the answer-bearing fact(s) for one query. Runs in
# an EPHEMERAL low-reasoning session (opened/closed per call) so (a) raw pages
# never enter the main search session, and (b) the reasoning HEAD-SWAP steers it
# to LOW effort — a distillation task never needs high reasoning (see _CONDENSE_CFG).
CONDENSE_PROMPT = load_prompt_text("deep_search/condense")

# Condense is a low-reasoning task: the reasoning HEAD-SWAP steers the ephemeral
# condense session to LOW effort (server forks the low head at turn 0), plus a low
# temperature + a token cap. Servers/models without the head-swap ignore
# "reasoning" (Optional field) and fall back to their default level — still bounded.
_CONDENSE_CFG = {"reasoning": "low", "temperature": "t*0.2", "max_tokens": 512}

CONCLUDE_SEARCH_PROMPT = load_prompt_text("deep_search/conclude")


def _bounded(s: str, n: int) -> str:
    s = (s or "").strip()
    return s if len(s) <= n else s[:n] + " …[truncated]"


def _round_observe(
    step_input: StepInput, message: str, extra: dict | None = None
) -> StepOutput:
    """Queue an observation into the search session and bump the round counter."""
    rnd = int(step_input.context.get("search_round", 0) or 0) + 1
    updates: dict = {"search_round": rnd}
    if extra:
        updates.update(extra)
    queue_injection(updates, step_input.context, message)
    return StepOutput(
        result={"action_ok": True},
        observations=f"deep_search round {rnd}/{MAX_SEARCH_ROUNDS}",
        context_updates=updates,
    )


def _round_correction(step_input: StepInput, msg: str) -> StepOutput:
    """Queue a correction WITHOUT spending a round (a bad/empty query recovers)."""
    corr = int(step_input.context.get("search_corrections", 0) or 0) + 1
    updates: dict = {"search_corrections": corr}
    queue_injection(updates, step_input.context, f"Search issue — {msg}")
    return StepOutput(
        result={"action_ok": False, "exhausted": corr >= MAX_SEARCH_CORRECTIONS},
        observations=f"deep_search correction ({corr}): {msg[:120]}",
        context_updates=updates,
    )


async def action_open_search_session(step_input: StepInput) -> StepOutput:
    """Open the memoryful research session and seed it with the brief.

    Declines cleanly (session_started=False) when web research is disabled for
    the mission (hermetic) or there are no effects — the caller then routes to
    its unavailable branch and gets an empty summary.

    Inputs: brief.  Publishes: search_session_id, inference_session_id,
    search_round, search_corrections, search_queries_run.
    """
    effects = step_input.effects
    if not effects:
        return StepOutput(
            result={"session_started": False},
            observations="No effects interface — cannot start research",
            context_updates={"research_summary": "", "search_sufficient": False},
        )

    # Gate on web_research (off for hermetic SWE). The ~/.exa_key absence is the
    # hard backstop in search_run, but declining here avoids a wasted session.
    try:
        mission = await effects.load_mission()
        web_ok = bool(getattr(getattr(mission, "config", None), "web_research", True))
    except (
        Exception
    ):  # noqa: BLE001 - no mission → assume enabled, rely on key backstop
        web_ok = True
    if not web_ok:
        return StepOutput(
            result={"session_started": False},
            observations="web_research disabled — research unavailable",
            context_updates={"research_summary": "", "search_sufficient": False},
        )

    brief = str((step_input.inputs or {}).get("brief", "") or "").strip()
    if not brief:
        return StepOutput(
            result={"session_started": False},
            observations="No brief provided — nothing to research",
            context_updates={"research_summary": "", "search_sufficient": False},
        )

    try:
        session_id = await effects.start_inference_session({"ttl_seconds": 600})
    except Exception as e:  # noqa: BLE001
        logger.error("Failed to start research session: %s", e)
        return StepOutput(
            result={"session_started": False},
            observations=f"Failed to start research session: {e}",
            context_updates={"research_summary": "", "search_sufficient": False},
        )

    seed = "\n".join(
        [
            SEARCH_SYSTEM_PROMPT,
            "",
            "## Research brief (answer this)",
            _bounded(brief, 3000),
            "",
            f"You have up to {MAX_SEARCH_ROUNDS} search rounds. Name the specific "
            "unknown, search for it, and finish when you can answer.",
        ]
    )
    updates: dict = {
        "inference_session_id": session_id,
        "search_session_id": session_id,
        "search_round": 0,
        "search_corrections": 0,
        "search_queries_run": [],
    }
    queue_injection(updates, step_input.context, seed)
    logger.info("deep_search session started: %s", session_id)
    return StepOutput(
        result={"session_started": True},
        observations="deep_search session opened",
        context_updates=updates,
    )


async def action_search_run(step_input: StepInput) -> StepOutput:
    """search tool: run one Exa query, publish raw hits for the condense step.

    Reuses the refinement_actions Exa MCP path (mcp_connect + web_search_exa +
    _extract_exa_hits). Context: search_choice_arg (the query). Publishes
    raw_search_results + appends the query to search_queries_run.
    """
    effects = step_input.effects
    query = str(step_input.context.get("search_choice_arg", "") or "").strip()
    if not query:
        return _round_correction(step_input, "search needs a query argument.")

    try:
        conn_id = await effects.mcp_connect("exa")
    except FileNotFoundError as e:
        # ~/.exa_key missing — the hard backstop. Fold a note and let the model
        # conclude from what it has (correction, not a round).
        return _round_correction(step_input, f"web search unavailable ({e}).")
    except Exception as e:  # noqa: BLE001
        return _round_correction(step_input, f"search backend failed to connect: {e}")

    try:
        mcp_result = await effects.mcp_call_tool(
            conn_id, "web_search_exa", {"query": query, "numResults": NUM_RESULTS}
        )
    except Exception as e:  # noqa: BLE001
        return _round_correction(step_input, f"search failed for {query!r}: {e}")

    hits = [
        {
            "url": h.get("url", ""),
            "title": h.get("title", ""),
            "content": h.get("content", ""),
        }
        for h in _extract_exa_hits(mcp_result)
        if h.get("content", "").strip()
    ]
    queries_run = list(step_input.context.get("search_queries_run", []) or []) + [query]
    if not hits:
        # No usable hits — nudge a refine; does NOT spend a round (correction).
        out = _round_correction(
            step_input, f"no results for {query!r} — refine the query."
        )
        out.context_updates["search_queries_run"] = queries_run
        out.context_updates["raw_search_results"] = []
        return out
    return StepOutput(
        result={"action_ok": True, "has_hits": True},
        observations=f"deep_search: {len(hits)} hits for {query!r}",
        context_updates={
            "raw_search_results": hits,
            "search_queries_run": queries_run,
            "last_query": query,
        },
    )


async def action_condense_results(step_input: StepInput) -> StepOutput:
    """condense: distill raw hits → the answer-bearing fact(s), fold ONLY that
    into the session (raw pages never enter it), and spend one round.

    STATELESS distillation (effects.run_inference over just the hits) so the raw
    documents stay out of the memoryful search session. Runs at low temperature
    + a token cap today; the reasoning-head-swap upgrade drops into _CONDENSE_CFG.
    """
    effects = step_input.effects
    hits = list(step_input.context.get("raw_search_results", []) or [])
    query = str(
        step_input.context.get("last_query", "")
        or step_input.context.get("search_choice_arg", "")
        or ""
    )
    if not hits:
        # Nothing to condense — treat as an empty round observation.
        return _round_observe(
            step_input, f"Observation (search {query!r}): no results to condense."
        )

    hits_block = "\n\n---\n\n".join(
        f"[{h.get('url', '')}] {h.get('title', '')}\n{_bounded(h.get('content', ''), 1500)}"
        for h in hits[:_CONDENSE_HITS]
    )
    prompt = CONDENSE_PROMPT.format(query=query, hits=hits_block)

    # Distill in an EPHEMERAL low-reasoning session: the raw hits go into a
    # throwaway session (never the main search session), and the reasoning
    # HEAD-SWAP (reasoning=low in _CONDENSE_CFG) steers it to low effort at turn 0.
    fact = ""
    if effects is not None:
        sess = None
        try:
            sess = await effects.start_inference_session({"ttl_seconds": 120})
            res = await effects.session_inference(sess, prompt, dict(_CONDENSE_CFG))
            fact = (getattr(res, "text", None) or "").strip()
        except (
            Exception
        ) as e:  # noqa: BLE001 - fold a degraded note, keep the loop alive
            logger.warning("condense failed for %r: %s", query, e)
            fact = ""
        finally:
            if sess:
                try:
                    await effects.end_inference_session(sess)
                except Exception:  # noqa: BLE001
                    pass
    if not fact:
        # Degrade to a bounded raw snippet rather than dropping the round's signal.
        fact = "(condense unavailable) " + _bounded(hits[0].get("content", ""), 600)

    return _round_observe(
        step_input,
        f"Finding for {query!r}:\n{_bounded(fact, 2000)}",
    )


async def action_conclude_search(step_input: StepInput) -> StepOutput:
    """synthesize: one session inference over the accumulated findings → the
    research_summary + sufficient flag the caller consumes.

    Context: search_session_id.  Publishes: research_summary, search_sufficient,
    queries_run.
    """
    effects = step_input.effects
    session_id = str(step_input.context.get("search_session_id", "") or "")
    queries_run = list(step_input.context.get("search_queries_run", []) or [])
    if not effects or not session_id:
        return StepOutput(
            result={"sufficient": False},
            observations="No session — cannot synthesize research",
            context_updates={
                "research_summary": "",
                "search_sufficient": False,
                "queries_run": queries_run,
            },
        )

    summary = ""
    sufficient = False
    try:
        res = await effects.session_inference(
            session_id, CONCLUDE_SEARCH_PROMPT, {"temperature": "t*0.3"}
        )
        text = getattr(res, "text", None) or str(res or "")
        parsed = parse_llm_json(text)
        if isinstance(parsed, dict):
            summary = str(parsed.get("research_summary", "") or "").strip()
            sufficient = bool(parsed.get("sufficient", False))
    except Exception as e:  # noqa: BLE001
        logger.warning("research conclude failed: %s", e)

    return StepOutput(
        result={"sufficient": sufficient},
        observations=(
            f"deep_search concluded: {len(queries_run)} queries, "
            f"sufficient={sufficient}, {len(summary)} chars"
        ),
        context_updates={
            "research_summary": summary,
            "search_sufficient": sufficient,
            "queries_run": queries_run,
        },
    )
