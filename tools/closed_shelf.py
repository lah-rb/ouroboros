#!/usr/bin/env python3
"""The CLOSED SHELF: owned copies of books that are not openly licensed.

Operator ruling 2026-10-01: scans of books the operator owns (not public
domain, not open access) enter the binder WITHOUT curation, and everything
derived from them is kept apart from the open corpus so it is never published
or redistributed and can be left out of a fully open model's training.

SEPARATION IS PHYSICAL, NOT A FLAG. The shelf is its own workspace root
(default ~/corpora/ouroboros-closed, env OUROBOROS_CLOSED_ROOT) with the open
workspace's layout: pdfs/, databank/{papers,extraction}.jsonl, markdown/,
figures/, figtext/, dataset/ (packs + its own key_registry.json). Nothing here
is written into the open workspace, so the open databank, every export of it,
and every training build that does not name this root are free of
closed-derived data by construction -- the open corpus builders glob every
dataset/*.json without consulting records, which a flag could not protect.
The root carries a CLOSED_MATERIAL marker; the training builder reads it only
when passed --closed-root, and labels what it reads distribution=closed.

MODELS ARE LOCAL ONLY (operator ruling): OCR on the 3060 box's paddle, figure
reading and packing on this machine's LLMVP. Book text is never sent to a
cloud API.

    tools/closed_shelf.py init
    tools/closed_shelf.py ingest ~/olmoBooks/*.pdf
    tools/closed_shelf.py ocr [--keys K ...] [--pages 40]      # resumable
    tools/closed_shelf.py figtext [--keys K ...] [--max-figures N]
    tools/closed_shelf.py pack [--keys K ...]                  # resumable per window
    tools/closed_shelf.py status

Records: record_kind=book, binder=True, distribution=closed,
license="all-rights-reserved (owned copy)", access_status=closed_owned,
review_status=accepted with curation_method=closed_preapproved (curation is
skipped: the binder is a hand-assembled set), pdf_path=pdfs/<key>.pdf.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as _dt
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

OPEN_ROOT = Path(os.path.expanduser("~/corpora/ouroboros-spectra"))
CLOSED_ROOT = Path(
    os.path.expanduser(
        os.environ.get("OUROBOROS_CLOSED_ROOT", "~/corpora/ouroboros-closed")
    )
)
MARKER = "CLOSED_MATERIAL"
LICENSE = "all-rights-reserved (owned copy)"
PREAPPROVAL = (
    "Preapproved closed-shelf book (operator ruling 2026-10-01): an owned copy of "
    "a book that is not openly licensed, entered into the binder without "
    "curation. Derived data stays in the closed workspace and is excluded from "
    "open builds and from any published artefact."
)
TOOL_PY = REPO / "tools" / "pdf_extract" / ".venv" / "bin" / "python"
TOOL_SCRIPT = REPO / "tools" / "pdf_extract" / "extract_batch.py"
MARKER_TEXT = f"""# CLOSED MATERIAL — do not publish, redistribute, or train an open model on this

This workspace holds owned copies of books that are NOT openly licensed, and
everything derived from them (OCR markdown, figure readings, packs, registry).
Created by tools/closed_shelf.py (operator ruling 2026-10-01).

- Never copy files from here into the open workspace ({OPEN_ROOT}).
- Never upload, publish, or share anything under this directory.
- Training builds include it ONLY when given --closed-root explicitly, and
  mark every document from it distribution=closed.
