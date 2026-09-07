#!/usr/bin/env python3
"""Build the discovery + enrichment tables from the OpenAlex mirror.

WHY (2026-09-04). The operator is weighing a flip of the whole scraping
process to local bucket mirrors. The identity table
(dev/openalex_identity_index.py) answers "does this title exist, what is
its DOI"; the mission's remaining API traffic wants MORE of each work --
page extent and license (enrichment), topics/keywords/OA status (search
discovery), and the reference list (snowball). This pulls those columns
into two tables:

  works_meta  (SSD, ~50 GB)  id, doi, year, type, language, primary topic
              + subfield/field names, topic ids, keywords, OA status/url,
              has_fulltext, cited_by_count, best OA pdf/license/version,
              first/last page.  No title: join works_identity on id.
  works_refs  (HDD, ~35 GB)  id, referenced_works, locations' pdf_urls.
              Batch-only: a full scan of this table on the USB spindle is
              minutes, which is fine for "who cites these 2,000 papers".

Abstracts are NOT included: abstract_inverted_index is 53% of the mirror's
bytes and a pass over it is hours on this disk. If topic-filtered discovery
proves out, a second pass can fetch abstracts for the filtered subset only.

    python dev/openalex_meta_index.py build --mirror /media/lah-rb/openalex
"""

from __future__ import annotations

import argparse
import glob
import os
import shutil
import time

import duckdb

META_DB = os.path.expanduser("~/corpora/openalex_meta.duckdb")
REFS_DB = "/media/lah-rb/openalex/openalex_refs.duckdb"

_META_SQL = """
CREATE OR REPLACE TABLE works_meta AS
SELECT
    regexp_extract(id, 'W\\d+') AS id,
    replace(doi, 'https://doi.org/', '') AS doi,
    publication_year AS pub_year,
    "type" AS work_type,
    "language" AS lang,
    primary_topic.id AS ptopic,
    primary_topic.display_name AS ptopic_name,
    primary_topic.subfield.display_name AS subfield,
    primary_topic."field".display_name AS field_name,
    list_transform(topics, t -> t.id) AS topic_ids,
    list_transform(keywords, k -> k.display_name) AS kw,
    open_access.is_oa AS is_oa,
    open_access.oa_status AS oa_status,
    open_access.oa_url AS oa_url,
    has_fulltext,
    cited_by_count,
    best_oa_location.pdf_url AS oa_pdf,
    best_oa_location.license AS lic,
    best_oa_location.version AS oa_version,
    biblio.first_page AS fp,
    biblio.last_page AS lp
FROM read_parquet('{src}', hive_partitioning=false, union_by_name=true)
"""

_REFS_SQL = """
CREATE OR REPLACE TABLE works_refs AS
SELECT
    regexp_extract(id, 'W\\d+') AS id,
    referenced_works,
    list_transform(locations, l -> l.pdf_url) AS loc_pdfs
FROM read_parquet('{src}', hive_partitioning=false, union_by_name=true)
WHERE len(referenced_works) > 0 OR len(locations) > 0
"""


def _free_gb(path: str) -> float:
    return shutil.disk_usage(os.path.dirname(path) or ".").free / 1e9


def build(mirror: str, threads: int, meta_db: str, refs_db: str, skip_refs: bool):
    src = os.path.join(mirror, "data", "parquet", "works", "**", "*.parquet")
    if not glob.glob(src, recursive=True):
        raise SystemExit(f"no parquet under {src}")
    # Headroom guards: the estimates come from single-partition probes
    # (76 GB with title, 111 GB with refs); refuse rather than fill a disk.
    if _free_gb(meta_db) < 70:
        raise SystemExit(f"{_free_gb(meta_db):.0f} GB free at {meta_db}: need 70")
    if not skip_refs and _free_gb(refs_db) < 60:
        raise SystemExit(f"{_free_gb(refs_db):.0f} GB free at {refs_db}: need 60")
    for db, sql, table in (
        (meta_db, _META_SQL, "works_meta"),
        (refs_db, _REFS_SQL, "works_refs"),
    ):
        if table == "works_refs" and skip_refs:
            continue
        con = duckdb.connect(db)
        con.execute(f"PRAGMA threads={threads}")
        con.execute("PRAGMA memory_limit='24GB'")
        con.execute("PRAGMA preserve_insertion_order=false")
        t0 = time.time()
        print(f"[{time.strftime('%H:%M:%S')}] building {table} -> {db}", flush=True)
        con.execute(sql.format(src=src))
        n = con.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        con.execute("CHECKPOINT")
        con.close()
        print(
            f"[{time.strftime('%H:%M:%S')}] {table}: {n:,} rows, "
            f"{os.path.getsize(db)/1e9:.1f} GB, {(time.time()-t0)/60:.0f} min",
            flush=True,
        )


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build")
    b.add_argument("--mirror", required=True)
    b.add_argument("--threads", type=int, default=8)
    b.add_argument("--meta-db", default=META_DB)
    b.add_argument("--refs-db", default=REFS_DB)
    b.add_argument("--skip-refs", action="store_true")
    a = ap.parse_args()
    build(a.mirror, a.threads, a.meta_db, a.refs_db, a.skip_refs)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
