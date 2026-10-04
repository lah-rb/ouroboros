#!/usr/bin/env python
"""Repair degenerate OCR loops by re-OCR'ing the looped PAGES with paddle.

Runs in tools/pdf_extract/.venv (paddlex + pymupdf). Read-only against the
databank JSONL; writes ONLY repaired markdown files (originals backed up under
databank/_loop_repair/backup/) and a JSONL report. Booking the databank rows
is tools/ocr_loop_repair_book.py (root venv), driven by that report.

HOW A PAGE IS LOCALISED. extract_batch joins pages with ``\\n\\n---\\n\\n``;
40 of 40 sampled documents split into exactly their PDF page count, so a loop's
page is the count of separators before it. Documents whose split disagrees
with the PDF are not re-OCR'd — their loops are collapsed to the marker.

HOW A PAGE IS REPAIRED. The looped pages (grouped into consecutive windows)
are re-extracted with the canonical REGION pipeline at the tool's default
temperature into a SCRATCH databank (no figure crops or part files touch the
real one). A new page is accepted when it carries no loop and recalls at least
``--min-similarity`` of the old page's clean prose (the guard that it is the
same page, not garbage); a page that loops again is retried once, warmer, on
its own; what still loops is collapsed to the marker — new page if it passed
the similarity check, otherwise the old one. Figure references are the OLD
page's (a scratch re-OCR numbers crops from fig_01): same count → substituted
in order, otherwise the old tags are appended and the new ones dropped, so
nothing dangles and nothing is lost.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import ocr_loop_lib as L  # noqa: E402

DEFAULT_WORKSPACE = os.path.expanduser("~/corpora/ouroboros-spectra")


def read_databank(databank_dir: str) -> dict:
    recs: dict[str, dict] = {}
    for fn in ("papers.jsonl", "extraction.jsonl"):
        path = os.path.join(databank_dir, fn)
        if not os.path.exists(path):
            continue
        with open(path, "rb") as fh:
            for raw in fh:
                raw = raw.strip(b"\x00\r\n ")
                if not raw:
                    continue
                try:
                    d = json.loads(raw)
                except Exception:  # noqa: BLE001 — a torn tail is not our problem here
                    continue
                k = d.get("paper_key")
                if k:
                    recs.setdefault(k, {}).update(d)
    return recs


def _abs(workspace: str, p: str) -> str:
    return p if os.path.isabs(p) else os.path.join(workspace, p)


def build_worklist(recs: dict, workspace: str, keys: list[str] | None) -> list[dict]:
    items = []
    for key, r in recs.items():
        if keys and key not in keys:
            continue
        if r.get("record_kind") == "supplement":
            continue
        # retired translations count too: loops were part of why they retired
        if r.get("extraction_status") not in (
            "extracted",
            "extract_lingual",
            "translate_failed",
        ):
            continue
        md = r.get("md_path")
        if not md:
            continue
        md_abs = _abs(workspace, md)
        try:
            with open(md_abs, encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except OSError:
            continue
        pages = L.split_pages(text)
        hits = []
        cost_w = cost_c = 0
        for i, p in enumerate(pages):
            c = L.loop_cost(p)
            if c["spans"]:
                hits.append(i)
                cost_w += c["words"]
                cost_c += c["chars"]
        if not hits:
            continue
        pdf = r.get("pdf_path") or ""
        items.append(
            {
                "key": key,
                "md": md_abs,
                "pdf": _abs(workspace, pdf) if pdf else "",
                "pages": len(pages),
                "hits": hits,
                "cost": {"words": cost_w, "chars": cost_c},
                "pending_translation": r.get("extraction_status") == "extract_lingual"
                and not r.get("translated"),
                "language": r.get("language"),
            }
        )
    # the translation queue first (its loops cost seat time tonight), then the worst
    items.sort(
        key=lambda x: (
            not x["pending_translation"],
            -(x["cost"]["words"] + x["cost"]["chars"]),
            x["key"],
        )
    )
    return items


def _windows(hits: list[int]) -> list[tuple[int, int]]:
    """Consecutive page runs as half-open [a, b) windows."""
    out: list[tuple[int, int]] = []
    for i in hits:
        if out and out[-1][1] == i:
            out[-1] = (out[-1][0], i + 1)
        else:
            out.append((i, i + 1))
    return out


class Reocr:
    """One built pipe, many page windows, all into a scratch databank."""

    def __init__(self, args):
        import extract_batch as X

        self.X = X
        self.args = args
        self.scratch = args.scratch
        os.makedirs(self.scratch, exist_ok=True)
        ok, why = X._ensure_llmvp_model(args.llmvp_url, args.model)
        if not ok:
            raise SystemExit(f"LLMVP cannot serve {args.model}: {why}")
        self.pipe = X._build_pipe(
            "llmvp", args.model, 0, args.concurrency, args.llmvp_url
        )

    def window(
        self, pdf: str, key: str, a: int, b: int, temperature: float
    ) -> list[str]:
        rep = self.X.extract_paper(
            self.pipe,
            pdf,
            key,
            self.scratch,
            dpi=self.args.dpi,
            temperature=temperature,
            top_p=0.95,
            page_range=(a, b),
            text_mode="region",
            llmvp_url=self.args.llmvp_url,
            llmvp_model=self.args.model,
        )
        if rep.get("error"):
            raise RuntimeError(str(rep["error"])[:200])
        part = os.path.join(self.scratch, rep["md_path"])
        try:
            with open(part, encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        finally:
            try:
                os.remove(part)
            except OSError:
                pass
            shutil.rmtree(
                os.path.join(self.scratch, "figures", key), ignore_errors=True
            )
        pages = L.split_pages(text)
        if len(pages) != b - a:
            raise RuntimeError(
                f"re-OCR of pages [{a},{b}) came back as {len(pages)} page(s)"
            )
        return pages


def pdf_page_count(pdf: str) -> int:
    import fitz

    with fitz.open(pdf) as doc:
        return len(doc)


def repair_doc(item: dict, reocr: Reocr | None, args) -> dict:
    t0 = time.time()
    key = item["key"]
    row = {
        "key": key,
        "pdf": item["pdf"],
        "pages_total": item["pages"],
        "hits": item["hits"],
        "before": dict(item["cost"]),
        "pages": [],
        "status": "dry_run" if args.dry_run else "ok",
        "error": "",
    }
    with open(item["md"], encoding="utf-8", errors="replace") as fh:
        original = fh.read()
    pages = L.split_pages(original)
    old_imgs = [L.img_tags(p) for p in pages]

    has_pdf = bool(item["pdf"]) and os.path.exists(item["pdf"])
    can_reocr = has_pdf
    if has_pdf:
        try:
            n_pdf = pdf_page_count(item["pdf"])
        except Exception as exc:  # noqa: BLE001
            n_pdf, row["error"] = -1, f"pdf unreadable: {exc}"[:160]
        if n_pdf != len(pages):
            can_reocr = False
            row["error"] = (
                row["error"] or f"page split {len(pages)} != pdf pages {n_pdf}"
            )

    def collapse_old(i: int, why: str, sim=None):
        new, cost = L.collapse_loops(pages[i])
        pages[i] = new
        row["pages"].append(
            {
                "page": i,
                "method": "collapsed_old",
                "why": why,
                "sim": sim,
                "removed": cost,
            }
        )

    if not can_reocr or args.dry_run or reocr is None:
        for i in item["hits"]:
            if args.dry_run:
                row["pages"].append(
                    {
                        "page": i,
                        "method": "would_reocr" if can_reocr else "would_collapse",
                        "cost": L.loop_cost(pages[i]),
                    }
                )
            else:
                collapse_old(i, "no re-OCR path")
    else:
        for a, b in _windows(item["hits"]):
            try:
                fresh = reocr.window(item["pdf"], key, a, b, args.temperature)
            except Exception as exc:  # noqa: BLE001 — collapse this window, continue
                for i in range(a, b):
                    collapse_old(i, f"re-OCR failed: {exc}"[:160])
                continue
            for i in range(a, b):
                old, new = pages[i], fresh[i - a]
                method = "reocr"
                if L.find_loops(new):
                    try:
                        new2 = reocr.window(
                            item["pdf"], key, i, i + 1, args.retry_temperature
                        )[0]
                    except Exception:  # noqa: BLE001
                        new2 = ""
                    if new2 and not L.find_loops(new2):
                        new, method = new2, "reocr_retry"
                    else:
                        cand = (
                            new2
                            if new2
                            and L.loop_cost(new2)["words"] + L.loop_cost(new2)["chars"]
                            < L.loop_cost(new)["words"] + L.loop_cost(new)["chars"]
                            else new
                        )
                        new, _ = L.collapse_loops(cand)
                        method = "reocr_collapsed"
                sim = L.similarity(old, new)
                if not new.strip() or sim < args.min_similarity:
                    collapse_old(i, f"new page rejected (sim {sim:.2f})", round(sim, 3))
                    continue
                new, note = L.remap_imgs(new, old_imgs[i])
                pages[i] = new
                row["pages"].append(
                    {
                        "page": i,
                        "method": method,
                        "sim": round(sim, 3),
                        "imgs": note,
                        "old_len": len(old),
                        "new_len": len(new),
                    }
                )

    repaired = L.join_pages(pages)
    residual = L.loop_cost(repaired)
    if residual["spans"] and not args.dry_run:
        repaired, _ = L.collapse_loops(repaired)
        row["residual_collapsed"] = residual
    row["after"] = L.loop_cost(repaired)
    row["methods"] = {}
    for p in row["pages"]:
        row["methods"][p["method"]] = row["methods"].get(p["method"], 0) + 1
    if not args.dry_run and repaired != original:
        bdir = os.path.join(args.databank_dir, "_loop_repair", "backup")
        os.makedirs(bdir, exist_ok=True)
        backup = os.path.join(bdir, os.path.basename(item["md"]))
        if not os.path.exists(backup):
            shutil.copy2(item["md"], backup)
        tmp = item["md"] + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(repaired)
        os.replace(tmp, item["md"])
        row["backup"] = backup
        row["written"] = True
    row["seconds"] = round(time.time() - t0, 1)
    return row


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--workspace", default=DEFAULT_WORKSPACE)
    ap.add_argument("--keys", nargs="*", default=None)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--report", default="")
    ap.add_argument(
        "--resume", action="store_true", help="skip keys already in the report"
    )
    ap.add_argument("--scratch", default="")
    ap.add_argument("--concurrency", type=int, default=2)
    ap.add_argument("--dpi", type=int, default=160)
    # Greedy, the tool's own default since 2026-10-04 (extract_batch
    # --vl-temperature); the recognizer's guards retry warmer from it.
    ap.add_argument("--temperature", type=float, default=0.0)
    ap.add_argument("--retry-temperature", type=float, default=1.0)
    ap.add_argument("--min-similarity", type=float, default=0.25)
    ap.add_argument(
        "--llmvp-url",
        default=os.environ.get("OUROBOROS_LLMVP_URL", "http://127.0.0.1:8008"),
    )
    ap.add_argument(
        "--model", default=os.environ.get("OUROBOROS_LLMVP_VL_MODEL", "paddle-ocr-vl")
    )
    args = ap.parse_args()
    args.databank_dir = os.path.join(args.workspace, "databank")
    args.report = args.report or os.path.join(
        args.databank_dir, "_loop_repair", "report.jsonl"
    )
    args.scratch = args.scratch or os.path.join(
        args.databank_dir, "_loop_repair", "scratch"
    )
    os.makedirs(os.path.dirname(args.report), exist_ok=True)

    recs = read_databank(args.databank_dir)
    work = build_worklist(recs, args.workspace, args.keys)
    done = set()
    if args.resume and os.path.exists(args.report):
        for line in open(args.report, encoding="utf-8"):
            try:
                done.add(json.loads(line)["key"])
            except Exception:  # noqa: BLE001
                pass
    work = [w for w in work if w["key"] not in done]
    if args.limit:
        work = work[: args.limit]
    total_cost = sum(w["cost"]["words"] + w["cost"]["chars"] for w in work)
    print(
        f"[repair] {len(work)} document(s), {sum(len(w['hits']) for w in work)} looped page(s), "
        f"{total_cost} words+chars, pending-translation first: "
        f"{sum(1 for w in work if w['pending_translation'])}",
        flush=True,
    )
    reocr = None if args.dry_run else Reocr(args)
    with open(args.report, "a", encoding="utf-8") as rep:
        for n, item in enumerate(work, 1):
            try:
                row = repair_doc(item, reocr, args)
            except (
                Exception
            ) as exc:  # noqa: BLE001 — one document must not stop the pass
                row = {
                    "key": item["key"],
                    "status": "error",
                    "error": f"{type(exc).__name__}: {exc}"[:300],
                }
            rep.write(json.dumps(row, ensure_ascii=False) + "\n")
            rep.flush()
            m = row.get("methods", {})
            print(
                f"[repair] {n}/{len(work)} {item['key'][:56]:56s} pages {len(item['hits']):>3} "
                f"{row['status']:7s} {json.dumps(m) if m else row.get('error','')[:80]} "
                f"after={row.get('after',{}).get('words','?')}w/{row.get('after',{}).get('chars','?')}c {row.get('seconds','')}s",
                flush=True,
            )
    print("[repair] done", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
