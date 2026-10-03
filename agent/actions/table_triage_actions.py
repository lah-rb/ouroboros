"""The table-triage lane: correct OCR'd tables against the page before packing.

WHY (2026-10-03). PaddleOCR-VL reads table digits well but gets structure
wrong often and invisibly: in a random sample of 60 tables from accepted
papers, 30 put at least one value under the wrong header or row, or misread
a cell, and packs inherit those as misattributed values the grounding gate
cannot catch. The v2 method (agent/table_triage.py) gives muse the page image
and the OCR's table; on that fresh sample its gate applied 26 corrections,
20 better, 5 same and 1 worse -- a page the sampler had mislocated -- and the
rows with a wrong value among them fell from 125 to 8 (pre-registered bar
passed, dev/bench_table_triage.py).

HOW IT FITS -- the translation precedent ("packs must be English"):

  * An accepted paper whose doc has tables WAITS to be packed until its
    tables are triaged: _curation_pending holds it out (the curate and repack
    lanes both select through it), and a review that accepts one in-round
    books the review and defers the pack.
  * This lane takes exactly those papers. Per round it triages a bounded
    slice of one paper's tables, records every table in a sidecar
    (databank/table_triage/<key>.json), and when every table has an outcome
    books `table_triage_status` = done on the paper, which makes it
    pack-eligible again. A paper with no tables, or whose page map cannot be
    trusted, is booked at once (no_tables / skipped) -- the gate never
    strands a paper.
  * The pack reads the doc with the applied corrections overlaid
    (curation_actions._raw_curator_doc). The OCR markdown is never edited.
    The missed-window repair reads the doc WITHOUT the overlay, so it stays
    consistent with the windows already booked.

PAGE PROVENANCE IS EXACT. The OCR writes one markdown chunk per PDF page,
joined by "\\n\\n---\\n\\n" (tools/pdf_extract/extract_batch.py, and the book
segments the same way); the chunk count equalled the PDF's page count on 122
of 122 sampled accepted papers with tables. A table's page is its chunk. A
doc whose chunk count does not match its PDF is skipped rather than guessed
-- a guessed page was the bench's one "worse" correction.

Disabled with OUROBOROS_TABLE_TRIAGE=0 or by naming the lane in
OUROBOROS_DISABLE_LANES -- either way the pack gate opens with it, so turning
the lane off can never leave papers waiting.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
from datetime import datetime, timezone

from agent import table_triage as tt

logger = logging.getLogger(__name__)

LANE = "table_triage"
SIDECAR_DIR = "databank/table_triage"
#: Page renders live INSIDE the workspace: the server reads image paths only
#: under model.vision_image_roots.
SHOT_DIR = "databank/_table_triage"
RENDER_DPI = 130
PAGE_SEP = "\n\n---\n\n"
#: Statuses after which a paper's pack no longer waits on this lane.
TERMINAL = frozenset({"done", "no_tables", "skipped"})
#: Reads per table: an EMPTY answer is a shrunk budget eaten by thinking on a
#: busy server (7 of 60 fresh reads needed a retry, 2 stayed empty after one),
#: never a verdict.
MAX_READS = 3
#: Degenerate or failed reads after which a table keeps its OCR form.
MAX_TABLE_ERRORS = 2
MAX_TOKENS = 16384
_TABLE_RE = re.compile(r"<table\b.*?</table>", re.S | re.I)

_TRIAGE_CLAIMS: set[str] = set()


def table_triage_enabled() -> bool:
    if os.environ.get("OUROBOROS_TABLE_TRIAGE", "1").strip() == "0":
        return False
    off = {
        x.strip()
        for x in os.environ.get("OUROBOROS_DISABLE_LANES", "").split(",")
        if x.strip()
    }
    return LANE not in off


def awaiting_table_triage(rec: dict) -> bool:
    """Does this paper's pack wait on the lane? Accepted, owed a pack, not a
    supplement, and no terminal table-triage status yet."""
    return (
        table_triage_enabled()
        and rec.get("record_kind") != "supplement"
        and rec.get("review_status") == "accepted"
        and rec.get("pack_status") not in ("packed", "pack_failed")
        and rec.get("table_triage_status") not in TERMINAL
    )


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def doc_tables(md: str) -> list[dict]:
    """Every table of the doc, in order: {index, page, html, sha}."""
    out: list[dict] = []
    for p, page in enumerate(md.split(PAGE_SEP)):
        for m in _TABLE_RE.finditer(page):
            out.append(
                {
                    "index": len(out),
                    "page": p + 1,
                    "html": m.group(0),
                    "sha": _sha(m.group(0)),
                }
            )
    return out


def apply_corrections(md: str, sidecar: dict | None) -> str:
    """The doc with every APPLIED correction in place of its OCR table.

    A correction replaces the table at its index only while that table still
    has the sha it was triaged from; a doc that changed since keeps its
    tables (the lane re-triages it)."""
    entries = {
        int(e["index"]): e
        for e in (sidecar or {}).get("tables") or []
        if isinstance(e, dict) and e.get("apply") and e.get("html")
    }
    if not entries:
        return md
    pos = [0]

    def _sub(m: re.Match) -> str:
        i = pos[0]
        pos[0] += 1
        e = entries.get(i)
        if e and e.get("sha") == _sha(m.group(0)):
            return e["html"]
        return m.group(0)

    return _TABLE_RE.sub(_sub, md)


def _md_name(paper_key: str, has_en: bool) -> str:
    return f"databank/markdown/{paper_key}{'.en' if has_en else ''}.md"


async def pack_markdown(effects, paper_key: str) -> tuple[str, str]:
    """(path, text) of the markdown the pack reads: the gated English
    translation when one exists, else the OCR markdown."""
    for has_en in (True, False):
        path = _md_name(paper_key, has_en)
        fc = await effects.read_file(path)
        if getattr(fc, "exists", False):
            return path, fc.content
    return "", ""


async def load_sidecar(effects, paper_key: str) -> dict:
    fc = await effects.read_file(f"{SIDECAR_DIR}/{paper_key}.json")
    if not getattr(fc, "exists", False) or not fc.content.strip():
        return {}
    try:
        data = json.loads(fc.content)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


async def _save_sidecar(effects, paper_key: str, sidecar: dict) -> None:
    await effects.write_file(
        f"{SIDECAR_DIR}/{paper_key}.json",
        json.dumps(sidecar, indent=1, ensure_ascii=False),
    )


def _pdf_pages(pdf_abs: str) -> int:
    import pymupdf

    try:
        with pymupdf.open(pdf_abs) as doc:
            return doc.page_count
    except Exception:  # noqa: BLE001 -- an unreadable PDF has no page map
        return -1


def _render_page(pdf_abs: str, page: int, out_abs: str) -> bool:
    import pymupdf

    try:
        os.makedirs(os.path.dirname(out_abs), exist_ok=True)
        with pymupdf.open(pdf_abs) as doc:
            doc[page - 1].get_pixmap(dpi=RENDER_DPI).save(out_abs)
        return True
    except Exception:  # noqa: BLE001
        return False


def _final(entry: dict | None) -> bool:
    """Does this table have an outcome (a verdict, or its errors used up)?"""
    if not entry:
        return False
    if entry.get("verdict") in ("ok", "fixed", "unfixable", "unparsed"):
        return True
    return int(entry.get("errors") or 0) >= MAX_TABLE_ERRORS


#: Tables per paper, cached for the process life by the doc's size: the
#: selector ranks every waiting paper each round, and re-reading 160 docs to
#: count them would cost more than the round.
_TABLE_COUNTS: dict[str, tuple[int, int]] = {}


async def _table_count(effects, paper_key: str) -> int:
    path, md = await pack_markdown(effects, paper_key)
    cached = _TABLE_COUNTS.get(paper_key)
    if cached and cached[0] == len(md):
        return cached[1]
    n = len(doc_tables(md))
    _TABLE_COUNTS[paper_key] = (len(md), n)
    return n


async def select_paper(effects, databank: dict) -> str:
    """Claim the next paper waiting on the lane: FEWEST TABLES FIRST, so most
    held packs are released early -- the backlog the day the lane opened was
    3,502 tables in 161 papers, most of them in a few long theses -- then
    fresh acceptances before owed repacks."""
    ranked: list[tuple[int, int, str]] = []
    for key, rec in databank.items():
        if key in _TRIAGE_CLAIMS or not awaiting_table_triage(rec):
            continue
        n = await _table_count(effects, key)
        ranked.append((n, 0 if not rec.get("pack_status") else 1, key))
    for _, _, key in sorted(ranked):
        if key not in _TRIAGE_CLAIMS:
            _TRIAGE_CLAIMS.add(key)
            return key
    return ""


async def _book(effects, paper_key: str, status: str, summary: dict) -> None:
    """The paper's terminal status, as a FULL merged record (last row wins)."""
    from agent.actions.scholarly_actions import append_records, read_databank

    rec = dict((await read_databank(effects)).get(paper_key) or {})
    if not rec:
        return
    rec["table_triage_status"] = status
    rec["table_triage"] = summary
    await append_records(effects, [rec])


