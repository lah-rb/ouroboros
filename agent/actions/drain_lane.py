"""Domain-free pieces of a drain lane.

WHAT A DRAIN LANE IS. A worker-pool lane runs one flow in a loop against a
resource cap (agent/scheduler/worker_pool.py). Each round: read a budget from
the environment, select an unclaimed slice of pending work, claim it, do it,
release the claim, and report a summary the pool can classify as work or as a
stand-down. Three lanes — figtext, ocr, curate — had grown three copies of
those six concerns, and the copies had drifted apart in ways that mattered:

  * the ENV BUDGET reader existed three times (plus a fourth variant in
    preocr_triage.py that reads at IMPORT time, so a station's env never
    reaches it and monkeypatch.setenv cannot move it in tests)
  * the DECLINE shape was rebuilt inline each time, and it is load-bearing:
    _did_work treats a blob carrying `reason` as a stand-down and anything
    else with a positive count as work. Getting that wrong is what let the
    OCR drain report "attempted 4" against a dead server for 2,864 rounds,
    looping as fast as the toolchain could fail.
  * SELECTION was count-bounded in one lane and cost-bounded in another. The
    count-bounded form takes N items regardless of their size, so an OCR
    round of four 100-page papers and one of four 5-page papers differ ~20x
    in duration against a single fixed EXTRACT_TIMEOUT_S.
  * the PARK idiom (status + reason recorded, never fatal, caches evicted)
    had three implementations and zero sharing, each citing the last as
    precedent.

WHY NOT agent/actions/fanout.py. That module is the fan-out gate for TEXT
work: it sizes bursts against kvPoolTokens with a chars/4*1.3 token estimate.
Drain lanes served by something other than the batched text pool declare
`est_kv=0, seats=0` precisely because they draw no text cells, so fanout's
budget dimension does not apply to them. Same spirit, different currency;
forcing one into the other would put a KV model in front of work that has no
KV. (fanout.py also already has a divergent inline copy in
contract_swarm_actions.py — a third variant is exactly what this module is
meant to stop.)

WHAT STAYS PUT. `is_toolchain_fault` lives in extraction_actions.py and is
already imported by the figtext booking loop — the one piece of cross-lane
reuse that happened on its own. It is not moved here; a working seam does not
need relocating.
"""

from __future__ import annotations

import contextlib
import logging
import os
from typing import Any, Callable, Iterable, Iterator, Optional, Sequence

logger = logging.getLogger(__name__)


# ── budget ───────────────────────────────────────────────────────────


def drain_budget(env_name: str, default: int) -> int:
    """Per-round budget from the environment. 0 (or negative) disables.

    READ AT CALL TIME, NOT IMPORT TIME. A station sets these vars when it
    launches a mission, long after this module is imported, and tests flip
    them with monkeypatch.setenv between cases. The import-time variant
    (preocr_triage.py's _PREOCR_PAPERS) silently ignores both.

    A malformed value falls back to the default rather than raising: a typo in
    an env var must not take a lane down, and the default is always a working
    configuration.
    """
    raw = (os.environ.get(env_name) or "").strip()
    if not raw:
        return default
    try:
        return max(0, int(raw))
    except ValueError:
        logger.warning(
            "%s=%r is not an integer — falling back to %d", env_name, raw, default
        )
        return default


# ── claims ───────────────────────────────────────────────────────────


