#!/usr/bin/env python3
"""OCR sampling A/B (2026-10-04): is paddle's T=0.8 what injects foreign script?

The finding this answers. PaddleOCR-VL drops CJK/Thai/Tamil/Arabic/Cyrillic
fragments into Latin-script prose and paraphrases the sentence around them
("the data points in Figure 5 demonstrate the重要性 of the model"): 47 % of
accepted Latin papers carry at least one such paragraph, ~4 % of pages, flat
across Aug/Sep/Oct. The OCR samples at T=0.8 (top_k 40, top_p 0.95, min_p 0.05,
llama-cpp-python's defaults) since 2026-08-15 as a hedge against greedy loops.
The lab's own client sends temperature 0 to every llama-cpp/vllm/sglang/mlx
backend, and the per-region loop guard (39f1727, 2026-09-19) now retries a
looping crop warmer and collapses what still loops, so the hedge may no longer
be needed. Prompt format was checked first and matches the lab: the GGUF's own
template (`<|begin_of_sentence|>User: <|IMAGE_START|>…<|IMAGE_END|>OCR:\\n
Assistant:\\n`), per-block prompts, and pixel bounds 112,896-1,003,520.

Design. Pages are sampled from accepted Latin-script papers: INJECTED pages
(the census marker on the stored markdown) and CONTROL pages (papers with no
marker anywhere). The production pipeline (layout + crop) runs on each page
with a recording recognizer, so the crops are byte-identical to what the OCR
sends; each crop keeps its block bbox. The crops are then read on CPU, in a
through the PRODUCTION client: GraphQL visionCompletion against the 3060 box's
LLMVP (borrowed from the repack lane, operator 2026-10-04), with production
sampling and the production loop guard. A CPU fallback reads the crops through
llama-cpp-python's PaddleOCRChatHandler (LLMVP's own handler) in a separate
process; it ran first at ~15 reads/min and was stopped at 264 reads.

Arms: T=0 (the lab's greedy, one draw) and T=0.8 (current, two draws).

PRE-REGISTERED DECISION (written before any run). Adopt T=0 + the existing
loop guard if, over all crops:
  1. injection: T=0's crop injection rate is below T=0.8's;
  2. fidelity: on CONTROL crops T=0's word and numeric recall against the
     text layer inside the crop are each no more than 1 point below T=0.8's;
  3. loops: T=0's loop-guard collapses (still looping after the warm retry)
     are at most 2 % of crops.
Otherwise report, and keep T=0.8 pending an injection guard.

Two venvs (paddlex+fitz vs llama_cpp), so imports are per command:
    tools/pdf_extract/.venv/bin/python dev/bench_ocr_sampling.py sample
    tools/pdf_extract/.venv/bin/python dev/bench_ocr_sampling.py crops
    tools/pdf_extract/.venv/bin/python dev/bench_ocr_sampling.py run \
        --server http://192.168.1.76:8008 --model paddle-ocr-vl-3060   # production path
    llmvp/.venv/bin/python dev/bench_ocr_sampling.py run --workers 3 --threads 8  # CPU fallback
    tools/pdf_extract/.venv/bin/python dev/bench_ocr_sampling.py score
"""

from __future__ import annotations

import argparse
import base64
import collections
import json
import os
import random
import re
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOL_DIR = os.path.join(REPO, "tools", "pdf_extract")
CORPUS = os.path.expanduser(
    os.environ.get("OUROBOROS_CORPUS", "~/corpora/ouroboros-spectra")
)
OUT = os.path.expanduser("~/tmp/ocr_sampling")
MODEL = os.path.expanduser("~/models/PaddleOCR-VL-1.6.Q8_0.gguf")
MMPROJ = os.path.expanduser("~/models/PaddleOCR-VL-1.6-GGUF-mmproj.gguf")
DPI = 160  # extract_batch's --dpi default
SEED = 20261004

# Production sampling (llmvp/configs/paddle-ocr-vl.yaml + llama-cpp-python's
# create_chat_completion defaults; the client sends only temperature).
TOP_P, TOP_K, MIN_P, REPEAT_PENALTY = 0.95, 40, 0.05, 1.0
MAX_TOKENS = 4096

