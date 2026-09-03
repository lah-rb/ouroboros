#!/usr/bin/env python3
"""Are the pack prompt's registry EXEMPLARS leaking into packs as fabricated values?

THE OBSERVATION (2026-09-03 staged-pack run): 15 of 42 ungrounded numbers the
gates reported were EXACTLY the registry exemplar shown for that key in the
pack prompt -- `emission_line_nm ... e.g. [{"wavelength_nm": 311, ...}]` and a
window packed 311 nm for a paper that never says it. Some coincidences are
world knowledge (Cu K-alpha 1.54 A, 128 scans); 311 nm is not.

THE TEST. Take every window that failed on UNGROUNDED values in the staged run
(same doc, same deterministic cut), and pack each TWICE: control = the
production registry block; treatment = the same block with exemplar VALUES
replaced by type placeholders (`{"wavelength_nm": <number>, "sample":
<string>}`). Same window, same model, same temperature, one variable.
Alternating order. Scored on grounding rate, pass, and how many ungrounded
values match an exemplar.

    .venv/bin/python dev/bench_exemplar_leak.py --from ~/tmp/bench_staged_pack.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import agent.actions.curation_actions as CA  # noqa: E402
from agent.actions.curation_actions import (  # noqa: E402
    REGISTRY_PROMPT_TOP_N,
    _load_registry,
    load_key_aliases,
)
from agent.actions.pack_windows import window_sections  # noqa: E402
from agent.actions.scholarly_actions import read_databank  # noqa: E402
from agent.effects.child import ChildEffects  # noqa: E402
from agent.effects.local import LocalEffects  # noqa: E402
from dev.bench_staged_pack import _doc_for, pack_window  # noqa: E402

ROOT = os.path.expanduser("~/corpora/ouroboros-spectra")

_NUM = re.compile(r"(?<![\w\"])-?\d+(?:\.\d+)?(?:e-?\d+)?(?![\w\"])")
_STRVAL = re.compile(r'(:\s*)"(?:[^"\\]|\\.)*"')
_STRITEM = re.compile(r'([\[,]\s*)"(?:[^"\\]|\\.)*"(?=\s*[,\]])')


def shape_only(exemplar) -> str:
    """The exemplar with its VALUES replaced by type placeholders, keys kept."""
    s = (
        exemplar
        if isinstance(exemplar, str)
        else json.dumps(exemplar, ensure_ascii=False)
    )
    s = _STRVAL.sub(r"\1<string>", s)
    s = _STRITEM.sub(r"\1<string>", s)
    s = _NUM.sub("<number>", s)
    s = re.sub(r"\b(true|false)\b", "<bool>", s)
    return s


def format_key_registry_shape_only(
    registry: dict, top_n: int = REGISTRY_PROMPT_TOP_N
) -> str:
    if not registry:
        return "(registry is empty — this is the first paper; coin clear, unit-suffixed keys)"
    ranked = sorted(registry.items(), key=lambda kv: -int(kv[1].get("count") or 0))
    lines = []
    for key, entry in ranked[:top_n]:
        desc = str(entry.get("description") or "").strip()
        line = f"- {key} ({entry.get('type')}, {entry.get('count')} paper(s))"
        if desc:
            line += f": {desc}"
        line += f" — shape {shape_only(entry.get('exemplar'))}"
        lines.append(line)
    if len(ranked) > top_n:
        lines.append(f"...and {len(ranked) - top_n} more keys")
    return "\n".join(lines)


def exemplar_tokens(registry: dict) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for k, e in registry.items():
        out[k] = set(re.findall(r"-?\d+(?:\.\d+)?", str(e.get("exemplar") or "")))
    return out


def fabricating_windows(rows: list[dict]) -> list[tuple[str, int]]:
    out = []
    for r in rows:
        for w in r.get("per_window", []):
            if "error" in w or w.get("passed"):
                continue
            if "UNGROUNDED" in (w.get("feedback") or ""):
                out.append((r["key"], int(w["window"])))
    return out


async def main_async(a) -> int:
    rows = json.load(open(a.from_results))
    targets = fabricating_windows(rows)
    if a.limit:
        targets = targets[: a.limit]
    print(
        f"fabricating windows to re-pack: {len(targets)} (control + treatment each)\n"
    )
    cfg = json.load(open(f"{a.root}/.agent/mission.json"))["config"]
    parent = LocalEffects(a.root, llmvp_domains=cfg.get("llmvp_domains"))
    fx = ChildEffects(parent, branch="bench:exemplar_leak", inference_domain=a.domain)
    await read_databank(fx)
    registry = await _load_registry(fx)
    aliases = await load_key_aliases(fx)
    ex_tok = exemplar_tokens(registry)
    real_fmt = CA.format_key_registry

    out_rows = []
    docs: dict[str, list] = {}
    for n, (key, widx) in enumerate(targets, 1):
        if key not in docs:
            doc = _doc_for(a.root, key)
            docs[key] = window_sections(doc) if doc else []
        wins = docs[key]
        if widx >= len(wins):
            print(
                f"[{n:>2}] {key[:40]} w{widx}: window index out of range (doc changed?)"
            )
            continue
        w = wins[widx]
        preface = (
            f"[This is part {w.index + 1} of {len(wins)} of one paper — {w.section_count} "
            f'consecutive section(s) starting at "{w.first_heading}". Pack ONLY values '
            "stated in THIS part; other parts are packed separately and merged.]\n\n"
        )
        arms = ["control", "treatment"] if n % 2 else ["treatment", "control"]
        res = {}
        for arm in arms:
            CA.format_key_registry = (
                real_fmt if arm == "control" else format_key_registry_shape_only
            )
            t0 = time.time()
            try:
                data, gates, attempts = await pack_window(
                    fx, w.text, registry, aliases, preface
                )
            except Exception as e:  # noqa: BLE001
                res[arm] = dict(error=str(e)[:160])
                print(
                    f"[{n:>2}] {key[:36]:<38} w{widx} {arm:<9} TRANSPORT {str(e)[:60]}",
                    flush=True,
                )
                continue
            finally:
                CA.format_key_registry = real_fmt
            g = gates.get("grounding") or {}
            ung = g.get("ungrounded") or []
            leak = 0
            for u in ung:
                k0 = str(u.get("path", "")).split("[")[0].split(".")[0]
                if str(u.get("token")) in ex_tok.get(k0, set()):
                    leak += 1
            res[arm] = dict(
                passed=bool(gates["passed"]),
                grounding_rate=g.get("grounding_rate"),
                numeric_leaves=g.get("numeric_leaves"),
                ungrounded=len(ung),
                exemplar_matches=leak,
                keys=len(data or {}),
                attempts=attempts,
                seconds=round(time.time() - t0),
            )
            print(
                f"[{n:>2}] {key[:36]:<38} w{widx} {arm:<9} {'PASS' if gates['passed'] else 'fail'} "
                f"gr={g.get('grounding_rate')} ungrounded={len(ung)} exemplar-matches={leak} "
                f"leaves={g.get('numeric_leaves')} {res[arm]['seconds']}s",
                flush=True,
            )
        out_rows.append(
            dict(
                key=key,
                window=widx,
                tokens=w.tokens,
                **{f"{k}_{m}": v for k, d in res.items() for m, v in d.items()},
            )
        )
        json.dump(out_rows, open(a.out, "w"), indent=1, ensure_ascii=False)

    json.dump(out_rows, open(a.out, "w"), indent=1, ensure_ascii=False)
    ok = [
        r
        for r in out_rows
        if "control_grounding_rate" in r and "treatment_grounding_rate" in r
    ]
    if ok:
        import statistics

        cg = [r["control_grounding_rate"] for r in ok]
        tg = [r["treatment_grounding_rate"] for r in ok]
        print(f"\nEXEMPLAR LEAK A/B over {len(ok)} windows")
        print(
            f"  grounding  control median {statistics.median(cg):.3f}  treatment median {statistics.median(tg):.3f}"
        )
        print(
            f"  passed     control {sum(1 for r in ok if r['control_passed'])}  treatment {sum(1 for r in ok if r['treatment_passed'])}"
        )
        print(
            f"  ungrounded values matching an exemplar  control {sum(r['control_exemplar_matches'] for r in ok)}  treatment {sum(r['treatment_exemplar_matches'] for r in ok)}"
        )
        print(
            f"  ungrounded total  control {sum(r['control_ungrounded'] for r in ok)}  treatment {sum(r['treatment_ungrounded'] for r in ok)}"
        )
        better = sum(
            1 for r in ok if r["treatment_grounding_rate"] > r["control_grounding_rate"]
        )
        worse = sum(
            1 for r in ok if r["treatment_grounding_rate"] < r["control_grounding_rate"]
        )
        print(
            f"  per-window: treatment better {better}, worse {worse}, tied {len(ok)-better-worse}"
        )
    print(f"wrote {a.out}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=ROOT)
    ap.add_argument("--domain", default="curate_remote")
    ap.add_argument(
        "--from-results", default=os.path.expanduser("~/tmp/bench_staged_pack.json")
    )
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument(
        "--out", default=os.path.expanduser("~/tmp/bench_exemplar_leak.json")
    )
    return asyncio.run(main_async(ap.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
