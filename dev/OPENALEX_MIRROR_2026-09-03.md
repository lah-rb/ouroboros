# OpenAlex local mirror — escaping the API meter

## Why

OpenAlex moved its API to a paid credit meter (measured 2026-09-03 against the
live endpoint): **$0.10/day free, 1,000 credits, 10 credits a search → ~100
searches a day**, resetting at midnight UTC. A polite-pool `mailto` does not
change it — the meter is an account/IP budget. The mission's own discovery
traffic spends the allowance before noon, after which every call returns 429
"Insufficient budget", and the pacer backs `api.openalex.org` off from 0.6 s
to 4.8 s absorbing the refusals.

Identity resolution (title+year → DOI) for the 332 DOI-less accepted/pending
papers needs a few hundred such lookups; Crossref is free but does not index
the institutional theses that make up most of that pool (13 DOIs from 169
candidates, every near-miss correctly refused). The snapshot is CC0.

## The bucket

`s3://openalex`, public, **unsigned reads, no requester-pays**. Layout:

    data/parquet/<entity>/updated_date=YYYY-MM-DD/part_NNNN.parquet
    data/jsonl/<entity>/...          (same content, JSON Lines)
    legacy-data/                     (ignored)

Parquet tree: **783.6 GB in 5,224 objects** — works 725.0, authors 52.8,
awards 5.5, everything else under 0.3. Refreshed ~monthly; partitioned by
`updated_date`, so a re-sync is a delta.

A works partition: ~400k rows, ~600 MB, 49 columns. `abstract_inverted_index`
is **53% of the bytes**; the four identity columns (`id`, `doi`, `title`,
`publication_year`) are **6.5% ≈ 47 GB** over the whole entity. 76% of works
carry a DOI. Measured single-stream read from this machine: 30 MB/s.

## The disk

Operator attached a Seagate FreeAgent GoFlex, 931.5 GB, USB 3 (5 Gbit/s),
holding 139 GB of personal files — shown to the operator, wiped on their
explicit "format it". ext4, label `openalex`, udisks auto-mounts it at
**`/media/lah-rb/openalex`**; 5% reserved leaves 870 GB, ~86 GB headroom over
the tree.

**The disk is the bottleneck, not the network:** 97% busy at 31–50 MB/s
writes with 8 streams; the Wi-Fi link (`wlo1`) carried 57 MB/s with headroom.
Relaunched at 3 streams to cut head thrash. Realistic pull time 4–5 h.

Formatting without sudo took three attempts: this udisks CLI has **no
`format` verb**; brew's `gdbus` **cannot reach the system bus**; systemd's
`busctl` calling `org.freedesktop.UDisks2.Block.Format` works (polkit allows
the console user). A failed attempt followed by `udisksctl mount` silently
re-mounts the OLD filesystem — verify FSTYPE/LABEL after every step.

## Tooling

- `dev/openalex_snapshot_sync.py --dest /media/lah-rb/openalex [--workers 3]`
  — boto3 unsigned, N streams, `.part` → rename so a crash never leaves a
  truncated file looking finished, skips size-matched files. **Re-running is
  the monthly refresh.**
- `dev/openalex_identity_index.py build --mirror /media/lah-rb/openalex` →
  DuckDB at `~/corpora/openalex_identity.duckdb` **on the SSD** (the FTS index
  over ~half a billion titles is tens of GB; the HDD is ~85% full once the
  mirror lands). Exact normalised-title key + BM25 full-text. Retrieval only:
  `is_confident_match` (agent/actions/identifiers.py) still decides, so the
  local index removes the meter, not the judgement.
- `dev/resolve_identifiers.py --via local` (now the default; `crossref` and
  `openalex` remain by flag).

Tested on one 400k-row partition: exact hit through case/punctuation/hyphen
mangling; fuzzy top-1 on a reworded title; ~230 ms a lookup; a two-word
difference ("optimization" vs "optimisation" + a dropped word) scored 0.714
and was correctly refused at the 0.72 bar.

## Status

2026-09-03 19:58 — mirror running, 3 streams. Index build and the
re-resolution of the 160 CORE-tier papers follow completion.
