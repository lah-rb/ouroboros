"""Research gate: coverage, cross-links, and tag-grounding probes.

The verify-before-harvest doctrine applied to the scraper: aspect tags
are CLAIMS, sampled and grounded against the stored abstracts before
the verdict — which is fully DERIVED (pass iff coverage report empty
AND ungrounded list empty); the LLM only writes prose. Fail-safe runs
the other way from the code gate's probes: an UNVERIFIABLE tag is
ungrounded (a tag that can't be justified must not silently pass into
the evidence base).

Parallel module to verification_actions by design — same doctrine,
different probe medium (abstracts, not PTYs).
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from typing import Any

from agent.models import StepInput, StepOutput

logger = logging.getLogger(__name__)

GROUNDING_SAMPLE_SIZE = 6
# Content-word overlap ratio at/above which a justification is grounded
# without a judge turn.
_OVERLAP_THRESHOLD = 0.6
_WORD_RE = re.compile(r"[a-z0-9][a-z0-9\-]{3,}")

LINKS_PATH = "databank/links.json"


def _content_words(text: str) -> set[str]:
    return set(_WORD_RE.findall(text.lower()))


def _overlap_grounded(justification: str, abstract: str) -> bool:
    words = _content_words(justification)
    if not words:
        return False
    hits = words & _content_words(abstract)
    return len(hits) / len(words) >= _OVERLAP_THRESHOLD


async def action_check_aspect_coverage(step_input: StepInput) -> StepOutput:
    """Per-aspect evidence-base counts; targets satisfied by exact+close.

    Adjacent tags never satisfy coverage — they're the periphery. The
    report distinguishes "no candidates" from "only adjacent hits" so
    the harvest note can give the right query-expansion hint.

    Context: mission
    Result: all_covered; Publishes: coverage_report
    """
    from agent.actions.scholarly_actions import read_databank

    effects = step_input.effects
    mission = step_input.context.get("mission")
    plan = getattr(mission, "research_plan", None) if mission else None
    if not plan:
        return StepOutput(
            result={"all_covered": False},
            observations="No research plan — coverage unmeasurable",
            context_updates={"coverage_report": []},
        )

    databank = await read_databank(effects)
    report = []
    for aspect in plan.aspects:
        strong = adjacent = 0
        for rec in databank.values():
            if rec.get("status") not in ("cataloged", "needs_retag"):
                continue
            tiers = {
                t.get("relevance")
                for t in (rec.get("tags") or [])
                if t.get("aspect") == aspect.name
            }
            if tiers & {"exact", "close"}:
                strong += 1
            elif "adjacent" in tiers:
                adjacent += 1
        if strong < aspect.coverage_target:
            report.append(
                {
                    "class": "coverage",
                    "aspect": aspect.name,
                    "have": strong,
                    "want": aspect.coverage_target,
                    "adjacent_only": strong == 0 and adjacent > 0,
                }
            )

    return StepOutput(
        result={"all_covered": not report},
        observations=(
            "Coverage: all aspects met"
            if not report
            else "Coverage short on: "
            + ", ".join(f"{i['aspect']} ({i['have']}/{i['want']})" for i in report)
        ),
        context_updates={"coverage_report": report},
    )


async def action_finalize_crosslinks(step_input: StepInput) -> StepOutput:
    """Corpus-internal citation edges from stored reference DOIs.

    Deferred to gate time so links to later-acquired papers are not
    missed; idempotent overwrite of databank/links.json.

    Result: edge_count, papers_linked
    """
    from agent.actions.scholarly_actions import read_databank

    effects = step_input.effects
    databank = await read_databank(effects)
    doi_to_key = {
        str(rec.get("doi") or "").lower(): key
        for key, rec in databank.items()
        if rec.get("doi")
    }
    edges = []
    linked = set()
    for key, rec in databank.items():
        for ref in rec.get("reference_dois") or []:
            target = doi_to_key.get(str(ref).lower())
            if target and target != key:
                edges.append({"from": key, "to": target})
                linked.update((key, target))

    payload = json.dumps({"edges": edges}, indent=2)
    if effects:
        await effects.write_file(LINKS_PATH, payload)
    return StepOutput(
        result={"edge_count": len(edges), "papers_linked": len(linked)},
        observations=f"Cross-links: {len(edges)} edge(s) among {len(linked)} paper(s)",
    )


def _sample_records(databank: dict[str, dict], mission_id: str) -> list[dict]:
    """Deterministic, reproducible sample stratified across relevance tiers."""

    def _rank(key: str) -> str:
        return hashlib.sha1(f"{mission_id}:{key}".encode()).hexdigest()

    tagged = {
        k: r
        for k, r in databank.items()
        if r.get("status") in ("cataloged", "needs_retag") and r.get("tags")
    }
    by_tier: dict[str, list[str]] = {"exact": [], "close": [], "adjacent": []}
    for key, rec in tagged.items():
        tiers = {t.get("relevance") for t in rec["tags"]}
        for tier in ("exact", "close", "adjacent"):
            if tier in tiers:
                by_tier[tier].append(key)
                break

    sample: list[str] = []
    pools = [sorted(v, key=_rank) for v in by_tier.values()]
    while len(sample) < GROUNDING_SAMPLE_SIZE and any(pools):
        for pool in pools:
            if pool and len(sample) < GROUNDING_SAMPLE_SIZE:
                key = pool.pop(0)
                if key not in sample:
                    sample.append(key)
    return [tagged[k] for k in sample]


def _probe_keys(entry: dict) -> dict:
    return {
        "probe_abstract": entry["abstract"],
        "probe_tags": json.dumps(entry["tags"], ensure_ascii=False, indent=2),
        "probe_paper_key": entry["paper_key"],
    }


async def action_prepare_tag_grounding(step_input: StepInput) -> StepOutput:
    """Sample records; cheap overlap check; queue survivors for the judge.

    A tag whose justification shares ≥60% of its content words with the
    abstract is grounded without inference. Records with no abstract
    text are judged (the model may have tagged from the title — the
    judge decides with what's stored).

    Context: mission
    Result: has_next, grounded, queued
    Publishes: grounding_queue, grounded_tags, ungrounded_tags, probe_* keys
    """
    from agent.actions.scholarly_actions import read_databank

    effects = step_input.effects
    mission = step_input.context.get("mission")
    databank = await read_databank(effects)
    sample = _sample_records(databank, str(getattr(mission, "id", "") or ""))

    grounded: list[dict] = []
    queue: list[dict] = []
    for rec in sample:
        abstract = str(rec.get("abstract") or "")
        pending = []
        for tag in rec.get("tags") or []:
            if abstract and _overlap_grounded(
                str(tag.get("justification") or ""), abstract
            ):
                grounded.append({"paper_key": rec["paper_key"], **tag})
            else:
                pending.append(tag)
        if pending:
            queue.append(
                {
                    "paper_key": rec["paper_key"],
                    "abstract": abstract or str(rec.get("title") or ""),
                    "tags": pending,
                }
            )

    context_updates: dict[str, Any] = {
        "grounding_queue": queue,
        "grounded_tags": grounded,
        "ungrounded_tags": [],
    }
    if queue:
        context_updates.update(_probe_keys(queue[0]))

    return StepOutput(
        result={
            "has_next": bool(queue),
            "grounded": len(grounded),
            "queued": len(queue),
        },
        observations=(
            f"Tag grounding: {len(grounded)} tag(s) grounded deterministically, "
            f"{len(queue)} record(s) to judge"
        ),
        context_updates=context_updates,
    )


async def action_record_tag_grounding(step_input: StepInput) -> StepOutput:
    """Record the head record's judge verdict and advance the queue.

    Judge JSON: {"grounded": bool, "ungrounded_aspects": [..], "reason"}.
    Unparseable or empty -> ALL the record's queued tags are ungrounded
    (an unverifiable tag must not silently pass into the evidence base).

    Context: grounding_queue (+ inference_response, lists)
    Result: has_next
    """
    from agent.llm_json import parse_llm_json

    queue = list(step_input.context.get("grounding_queue") or [])
    grounded = list(step_input.context.get("grounded_tags") or [])
    ungrounded = list(step_input.context.get("ungrounded_tags") or [])

    if not queue:
        return StepOutput(
            result={"has_next": False}, observations="Grounding queue empty"
        )

    entry = queue.pop(0)
    parsed = parse_llm_json(str(step_input.context.get("inference_response", "")))
    verdict = parsed if isinstance(parsed, dict) else {}
    is_grounded = verdict.get("grounded")
    bad_aspects = {
        str(a).strip() for a in (verdict.get("ungrounded_aspects") or []) if str(a)
    }
    reason = str(verdict.get("reason") or "")[:300]

    for tag in entry["tags"]:
        tag_record = {"paper_key": entry["paper_key"], **tag, "reason": reason}
        if is_grounded is True and tag["aspect"] not in bad_aspects:
            grounded.append(tag_record)
        elif is_grounded is False and bad_aspects and tag["aspect"] not in bad_aspects:
            grounded.append(tag_record)
        else:
            # grounded missing/unparseable, or aspect named ungrounded.
            ungrounded.append(
                {
                    **tag_record,
                    "reason": reason or "judge gave no parseable verdict",
                }
            )

    context_updates: dict[str, Any] = {
        "grounding_queue": queue,
        "grounded_tags": grounded,
        "ungrounded_tags": ungrounded,
    }
    if queue:
        context_updates.update(_probe_keys(queue[0]))

    return StepOutput(
        result={"has_next": bool(queue)},
        observations=(
            f"Grounding verdict for {entry['paper_key']}: "
            f"{len(ungrounded)} ungrounded so far ({len(queue)} remaining)"
        ),
        context_updates=context_updates,
    )


async def action_apply_research_gate_results(step_input: StepInput) -> StepOutput:
    """DERIVED verdict: pass iff no coverage issues AND no ungrounded tags.

    Builds gate_results {verdict, blocking_issues, stats} and pushes
    telemetry notes per issue (research_control's harvest freshens
    notes before saving — the lost-update guard).

    Context: mission, coverage_report, ungrounded_tags
    Result: all_passing; Publishes: gate_results
    """
    from agent.actions.scholarly_actions import read_databank

    effects = step_input.effects
    coverage = list(step_input.context.get("coverage_report") or [])
    ungrounded = list(step_input.context.get("ungrounded_tags") or [])

    issues = list(coverage)
    for tag in ungrounded:
        issues.append(
            {
                "class": "grounding",
                "paper_key": tag.get("paper_key", ""),
                "aspect": tag.get("aspect", ""),
                "claimed_relevance": tag.get("relevance", ""),
                "detail": tag.get("reason", ""),
            }
        )

    databank = await read_databank(effects)
    statuses = [r.get("status") for r in databank.values()]
    stats = {
        "papers": len(databank),
        "cataloged": statuses.count("cataloged"),
        "pdfs": sum(1 for r in databank.values() if r.get("pdf_path")),
        "closed": sum(
            1 for r in databank.values() if r.get("access_status") == "closed"
        ),
    }

    all_passing = not issues
    if effects:
        for issue in issues:
            if issue["class"] == "coverage":
                content = (
                    f"Research gate issue: aspect '{issue['aspect']}' coverage "
                    f"{issue['have']}/{issue['want']}"
                    + (" (adjacent-only hits)" if issue.get("adjacent_only") else "")
                )
            else:
                content = (
                    f"Research gate issue: ungrounded tag '{issue['aspect']}' "
                    f"({issue['claimed_relevance']}) on {issue['paper_key']} — "
                    f"{issue['detail'] or 'justification unsupported by abstract'}"
                )
            await effects.push_note(
                content=content,
                category="failure_analysis",
                tags=[issue["class"]],
                source_flow="research_gate",
            )

    return StepOutput(
        result={
            "all_passing": all_passing,
            "coverage_issues": len(coverage),
            "ungrounded": len(ungrounded),
        },
        observations=(
            f"Research gate: {'PASS' if all_passing else 'FAIL'} — "
            f"{len(coverage)} coverage issue(s), {len(ungrounded)} ungrounded tag(s); "
            f"corpus {stats['cataloged']}/{stats['papers']} cataloged, "
            f"{stats['pdfs']} PDF(s)"
        ),
        context_updates={
            "gate_results": {
                "verdict": "pass" if all_passing else "fail",
                "blocking_issues": issues,
                "stats": stats,
            }
        },
    )