class ClaimSet:
    """In-process claim set for one work kind.

    WHY CLAIMS AT ALL. Nothing marks an item in-flight: status is written
    AFTER the work, so two selectors running concurrently — two lanes of the
    same drain, or a drain and a dispatch — would pick the same items and do
    them twice. Plain set operations suffice because every selector runs on
    the one asyncio loop with no awaits between check and claim.

    IN-PROCESS ONLY. A second agent process would need a flock'd claim file
    (the events queue is the precedent). Until then, one mission = one
    process, and that is what makes N lanes of the same drain safe: they
    share this set, so they claim disjoint slices.
    """

    __slots__ = ("name", "_held")

    def __init__(self, name: str) -> None:
        self.name = name
        self._held: set[str] = set()

    # Set-shaped on purpose. The lanes' module-level claim sets are named in
    # production callers (acquire_overlap_actions) and reached into by tests
    # (`.clear()`, `.add()`, truthiness), so this type replaces a bare `set`
    # in place rather than forcing every call site to change at once.
    def __contains__(self, key: str) -> bool:
        return key in self._held

    def __len__(self) -> int:
        return len(self._held)

    def __bool__(self) -> bool:
        return bool(self._held)

    def __iter__(self) -> Iterator[str]:
        return iter(self._held)

    def __repr__(self) -> str:
        return f"ClaimSet({self.name!r}, {len(self._held)} held)"

    def add(self, key: str) -> None:
        self._held.add(key)

    def clear(self) -> None:
        self._held.clear()

    def update(self, keys: Iterable[str]) -> None:
        self._held.update(keys)

    def difference_update(self, keys: Iterable[str]) -> None:
        self._held.difference_update(list(keys))

    def claim(self, keys: Iterable[str]) -> list[str]:
        ks = list(keys)
        self._held.update(ks)
        return ks

    def release(self, keys: Iterable[str]) -> None:
        self._held.difference_update(list(keys))

    @contextlib.contextmanager
    def scope(self, keys: Sequence[str]) -> Iterator[Sequence[str]]:
        """Hold `keys` for the body, release them however it exits.

        The release must survive an exception, a cancellation and an early
        return alike: a leaked claim is invisible (the item simply stops being
        selectable) and only shows up later as a queue that will not drain.
        Callers pass the ORIGINALLY claimed list, not a filtered one — work
        that triage or sizing dropped mid-round is still claimed.
        """
        held = list(keys)
        try:
            yield held
        finally:
            self.release(held)


# ── stand-down ───────────────────────────────────────────────────────


def decline(
    reason: str,
    *,
    summary_key: str,
    counters: Optional[dict] = None,
    label: Optional[str] = None,
    extra: Optional[dict] = None,
):
    """Build the StepOutput for a round that deliberately did nothing.

    THE `reason` KEY IS THE CONTRACT. WorkerPool._did_work treats any result
    blob carrying a truthy `reason` as a stand-down and backs the lane off;
    without it a zero-work round with a positive counter reads as work and the
    lane spins. This helper exists so that contract is satisfied by
    construction rather than by remembering.

    `counters` are the lane's zeroed work verbs (e.g. {"attempted": 0}) so the
    summary keeps a stable shape across working and idle rounds — a consumer
    reading `attempted` should not have to handle its absence.
    """
    from agent.models import StepOutput

    summary = {**(counters or {}), "reason": reason, **(extra or {})}
    name = label or summary_key.removesuffix("_summary")
    return StepOutput(
        result=summary,
        observations=f"{name} drain idle ({reason})",
        context_updates={summary_key: summary},
    )


# ── selection ────────────────────────────────────────────────────────


def select_cost_bounded(
    databank: dict,
    *,
    budget: int,
    pending: Callable[[dict], bool],
    cost: Callable[[str, dict], int],
    sort_key: Callable[[str, dict], tuple],
    claims: ClaimSet,
    max_items: Optional[int] = None,
) -> list[str]:
    """Claim a slice whose total COST fits `budget`, never starving big items.

    Cost-bounded, not count-bounded: the budget is the thing a round actually
    spends (figures to describe, pages to OCR), so round duration stays
    roughly constant instead of varying with whatever the queue happens to
    hold. A count-bounded round of four 100-page papers and one of four
    5-page papers differ ~20x against one fixed timeout.

    THE NEVER-STARVE ESCAPE. When nothing fits whole, the head item is taken
    ALONE and the tool's own per-round pool bounds the work. Without it, any
    item costing more than the budget is unreachable forever — the figtext
    lane had 693 papers at 13+ figures in exactly that state under a skip
    rule, and they could only ever have been rescued by hand.

    `cost` is called LAZILY, in sorted order, and only until the budget fills.
    That matters when cost is not free to compute (OCR opens the PDF to count
    pages): a round considers a handful of candidates, not the whole queue.

    `max_items` is a SECOND ceiling, not a replacement for the budget. A lane
    migrating from count-bounded to cost-bounded selection keeps its old
    per-round item cap alongside the new cost cap, so the change can only make
    rounds smaller or more even — never larger than what already ran in
    production.
    """
    if budget <= 0 or (max_items is not None and max_items <= 0):
        return []
    candidates = sorted(
        ((k, r) for k, r in databank.items() if k not in claims and pending(r)),
        key=lambda kr: sort_key(kr[0], kr[1]),
    )
    batch: list[str] = []
    spent = 0
    for key, rec in candidates:
        if max_items is not None and len(batch) >= max_items:
            break
        try:
            n = max(0, int(cost(key, rec)))
        except Exception:  # noqa: BLE001 — an unsizable item is not a crash
            logger.debug("cost() failed for %s; treating as 1", key, exc_info=True)
            n = 1
        if spent + n > budget:
            if batch:
                break
            # Nothing fits whole. Take the head alone rather than skipping it
            # forever; the tool's own budget caps the round.
            batch.append(key)
            break
        batch.append(key)
        spent += n
        # No early break at spent == budget: a zero-cost item is still
        # eligible, and stopping here would change the behaviour the figtext
        # packer has been running with. The next positive-cost item exits the
        # loop on its own.
    return claims.claim(batch)


