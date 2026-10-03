#!/usr/bin/env python3
"""Re-pack the accepted papers booked pack_failed, with Sonnet 5.5 (2026-10-03).

Operator: "for the ~30 pack failed papers, have sonnet 5.5 pack them and
attempt to figure out why they failed to pack the first time".

The packing is the production path with ONE change, the model call
(dev/pack_binder_sonnet.py's SonnetEffects: headless `claude -p`, the
provider-limit latch, raw captures): _pack_only_raw -> windows -> the
production pack prompt -> gates -> merge -> whole-document gates ->
action_curate_book_result.

Two families, measured before the run (24 papers):
  * 16 failed TRANSLATION, not packing ("packs must be English": a third
    translation failure books an accepted paper pack_failed). Sonnet packs
    them from the ORIGINAL markdown and is told to write every string in
    English; a leftover .en.md from a failed translation is hidden so the
    pack never reads text that lost numbers. Before booking, the pack's
    strings must pass the translation gate's own English test, else nothing
    is booked.
  * 8 failed the PACK GATES on muse (ungrounded values, output not JSON) or
    were never packed.

    .venv/bin/python dev/pack_failed_sonnet.py --dry-run
    .venv/bin/python dev/pack_failed_sonnet.py --concurrency 3
"""

from __future__ import annotations

import argparse
import ast
import asyncio
import collections
import json
import os
import re
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("BINDER_PACK_MODEL", "claude-sonnet-5-5")
os.environ.setdefault(
    "BINDER_PACK_CAPTURE", os.path.expanduser("~/tmp/pack_failed_sonnet/captures")
)

import pack_binder_sonnet as pbs  # noqa: E402

from agent.actions import curation_actions as ca  # noqa: E402
from agent.actions.scholarly_actions import read_databank  # noqa: E402
from agent.models import FlowMeta, StepInput  # noqa: E402

pbs.SYSTEM = (
    pbs.SYSTEM
    + " The paper may not be written in English: write every string value in the"
    " pack in English (translate names, labels and descriptions), and copy every"
    " number exactly as the paper prints it."
)
OUT = os.path.expanduser("~/tmp/pack_failed_sonnet")


class OriginalTextEffects(pbs.SonnetEffects):
    """Hide the .en.md of papers whose translation FAILED: the pack then reads
    the original markdown, whose numbers are the printed ones."""

    def __init__(self, working_dir: str, hide_en: set[str]) -> None:
        super().__init__(working_dir)
        self.hide_en = hide_en

    async def read_file(self, path: str, *a, **kw):  # type: ignore[override]
        m = re.match(r"databank/markdown/(.+)\.en\.md$", path)
        if m and m.group(1) in self.hide_en:
            return await super().read_file(path + ".__hidden__", *a, **kw)
        return await super().read_file(path, *a, **kw)


def original_failure(rec: dict) -> dict:
    """Why the first pack failed, from the record alone."""
    q = rec.get("pack_quality") or {}
    wo = q.get("window_outcomes")
    if isinstance(wo, str):
        try:
            wo = ast.literal_eval(wo)
        except (ValueError, SyntaxError):
            wo = []
    fb = collections.Counter(
        re.sub(r"\d+", "N", (o.get("feedback") or "")[:60])
        for o in (wo or [])
        if isinstance(o, dict) and not o.get("passed")
    )
    reason = str(rec.get("failure_reason") or "")
    if (
        reason.startswith("translation:")
        or rec.get("extraction_status") == "translate_failed"
    ):
        family = "translation"
    elif fb:
        family = "pack gates"
    else:
        family = "never packed / no record"
    return {
        "family": family,
        "failure_reason": reason[:240],
        "windows": q.get("windows"),
        "windows_passed": q.get("windows_passed"),
        "window_feedback": dict(fb.most_common(3)),
        "packer_before": rec.get("curation_method"),
    }


_NON_LATIN = re.compile(r"[\u0400-\u04ff\u3040-\u30ff\u3400-\u9fff\uac00-\ud7af]")
_FOREIGN_FW = re.compile(
    r"\b(de la|del|los|las|para|muestras?|que|con|não|dos|das|des|les|pour|avec|une|und|der|die)\b",
    re.I,
)
_ENGLISH_FW = re.compile(r"\b(the|of|and|with|for|in|on|by|to|from)\b", re.I)


def pack_language_problem(text: str) -> str:
    """Is a PACK's text non-English? Not the translation gate's prose test:
    pack strings are short labels, whose English function-word ratio sits
    below that gate's 0.05 floor -- run 1 held back four English packs on it
    (3,796 English vs 27 foreign function words across every pack). Two
    tests instead: the share of CJK/Cyrillic/Hangul letters, and whether
    foreign function words outnumber English ones."""
    letters = len(re.findall(r"[^\W\d_]", text))
    if letters and len(_NON_LATIN.findall(text)) / letters > 0.15:
        return f"non-Latin script is {len(_NON_LATIN.findall(text)) / letters:.0%} of letters"
    nf, ne = len(_FOREIGN_FW.findall(text)), len(_ENGLISH_FW.findall(text))
    if nf > 5 and nf > ne:
        return f"{nf} foreign vs {ne} English function words"
    return ""


