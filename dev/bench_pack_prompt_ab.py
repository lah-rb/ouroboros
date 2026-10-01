"""Pack prompt A/B: does "a missing registry key is never a reason to omit" recover tables?

WHY (2026-10-01). Muse packed only methods metadata from window 0 of
doi_10.2109_jcersj.107.249 -- Tables 3-7, the paper's weight-fraction results,
omitted on 3/3 replays and in the stored September pack. Asked to justify
each table (dev/replay_pack_window.py --append), it answered "No registry key
for weight fraction ... Packing would require inventing keys" and dropped a
particle size in µm because the registry lists only nm keys. The prompt says
the opposite -- a NEW key for a quantity the registry genuinely lacks, the
unit carried in the key -- so arm B states it as a rule. Production window-0
density is 3.4 values/1k tokens against 5.7 for middle windows (449 packs).

    A  production curator/pack_data
    B  + RULE_B (below), inserted before "- Include the source conditions"

Both arms run PRODUCTION _pack_windowed on muse (the default domain), in
parallel, same documents, nothing booked. The patch is a wrapper around
_render_prompt in THIS process only: the live mission keeps reading the
unchanged prompts/curator/pack_data.yaml.

Papers: multi-window muse packs (raw form) -- `w0tables`: window 0 holds an
HTML table of >= 20 numeric cells (jcersj forced in); `control`: it does not.

    .venv/bin/python dev/bench_pack_prompt_ab.py select
    setsid nohup .venv/bin/python -u dev/bench_pack_prompt_ab.py run A > ~/tmp/pack_prompt_ab/A.log 2>&1 &
    setsid nohup .venv/bin/python -u dev/bench_pack_prompt_ab.py run B > ~/tmp/pack_prompt_ab/B.log 2>&1 &
    .venv/bin/python dev/bench_pack_prompt_ab.py report

Metrics per paper: windows passed, numeric leaves, grounding, new registry
keys (coinage), entity-named keys (a sample/compound id inside a key), and
TABLE COVERAGE -- the share of the doc's numeric table cells (multiset) that
appear in the pack.
"""

from __future__ import annotations

import asyncio
import collections
import hashlib
import json
import os
import random
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.actions import curation_actions as ca  # noqa: E402
from agent.actions.pack_windows import window_sections  # noqa: E402
from agent.effects.child import ChildEffects  # noqa: E402
from agent.effects.local import LocalEffects  # noqa: E402

ROOT = os.path.expanduser("~/corpora/ouroboros-spectra")
OUT = os.path.expanduser(os.environ.get("BENCH_OUT", "~/tmp/pack_prompt_ab"))
PAPERS = os.path.join(OUT, "papers.json")
SEED = 20261001
N_W0TABLES, N_CONTROL = 8, 4
FORCED = ["doi_10.2109_jcersj.107.249"]
ANCHOR = "- Include the source conditions a value is meaningless without"
RULE_B = """- A MISSING REGISTRY KEY IS NEVER A REASON TO OMIT A VALUE. The
  registry is vocabulary to reuse, not a list of what may be packed. A
  quantity it lacks -- weight or phase fractions, fit statistics such as
  R_wp, uncertainties, a size in µm when the registry lists only nm --
  gets a NEW reusable key, unit-suffixed, with the sample, phase and
  method carried INSIDE each object, never in the key:
      ✅ "weight_fraction_pct": [{"sample": "S8", "phase": "quartz",
           "method": "WPPD", "known": 5.0, "found": 5.65, "delta": 0.65}]
      ❌ "weight_fraction_s8": [...]
  A results table the paper prints is packed row by row, whole.
"""
_orig_render = ca._render_prompt


async def _render_b(name, ctx):
    text = await _orig_render(name, ctx)
    if name == "curator/pack_data":
        if ANCHOR not in text:
            raise RuntimeError("arm B anchor missing from the rendered pack prompt")
        text = text.replace(ANCHOR, RULE_B + ANCHOR, 1)
    return text


