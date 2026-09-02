#!/usr/bin/env python3
"""Front-matter triage of the oversize-parked curate pool. One-shot.

WHY. 222 papers are parked `curate_oversize`: their deepest-compression doc
floor exceeds the local 65k seat. A 256k seat (qwen3-next on the mac) fits
210 of them, but a full review costs 3-16 minutes EACH at that length, and
the 2026-09-02 bench showed the pool is mostly material to deny: six of eight
sampled were a mission overview, a multi-field proceedings volume, a clinical
guideline, a data compilation, and two out-of-scope theses -- every one
recognisable from its title page, abstract and table of contents. Reading
the front matter first (~2-12k tokens, ~30 s) and denying only the
unmistakable cases cuts the full-review bill more than any other lever.

WHAT IT DOES. For each parked paper: catalogue record + opening pages
(title page, abstract, TOC where present) -> triage prompt on the resident
model -> "proceed" | "deny". With --apply:
  deny    -> papers.jsonl: review_status=denied, the production deny
             categories, a summary that names the triage (FULL merged row).
  proceed -> (only with --unpark) extraction.jsonl: extraction_status back
             to "extracted" so the curate drain -- the remote lane with the
             large seat -- reviews and packs it through the production path.
Doubt means proceed: the triage never accepts anything, it only removes what
cannot serve the corpus.

CALIBRATION. --keys with the eight bench papers first: the two the full
review ACCEPTED (Mastcam Mars spectra; the tissue Raman thesis) must come
back "proceed". A triage that denies a known accept is not saving time, it
is losing papers.

    python dev/triage_parked_frontmatter.py --keys K1 K2 ...     # dry, subset
    python dev/triage_parked_frontmatter.py                     # dry, all
    python dev/triage_parked_frontmatter.py --apply             # book denials
    python dev/triage_parked_frontmatter.py --apply --unpark    # + un-park
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import time
from datetime import datetime, timezone

import httpx

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.actions.scholarly_actions import (  # noqa: E402
    append_extraction_records,
    append_records,
    read_databank,
)
from agent.effects.local import LocalEffects  # noqa: E402
from agent.llm_json import parse_llm_json  # noqa: E402

ROOT = os.path.expanduser("~/corpora/ouroboros-spectra")
PAGE_SEP = "\n\n---\n\n"  # extract_batch joins pages with this

# Excerpt policy, measured on the pool: extraction pages of these scanned
# documents are short (first three pages median 1.9k chars), so a page count
# alone under-reads. Take pages until ~12k chars, never fewer than three,
# then any TOC/abstract page in the first fifteen, capped at 40k chars.
MIN_PAGES = 3
TARGET_CHARS = 12_000
SCAN_PAGES = 15
CAP_CHARS = 40_000

ABSTRACT_WORDS = (
    "abstract",
    "resumen",
    "resumo",
    "résumé",
    "zusammenfassung",
    "аннотация",
    "要旨",
    "摘要",
    "summary",
)
TOC_WORDS = (
    "contents",
    "table of contents",
    "índice",
    "indice",
    "sommaire",
    "оглавление",
    "目次",
)

COMPLETE = """mutation($r:CompletionRequest!){
  createCompletion(request:$r){ text promptTokens generatedTokens truncated finished }
}"""
MODELS = "{ models { name active } }"


def _clean(text: str) -> str:
    text = re.sub(r"<img[^>]*>", "", text)
    text = re.sub(r"\*?\[figure removed by extraction filter\]\*?", "", text)
    text = re.sub(r"<div[^>]*>|</div>", "", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _is_toc(page: str) -> bool:
    low = page.lower()[:400]
    if any(w in low for w in TOC_WORDS):
        return True
    lines = [ln for ln in page.splitlines() if ln.strip()]
    if len(lines) < 6:
        return False
    numbered = sum(1 for ln in lines if re.search(r"(\.{3,}|\s)\d{1,3}\s*$", ln))
    return numbered / len(lines) >= 0.4


def _has_abstract(page: str) -> bool:
    low = page.lower()[:600]
    return any(w in low for w in ABSTRACT_WORDS)


def front_matter(md: str) -> tuple[str, list[int]]:
    """The opening of the document, and which 1-based pages were taken."""
    pages = md.split(PAGE_SEP)
    taken: list[int] = []
    out: list[str] = []
    chars = 0
    for i, pg in enumerate(pages):
        body = _clean(pg)
        if not body:
            continue
        want = i < MIN_PAGES or chars < TARGET_CHARS
        if not want and i < SCAN_PAGES and (_is_toc(body) or _has_abstract(body)):
            want = True
        if not want:
            if i >= SCAN_PAGES:
                break
            continue
        if chars + len(body) > CAP_CHARS:
            body = body[: max(0, CAP_CHARS - chars)]
        out.append(f"[page {i + 1}]\n{body}")
        taken.append(i + 1)
        chars += len(body)
        if chars >= CAP_CHARS:
            break
    return "\n\n".join(out), taken


def catalogue_header(rec: dict) -> str:
    authors = rec.get("authors") or []
    if isinstance(authors, list):
        authors = "; ".join(str(a) for a in authors[:6]) + (
            " ..." if len(authors) > 6 else ""
        )
    lines = [
        "CATALOGUE RECORD",
        f"title: {rec.get('title') or ''}",
        f"authors: {authors or ''}",
        f"venue: {rec.get('venue') or ''}",
        f"year: {rec.get('year') or ''}",
        f"language: {rec.get('language') or ''}",
        f"doi: {rec.get('doi') or ''}",
        f"discovered under aspects: {', '.join(rec.get('source_aspects') or [])}",
    ]
    abstract = str(rec.get("abstract") or "").strip()
    if abstract:
        lines.append(f"catalogue abstract: {abstract[:2000]}")
    return "\n".join(ln for ln in lines if not ln.endswith(": ") and ln.strip())


def corpus_subject() -> str:
    try:
        m = json.load(open(f"{ROOT}/.agent/mission.json"))
        return str(m.get("objective") or "").strip()
    except Exception:
        return ""


def triage_prompt(subject: str, header: str, excerpt: str) -> str:
    return f"""{header}