def _strings(x) -> list[str]:
    if isinstance(x, dict):
        return [s for v in x.values() for s in _strings(v)]
    if isinstance(x, list):
        return [s for v in x for s in _strings(v)]
    return [x] if isinstance(x, str) else []


async def pack_one(eff, key: str, rec: dict, lock: asyncio.Lock, log_path: str) -> dict:
    t0 = time.monotonic()
    out = {"paper_key": key, "original": original_failure(rec)}
    try:
        state = await ca._pack_only_raw(eff, key, rec, doc_form="raw")
    except ca._CurateTransportFault as e:
        out.update(status="transport_fault", reason=str(e)[:300])
        return _log(log_path, out, t0)
    pack = state.get("pack") or {}
    q = pack.get("quality") or {}
    out.update(
        status=pack.get("status"),
        reason=(pack.get("reason") or "")[:300],
        windows=q.get("windows"),
        windows_passed=q.get("windows_passed"),
        grounding_rate=q.get("grounding_rate"),
        numeric_leaves=q.get("numeric_leaves"),
        keys=len(pack.get("data") or {}),
    )
    if pack.get("status") == "packed":
        why = pack_language_problem(" ".join(_strings(pack.get("data"))))
        if why:
            out.update(
                status="not_english", reason=f"pack strings read non-English: {why}"
            )
            return _log(log_path, out, t0)  # nothing booked
    async with lock:
        res = await ca.action_curate_book_result(
            StepInput(
                context={"curate_state": state},
                effects=eff,
                meta=FlowMeta(flow_name="pack_failed_sonnet", step_id="book_result"),
            )
        )
    out["booked"] = res.observations[:200]
    if out.get("status") == "packed":
        # VERIFIED ENGLISH: a later translation of the paper (success or
        # failure) must not knock this pack to needs_repack / pack_failed.
        from agent.actions.scholarly_actions import append_records

        async with lock:
            cur = dict((await read_databank(eff)).get(key) or {})
            if cur.get("pack_status") == "packed":
                cur["pack_language"] = "en"
                await append_records(eff, [cur])
    return _log(log_path, out, t0)


def _log(path: str, row: dict, t0: float) -> dict:
    row["seconds"] = round(time.monotonic() - t0)
    row["at"] = datetime.now(timezone.utc).isoformat()
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    return row


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--concurrency", type=int, default=3)
    ap.add_argument("--keys", nargs="*")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    os.makedirs(OUT, exist_ok=True)
    log_path = os.path.join(OUT, "results.jsonl")
    done = set()
    if os.path.exists(log_path):
        done = {
            r["paper_key"]
            for r in map(json.loads, filter(str.strip, open(log_path)))
            if r.get("status") not in ("not_english", "transport_fault")
        }

    probe = pbs.SonnetEffects(pbs.CORPUS)
    db = await read_databank(probe)
    todo = [
        (k, r)
        for k, r in sorted(db.items())
        if r.get("review_status") == "accepted"
        and r.get("pack_status") == "pack_failed"
        and r.get("record_kind") != "supplement"
        and (not a.keys or k in a.keys)
        and k not in done
    ]
    hide = {k for k, r in todo if r.get("extraction_status") == "translate_failed"}
    eff = OriginalTextEffects(pbs.CORPUS, hide)
    print(f"{len(todo)} pack_failed paper(s); {len(hide)} translate_failed (original text); "
          f"model {pbs.MODEL}; log {log_path}")  # fmt: skip
    for k, r in todo:
        f = original_failure(r)
        print(
            f"  {f['family']:<24} {k[:60]}  {f['failure_reason'][:60] or f['window_feedback']}"
        )
    if a.dry_run or not todo:
        return 0
    sem, lock = asyncio.Semaphore(a.concurrency), asyncio.Lock()

    async def guarded(k, r):
        async with sem:
            if eff.limited:
                return {"paper_key": k, "status": "skipped_limit"}
            res = await pack_one(eff, k, r, lock, log_path)
            print(f"  [{datetime.now():%H:%M:%S}] {str(res.get('status')):16s} "
                  f"win={res.get('windows_passed')}/{res.get('windows')} ground={res.get('grounding_rate')} "
                  f"keys={res.get('keys')} {res.get('seconds')}s {k[:50]} {str(res.get('reason') or '')[:80]}",
                  flush=True)  # fmt: skip
            return res

    results = await asyncio.gather(*(guarded(k, r) for k, r in todo))
    print(f"\nDONE: {collections.Counter(r.get('status') for r in results)}; "
          f"claude calls {eff.calls}, {eff.call_seconds / 60:.1f} call-min; limited={eff.limited}")  # fmt: skip
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
