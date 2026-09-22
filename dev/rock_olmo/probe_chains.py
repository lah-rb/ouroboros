#!/usr/bin/env python3
"""Controlled chained recall: exactly what the model sees, one field per turn (rock venv).

Operator request (2026-09-22): the §22 probes mix cues (sample IDs, laser, system, the
rest of a record), so it is hard to tell what a model is keying on. This probe shows each
model ONLY the fields named in a chain, asks for ONE field per turn, and carries the GOLD
answer of every earlier turn into the next (teacher forcing), so every step is measured
on exactly its listed cues. Chains (given → target → target …):

  1 name → formula → top band → first line
  2 formula → name → top band → first line
  3 bands → name → formula          4 bands → formula → name
  5 lines → name → formula          6 lines → formula → name
  7 bands+lines → name → formula    8 bands+lines → formula → name

"bands" as a cue = the four strongest Raman bands (integers, strongest first); "lines" =
the record's four LIBS lines. As TARGETS: the top (strongest) band, and the first line of
the record (the strongest line of the first element group; each element's strongest line
is normalised to 100, so lines 2–4 tie with it and score NEAR, not MISS).

Every step runs in two formats for every model:
  prose  a Q/A transcript, gold answers of earlier turns filled in
  xml    the §22 record holding ONLY the known fields, one blank, via the FIM sentinels
         (PSM and SPM); absent fields are omitted, never blanked, and no laser / system
When a formula is a CUE, v2 (trained on the raw IMA spelling, e.g. CaS6+O4·2H2O) is also
run with its own spelling ("v2·raw"), so an unfamiliar string is not mistaken for missing
knowledge.

Scoring: name — HIT if the answer IS the name, NEAR if the name appears; formula — HIT
if the answer normalises (formula_norm, "." ≡ "·") to the gold, NEAR if the gold appears
in the normalised line; top band — HIT within ±10 cm-1 of the strongest, NEAR within ±10
of another of the four strongest; first line — HIT within ±0.2 nm of line 1, NEAR of
lines 2–4.

  ./.venv/bin/python probe_chains.py --minerals common=Gypsum,less_common=X,stage1_only=Y \\
      --out ~/tmp/analysis/v3/chain_probe
"""

from __future__ import annotations

import argparse
import collections
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import torch  # noqa: E402
from transformers import AutoModelForCausalLM, AutoTokenizer  # noqa: E402

from fim_transform import fim_wrap  # noqa: E402
from formula_norm import normalize_formula  # noqa: E402

M = os.path.expanduser("~/models/olmo2-1b-spectra-full")
MODELS = {"v2": f"{M}/stage2/final", "v3": f"{M}/v3_stage2/final"}
RECORDS = os.path.expanduser("~/corpora/rock-olmo-training/v6/stage2/docs/xml_records.json")
V2_SHAPES = os.path.expanduser("~/corpora/rock-olmo-training/v4/stage2/docs/shapes.jsonl")

CHAINS = [
    ("name → formula → top band → first line", ["name"], ["formula", "top", "line"]),
    ("formula → name → top band → first line", ["formula"], ["name", "top", "line"]),
    ("bands → name → formula", ["bands"], ["name", "formula"]),
    ("bands → formula → name", ["bands"], ["formula", "name"]),
    ("lines → name → formula", ["lines"], ["name", "formula"]),
    ("lines → formula → name", ["lines"], ["formula", "name"]),
    ("bands + lines → name → formula", ["bands", "lines"], ["name", "formula"]),
    ("bands + lines → formula → name", ["bands", "lines"], ["formula", "name"]),
]
TARGET_LABEL = {"name": "name", "formula": "formula", "top": "top Raman band", "line": "first LIBS line"}


# ── gold ─────────────────────────────────────────────────────────────
def load_gold(species: str) -> dict:
    d = json.load(open(RECORDS))
    man = os.path.join(os.path.dirname(os.path.dirname(RECORDS)), "docs_manifest.json")
    val = set(json.load(open(man)).get("species_val", [])) if os.path.exists(man) else set()
    for group in ("trained", "untouched"):
        for r in d[group]:
            if r["species"] == species:
                grp = "val" if (group == "trained" and species in val) else group
                return {"name": r["species"], "formula": r["formula"], "bands": [int(b) for b in r["bands"]], "lines": [float(x) for x in r["libs"]], "xml_group": grp}
    raise SystemExit(f"{species}: not in xml_records.json (needs formula + canonical Raman >= 4 bands + LIBS)")


