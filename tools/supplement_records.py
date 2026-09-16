#!/usr/bin/env python3
"""Mark supplementary material so the pipeline and the trainer handle it well.

WHY. 599 supplement files (1.4 GB, 438 papers) were fetched by
tools/pmc_acquire.py --supplements and tools/publisher_supplements.py, and the
records only knew their names. Measured 2026-09-16: 47 of the 313 PDFs were
the parent ARTICLE (a link-regex slip), the rest split into SI documents
(PDF, Word), data files (spreadsheets, CSV, text dumps), crystal structures
(CIF) and media. Each kind needs a different handler, and whatever reaches the
training corpus must say it is a supplement and which paper it belongs to.

THE MARKS, in three places:

  1. On the parent's `supplements[]` entries (papers.jsonl, scraper-owned):
       form   si_pdf | si_docx | si_legacy_doc | si_table | si_text | si_cif |
              si_cif_aux | si_zip | si_web | si_image | si_media | si_other
       route  document | data | structure | expand | expanded | deferred |
              skip | article_duplicate
       child_key   set once a document-form entry has spawned a child record
       from_zip    the archive a member was expanded out of
  2. A CHILD RECORD per document-form entry of an ACCEPTED parent:
       paper_key      <parent>__suppNN
       record_kind    "supplement"        (the one mark the emitter reads)
       supplement_of  <parent key>, supplement_form, supplement_name, parent_doi
       review_status  "accepted", curation_method "supplement_inherited"
       pack_status    "pack_skipped_supplement"  (never curated or packed
                      alone — curation_actions._curation_pending returns False
                      on record_kind == "supplement")
     A PDF child carries access_status oa_pdf + pdf_path and takes the OCR
     drain, figtext and translation exactly as a paper does. A Word child is
     converted HERE (pandoc → GitHub markdown, media → databank/figures/<child>/
     fig_NN.png, refs rewritten to the extractor's ../figures/<key>/<fig>
     convention) and booked to the extraction sidecar with
     extraction_method "pandoc"; figtext then runs on its figures unchanged.
  3. The emitter (dev/rock_olmo/corpus_stage1.py, emit.py) labels a child
     `supplement_markdown`, carries supplement_of in provenance, and keys the
     val split on the PARENT.

Data-form entries (tables, spectra sheets, CIFs) keep their route mark on the
parent entry and no child record; their readers extend the reference layers.

Both databank writers are the agent's own (O_APPEND + lock + field filter);
every row written is the LAST row for the key plus changes, never a partial
row (last-row-replaces semantics).

    .venv/bin/python tools/supplement_records.py mark            # plan
    .venv/bin/python tools/supplement_records.py mark  --apply   # forms, routes, misfiles, zip expansion
    .venv/bin/python tools/supplement_records.py purge --apply   # delete article duplicates
    .venv/bin/python tools/supplement_records.py spawn --apply   # child records (+ pandoc for Word)
    .venv/bin/python tools/supplement_records.py convert-legacy --apply  # DOC/PPT → PDF (LibreOffice), then spawn
    .venv/bin/python tools/supplement_records.py reconvert --apply       # Word children whose EMF/WMF figures were dropped

LibreOffice is used two ways, both headless with a fresh profile per call:
legacy DOC/PPT are RENDERED TO PDF so they take the proven OCR + figtext route
(one dialect, and the engine that drew the embedded vector figures renders
them); EMF/WMF images inside Word files are rendered to PNG so the pandoc route
keeps every figure. Numbering is stable across re-conversion (rewrite_images).
"""

from __future__ import annotations

import argparse
import asyncio
import collections
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.publisher_supplements import is_article_pdf  # noqa: E402

CORPUS = os.path.expanduser("~/corpora/ouroboros-spectra")
SUPP_DIR = "supplements"
CHILD_SUFFIX = "__supp"
CHILD_RE = re.compile(r"__supp(\d+)$")
MIN_FIGURE_PX = 64

