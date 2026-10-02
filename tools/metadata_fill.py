#!/usr/bin/env python3
"""Fill missing bibliographic fields from the document itself and open registries.

WHY (2026-10-02). The closed-shelf books were catalogued from their filenames
("Principals Of Instrumental Analysis Fifth Edition Skoog Holler Nieman", no
authors, year 0), and 426 accepted papers in the open corpus carry no authors,
484 no venue. Operator ruling 2026-10-02: an agent should fill the missing
metadata fields as thoroughly as it can from the extracted markdown and from
the web.

SOURCES, per field, best first:
  * papers: Crossref by DOI (the publisher's own deposit), then the front
    matter of the extracted markdown;
  * books: the front matter (title page, copyright page -- THIS copy and
    edition), then Open Library by an ISBN printed there, then an Open
    Library title search (title and authors only: a search hit is the work,
    not this edition, so it never supplies a year or a publisher).
The front matter is read by the LOCAL model only (this machine's LLMVP) -- a
closed book's text never leaves the machine; only its title, authors or ISBN
go to Open Library. Every value read from the front matter must be verifiable
in that text (title words, author surnames, the year, the ISBN digits), the
same discipline as the pack grounding gate; an unverifiable value is dropped
and reported, never booked.

WRITES. Only fields that are empty -- or provisional, like a book title taken
from its filename -- are filled; a value already on the record is never
replaced. `plan` writes <root>/databank/metadata_fill_plan.json for review;
`apply` books that plan onto FRESH rows (re-read at apply time; a field
filled meanwhile is skipped), recording each field's source in
`metadata_sources`.

    .venv/bin/python tools/metadata_fill.py plan --root ~/corpora/ouroboros-closed
    .venv/bin/python tools/metadata_fill.py plan --scope accepted --no-llm
    .venv/bin/python tools/metadata_fill.py apply --root ~/corpora/ouroboros-closed
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import html
import json
import os
import re
import sys
import unicodedata
import urllib.parse
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

DEFAULT_ROOT = os.path.expanduser("~/corpora/ouroboros-spectra")
PLAN_FILE = "databank/metadata_fill_plan.json"
BOOK_FIELDS = ("title", "authors", "year", "edition", "publisher", "isbn")
PAPER_FIELDS = ("title", "authors", "year", "venue")
FRONT_CHARS = {"book": 6000, "paper": 6000}
_CROSSREF = "https://api.crossref.org/works/"
_OL_BOOKS = "https://openlibrary.org/api/books"
_OL_SEARCH = "https://openlibrary.org/search.json"

PROMPT = """Below is the OPENING of a {kind} as extracted from a scan or PDF: {where}. Report its bibliographic details exactly as they are printed there.

<text>
{text}
</text>