"""


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


def _effects(root: Path):
    from agent.effects.local import LocalEffects

    return LocalEffects(str(root))


def _require_shelf(root: Path) -> None:
    if not (root / MARKER).is_file():
        raise SystemExit(
            f"{root} is not a closed shelf (no {MARKER}); run `init` first"
        )


def _words(stem: str) -> list[str]:
    """camelCase / spaced filename -> words: "Introduction9ECallister" ->
    Introduction, 9E, Callister; "XRay" -> X, Ray."""
    s = re.sub(r"(?<=[a-z])(?=[A-Z0-9])", " ", stem)
    s = re.sub(r"(?<=[A-Z])(?=[A-Z][a-z])", " ", s)
    return re.sub(r"[^A-Za-z0-9]+", " ", s).split()


def _slug(stem: str) -> str:
    s = "_".join(w.lower() for w in _words(stem))
    return s if len(s) <= 72 else s[:72].rsplit("_", 1)[0]


def _title_from_stem(stem: str) -> str:
    return " ".join(
        w if any(c.isdigit() for c in w) or w.isupper() else w.capitalize()
        for w in _words(stem)
    )


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _pdf_pages(path: Path) -> int:
    try:
        out = subprocess.run(
            ["pdfinfo", str(path)], capture_output=True, text=True, timeout=60
        )
        m = re.search(r"^Pages:\s+(\d+)", out.stdout, re.M)
        return int(m.group(1)) if m else 0
    except Exception:  # noqa: BLE001
        return 0


# ── init / ingest ─────────────────────────────────────────────────────


def cmd_init(root: Path) -> None:
    if (
        root.resolve() == OPEN_ROOT.resolve()
        or OPEN_ROOT.resolve() in root.resolve().parents
    ):
        raise SystemExit("the closed shelf must not live inside the open workspace")
    for d in (
        "pdfs",
        "databank/markdown",
        "databank/figures",
        "databank/figtext",
        "databank/dataset",
        "databank/pack_progress",
        "locks",
    ):
        (root / d).mkdir(parents=True, exist_ok=True)
    (root / MARKER).write_text(MARKER_TEXT, encoding="utf-8")
    reg = root / "databank" / "dataset" / "key_registry.json"
    src = OPEN_ROOT / "databank" / "dataset" / "key_registry.json"
    if not reg.exists() and src.exists():
        # Vocabulary to REUSE, copied once: keys the closed packs coin fold into
        # THIS copy only, so the open registry never carries closed-derived keys.
        shutil.copy2(src, reg)
    print(
        f"closed shelf ready at {root} (marker {MARKER}, registry {'seeded' if reg.exists() else 'empty'})"
    )


def book_record(
    pdf: Path,
    root: Path,
    *,
    title: str = "",
    authors: list[str] | None = None,
    key: str = "",
) -> dict:
    key = key or "book_" + _slug(pdf.stem)
    return {
        "paper_key": key,
        "title": title or _title_from_stem(pdf.stem),
        "authors": authors or [],
        "year": 0,
        "doi": "",
        "identifier": "",
        "identifier_kind": "none",
        "metadata_note": "title from the filename; verify against the title page",
        "record_kind": "book",
        "binder": True,
        "distribution": "closed",
        "license": LICENSE,
        "access_status": "closed_owned",
        "retrieval_method": "operator_owned_copy",
        "status": "acquired",
        "pdf_path": f"pdfs/{key}.pdf",
        "source_file": pdf.name,
        "sha256": _sha256(pdf),
        "pages": _pdf_pages(pdf),
        "review_status": "accepted",
        "review_summary": PREAPPROVAL,
        "review_issues": [],
        "review_document_form": "book",
        "deny_category": "",
        "curation_method": "closed_preapproved",
        "ingested_at": _now(),
        "updated_at": _now(),
    }


async def cmd_ingest(root: Path, pdfs: list[Path]) -> None:
    from agent.actions.scholarly_actions import append_records, read_databank

    _require_shelf(root)
    fx = _effects(root)
    bank = await read_databank(fx)
    have = {r.get("sha256") for r in bank.values()}
    rows = []
    for pdf in pdfs:
        if not pdf.is_file() or pdf.suffix.lower() != ".pdf":
            print(f"SKIP {pdf}: not a PDF file")
            continue
        rec = book_record(pdf, root)
        if rec["sha256"] in have or rec["paper_key"] in bank:
            print(f"SKIP {pdf.name}: already on the shelf as {rec['paper_key']}")
            continue
        dest = root / rec["pdf_path"]
        shutil.copy2(pdf, dest)
        rows.append(rec)
        print(f"NEW  {rec['paper_key']}  ({rec['pages']} pages)  <- {pdf.name}")
    if rows:
        await append_records(fx, rows)
    print(f"booked {len(rows)} book(s) on the closed shelf")


# ── OCR (segmented, resumable) ────────────────────────────────────────


def _ocr_route(args) -> list[str]:
    """argv routing the OCR tool to the paddle the open mission uses (the 3060)."""
    endpoint, model, layout = args.endpoint, args.model, args.layout_url
    if not endpoint:
        try:
            cfg = json.load(open(OPEN_ROOT / ".agent" / "mission.json"))["config"]
            route = (cfg.get("llmvp_domains") or {}).get("ocr") or {}
            endpoint = route.get("endpoint", "")
            model = model or route.get("model", "")
            layout = layout or route.get("layout_endpoint", "")
        except Exception:  # noqa: BLE001
            pass
    if not endpoint:
        raise SystemExit(
            "no OCR route: pass --endpoint/--model (local paddle beside tensor muse deadlocks)"
        )
    out = ["--llmvp-url", endpoint.removesuffix("/graphql").rstrip("/")]
    if model:
        out += ["--model", model]
    if layout:
        out += ["--layout-url", layout.rstrip("/")]
    return out


def _run_tool(argv: list[str], timeout: int) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, text=True, timeout=timeout)


class _Lock:
    """One process per book: <root>/locks/<key>.lock holds the owner's pid."""

    def __init__(self, root: Path, key: str):
        self.path = root / "locks" / f"{key}.lock"

    def __enter__(self):
        if self.path.exists():
            try:
                pid = int(self.path.read_text().strip() or 0)
                os.kill(pid, 0)
                raise SystemExit(f"{self.path.stem} is being worked by pid {pid}")
            except (ProcessLookupError, ValueError):
                pass  # stale lock
        self.path.write_text(str(os.getpid()))
        return self

    def __exit__(self, *exc):
        self.path.unlink(missing_ok=True)