_NL = r"Ѐ-ӿ֐-ۿऀ-෿฀-๿぀-ヿ㐀-鿿가-힯"
NONLATIN = re.compile(f"[{_NL}]")
# A non-Latin run of 1-8 characters glued to Latin text: the injection marker.
INJ = re.compile(
    rf"[A-Za-zÀ-ÿ][^\sA-Za-zÀ-ÿ]{{0,1}}[{_NL}]{{1,8}}(?=[\sA-Za-zÀ-ÿ.,;:)。、，]|$)"
)
PAGE_SEP = "\n\n---\n\n"


def injected(text: str) -> bool:
    return bool(INJ.search(text or ""))


def _records() -> dict:
    recs: dict = {}
    for name in ("papers.jsonl", "extraction.jsonl"):
        with open(
            os.path.join(CORPUS, "databank", name), encoding="utf-8", errors="replace"
        ) as fh:
            for line in fh:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                k = r.get("paper_key")
                if k:
                    recs[k] = {**recs.get(k, {}), **r}
    return recs


# ── sample (tool venv) ──────────────────────────────────────────────────────


def cmd_sample(a) -> None:
    import fitz

    rng = random.Random(SEED)
    recs = _records()
    cand_inj, cand_ctl = [], []
    for k, r in sorted(recs.items()):
        if r.get("review_status") != "accepted" or r.get("record_kind") == "supplement":
            continue
        md_path = os.path.join(CORPUS, "databank", "markdown", f"{k}.md")
        pdf = os.path.join(CORPUS, r.get("pdf_path") or "")
        if not (r.get("pdf_path") and os.path.exists(md_path) and os.path.exists(pdf)):
            continue
        md = open(md_path, encoding="utf-8", errors="replace").read()
        letters = len(re.findall(r"[^\W\d_]", md))
        if not letters or len(NONLATIN.findall(md)) / letters > 0.02:
            continue
        pages = md.split(PAGE_SEP)
        bad = [i for i, p in enumerate(pages) if injected(p)]
        (cand_inj if bad else cand_ctl).append((k, pdf, len(pages), bad))
    rng.shuffle(cand_inj)
    rng.shuffle(cand_ctl)
    chosen = []
    for pool, kind, want in (
        (cand_inj, "injected", a.injected),
        (cand_ctl, "control", a.control),
    ):
        n = 0
        for k, pdf, n_md, bad in pool:
            if n >= want:
                break
            try:
                doc = fitz.open(pdf)
            except Exception:  # noqa: BLE001 — unreadable PDF: skip
                continue
            if doc.page_count != n_md:
                continue  # page provenance must hold
            if kind == "injected":
                i = rng.choice(bad)
            else:
                i = rng.randrange(n_md)
            if len(doc[i].get_text().strip()) < 400:
                continue  # need a text layer to score against
            chosen.append({"paper_key": k, "pdf": pdf, "page": i, "set": kind})
            n += 1
    os.makedirs(OUT, exist_ok=True)
    with open(os.path.join(OUT, "pages.json"), "w") as fh:
        json.dump(chosen, fh, indent=1)
    print(f"{len(cand_inj)} injected / {len(cand_ctl)} clean candidate papers; "
          f"chose {collections.Counter(p['set'] for p in chosen)} -> {OUT}/pages.json")  # fmt: skip


# ── crops (tool venv) ───────────────────────────────────────────────────────


class _Recorder:
    """Stands in for the VL recognizer: keeps each crop exactly as the
    production recognizer would encode it, and answers with a unique marker so
    the assembled blocks say which crop each one is."""

    def __init__(self):
        import types

        self.batch_sampler = types.SimpleNamespace(batch_size=8192)
        self.items: list = []

    def predict(self, items, **kw):
        sys.path.insert(0, TOOL_DIR)
        import extract_batch as eb
        from paddlex.inference.models.doc_vlm.result import DocVLMResult

        for item in items:
            n = len(self.items)
            uri = eb._encode_png_data_uri(item["image"])
            self.items.append({"query": str(item.get("query") or "OCR:"), "uri": uri})
            out = {k: v for k, v in item.items()}
            out["result"] = f"QXCROP{n}QX"
            yield DocVLMResult(out)

    def close(self) -> None:
        return None


