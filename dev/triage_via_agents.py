#!/usr/bin/env python3
"""Front-matter triage of the curate queue, driven by sub-agents.

WHY (operator, 2026-09-05). 279 papers wait for curation: median 178k
tokens, 92% too large for the local seat, so all of them queue behind ONE
remote lane clearing ~2.9 reviews an hour -- about a week. Measured over
that same run, 25 of 27 reviews came back DENIED. Nearly all of the week
is spent proving documents are off-corpus at full length. Reading the front
matter first and denying only the unmistakable cases is the biggest lever
available, and it costs no local decode: the agents are an API model.

The prompt, the deny categories and the discipline are
`dev/triage_parked_frontmatter.py` verbatim -- DOUBT MEANS PROCEED, the
triage never accepts anything, it only removes what cannot serve the
corpus. Only the transport changes: briefs to files, agents write verdicts
back, and a validated booking pass applies them.

CALIBRATION IS NOT OPTIONAL. Each batch is seeded with already-reviewed
papers whose verdicts are hidden from the agent. A triage that denies a
paper the full curator ACCEPTED is not saving time, it is losing papers, so
`book` refuses to apply anything until the measured false-deny rate on that
control set is reported.

    python dev/triage_via_agents.py prepare --out ~/tmp/triage_boost
    python dev/triage_via_agents.py book --in ~/tmp/triage_boost [--apply]
"""

from __future__ import annotations

import argparse
import asyncio
import collections
import glob
import json
import os
import random
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from agent.actions import curation_actions as ca  # noqa: E402
from agent.actions.scholarly_actions import (  # noqa: E402
    DATABANK_PATH,
    _read_jsonl_records,
    append_records,
    read_databank,
)
from agent.effects.local import LocalEffects  # noqa: E402
from triage_parked_frontmatter import (  # noqa: E402
    catalogue_header,
    corpus_subject,
    front_matter,
    triage_prompt,
)

ROOT = os.path.expanduser("~/corpora/ouroboros-spectra")
PAPERS_PER_JOB = 8
CONTROLS = 30  # already-reviewed papers seeded blind across the batches


def _md(root: str, key: str) -> str | None:
    for p in (
        f"{root}/databank/markdown/{key}.en.md",
        f"{root}/databank/markdown/{key}.md",
    ):
        if os.path.exists(p):
            return open(p, encoding="utf-8", errors="ignore").read()
    return None