# ── server liveness ──────────────────────────────────────────────────


async def server_alive(effects: Any) -> bool:
    """Is the inference server reachable right now?

    WHY A LANE ASKS BEFORE BOOKING. Zero results is the shape of a DEAD
    SERVER, not of bad inputs: a tool whose every call fails to connect exits
    with nothing to say. Booking failure verdicts on that mass-marked 965
    papers figtext_failed on the 2026-08-22 port day and 437 more during the
    2026-08-26 server fault — both mass-heals. A reachable server with zero
    results still books, because a permanently broken tool must not spin.

    Unreachable is the signal, so every failure mode answers False.
    """
    if effects is None:
        return False
    probe = getattr(effects, "inference_pool_health", None)
    if probe is None:
        # An effects implementation without the probe cannot be interrogated;
        # assume alive so the absence of a probe never blocks a lane.
        return True
    try:
        return bool(await probe())
    except Exception:  # noqa: BLE001 — unreachable is the answer, not an error
        return False


# ── park ─────────────────────────────────────────────────────────────


async def park(
    effects: Any,
    paper_key: str,
    status: str,
    reason: str,
    *,
    evict: Sequence[dict] = (),
) -> None:
    """Park an item the lane can never process — a REVIEW QUEUE, not a
    rejection.

    Status plus reason go to the extraction sidecar, where they are listable
    and clearable by hand (tools/extract_triage.py). The distinction from a
    failure verdict is the whole point: a park says "this needs a decision",
    not "this is bad", and the operator can reverse it when budgets or
    geometry change.

    NEVER FATAL. A park that raised would take down the round that was trying
    to be careful, so a booking failure is logged and swallowed. `evict` names
    in-process caches to drop the key from, so a parked item does not keep
    occupying memory it will never use again.
    """
    from agent.actions.scholarly_actions import (
        EXTRACTION_PATH,
        _read_jsonl_records,
        append_extraction_records,
    )

    try:
        # FULL ROW, NOT A STUB. The sidecar is last-row-wins on read, so a
        # three-field park row shadowed every extraction field the paper had --
        # md_path, figure_count, extraction_quality, and translated/md_en_path on
        # translated papers. Measured 2026-09-06 on curate parks; this shared
        # idiom did the same for every drain. Read the key's current row and
        # change only what the park owns.
        try:
            current = (await _read_jsonl_records(effects, EXTRACTION_PATH)).get(
                paper_key
            ) or {}
        except Exception:  # noqa: BLE001 -- enrichment only; a park must still land
            current = {}
        row = dict(current)
        row.update(
            paper_key=paper_key, extraction_status=status, failure_reason=reason[:300]
        )
        await append_extraction_records(effects, [row])
        logger.warning("%s: parked %s — %s", status, paper_key, reason[:160])
    except Exception:  # noqa: BLE001 — a park must not break the lane
        logger.exception("failed to book %s for %s", status, paper_key)
    for cache in evict:
        try:
            cache.pop(paper_key, None)
        except Exception:  # noqa: BLE001
            pass