Return ONLY a JSON object with these keys (null for anything not printed in the text above; never guess, never use outside knowledge):
{{"title": "the full title, with any subtitle after a colon", "authors": ["each author or editor's full name as printed"], "year": "the publication or copyright year of THIS edition (4 digits)", "edition": "the edition statement, e.g. Fifth Edition", "publisher": "the publisher's name", "isbn": "one ISBN printed for this edition", "venue": "the journal or series name"}}"""


# ── fields ────────────────────────────────────────────────────────────


def _kind(rec: dict) -> str:
    return "book" if rec.get("record_kind") == "book" else "paper"


def _empty(v) -> bool:
    return v in (None, "", [], 0, "0") or (isinstance(v, str) and not v.strip())


def fillable_fields(rec: dict) -> list[str]:
    """Fields this record lacks, or holds only provisionally."""
    provisional = set(rec.get("metadata_provisional") or [])
    if "title from the filename" in str(rec.get("metadata_note") or ""):
        provisional.add("title")
    fields = BOOK_FIELDS if _kind(rec) == "book" else PAPER_FIELDS
    return [f for f in fields if f in provisional or _empty(rec.get(f))]


def _fold(s: str) -> str:
    s = unicodedata.normalize("NFKD", str(s)).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


def _words(s: str) -> list[str]:
    return [w for w in _fold(s).split() if len(w) > 2]


def isbn_valid(isbn: str) -> bool:
    d = re.sub(r"[^0-9Xx]", "", isbn).upper()
    if len(d) == 10 and d[:9].isdigit():
        total = sum((10 - i) * (10 if c == "X" else int(c)) for i, c in enumerate(d))
        return total % 11 == 0
    if len(d) == 13 and d.isdigit():
        return sum((3 if i % 2 else 1) * int(c) for i, c in enumerate(d)) % 10 == 0
    return False


_ISBN_RE = re.compile(
    r"isbn(?:[\s\-]*1[03])?[\s:#.\-]*((?:97[89][\s\-]?)?(?:\d[\s\-]?){9}[\dXx])", re.I
)


def printed_isbns(text: str) -> list[str]:
    """Every checksum-valid ISBN printed after an "ISBN" label, 13-digit first."""
    out: list[str] = []
    for m in _ISBN_RE.finditer(text):
        d = re.sub(r"[^0-9Xx]", "", m.group(1)).upper()
        if isbn_valid(d) and d not in out:
            out.append(d)
    return sorted(out, key=lambda d: -len(d))


def verified(field: str, value, text: str) -> bool:
    """Can this front-matter value be found in the text it was read from?"""
    folded = _fold(text)
    if field == "title":
        words = _words(value)
        return bool(words) and (
            _fold(value) in folded or all(w in folded.split() for w in words)
        )
    if field == "authors":
        names = [n for n in value if isinstance(n, str) and n.strip()]
        return bool(names) and all(
            (_fold(n).split() or [""])[-1] in folded.split() for n in names
        )
    if field == "year":
        return bool(re.search(rf"(?<!\d){value}(?!\d)", text))
    if field == "isbn":
        return re.sub(r"[^0-9X]", "", str(value).upper()) in re.sub(
            r"[^0-9X]", "", text.upper()
        )
    return bool(_fold(value)) and _fold(value) in folded


def _clean(field: str, value):
    """A model or registry value in the record's own shape, or None."""
    if _empty(value):
        return None
    if field == "authors":
        if isinstance(value, str):
            value = [value]
        names = [
            re.sub(r"\s+", " ", html.unescape(str(n))).strip()
            for n in value
            if str(n).strip()
        ]
        return names or None
    if field == "year":
        m = re.search(r"(1[5-9]\d\d|20\d\d)", str(value))
        if not m or int(m.group(1)) > dt.date.today().year:
            return None
        return int(m.group(1))
    if field == "isbn":
        d = re.sub(r"[^0-9Xx]", "", str(value)).upper()
        return d if isbn_valid(d) else None
    # Registries send markup: Crossref titles carry JATS tags (<i>, <sub>) and
    # HTML entities ("Particle &amp; Particle Systems Characterization").
    text = html.unescape(re.sub(r"<[^>]+>", "", str(value)))
    return re.sub(r"\s+", " ", text).strip() or None


# ── sources ───────────────────────────────────────────────────────────


async def crossref_by_doi(fx, doi: str) -> dict:
    from agent.actions.scholarly_actions import _contact_email, polite_request

    try:
        resp = await polite_request(
            fx,
            "GET",
            _CROSSREF + urllib.parse.quote(doi, safe="/"),
            params={"mailto": _contact_email()},
        )
    except Exception:  # noqa: BLE001 -- a lookup never fails the run
        return {}
    msg = (resp.json_data or {}).get("message") if resp.status == 200 else None
    if not isinstance(msg, dict):
        return {}
    people = msg.get("author") or msg.get("editor") or []
    names = [
        " ".join(x for x in (p.get("given"), p.get("family")) if x) or p.get("name", "")
        for p in people
        if isinstance(p, dict)
    ]
    issued = (msg.get("issued") or {}).get("date-parts") or [[None]]
    return {
        "title": " ".join((msg.get("title") or [""])[:1]),
        "authors": names,
        "year": (issued[0] or [None])[0],
        "venue": " ".join((msg.get("container-title") or [""])[:1]),
        "publisher": msg.get("publisher") or "",
        "isbn": (msg.get("ISBN") or [""])[0],
        "edition": msg.get("edition-number") or "",
    }


async def openlibrary_by_isbn(fx, isbn: str) -> dict:
    from agent.actions.scholarly_actions import polite_request

    try:
        resp = await polite_request(
            fx,
            "GET",
            _OL_BOOKS,
            params={"bibkeys": f"ISBN:{isbn}", "format": "json", "jscmd": "data"},
        )
    except Exception:  # noqa: BLE001
        return {}
    data = (resp.json_data or {}).get(f"ISBN:{isbn}") if resp.status == 200 else None
    if not isinstance(data, dict):
        return {}
    title = data.get("title") or ""
    if data.get("subtitle"):
        title = f"{title}: {data['subtitle']}"
    return {
        "title": title,
        "authors": [a.get("name", "") for a in data.get("authors") or []],
        "year": data.get("publish_date") or "",
        "publisher": " ".join(
            p.get("name", "") for p in (data.get("publishers") or [])[:1]
        ),
        "isbn": isbn,
    }


