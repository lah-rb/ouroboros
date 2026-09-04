# Can the OpenAlex mirror replace the API for discovery and enrichment?

**Question (operator, 2026-09-04).** The whole scraping process may flip to
local bucket mirrors. Measure how far the downloaded OpenAlex snapshot
substitutes for the two things the mission still asks the API for: finding
candidate papers (discovery) and filling metadata on papers we hold
(enrichment). Everything below is measured against the curator's own
verdicts — 1,948 accepted, 1,789 denied — not against the API's answers.

## The substrate

Snapshot cut: `updated_date=2026-06-26` (483 partitions, 725 GB of works
parquet). Three DuckDB tables derived from it, no indexes:

| table | rows | size | where | build |
|---|---|---|---|---|
| `works_identity` (id, doi, title, title_norm, year) | 502.3M | 57 GB | SSD | 45 min |
| `works_meta` (topics, keywords, OA status/pdf, license, pages, language, type, cited_by) | 510.4M | 48.6 GB | SSD | 115 min |
| `works_refs` (referenced_works, location pdf urls) | — | — | HDD | pending |

The HDD delivers **~13 MB/s on projected column reads** regardless of thread
count (all 8 DuckDB threads in disk wait); the wide `referenced_works` and
`locations` columns read several times faster because they are contiguous.
A pass over the abstract column (30% of bytes) is roughly 4.5 h at the
slower rate. Column-equality scans over the SSD tables answer in 1.5–4 s.

## Coverage: the snapshot has the corpus

| group | with DOI | found by DOI | no identifier at all | resolved by exact title |
|---|---|---|---|---|
| accepted | 1,766 | 1,740 (99%) | 122 | 86 (70%) |
| denied | 1,432 | 1,397 (98%) | 226 | 130 (58%) |
| unreviewed | 8,867 | 8,747 (99%) | 2,505 | 1,442 (58%) |

Of the 26 accepted DOIs the snapshot lacks, 16 were published after the
cut, 6 carry a trailing period from our own DOI normaliser (23 corpus
records do; `scholarly_actions.py:1339` strips it on one path only), 2 are
pre-1975. Papers that reached us through Semantic Scholar or CORE (47% of
accepted) are in the snapshot just the same.

## Discovery

What the API did: `works?search=<query>&filter=is_oa:true,has_fulltext:true`
— relevance search over title, abstract and full text, one call per query
per language (1,872 refined queries, 36% non-Latin script, 57% zero-hit,
16,701 hits total) — plus `works?filter=openalex_id:…` for the citation
snowball. The snapshot has no text index; it has **topics** (on 54% of
works, ~100% of articles), **keywords** (85%) and **titles** (all).

### Topics: the strong local route, and what it cannot see

The accepted set's primary topics have a long tail: the top three
(Cultural Heritage Materials Analysis 17%, Laser-induced spectroscopy and
plasma 9%, Planetary Science 6%) cover a third; 17 topics cover half the
primaries, 93 cover 80%, 186 cover 90%. Matching a work if ANY of its three
topics is in the set:

| topic set | recall, accepted | denied also caught | pool | with OA pdf |
|---|---|---|---|---|
| 17 topics | 64% | 39% | 10.3M | 0.84M |
| 93 topics | 90% | 72% | 48.1M | 5.2M |
| 186 topics | 96% | 80% | 66.7M | 7.8M |

Per aspect, the 17-topic set recalls 76–78% of Raman/FTIR heritage and LIBS
acceptances but 50–53% of XRD and XRF ones and 26% of mineral-formation
ones — the aspect→topic map must be per aspect. Language mix of the pool is
71% English then Japanese, Spanish, German, French, Russian, Portuguese:
topics are language-agnostic, which the API's text search never was.

**Topics do not carry the curator's verdict.** Denied papers are as
on-topic as accepted ones (Planetary Science is the *top* denied topic).
Topics replace the search, not the review. And 10M–48M works is not a
candidate list; a second layer must cut it by two orders of magnitude.

### Title terms: the weak second layer

