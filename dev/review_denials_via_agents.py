#!/usr/bin/env python3
"""Re-judge existing curator denials from their own summaries, cheaply.

WHY (operator, 2026-09-05). The qwen curator denied 178 papers. 120 of its
139 "corpus_fit" denials name an in-scope technique IN THEIR OWN SUMMARY:
the paper measured the right things on the right materials but reported a
peak table or a composition table instead of the raw trace, and the curator
refused it and called that a fit problem. Peak lists and compositions are
the corpus's most valuable content, so the review prompt has been loosened
-- and the papers already denied under the old rule are worth recovering.

The cheap pass: the curator's own summary and issues are the evidence. A
summary that says "reports XRD, Raman, XPS and UV-Vis data" convicts the
denial without anyone re-reading a 178k-token thesis. Sub-agents sort every
denial into:

  recover    the summary itself shows in-scope measurements on named
             materials -> re-arm for a full review under the new prompt
  borderline arguable either way -> left denied, listed for the operator
  clean      genuinely off-corpus, or no values of any kind -> stays denied

NOTHING IS ACCEPTED HERE. "recover" only clears the verdict so the paper
re-enters the queue; the full review still decides, and it can deny again.

    python dev/review_denials_via_agents.py prepare --out ~/tmp/denial_review
    python dev/review_denials_via_agents.py book --in ~/tmp/denial_review [--apply]
"""

from __future__ import annotations

import argparse
import asyncio
import collections
import glob
import json
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.actions.scholarly_actions import (  # noqa: E402
    DATABANK_PATH,
    _read_jsonl_records,
    append_records,
    read_databank,
)
from agent.effects.local import LocalEffects  # noqa: E402

ROOT = os.path.expanduser("~/corpora/ouroboros-spectra")
PER_JOB = 20


def _is_curator_denial(r: dict) -> bool:
    return (
        r.get("review_status") == "denied"
        and "FRONT-MATTER TRIAGE" not in str(r.get("review_summary") or "")
        and bool(str(r.get("review_summary") or "").strip())
    )


async def prepare(a) -> int:
    bank = await read_databank(LocalEffects(a.root))
    rows = [
        {
            "key": k,
            "title": str(r.get("title") or "")[:200],
            "language": r.get("language"),
            "deny_category": r.get("deny_category"),
            "source_aspects": r.get("source_aspects") or [],
            "curator_summary": str(r.get("review_summary") or "")[:1200],
            "curator_issues": [str(i)[:300] for i in (r.get("review_issues") or [])][
                :6
            ],
        }
        for k, r in sorted(bank.items())
        if _is_curator_denial(r)
    ]
    if a.limit:
        rows = rows[: a.limit]
    os.makedirs(a.out, exist_ok=True)
    jobs = [rows[i : i + PER_JOB] for i in range(0, len(rows), PER_JOB)]
    for i, job in enumerate(jobs):
        json.dump(
            job, open(f"{a.out}/job_{i:02d}.json", "w"), indent=1, ensure_ascii=False
        )
    print(f"{len(rows)} curator denials -> {len(jobs)} jobs of <={PER_JOB} in {a.out}")
    print(
        "  by category:",
        dict(collections.Counter(str(r["deny_category"]) for r in rows)),
    )
    return 0