def _effects():
    cfg = json.load(open(f"{ROOT}/.agent/mission.json"))["config"]
    parent = LocalEffects(ROOT, llmvp_domains=cfg.get("llmvp_domains"))
    return ChildEffects(parent, branch="bench:pack_prompt_ab", inference_domain="")


def _table_numbers(text: str) -> collections.Counter:
    out: collections.Counter = collections.Counter()
    for cell in re.findall(r"<td[^>]*>(.*?)</td>", text, re.S):
        cell = re.sub(r"\$[^$]*\$|<[^>]+>", " ", cell)
        for m in re.finditer(r"(?<![\w.])-?\d+(?:\.\d+)?", cell):
            out[abs(round(float(m.group()), 4))] += 1
    return out


def _pack_numbers(data) -> collections.Counter:
    c: collections.Counter = collections.Counter()
    for _, t in ca._numeric_leaf_tokens(data or {}):
        try:
            c[abs(round(float(t), 4))] += 1
        except ValueError:
            pass
    return c


_ENTITY_TOKEN = re.compile(r"^[a-z]{1,3}\d{1,3}[a-z]?$")


def _entity_keys(data) -> list[str]:
    keys: list[str] = []

    def walk(x):
        if isinstance(x, dict):
            for k, v in x.items():
                if any(_ENTITY_TOKEN.match(t) for t in str(k).lower().split("_")):
                    keys.append(k)
                walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)

    walk(data)
    return sorted(set(keys))


def _latest_papers() -> dict:
    rows: dict = {}
    with open(f"{ROOT}/databank/papers.jsonl") as f:
        for line in f:
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if d.get("paper_key"):
                rows[d["paper_key"]] = d
    return rows


async def select() -> None:
    if os.path.exists(PAPERS):
        print(f"{PAPERS} exists — keeping it")
        return
    os.makedirs(OUT, exist_ok=True)
    fx = _effects()
    rows = _latest_papers()
    keys = sorted(
        k
        for k, r in rows.items()
        if r.get("pack_status") == "packed"
        and r.get("pack_doc_form") == "raw"
        and r.get("review_status") == "accepted"
    )
    random.Random(SEED).shuffle(keys)
    keys = FORCED + [k for k in keys if k not in FORCED]
    target, cap = ca._pack_window_sizes()
    picked = {"w0tables": [], "control": []}
    want = {"w0tables": N_W0TABLES, "control": N_CONTROL}
    for k in keys:
        if all(len(picked[s]) >= want[s] for s in want):
            break
        doc = await ca._raw_curator_doc(fx, k)
        wins = window_sections(doc, target, cap)
        if not 2 <= len(wins) <= 4 or max(w.tokens for w in wins) > 25_000:
            continue
        w0 = sum(_table_numbers(wins[0].text).values())
        s = "w0tables" if w0 >= 20 else "control"
        if len(picked[s]) < want[s]:
            picked[s].append(
                {
                    "key": k,
                    "stratum": s,
                    "windows": len(wins),
                    "w0_table_numbers": w0,
                    "doc_table_numbers": sum(_table_numbers(doc).values()),
                    "doc_sha": hashlib.sha256(doc.encode()).hexdigest()[:16],
                }
            )
    papers = picked["w0tables"] + picked["control"]
    json.dump(papers, open(PAPERS, "w"), indent=1)
    for p in papers:
        print(
            f"{p['stratum']:<9} {p['windows']}w  w0 table nums {p['w0_table_numbers']:>4}  {p['key']}"
        )
    print(f"wrote {PAPERS} ({len(papers)} papers)")


