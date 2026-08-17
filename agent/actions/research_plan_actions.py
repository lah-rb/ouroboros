"""Research planning, sweeps, and harvest for the scraper flow set.

The research plan is the scraper's architecture analog: plan_research
parses the abstract into aspects (mission.research_plan), goals derive
deterministically from them — aspects ARE the decomposition, no second
inference pass — and the sweeps drive dispatches against the workspace
databank worklist (papers are never goals; see scholarly_actions).
"""

from __future__ import annotations

import logging

from agent.models import StepInput, StepOutput

logger = logging.getLogger(__name__)

# Absolute backstop only. The REAL stop is yield-based (DRY_ROUNDS_TO_STOP
# below): an aspect keeps going while rounds still add papers. A fixed cap
# of 3 was the binding constraint on the first corpus run — discovery
# returned ~136 candidates against a target of 10, so every aspect stopped
# after one round with the literature nowhere near exhausted.
MAX_DISCOVERY_ROUNDS = 40
# Consecutive no-new-papers rounds that mean the queries are spent.
DRY_ROUNDS_TO_STOP = 2
CORPUS_GOAL_SIGNATURE = "corpus-catalog"


async def _set_goal_status(effects, mission, goal) -> None:
    """Persist one goal's (already in-memory-mutated) status.

    Mission-ops pilot: prefers effects.mission_apply with a GoalStatusOp —
    a field-granular, merge-safe write — and falls back to whole-document
    save_mission for effects doubles without the surface. Status sets are
    idempotent, so mirroring on the in-memory object AND applying the op is
    safe (counters would not be — see the loop park site)."""
    apply = getattr(effects, "mission_apply", None)
    if apply is not None:
        from agent.persistence.models import GoalStatusOp

        applied = await apply([GoalStatusOp(goal_id=goal.id, status=goal.status)])
        if applied is not None:
            return
    await effects.save_mission(mission)


def _aspect_slug(name: str) -> str:
    return "".join(c if c.isalnum() else "-" for c in name.strip().lower()).strip("-")


def _discovery_signature(aspect_name: str) -> str:
    return f"aspect-discovery:{_aspect_slug(aspect_name)}"


async def action_parse_and_store_research_plan(step_input: StepInput) -> StepOutput:
    """Parse the plan JSON into mission.research_plan.

    Expected LLM shape: {"aspects": [{name, description, seed_queries,
    coverage_target}], "notes": "..."} — AspectSpec.from_llm_dict
    tolerates field-name drift; nameless aspects are dropped.

    Context: mission, inference_response
    Result: plan_parsed, aspect_count
    """
    from agent.llm_json import parse_llm_json
    from agent.persistence.models import AspectSpec, ResearchPlanState

    effects = step_input.effects
    mission = step_input.context.get("mission")
    if not mission:
        return StepOutput(result={"plan_parsed": False}, observations="No mission")

    parsed = parse_llm_json(str(step_input.context.get("inference_response", "")))
    if not isinstance(parsed, dict):
        return StepOutput(
            result={"plan_parsed": False},
            observations="Research plan JSON did not parse",
        )

    aspects = []
    for raw in parsed.get("aspects") or []:
        if not isinstance(raw, dict):
            continue
        aspect = AspectSpec.from_llm_dict(raw)
        if aspect.name:
            aspects.append(aspect)

    if not aspects:
        return StepOutput(
            result={"plan_parsed": False},
            observations="Research plan parsed but contained no named aspects",
        )

    # The planner sets the SHAPE, mission.config sets the SCALE. Rescaling
    # preserves whatever relative weighting the model chose across aspects
    # while keeping the corpus-size decision with the operator -- a model
    # reading an example number in a prompt should not be deciding how big
    # the dataset is.
    corpus_target = int(getattr(getattr(mission, "config", None), "corpus_target", 0))
    if corpus_target > 0 and aspects:
        weight_total = sum(max(1, a.coverage_target) for a in aspects)
        for aspect in aspects:
            share = max(1, aspect.coverage_target) / weight_total
            aspect.coverage_target = max(1, round(corpus_target * share))

    mission.research_plan = ResearchPlanState(
        abstract=str(getattr(mission, "objective", "") or ""),
        aspects=aspects,
        notes=str(parsed.get("notes") or ""),
    )
    if effects:
        await effects.save_mission(mission)

    return StepOutput(
        result={"plan_parsed": True, "aspect_count": len(aspects)},
        observations=(
            "Research plan stored: "
            + ", ".join(f"'{a.name}' (target {a.coverage_target})" for a in aspects)
        ),
        context_updates={"mission": mission},
    )