async def openlibrary_search(fx, title: str, author: str = "") -> dict:
    """Title and authors of the best-matching WORK, only on a confident match."""
    from agent.actions.scholarly_actions import polite_request

    params = {
        "title": title[:200],
        "limit": "5",
        "fields": "title,subtitle,author_name",
    }
    if author:
        params["author"] = author
    try:
        resp = await polite_request(fx, "GET", _OL_SEARCH, params=params)
    except Exception:  # noqa: BLE001
        return {}
    want = set(_words(title))
    for doc in (resp.json_data or {}).get("docs") or [] if resp.status == 200 else []:
        got = set(_words(doc.get("title", "")))
        if want and got and len(want & got) / len(want | got) >= 0.75:
            t = doc.get("title", "")
            if doc.get("subtitle"):
                t = f"{t}: {doc['subtitle']}"
            return {"title": t, "authors": list(doc.get("author_name") or [])[:12]}
    return {}


async def front_matter(fx, rec: dict, text: str) -> tuple[dict, dict]:
    """(verified fields, rejected fields) the local model read from the text."""
    from agent.llm_json import parse_llm_json

    kind = _kind(rec)
    where = (
        "the cover and title page come first, then (after [...]) the copyright page"
        if kind == "book"
        else "the title block and abstract come first"
    )
    r = await fx.run_inference(
        PROMPT.format(kind=kind, where=where, text=text),
        {"max_tokens": 3000, "temperature": 0.0},
    )
    got = parse_llm_json(r.text or "")
    if not isinstance(got, dict):
        return {}, {"_error": (r.error or "no JSON object")[:200]}
    ok, bad = {}, {}
    for field, value in got.items():
        v = _clean(field, value)
        if v is None:
            continue
        (ok if verified(field, v, text) else bad)[field] = v
    return ok, bad


# ── plan / apply ──────────────────────────────────────────────────────


#: Where a book prints its edition, publisher, year and ISBN.
_COPYRIGHT_RE = re.compile(r"copyright|\u00a9|\bisbn\b|library of congress", re.I)


def _front_text(root: Path, rec: dict) -> str:
    """The text the front matter is read from, markup stripped.

    A paper: its opening. A book: its opening AND the copyright page, which
    textbooks print after inside-cover tables -- Callister's sits 32k
    characters in, Skoog's 56k, past any fixed window.
    """
    key = rec["paper_key"]
    for name in (f"{key}.en.md", f"{key}.md"):
        p = root / "databank" / "markdown" / name
        if not p.exists():
            continue
        text = p.read_text(encoding="utf-8", errors="replace")
        text = re.sub(r"<[^>]+>", " ", text)  # images, divs, table cells
        text = re.sub(r"[ \t]+", " ", text)
        text = re.sub(r"\s*\n\s*\n\s*", "\n\n", text)
        head = FRONT_CHARS[_kind(rec)]
        if _kind(rec) != "book":
            return text[:head]
        m = _COPYRIGHT_RE.search(text, 0, 200_000)
        if m is None or m.start() < head - 1500:
            return text[: head + 3000]
        start = max(head, m.start() - 2500)
        return text[:head] + "\n\n[...]\n\n" + text[start : m.start() + 3500]
    return ""


async def plan_one(fx, root: Path, rec: dict, *, llm: bool, web: bool) -> dict:
    want = fillable_fields(rec)
    proposals: dict[str, dict] = {}  # field -> {"value", "source"}
    notes: list[str] = []

    def offer(fields: dict, source: str) -> None:
        for f in want:
            v = _clean(f, fields.get(f))
            if v is None:
                continue
            if f in proposals:
                if _fold(json.dumps(proposals[f]["value"])) != _fold(json.dumps(v)):
                    notes.append(
                        f"{f}: {source} says {v!r}, kept {proposals[f]['source']}"
                    )
                continue
            proposals[f] = {"value": v, "source": source}

    text = _front_text(root, rec)
    doi = str(rec.get("doi") or "").strip()
    if _kind(rec) == "paper" and doi and web:
        offer(await crossref_by_doi(fx, doi), "crossref")
    if llm and text and any(f not in proposals for f in want):
        ok, bad = await front_matter(fx, rec, text)
        offer(ok, "front_matter")
        if bad:
            notes.append(f"unverifiable in the text, dropped: {bad}")
    if _kind(rec) == "book" and text:
        isbns = printed_isbns(text)
        if isbns:
            offer({"isbn": isbns[0]}, "front_matter")
            if web:
                offer(
                    await openlibrary_by_isbn(fx, isbns[0]),
                    f"openlibrary:isbn:{isbns[0]}",
                )
        if web and any(f in want and f not in proposals for f in ("title", "authors")):
            title = (proposals.get("title") or {}).get("value") or str(
                rec.get("title") or ""
            )
            hit = await openlibrary_search(fx, title)
            offer({k: hit.get(k) for k in ("title", "authors")}, "openlibrary:search")
    return {
        "paper_key": rec["paper_key"],
        "kind": _kind(rec),
        "wanted": want,
        "current": {f: rec.get(f) for f in want},
        "fill": proposals,
        "notes": notes,
    }