def cmd_crops(a) -> None:
    import fitz

    sys.path.insert(0, TOOL_DIR)
    import extract_batch as eb
    from paddleocr import PaddleOCRVL

    pages = json.load(open(os.path.join(OUT, "pages.json")))
    pipe = PaddleOCRVL(**eb._vl_pipe_kwargs("llmvp", "paddle-ocr-vl", 0))
    inner = pipe.paddlex_pipeline._pipeline
    rec = _Recorder()
    inner.vl_rec_model = rec
    crop_dir = os.path.join(OUT, "crops")
    os.makedirs(crop_dir, exist_ok=True)
    manifest = []
    for pi, pg in enumerate(pages):
        doc = fitz.open(pg["pdf"])
        page = doc[pg["page"]]
        png = os.path.join(OUT, "page.png")
        page.get_pixmap(dpi=DPI).save(png)
        rec.items = []
        blocks = {}
        for res in pipe.predict(png, temperature=0.0, top_p=TOP_P):
            for blk in res["parsing_res_list"]:
                m = re.search(r"QXCROP(\d+)QX", str(getattr(blk, "content", "") or ""))
                if m:
                    blocks[int(m.group(1))] = {
                        "label": getattr(blk, "label", ""),
                        "bbox": [float(v) for v in getattr(blk, "bbox", [])[:4]],
                    }
        scale = 72.0 / DPI
        for n, it in enumerate(rec.items):
            b = blocks.get(n) or {}
            bbox_pt = [v * scale for v in b.get("bbox", [])] or None
            words = []
            if bbox_pt:
                x0, y0, x1, y1 = bbox_pt
                words = [
                    w[4]
                    for w in page.get_text("words")
                    if x0 <= (w[0] + w[2]) / 2 <= x1 and y0 <= (w[1] + w[3]) / 2 <= y1
                ]
            cid = f"{pi:03d}_{n:03d}"
            with open(os.path.join(crop_dir, f"{cid}.png"), "wb") as fh:
                fh.write(base64.b64decode(it["uri"].split(",", 1)[1]))
            manifest.append(
                {
                    "crop": cid,
                    "page_ix": pi,
                    "paper_key": pg["paper_key"],
                    "page": pg["page"],
                    "set": pg["set"],
                    "query": it["query"],
                    "label": b.get("label", ""),
                    "bbox_pt": bbox_pt,
                    "truth": " ".join(words),
                }
            )
        print(f"[{pi + 1}/{len(pages)}] {pg['set']:<8} {pg['paper_key'][:50]} p{pg['page'] + 1}: "
              f"{len(rec.items)} crops", flush=True)  # fmt: skip
    with open(os.path.join(OUT, "crops.jsonl"), "w") as fh:
        for m in manifest:
            fh.write(json.dumps(m, ensure_ascii=False) + "\n")
    print(f"{len(manifest)} crops -> {OUT}/crops.jsonl")


# ── run (llmvp venv, CPU only) ──────────────────────────────────────────────