Re-running the mission's 640 distinct Latin-script term sets against
titles (match = at least half the stemmed terms present, minimum two):

| rule | recall accepted | recall denied |
|---|---|---|
| ≥2 terms in title | 51% | 33% |
| ≥ half of terms | 36% | 22% |
| ≥3 terms | 18% | 8% |

Combined with a topic pool, the union of all term sets:

| topic set ∧ title terms | pool | with OA pdf | recall accepted | denied caught | accepted, OA pdf required |
|---|---|---|---|---|---|
| 17 topics | 1.19M | 53k | 28% | 12% | 22% |
| 93 topics | 1.64M | 126k | 33% | 20% | 27% |

Widening the topic set from 17 to 93 adds only 5 points of recall for a 38%
larger pool: the title layer, not the topic layer, is the binding
constraint. Titles alone lose about half of what abstract/full-text search
found — the expected cost of not having the abstract column.

### Abstracts: the missing layer, costed

`abstract_inverted_index` is 53% of the mirror's bytes. A field-restricted
subset (Earth & Planetary, Materials, Chemistry, Physics, Arts & Humanities,
Chemical Engineering) is **48M works (9%), 11M with an OA pdf**: one ~4.5 h
pass to extract, then a scan-per-query table rather than an FTS index (a
B-tree over 502M strings already exhausted 24 GB). Not built; build it if the
topic route is adopted, because it is what recovers the other half.

## Enrichment: a JOIN, not a request

`dev/openalex_local_enrich.py` fills EMPTY fields from `works_meta`, stamps
each with a `*_source` naming the snapshot, and appends the snapshot's best
OA pdf as a new untried location. Dry run:

| scope | records | in snapshot | pages | language | openalex_id | license | new OA location |
|---|---|---|---|---|---|---|---|
| reviewed + held | 3,802 | 3,735 | 646 | 1,112 | 1,345 | 100 | 1 |
| everything with a DOI | 12,066 | 11,900 | 5,032 | 4,577 | 4,805 | 283 | 178 |

License is the one field the snapshot is poor at — ~2% of works carry one
— so the 797 unlicensed accepted papers mostly stay unlicensed. Page
extents (98% of works) and languages fill.

## Acquisition leads

Of 8,288 unacquired candidates found by DOI, the snapshot lists an OA pdf
for 1,823; **228 are URLs the scraper never tried** (98 green, 72 bronze, 29
gold): 62 on hdl.handle.net, 46 Wiley, 13 MDPI, 8 Elsevier CDN, 5 Zenodo.
Their prior failures were mostly "response is text/html" landing pages.
Separately, 419 records we flagged `closed` are OA in the snapshot (259
green). List: `~/tmp/openalex_mirror_oa_leads.json`.

## What the mirror does not replace

- **Freshness.** Two months behind at the cut; 16 of the 26 accepted misses.
  A monthly delta re-sync closes it; the table rebuilds are ~2 h each.
- **Text search.** Until the abstract subset exists, titles carry half.
- **The other sources.** Historically: Semantic Scholar 4,048 calls, Unpaywall
  3,980, CORE 2,312, Europe PMC 1,588 (in the last 16 h: OpenAlex 421, nothing
  else — discovery goals are complete and acquisition is dry). CORE-only
  theses (15% of accepted) are *in* the snapshot by DOI/title but reached us
  through CORE's full-text search.
- **Fetching.** Publisher and repository downloads are network by nature.

## The other sources, and their mirrors (verified 2026-09-04)

Six of the scraper's seven external sources have bulk data; the seventh —
fetching PDFs from publishers and repositories — does not, beyond the share
the full-text corpora already parsed.