async def run(arm: str) -> None:
    if arm == "B":
        ca._render_prompt = _render_b
    papers = json.load(open(PAPERS))
    fx = _effects()
    registry = await ca._load_registry(fx)
    out_rows = os.path.join(OUT, f"{arm}.json")
    rows = json.load(open(out_rows)) if os.path.exists(out_rows) else []
    done = {r["key"] for r in rows}
    os.makedirs(os.path.join(OUT, arm), exist_ok=True)
    for p in papers:
        if p["key"] in done:
            continue
        doc = await ca._raw_curator_doc(fx, p["key"])
        t0 = time.time()
        pack, faults = None, []
        for _ in range(3):
            try:
                pack = await ca._pack_windowed(fx, doc, registry)
                break
            except ca._CurateTransportFault as e:
                faults.append(str(e)[:160])
                print(f"{arm} {p['key']} transport fault: {str(e)[:120]}", flush=True)
        if pack is None:
            pack = {"status": "transport_fault", "quality": {}, "data": {}}
        q = pack.get("quality") or {}
        data = pack.get("data") or {}
        tn = _table_numbers(doc)
        cov = sum((tn & _pack_numbers(data)).values()) / max(1, sum(tn.values()))
        row = dict(
            arm=arm,
            key=p["key"],
            stratum=p["stratum"],
            doc_sha_match=hashlib.sha256(doc.encode()).hexdigest()[:16] == p["doc_sha"],
            status=pack["status"],
            seconds=round(time.time() - t0),
            windows=q.get("windows"),
            windows_passed=q.get("windows_passed"),
            leaves=q.get("numeric_leaves"),
            grounding=q.get("grounding_rate"),
            new_keys=q.get("new_keys"),
            entity_keys=_entity_keys(data),
            table_coverage=round(cov, 3),
            transport_faults=faults,
            window_outcomes=q.get("window_outcomes"),
        )
        rows.append(row)
        json.dump(rows, open(out_rows, "w"), indent=1)
        json.dump(pack, open(os.path.join(OUT, arm, f"{p['key']}.json"), "w"), indent=1)
        print(
            f"{arm} {p['stratum']:<9} {p['key']:<42} {pack['status']:<12} "
            f"win {q.get('windows_passed')}/{q.get('windows')} leaves {q.get('numeric_leaves')} "
            f"tables {cov:.0%} new {q.get('new_keys')} entity-keys {len(row['entity_keys'])} "
            f"{row['seconds']}s",
            flush=True,
        )


def report() -> None:
    papers = json.load(open(PAPERS))
    arms = {}
    for arm in ("A", "B"):
        path = os.path.join(OUT, f"{arm}.json")
        arms[arm] = (
            {r["key"]: r for r in json.load(open(path))} if os.path.exists(path) else {}
        )
    print(f"{'paper':<44}{'stratum':<10}{'A':>40}{'B':>40}")
    for p in papers:
        cells = []
        for arm in ("A", "B"):
            r = arms[arm].get(p["key"])
            cells.append(
                "—"
                if r is None
                else f"{r['status'][:6]} {r['windows_passed']}/{r['windows']} lv {r['leaves']} "
                f"tab {r['table_coverage']:.0%} new {r['new_keys']} ent {len(r['entity_keys'])}"
            )
        print(
            f"{p['key'][:43]:<44}{p['stratum']:<10}"
            + "".join(f"{c:>40}" for c in cells)
        )
    print()
    for stratum in ("w0tables", "control", None):
        for arm in ("A", "B"):
            rs = [
                r
                for r in arms[arm].values()
                if stratum is None or r["stratum"] == stratum
            ]
            if not rs:
                continue
            both = [r for r in rs if r["key"] in arms["A"] and r["key"] in arms["B"]]
            packed = [r for r in both if r["status"] == "packed"]
            lv = sum(r["leaves"] or 0 for r in packed)
            cov = sorted(r["table_coverage"] for r in both)
            print(
                f"{stratum or 'all':<9} {arm}: papers {len(both)} packed {len(packed)}  "
                f"windows {sum(r['windows_passed'] or 0 for r in both)}/{sum(r['windows'] or 0 for r in both)}  "
                f"leaves {lv}  median table coverage {cov[len(cov)//2] if cov else 0:.0%}  "
                f"new keys {sum(r['new_keys'] or 0 for r in packed)}  "
                f"entity keys {sum(len(r['entity_keys']) for r in both)}  "
                f"{sum(r['seconds'] for r in both)/max(1,len(both))/60:.1f} min/paper"
            )


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "report"
    if cmd == "select":
        asyncio.run(select())
    elif cmd == "run":
        asyncio.run(run(sys.argv[2]))
    else:
        report()