def _worker(args) -> list:
    """Read a shard of crops under every arm. One model per worker process."""
    shard, arms, threads = args
    os.environ["CUDA_VISIBLE_DEVICES"] = ""
    sys.path.insert(0, os.path.join(REPO, "llmvp"))
    sys.path.insert(0, TOOL_DIR)
    import ocr_loop_lib as loops
    from inference.vision_text import clean as vision_clean
    from llama_cpp import Llama
    from llama_cpp.llama_multimodal import PaddleOCRChatHandler

    llm = Llama(
        model_path=MODEL,
        chat_handler=PaddleOCRChatHandler(
            mmproj_path=MMPROJ, verbose=False, use_gpu=False
        ),
        n_ctx=16384,
        n_gpu_layers=0,
        n_threads=threads,
        n_threads_batch=threads,
        verbose=False,
        seed=-1,
    )

    def read(uri: str, query: str, temp: float) -> tuple[str, int]:
        msg = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": query},
                    {"type": "image_url", "image_url": {"url": uri}},
                ],
            }
        ]  # fmt: skip — GraphQL's part order (graphql_api.vision_completion)
        out = llm.create_chat_completion(
            messages=msg,
            max_tokens=MAX_TOKENS,
            temperature=temp,
            top_p=TOP_P,
            top_k=TOP_K,
            min_p=MIN_P,
            repeat_penalty=REPEAT_PENALTY,
            stop=["</s>"],
        )
        text = vision_clean(
            out["choices"][0]["message"].get("content") or "", "paddleocr"
        )
        return text, int((out.get("usage") or {}).get("completion_tokens") or 0)

    rows = []
    for c in shard:
        png = open(os.path.join(OUT, "crops", f"{c['crop']}.png"), "rb").read()
        uri = "data:image/png;base64," + base64.b64encode(png).decode("ascii")
        for temp, draws in arms:
            for d in range(draws):
                t0 = time.monotonic()
                first, toks = read(uri, c["query"], temp)
                final, guard = first, "clean"
                if loops.find_loops(
                    first
                ):  # extract_batch._loop_guard, verbatim policy
                    warm = min(1.0, max(float(temp), 0.6) + 0.2)
                    second, toks2 = read(uri, c["query"], warm)
                    toks += toks2
                    if second and not loops.find_loops(second):
                        final, guard = second, "retried"
                    else:

                        def cost(t):
                            lc = loops.loop_cost(t)
                            return lc["words"] + lc["chars"]

                        cand = (
                            second if second and cost(second) < cost(first) else first
                        )
                        final, _ = loops.collapse_loops(cand)
                        guard = "collapsed"
                rows.append({
                    "crop": c["crop"], "temp": temp, "draw": d, "first": first,
                    "final": final, "guard": guard, "tokens": toks,
                    "seconds": round(time.monotonic() - t0, 2),
                })  # fmt: skip
        print(f"  {c['crop']} done ({len(rows)} reads)", flush=True)
    return rows


def _read_remote(c: dict, arms, url: str, model: str) -> list:
    """One crop under every arm through the PRODUCTION client: GraphQL
    visionCompletion (prompt, image, model, maxTokens, temperature — nothing
    else is transported), the box's LLMVP and its mtmd handler, and the
    tool's loop-guard policy. Tool venv."""
    sys.path.insert(0, TOOL_DIR)
    import extract_batch as eb
    import ocr_loop_lib as loops

    png = open(os.path.join(OUT, "crops", f"{c['crop']}.png"), "rb").read()
    uri = "data:image/png;base64," + base64.b64encode(png).decode("ascii")

    def read(temp: float) -> str:
        text, _served = eb._vision_completion(
            url, model, uri, c["query"], MAX_TOKENS, temp
        )
        return text

    rows = []
    for temp, draws in arms:
        for d in range(draws):
            t0 = time.monotonic()
            first = read(temp)
            final, guard = first, "clean"
            if loops.find_loops(first):  # extract_batch._loop_guard, verbatim policy
                second = read(min(1.0, max(float(temp), 0.6) + 0.2))
                if second and not loops.find_loops(second):
                    final, guard = second, "retried"
                else:

                    def cost(t):
                        lc = loops.loop_cost(t)
                        return lc["words"] + lc["chars"]

                    cand = second if second and cost(second) < cost(first) else first
                    final, _ = loops.collapse_loops(cand)
                    guard = "collapsed"
            rows.append({
                "crop": c["crop"], "temp": temp, "draw": d, "first": first,
                "final": final, "guard": guard, "tokens": 0,
                "seconds": round(time.monotonic() - t0, 2),
            })  # fmt: skip
    return rows