async def ocr_book(root: Path, rec: dict, route: list[str], pages: int) -> dict:
    """OCR one book segment by segment, banking each part; assemble at the end."""
    from agent.actions import extraction_actions as ea
    from agent.actions.scholarly_actions import append_extraction_records

    fx = _effects(root)
    key = rec["paper_key"]
    pdf = root / rec["pdf_path"]
    progress = dict(rec.get("book_progress") or {"next_page": 0, "parts": []})
    while True:
        start = int(progress.get("next_page") or 0)
        total = int(progress.get("total_pages") or rec.get("pages") or 0)
        if total and start >= total:
            break
        argv = [
            str(TOOL_PY),
            str(TOOL_SCRIPT),
            "--pdfs",
            str(pdf),
            "--keys",
            key,
            "--databank-dir",
            str(root / "databank"),
            "--vl-backend",
            "llmvp",
            *route,
            "--page-range",
            f"{start}:{start + pages}",
        ]
        res = _run_tool(argv, timeout=pages * 12 + 300)
        rep = None
        for line in (res.stdout or "").splitlines():
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(r, dict) and r.get("paper_key") == key:
                rep = r
        if rep is None or rep.get("error"):
            why = (rep or {}).get(
                "error"
            ) or f"no report (rc {res.returncode}): {(res.stderr or '')[-300:]}"
            print(f"  {key}: segment {start}:{start + pages} failed -- {why[:200]}")
            return {"key": key, "status": "segment_failed", "at": start, "why": why}
        total = int(rep.get("total_pages") or total)
        progress["parts"] = list(progress.get("parts") or []) + [
            ea._part_from(rep, start, pages)
        ]
        progress["next_page"] = min(start + pages, total)
        progress["total_pages"] = total
        rec = {**rec, "book_progress": progress, "updated_at": _now()}
        await append_extraction_records(fx, [rec])
        print(f"  {key}: pages {progress['next_page']}/{total}", flush=True)
    assembled = ea._assemble_segments(str(root), key, progress["parts"])
    agg = ea.aggregate_book_parts(progress["parts"])
    figdir = root / "databank" / "figures" / key
    figures = (
        sum(1 for f in os.listdir(figdir) if f.startswith("fig_"))
        if figdir.is_dir()
        else 0
    )
    rec = {
        **rec,
        "extraction_status": "extracted",
        "md_path": f"databank/markdown/{key}.md",
        "extraction_method": "paddleocr-vl-llmvp-segmented",
        "extraction_quality": {**agg, "closed_shelf": True},
        "script_profile": ea.markdown_script_profile(assembled),
        "figure_count": figures,
        "failure_reason": "",
        "updated_at": _now(),
    }
    await append_extraction_records(fx, [rec])
    return {
        "key": key,
        "status": "extracted",
        "pages": agg.get("pages"),
        "chars": len(assembled),
        "figures": figures,
    }