async def triage_paper(effects, paper_key: str, rec: dict, budget: int) -> dict:
    """One round on one paper: up to `budget` tables read; book the paper
    when every table has an outcome. Returns a summary dict."""
    stamp = datetime.now(timezone.utc).isoformat()
    model = ""
    try:
        from agent.actions.curation_actions import _provenance_model

        model = _provenance_model(effects)
    except Exception:  # noqa: BLE001 -- provenance is a label, not a gate
        pass
    md_path, md = await pack_markdown(effects, paper_key)
    tables = doc_tables(md)
    base = {"paper_key": paper_key, "tables": len(tables)}
    if not tables:
        await _book(effects, paper_key, "no_tables", {"tables": 0, "at": stamp})
        return {**base, "outcome": "no tables"}

    root = getattr(effects, "working_directory", "") or ""
    pdf_rel = str(rec.get("pdf_path") or "")
    pdf_abs = pdf_rel if os.path.isabs(pdf_rel) else os.path.join(root, pdf_rel)
    n_pages = md.count(PAGE_SEP) + 1
    pdf_pages = _pdf_pages(pdf_abs) if pdf_rel else -1
    if pdf_pages != n_pages:
        why = f"page map: {n_pages} markdown pages vs {pdf_pages} PDF pages"
        await _book(
            effects,
            paper_key,
            "skipped",
            {"tables": len(tables), "reason": why, "at": stamp},
        )
        return {**base, "outcome": f"skipped ({why})"}

    doc_sha = _sha(md)
    sidecar = await load_sidecar(effects, paper_key)
    if sidecar.get("doc_sha") != doc_sha:
        sidecar = {
            "paper_key": paper_key,
            "doc": md_path,
            "doc_sha": doc_sha,
            "tables": [],
        }
    by_index = {int(e["index"]): e for e in sidecar.get("tables") or []}

    todo = [t for t in tables if not _final(by_index.get(t["index"]))][:budget]
    read = 0
    renders: set[str] = set()
    for t in todo:
        entry = by_index.get(t["index"]) or {
            "index": t["index"],
            "page": t["page"],
            "sha": t["sha"],
        }
        out_rel = f"{SHOT_DIR}/{paper_key[:80].replace('/', '_')}_p{t['page']}.png"
        out_abs = os.path.join(root, out_rel)
        renders.add(out_abs)
        if not _render_page(pdf_abs, t["page"], out_abs):
            entry.update(verdict="unparsed", error="page would not render", at=stamp)
            by_index[t["index"]] = entry
            continue
        prompt = tt.TRIAGE_PROMPT.format(table=tt.slim(t["html"]))
        text, err = "", ""
        t0 = time.monotonic()
        for _ in range(MAX_READS):
            res = await effects.run_vision(
                prompt, out_abs, max_tokens=MAX_TOKENS, reasoning=tt.TRIAGE_REASONING
            )
            err = str(getattr(res, "error", None) or "")
            text = str(getattr(res, "text", "") or "")
            if err or text.strip():
                break
        read += 1
        if err:
            from agent.actions.curation_actions import _is_degenerate_fault

            if not _is_degenerate_fault(err):
                # Transport: nothing about the table is known. End the round;
                # the table is read again next time, nothing spent.
                logger.warning(
                    "table triage: transport fault on %s: %s", paper_key, err[:160]
                )
                break
            entry.update(
                errors=int(entry.get("errors") or 0) + 1, error=err[:200], at=stamp
            )
            by_index[t["index"]] = entry
            continue
        d = tt.parse_triage(text)
        g = tt.gate(d, t["html"])
        entry.update(
            verdict=d["verdict"],
            problems=d["problems"][:12],
            apply=g["apply"],
            reason=g["reason"],
            numbers=(g["numbers"] or {}).get("kind"),
            html=d["html"] if g["apply"] else "",
            model=model,
            at=stamp,
            seconds=round(time.monotonic() - t0),
        )
        entry.pop("error", None)
        by_index[t["index"]] = entry
    for path in renders:
        try:
            os.remove(path)
        except OSError:
            pass

    sidecar["tables"] = [by_index[i] for i in sorted(by_index)]
    await _save_sidecar(effects, paper_key, sidecar)
    done = all(_final(by_index.get(t["index"])) for t in tables)
    corrected = sum(1 for e in sidecar["tables"] if e.get("apply"))
    if done:
        await _book(
            effects,
            paper_key,
            "done",
            {
                "tables": len(tables),
                "corrected": corrected,
                "model": model,
                "at": stamp,
            },
        )
    return {
        **base,
        "read": read,
        "corrected": corrected,
        "outcome": (
            "done"
            if done
            else f"{sum(_final(by_index.get(t['index'])) for t in tables)}/{len(tables)} tables"
        ),
    }