FORM_BY_EXT = {
    "pdf": "si_pdf",
    "docx": "si_docx",
    "doc": "si_legacy_doc",
    "ppt": "si_legacy_doc",
    "pptx": "si_legacy_doc",
    "odt": "si_legacy_doc",
    "rtf": "si_legacy_doc",
    "xlsx": "si_table",
    "xls": "si_table",
    "xlsm": "si_table",
    "ods": "si_table",
    "csv": "si_table",
    "tsv": "si_table",
    "txt": "si_text",
    "dat": "si_text",
    "xy": "si_text",
    "asc": "si_text",
    "md": "si_text",
    "json": "si_text",
    "cif": "si_cif",
    "hkl": "si_cif_aux",
    "fcf": "si_cif_aux",
    "res": "si_cif_aux",
    "ins": "si_cif_aux",
    "zip": "si_zip",
    "gz": "si_zip",
    "tgz": "si_zip",
    "tar": "si_zip",
    "7z": "si_zip",
    "rar": "si_zip",
    "html": "si_web",
    "htm": "si_web",
    "xml": "si_web",
    "jpg": "si_image",
    "jpeg": "si_image",
    "png": "si_image",
    "gif": "si_image",
    "tif": "si_image",
    "tiff": "si_image",
    "bmp": "si_image",
    "svg": "si_image",
    "eps": "si_image",
    "mp4": "si_media",
    "avi": "si_media",
    "mov": "si_media",
    "mpg": "si_media",
    "mpeg": "si_media",
    "wmv": "si_media",
    "mkv": "si_media",
}
ROUTE_BY_FORM = {
    "si_pdf": "document",
    "si_docx": "document",
    "si_legacy_doc": "deferred",  # until `convert-legacy` renders it to PDF
    "si_table": "data",
    "si_text": "data",
    "si_cif": "structure",
    "si_cif_aux": "skip",
    "si_zip": "expand",
    "si_web": "skip",
    "si_image": "skip",
    "si_media": "skip",
    "si_other": "skip",
}
# Archive members worth expanding: documents, data and structures. Media and
# images stay inside the archive (the fetchers skip them for the same reason).
EXPAND_FORMS = ("si_pdf", "si_docx", "si_legacy_doc", "si_table", "si_text", "si_cif")
DOCUMENT_FORMS = ("si_pdf", "si_docx")