| source | role here | share of accepted | bulk mirror | size / terms |
|---|---|---|---|---|
| OpenAlex | search, snowball, enrichment, OA locations | 45% | mirrored | 784 GB, CC0, public S3 |
| Unpaywall | OA locations | acquisition only | inside the OpenAlex snapshot | its own snapshots ended 2022; now served from OpenAlex data |
| Semantic Scholar | title/abstract search, external ids, OA pdf, references | 32% (S2-only) | Datasets API, release 2026-09-01 | papers 200M/45 GB, abstracts 100M/54 GB, citations 2.4B/255 GB, paper-ids 15 GB, S2ORC v2 16M full texts/180 GB; listing public, downloads need a free API key (401 without) |
| CORE | repository full-text search (theses), downloads | 15% (CORE-only) | CORE dataset, registration by email | 291M records, 32.8M full texts (2023), latest dump 2024, size unpublished |
| Crossref | title→DOI fallback, biblio | resolution only | 2026 public data file | ~180M records, 208 GB compressed; torrent or S3 requester-pays (~$18) |
| Europe PMC | OA pdf by PMCID | 6% yield | FTP open-access subset | 1,281 XML files, 165 GB compressed, weekly; PDFs on FTP |
| doi.org, publishers, repositories, Wayback | the fetch | all acquisition | none | S2ORC / CORE / Europe PMC texts cover the OA share they parsed |

Two consequences. The **S2 abstracts dataset (54 GB, joins by DOI) is the
cheap abstract layer** — versus a ~4.5 h pass over OpenAlex's inverted index
— and would lift local discovery past the title-only ceiling. The **CORE
dataset is the only mirror that reaches the repository theses** behind the
CORE-only 15% of acceptances. Storage: a metadata-only flip adds ~1.5 TB on
top of the OpenAlex drive (133 GB free); S2ORC adds 180 GB; CORE full texts
are terabytes. A 4 TB drive covers everything but CORE's full text.

## Citation snowball: the discriminating local route

`works_refs` (id, referenced_works) answers both directions of the citation
graph with two scans — the outgoing one cold from the HDD in 14 min, the
incoming one 9 s once the 33.5 GB table sits in page cache — where the API
path made one call per 50 ids and Semantic Scholar's references endpoint
429'd on most of the corpus.

| one hop from the 1,757 accepted papers | works | with OA pdf | since 2015 |
|---|---|---|---|
| works they cite | 50,501 | 10,339 | 16,837 |
| works citing ≥1 of them | 144,652 | 40,169 | 121,623 |
| works citing ≥2 of them | 14,493 | 4,880 | 12,874 |

Recall against the verdicts: **45% of accepted papers are one hop from
another accepted paper** (29% cited by one, 32% citing one) against 30% of
denied papers (24% / 17%). Topics separated accepted from denied at a ratio
of 1.6 with a 10M pool; citation proximity separates at 1.5 with a pool two
orders of magnitude smaller, and the ≥2-citers pool (14.5k works) is the
size of the mission's entire historical candidate list. The corpus's own
biblio snowball converted 164 acceptances from 1,872 candidates (8.8%);
this is the same mechanism without the meter, at any depth.

## Verdict

The snapshot replaces the API for **identity** (99% of DOIs, 58–70% of
no-id papers by exact title), **enrichment** (page extents, languages,
OpenAlex ids; not licenses), **OA locations** (Unpaywall is inside it) and
the **citation snowball** (two scans, no meter). It replaces text-search
**discovery** only partly: topics narrow 510M to 10M but do not carry the
curator's verdict; titles recover half of what abstract search found. A
local discovery loop that would stand on its own is the citation
neighbourhood of the accepted set, unioned with topics ∧ text, where the
text layer comes cheapest from Semantic Scholar's 54 GB abstracts dataset
rather than a 4.5 h pass over OpenAlex's inverted index. Everything above
except the fetch of the PDF itself can be local.

Two engineering notes from the run: DuckDB materialised a FROM-clause
`UNNEST` join over 10 billion reference rows and spilled >100 GB of temp
into the process's cwd until the SSD was full (the mission survived; the
temp cleaned itself on exit) — keep `unnest()` in the projection so it
streams into the semi-join, and set `temp_directory` to the HDD before any
large join. And keep the refs table warm: the 9 s incoming scan was from
page cache; cold it is a quarter hour.
