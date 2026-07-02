#!/usr/bin/env python3
"""Curator text-model bake-off: review + pack over sample papers.

Runs the REAL curator prompts (prompts/curator/*.yaml via the real
PromptRenderer) and the REAL deterministic gates (curation_actions) as
stateless full-prompt completions against a running LLMVP server — no
snapshot tier required, no prompt/gate drift possible.

Scores per model:
  - parse_rate           fenced-JSON extraction success (review + pack)
  - verdict_validity     review verdict in {accept, deny}
  - gold_agreement       accept/deny vs dev/bakeoff_gold.json (where labeled)
  - grounding_rate       corpus-weighted numeric grounding of packs
  - envelope_pass_rate   required-fields gate on assembled envelopes
  - new_key_rate         registry discipline: new keys per pack as the
                         registry grows ACROSS papers (order fixed)
  - near_dup_flags       synonym-coining count
  - needle_accuracy      long-context check on the largest papers (a
                         canary sentence at ~80% depth, targeted question)

Writes dev/bakeoff_results/<model>.json and appends to summary.md.

Usage (server already running the model under test):
    uv run python dev/bakeoff_text.py --databank ~/corpora/ouroboros-hea/databank \
        [--papers 10] [--endpoint http://localhost:8008/graphql] [--label NAME]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from agent.actions.curation_actions import (  # noqa: E402
    build_curator_doc,
    format_key_registry,
    grounding_check,
    near_duplicate_keys,
    registry_check,
    required_fields_check,
    update_key_registry,
)
from agent.llm_json import parse_llm_json  # noqa: E402
from agent.loader import PromptRenderer  # noqa: E402

RESULTS_DIR = Path(__file__).parent / "bakeoff_results"
GOLD_PATH = Path(__file__).parent / "bakeoff_gold.json"
NEEDLE = (
    "NOTE-FOR-INDEX: the archival accession code for this manuscript is ZX-7741-OMEGA."
)
NEEDLE_QUESTION = (
    "\n\nBefore anything else: this document contains an archival accession "
    "code (format ZX-NNNN-WORD). Include it in your JSON as the additional "
    'top-level field "accession_code".'
)


async def _completion(endpoint: str, prompt: str, max_tokens: int = 4096) -> str:
    from agent.effects.inference import COMPLETION_QUERY

    import httpx

    async with httpx.AsyncClient(timeout=1800.0) as client:
        resp = await client.post(
            endpoint,
            json={
                "query": COMPLETION_QUERY,
                "variables": {
                    "request": {
                        "prompt": prompt,
                        "temperature": 0.3,
                        "maxTokens": max_tokens,
                    }
                },
            },
        )
        resp.raise_for_status()
        data = resp.json()
        if "errors" in data:
            raise RuntimeError(data["errors"])
        return str(data["data"]["completion"]["text"] or "")


def _server_model(endpoint: str) -> str:
    import urllib.request

    req = urllib.request.Request(
        endpoint,
        data=json.dumps({"query": "query { health { status } }"}).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        urllib.request.urlopen(req, timeout=5)
    except Exception as e:
        raise SystemExit(f"LLMVP not reachable at {endpoint}: {e}") from e
    # Model name isn't in health; the run label comes from active_config.
    root = Path(__file__).parent.parent / "llmvp" / "active_config.txt"
    return root.read_text().strip() if root.is_file() else "unknown-model"


def _pick_papers(databank: Path, n: int) -> list[dict]:
    """Extracted records, mixed sizes, largest-first tail for needles."""
    records: dict[str, dict] = {}
    bank = databank / "papers.jsonl"
    for line in bank.read_text().splitlines():
        if line.strip():
            rec = json.loads(line)
            records[rec.get("paper_key", "")] = rec
    done = [
        r
        for r in records.values()
        if r.get("extraction_status") == "extracted" and r.get("md_path")
    ]
    if not done:
        raise SystemExit(f"no extracted papers in {bank}")

    def _md_size(rec):
        p = databank / "markdown" / f"{rec['paper_key']}.md"
        return p.stat().st_size if p.is_file() else 0

    done.sort(key=_md_size)
    # Deterministic mix: mostly median-sized + the two largest (needle hosts).
    mid = done[: max(0, len(done) - 2)]
    step = max(1, len(mid) // max(1, n - 2))
    picked = mid[::step][: n - 2] + done[-2:]
    return picked[:n]


def _load_doc(databank: Path, rec: dict) -> str:
    md = (databank / "markdown" / f"{rec['paper_key']}.md").read_text()
    figtext_path = databank / "figtext" / f"{rec['paper_key']}.json"
    figtext = json.loads(figtext_path.read_text()) if figtext_path.is_file() else None
    return build_curator_doc(md, figtext)


def _insert_needle(doc: str) -> str:
    paragraphs = doc.split("\n\n")
    at = int(len(paragraphs) * 0.8)
    return "\n\n".join(paragraphs[:at] + [NEEDLE] + paragraphs[at:])


async def run(args) -> None:
    databank = Path(os.path.expanduser(args.databank))
    renderer = PromptRenderer(Path(__file__).parent.parent / "prompts")
    label = args.label or _server_model(args.endpoint)
    papers = _pick_papers(databank, args.papers)
    gold = json.loads(GOLD_PATH.read_text()) if GOLD_PATH.is_file() else {}
    gold_verdicts = {k: v.get("verdict") for k, v in gold.items() if v.get("verdict")}

    registry: dict = {}
    rows = []
    needle_hosts = {p["paper_key"] for p in papers[-2:]}
    t_start = time.time()

    for rec in papers:
        key = rec["paper_key"]
        doc = _load_doc(databank, rec)
        is_needle = key in needle_hosts
        if is_needle:
            doc = _insert_needle(doc)
        row = {
            "paper_key": key,
            "doc_chars": len(doc),
            "review_parsed": False,
            "verdict": "",
            "pack_parsed": False,
            "needle": None,
        }

        review_prompt = (
            doc
            + "\n\n---\n\n"
            + renderer.render(
                "curator/review_paper", {"input": {}, "context": {}, "meta": {}}
            )
        )
        raw = await _completion(args.endpoint, review_prompt)
        review = parse_llm_json(raw)
        if isinstance(review, dict) and review.get("verdict") in ("accept", "deny"):
            row["review_parsed"] = True
            row["verdict"] = review["verdict"]
            row["issues"] = len(review.get("issues") or [])

        if row["verdict"] == "accept":
            pack_prompt = (
                doc
                + "\n\n---\n\n"
                + renderer.render(
                    "curator/pack_data",
                    {
                        "input": {},
                        "context": {
                            "key_registry_block": format_key_registry(registry),
                            "gate_feedback": "",
                        },
                        "meta": {},
                    },
                )
                + (NEEDLE_QUESTION if is_needle else "")
            )
            raw = await _completion(args.endpoint, pack_prompt, max_tokens=8192)
            data = parse_llm_json(raw)
            if isinstance(data, dict) and data:
                row["pack_parsed"] = True
                if is_needle:
                    row["needle"] = "ZX-7741-OMEGA" in json.dumps(data)
                    data.pop("accession_code", None)  # not real paper data
                g = grounding_check(data, doc)
                reg = registry_check(data, registry)
                dups = near_duplicate_keys(reg["new_keys"], registry, data)
                envelope = {
                    "paper_key": key,
                    "title": rec.get("title", ""),
                    "doi": rec.get("doi", ""),
                    "arxiv_id": rec.get("arxiv_id", ""),
                    "license": rec.get("license", "") or "unknown",
                    "review": {"status": "accepted", "summary": "bakeoff"},
                    "data": data,
                }
                row.update(
                    grounding=g,
                    new_keys=len(reg["new_keys"]),
                    reused_keys=len(reg["reused_keys"]),
                    type_mismatches=len(reg["type_mismatches"]),
                    near_dup_flags=len(dups),
                    envelope_problems=required_fields_check(envelope),
                    data_keys=len(data),
                )
                update_key_registry(registry, data, key)
        rows.append(row)
        print(
            f"  {key[:44]:44s} verdict={row['verdict'] or '?':6s} "
            f"pack={'y' if row['pack_parsed'] else '-'} "
            f"ground={row.get('grounding', {}).get('grounding_rate', '')} "
            f"needle={row['needle']}"
        )

    packs = [r for r in rows if r["pack_parsed"]]
    n_reviewed = sum(1 for r in rows if r["review_parsed"])
    agree = [
        r["verdict"] == gold_verdicts[r["paper_key"]]
        for r in rows
        if r["paper_key"] in gold_verdicts and r["verdict"]
    ]
    g_tot = sum(p["grounding"]["numeric_leaves"] for p in packs)
    g_hit = sum(
        round(p["grounding"]["grounding_rate"] * p["grounding"]["numeric_leaves"])
        for p in packs
    )
    needles = [r["needle"] for r in rows if r["needle"] is not None]
    summary = {
        "model": label,
        "papers": len(rows),
        "parse_rate_review": round(n_reviewed / len(rows), 3),
        "parse_rate_pack": round(
            len(packs) / max(1, sum(1 for r in rows if r["verdict"] == "accept")), 3
        ),
        "accepts": sum(1 for r in rows if r["verdict"] == "accept"),
        "gold_agreement": round(sum(agree) / len(agree), 3) if agree else None,
        "grounding_rate": round(g_hit / g_tot, 4) if g_tot else None,
        "envelope_pass_rate": round(
            sum(1 for p in packs if not p["envelope_problems"]) / len(packs), 3
        )
        if packs
        else None,
        "new_key_rate": round(
            sum(p["new_keys"] for p in packs)
            / max(1, sum(p["data_keys"] for p in packs)),
            3,
        )
        if packs
        else None,
        "near_dup_flags": sum(p["near_dup_flags"] for p in packs),
        "needle_accuracy": round(sum(needles) / len(needles), 3) if needles else None,
        "minutes": round((time.time() - t_start) / 60, 1),
        "rows": rows,
        "final_registry_keys": len(registry),
    }

    RESULTS_DIR.mkdir(exist_ok=True)
    out = RESULTS_DIR / f"{label}.json"
    out.write_text(json.dumps(summary, indent=1, ensure_ascii=False))
    line = (
        f"| {label} | {summary['parse_rate_review']} | {summary['parse_rate_pack']} "
        f"| {summary['gold_agreement']} | {summary['grounding_rate']} "
        f"| {summary['new_key_rate']} | {summary['near_dup_flags']} "
        f"| {summary['needle_accuracy']} | {summary['minutes']}m |\n"
    )
    md = RESULTS_DIR / "summary.md"
    if not md.is_file():
        md.write_text(
            "# Curator text bake-off\n\n"
            "| model | parse(rev) | parse(pack) | gold | grounding "
            "| new-key rate | near-dups | needle | wall |\n"
            "|---|---|---|---|---|---|---|---|---|\n"
        )
    with open(md, "a") as f:
        f.write(line)
    print(f"\nwrote {out} and appended summary.md")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--databank", default="~/corpora/ouroboros-hea/databank")
    ap.add_argument("--papers", type=int, default=10)
    ap.add_argument("--endpoint", default="http://localhost:8008/graphql")
    ap.add_argument("--label", default="", help="result label (default: active_config)")
    args = ap.parse_args()
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
