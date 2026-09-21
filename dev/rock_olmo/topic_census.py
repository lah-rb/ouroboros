#!/usr/bin/env python3
"""Topic exposure per pretraining source with ONE fixed classifier (rock venv; network for two Hub shards). See BASE_EXPOSURE.md.

Sources sampled: the three local dolmino shards (pes2o, wiki, dclm) plus the smallest
arxiv and starcoder shards of allenai/olmo-mix-1124 (OLMo-2's pretraining mix; ungated).
Per source, up to --docs documents: (a) `replay.science_like` — >= 3 generic science
keyword hits in the first 1,500 chars (the replay filter); (b) a DOMAIN lexicon of
mineralogy / vibrational-spectroscopy terms — a document counts when >= 2 distinct
terms occur. Fractions are reported by documents and by characters (token proxy).
Multiply by the published per-source token counts to estimate exposure; the same
classifier applied to any other corpus (The Stack v2, a code shard) is the
apples-to-apples comparison."""
import argparse, collections, gzip, io, json, os, re, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from huggingface_hub import HfApi, hf_hub_download
from replay import science_like
from fim_transform import iter_docs

RAW = os.path.expanduser("~/corpora/rock-olmo-training/v4/replay/raw/data")
LOCAL = {"pes2o": f"{RAW}/pes2o/pes2o-0025.json.gz", "wiki": f"{RAW}/wiki/wiki-0001.json.gz", "dclm": f"{RAW}/dclm/0246/dclm-0001.json.zst"}
DOMAIN = re.compile(
    r"\b(raman|infrared|ftir|wavenumber|cm-1|cm⁻¹|libs|laser-induced breakdown|emission line|xrd|x-ray diffraction|"
    r"mineral\w*|crystal\w*|lattice|unit cell|space group|polymorph|mineralog\w*|petrolog\w*|geochem\w*|"
    r"quartz|calcite|feldspar|olivine|pyroxene|garnet|zeolite|mica|amphibole|apatite|hematite|magnetite|gypsum|"
    r"sulfide|sulphide|carbonate|silicate|phosphate|sulfate|sulphate|oxide|hydroxide|"
    r"spectroscop\w*|spectr(um|a)\b|absorption band|vibrational mode|phonon)\b",
    re.I,
)

WINDOW = 20000  # chars scanned for domain terms (LaTeX preambles and licence headers eat the first few k)

def domain_hits(text: str) -> int:
    return len({m.group(0).lower() for m in DOMAIN.finditer(text[:WINDOW])})

def smallest_shard(repo: str, prefix: str, min_mb: float = 10.0) -> str:
    """The smallest shard of at least min_mb (a 0 MB shard of 25 MATLAB files is not a sample)."""
    api = HfApi()
    files = [(f.size or 0, f.path) for f in api.list_repo_tree(repo, repo_type="dataset", path_in_repo=prefix, recursive=True) if getattr(f, "size", None)]
    files = [x for x in files if x[1].endswith((".json.gz", ".jsonl.gz", ".json.zst", ".jsonl.zst")) and x[0] >= min_mb * 1e6]
    size, path = min(files)
    print(f"  {prefix}: smallest shard >= {min_mb} MB: {path} ({size/1e6:.0f} MB)", flush=True)
    return hf_hub_download(repo, path, repo_type="dataset", local_dir=os.path.expanduser("~/corpora/rock-olmo-training/v6/census_raw"))

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--docs", type=int, default=20000)
    ap.add_argument("--only", default="", help="comma list of sources to run (default all)")
    ap.add_argument("--out", default=os.path.expanduser("~/tmp/analysis/v3/topic_census.json"))
    a = ap.parse_args()
    sources = dict(LOCAL)
    for name in ("arxiv", "starcoder"):
        try:
            sources[name] = smallest_shard("allenai/olmo-mix-1124", f"data/{name}")
        except Exception as e:  # noqa: BLE001
            print(f"  {name}: fetch failed {type(e).__name__}: {str(e)[:160]}", flush=True)
    if a.only:
        keep = set(a.only.split(","))
        sources = {k: v for k, v in sources.items() if k in keep}
        prev = json.load(open(a.out)) if os.path.exists(a.out) else {}
    else:
        prev = {}
    out = dict(prev)
    for name, path in sources.items():
        t0 = time.time()
        n = chars = sci = sci_chars = dom = dom_chars = 0
        hist = collections.Counter()
        for d in iter_docs(path):
            text = (d.get("text") or "")
            if len(text) < 200:
                continue
            n += 1; chars += len(text)
            if science_like(text):
                sci += 1; sci_chars += len(text)
            h = domain_hits(text)
            hist[min(h, 5)] += 1
            if h >= 2:
                dom += 1; dom_chars += len(text)
            if n >= a.docs:
                break
        out[name] = {"docs": n, "MB": round(chars / 1e6, 1), "science_like_docs": round(sci / max(1, n), 4), "science_like_chars": round(sci_chars / max(1, chars), 4),
                     "domain_docs": round(dom / max(1, n), 4), "domain_chars": round(dom_chars / max(1, chars), 4), "domain_hits_hist": dict(sorted(hist.items())), "shard": os.path.basename(path)}
        print(f"{name:10s} docs {n:6d} | science_like {100*sci/max(1,n):5.1f}% docs / {100*sci_chars/max(1,chars):5.1f}% chars | domain(>=2 terms) {100*dom/max(1,n):5.1f}% docs / {100*dom_chars/max(1,chars):5.1f}% chars | {time.time()-t0:.0f}s", flush=True)
    json.dump(out, open(a.out, "w"), indent=1)
    print("DONE", flush=True)
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