async def verify(a) -> int:
    """Every out_NN.json must answer exactly its job_NN.json, key for key.

    WHY THIS IS NOT OPTIONAL. The agents write their answers as files in a
    shared directory, and one run reported a scratch file of its own being
    overwritten by another agent mid-task. The job files themselves are
    read-only and were checksum-verified unchanged, but "the answers I read
    back are the answers to the questions I asked" is exactly the property a
    shared filesystem does not give you for free. A foreign key here would
    book a verdict against the wrong paper.
    """
    problems: collections.Counter = collections.Counter()
    judged = 0
    for jf in sorted(glob.glob(f"{a.inp}/job_*.json")):
        n = os.path.basename(jf)[4:-5]
        of = f"{a.inp}/out_{n}.json"
        if not os.path.exists(of):
            problems["missing"] += 1
            continue
        asked = [r["key"] for r in json.load(open(jf))]
        try:
            answered = json.load(open(of)).get("verdicts") or []
        except Exception as e:  # noqa: BLE001
            print(f"  UNREADABLE out_{n}: {e}")
            problems["unreadable"] += 1
            continue
        judged += len(answered)
        got = [str(v.get("key")) for v in answered]
        if collections.Counter(got) != collections.Counter(asked):
            foreign = set(got) - set(asked)
            print(
                f"  MISMATCH out_{n}: {len(asked)} asked, {len(got)} answered, "
                f"{len(set(asked)-set(got))} missing, {len(foreign)} foreign"
            )
            for k in list(foreign)[:3]:
                print(f"      foreign key: {k[:60]}")
            problems["mismatch"] += 1
        for v in answered:
            if str(v.get("verdict")) not in ("recover", "clean", "borderline"):
                print(f"  BAD VERDICT out_{n}: {v.get('verdict')!r}")
                problems["verdict"] += 1
    print(f"\n{judged} verdicts checked; problems: {dict(problems) or 'none'}")
    fatal = problems["mismatch"] + problems["unreadable"] + problems["verdict"]
    return 1 if fatal else 0


async def book(a) -> int:
    fx = LocalEffects(a.root)
    bank = await read_databank(fx)
    side = await _read_jsonl_records(fx, DATABANK_PATH)
    got: dict[str, dict] = {}
    for f in sorted(glob.glob(f"{a.inp}/out_*.json")):
        try:
            d = json.load(open(f))
        except Exception as e:  # noqa: BLE001
            print(f"  UNREADABLE {os.path.basename(f)}: {e}")
            continue
        for row in d.get("verdicts") or []:
            k = str(row.get("key") or "")
            if k in bank and _is_curator_denial(bank[k]):
                got[k] = row
    C = collections.Counter(str(v.get("verdict")) for v in got.values())
    print(f"{len(got)} denials re-judged: {dict(C)}")
    rec_rows = [(k, v) for k, v in got.items() if str(v.get("verdict")) == "recover"]
    print(
        "  by original category:",
        dict(
            collections.Counter(str(bank[k].get("deny_category")) for k, _ in rec_rows)
        ),
    )
    for k, v in rec_rows[:8]:
        print(f"    {k[:52]:<54} {str(v.get('why'))[:70]}")
    if not a.apply:
        print("\n(dry run: nothing written)")
        return 0
    stamp = datetime.now(timezone.utc).isoformat()
    out = []
    for k, v in rec_rows:
        rec = dict(side.get(k) or bank[k])
        rec["paper_key"] = k
        prior = str(bank[k].get("review_summary") or "")[:600]
        # Clear the verdict so _curation_pending picks it up again. The pack
        # state goes with it: a denied paper carries none, and a re-review
        # must not inherit one.
        rec["review_status"] = ""
        rec["deny_category"] = ""
        rec["pack_status"] = ""
        rec["review_issues"] = []
        rec["review_summary"] = (
            f"RE-ARMED {stamp[:10]}: denied under the pre-2026-09-05 review rule, "
            f"which refused papers whose values are peak tables or compositions "
            f"rather than raw traces. Re-judge: {str(v.get('why') or '')[:200]} "
            f"| prior denial: {prior}"
        )[:1500]
        out.append(rec)
    await append_records(fx, out)
    print(f"\nre-armed {len(out)} papers for a full review under the loosened prompt")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=ROOT)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("prepare")
    p.add_argument("--out", required=True)
    p.add_argument("--limit", type=int, default=0)
    b = sub.add_parser("book")
    b.add_argument("--in", dest="inp", required=True)
    b.add_argument("--apply", action="store_true")
    v = sub.add_parser("verify")
    v.add_argument("--in", dest="inp", required=True)
    a = ap.parse_args()
    return asyncio.run({"prepare": prepare, "book": book, "verify": verify}[a.cmd](a))


if __name__ == "__main__":
    raise SystemExit(main())
