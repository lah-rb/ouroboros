# Scraper / corpus backlog

*Consolidated 2026-08-13 from live measurement on `~/corpora/ouroboros-spectra`
(5,556 unique papers). Every size here is MEASURED unless marked ESTIMATE —
this pipeline has a history of fixture-shaped estimates collapsing under live
test, so the provenance matters more than the number.*

## The shape of the problem, in one table

| population | count | share | what it needs |
|---|---|---|---|
| unverified (status never determined) | 3,946 | 71.0% | verification throughput |
| closed (no OA route) | 968 | 17.4% | nothing — closed is an access state |
| **oa_unresolved** | **482** | **8.7%** | split below |
| oa_pdf (retrieved) | 170 | 3.1% | — |

Verification runs at **108 papers/hour** and **9.2%** of what it verifies
yields a PDF. So the 3,946 backlog is ~37 h of work worth **~360 PDFs**, and
that single number dwarfs everything in the retrieval section.

### oa_unresolved, split by what actually failed

| bucket | count | share | tractable? |
|---|---|---|---|
| `hard_wall` (403 WAF) | 247 | 51.2% | **no** — see below |
| `landing_page` (page fetched, PDF link not found) | 198 | 41.1% | **yes** |
| `async_pending` (202) | 11 | 2.3% | yes, trivially |
| `other` | 11 | 2.3% | unclassified |
| `wrong_asset` (followed a link to a figure) | 9 | 1.9% | **yes** |
| `gone` (404) | 6 | 1.2% | no |

**NAVIGABLE = 207 (43%).** The fetch SUCCEEDED for these; we just failed to
find the PDF on the page we got. 92 of them are `link.springer.com`.

**The hard wall is Cloudflare and it is not a header problem.** Fingerprinted
2026-08-13: Cloudflare on 69% of a sample, concentrated in Wiley and
ScienceDirect. We already send a browser User-Agent and follow redirects
(fixed July for this reason); MDPI 403s a real Chrome UA too, and follows the
redirect to the same 403. Chasing these means defeating bot detection — out of
scope, and it would risk the polite-crawler standing every other source
depends on. The sanctioned route is the publishers' own text-mining APIs.

---

## Ranked backlog

### 1. Stale-retry sweep — ~47 papers, near-zero effort
`action_resolve_oa_pdf` re-arms a paper only when it finds a location **not
already in `oa_attempted`**, so a URL that failed once is never tried again.
Re-fetching 40 unresolved URLs with our own production headers: **10% now
return a valid PDF** (4/4 of the Springer ones). Those were transient blocks,
and nothing will ever retry them. Needs a periodic clear of `oa_attempted`,
not new code.

### 2. LLM landing-page navigation — ~207 papers, the real build
The request succeeds and returns a real page; our extractor doesn't recognise
the PDF link because every publisher lays it out differently. This is the
shape rigid selectors lose at and inference wins at: fetch page → model
identifies the PDF href → fetch that. **No evasion involved** — we are already
allowed to read the page we were handed.
Scope note: keep it a bounded sub-flow (one page in, one URL out, validated by
PDF magic), not an open browsing loop.

### 3. Verification routing — the 67.5% saving
`_OPENALEX_SELECT` already requests `best_oa_location` and `locations`, so we
**hold the OA status at discovery and don't use it**. `action_resolve_oa_pdf`
calls Unpaywall whenever it has fewer than two URLs — precisely the papers
OpenAlex already said have no OA location. Two levels:
* **skip/defer** the Unpaywall call when OpenAlex reports no OA location —
  pure saving, no behaviour change, no breadth loss;
* **prioritise** verification by OA likelihood so PDFs arrive sooner.

### 4. Manual worklist + ingest — SHIPPED 2026-08-13
`tools/oa_triage.py`. Classifies the buckets above, emits a host-grouped TSV
of the 207 navigable rows, and ingests hand-downloaded PDFs back into the
workspace (`<paper_key>.pdf` → filed, `access_status: oa_pdf`,
`retrieval_method: manual`). Ingest REFUSES while a mission is running,
because `append_records` rewrites papers.jsonl wholesale and a concurrent
write loses one side entirely.

### 5. Publisher TDM tokens — ~180 papers, needs credentials
Wiley and Elsevier both issue free text-mining tokens that serve OA content
over a supported route. That is the legitimate answer to the two publishers
holding most of the 247 hard-walled papers. Operator/credential work, not code.

### 6. Discovery OA filter — 3.5x yield, corpus-scope decision
Measured on one aspect query: papers with a retrievable OA location go
**22% → 92%** of the page under `is_oa:true,has_fulltext:true`, at a cost of
**6%** of matching corpus (13,516 → 12,652). Downstream fetch success is
UNCHANGED (~17% in every arm) — the filter doesn't beat the wall, it stops us
discovering papers that were never going to yield.
CAVEAT: the 22/78/92 counts are solid (out of 50); the end-to-end yield figures
rest on 12 fetch attempts per arm and are directional only.
This stops cataloguing closed papers as metadata, so it is a scope call.

### 7. Alt-host tier — ~26 papers, low priority
OpenAlex `locations[]` on non-publisher hosts + Europe PMC recovered **1 of 18**
(6%) on live test. Worth wiring as a last resort; not worth prioritising.

---

## Correctness / integrity items

### 8. `papers.jsonl` line count ≠ paper count
It is an append-only log with last-record-wins on read; **24% of lines are
rewrites**. `wc -l` overstated the corpus by that much and did so in every
progress report until it was caught. Any tooling or check-in counting lines
needs `read_records()`-style dedupe.

### 9. Nothing claims a paper before extraction
`extraction_status` is written only AFTER a batch finishes, so two OCR lanes
would extract the same PDFs twice. This is why `_OCR_CONCURRENCY` is pinned at
1 — not GPU saturation (OCR against text decode overlaps at serialization
0.342). A claim/lease is the prerequisite for any multi-lane extraction.

### 10. `append_records` is a read-modify-write of the whole file
Despite the name. Making it a true `O_APPEND` single-line write would turn
`databank/*.jsonl` into the multi-writer LWW-map its read path already assumes,
make the papers/extraction split an optimisation rather than a correctness
requirement, and let the ingest tool run against a live mission.

### 11. Duplicate PDF targets
173 records pointed at 157 unique PDFs (15 shared by 31 records) — recorded
earlier, unfixed.

---

## Pipeline / quality items

### 12. Gate calibration
24 of 48 extraction rejections were within 0.05 of a gate. Whether 0.85/0.75 is
right for this corpus is an operator call — it sets what enters the dataset.

### 13. Translation pass never written
The `language` field exists to route a reviewer-flagged translation over the
markdown; the pass was never built AND the field comes back empty. Do not
translate inside `extract_batch`.

### 14. Snowballing continues after discovery completes
Discovery goals are complete but reference-chasing keeps expanding the
candidate pool during catalog, which feeds the 3,946 unverified backlog faster
than verification drains it.

### 15. Measured-and-closed (do not re-open without new evidence)
* `cache hit-rate 0%` — cosmetic. Prefill is 10.4 min of a 540 min run; perfect
  caching saves under 2%.
* Retry-the-same-URL-immediately — no yield. (The STALE retry in item 1 is a
  different thing: weeks later, after transient blocks lift.)
* MDPI as "a source to add" — we already request the exact canonical PDF URL
  and get 403. There is no source to add.