def _in_scope(rec: dict, scope: str) -> bool:
    if rec.get("record_kind") == "supplement":
        return False
    if scope == "books":
        return rec.get("record_kind") == "book"
    if scope == "accepted":
        return rec.get("review_status") == "accepted"
    return True


async def cmd_plan(a) -> int:
    from agent.actions.scholarly_actions import read_databank
    from agent.effects.local import LocalEffects

    root = Path(a.root).expanduser()
    fx = LocalEffects(str(root))  # this machine's LLMVP: never a remote route
    bank = await read_databank(fx)
    keys = a.keys.split(",") if a.keys else sorted(bank)
    todo = [
        bank[k]
        for k in keys
        if k in bank and _in_scope(bank[k], a.scope) and fillable_fields(bank[k])
    ][: a.limit or None]
    print(f"{len(todo)} record(s) with fields to fill under {root}", flush=True)
    plans = []
    for rec in todo:
        p = await plan_one(fx, root, rec, llm=not a.no_llm, web=not a.offline)
        plans.append(p)
        filled = {f: f"{x['value']!r} [{x['source']}]" for f, x in p["fill"].items()}
        print(f"\n{p['paper_key']}  wants {p['wanted']}", flush=True)
        for f, s in filled.items():
            print(f"  {f:<10} {str(p['current'].get(f))[:50]!r:<54} -> {s[:150]}")
        for n in p["notes"]:
            print(f"  note: {n[:300]}")
    out = root / PLAN_FILE
    out.write_text(
        json.dumps(
            {"made_at": dt.datetime.now(dt.timezone.utc).isoformat(), "plans": plans},
            indent=1,
            ensure_ascii=False,
        )
    )
    n = sum(len(p["fill"]) for p in plans)
    print(
        f"\nplan: {n} field(s) over {sum(bool(p['fill']) for p in plans)} record(s) -> {out}"
    )
    return 0


async def cmd_apply(a) -> int:
    from agent.actions.scholarly_actions import append_records, read_databank
    from agent.effects.local import LocalEffects

    root = Path(a.root).expanduser()
    plan = json.loads((root / PLAN_FILE).read_text())
    fx = LocalEffects(str(root))
    bank = await read_databank(fx)  # FRESH: the plan may be hours old
    stamp = dt.datetime.now(dt.timezone.utc).isoformat()
    rows = []
    n_fields = 0
    for p in plan["plans"]:
        rec = dict(bank.get(p["paper_key"]) or {})
        still = set(fillable_fields(rec)) if rec else set()
        fill = {f: x for f, x in p["fill"].items() if f in still}
        if not fill:
            continue
        sources = dict(rec.get("metadata_sources") or {})
        for f, x in fill.items():
            rec[f] = x["value"]
            sources[f] = x["source"]
        rec["metadata_sources"] = sources
        rec["metadata_filled_at"] = stamp
        n_fields += len(fill)
        prov = [f for f in rec.get("metadata_provisional") or [] if f not in fill]
        if prov:
            rec["metadata_provisional"] = prov
        else:
            rec.pop("metadata_provisional", None)
        if (
            "title from the filename" in str(rec.get("metadata_note") or "")
            and "title" in fill
        ):
            rec["metadata_note"] = (
                f"filled by tools/metadata_fill.py {stamp[:10]} "
                f"(the filename title was provisional)"
            )
        rec["updated_at"] = stamp
        rows.append(rec)
    if rows:
        await append_records(fx, rows)
    print(f"applied {n_fields} field(s) on {len(rows)} record(s)")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("plan", help="look up and propose fills; writes the plan file")
    p.add_argument("--root", default=DEFAULT_ROOT)
    p.add_argument("--scope", choices=("books", "accepted", "all"), default="all")
    p.add_argument("--keys", default="", help="comma-separated paper keys")
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--no-llm", action="store_true", help="registries only")
    p.add_argument("--offline", action="store_true", help="front matter only")
    q = sub.add_parser("apply", help="book the plan file onto fresh rows")
    q.add_argument("--root", default=DEFAULT_ROOT)
    a = ap.parse_args()
    return asyncio.run(cmd_plan(a) if a.cmd == "plan" else cmd_apply(a))


if __name__ == "__main__":
    raise SystemExit(main())