async def action_derive_research_goals(step_input: StepInput) -> StepOutput:
    """Deterministic goals from the plan: one discovery goal per aspect
    plus ONE corpus-level catalog goal (type "extraction").

    Idempotent by finding_signature — re-planning never duplicates goals
    (mirrors the harvest dedup mechanism).

    Context: mission
    Result: goals_derived, goal_count
    """
    from agent.persistence.models import GoalRecord

    effects = step_input.effects
    mission = step_input.context.get("mission")
    plan = getattr(mission, "research_plan", None) if mission else None
    if not plan or not plan.aspects:
        return StepOutput(
            result={"goals_derived": False}, observations="No research plan"
        )

    existing_sigs = {
        getattr(g, "finding_signature", "")
        for g in mission.goals
        if getattr(g, "finding_signature", "")
    }
    created = 0
    for aspect in plan.aspects:
        sig = _discovery_signature(aspect.name)
        if sig in existing_sigs:
            continue
        mission.goals.append(
            GoalRecord(
                description=(
                    f"Aspect '{aspect.name}': discover at least "
                    f"{aspect.coverage_target} candidate papers"
                ),
                type="discovery",
                origin="design",
                finding_signature=sig,
            )
        )
        created += 1
    if CORPUS_GOAL_SIGNATURE not in existing_sigs:
        mission.goals.append(
            GoalRecord(
                description="Acquire OA PDFs and catalog all candidate papers",
                type="extraction",
                origin="design",
                finding_signature=CORPUS_GOAL_SIGNATURE,
            )
        )
        created += 1

    if effects:
        await effects.save_mission(mission)

    return StepOutput(
        result={"goals_derived": True, "goal_count": created},
        observations=f"Derived {created} research goal(s)",
        context_updates={"mission": mission},
    )


