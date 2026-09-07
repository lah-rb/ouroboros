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

    NO SECONDARY INDEXES, DELIBERATELY. A B-tree over 502M title strings
    exhausted a 24 GB budget and died (measured 2026-09-04), and an FTS index
    over the same column would be far larger. It is also unnecessary: the
    table is columnar, so an equality filter on `title_norm` is a single
    column scan -- **1.5 s warm, 3.5 s cold over 502M rows** -- and the real
    workload is a few hundred titles at a time, which `resolve_batch` answers
    in ONE scan rather than one scan each.

    NO DEDUPE. Partitions are keyed by `updated_date` and a work lives in
    exactly the partition of its latest update -- verified over 30 partitions
    / 11.78M rows, zero repeated ids.

    Measured on the full mirror: 502,347,343 works with a title, 45 min scan,
    57 GB of database. Do NOT run while the mirror is syncing -- both hammer
    the same USB spindle.
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
    con.execute("PRAGMA preserve_insertion_order=false")
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
    con.execute("CHECKPOINT")
    con.close()
    print(
        f"built {db}: {n:,} works, {os.path.getsize(db)/1e9:.1f} GB in {(time.time()-t0)/60:.1f} min"
    )


class IdentityIndex:
    """Candidate retrieval over the local snapshot; matching is the caller's.

    Exact normalised-title equality only. There is no fuzzy tier: BM25 over
    502M titles needs an index this scale cannot afford, and the matcher
    already refuses partial titles without a corroborating year, so a fuzzy
    tier would mostly feed it candidates it would reject.
    """

    def __init__(self, db: str = DEFAULT_DB, threads: int = 8, memory: str = "16GB"):
        self.con = duckdb.connect(db, read_only=True)
        self.con.execute(f"PRAGMA threads={threads}")
        self.con.execute(f"PRAGMA memory_limit='{memory}'")

    def exact(self, title: str, limit: int = 5) -> list[dict]:
        key = norm_title(title)
        if not key:
            return []
        rows = self.con.execute(
            "SELECT id, doi, title, year FROM works_identity WHERE title_norm = ? LIMIT ?",
            [key, limit],
        ).fetchall()
        return [
            dict(id=r[0], doi=r[1], title=r[2], publication_year=r[3]) for r in rows
        ]

    def resolve(self, record: dict) -> tuple[str, str, str]:
        """(identifier, kind, doi) for one record, or ("", "", "")."""
        for work in self.exact(str(record.get("title") or "")):
            if not is_confident_match(work, record):
                continue
            doi = str(work.get("doi") or "").strip()
            if doi:
                return doi, "doi", doi
            if work.get("id"):
                return str(work["id"]), "openalex", ""
        return "", "", ""

    def resolve_batch(
        self, records: dict[str, dict]
    ) -> dict[str, tuple[str, str, str]]:
        """{key: (identifier, kind, doi)} for many records in ONE table scan.

        A scan costs the same whether it answers one title or a thousand, so
        the batch form is what makes a 57 GB local table cheaper than an API
        call per paper. Candidates are gated by `is_confident_match` exactly
        as the single-record path is.
        """
        wanted = {}
        for k, rec in records.items():
            key = norm_title(rec.get("title"))
            if key:
                wanted.setdefault(key, []).append(k)
        if not wanted:
            return {}
        self.con.execute("CREATE OR REPLACE TEMP TABLE _probe (title_norm VARCHAR)")
        self.con.executemany("INSERT INTO _probe VALUES (?)", [[k] for k in wanted])
        rows = self.con.execute("""
            SELECT w.title_norm, w.id, w.doi, w.title, w.year
            FROM works_identity w JOIN _probe p USING (title_norm)
            """).fetchall()
        cands: dict[str, list[dict]] = {}
        for tn, wid, doi, title, year in rows:
            cands.setdefault(tn, []).append(
                dict(id=wid, doi=doi, title=title, publication_year=year)
            )
        out: dict[str, tuple[str, str, str]] = {}
        for tn, keys in wanted.items():
            for k in keys:
                rec = records[k]
                for work in cands.get(tn, []):
                    if not is_confident_match(work, rec):
                        continue
                    doi = str(work.get("doi") or "").strip()
                    out[k] = (
                        (doi, "doi", doi) if doi else (str(work["id"]), "openalex", "")
                    )
                    break
        return out


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
    print("resolve:", ix.resolve(rec), f"({(time.time()-t0)*1000:.0f} ms)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