async def cmd_ocr(root: Path, args) -> None:
    from agent.actions.scholarly_actions import read_databank

    _require_shelf(root)
    route = _ocr_route(args)
    bank = await read_databank(_effects(root))
    keys = args.keys or sorted(
        k for k, r in bank.items() if r.get("record_kind") == "book"
    )
    for key in keys:
        rec = bank.get(key)
        if rec is None:
            print(f"SKIP {key}: not on the shelf")
            continue
        if rec.get("extraction_status") == "extracted":
            print(f"SKIP {key}: already extracted")
            continue
        with _Lock(root, key):
            print(f"OCR  {key}", flush=True)
            out = await ocr_book(root, rec, route, args.pages)
            print(f"     {out}", flush=True)


# ── figure readings (local muse) ──────────────────────────────────────

FIG_PY = REPO / "tools" / "fig_review" / ".venv" / "bin" / "python"
FIG_SCRIPT = REPO / "tools" / "fig_review" / "fig_review.py"
LOCAL_LLMVP = os.environ.get("OUROBOROS_CLOSED_LLMVP", "http://localhost:8008")


async def cmd_figtext(root: Path, args) -> None:
    """Read each book's figures on THIS machine's LLMVP (never remote).
    fig_review banks every figure as it lands, so a run can stop anywhere;
    --max-figures bounds one call."""
    from agent.actions.scholarly_actions import append_records, read_databank

    _require_shelf(root)
    fx = _effects(root)
    bank = await read_databank(fx)
    keys = args.keys or sorted(
        k for k, r in bank.items() if r.get("extraction_status") == "extracted"
    )
    for key in keys:
        if bank.get(key, {}).get("extraction_status") != "extracted":
            print(f"SKIP {key}: not OCR'd yet")
            continue
        argv = [
            str(FIG_PY),
            str(FIG_SCRIPT),
            "--keys",
            key,
            "--figures-root",
            str(root / "databank" / "figures"),
            "--markdown-dir",
            str(root / "databank" / "markdown"),
            "--out-dir",
            str(root / "databank" / "figtext"),
            "--vl-backend",
            "llmvp",
            "--llmvp-url",
            LOCAL_LLMVP,
            "--max-figures",
            str(args.max_figures),
        ]
        with _Lock(root, key):
            res = _run_tool(argv, timeout=24 * 3600)
        rep = {}
        for line in (res.stdout or "").splitlines():
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(r, dict) and r.get("paper_key") == key:
                rep = r
        print(
            f"  {key}: {json.dumps({k: rep.get(k) for k in ('described', 'figs_total', 'remaining', 'error')})}"
        )
        if rep and not rep.get("error") and not rep.get("remaining"):
            rec = dict(bank[key])
            rec.update(
                figtext_status="figtext_done",
                figtext_path=f"databank/figtext/{key}.json",
                updated_at=_now(),
            )
            await append_records(fx, [rec])


# ── pack (local muse, resumable per window) ───────────────────────────