def v2_raw_formula(species: str) -> str:
    """The spelling v2's stage 2 trained on, read from its own shapes ('Gypsum (CaS6+O4·2H2O)')."""
    c = collections.Counter()
    pat = re.compile(re.escape(species) + r" \(([^()]*(?:\([^()]*\)[^()]*)*)\)")
    with open(V2_SHAPES) as fh:
        for line in fh:
            if f'"species": "{species}"' not in line:
                continue
            r = json.loads(line)
            for m in pat.finditer(r["prompt"] + " " + r["completion"]):
                c[m.group(1)] += 1
    return c.most_common(1)[0][0] if c else ""


# ── prompts ──────────────────────────────────────────────────────────
def fmt_bands(b: list[int]) -> str:
    return ", ".join(str(x) for x in b[:-1]) + f" and {b[-1]}"


def fmt_lines(ls: list[float]) -> str:
    s = [f"{x:.2f}" for x in ls]
    return ", ".join(s[:-1]) + f" and {s[-1]}"


OPENING = {
    "name": lambda g: f"The mineral is {g['name']}.",
    "formula": lambda g: f"A mineral has the ideal formula {g['formula']}.",
    "bands": lambda g: f"A mineral's four strongest Raman bands are at {fmt_bands(g['bands'])} cm-1, strongest first.",
    "lines": lambda g: f"A mineral's four strongest LIBS emission lines are at {fmt_lines(g['lines'])} nm.",
}
QUESTION = {
    "name": "What is the mineral?",
    "formula": "What is its chemical formula?",
    "top": "What is its strongest Raman band, in cm-1?",
    "line": "What is its strongest LIBS emission line, in nm?",
}


def gold_answer(g: dict, target: str) -> str:
    return {"name": g["name"], "formula": g["formula"], "top": str(g["bands"][0]), "line": f"{g['lines'][0]:.2f}"}[target]


FOLLOW_ON = {  # a second cue in the same opening refers back to the same mineral
    "lines": lambda g: f"Its four strongest LIBS emission lines are at {fmt_lines(g['lines'])} nm.",
}


def prose_prompt(g: dict, given: list[str], asked: list[str], target: str) -> str:
    lines = [" ".join(OPENING[c](g) if i == 0 else FOLLOW_ON[c](g) for i, c in enumerate(given))]
    for t in asked:
        lines += [f"Q: {QUESTION[t]}", f"A: {gold_answer(g, t)}"]
    lines += [f"Q: {QUESTION[target]}", "A:"]
    return "\n".join(lines)


BLANK = "\x00"


def xml_record(g: dict, known: set[str], target: str) -> str:
    """The §22 schema holding only the known fields, the target field BLANK, block order
    raman before libs. `known` may hold the full cue blocks ("bands", "lines") or the
    singletons an earlier turn answered ("top", "line")."""
    attrs = []
    for f, key in (("species", "name"), ("formula", "formula")):
        if key == target:
            attrs.append(f'{f}="{BLANK}"')
        elif key in known:
            attrs.append(f'{f}="{g[key]}"')
    out = ["<mineral" + ("" if not attrs else " " + " ".join(attrs)) + ">"]
    b = g["bands"]
    if "bands" in known:
        out.append("<raman>" + f"<top>{b[0]}</top>" + "".join(f"<next>{x}</next>" for x in b[1:]) + "</raman>")
    elif target == "top":
        out.append(f"<raman><top>{BLANK}</top></raman>")
    elif "top" in known:
        out.append(f"<raman><top>{b[0]}</top></raman>")
    ls = g["lines"]
    if "lines" in known:
        out.append("<libs>" + "".join(f"<line>{x:.2f}</line>" for x in ls) + "</libs>")
    elif target == "line":
        out.append(f"<libs><line>{BLANK}</line></libs>")
    elif "line" in known:
        out.append(f"<libs><line>{ls[0]:.2f}</line></libs>")
    out.append("</mineral>")
    return "\n".join(out)


# ── scoring ──────────────────────────────────────────────────────────
def _canon(s: str) -> str:
    return re.sub(r"\s+", "", s).replace("·", ".").lower().rstrip(".")


