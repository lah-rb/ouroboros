#!/usr/bin/env python3
"""Repack pack_failed papers with an external model driven by sub-agents.

WHY (operator, 2026-09-04): 95 accepted papers sit pack_failed -- 91 from
the whole-document era (26 of them refused by the old doi|arxiv envelope
rule that identity tiers have since replaced) -- and the local seats are
saturated by the curation backlog. An API model (Claude Haiku, driven as
sub-agents) is a boost that costs no local decode. The MODEL is the only
thing that changes: windows, prompts, gates, merge, envelope and registry
fold are production's own functions, so a boosted pack is indistinguishable
from a local one except for its provenance.

THREE PHASES, files between them, because the agent runs outside Python:

  prepare  -> DIR/<key>/window_NN.json + window_NN.prompt.txt, DIR/jobs.json
              (production windows and the production pack prompt, rendered;
              prior_keys is not carried between windows -- each window is
              prompted as a first window)
  gate     <- the agent writes window_NN.answer.json and runs this: parse,
              canonicalise, repair_shapes, _run_pack_gates against THE WINDOW,
              verdict + feedback printed for the agent's one retry
  book     -> passed windows merge (merge_packs), face the whole-document
              gates, and are booked exactly as action_curate_book_result
              books a pack: envelope, required_fields_check, dataset file,
              fold_pack_into_registry (coinage guard included), papers-side
              record. Dry run by default.

    python dev/repack_via_agents.py prepare --out ~/tmp/repack_boost
    python dev/repack_via_agents.py gate --window W.json --answer A.json --attempt 1
    python dev/repack_via_agents.py book --in ~/tmp/repack_boost [--apply]
"""

from __future__ import annotations

import argparse
import asyncio
import glob
import hashlib
import json
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from agent.actions import curation_actions as ca  # noqa: E402
from agent.actions.identifiers import envelope_identity  # noqa: E402
from agent.actions.pack_windows import (  # noqa: E402
    MergeReport,
    merge_packs,
    repair_shapes,
    window_sections,
)
from agent.actions.scholarly_actions import (  # noqa: E402
    DATABANK_PATH,
    _read_jsonl_records,
    append_records,
    read_databank,
)
from agent.effects.local import LocalEffects  # noqa: E402
from agent.llm_json import parse_llm_json  # noqa: E402
from bench_staged_pack import _doc_for  # noqa: E402

ROOT = os.path.expanduser("~/corpora/ouroboros-spectra")
BOOST_MODEL = "claude-haiku-4-5 (api boost via sub-agent)"
WINDOWS_PER_JOB = 3