async def action_discovery_sweep_next(step_input: StepInput) -> StepOutput:
    """Dispatch the next incomplete aspect's discovery, or complete them.

    Completion check FIRST (candidate count ≥ target, or the goal has
    burned MAX_DISCOVERY_ROUNDS dispatches — thin literature must not
    loop forever; the gate reports residual under-coverage).

    Context: mission
    Result: needs_discover (+ dispatch_config) | sweep_complete
    """
    from agent.actions.scholarly_actions import read_databank

    effects = step_input.effects
    mission = step_input.context.get("mission")
    plan = getattr(mission, "research_plan", None) if mission else None
    if not mission or not plan:
        return StepOutput(result={"sweep_complete": True}, observations="No plan")

    aspects_by_sig = {_discovery_signature(a.name): a for a in plan.aspects}
    databank = await read_databank(effects)

    def _aspect_count(name: str) -> int:
        return sum(
            1 for rec in databank.values() if name in (rec.get("source_aspects") or [])
        )

    changed = False
    for goal in mission.goals:
        if goal.type != "discovery" or goal.status != "incomplete":
            continue
        aspect = aspects_by_sig.get(goal.finding_signature)
        if aspect is None:
            goal.status = "complete"  # plan drifted; don't strand the mission
            changed = True
            continue
        have = _aspect_count(aspect.name)
        # A round that added nothing is the signal the queries are spent.
        # Count it BEFORE deciding, so a dispatched-but-barren round is
        # visible on the next pass.
        if goal.reports:
            if have <= aspect.last_have:
                aspect.dry_rounds += 1
            else:
                aspect.dry_rounds = 0
            changed = True
        exhausted = aspect.dry_rounds >= DRY_ROUNDS_TO_STOP
        # A GATE-REOPENED GOAL MUST RUN AT LEAST ONE ROUND. The gate's
        # coverage metric is TAGGED papers; this sweep's target counts RAW
        # candidates — after a reopen (harvest empties goal.reports) the
        # candidate count usually already exceeds the target, and completing
        # on it re-closes the goal with "0 round(s)" without ever searching.
        # Live: a 200-cycle gate↔harvest spin, zero discover dispatches,
        # while four aspects sat at 149-797/833 TAGGED. With reports empty,
        # the candidate target is not a reason to complete; only exhaustion
        # or the round cap is. New rounds now also run with the seeded
        # corpus_languages, which the exhaustion verdict predates.
        target_met = have >= aspect.coverage_target and bool(goal.reports)
        if target_met or exhausted or len(goal.reports) >= MAX_DISCOVERY_ROUNDS:
            goal.status = "complete"
            changed = True
            logger.info(
                "Discovery complete for '%s': %d/%d candidates (%d round(s)%s)",
                aspect.name,
                have,
                aspect.coverage_target,
                len(goal.reports),
                ", queries exhausted" if exhausted else "",
            )
            continue
        aspect.last_have = have
        if changed and effects:
            await effects.save_mission(mission)
        return StepOutput(
            result={"needs_discover": True},
            observations=(
                f"Discovery sweep: '{aspect.name}' at {have}/"
                f"{aspect.coverage_target} — dispatching round "
                f"{len(goal.reports) + 1}"
            ),
            context_updates={
                "dispatch_config": {
                    "goal_id": goal.id,
                    "goal_description": goal.description,
                    "flow": "discover",
                    "aspect_name": aspect.name,
                    "aspect_description": aspect.description,
                    "seed_queries": list(aspect.seed_queries),
                    "coverage_target": aspect.coverage_target,
                    "have_count": have,
                    # Carried into the query-refinement prompt so the model can
                    # emit native-language phrasings, not translations of the
                    # English wording. Empty for an English-only mission, and
                    # the prompt section is `when`-gated on it.
                    "corpus_languages": list(
                        getattr(mission.config, "corpus_languages", None) or []
                    ),
                    "flow_directive": (
                        f"Find candidate papers for the aspect '{aspect.name}' "
                        f"({aspect.description or 'no description'}). The aspect "
                        f"has {have} of {aspect.coverage_target} candidates."
                    ),
                }
            },
        )

    if changed and effects:
        await effects.save_mission(mission)
    return StepOutput(
        result={"sweep_complete": True},
        observations="Discovery sweep: all aspects complete",
    )


# How many of a 5-record batch may be stale re-arms. Small on purpose: the
# repair lane must never starve new acquisition.
_STALE_RETRY_PER_BATCH = 2


def _stale_retry_keys(databank: dict) -> list:
    """Previously-unresolved papers worth re-arming, best-yield first.

    The staleness PREDICATE lives in scholarly_actions beside the record
    schema, because the resolver applies the same test to do the actual
    re-arm — two copies would drift and the selector would queue papers the
    resolver then declined to touch.
    """
    from agent.actions.scholarly_actions import (
        RETRYABLE_BUCKETS,
        classify_failure,
        is_stale_retry_candidate,
    )

    out = []
    for key, rec in databank.items():
        if not is_stale_retry_candidate(rec):
            continue
        out.append(
            (
                RETRYABLE_BUCKETS.index(
                    classify_failure(rec.get("failure_reason", ""))
                ),
                key,
            )
        )
    return [k for _, k in sorted(out)]


def _oa_first(items: list) -> list:
    """Candidate keys, OA-likely ones first.

    `_normalize_openalex` already stores every OA location it found into
    `oa_pdf_urls`, so a record carrying an `openalex_id` with NO urls is one
    OpenAlex looked at and found no route for. Verification confirms 67.5% of
    what it checks is closed; working the likely ones first means PDFs arrive
    far sooner from the same total effort.

    Ordering only — every paper is still verified eventually, so nothing is
    lost if the signal is wrong. The hard skip is opt-in and lives in the
    resolver.
    """

    def rank(kr: tuple) -> tuple:
        _, rec = kr
        hopeless = bool(rec.get("openalex_id")) and not (rec.get("oa_pdf_urls") or [])
        return (1 if hopeless else 0, kr[0])

    return [k for k, _ in sorted(items, key=rank)]