def _norm(s: str) -> str:
    try:
        return normalize_formula(s)
    except Exception:  # noqa: BLE001
        return s


def extract(gen: str, fmt: str, target: str) -> str:
    g = gen.lstrip()
    if fmt == "xml":
        stop = '"' if target in ("name", "formula") else "<"
        return g.split(stop, 1)[0].split("\n", 1)[0].strip()
    return g.split("\n", 1)[0].strip()


def score(target: str, ans: str, g: dict) -> str:
    a = ans.strip()
    if target == "name":
        first = re.split(r'[\n.,;(\"]', a, maxsplit=1)[0].strip().lower()
        if first == g["name"].lower():
            return "HIT"
        return "NEAR" if g["name"].lower() in a.lower() else "MISS"
    if target == "formula":
        tok = re.split(r"[\s,;]", a.strip(), maxsplit=1)[0].rstrip(".")
        if _canon(_norm(tok)) == _canon(g["formula"]) or _canon(_norm(a.rstrip("."))) == _canon(g["formula"]):
            return "HIT"
        return "NEAR" if _canon(g["formula"]) in _canon(_norm(a)) else "MISS"
    nums = [float(x) for x in re.findall(r"\d+(?:\.\d+)?", a)[:1]]
    if not nums:
        return "MISS"
    x = nums[0]
    ref, tol = (g["bands"], 10.0) if target == "top" else (g["lines"], 0.2)
    if abs(x - ref[0]) <= tol:
        return "HIT"
    return "NEAR" if any(abs(x - r) <= tol for r in ref[1:]) else "MISS"


# ── run ──────────────────────────────────────────────────────────────
def build_jobs(minerals: dict[str, str]) -> list[dict]:
    jobs = []
    for cat, sp in minerals.items():
        g = load_gold(sp)
        raw = v2_raw_formula(sp)
        g_raw = dict(g, formula=raw) if raw else None
        for ci, (label, given, targets) in enumerate(CHAINS, 1):
            for si, target in enumerate(targets):
                asked = targets[:si]
                cues = given + asked
                base = {"category": cat, "species": sp, "chain": ci, "chain_label": label, "step": si + 1, "target": target, "cues": cues, "gold": gold_answer(g, target)}
                jobs.append({**base, "fmt": "prose", "variant": "", "prompt": prose_prompt(g, given, asked, target), "gold_rec": g})
                rec = xml_record(g, set(given) | set(asked), target)
                pre, suf = rec.split(BLANK)
                for order in ("psm", "spm"):
                    jobs.append({**base, "fmt": "xml", "variant": order, "prompt": fim_wrap(pre, "", suf, order), "record": rec.replace(BLANK, "▢"), "gold_rec": g})
                if g_raw and "formula" in cues and raw != g["formula"]:
                    jobs.append({**base, "fmt": "prose", "variant": "raw", "prompt": prose_prompt(g_raw, given, asked, target), "gold_rec": g, "only_model": "v2"})
    return jobs


@torch.no_grad()
def generate(path: str, prompts: list[str], device: str, max_new: int) -> list[str]:
    tok = AutoTokenizer.from_pretrained(path)
    tok.padding_side = "left"
    if tok.pad_token_id is None:
        tok.pad_token = tok.eos_token
    model = AutoModelForCausalLM.from_pretrained(path, dtype=torch.bfloat16).to(device).eval()
    out = []
    for b in range(0, len(prompts), 16):
        enc = tok(prompts[b : b + 16], return_tensors="pt", padding=True).to(device)
        gen = model.generate(**enc, max_new_tokens=max_new, do_sample=False, pad_token_id=tok.pad_token_id)
        out += [tok.decode(r[enc["input_ids"].shape[1] :], skip_special_tokens=True) for r in gen]
    del model
    torch.cuda.empty_cache()
    return out


MARK = {"HIT": "✅", "NEAR": "🟡", "MISS": "❌"}


def _cell(s: str, n: int = 70) -> str:
    s = s.replace("\n", "⏎").replace("|", "\\|")
    return (s[:n] + "…") if len(s) > n else s