def _sha(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8", "ignore")).hexdigest()[:12]


async def prepare(a) -> int:
    fx = LocalEffects(a.root)
    bank = await read_databank(fx)
    keys = a.keys or sorted(
        k
        for k, r in bank.items()
        if r.get("review_status") == "accepted"
        and r.get("pack_status") == "pack_failed"
    )
    if a.limit:
        keys = keys[: a.limit]
    registry = await ca._load_registry(fx)
    target, cap = ca._pack_window_sizes()
    block = ca.format_key_registry(registry)
    os.makedirs(a.out, exist_ok=True)
    jobs = []
    manifest = {}
    for key in keys:
        doc = _doc_for(a.root, key)
        if not doc:
            print(f"  skip {key}: no markdown")
            continue
        windows = window_sections(doc, target, cap)
        multi = len(windows) > 1
        d = os.path.join(a.out, key)
        os.makedirs(d, exist_ok=True)
        wpaths = []
        for w in windows:
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
            pack_prompt = await ca._render_prompt(
                "curator/pack_data",
                {"key_registry_block": block, "gate_feedback": ""},
            )
            prompt = preface + w.text + "\n\n---\n\n" + pack_prompt
            wp = os.path.join(d, f"window_{w.index:02d}.json")
            json.dump(
                {
                    "key": key,
                    "index": w.index,
                    "tokens": w.tokens,
                    "sections": w.section_count,
                    "oversize": w.oversize,
                    "first_heading": w.first_heading,
                    "text": w.text,
                },
                open(wp, "w", encoding="utf-8"),
                ensure_ascii=False,
            )
            open(
                os.path.join(d, f"window_{w.index:02d}.prompt.txt"),
                "w",
                encoding="utf-8",
            ).write(prompt)
            wpaths.append(wp)
        manifest[key] = {
            "windows": len(windows),
            "doc_sha": _sha(doc),
            "tokens": [w.tokens for w in windows],
            "language": bank[key].get("language"),
        }
        for i in range(0, len(wpaths), WINDOWS_PER_JOB):
            chunk = wpaths[i : i + WINDOWS_PER_JOB]
            jobs.append(
                {
                    "job": f"{key}#{i // WINDOWS_PER_JOB}",
                    "key": key,
                    "windows": [
                        {
                            "window": p,
                            "prompt": p.replace(".json", ".prompt.txt"),
                            "answer": p.replace(".json", ".answer.json"),
                        }
                        for p in chunk
                    ],
                }
            )
        print(
            f"  {key[:56]:<58} {len(windows):>2} windows  {sum(w.tokens for w in windows):>7,} tok"
        )
    json.dump(manifest, open(os.path.join(a.out, "manifest.json"), "w"), indent=1)
    json.dump(jobs, open(os.path.join(a.out, "jobs.json"), "w"), indent=1)
    print(
        f"\n{len(manifest)} papers, {sum(m['windows'] for m in manifest.values())} windows, {len(jobs)} jobs -> {a.out}"
    )
    return 0


async def gate(a) -> int:
    fx = LocalEffects(a.root)
    registry = await ca._load_registry(fx)
    aliases = await ca.load_key_aliases(fx)
    w = json.load(open(a.window, encoding="utf-8"))
    raw = open(a.answer, encoding="utf-8").read() if os.path.exists(a.answer) else ""
    parsed = parse_llm_json(raw)
    data = parsed if isinstance(parsed, dict) and parsed else None
    repairs: list = []
    if data is not None:
        data = ca.canonicalize_pack_keys(data, aliases)
        data, repairs = repair_shapes(data, registry)
        gates = ca._run_pack_gates(data, w["text"], registry)
    else:
        gates = {"passed": False, "feedback": "output was not a JSON object"}
    g = gates.get("grounding") or {}
    out = {
        "attempt": a.attempt,
        "passed": bool(gates["passed"]),
        "feedback": "" if gates["passed"] else str(gates.get("feedback") or ""),
        "grounding_rate": g.get("grounding_rate"),
        "numeric_leaves": g.get("numeric_leaves"),
        "data": data,
        "repairs": repairs[:25],
        "graded_at": datetime.now(timezone.utc).isoformat(),
    }
    json.dump(
        out,
        open(a.window.replace(".json", ".gate.json"), "w", encoding="utf-8"),
        ensure_ascii=False,
    )
    if gates["passed"]:
        print(
            f"PASSED window {w['index']} (grounding {g.get('grounding_rate')}, {g.get('numeric_leaves')} numeric values)"
        )
    else:
        print(
            f"FAILED window {w['index']} attempt {a.attempt}:\n{out['feedback'][:1500]}"
        )
    return 0


async def book(a) -> int:
    fx = LocalEffects(a.root)
    bank = await read_databank(fx)
    side = await _read_jsonl_records(fx, DATABANK_PATH)
    registry = await ca._load_registry(fx)
    manifest = json.load(open(os.path.join(a.inp, "manifest.json")))
    booked = failed = pending = 0
    rows = []
    for key, m in manifest.items():
        rec = bank.get(key) or {}
        if rec.get("review_status") != "accepted" or rec.get("pack_status") == "packed":
            print(
                f"  skip {key[:50]}: now {rec.get('review_status')}/{rec.get('pack_status')}"
            )
            continue
        d = os.path.join(a.inp, key)
        gfiles = sorted(glob.glob(os.path.join(d, "window_*.gate.json")))
        if len(gfiles) < m["windows"]:
            pending += 1
            print(f"  pending {key[:50]}: {len(gfiles)}/{m['windows']} windows graded")
            continue
        doc = _doc_for(a.root, key)
        if not doc or _sha(doc) != m["doc_sha"]:
            print(f"  skip {key[:50]}: document changed since prepare")
            continue
        outcomes, passed, attempts = [], [], 0
        for gf in gfiles:
            g = json.load(open(gf, encoding="utf-8"))
            wi = json.load(open(gf.replace(".gate.json", ".json"), encoding="utf-8"))
            attempts += int(g.get("attempt") or 1)
            outcomes.append(
                {
                    "window": wi["index"],
                    "tokens": wi["tokens"],
                    "sections": wi["sections"],
                    "oversize": wi["oversize"],
                    "passed": g["passed"],
                    "attempts": g.get("attempt"),
                    "grounding_rate": g.get("grounding_rate"),
                    "numeric_leaves": g.get("numeric_leaves"),
                    "feedback": (
                        "" if g["passed"] else str(g.get("feedback") or "")[:300]
                    ),
                }
            )
            if g["passed"] and g.get("data"):
                passed.append(g["data"])
        multi = m["windows"] > 1
        if not passed:
            failed += 1
            print(f"  FAILED {key[:50]}: no window passed ({m['windows']} windows)")
            continue
        report = MergeReport()
        if multi:
            merged = merge_packs(passed, report)
            final = ca._run_pack_gates(merged, doc, registry)
        else:
            merged = passed[0]
            final = ca._run_pack_gates(merged, doc, registry)
        if not final["passed"]:
            failed += 1
            print(
                f"  FAILED {key[:50]}: merged pack failed whole-document gates: {final['feedback'][:120]}"
            )
            continue
        quality = {
            "grounding_rate": final["grounding"]["grounding_rate"],
            "numeric_leaves": final["grounding"]["numeric_leaves"],
            "ungrounded": final["grounding"]["ungrounded"],
            "new_keys": len(final["registry"]["new_keys"]),
            "reused_keys": len(final["registry"]["reused_keys"]),
            "near_duplicate_flags": final["near_dups"],
            "parse_attempts": attempts,
            "windows": m["windows"],
            "windows_passed": len(passed),
            "window_conflicts": report.conflicts[:25],
            "shape_repairs": [],
            "window_outcomes": outcomes,
            "pack_boost": BOOST_MODEL,
        }
        figtext_model = await ca._figtext_model(fx, key)
        envelope = {
            "paper_key": key,
            "title": rec.get("title", ""),
            "doi": rec.get("doi", ""),
            "arxiv_id": rec.get("arxiv_id", ""),
            **envelope_identity(rec),
            "license": rec.get("license", "") or "unknown",
            "year": rec.get("year", 0),
            "review": {"status": "accepted", "summary": rec.get("review_summary", "")},
            "data": merged,
            "provenance": {
                "model": BOOST_MODEL,
                "figtext_model": figtext_model,
                "packed_at": datetime.now(timezone.utc).isoformat(),
                "md_path": rec.get("md_path", ""),
            },
        }
        problems = ca.required_fields_check(envelope)
        if problems:
            failed += 1
            print(f"  FAILED {key[:50]}: envelope: {'; '.join(problems)}")
            continue
        print(
            f"  PACKED {key[:50]}: {len(merged)} keys, {quality['numeric_leaves']} values, "
            f"gr {quality['grounding_rate']}, new {quality['new_keys']} reused {quality['reused_keys']}, "
            f"windows {len(passed)}/{m['windows']}"
        )
        booked += 1
        if a.apply:
            dataset_path = f"{ca.DATASET_DIR}/{key}.json"
            await fx.write_file(
                dataset_path, json.dumps(envelope, indent=1, ensure_ascii=False)
            )
            coinage = await ca.fold_pack_into_registry(fx, registry, merged, key)
            row = dict(side.get(key) or rec)
            row["paper_key"] = key
            row["pack_status"] = "packed"
            row["dataset_path"] = dataset_path
            row["pack_quality"] = {**quality, **coinage}
            row["failure_reason"] = ""
            row["curation_method"] = f"claude-haiku-4-5-api+{figtext_model}"
            rows.append(row)
    if a.apply and rows:
        await append_records(fx, rows)
    print(
        f"\npacked {booked}, failed {failed}, pending {pending}"
        + ("" if a.apply else "  (dry run)")
    )
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=ROOT)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("prepare")
    p.add_argument("--out", required=True)
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--keys", nargs="*")
    g = sub.add_parser("gate")
    g.add_argument("--window", required=True)
    g.add_argument("--answer", required=True)
    g.add_argument("--attempt", type=int, default=1)
    b = sub.add_parser("book")
    b.add_argument("--in", dest="inp", required=True)
    b.add_argument("--apply", action="store_true")
    a = ap.parse_args()
    return asyncio.run({"prepare": prepare, "gate": gate, "book": book}[a.cmd](a))


if __name__ == "__main__":
    raise SystemExit(main())