async def pack_book(root: Path, rec: dict) -> dict:
    """Pack one book's raw doc with the production windowed packer on THIS
    machine's LLMVP, banking every window to databank/pack_progress/<key>.jsonl
    so an interruption costs one window. Books through the production booking
    path (envelope, registry fold + coinage guard -- into THIS shelf's registry)
    and re-stamps the preapproval the booking overwrites."""
    from agent.actions import curation_actions as ca
    from agent.actions.scholarly_actions import append_records, read_databank
    from agent.models import StepInput

    fx = _effects(root)
    key = rec["paper_key"]
    prog = root / "databank" / "pack_progress" / f"{key}.jsonl"
    resume: dict = {}
    if prog.exists():
        for line in prog.read_text(encoding="utf-8").splitlines():
            try:
                w = json.loads(line)
            except json.JSONDecodeError:
                continue  # a torn last line costs that window only
            resume[int(w["index"])] = w

    async def bank_window(index: int, wrec: dict) -> None:
        with open(prog, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"index": index, **wrec}, ensure_ascii=False) + "\n")

    registry = await ca._load_registry(fx)
    doc = await ca._raw_curator_doc(fx, key)
    try:
        pack = await ca._pack_windowed(
            fx, doc, registry, resume=resume, on_window=bank_window
        )
    except ca._CurateTransportFault as e:
        return {
            "key": key,
            "status": "interrupted",
            "why": str(e)[:200],
            "windows_banked": len(resume),
        }
    state = {
        "paper_key": key,
        "session_id": "",
        "review": {
            "status": "accepted",
            "summary": rec.get("review_summary") or PREAPPROVAL,
            "issues": [],
            "deny_category": "",
            "document_form": "book",
        },
        "pack": pack,
        "pack_doc_form": "raw",
    }
    out = await ca.action_curate_book_result(
        StepInput(effects=fx, context={"curate_state": state})
    )
    booked = (await read_databank(fx)).get(key) or {}
    method = str(booked.get("curation_method") or "")
    if not method.startswith("closed_preapproved"):
        booked["curation_method"] = f"closed_preapproved+{method}"
        await append_records(fx, [booked])
    q = pack.get("quality") or {}
    return {
        "key": key,
        "status": pack.get("status"),
        "booked": (out.result or {}).get("outcome"),
        "windows": f"{q.get('windows_passed')}/{q.get('windows')}",
        "leaves": q.get("numeric_leaves"),
    }


async def cmd_pack(root: Path, args) -> None:
    from agent.actions.scholarly_actions import read_databank

    _require_shelf(root)
    bank = await read_databank(_effects(root))
    keys = args.keys or sorted(
        k
        for k, r in bank.items()
        if r.get("extraction_status") == "extracted"
        and r.get("pack_status") != "packed"
    )
    for key in keys:
        rec = bank.get(key)
        if not rec or rec.get("extraction_status") != "extracted":
            print(f"SKIP {key}: not OCR'd yet")
            continue
        with _Lock(root, key):
            print(f"PACK {key}", flush=True)
            print(f"     {await pack_book(root, rec)}", flush=True)


# ── status ────────────────────────────────────────────────────────────


async def cmd_status(root: Path) -> None:
    from agent.actions.scholarly_actions import read_databank

    _require_shelf(root)
    bank = await read_databank(_effects(root))
    print(f"closed shelf {root}: {len(bank)} record(s)")
    for key, r in sorted(bank.items()):
        bp = r.get("book_progress") or {}
        q = r.get("pack_quality") or {}
        ft = root / "databank" / "figtext" / f"{key}.json"
        nfig = len(json.load(open(ft)).get("figs") or []) if ft.exists() else 0
        print(
            f"  {key[:60]:<60} pages {bp.get('next_page', 0)}/{bp.get('total_pages') or r.get('pages', 0)}"
            f"  ocr {r.get('extraction_status') or '-':<10} figtext {nfig:>4}/{r.get('figure_count', 0):<4}"
            f"  pack {r.get('pack_status') or '-'} {q.get('windows_passed', '')}/{q.get('windows', '')}"
        )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--root", type=Path, default=CLOSED_ROOT)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init")
    p = sub.add_parser("ingest")
    p.add_argument("pdfs", nargs="+", type=Path)
    p = sub.add_parser("ocr")
    p.add_argument("--keys", nargs="*")
    p.add_argument("--pages", type=int, default=40)
    p.add_argument("--endpoint", default="")
    p.add_argument("--model", default="")
    p.add_argument("--layout-url", default="")
    p = sub.add_parser("figtext")
    p.add_argument("--keys", nargs="*")
    p.add_argument("--max-figures", type=int, default=0)
    p = sub.add_parser("pack")
    p.add_argument("--keys", nargs="*")
    sub.add_parser("status")
    args = ap.parse_args()
    root = args.root.expanduser()
    if args.cmd == "init":
        cmd_init(root)
    elif args.cmd == "ingest":
        asyncio.run(cmd_ingest(root, [p.expanduser() for p in args.pdfs]))
    elif args.cmd == "ocr":
        asyncio.run(cmd_ocr(root, args))
    elif args.cmd == "figtext":
        asyncio.run(cmd_figtext(root, args))
    elif args.cmd == "pack":
        asyncio.run(cmd_pack(root, args))
    elif args.cmd == "status":
        asyncio.run(cmd_status(root))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