def cmd_run(a) -> None:
    crops = [json.loads(x) for x in open(os.path.join(OUT, "crops.jsonl"))]
    out_path = os.path.join(OUT, "reads.jsonl")
    done = set()
    if os.path.exists(out_path):
        done = {json.loads(x)["crop"] for x in open(out_path) if x.strip()}
    todo = [c for c in crops if c["crop"] not in done]
    arms = [(float(t), int(n)) for t, n in zip(a.arms.split(","), a.draws.split(","))]
    if a.server:
        from concurrent.futures import ThreadPoolExecutor, as_completed

        print(f"{len(todo)} crops to read ({len(done)} done) x arms {arms} on "
              f"{a.server} [{a.model}] x{a.concurrency}", flush=True)  # fmt: skip
        with ThreadPoolExecutor(a.concurrency) as pool, open(out_path, "a") as fh:
            futs = {
                pool.submit(_read_remote, c, arms, a.server, a.model): c for c in todo
            }
            for n, f in enumerate(as_completed(futs), 1):
                try:
                    rows = f.result()
                except (
                    Exception
                ) as exc:  # noqa: BLE001 — a crop that errors is retried on resume
                    print(
                        f"  {futs[f]['crop']} ERROR {type(exc).__name__}: {str(exc)[:160]}",
                        flush=True,
                    )
                    continue
                for r in rows:
                    fh.write(json.dumps(r, ensure_ascii=False) + "\n")
                fh.flush()
                if n % 25 == 0:
                    print(f"  {n}/{len(todo)} crops", flush=True)
        return
    from multiprocessing import get_context

    # Small shards so progress lands on disk as it goes (resume-safe).
    shards = [todo[i : i + 4] for i in range(0, len(todo), 4)]
    print(
        f"{len(todo)} crops to read ({len(done)} done) x arms {arms} on {a.workers} CPU workers"
    )
    with get_context("spawn").Pool(a.workers) as pool, open(out_path, "a") as fh:
        for rows in pool.imap_unordered(
            _worker, [(s, arms, a.threads) for s in shards]
        ):
            for r in rows:
                fh.write(json.dumps(r, ensure_ascii=False) + "\n")
            fh.flush()


# ── score (tool venv) ───────────────────────────────────────────────────────


def _norm_words(s: str) -> list[str]:
    s = re.sub(r"\$|\\[a-z]+|[{}^_]", " ", s.lower())
    return re.findall(r"[^\W_]{2,}", s)


def _nums(s: str) -> list[str]:
    return re.findall(r"\d+(?:[.,]\d+)?", s)


def _recall(truth: list[str], got: list[str]) -> tuple[int, int]:
    pool = collections.Counter(got)
    hit = 0
    for w in truth:
        if pool[w] > 0:
            pool[w] -= 1
            hit += 1
    return hit, len(truth)


