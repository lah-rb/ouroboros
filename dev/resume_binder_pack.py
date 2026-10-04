#!/usr/bin/env python3
"""Finish a partially-packed paper by re-running ONLY the windows it lost.

Operator direction (2026-09-07): "clean up the corrupted packs, and resume
them without redoing the correct packs." A provider session limit came back
as ordinary text mid-run, so the gates read it as a bad answer and five
binder papers booked artifacts merged from just the windows that had already
passed (see the provider-limit memory). Their data is VALID -- it cleared the
gates -- but incomplete, and a whole-paper repack would spend the model again
on windows that were already right.

WHAT THIS DOES. For each paper: rebuild the same section-bounded windows from
the same raw curator doc (deterministic -- same doc, same target/cap, and the
window count is checked against the booked pack_quality.windows before
anything runs), re-pack only the named window indices with the production
prompt and gates, merge what passes INTO the existing artifact's data, re-run
the whole-document gates on the merge, and book. Everything but the window
selection is the production path: `curation_actions._pack_windowed` is
mirrored call-for-call so a resumed pack is indistinguishable from a clean
one.

WHICH WINDOWS. Default: the ones whose booked feedback is the limit's
signature ("output was not a JSON object"). --also-stale-gate adds windows
that failed on grounding BEFORE a gate fix landed -- their verdict is stale,
not wrong (2026-09-07: the raised-dot decimal fix, a08f30c).

MERGE ORDER matters: the existing data goes FIRST, so `merge_packs`'s
first-wins rule keeps the already-booked value on any scalar disagreement and
the resumed windows only ever ADD.

  .venv/bin/python dev/resume_binder_pack.py --list
  .venv/bin/python dev/resume_binder_pack.py --dry-run
  .venv/bin/python dev/resume_binder_pack.py --also-stale-gate --concurrency 2
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.actions import curation_actions as ca  # noqa: E402
from agent.actions.pack_windows import (  # noqa: E402
    MergeReport,
    merge_packs,
    repair_shapes,
    window_sections,
)
from agent.actions.scholarly_actions import read_databank  # noqa: E402
from agent.llm_json import parse_llm_json  # noqa: E402
from agent.models import FlowMeta, StepInput  # noqa: E402

from pack_binder_sonnet import CORPUS, MODEL, SonnetEffects  # noqa: E402

#: The provider-limit signature in a booked window outcome. A limit message is
#: returned as ordinary text, so the parse step is what records it.
LIMIT_FEEDBACK = "not a JSON object"


def windows_to_redo(record: dict, *, stale_gate: bool) -> tuple[list[int], list[dict]]:
    """(indices to re-run, outcomes to keep verbatim)."""
    q = record.get("pack_quality") or {}
    outcomes = list(q.get("window_outcomes") or [])
    redo, keep = [], []
    for o in outcomes:
        if o.get("passed"):
            keep.append(o)
            continue
        feedback = str(o.get("feedback") or "")
        if LIMIT_FEEDBACK in feedback or stale_gate:
            redo.append(int(o.get("window", -1)))
        else:
            keep.append(o)
    return [i for i in redo if i >= 0], keep


async def resume_one(
    eff: SonnetEffects,
    key: str,
    rec: dict,
    *,
    stale_gate: bool,
    book_lock: asyncio.Lock,
    dry_run: bool,
    log_path: str,
) -> dict:
    t0 = time.monotonic()
    out: dict = {"paper_key": key, "started": datetime.now(timezone.utc).isoformat()}
    q = rec.get("pack_quality") or {}

    doc = await ca._raw_curator_doc(eff, key)
    target, cap = ca._pack_window_sizes()
    windows = window_sections(doc, target, cap)
    booked_n = q.get("windows")
    if booked_n and len(windows) != int(booked_n):
        # The window split must be the one the booked outcomes refer to, or an
        # index means something different now and the merge would be nonsense.
        out.update(
            status="skipped",
            reason=f"window split moved: {len(windows)} now vs {booked_n} booked",
        )
        return out

    redo, keep = windows_to_redo(rec, stale_gate=stale_gate)
    if not redo and rec.get("pack_status") == "packed":
        out.update(status="nothing_to_do", reason="no lost windows")
        return out
    if rec.get("pack_status") != "packed":
        # No artifact to preserve (pack_failed): every window is fair game,
        # whatever the booked outcomes said -- Laue 1912 failed its only
        # window on the decimal blind spot that a08f30c fixed.
        redo, keep = [w.index for w in windows], []

    existing: dict = {}
    ds = rec.get("dataset_path") or ""
    if ds:
        fc = await eff.read_file(ds)
        if getattr(fc, "exists", False):
            try:
                existing = (json.loads(fc.content) or {}).get("data") or {}
            except Exception:  # noqa: BLE001
                existing = {}
    out.update(redo=redo, kept_windows=len(keep), existing_keys=len(existing))
    if dry_run:
        out["status"] = "dry_run"
        return out

    registry = await ca._load_registry(eff)
    aliases = await ca.load_key_aliases(eff)
    multi = len(windows) > 1
    new_packs: list[dict] = []
    attempts = 0
    outcomes = list(keep)
    for idx in redo:
        w = windows[idx]
        preface = (
            ca._PACK_PREFACE.format(
                n=w.index + 1,
                total=len(windows),
                sections=w.section_count,
                heading=w.first_heading,
            )
            if multi
            else ""
        )
        # The paper's vocabulary so far: what the kept windows already packed.
        prior_keys = sorted(set(existing) | {k for p in new_packs for k in p})
        feedback = ""
        gates: dict = {"passed": False, "feedback": ""}
        data = None
        w_attempts = 0
        for _ in range(2):
            w_attempts += 1
            attempts += 1
            ctx = {
                "key_registry_block": ca.format_key_registry(registry),
                "gate_feedback": feedback,
            }
            if prior_keys:
                ctx["prior_keys"] = ", ".join(prior_keys[:120])
            prompt = await ca._render_prompt("curator/pack_data", ctx)
            try:
                text = await ca._curate_turn(
                    eff, preface + w.text + "\n\n---\n\n" + prompt, 8192
                )
            except ca._CurateTransportFault as exc:
                # A limit or a dead transport is not a verdict: stop the paper
                # with its existing pack untouched rather than book worse.
                out.update(
                    status="transport_fault",
                    reason=str(exc)[:200],
                    seconds=round(time.monotonic() - t0),
                )
                return out
            parsed = parse_llm_json(text)
            data = parsed if isinstance(parsed, dict) and parsed else None
            if data is not None:
                data = ca.canonicalize_pack_keys(data, aliases)
                data, _ = repair_shapes(data, registry)
            gates = (
                ca._run_pack_gates(data, w.text, registry)
                if data is not None
                else {"passed": False, "feedback": "output was not a JSON object"}
            )
            if gates["passed"]:
                break
            feedback = gates["feedback"]
        g = gates.get("grounding") or {}
        outcomes.append(
            {
                "window": w.index,
                "tokens": w.tokens,
                "sections": w.section_count,
                "oversize": w.oversize,
                "passed": bool(gates["passed"]),
                "attempts": w_attempts,
                "grounding_rate": g.get("grounding_rate"),
                "numeric_leaves": g.get("numeric_leaves"),
                "feedback": (
                    "" if gates["passed"] else str(gates.get("feedback") or "")[:300]
                ),
                "resumed": True,
            }
        )
        if gates["passed"] and data:
            new_packs.append(data)

    if not new_packs:
        out.update(
            status="no_new_windows",
            reason="every resumed window failed its gates",
            seconds=round(time.monotonic() - t0),
        )
        _log(log_path, out)
        return out

    report = MergeReport()
    merged = merge_packs(([existing] if existing else []) + new_packs, report)
    final = ca._run_pack_gates(merged, doc, registry)
    outcomes.sort(key=lambda o: o.get("window", 0))
    passed_n = sum(1 for o in outcomes if o.get("passed"))
    if not final["passed"]:
        # The merge lost the whole-document gate: keep what is booked.
        out.update(
            status="merge_failed",
            reason=str(final["feedback"])[:300],
            seconds=round(time.monotonic() - t0),
        )
        _log(log_path, out)
        return out

    pack_state = {
        "status": "packed",
        "data": merged,
        "attempts": attempts,
        "quality": {
            "grounding_rate": final["grounding"]["grounding_rate"],
            "numeric_leaves": final["grounding"]["numeric_leaves"],
            "ungrounded": final["grounding"]["ungrounded"],
            "new_keys": len(final["registry"]["new_keys"]),
            "reused_keys": len(final["registry"]["reused_keys"]),
            "near_duplicate_flags": final["near_dups"],
            "parse_attempts": attempts,
            "windows": len(windows),
            "windows_passed": passed_n,
            "window_conflicts": report.conflicts[:25],
            "window_outcomes": outcomes,
            "resumed_windows": redo,
        },
    }
    state = {
        "paper_key": key,
        "session_id": "",
        "review": {
            "status": "accepted",
            "summary": str(rec.get("review_summary") or ""),
            "issues": list(rec.get("review_issues") or []),
            "deny_category": "",
            "document_form": str(rec.get("review_document_form") or ""),
        },
        "pack": pack_state,
        "pack_doc_form": str(rec.get("pack_doc_form") or "raw-oversize"),
    }
    async with book_lock:
        res = await ca.action_curate_book_result(
            StepInput(
                context={"curate_state": state},
                effects=eff,
                meta=FlowMeta(flow_name="binder_pack_resume", step_id="book_result"),
            )
        )
    out.update(
        status="packed",
        windows=len(windows),
        windows_passed=passed_n,
        grounding_rate=pack_state["quality"]["grounding_rate"],
        numeric_leaves=pack_state["quality"]["numeric_leaves"],
        keys_before=len(existing),
        keys_after=len(merged),
        booked=res.observations[:160],
        seconds=round(time.monotonic() - t0),
    )
    _log(log_path, out)
    return out


def _log(path: str, row: dict) -> None:
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--keys", nargs="*", default=None)
    ap.add_argument(
        "--also-stale-gate",
        action="store_true",
        help="also re-run windows that failed grounding before a gate fix",
    )
    ap.add_argument("--concurrency", type=int, default=2)
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--log", default=os.path.expanduser("~/tmp/binder_resume.jsonl"))
    args = ap.parse_args()

    eff = SonnetEffects(CORPUS)
    db = await read_databank(eff)
    todo = []
    for k, r in sorted(db.items()):
        if not r.get("binder") or r.get("review_status") != "accepted":
            continue
        if args.keys and k not in args.keys:
            continue
        redo, keep = windows_to_redo(r, stale_gate=args.also_stale_gate)
        never_packed = r.get("pack_status") != "packed"
        if not redo and not never_packed:
            continue
        todo.append((k, r, redo, keep, never_packed))

    print(f"{len(todo)} paper(s) to resume with {MODEL}")
    for k, r, redo, keep, never in todo:
        q = r.get("pack_quality") or {}
        tag = "NEVER PACKED" if never else f"{len(keep)} kept"
        print(
            f"  {str(r.get('pack_status')):12s} windows={q.get('windows')} redo={redo or 'all'} {tag}  {k[:52]}"
        )
    if args.list or not todo:
        return 0

    sem = asyncio.Semaphore(args.concurrency)
    lock = asyncio.Lock()

    async def guarded(k, r):
        async with sem:
            if eff.limited:
                print(f"  SKIPPED (provider limit) {k[:56]}", flush=True)
                return {"paper_key": k, "status": "skipped_limit"}
            res = await resume_one(
                eff,
                k,
                r,
                stale_gate=args.also_stale_gate,
                book_lock=lock,
                dry_run=args.dry_run,
                log_path=args.log,
            )
            print(
                f"  [{datetime.now().strftime('%H:%M:%S')}] {str(res.get('status')):16s} "
                f"win={res.get('windows_passed')}/{res.get('windows')} "
                f"ground={res.get('grounding_rate')} keys {res.get('keys_before')}->{res.get('keys_after')} "
                f"{res.get('seconds')}s {k[:46]} {str(res.get('reason') or '')[:70]}",
                flush=True,
            )
            return res

    results = await asyncio.gather(*(guarded(k, r) for k, r, *_ in todo))
    ok = sum(1 for r in results if r.get("status") == "packed")
    print(
        f"\nDONE: {ok}/{len(results)} resumed; claude calls {eff.calls}, {eff.call_seconds/60:.1f} call-min"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
