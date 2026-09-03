#!/usr/bin/env python3
"""Resolve identifiers for papers with no DOI, in tiers; record the rest as none.

OPERATOR POLICY (2026-09-03): "attempt more manual action to resolve what we
can, look for alternative ids on older papers, and emit the rest as
identifier: none."

TIERS, cheapest first. Every paper gets exactly one, written once to
`identifier` / `identifier_kind` so it is never looked up twice:

  1. openalex   an `openalex_id` already on the record (free, no network)
  2. hdl/urn/   a persistent identifier embedded in the source URL: repository
     theses.fr  handles, national thesis numbers, URNs (free, no network)
     /nbn
  3. openalex   a title+year lookup against the OpenAlex polite pool, taken
     (looked up) ONLY on a confident match (title overlap >= 0.72 AND years within 1).
                A DOI found this way is promoted to the `doi` field -- it is a
                real DOI, not an alternative id.
  4. core       the CORE download id: identifies a COPY, not a work, so it is
                the last resort before "none".
  5. none       recorded explicitly, so a consumer can tell an unidentified
                paper from one nobody looked at.

Network tiers run under the existing polite pacer (api.openalex.org at 0.6 s
min interval, mailto in every request). --offline skips tier 3 entirely.

    python dev/resolve_identifiers.py                    # dry run, all tiers
    python dev/resolve_identifiers.py --offline          # dry run, no network
    python dev/resolve_identifiers.py --apply --limit 50
"""

from __future__ import annotations

import argparse
import asyncio
import collections
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.actions.curation_actions import _curation_pending  # noqa: E402
from agent.actions.identifiers import (  # noqa: E402
    is_component_doi,
    is_confident_match,
    openalex_id_short,
    record_identifier,
)
from agent.actions.scholarly_actions import (  # noqa: E402
    DATABANK_PATH,
    _contact_email,
    _read_jsonl_records,
    append_records,
    polite_request,
    read_databank,
)
from agent.effects.local import LocalEffects  # noqa: E402

ROOT = os.path.expanduser("~/corpora/ouroboros-spectra")
_OPENALEX = "https://api.openalex.org/works"
_CROSSREF = "https://api.crossref.org/works"


# Tiers worth a network lookup: they identify a COPY or nothing at all.
_WEAK_TIERS = ("core", "none", "other")


def _in_scope(rec: dict) -> bool:
    return rec.get("review_status") == "accepted" or _curation_pending(rec)


def needs_identity(rec: dict) -> bool:
    """Papers that matter, with no identifier of any tier recorded yet."""
    if rec.get("doi") or rec.get("arxiv_id") or rec.get("identifier_kind"):
        return False
    return _in_scope(rec)


def wants_upgrade(rec: dict) -> bool:
    """Papers resolved only to a weak tier: a CORE download id names a COPY,
    "other"/"none" name little or nothing. A real DOI would be better, and is
    worth one polite lookup each."""
    if rec.get("doi") or rec.get("arxiv_id"):
        return False
    return str(rec.get("identifier_kind") or "") in _WEAK_TIERS and _in_scope(rec)


async def lookup_openalex(effects, rec: dict) -> tuple[str, str, str]:
    """(identifier, kind, doi) from a title+year search, or ("", "", "").

    Never raises: an unreachable index means "not resolved", not a failure.
    """
    title = str(rec.get("title") or "").strip()
    if len(title) < 12:
        return "", "", ""
    params = {
        "search": title[:250],
        "per_page": "5",
        "select": "id,doi,display_name,publication_year",
        "mailto": _contact_email(),
    }
    year = rec.get("year")
    try:
        if year and 1800 < int(year) < 2100:
            params["filter"] = (
                f"from_publication_date:{int(year)-1}-01-01,to_publication_date:{int(year)+1}-12-31"
            )
    except (TypeError, ValueError):
        pass
    try:
        resp = await polite_request(effects, "GET", _OPENALEX, params=params)
    except Exception:  # noqa: BLE001 -- a lookup never fails a resolution
        return "", "", ""
    if resp.status != 200 or not isinstance(resp.json_data, dict):
        return "", "", ""
    for work in (resp.json_data.get("results") or [])[:5]:
        if not isinstance(work, dict) or not is_confident_match(work, rec):
            continue
        doi = str(work.get("doi") or "").replace("https://doi.org/", "").strip()
        wid = openalex_id_short(str(work.get("id") or ""))
        if doi:
            return doi, "doi", doi
        if wid:
            return wid, "openalex", ""
    return "", "", ""