async def action_catalog_sweep_next(step_input: StepInput) -> StepOutput:
    """Dispatch the next acquire+catalog batch from the worklist.

    needs_retag records (gate grounding failures) take priority over
    fresh candidates, which are themselves ordered OA-likely-first. A capped
    tail of STALE re-arms repairs old acquisition failures without starving
    new work. Empty worklist completes the corpus goal.

    Context: mission
    Result: needs_catalog (+ dispatch_config) | sweep_complete
    """
    from agent.actions.scholarly_actions import CATALOG_BATCH_SIZE, read_databank

    effects = step_input.effects
    mission = step_input.context.get("mission")
    if not mission:
        return StepOutput(result={"sweep_complete": True}, observations="No mission")

    goal = next(
        (
            g
            for g in mission.goals
            if g.type == "extraction" and g.status == "incomplete"
        ),
        None,
    )
    if goal is None:
        return StepOutput(
            result={"sweep_complete": True},
            observations="Catalog sweep: corpus goal complete",
        )

    # A SPENT BUDGET MUST END THE PHASE, NOT SPIN IT. The sweep completes
    # the corpus goal only when the worklist empties — but with the request
    # budget exhausted no record can ever be processed, so records stay
    # "candidate" and the same batch redispatches forever. Measured: 296
    # budget-exhausted warnings across 121 cycles, zero progress possible,
    # and it would have burned the full 8h wall. Same shape as a gate that
    # cannot fail: an exhausted resource has to be a terminal state.
    from agent.actions.scholarly_actions import _HTTP_STATE_KEY, _request_budget

    budget = _request_budget()
    if budget and effects:
        http_state = await effects.read_state(_HTTP_STATE_KEY) or {}
        used = int(http_state.get("total_requests") or 0)
        if used >= budget:
            # Mission-ops pilot: an idempotent status set — mirrored on the
            # cycle's shared in-memory object AND persisted as an op (safe to
            # double-apply, unlike counters).
            goal.status = "complete"
            await _set_goal_status(effects, mission, goal)
            return StepOutput(
                result={"sweep_complete": True, "budget_exhausted": True},
                observations=(
                    f"Catalog sweep: request budget exhausted ({used}/{budget}) — "
                    f"corpus goal closed as PARTIAL. Raise or unset "
                    f"OUROBOROS_SCRAPER_HTTP_BUDGET and re-run to continue "
                    f"acquiring; the databank is preserved."
                ),
            )

    databank = await read_databank(effects)
    retag = [k for k, r in databank.items() if r.get("status") == "needs_retag"]
    # `duplicate_of` means this paper reached us twice under two identities and
    # the other copy is the one to work. Suppressed HERE rather than at merge,
    # so the record still exists and the flag stays reversible — but no fetch,
    # no OCR and no translation is ever spent on it.
    fresh = _oa_first(
        [
            (k, r)
            for k, r in databank.items()
            if r.get("status") == "candidate" and not r.get("duplicate_of")
        ]
    )
    stale = _stale_retry_keys(databank)
    # Stale re-arms get a CAPPED reservation, so repairing old failures can
    # never crowd out new acquisition — and when there are none, the whole
    # batch is fresh work exactly as before.
    stale_slots = min(len(stale), _STALE_RETRY_PER_BATCH)
    batch = (sorted(retag) + fresh)[: CATALOG_BATCH_SIZE - stale_slots]
    batch += stale[:stale_slots]

    if not batch:
        goal.status = "complete"
        if effects:
            await _set_goal_status(effects, mission, goal)
        return StepOutput(
            result={"sweep_complete": True},
            observations="Catalog sweep: worklist empty — corpus goal complete",
        )

    return StepOutput(
        result={"needs_catalog": True},
        observations=f"Catalog sweep: dispatching batch of {len(batch)}",
        context_updates={
            "dispatch_config": {
                "goal_id": goal.id,
                "goal_description": goal.description,
                "flow": "acquire_catalog",
                "paper_keys": batch,
                "flow_directive": (
                    f"Acquire and catalog {len(batch)} paper(s) from the "
                    f"candidate worklist."
                ),
            }
        },
    )