def report(results: list[dict], minerals: dict[str, str], exposure: dict, path: str) -> None:
    L = []
    L.append("# Controlled chained recall: v2 stage 2 vs v3 stage 2\n")
    L.append(f"Generated {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime())} by `dev/rock_olmo/probe_chains.py`. "
             "Greedy decoding, 40 new tokens. Each turn shows the model ONLY the fields named in its cues; "
             "answers to earlier turns are the GOLD values (teacher forcing), so every step is measured on exactly its listed cues.\n")
    L.append("**Legend.** ✅ HIT · 🟡 NEAR (name mentioned but not the answer; formula present in a longer answer; "
             "a band/line that is another of the four strongest) · ❌ MISS. "
             "Formats: **prose** = Q/A transcript; **xml·psm / xml·spm** = the §22 record holding only the known fields, one blank, "
             "in the two fill-in-the-middle orders; **v2·raw** = v2 re-asked with the formula in the raw IMA spelling it was trained on "
             "(only where a formula is a cue).\n")
    L.append("## Minerals\n")
    L.append("| category | species | formula | 4 strongest Raman bands | 4 LIBS lines | paper mentions (v6 pile) | v2 stage-2 rows | in v3 XML stage 2 |")
    L.append("|---|---|---|---|---|---|---|---|")
    for cat, sp in minerals.items():
        g = load_gold(sp)
        e = exposure.get(sp, {})
        L.append(f"| {cat} | {sp} | `{g['formula']}` (v2 trained `{e.get('v2_raw', '?')}`) | {', '.join(map(str, g['bands']))} | "
                 f"{', '.join(f'{x:.2f}' for x in g['lines'])} | {e.get('mentions', '?')} in {e.get('papers', '?')} papers | "
                 f"{e.get('v2_train', '?')} train / {e.get('v2_val', '?')} val | { {'trained': 'yes', 'val': 'no (validation species, held out)', 'untouched': 'no (untouched set)'}[g['xml_group']] } |")
    # summary
    L.append("\n## Summary\n")
    L.append("Best-format result per model and step (prose, xml·psm, xml·spm, v2·raw — the best status across that model's formats); the per-format detail follows.\n")
    cols = [(m, "any") for m in MODELS]
    by = collections.defaultdict(dict)
    for r in results:
        k = (r["species"], r["chain"], r["step"])
        prev = by[k].get(r["model"])
        rank = {"HIT": 2, "NEAR": 1, "MISS": 0}
        if prev is None or rank[r["status"]] > rank[prev]:
            by[k][r["model"]] = r["status"]
    for cat, sp in minerals.items():
        L.append(f"\n### {cat}: {sp}\n")
        L.append("| chain | step | cues | target | " + " | ".join(m for m in MODELS) + " |")
        L.append("|---|---|---|---|" + "---|" * len(MODELS))
        for ci, (label, given, targets) in enumerate(CHAINS, 1):
            for si, t in enumerate(targets):
                cues = " + ".join(given + targets[:si])
                L.append(f"| {ci}. {label} | {si+1} | {cues} | {TARGET_LABEL[t]} | " + " | ".join(MARK[by[(sp, ci, si + 1)].get(m, 'MISS')] for m in MODELS) + " |")
    # tallies
    L.append("\n### Tally by format (HIT / NEAR / MISS over all steps)\n")
    L.append("| model | format | HIT | NEAR | MISS |")
    L.append("|---|---|---|---|---|")
    tally = collections.Counter((r["model"], r["fmt"] + (f"·{r['variant']}" if r["variant"] else ""), r["status"]) for r in results)
    for m in MODELS:
        for f in ("prose", "prose·raw", "xml·psm", "xml·spm"):
            n = sum(tally[(m, f, s)] for s in MARK)
            if n:
                L.append(f"| {m} | {f} | {tally[(m, f, 'HIT')]} | {tally[(m, f, 'NEAR')]} | {tally[(m, f, 'MISS')]} |")
    L.append("\n### Tally by direction (best format per model)\n")
    L.append("| target | cues | " + " | ".join(f"{m} HIT" for m in MODELS) + " | steps |")
    L.append("|---|---|" + "---|" * len(MODELS) + "---|")
    groups = collections.defaultdict(lambda: collections.Counter())
    for (sp, ci, st), d in by.items():
        label, given, targets = CHAINS[ci - 1]
        t = targets[st - 1]
        cueset = " + ".join(sorted(set(given + targets[: st - 1]), key=["name", "formula", "bands", "lines", "top", "line"].index))
        groups[(TARGET_LABEL[t], cueset)]["n"] += 1
        for m in MODELS:
            groups[(TARGET_LABEL[t], cueset)][m] += d.get(m) == "HIT"
    for (t, c), cnt in sorted(groups.items()):
        L.append(f"| {t} | {c} | " + " | ".join(str(cnt[m]) for m in MODELS) + f" | {cnt['n']} |")
    # detail
    L.append("\n## Detail: every prompt and response\n")
    idx = collections.defaultdict(list)
    for r in results:
        idx[(r["species"], r["chain"], r["step"])].append(r)
    for cat, sp in minerals.items():
        L.append(f"\n### {cat}: {sp}\n")
        for ci, (label, given, targets) in enumerate(CHAINS, 1):
            L.append(f"\n#### Chain {ci}: {label}\n")
            for si, t in enumerate(targets):
                rows = idx[(sp, ci, si + 1)]
                cues = " + ".join(given + targets[:si])
                L.append(f"**Step {si+1}. Cues: {cues}. Target: {TARGET_LABEL[t]}. Gold: `{rows[0]['gold']}`**\n")
                pr = next(r for r in rows if r["fmt"] == "prose" and not r["variant"])
                L.append("Prose prompt:\n\n```text\n" + pr["prompt"] + "\n```\n")
                xr = next(r for r in rows if r["fmt"] == "xml")
                L.append("XML record shown (▢ = the blank; wrapped as `<|fim_prefix|>…<|fim_suffix|>…<|fim_middle|>` in PSM, `<|fim_suffix|>…<|fim_prefix|>…<|fim_middle|>` in SPM):\n\n```xml\n" + xr["record"] + "\n```\n")
                raw = [r for r in rows if r["variant"] == "raw"]
                if raw:
                    L.append("v2·raw prose prompt (the spelling v2 trained on):\n\n```text\n" + raw[0]["prompt"] + "\n```\n")
                L.append("| model | format | response (raw, first 70 chars) | extracted answer | status |")
                L.append("|---|---|---|---|---|")
                order = {"prose": 0, "xml": 1}
                for r in sorted(rows, key=lambda r: (list(MODELS).index(r["model"]), order[r["fmt"]], r["variant"])):
                    f = r["fmt"] + (f"·{r['variant']}" if r["variant"] else "")
                    L.append(f"| {r['model']} | {f} | `{_cell(r['response'])}` | `{_cell(r['answer'], 40)}` | {MARK[r['status']]} |")
                L.append("")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    open(path, "w", encoding="utf-8").write("\n".join(L) + "\n")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--minerals", required=True, help="category=Species[,category=Species...]")
    ap.add_argument("--exposure", default="", help="JSON {species: {mentions, papers, v2_train, v2_val}} for the minerals table")
    ap.add_argument("--device", default="cuda:0")
    ap.add_argument("--max-new", type=int, default=40)
    ap.add_argument("--out", default=os.path.expanduser("~/tmp/analysis/v3/chain_probe"))
    args = ap.parse_args()
    minerals = dict(kv.split("=", 1) for kv in args.minerals.split(","))
    exposure = json.load(open(args.exposure)) if args.exposure else {}
    for sp in minerals.values():
        exposure.setdefault(sp, {})["v2_raw"] = v2_raw_formula(sp)
    jobs = build_jobs(minerals)
    results = []
    for m, path in MODELS.items():
        mine = [j for j in jobs if j.get("only_model", m) == m]
        t0 = time.time()
        gens = generate(path, [j["prompt"] for j in mine], args.device, args.max_new)
        for j, gtext in zip(mine, gens):
            ans = extract(gtext, j["fmt"], j["target"])
            results.append({**{k: v for k, v in j.items() if k != "gold_rec"}, "model": m, "response": gtext, "answer": ans, "status": score(j["target"], ans, j["gold_rec"])})
        print(f"{m}: {len(mine)} prompts in {time.time() - t0:.0f}s", flush=True)
    os.makedirs(args.out, exist_ok=True)
    json.dump(results, open(os.path.join(args.out, "chain_results.json"), "w"), indent=1, ensure_ascii=False)
    rpath = os.path.join(args.out, "chain_report.md")
    report(results, minerals, exposure, rpath)
    print(f"-> {rpath}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