async def prepare(a) -> int:
    fx = LocalEffects(a.root)
    bank = await read_databank(fx)
    subject = corpus_subject()
    queue = sorted(
        k
        for k, r in bank.items()
        if ca._curation_pending(r) and not r.get("review_status")
    )
    reviewed = [
        k
        for k, r in bank.items()
        if r.get("review_status") in ("accepted", "denied")
        and str(r.get("review_doc_form") or "") != ""
    ] or [
        k for k, r in bank.items() if r.get("review_status") in ("accepted", "denied")
    ]
    rnd = random.Random(20260905)
    acc = [k for k in reviewed if bank[k]["review_status"] == "accepted"]
    den = [k for k in reviewed if bank[k]["review_status"] == "denied"]
    rnd.shuffle(acc)
    rnd.shuffle(den)
    controls = acc[: CONTROLS // 2] + den[: CONTROLS // 2]
    if a.limit:
        queue = queue[: a.limit]
    items = [(k, "queue") for k in queue] + [(k, "control") for k in controls]
    rnd.shuffle(items)

    os.makedirs(a.out, exist_ok=True)
    manifest, briefs = {}, []
    for key, kind in items:
        md = _md(a.root, key)
        if not md:
            print(f"  skip {key[:50]}: no markdown")
            continue
        excerpt, pages = front_matter(md)
        if not excerpt.strip():
            print(f"  skip {key[:50]}: no front matter")
            continue
        prompt = triage_prompt(subject, catalogue_header(bank[key]), excerpt)
        briefs.append((key, prompt))
        manifest[key] = {
            "kind": kind,
            "pages": pages,
            "truth": bank[key].get("review_status") if kind == "control" else None,
            "chars": len(excerpt),
        }
    jobs = [
        briefs[i : i + PAPERS_PER_JOB] for i in range(0, len(briefs), PAPERS_PER_JOB)
    ]
    for i, job in enumerate(jobs):
        d = os.path.join(a.out, f"job_{i:03d}")
        os.makedirs(d, exist_ok=True)
        for key, prompt in job:
            open(
                os.path.join(d, f"{key[:80]}.prompt.txt"), "w", encoding="utf-8"
            ).write(prompt)
        json.dump(
            [
                {
                    "key": k,
                    "prompt": f"{d}/{k[:80]}.prompt.txt",
                    "answer": f"{d}/{k[:80]}.answer.json",
                }
                for k, _ in job
            ],
            open(f"{d}/items.json", "w"),
            indent=1,
            ensure_ascii=False,
        )
    json.dump(
        manifest, open(f"{a.out}/manifest.json", "w"), indent=1, ensure_ascii=False
    )
    n_ctl = sum(1 for v in manifest.values() if v["kind"] == "control")
    print(
        f"\n{len(manifest)} briefs ({len(manifest)-n_ctl} queue, {n_ctl} blind controls) "
        f"in {len(jobs)} jobs of <={PAPERS_PER_JOB} -> {a.out}"
    )
    return 0


def _verdicts(inp: str) -> dict:
    out = {}
    for f in glob.glob(f"{inp}/job_*/*.answer.json"):
        try:
            d = json.load(open(f))
        except Exception:
            try:
                from agent.llm_json import parse_llm_json

                d = parse_llm_json(open(f, encoding="utf-8").read())
            except Exception:
                d = None
        if not isinstance(d, dict):
            print(f"  UNREADABLE {os.path.basename(f)}")
            continue
        key = os.path.basename(f)[: -len(".answer.json")]
        out[key] = d
    return out


async def book(a) -> int:
    fx = LocalEffects(a.root)
    bank = await read_databank(fx)
    side = await _read_jsonl_records(fx, DATABANK_PATH)
    manifest = json.load(open(f"{a.inp}/manifest.json"))
    got = _verdicts(a.inp)
    # answers are keyed by the truncated filename; map back to full keys
    fixed = {}
    for k in manifest:
        fixed[k] = got.get(k[:80]) or got.get(k)
    got = {k: v for k, v in fixed.items() if v}
    print(f"{len(got)} verdicts of {len(manifest)} briefs")

    ctl = [(k, v) for k, v in got.items() if manifest[k]["kind"] == "control"]
    conf = collections.Counter(
        (manifest[k]["truth"], str(v.get("verdict"))) for k, v in ctl
    )
    n_acc = sum(n for (t, _), n in conf.items() if t == "accepted")
    false_deny = sum(
        n for (t, verdict), n in conf.items() if t == "accepted" and verdict == "deny"
    )
    caught = sum(
        n for (t, verdict), n in conf.items() if t == "denied" and verdict == "deny"
    )
    n_den = sum(n for (t, _), n in conf.items() if t == "denied")
    print("\nCALIBRATION on blind controls")
    print(
        f"  known ACCEPTED: {n_acc} | triage denied {false_deny}  <- false denials, must be 0"
    )
    print(
        f"  known DENIED:   {n_den} | triage denied {caught} ({100*caught/max(n_den,1):.0f}% caught early)"
    )
    for k, v in ctl:
        if manifest[k]["truth"] == "accepted" and str(v.get("verdict")) == "deny":
            print(f"     FALSE DENY {k[:56]}: {str(v.get('summary'))[:90]}")

    q = [(k, v) for k, v in got.items() if manifest[k]["kind"] == "queue"]
    dq = [(k, v) for k, v in q if str(v.get("verdict")) == "deny"]
    print(
        f"\nQUEUE: {len(q)} triaged | deny {len(dq)} ({100*len(dq)/max(len(q),1):.0f}%) | proceed {len(q)-len(dq)}"
    )
    print(
        "  deny categories:",
        dict(collections.Counter(str(v.get("deny_category") or "-") for _, v in dq)),
    )
    print(
        "  document types:",
        dict(collections.Counter(str(v.get("document_type") or "-") for _, v in q)),
    )
    if not a.apply:
        print("\n(dry run: nothing written)")
        return 0
    if false_deny > a.max_false_deny:
        print(
            f"\nREFUSING TO APPLY: {false_deny} false denials exceeds --max-false-deny {a.max_false_deny}"
        )
        return 2
    rows = []
    stamp = datetime.now(timezone.utc).isoformat()
    for k, v in dq:
        rec = dict(side.get(k) or bank[k])
        rec["paper_key"] = k
        rec["review_status"] = "denied"
        rec["deny_category"] = str(v.get("deny_category") or "corpus_fit")
        rec["review_summary"] = (
            f"FRONT-MATTER TRIAGE (sonnet sub-agent, {stamp[:10]}; operator "
            "ruling: a borderline document this large is cleaner denied whole, "
            "its figures accepted as the loss): "
            f"{str(v.get('summary') or '')[:400]}"
        )
        rec["review_issues"] = [
            f"triage evidence: {str(v.get('evidence') or '')[:300]}",
            f"document_type: {v.get('document_type')}",
            "front matter only; the full text was never read",
        ]
        rec["review_document_form"] = str(v.get("document_type") or "")
        rows.append(rec)
    await append_records(fx, rows)
    print(f"\nbooked {len(rows)} denials")
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
    # The gate is 0 by default: a triage that denies a paper the full curator
    # accepted is losing papers, not saving time. Raising it is an OPERATOR
    # decision about a measured risk, never a default -- 2026-09-05 the
    # measured rate was 1 in 15 controls and the operator accepted it.
    b.add_argument("--max-false-deny", type=int, default=0)
    a = ap.parse_args()
    return asyncio.run({"prepare": prepare, "book": book}[a.cmd](a))


if __name__ == "__main__":
    raise SystemExit(main())
