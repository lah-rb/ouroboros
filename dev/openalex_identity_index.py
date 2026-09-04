#!/usr/bin/env python3
"""Build and query a local title -> DOI identity index from the OpenAlex mirror.

WHY. Identity resolution needs "does this title+year exist, and what is its
DOI?" a few hundred times a day, and OpenAlex's API now meters that by credit
while Crossref does not index the theses that make up most of the unresolved
pool. The mirror (dev/openalex_snapshot_sync.py) holds every work; this pulls
the four identity columns out of 725 GB of parquet -- id, doi, title,
publication_year, ~6.5% of the bytes -- into one DuckDB file on the SSD, with
a normalised-title key for exact hits and a full-text index for the rest.

The mirror lives on the HDD; this index lives on /home: the FTS index over
~half a billion titles is tens of GB, and the HDD is at ~85% once the mirror
is on it.

MATCHING stays in agent/actions/identifiers.py: this module only RETRIEVES
candidates (exact normalised title, then BM25 top-5); `is_confident_match`
decides, with the same title-overlap + corroborating-year rule the Crossref
path uses. A local index removes the rate limit, not the judgement.

    python dev/openalex_identity_index.py build --mirror /media/lah-rb/openalex
    python dev/openalex_identity_index.py lookup "Raman spectra of quartz" --year 2013
"""

from __future__ import annotations

import argparse
import glob
import os
import re
import sys
import time

import duckdb

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.actions.identifiers import is_confident_match  # noqa: E402

DEFAULT_DB = os.path.expanduser("~/corpora/openalex_identity.duckdb")
_WORD = re.compile(r"[a-z0-9]+")


def norm_title(t: str | None) -> str:
    """Lower-cased alphanumeric words joined by single spaces: the exact-hit
    key. Punctuation, case and OCR spacing differences vanish; word order and
    stopwords are kept (the fuzzy tier handles those)."""
    return " ".join(_WORD.findall(str(t or "").lower()))


def build(mirror: str, db: str, threads: int = 8) -> None:
    """Extract the identity columns of every mirrored work into one table.

    NO DEDUPE. Partitions are keyed by `updated_date` and a work lives in
    exactly the partition of its LATEST update -- verified 2026-09-03 over 30
    partitions / 11.78M rows: 11,784,027 distinct ids, zero repeats. If that
    ever stops holding the exact-title lookup returns near-duplicates and the
    match gate picks one, which is survivable, but the check is cheap enough
    to redo after a schema change.

    Measured on 6 real partitions (2.9 GB, 2.4M rows): scan 68 GB/min, exact
    index 4 s, FTS 7 s, 0.61 GB of database per 2.4M rows -- so the full works
    entity lands around 66 GB, which is why the database belongs on the SSD
    and not beside the mirror on the HDD.

    Do NOT run this while the mirror is still syncing: both hammer the same
    USB spindle and the scan crawls.
    """
    src = os.path.join(mirror, "data", "parquet", "works", "**", "*.parquet")
    if not glob.glob(src, recursive=True):
        raise SystemExit(
            f"no parquet under {src} (a symlinked partition will NOT be globbed -- "
            "point --mirror at the real tree)"
        )
    con = duckdb.connect(db)
    con.execute(f"PRAGMA threads={threads}")
    con.execute("PRAGMA memory_limit='24GB'")
    t0 = time.time()
    print(f"scanning {src} (identity columns only) ...", flush=True)
    con.execute(f"""
        CREATE OR REPLACE TABLE works_identity AS
        SELECT
            regexp_extract(id, 'W\\d+') AS id,
            replace(doi, 'https://doi.org/', '') AS doi,
            title,
            lower(regexp_replace(regexp_replace(coalesce(title,''), '[^A-Za-z0-9]+', ' ', 'g'), '^ | $', '', 'g')) AS title_norm,
            publication_year AS year
        FROM read_parquet('{src}', hive_partitioning=false, union_by_name=true)
        WHERE title IS NOT NULL AND length(title) > 0
        """)
    n = con.execute("SELECT count(*) FROM works_identity").fetchone()[0]
    print(f"  {n:,} works with a title in {(time.time()-t0)/60:.1f} min", flush=True)
    t1 = time.time()
    con.execute(
        "CREATE INDEX IF NOT EXISTS idx_title_norm ON works_identity(title_norm)"
    )
    print(f"  exact-title index in {(time.time()-t1)/60:.1f} min", flush=True)
    t2 = time.time()
    con.execute("INSTALL fts; LOAD fts;")
    con.execute(
        "PRAGMA create_fts_index('works_identity', 'id', 'title', stemmer='porter', "
        "stopwords='english', ignore='(\\\\.|[^a-z0-9])+', overwrite=1)"
    )
    print(f"  full-text index in {(time.time()-t2)/60:.1f} min", flush=True)
    con.execute("CHECKPOINT")
    con.close()
    print(
        f"built {db}: {os.path.getsize(db)/1e9:.1f} GB in {(time.time()-t0)/60:.1f} min total"
    )


class IdentityIndex:
    """Candidate retrieval over the local index; matching is the caller's."""

    def __init__(self, db: str = DEFAULT_DB):
        self.con = duckdb.connect(db, read_only=True)
        self.con.execute("LOAD fts;")

    def exact(self, title: str) -> list[dict]:
        key = norm_title(title)
        if not key:
            return []
        rows = self.con.execute(
            "SELECT id, doi, title, year FROM works_identity WHERE title_norm = ? LIMIT 5",
            [key],
        ).fetchall()
        return [
            dict(id=r[0], doi=r[1], title=r[2], publication_year=r[3]) for r in rows
        ]

    def fuzzy(self, title: str, k: int = 5) -> list[dict]:
        q = norm_title(title)
        if not q:
            return []
        rows = self.con.execute(
            """
            SELECT id, doi, title, year
            FROM (SELECT *, fts_main_works_identity.match_bm25(id, ?) AS score FROM works_identity)
            WHERE score IS NOT NULL ORDER BY score DESC LIMIT ?
            """,
            [q, k],
        ).fetchall()
        return [
            dict(id=r[0], doi=r[1], title=r[2], publication_year=r[3]) for r in rows
        ]

    def resolve(self, record: dict) -> tuple[str, str, str]:
        """(identifier, kind, doi) for a record, or ("", "", ""). Exact
        normalised title first, then BM25 candidates; `is_confident_match`
        gates both, so a local index never lowers the bar."""
        title = str(record.get("title") or "")
        for work in self.exact(title) + self.fuzzy(title):
            if not is_confident_match(work, record):
                continue
            doi = str(work.get("doi") or "").strip()
            if doi:
                return doi, "doi", doi
            if work.get("id"):
                return str(work["id"]), "openalex", ""
        return "", "", ""


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build")
    b.add_argument("--mirror", required=True)
    b.add_argument("--db", default=DEFAULT_DB)
    b.add_argument("--threads", type=int, default=8)
    q = sub.add_parser("lookup")
    q.add_argument("title")
    q.add_argument("--year", type=int, default=0)
    q.add_argument("--db", default=DEFAULT_DB)
    a = ap.parse_args()
    if a.cmd == "build":
        build(a.mirror, a.db, a.threads)
        return 0
    ix = IdentityIndex(a.db)
    rec = {"title": a.title, "year": a.year}
    t0 = time.time()
    print("exact :", ix.exact(a.title)[:3])
    print(
        "fuzzy :",
        [(w["doi"], w["title"][:50], w["publication_year"]) for w in ix.fuzzy(a.title)],
    )
    print("resolve:", ix.resolve(rec), f"({(time.time()-t0)*1000:.0f} ms)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