def cmd_score(a) -> None:
    sys.path.insert(0, TOOL_DIR)
    import ocr_loop_lib as loops

    crops = {
        json.loads(x)["crop"]: json.loads(x)
        for x in open(os.path.join(OUT, "crops.jsonl"))
    }
    reads = [json.loads(x) for x in open(os.path.join(OUT, "reads.jsonl")) if x.strip()]
    agg = collections.defaultdict(collections.Counter)
    examples = collections.defaultdict(list)
    for r in reads:
        c = crops[r["crop"]]
        for key in ((r["temp"], "all"), (r["temp"], c["set"])):
            g = agg[key]
            g["reads"] += 1
            g["injected"] += injected(r["final"])
            g["first_loop"] += bool(loops.find_loops(r["first"]))
            g["collapsed"] += r["guard"] == "collapsed"
            g["tokens"] += r["tokens"]
            g["seconds"] += r["seconds"]
            if c["truth"] and c["query"] == "OCR:":
                wh, wt = _recall(_norm_words(c["truth"]), _norm_words(r["final"]))
                nh, nt = _recall(_nums(c["truth"]), _nums(r["final"]))
                g["w_hit"] += wh
                g["w_tot"] += wt
                g["n_hit"] += nh
                g["n_tot"] += nt
        if injected(r["final"]) and len(examples[r["temp"]]) < 6:
            m = INJ.search(r["final"])
            examples[r["temp"]].append(
                f"{r['crop']}: …{r['final'][max(0, m.start() - 60):m.end() + 20]!r}"
            )
    n_crops = len({r["crop"] for r in reads})
    print(f"{n_crops} crops read; {len(reads)} reads\n")
    print(f"{'arm':<7} {'set':<9} {'reads':>6} {'injected':>9} {'1st-loop':>9} {'collapsed':>10} "
          f"{'word rec':>9} {'num rec':>8} {'tok/read':>9} {'s/read':>7}")  # fmt: skip
    for (temp, s), g in sorted(agg.items()):
        rd = max(1, g["reads"])
        print(
            f"T={temp:<5} {s:<9} {g['reads']:>6} {g['injected'] / rd:>9.2%} {g['first_loop'] / rd:>9.2%} "
            f"{g['collapsed'] / rd:>10.2%} {g['w_hit'] / max(1, g['w_tot']):>9.3f} "
            f"{g['n_hit'] / max(1, g['n_tot']):>8.3f} {g['tokens'] / rd:>9.0f} {g['seconds'] / rd:>7.1f}"
        )
    for temp, xs in sorted(examples.items()):
        print(f"\ninjections at T={temp}:")
        for x in xs:
            print("   ", x)

    # PAIRED, per crop: greedy against the mean of the T=0.8 draws, so crop
    # difficulty cancels. Text crops with a text layer only.
    by_crop = collections.defaultdict(lambda: collections.defaultdict(list))
    for r in reads:
        by_crop[r["crop"]][r["temp"]].append(r)
    temps = sorted({r["temp"] for r in reads})
    if len(temps) >= 2:
        lo, hi = temps[0], temps[-1]
        print(f"\npaired word recall, T={lo} minus T={hi} (per text crop, by set):")
        for s in ("control", "injected"):
            deltas = []
            for cid, arms in by_crop.items():
                c = crops[cid]
                if (
                    c["set"] != s
                    or c["query"] != "OCR:"
                    or not c["truth"]
                    or not arms[lo]
                    or not arms[hi]
                ):
                    continue
                tw = _norm_words(c["truth"])
                if len(tw) < 5:
                    continue

                def rec(r):
                    h, t = _recall(tw, _norm_words(r["final"]))
                    return h / t

                deltas.append(
                    rec(arms[lo][0]) - sum(map(rec, arms[hi])) / len(arms[hi])
                )
            if deltas:
                deltas.sort()
                worse = sum(d < -0.02 for d in deltas)
                better = sum(d > 0.02 for d in deltas)
                print(f"  {s:<9} n={len(deltas):>4} mean {sum(deltas) / len(deltas):+.4f} "
                      f"median {deltas[len(deltas) // 2]:+.4f}  greedy better {better}, worse {worse}")  # fmt: skip
        # INJECTION GUARD, simulated: where greedy injects, does a warm redraw
        # come back clean? (What a per-region retry would buy.)
        g_inj = [
            cid
            for cid, arms in by_crop.items()
            if arms[lo] and injected(arms[lo][0]["final"])
        ]
        rescued = [
            cid
            for cid in g_inj
            if any(not injected(r["final"]) for r in by_crop[cid][hi])
        ]
        print(f"\nguard simulation: greedy injected on {len(g_inj)} crops; a T={hi} draw was clean "
              f"on {len(rescued)} of them")  # fmt: skip


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("sample")
    s.add_argument("--injected", type=int, default=30)
    s.add_argument("--control", type=int, default=20)
    sub.add_parser("crops")
    r = sub.add_parser("run")
    r.add_argument("--arms", default="0,0.8")
    r.add_argument("--draws", default="1,2")
    r.add_argument("--workers", type=int, default=3)
    r.add_argument("--threads", type=int, default=8)
    r.add_argument(
        "--server", default="", help="LLMVP base URL: read through production GraphQL"
    )
    r.add_argument("--model", default="paddle-ocr-vl-3060")
    r.add_argument("--concurrency", type=int, default=4)
    sub.add_parser("score")
    a = ap.parse_args()
    {"sample": cmd_sample, "crops": cmd_crops, "run": cmd_run, "score": cmd_score}[
        a.cmd
    ](a)


if __name__ == "__main__":
    main()