def _budget() -> int:
    raw = os.environ.get("OUROBOROS_TABLE_TRIAGE_TABLES", "").strip()
    try:
        return max(1, int(raw)) if raw else 4
    except ValueError:
        return 4


async def action_table_triage_drain_batch(step_input):
    """Claim one paper waiting on table triage and read a bounded slice of its
    tables against their pages. Books only sidecar entries and, when the
    paper is complete, its terminal status. Inputs: working_directory."""
    from agent.actions.scholarly_actions import read_databank
    from agent.models import StepOutput

    effects = step_input.effects

    def _out(outcomes: list, reason: str = "") -> StepOutput:
        summary = {"attempted": len(outcomes), "outcomes": outcomes}
        if reason:
            summary["reason"] = reason
        obs = (
            "table triage: "
            + "; ".join(f"{o['paper_key']} → {o['outcome']}" for o in outcomes)
            if outcomes
            else f"table triage idle ({reason})"
        )
        return StepOutput(
            result=summary,
            observations=obs,
            context_updates={"table_triage_summary": summary},
        )

    if effects is None:
        return _out([], "no effects")
    if not table_triage_enabled():
        return _out([], "disabled")
    databank = await read_databank(effects)
    key = await select_paper(effects, databank)
    if not key:
        return _out([], "no paper waiting on table triage")
    try:
        res = await triage_paper(effects, key, databank.get(key) or {}, _budget())
    except Exception:  # noqa: BLE001 -- a code fault must not book anything
        logger.exception("table triage errored on %s", key)
        return _out([], "internal error (see log)")
    finally:
        _TRIAGE_CLAIMS.discard(key)
    logger.info("table triage: %s -> %s", key, res.get("outcome"))
    return _out([res])