OPENING PAGES OF THE DOCUMENT (title page, abstract and table of contents where present -- NOT the full text):

{excerpt}

---

THE CORPUS YOU ARE CURATING FOR:
{subject}

You are a sceptical research-data curator doing a FRONT-MATTER TRIAGE. You have
seen only the opening of a long document. A full review of it costs an hour of
model time, so your job is to spend that hour only where it can pay.

Decide:
- "proceed" -- this could be a paper, thesis or report that reports MEASURED
  spectra or diffraction data (LIBS / atomic emission, Raman, FTIR, XRD, XRF,
  UV-Vis-NIR reflectance, and kin) on named material systems that serve the
  corpus above -- OR you cannot tell from the front matter. Doubt means
  proceed: the full review decides, and it can still deny.
- "deny" -- the front matter makes it UNMISTAKABLE that the document cannot
  serve this corpus: an unrelated discipline; a multi-field conference
  proceedings volume; a clinical, policy or regulatory guideline; a mission,
  engineering or programme report; a data compilation or handbook with no
  measured spectra; a review with no new measurements. Quote the evidence.

Set "document_type" to one of: paper | thesis | report | proceedings | book |
guideline | review | other. When denying, set "deny_category" to "corpus_fit"
or "no_usable_data". Give a one-sentence "summary" of what the document is.