async def lookup_crossref(effects, rec: dict) -> tuple[str, str, str]:
    """(identifier, kind, doi) from a Crossref bibliographic search.

    WHY NOT OPENALEX (measured 2026-09-03). OpenAlex now meters by credit: the
    free allowance is $0.10/day (1,000 credits) and a search costs 10, so ~100
    searches a day, and the mission's own discovery had already spent it --
    every request returned 429 "Insufficient budget", mailto or not. Crossref
    is free, wants only a polite mailto, and returns the DOI directly, which
    is the identifier we actually want rather than a work id standing in for
    one.
    """
    title = str(rec.get("title") or "").strip()
    if len(title) < 12:
        return "", "", ""
    params = {
        "query.bibliographic": title[:250],
        "rows": "5",
        "select": "DOI,title,issued",
        "mailto": _contact_email(),
    }
    try:
        resp = await polite_request(effects, "GET", _CROSSREF, params=params)
    except Exception:  # noqa: BLE001 -- a lookup never fails a resolution
        return "", "", ""
    if resp.status != 200 or not isinstance(resp.json_data, dict):
        return "", "", ""
    for item in ((resp.json_data.get("message") or {}).get("items") or [])[:5]:
        if not isinstance(item, dict):
            continue
        parts = (item.get("issued") or {}).get("date-parts") or [[None]]
        work = {
            "title": (item.get("title") or [""])[0],
            "publication_year": (parts[0] or [None])[0],
        }
        if not is_confident_match(work, rec):
            continue
        doi = str(item.get("DOI") or "").strip()
        if doi and not is_component_doi(doi):
            return doi, "doi", doi
    return "", "", ""


async def main_async(a) -> int:
    fx = LocalEffects(a.root)
    bank = await read_databank(fx)
    side = await _read_jsonl_records(fx, DATABANK_PATH)
    pick = wants_upgrade if a.upgrade else needs_identity
    todo = [k for k, r in sorted(bank.items()) if pick(r)]
    if a.limit:
        todo = todo[: a.limit]
    print(f"papers needing identity: {len(todo)}{' (limited)' if a.limit else ''}\n")

    tiers = collections.Counter()
    rows: list[dict] = []
    for n, key in enumerate(todo, 1):
        rec = bank[key]
        ident, kind = record_identifier(rec)  # tiers 1, 2 and 4: free
        ident = openalex_id_short(ident) if kind == "openalex" else ident
        doi = ""
        if (not ident or kind in _WEAK_TIERS) and not a.offline:
            # A CORE id identifies a copy; try for the work itself first.
            lookup = lookup_openalex if a.via == "openalex" else lookup_crossref
            l_ident, l_kind, l_doi = await lookup(fx, rec)
            if l_ident:
                ident, kind, doi = l_ident, l_kind, l_doi
                kind = f"{kind}*"  # starred = looked up, for the report
        if not ident:
            kind = "none"
        tiers[kind] += 1
        merged = dict(side.get(key) or rec)
        merged["paper_key"] = key
        merged["identifier"] = ident
        merged["identifier_kind"] = kind.rstrip("*")
        if doi and not merged.get("doi"):
            merged["doi"] = doi  # a real DOI belongs in the DOI field
        rows.append(merged)
        if n <= a.show or kind.endswith("*"):
            print(f"  [{n:>3}] {kind:<11} {ident[:44]:<46} {key[:44]}")
        if n % 25 == 0:
            print(f"  ... {n}/{len(todo)}  {dict(tiers)}", flush=True)

    print(f"\ntiers: {dict(tiers)}")
    resolved = sum(v for k, v in tiers.items() if k != "none")
    print(
        f"resolved {resolved}/{len(todo)} ({100*resolved/max(len(todo),1):.0f}%); "
        f"explicit none {tiers['none']}"
    )
    json.dump(
        [
            {
                k: r.get(k)
                for k in ("paper_key", "identifier", "identifier_kind", "doi", "title")
            }
            for r in rows
        ],
        open(a.out, "w"),
        indent=1,
        ensure_ascii=False,
    )
    print(f"wrote {a.out}")
    if not a.apply:
        print("\n(dry run: nothing written to the databank)")
        return 0
    await append_records(fx, rows)
    print(f"applied {len(rows)} records")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=ROOT)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--show", type=int, default=15)
    ap.add_argument(
        "--offline", action="store_true", help="skip the network lookup tier"
    )
    ap.add_argument(
        "--via",
        choices=("crossref", "openalex"),
        default="crossref",
        help="lookup index for the network tier (OpenAlex now meters by credit)",
    )
    ap.add_argument("--apply", action="store_true")
    ap.add_argument(
        "--upgrade",
        action="store_true",
        help="target papers already resolved only to a weak tier (core/other/none)",
    )
    ap.add_argument(
        "--out", default=os.path.expanduser("~/tmp/resolve_identifiers.json")
    )
    return asyncio.run(main_async(ap.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