async def action_harvest_research_findings(step_input: StepInput) -> StepOutput:
    """Turn research-gate findings into reopened goals / retag marks.

    coverage issue  -> reopen that aspect's discovery goal + a note with
                       the expansion hint (distinguishing "no candidates"
                       from "only adjacent hits").
    grounding issue -> mark the paper needs_retag + reopen the corpus goal.

    Freshens mission.notes from disk first — the gate just pushed notes
    and this cycle's context mission predates them (lost-update hazard,
    see action_harvest_quality_findings).

    Context: mission; optional gate_results
    Result: harvested | done
    """
    from agent.actions.scholarly_actions import append_records, read_databank

    effects = step_input.effects
    mission = step_input.context.get("mission")
    if not mission:
        return StepOutput(result={"done": True}, observations="No mission")

    if effects:
        try:
            fresh = await effects.load_mission()
            if fresh is not None and len(fresh.notes) > len(mission.notes):
                mission.notes = fresh.notes
        except Exception:
            pass

    gate_results = step_input.context.get("gate_results") or {}
    issues = (
        (gate_results.get("blocking_issues") or [])
        if isinstance(gate_results, dict)
        else []
    )
    if not issues:
        return StepOutput(
            result={"done": True},
            observations="Research gate failed but produced no issues — finalizing",
        )

    by_sig = {
        getattr(g, "finding_signature", ""): g
        for g in mission.goals
        if getattr(g, "finding_signature", "")
    }
    reopened = retagged = 0
    retag_records: list[dict] = []
    databank = await read_databank(effects)

    for issue in issues:
        if not isinstance(issue, dict):
            continue
        cls = issue.get("class")
        if cls == "coverage":
            sig = _discovery_signature(str(issue.get("aspect") or ""))
            goal = by_sig.get(sig)
            if goal is not None and goal.status == "complete":
                goal.status = "incomplete"
                # Reset the round budget — the gate has authorized more rounds.
                goal.reports = []
                reopened += 1
            # COVERAGE IS MEASURED IN TAGGED PAPERS, and new candidates are
            # untagged until CATALOGED — so a coverage reopen must also
            # reopen the corpus catalog goal, or discovery pours candidates
            # into a worklist no phase ever drains (the phase ladder only
            # runs catalog while the extraction goal is incomplete) and the
            # gate's coverage number can never move.
            corpus = by_sig.get(CORPUS_GOAL_SIGNATURE)
            if corpus is not None and corpus.status == "complete":
                corpus.status = "incomplete"
                reopened += 1
            if effects:
                hint = (
                    "only adjacent-relevance hits — broaden toward the aspect's "
                    "core phenomenon"
                    if issue.get("adjacent_only")
                    else "too few candidates — widen query phrasing"
                )
                await effects.push_note(
                    content=(
                        f"Research gate: aspect '{issue.get('aspect')}' at "
                        f"{issue.get('have', '?')}/{issue.get('want', '?')} — {hint}"
                    ),
                    category="failure_analysis",
                    tags=["coverage"],
                    source_flow="research_control",
                )
        elif cls == "grounding":
            key = str(issue.get("paper_key") or "")
            rec = databank.get(key)
            if rec is not None and rec.get("status") != "needs_retag":
                rec["status"] = "needs_retag"
                retag_records.append(rec)
                retagged += 1
            corpus = by_sig.get(CORPUS_GOAL_SIGNATURE)
            if corpus is not None and corpus.status == "complete":
                corpus.status = "incomplete"
                reopened += 1

    if retag_records:
        await append_records(effects, retag_records)
    if effects:
        # NOT migrated to mission ops: a reopen is status + reports=[] — a
        # nested multi-field change, i.e., an owner-shaped write. Ops cover
        # single-field shapes only (see models.MissionOp); forcing this one
        # would leave stale round budgets in the crash window.
        await effects.save_mission(mission)

    return StepOutput(
        result={"harvested": True},
        observations=(
            f"Research harvest: {reopened} goal(s) reopened, "
            f"{retagged} paper(s) marked for retag"
        ),
    )