IMG_TAG_RE = re.compile(r"<img\b[^>]*?\bsrc=\"([^\"]+)\"[^>]*?/?>", re.IGNORECASE)
IMG_MD_RE = re.compile(r"!\[[^\]]*\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
RASTER_EXT = (".png", ".jpg", ".jpeg", ".gif", ".tif", ".tiff", ".bmp")
# Windows metafiles — Word's native vector figures (247 of 1,154 images in the
# first 129 Word supplements). Pillow cannot read them; LibreOffice Draw
# renders them (the flatpak converts one in ~1 s, measured 2026-09-16).
VECTOR_EXT = (".emf", ".wmf")
# Headless LibreOffice. This machine's install is the flatpak; override with
# OUROBOROS_SOFFICE (e.g. "soffice") where a native binary exists. Every call
# gets a FRESH user profile under the corpus root — a stale profile lock is
# what made LibreOffice fussy here in the past.
SOFFICE_CMD = shlex.split(
    os.environ.get(
        "OUROBOROS_SOFFICE",
        "flatpak run --filesystem=/tmp org.libreoffice.LibreOffice",
    )
)
LEGACY_FORMS = ("si_legacy_doc",)


# ── classification ─────────────────────────────────────────────────────


def classify(name: str) -> tuple[str, str]:
    """(form, route) for a supplement file name."""
    ext = Path(str(name)).suffix.lower().lstrip(".")
    form = FORM_BY_EXT.get(ext, "si_other")
    return form, ROUTE_BY_FORM[form]


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def entry_path(root: str, parent_key: str, entry: dict) -> Path:
    return Path(root) / SUPP_DIR / parent_key / str(entry.get("name") or "")


def parent_pdf_sha(root: str, rec: dict) -> str:
    pp = str(rec.get("pdf_path") or "")
    if not pp:
        return ""
    p = Path(pp) if os.path.isabs(pp) else Path(root) / pp
    return sha256_file(p) if p.exists() else ""


def mark_entries(rec: dict, root: str, *, refresh: bool = False) -> bool:
    """Stamp form/route on every supplements[] entry. Returns True if changed.

    An entry whose URL is an article-PDF path, or whose bytes equal the
    parent's own PDF, is routed `article_duplicate` (the purge target).
    Existing marks are kept unless refresh — a route set by a later stage
    (expanded, deferred with a note) must survive a re-run."""
    entries = rec.get("supplements") or []
    if not entries:
        return False
    psha = ""
    changed = False
    for e in entries:
        if not isinstance(e, dict):
            continue
        form, route = classify(e.get("name") or "")
        if refresh or "form" not in e:
            e["form"] = form
            changed = True
        if refresh or "route" not in e:
            e["route"] = route
            changed = True
        if e.get("route") in ("article_duplicate", "expanded"):
            continue
        dup = is_article_pdf(str(e.get("url") or ""))
        if not dup and e.get("form") == "si_pdf":
            fp = entry_path(root, rec["paper_key"], e)
            if fp.exists():
                if not psha:
                    psha = parent_pdf_sha(root, rec) or "-"
                dup = psha not in ("", "-") and sha256_file(fp) == psha
        if dup and e.get("route") != "article_duplicate":
            e["route"] = "article_duplicate"
            changed = True
    return changed


# ── archives ───────────────────────────────────────────────────────────


def safe_member_name(member: str) -> str:
    base = member.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
    return re.sub(r"[^A-Za-z0-9._-]", "_", base)[:120] or "member"


def expand_zip(zip_path: Path, parent_key: str, root: str) -> list[dict]:
    """Extract document/data/structure members beside the archive.

    Members land at supplements/<parent>/<zipstem>/<safe name> (basename only:
    no zip-slip). Returns the new supplements[] entries, each carrying
    from_zip. Idempotent: an existing target of the same size is reused."""
    stem = re.sub(r"[^A-Za-z0-9._-]", "_", zip_path.stem)[:80]
    dest = Path(root) / SUPP_DIR / parent_key / stem
    out: list[dict] = []
    seen: set[str] = set()
    with zipfile.ZipFile(zip_path) as z:
        for info in z.infolist():
            if info.is_dir() or "__MACOSX" in info.filename:
                continue
            base = safe_member_name(info.filename)
            if base.startswith("."):
                continue
            form, _route = classify(base)
            if form not in EXPAND_FORMS:
                continue
            name = base
            k = 2
            while name in seen:
                stem_, dot, ext = base.rpartition(".")
                name = f"{stem_}_{k}.{ext}" if dot else f"{base}_{k}"
                k += 1
            seen.add(name)
            target = dest / name
            if not (target.exists() and target.stat().st_size == info.file_size):
                dest.mkdir(parents=True, exist_ok=True)
                with z.open(info) as src, open(target, "wb") as dst:
                    shutil.copyfileobj(src, dst)
            out.append(
                {
                    "name": f"{stem}/{name}",
                    "bytes": info.file_size,
                    "from_zip": zip_path.name,
                    "form": form,
                    "route": ROUTE_BY_FORM[form],
                }
            )
    return out


# ── purge ──────────────────────────────────────────────────────────────


def purge_entries(rec: dict) -> tuple[list[dict], list[dict]]:
    """Split supplements[] into (kept, article_duplicates)."""
    kept, dupes = [], []
    for e in rec.get("supplements") or []:
        (
            dupes
            if isinstance(e, dict) and e.get("route") == "article_duplicate"
            else kept
        ).append(e)
    return kept, dupes


# ── child records ──────────────────────────────────────────────────────


def next_child_index(rec: dict) -> int:
    n = 0
    for e in rec.get("supplements") or []:
        m = CHILD_RE.search(str((e or {}).get("child_key") or ""))
        if m:
            n = max(n, int(m.group(1)))
    return n + 1


def child_key_for(parent_key: str, index: int) -> str:
    return f"{parent_key}{CHILD_SUFFIX}{index:02d}"


def child_record(parent: dict, entry: dict, child_key: str) -> dict:
    """The papers.jsonl row for one supplement child (pure)."""
    form = str(entry.get("form") or classify(entry.get("name") or "")[0])
    name = str(entry.get("name") or "")
    rec = {
        "paper_key": child_key,
        "record_kind": "supplement",
        "supplement_of": parent["paper_key"],
        "supplement_form": form,
        "supplement_name": name,
        "parent_doi": parent.get("doi") or "",
        "title": f"{parent.get('title') or parent['paper_key']} — Supplementary material: {name}",
        "status": "cataloged",
        "year": parent.get("year"),
        "venue": parent.get("venue") or "",
        "license": parent.get("license") or "",
        "language": parent.get("language") or "",
        "review_status": "accepted",
        "curation_method": "supplement_inherited",
        "review_summary": (
            "Supplementary material of an accepted paper; inherits the parent's "
            "acceptance and is never curated or packed on its own."
        ),
        "pack_status": "pack_skipped_supplement",
        "retrieval_method": "supplement",
    }
    if parent.get("binder"):
        rec["binder"] = True
    if form == "si_pdf":
        rec["access_status"] = "oa_pdf"
        rec["pdf_path"] = f"{SUPP_DIR}/{parent['paper_key']}/{name}"
    else:
        # Not a PDF: no acquisition stage may ever look for one.
        rec["access_status"] = "supplement_doc"
        rec["pdf_path"] = ""
    return rec


# ── Word → markdown ────────────────────────────────────────────────────


def rewrite_images(md: str, child_key: str) -> tuple[str, list[str]]:
    """Replace every image reference with the extractor's convention.

    Returns (markdown, [source paths]); the i-th source becomes
    fig_{i+1:02d}.png. NUMBERING IS STABLE ACROSS RE-CONVERSION: raster
    sources come first in appearance order (exactly the numbering the first
    conversion pass used), vector sources (EMF/WMF, rendered by LibreOffice)
    are appended after them — so a child re-converted to pick up its vector
    figures keeps every fig_NN name its figtext sidecar already describes.
    Sources of any other kind are left as an HTML comment so nothing dangles."""
    seq = [m.group(1) for m in IMG_TAG_RE.finditer(md)] + [
        m.group(1) for m in IMG_MD_RE.finditer(md)
    ]
    order: list[str] = []
    for exts in (RASTER_EXT, VECTOR_EXT):
        for s in seq:
            if s.lower().endswith(exts) and s not in order:
                order.append(s)

    def repl(m: re.Match) -> str:
        src = m.group(1)
        if src not in order:
            return f"<!-- unconverted figure: {Path(src).name} -->"
        return f'<img src="../figures/{child_key}/fig_{order.index(src) + 1:02d}.png">'

    md = IMG_TAG_RE.sub(repl, md)
    md = IMG_MD_RE.sub(repl, md)
    return md, order


def latin_ratio(text: str) -> float:
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return 1.0
    return sum(1 for c in letters if ord(c) < 0x250) / len(letters)


def soffice_convert(
    files: list[Path], fmt: str, outdir: Path, root: str, timeout: int = 900
) -> dict[Path, Path]:
    """Convert files with headless LibreOffice; {source: output} for those produced.

    One process per call (LibreOffice starts in ~1 s, so batch the files), a
    fresh user profile per call, removed afterwards. A missing output is not
    an exception — the caller records what did not convert."""
    files = [f for f in files if f.exists()]
    if not files:
        return {}
    outdir.mkdir(parents=True, exist_ok=True)
    profile = Path(root) / ".soffice" / f"profile-{os.getpid()}-{time.time_ns()}"
    profile.mkdir(parents=True, exist_ok=True)
    cmd = [
        *SOFFICE_CMD,
        f"-env:UserInstallation=file://{profile}",
        "--headless",
        "--convert-to",
        fmt,
        "--outdir",
        str(outdir),
        *map(str, files),
    ]
    try:
        subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        pass
    finally:
        shutil.rmtree(profile, ignore_errors=True)
    out: dict[Path, Path] = {}
    for f in files:
        cand = outdir / f"{f.stem}.{fmt}"
        if cand.exists() and cand.stat().st_size > 0:
            out[f] = cand
    return out


def rasterise(src: Path, dst: Path) -> bool:
    """Write src as PNG at dst; False for icons and unreadable images."""
    from PIL import Image

    try:
        with Image.open(src) as im:
            if im.width < MIN_FIGURE_PX or im.height < MIN_FIGURE_PX:
                return False
            if im.mode not in ("RGB", "RGBA", "L"):
                im = im.convert("RGBA" if "A" in im.mode else "RGB")
            dst.parent.mkdir(parents=True, exist_ok=True)
            im.save(dst, format="PNG")
            return True
    except Exception:  # noqa: BLE001 — a broken image is a skipped figure
        return False


def convert_docx(src: Path, child_key: str, root: str) -> dict:
    """pandoc a Word file into the databank as if the extractor had read it."""
    rep = {"ok": False, "chars": 0, "tables": 0, "figures": 0, "skipped": 0}
    md_dir = Path(root) / "databank" / "markdown"
    fig_dir = Path(root) / "databank" / "figures" / child_key
    work = Path(root) / ".soffice"
    work.mkdir(parents=True, exist_ok=True)
    # Scratch under the corpus root, not /tmp: the LibreOffice sandbox sees
    # the host filesystem, and a re-conversion must not leave stale figures.
    shutil.rmtree(fig_dir, ignore_errors=True)
    with tempfile.TemporaryDirectory(dir=str(work)) as tmp:
        out_md = Path(tmp) / "doc.md"
        cmd = [
            "pandoc",
            str(src),
            "-t",
            "gfm",
            "--wrap=none",
            f"--extract-media={tmp}",
            "-o",
            str(out_md),
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        if proc.returncode != 0 or not out_md.exists():
            rep["error"] = (proc.stderr or "pandoc failed")[:300]
            return rep
        md = out_md.read_text(encoding="utf-8", errors="replace")
        md, sources = rewrite_images(md, child_key)
        paths = [Path(s) if os.path.isabs(s) else Path(tmp) / s for s in sources]
        vectors = [p for p in paths if p.suffix.lower() in VECTOR_EXT]
        rendered = (
            soffice_convert(vectors, "png", Path(tmp) / "vec", root) if vectors else {}
        )
        rep["vector_rendered"] = len(rendered)
        kept = 0
        for i, (s, sp) in enumerate(zip(sources, paths), 1):
            sp = rendered.get(sp, sp)
            target = fig_dir / f"fig_{i:02d}.png"
            if rasterise(sp, target):
                kept += 1
            else:
                rep["skipped"] += 1
                md = md.replace(
                    f'<img src="../figures/{child_key}/fig_{i:02d}.png">',
                    f"<!-- dropped figure: {Path(s).name} -->",
                )
        rep["figures"] = kept
        rep["skipped"] += sum(1 for _ in re.finditer(r"<!-- unconverted figure:", md))
    md = re.sub(r"\n{3,}", "\n\n", md).strip() + "\n"
    md_dir.mkdir(parents=True, exist_ok=True)
    (md_dir / f"{child_key}.md").write_text(md, encoding="utf-8")
    rep["chars"] = len(md)
    rep["tables"] = sum(1 for ln in md.splitlines() if ln.startswith("|")) + md.count(
        "<table"
    )
    rep["latin_ratio"] = round(latin_ratio(md), 3)
    rep["ok"] = True
    return rep


def extraction_row(child_key: str, rep: dict) -> dict:
    status = "extracted" if rep.get("latin_ratio", 1.0) >= 0.5 else "extract_lingual"
    return {
        "paper_key": child_key,
        "extraction_status": status,
        "failure_reason": "",
        "md_path": f"databank/markdown/{child_key}.md",
        "figure_count": int(rep.get("figures") or 0),
        "extraction_method": "pandoc",
        "extraction_quality": {
            "oracle": "none",
            "native_text": True,
            "chars": int(rep.get("chars") or 0),
            "table_lines": int(rep.get("tables") or 0),
            "figures_dropped": int(rep.get("skipped") or 0),
            "latin_ratio": rep.get("latin_ratio", 1.0),
        },
    }


# ── databank I/O ───────────────────────────────────────────────────────


def last_rows(path: Path) -> dict[str, dict]:
    out: dict[str, dict] = {}
    if not path.exists():
        return out
    with open(path, "rb") as fh:
        for raw in fh:
            raw = raw.strip(b"\x00\r\n ")
            if not raw:
                continue
            try:
                r = json.loads(raw)
            except Exception:  # noqa: BLE001
                continue
            k = r.get("paper_key")
            if k:
                out[k] = r
    return out


async def write_rows(root: str, papers: list[dict], extraction: list[dict]) -> None:
    from agent.actions.scholarly_actions import (
        append_extraction_records,
        append_records,
    )
    from agent.effects.local import LocalEffects

    fx = LocalEffects(root)
    if papers:
        await append_records(fx, papers)
    if extraction:
        await append_extraction_records(fx, extraction)


# ── commands ───────────────────────────────────────────────────────────


def cmd_mark(root: str, apply: bool, refresh: bool) -> None:
    papers = last_rows(Path(root) / "databank" / "papers.jsonl")
    rows: list[dict] = []
    forms: collections.Counter = collections.Counter()
    routes: collections.Counter = collections.Counter()
    expanded_members = 0
    for key, rec in sorted(papers.items()):
        if not rec.get("supplements"):
            continue
        rec = dict(rec)
        rec["supplements"] = [
            dict(e) if isinstance(e, dict) else e for e in rec["supplements"]
        ]
        changed = mark_entries(rec, root, refresh=refresh)
        new_entries: list[dict] = []
        for e in rec["supplements"]:
            if not isinstance(e, dict) or e.get("route") != "expand":
                continue
            zp = entry_path(root, key, e)
            if not zp.exists() or not zipfile.is_zipfile(zp):
                e["route"] = "skip"
                e["note"] = "not a readable zip"
                changed = True
                continue
            if apply:
                try:
                    members = expand_zip(zp, key, root)
                except Exception as exc:  # noqa: BLE001
                    e["route"] = "skip"
                    e["note"] = f"expand failed: {exc}"[:200]
                    changed = True
                    continue
                new_entries.extend(members)
                expanded_members += len(members)
                e["route"] = "expanded"
                changed = True
        rec["supplements"].extend(new_entries)
        for e in rec["supplements"]:
            if isinstance(e, dict):
                forms[e.get("form")] += 1
                routes[e.get("route")] += 1
        if changed:
            rows.append(rec)
    print(
        f"papers with supplements: {sum(1 for r in papers.values() if r.get('supplements'))}"
    )
    print("forms:", dict(forms.most_common()))
    print("routes:", dict(routes.most_common()))
    print(f"rows to write: {len(rows)}; zip members expanded: {expanded_members}")
    if apply and rows:
        asyncio.run(write_rows(root, rows, []))
        print("written.")
    elif not apply:
        print("dry run — pass --apply to expand archives and write rows.")


def cmd_purge(root: str, apply: bool) -> None:
    papers = last_rows(Path(root) / "databank" / "papers.jsonl")
    rows: list[dict] = []
    n_files = 0
    n_bytes = 0
    for key, rec in sorted(papers.items()):
        kept, dupes = purge_entries(rec)
        if not dupes:
            continue
        for e in dupes:
            fp = entry_path(root, key, e)
            if fp.exists():
                n_files += 1
                n_bytes += fp.stat().st_size
                print(f"  rm {fp.relative_to(root)}")
                if apply:
                    fp.unlink()
        rec = dict(rec)
        rec["supplements"] = kept
        if not kept:
            rec["supplement_source"] = ""
        rows.append(rec)
    print(
        f"article duplicates: {sum(len(purge_entries(r)[1]) for r in papers.values())} entries, {n_files} files, {n_bytes/1e6:.1f} MB; rows to write: {len(rows)}"
    )
    if apply and rows:
        asyncio.run(write_rows(root, rows, []))
        print("written.")
    elif not apply:
        print("dry run — pass --apply to delete the files and rewrite the rows.")


def cmd_spawn(root: str, apply: bool, forms: tuple[str, ...], limit: int) -> None:
    papers = last_rows(Path(root) / "databank" / "papers.jsonl")
    existing = {k for k in papers if CHILD_RE.search(k)}
    parent_rows: list[dict] = []
    child_rows: list[dict] = []
    ext_rows: list[dict] = []
    planned: collections.Counter = collections.Counter()
    reports: collections.Counter = collections.Counter()
    for key, rec in sorted(papers.items()):
        if rec.get("review_status") != "accepted" or not rec.get("supplements"):
            continue
        if rec.get("record_kind") == "supplement":
            continue
        rec = dict(rec)
        rec["supplements"] = [
            dict(e) if isinstance(e, dict) else e for e in rec["supplements"]
        ]
        touched = False
        idx = next_child_index(rec)
        for e in rec["supplements"]:
            if (
                not isinstance(e, dict)
                or e.get("route") != "document"
                or e.get("child_key")
            ):
                continue
            if e.get("form") not in forms:
                continue
            fp = entry_path(root, key, e)
            if not fp.exists():
                e["route"] = "skip"
                e["note"] = "file missing on disk"
                touched = True
                continue
            head = fp.open("rb").read(4)
            if e["form"] == "si_pdf" and not head.startswith(b"%PDF"):
                e["route"] = "skip"
                e["note"] = "not a PDF"
                touched = True
                continue
            if e["form"] == "si_docx" and not head.startswith(b"PK"):
                e["route"] = "deferred"
                e["note"] = "not a Word (OOXML) file"
                touched = True
                continue
            ck = child_key_for(key, idx)
            while ck in existing:
                idx += 1
                ck = child_key_for(key, idx)
            idx += 1
            planned[e["form"]] += 1
            if limit and sum(planned.values()) > limit:
                break
            if not apply:
                continue
            if e["form"] == "si_docx":
                rep = convert_docx(fp, ck, root)
                if not rep.get("ok"):
                    e["route"] = "deferred"
                    e["note"] = f"pandoc: {rep.get('error', 'failed')}"[:200]
                    touched = True
                    reports["docx_failed"] += 1
                    continue
                ext_rows.append(extraction_row(ck, rep))
                reports["docx_ok"] += 1
                reports["docx_figures"] += rep["figures"]
            child_rows.append(child_record(rec, e, ck))
            existing.add(ck)
            e["child_key"] = ck
            touched = True
        if touched:
            parent_rows.append(rec)
        if limit and sum(planned.values()) > limit:
            break
    print(
        f"children to spawn by form: {dict(planned)}; existing children: {len(existing)}"
    )
    if apply:
        print(f"converted: {dict(reports)}")
        asyncio.run(write_rows(root, child_rows + parent_rows, ext_rows))
        print(
            f"written: {len(child_rows)} children, {len(parent_rows)} parents, {len(ext_rows)} extraction rows."
        )
    else:
        print("dry run — pass --apply to convert and write.")


def legacy_pdf_entry(entry: dict, pdf: Path, parent_key: str, root: str) -> dict:
    """The supplements[] entry for a PDF LibreOffice rendered from a legacy file."""
    return {
        "name": str(pdf.relative_to(Path(root) / SUPP_DIR / parent_key)),
        "bytes": pdf.stat().st_size,
        "from_convert": str(entry.get("name") or ""),
        "form": "si_pdf",
        "route": "document",
    }


def cmd_convert_legacy(root: str, apply: bool) -> None:
    """Render deferred DOC/PPT supplements of accepted parents to PDF.

    The PDF becomes a new document-form entry (spawn then gives it a child on
    the proven OCR + figtext route — one dialect, and embedded vector figures
    are rendered by the same engine that drew them); the original entry is
    routed `converted` and points at it."""
    papers = last_rows(Path(root) / "databank" / "papers.jsonl")
    rows: list[dict] = []
    planned = made = 0
    for key, rec in sorted(papers.items()):
        if rec.get("review_status") != "accepted" or not rec.get("supplements"):
            continue
        rec = dict(rec)
        rec["supplements"] = [
            dict(e) if isinstance(e, dict) else e for e in rec["supplements"]
        ]
        targets = [
            e
            for e in rec["supplements"]
            if isinstance(e, dict)
            and e.get("route") == "deferred"
            and e.get("form") in LEGACY_FORMS
        ]
        if not targets:
            continue
        by_dir: dict[Path, list[tuple[dict, Path]]] = collections.defaultdict(list)
        for e in targets:
            src = entry_path(root, key, e)
            if not src.exists():
                e["note"] = "file missing on disk"
                continue
            planned += 1
            print(f"  {key[:60]:62s} {e['name']}")
            by_dir[src.parent].append((e, src))
        if not apply:
            continue
        new_entries: list[dict] = []
        for outdir, pairs in by_dir.items():
            got = soffice_convert([s for _, s in pairs], "pdf", outdir, root)
            for e, s in pairs:
                pdf = got.get(s)
                if not pdf:
                    e["note"] = "libreoffice produced no PDF"
                    continue
                ne = legacy_pdf_entry(e, pdf, key, root)
                new_entries.append(ne)
                e["route"] = "converted"
                e["converted_to"] = ne["name"]
                made += 1
        rec["supplements"].extend(new_entries)
        rows.append(rec)
    print(f"legacy files to render: {planned}; rendered: {made}")
    if apply and rows:
        asyncio.run(write_rows(root, rows, []))
        print(
            f"written {len(rows)} parent rows — run `spawn --apply` for the children."
        )
    elif not apply:
        print("dry run — pass --apply to render and write.")


def needs_reconvert(child: dict, ext_row: dict, include_done: bool) -> bool:
    """A Word child whose first conversion dropped figures LibreOffice can render."""
    if child.get("record_kind") != "supplement":
        return False
    if child.get("supplement_form") != "si_docx":
        return False
    if ext_row.get("extraction_method") != "pandoc":
        return False
    if int((ext_row.get("extraction_quality") or {}).get("figures_dropped") or 0) <= 0:
        return False
    return include_done or child.get("figtext_status") != "figtext_done"


def cmd_reconvert(root: str, apply: bool, include_done: bool) -> None:
    """Re-run the Word conversion where figures were dropped, now with EMF/WMF.

    Stable numbering (rewrite_images) keeps every existing fig_NN name, so a
    figtext sidecar banked so far stays valid; new figures take new numbers.
    A child already figtext_done gets its status cleared (--include-done) so
    the resumable fig_review pass describes only the new figures."""
    papers = last_rows(Path(root) / "databank" / "papers.jsonl")
    ext = last_rows(Path(root) / "databank" / "extraction.jsonl")
    ext_rows: list[dict] = []
    paper_rows: list[dict] = []
    planned = 0
    stats: collections.Counter = collections.Counter()
    for ck, child in sorted(papers.items()):
        row = ext.get(ck) or {}
        if not needs_reconvert(child, row, include_done):
            continue
        parent = papers.get(child.get("supplement_of") or "") or {}
        entry = next(
            (
                e
                for e in parent.get("supplements") or []
                if isinstance(e, dict) and e.get("child_key") == ck
            ),
            None,
        )
        if not entry:
            continue
        src = entry_path(root, parent["paper_key"], entry)
        if not src.exists():
            continue
        planned += 1
        if not apply:
            continue
        rep = convert_docx(src, ck, root)
        if not rep.get("ok"):
            stats["failed"] += 1
            continue
        ext_rows.append({**row, **extraction_row(ck, rep)})
        stats["reconverted"] += 1
        stats["figures_now"] += rep["figures"]
        stats["vector_rendered"] += rep.get("vector_rendered", 0)
        if child.get("figtext_status") == "figtext_done":
            paper_rows.append({**child, "figtext_status": "", "figtext_path": ""})
    print(f"children to re-convert: {planned}")
    if apply:
        asyncio.run(write_rows(root, paper_rows, ext_rows))
        print(
            f"{dict(stats)}; written {len(ext_rows)} extraction rows, {len(paper_rows)} figtext resets."
        )
    else:
        print("dry run — pass --apply to re-convert.")


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument(
        "command", choices=("mark", "purge", "spawn", "convert-legacy", "reconvert")
    )
    ap.add_argument(
        "--include-done",
        action="store_true",
        help="reconvert: also children whose figtext is already done (status reset)",
    )
    ap.add_argument("--root", default=CORPUS)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument(
        "--refresh",
        action="store_true",
        help="mark: recompute form/route on every entry",
    )
    ap.add_argument(
        "--forms",
        nargs="*",
        default=list(DOCUMENT_FORMS),
        help="spawn: forms to spawn children for",
    )
    ap.add_argument("--limit", type=int, default=0, help="spawn: at most N children")
    a = ap.parse_args()
    if a.command == "mark":
        cmd_mark(a.root, a.apply, a.refresh)
    elif a.command == "purge":
        cmd_purge(a.root, a.apply)
    elif a.command == "convert-legacy":
        cmd_convert_legacy(a.root, a.apply)
    elif a.command == "reconvert":
        cmd_reconvert(a.root, a.apply, a.include_done)
    else:
        cmd_spawn(a.root, a.apply, tuple(a.forms), a.limit)
    return 0


if __name__ == "__main__":
    sys.exit(main())