Return ONLY a JSON object inside a fenced code block, for example:
```json
{{"verdict": "deny", "deny_category": "corpus_fit", "document_type": "proceedings",
 "summary": "Multi-field university conference proceedings (economics, law, linguistics); the TOC lists no spectroscopy.",
 "evidence": "TOC pages 3-6: sections on pedagogy, translation studies, regional history"}}
```
```json
{{"verdict": "proceed", "document_type": "paper", "summary": "Multispectral reflectance of Martian rocks and soils from Mastcam; abstract reports spectral classes and band parameters.", "evidence": "abstract, key points"}}
```
"verdict" MUST be exactly "proceed" or "deny". Return ONLY the fenced JSON."""


async def ask(
    client: httpx.AsyncClient, url: str, prompt: str, temp: float
) -> tuple[dict, float]:
    t0 = time.time()
    r = await client.post(
        url,
        json={
            "query": COMPLETE,
            "variables": {
                "r": {"prompt": prompt, "maxTokens": 700, "temperature": temp}
            },
        },
        timeout=1800.0,
    )
    r.raise_for_status()
    d = r.json()
    if d.get("errors"):
        raise RuntimeError(json.dumps(d["errors"])[:300])
    return d["data"]["createCompletion"], time.time() - t0


async def active_model(client: httpx.AsyncClient, url: str) -> str:
    r = await client.post(url, json={"query": MODELS}, timeout=30.0)
    r.raise_for_status()
    names = [
        m["name"]
        for m in (r.json().get("data") or {}).get("models", [])
        if m.get("active")
    ]
    return names[0] if names else "unknown"


async def main_async(a) -> int:
    fx = LocalEffects(a.root)
    bank = await read_databank(fx)
    parked = {
        k: r for k, r in bank.items() if r.get("extraction_status") == "curate_oversize"
    }
    if a.keys:
        missing = [k for k in a.keys if k not in bank]
        if missing:
            print("unknown keys:", missing)
            return 2
        todo = {k: bank[k] for k in a.keys}
    else:
        todo = dict(sorted(parked.items()))
        if a.limit:
            todo = dict(list(todo.items())[: a.limit])
    subject = corpus_subject()
    assert subject, "mission objective missing -- the corpus section would render empty"

    rows: list[dict] = []
    async with httpx.AsyncClient() as client:
        model = await active_model(client, a.url)
        print(
            f"resident model: {model}   parked pool: {len(parked)}   to triage: {len(todo)}\n"
        )
        for i, (key, rec) in enumerate(todo.items(), 1):
            md_p = os.path.join(
                a.root, rec.get("md_path") or f"databank/markdown/{key}.md"
            )
            if not os.path.exists(md_p):
                rows.append(dict(key=key, verdict="error", detail="markdown missing"))
                print(f"  [{i:>3}] {key[:52]:<54} MARKDOWN MISSING")
                continue
            md = open(md_p, encoding="utf-8", errors="ignore").read()
            excerpt, taken = front_matter(md)
            prompt = triage_prompt(subject, catalogue_header(rec), excerpt)
            try:
                c, secs = await ask(client, a.url, prompt, a.temperature)
            except Exception as e:  # noqa: BLE001
                rows.append(dict(key=key, verdict="error", detail=str(e)[:300]))
                print(f"  [{i:>3}] {key[:52]:<54} TRANSPORT {str(e)[:80]}")
                continue
            parsed = parse_llm_json(c.get("text") or "")
            parsed = parsed if isinstance(parsed, dict) else {}
            v = str(parsed.get("verdict") or "").strip().lower()
            verdict = v if v in ("proceed", "deny") else "unparseable"
            if verdict == "unparseable":
                verdict = "proceed"  # an unreadable triage never denies
                parsed = {
                    "summary": "(triage output unparseable; proceeding)",
                    "document_type": "other",
                }
            row = dict(
                key=key,
                verdict=verdict,
                deny_category=str(parsed.get("deny_category") or ""),
                document_type=str(parsed.get("document_type") or ""),
                summary=str(parsed.get("summary") or "")[:600],
                evidence=str(parsed.get("evidence") or "")[:400],
                pages_taken=taken,
                excerpt_chars=len(excerpt),
                prompt_tokens=c.get("promptTokens"),
                generated=c.get("generatedTokens"),
                seconds=round(secs, 1),
                model=model,
                title=str(rec.get("title") or "")[:120],
            )
            rows.append(row)
            print(
                f"  [{i:>3}] {key[:52]:<54} {verdict:<8} {row['document_type']:<11} "
                f"{secs:>5.0f}s {c.get('promptTokens') or 0:>6} tok  | {row['summary'][:70]}",
                flush=True,
            )
            json.dump(rows, open(a.out, "w"), indent=1, ensure_ascii=False)

    json.dump(rows, open(a.out, "w"), indent=1, ensure_ascii=False)
    deny = [r for r in rows if r["verdict"] == "deny"]
    proceed = [r for r in rows if r["verdict"] == "proceed"]
    errs = [r for r in rows if r["verdict"] == "error"]
    secs = sorted(r["seconds"] for r in rows if r.get("seconds"))
    print(
        f"\ntriaged {len(rows)}: deny {len(deny)}  proceed {len(proceed)}  error {len(errs)}"
        + (f"  median {secs[len(secs)//2]:.0f}s" if secs else "")
    )
    types: dict[str, int] = {}
    for r in deny:
        types[r["document_type"]] = types.get(r["document_type"], 0) + 1
    if deny:
        print(
            "denied by document_type:",
            dict(sorted(types.items(), key=lambda kv: -kv[1])),
        )
    print(f"wrote {a.out}")

    if not a.apply:
        print(
            "\n(dry run: nothing booked; --apply books denials, --apply --unpark also un-parks)"
        )
        return 0

    now = datetime.now(timezone.utc).isoformat()
    deny_rows = []
    for r in deny:
        merged = dict(bank[r["key"]])  # FULL record -- append is last-row-replaces
        merged["paper_key"] = r["key"]
        merged["review_status"] = "denied"
        merged["review_summary"] = f"front-matter triage ({r['model']}): {r['summary']}"
        merged["review_issues"] = [f"document_type: {r['document_type']}"] + (
            [f"evidence: {r['evidence']}"] if r.get("evidence") else []
        )
        merged["deny_category"] = r["deny_category"] or "corpus_fit"
        merged["review_doc_form"] = "frontmatter"
        merged["review_model"] = r["model"]
        merged["reviewed_at"] = now
        deny_rows.append(merged)
    if deny_rows:
        await append_records(fx, deny_rows)
        print(f"booked {len(deny_rows)} denials to papers.jsonl")

    if a.unpark:
        up_rows = []
        for r in proceed:
            merged = dict(bank[r["key"]])
            merged["paper_key"] = r["key"]
            merged["extraction_status"] = "extracted"
            merged["failure_reason"] = ""
            merged["updated_at"] = now
            up_rows.append(merged)
        if up_rows:
            await append_extraction_records(fx, up_rows)
            print(
                f"un-parked {len(up_rows)} papers in extraction.jsonl (extraction_status=extracted)"
            )
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=ROOT)
    ap.add_argument("--url", default="http://192.168.1.209:8008/graphql")
    ap.add_argument("--keys", nargs="*", default=None)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--temperature", type=float, default=0.28)
    ap.add_argument("--out", default=os.path.expanduser("~/tmp/triage_parked.json"))
    ap.add_argument("--apply", action="store_true", help="book denials")
    ap.add_argument(
        "--unpark", action="store_true", help="with --apply: un-park the proceeds"
    )
    return asyncio.run(main_async(ap.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
