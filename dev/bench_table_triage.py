#!/usr/bin/env python3
"""Can muse triage OCR'd tables against the page image? (2026-10-02)

WHY. The table audit (48 tables, 4 reviewers against page images) found the
OCR's digits mostly right but the STRUCTURE often wrong: 26 of 48 tables put
values under the wrong header or row -- most often the header row loses the
label column's cell and every heading slides one column left -- and 3 of 8
tables whose every row matched the PDF text were still misattributed. Only the
page image shows it. Operator: tables may need a triage step from muse after
creation. This bench asks muse, per table, with the page image and the OCR's
HTML: is every value under its right header and row? if not, return the
corrected table -- moving cells only, never changing a number.

    .venv/bin/python dev/bench_table_triage.py pages    # find + render the true pages
    .venv/bin/python dev/bench_table_triage.py run [--limit N]
    .venv/bin/python dev/bench_table_triage.py score

Inputs: the audit's table_batch_*.json / table_result_*.json (reviewer labels)
under ~/tmp/table_triage/audit. Outputs beside them.
"""

from __future__ import annotations

import argparse
import base64
import collections
import html as htmlmod
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # agent.table_triage

ROOT = Path(os.path.expanduser("~/corpora/ouroboros-spectra"))
OUT = Path(os.path.expanduser("~/tmp/table_triage"))
AUDIT = OUT / "audit"
LLMVP = os.environ.get("OUROBOROS_LLMVP_URL", "http://127.0.0.1:8008").rstrip("/")
DPI = 130
NUM = re.compile(r"(?<![\d.,])\d+(?:[.,]\d+)?(?![\d])")

PROMPT = """The image is one page of a scientific paper. Below is an HTML transcription of ONE table on this page, made by an OCR model. Such transcriptions usually read the digits correctly but often get the table's STRUCTURE wrong:
- the header row loses its first cell (the header of the row-label column), so every header sits one column to the LEFT of its values, with an empty cell added at the end;
- a column is dropped, merged or duplicated, or an empty cell is dropped so the rest of the row slides left;
- headers spanning several rows or columns, or group labels spanning several rows, are misplaced;
- two values stacked in one printed cell are fused into one string;
- the last column of a wide table is missing.

Find this table in the image and compare it with the transcription, row by row and column by column.
If every value already sits under its correct column header and on its correct row, answer {{"verdict": "ok"}}.
If not, return the corrected table: move cells, insert empty cells, fix header text and spans, split fused stacked values. NEVER change any number or word in the transcription and NEVER add a value that is not in the transcription. If the structure cannot be repaired from the transcription (values missing, digits wrong), answer "unfixable" and say why.

Answer with ONE JSON object only:
{{"verdict": "ok" | "fixed" | "unfixable", "problems": ["one line per problem found"], "html": "<table>...</table>"}}
("html" only when the verdict is "fixed").

<transcription>
{table}
</transcription>"""

_VISION = """
mutation VisionCompletion($request: VisionCompletionRequest!) {
    visionCompletion(request: $request) { text generatedTokens promptTokens decodeMs }
}
"""


def slim(table_html: str) -> str:
    """The OCR's HTML without per-cell style noise (spans kept)."""
    t = re.sub(r"\s+style=(['\"]).*?\1", "", table_html)
    t = re.sub(r"\s+border=\d+", "", t)
    return t


def numbers(table_html: str) -> collections.Counter:
    text = htmlmod.unescape(re.sub(r"<[^>]+>", " ", table_html))
    return collections.Counter(n.replace(",", ".") for n in NUM.findall(text))


def digits_check(before: str, after: str) -> dict:
    """Did a correction keep every number? Splitting a fused cell is allowed:
    a number that disappeared must be the concatenation of numbers that appeared."""
    a, b = numbers(before), numbers(after)
    missing = a - b
    extra = b - a
    unexplained = []
    pool = list(extra.elements())
    for m in missing.elements():
        hit = None
        for i in range(len(pool)):
            for j in range(i + 1, len(pool)):
                if pool[i] + pool[j] == m or pool[j] + pool[i] == m:
                    hit = (i, j)
                    break
            if hit:
                break
        if hit:
            for k in sorted(hit, reverse=True):
                pool.pop(k)
        else:
            unexplained.append(m)
    return {
        "preserved": not unexplained and not pool,
        "missing": unexplained[:10],
        "added": pool[:10],
    }


# ── pages ─────────────────────────────────────────────────────────────


def _toks(s: str) -> collections.Counter:
    return collections.Counter(
        t.replace(",", ".") for t in NUM.findall(s.replace("−", "-")) if len(t) >= 2
    )


def _words(s: str) -> collections.Counter:
    return collections.Counter(w.lower() for w in re.findall(r"[^\W\d_]{4,}", s))


def cmd_pages(a) -> int:
    items = [i for b in sorted(AUDIT.glob("table_batch_*.json")) for i in json.loads(b.read_text())]
    pinned = dict(p.split("=") for p in a.page) if a.page else {}
    papers = {}
    for line in (ROOT / "databank" / "papers.jsonl").read_text().splitlines():
        try:
            d = json.loads(line)
            papers[d["paper_key"]] = d
        except (ValueError, KeyError):
            pass
    out = []
    (OUT / "pages").mkdir(parents=True, exist_ok=True)
    for it in items:
        table = Path(it["ocr_table"]).read_text()
        want = _toks(re.sub(r"<[^>]+>", " ", table))
        # A table of words (ragged-03: no numbers at all) matched every page
        # at zero and landed on the cover; fall back to its longer words.
        if sum(want.values()) < 5:
            want = _words(re.sub(r"<[^>]+>", " ", table))
        pdf = ROOT / papers[it["paper_key"]]["pdf_path"]
        text = subprocess.run(["pdftotext", "-layout", str(pdf), "-"],
                              capture_output=True, text=True).stdout  # fmt: skip
        pages = text.split("\f")
        bag = _toks if any(any(c.isdigit() for c in w) for w in want) else _words
        scores = [sum((bag(p) & want).values()) for p in pages]
        page = max(range(len(pages)), key=lambda i: scores[i]) + 1 if pages else 1
        # A reviewer's page beats the bag match (ragged-02: two tables on
        # facing pages share most numbers).
        page = int(pinned.get(it["id"], page))
        png = OUT / "pages" / f"{it['id']}"
        subprocess.run(["pdftoppm", "-r", str(DPI), "-png", "-f", str(page), "-l", str(page),
                        "-singlefile", str(pdf), str(png)], capture_output=True)  # fmt: skip
        moved = page != it.get("page")
        out.append({**it, "page": page, "page_image": str(png) + ".png", "page_moved": moved,
                    "page_score": scores[page - 1] if pages else 0})  # fmt: skip
        print(f"{it['id']:<12} page {it.get('page')} -> {page}{'  (moved)' if moved else ''}", flush=True)
    (OUT / "items.json").write_text(json.dumps(out, indent=1))
    print(f"{sum(o['page_moved'] for o in out)} of {len(out)} pages corrected")
    return 0


# ── run ───────────────────────────────────────────────────────────────


def vision(
    prompt: str, png: Path, max_tokens: int, *, reasoning: str | None = None, temperature: float | None = 0.0
) -> dict:
    uri = "data:image/png;base64," + base64.b64encode(png.read_bytes()).decode()
    req = {"prompt": prompt, "images": [{"url": uri}], "maxTokens": max_tokens}
    if temperature is not None:
        req["temperature"] = temperature
    if reasoning:
        req["reasoning"] = reasoning
    r = urllib.request.Request(
        f"{LLMVP}/graphql",
        data=json.dumps({"query": _VISION, "variables": {"request": req}}).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(r, timeout=1800) as resp:
        out = json.loads(resp.read())
    if out.get("errors"):
        raise RuntimeError(str(out["errors"][0].get("message")))
    return out["data"]["visionCompletion"]


def parse(text: str) -> dict | None:
    """The LAST JSON object carrying a verdict. The vision path returns
    muse's reasoning with the answer (it does not split them), and the
    reasoning drafts verdicts of its own: missing-06 mused '"verdict":
    "fixed"' and answered {"verdict":"ok"}."""
    dec = json.JSONDecoder()
    # LaTeX in a JSON string ("$\\pm$") is an invalid escape: each candidate
    # is retried with every backslash that starts no JSON escape doubled,
    # BEFORE an earlier candidate (a reasoning draft) is considered.
    for pos in reversed([m.start() for m in re.finditer(r"\{", text)]):
        tail = text[pos:]
        for src in (tail, re.sub(r'\\(?!["\\/bfnrtu])', r"\\\\", tail)):
            try:
                d, _ = dec.raw_decode(src)
            except ValueError:
                continue
            if isinstance(d, dict) and "verdict" in d:
                return d
    # HTML inside a JSON string often breaks it (an unescaped quote): take
    # the LAST verdict and, for a fix, the LAST table -- the answer comes
    # after any deliberation, never before it.
    verdicts = re.findall(r'"verdict"\s*:\s*"(\w+)"', text)
    if not verdicts:
        return None
    out = {"verdict": verdicts[-1], "problems": ["(JSON invalid; fields recovered)"]}
    if verdicts[-1] == "fixed" and "<table" in text:
        tail = text[text.rindex("<table"):]
        end = tail.find("</table>")
        out["html"] = tail[: end + len("</table>")] if end != -1 else ""
    return out


def _one(it: dict, max_tokens: int) -> dict:
    table = slim(Path(it["ocr_table"]).read_text())
    t0 = time.time()
    try:
        r = vision(PROMPT.format(table=table), Path(it["page_image"]), max_tokens)
        d = parse(r.get("text") or "")
        row = {"id": it["id"], "stratum": it["stratum"], "seconds": round(time.time() - t0),
               "gen": r.get("generatedTokens"), "verdict": (d or {}).get("verdict", "unparsed"),
               "problems": (d or {}).get("problems", []), "html": (d or {}).get("html", ""),
               "raw": r.get("text") or "", "raw_tail": (r.get("text") or "")[-400:]}  # fmt: skip
        if row["html"]:
            row["digits"] = digits_check(table, row["html"])
    except Exception as e:  # noqa: BLE001
        row = {"id": it["id"], "stratum": it["stratum"], "verdict": "error", "error": str(e)[:300]}
    return row


def cmd_run(a) -> int:
    items = json.loads((OUT / "items.json").read_text())[: a.limit or None]
    path = OUT / "results.jsonl"
    done = {json.loads(x)["id"] for x in path.read_text().splitlines()} if path.exists() else set()
    todo = [it for it in items if it["id"] not in done]
    with ThreadPoolExecutor(max_workers=a.concurrency) as pool:
        for fut in as_completed(pool.submit(_one, it, a.max_tokens) for it in todo):
            row = fut.result()
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
            brief = {k: row.get(k) for k in ("id", "verdict", "seconds", "gen")}
            print(json.dumps(brief | {"digits_ok": (row.get("digits") or {}).get("preserved")}), flush=True)
    return 0


# ── score ─────────────────────────────────────────────────────────────

STRUCTURAL = {"column_shift", "header_problems", "row_shift", "stacked_merged"}


def _norm(h: str) -> str:
    return re.sub(r"\s+", "", slim(h))


def effective(r: dict, items: dict) -> str:
    """Muse's verdict, with a "fixed" whose table is the OCR's unchanged
    counted as "ok" (control-04: a correct table, flagged, returned as-is)."""
    if r.get("verdict") == "fixed" and r.get("html"):
        if _norm(r["html"]) == _norm(Path(items[r["id"]]["ocr_table"]).read_text()):
            return "ok"
    return r.get("verdict", "?")


def numbers_outcome(r: dict) -> str:
    """structure-only (every number kept) | from-image (numbers added or
    changed, none lost outright) | lost (numbers dropped) | none."""
    d = r.get("digits")
    if not d:
        return "none"
    if d["preserved"]:
        return "structure-only"
    if d["missing"] and not d["added"]:
        return "lost"
    return "from-image"


def cmd_score(a) -> int:
    items = {i["id"]: i for i in json.loads((OUT / "items.json").read_text())}
    labels = {r["id"]: r for b in sorted(AUDIT.glob("table_result_*.json")) for r in json.loads(b.read_text())}
    res = [json.loads(x) for x in (OUT / "results.jsonl").read_text().splitlines() if x.strip()]
    grid = collections.Counter()
    for r in res:
        lab = labels.get(r["id"], {})
        truth = "structural" if lab.get("verdict") in STRUCTURAL else lab.get("verdict", "?")
        grid[(truth, effective(r, items))] += 1
    print(f"{len(res)} tables. reviewer truth x muse verdict (no-op fixes counted ok):")
    for (t, v), n in sorted(grid.items()):
        print(f"  {t:<16} {v:<10} {n}")
    fixed = [r for r in res if effective(r, items) == "fixed"]
    print(f"fixed {len(fixed)}, numbers:", dict(collections.Counter(numbers_outcome(r) for r in fixed)))
    secs = sorted(r.get("seconds", 0) for r in res if r.get("seconds"))
    if secs:
        print(f"median {secs[len(secs) // 2]}s per table")
    return 0

# ── judge: a second reviewer round on muse's corrections ──────────────

JUDGE_FIELDS = (
    "id, muse_call_right (bool: muse's ok/fixed/unfixable matches the page), "
    "correction ('better'|'same'|'worse'|'n/a' when muse returned no table), "
    "rows_checked, rows_wrong_before, rows_wrong_after (rows with ANY value under the wrong "
    "header/row or wrong digits, in the OCR table and in muse's table), "
    "digit_edits ([{before, after, printed, right: bool}] for every number muse changed), "
    "introduced (errors muse's table has that the OCR table did not; '' if none), "
    "faithful_after (bool: every value of muse's table sits under its printed header and row), "
    "note"
)


def cmd_judge_batches(a) -> int:
    items = {i["id"]: i for i in json.loads((OUT / "items.json").read_text())}
    labels = {r["id"]: r for b in sorted(AUDIT.glob("table_result_*.json")) for r in json.loads(b.read_text())}
    res = [json.loads(x) for x in (OUT / "results.jsonl").read_text().splitlines() if x.strip()]
    (OUT / "muse_html").mkdir(exist_ok=True)
    papers = {}
    for line in (ROOT / "databank" / "papers.jsonl").read_text().splitlines():
        try:
            d = json.loads(line)
            papers[d["paper_key"]] = d
        except (ValueError, KeyError):
            pass
    rows = []
    only = set(a.ids.split(",")) if a.ids else None
    for r in res:
        if r.get("verdict") == "error" or (only and r["id"] not in only):
            continue
        it, lab = items[r["id"]], labels.get(r["id"], {})
        mh = OUT / "muse_html" / f"{r['id']}.html"
        if r.get("html"):
            mh.write_text(r["html"])
        rows.append({
            "id": r["id"], "page_image": it["page_image"], "pdf_page": it["page"],
            "pdf": str(ROOT / papers[it["paper_key"]]["pdf_path"]),
            "muse_effective_verdict": effective(r, items), "numbers_outcome": numbers_outcome(r),
            "ocr_table": it["ocr_table"], "muse_verdict": r.get("verdict"),
            "muse_problems": r.get("problems", []),
            "muse_table": str(mh) if r.get("html") else None,
            "numbers_check": r.get("digits"),
            "earlier_review": {k: lab.get(k) for k in ("verdict", "secondary", "examples", "note")},
        })  # fmt: skip
    n, first = a.batches, a.first_batch
    for k in range(n):
        (OUT / f"judge_batch_{first + k}.json").write_text(json.dumps(rows[k::n], indent=1))
    print(f"{len(rows)} rows in {n} batches; fields: {JUDGE_FIELDS}")
    return 0


def cmd_judge_score(a) -> int:
    # a later batch re-judges a table (a re-run after a fix): the last word wins
    by_id = {}
    for b in sorted(OUT.glob("judge_result_*.json"), key=lambda f: int(f.stem.rsplit("_", 1)[1])):
        for r in json.loads(b.read_text()):
            by_id[r["id"]] = r
    js = list(by_id.values())
    items = {i["id"]: i for i in json.loads((OUT / "items.json").read_text())}
    res = {}
    for x in (OUT / "results.jsonl").read_text().splitlines():
        if x.strip():
            r = json.loads(x)
            res[r["id"]] = r
    js = [j for j in js if j["id"] in res and res[j["id"]].get("verdict") != "error"]
    print(f"{len(js)} judged against the page")
    flag = {j["id"]: effective(res[j["id"]], items) != "ok" for j in js}
    bad = {j["id"]: bool(j.get("ocr_had_errors")) for j in js}
    print("detection: damaged flagged %d / missed %d; clean flagged %d / passed %d" % (
        sum(flag[k] and bad[k] for k in flag), sum(bad[k] and not flag[k] for k in flag),
        sum(flag[k] and not bad[k] for k in flag), sum(not flag[k] and not bad[k] for k in flag)))  # fmt: skip
    fixed = [j for j in js if effective(res[j["id"]], items) == "fixed"]
    for name, group in (("damaged", [j for j in fixed if bad[j["id"]]]),
                        ("clean", [j for j in fixed if not bad[j["id"]]])):  # fmt: skip
        c = collections.Counter(j.get("correction") for j in group)
        ok = sum(bool(j.get("faithful_after")) for j in group)
        print(f"  corrections on {name} tables ({len(group)}): {dict(c)}; faithful after {ok}")
    by = collections.defaultdict(collections.Counter)
    for j in fixed:
        o = numbers_outcome(res[j["id"]])
        by[o][j.get("correction")] += 1
        by[o]["faithful_after"] += bool(j.get("faithful_after"))
    for o, c in sorted(by.items()):
        print(f"  numbers {o:<15} {dict(c)}")
    rb = sum(j.get("rows_wrong_before") or 0 for j in fixed)
    ra = sum(j.get("rows_wrong_after") or 0 for j in fixed)
    print(f"rows wrong {rb} -> {ra} of {sum(j.get('rows_checked') or 0 for j in fixed)} checked")
    edits = [e for j in js for e in (j.get("digit_edits") or [])]
    print(f"number edits judged {len(edits)}: right {sum(bool(e.get('right')) for e in edits)}")
    print("worse:", [j["id"] for j in fixed if j.get("correction") == "worse"])
    print("earlier review right:", sum(bool(j.get("earlier_review_right")) for j in js), "of", len(js))
    return 0

# ── v2: reasoning channel, evidence prompt, blind A/B verify, gate ──────
#
# agent/table_triage.py holds the method; this drives it over the same 48
# tables. Muse thinks in its own channel (an explicit reasoning level), at the
# family's own sampling (temperature unset), and EVERY non-trivial fix gets
# the blind A/B read so the gate and the verify-everything policy can both be
# scored from one run.

V2 = OUT / "v2"
ITEMS = OUT / "items.json"


def _one_v2(it: dict, max_tokens: int) -> dict:
    from agent import table_triage as tt

    ocr = Path(it["ocr_table"]).read_text()
    png = Path(it["page_image"])
    row = {"id": it["id"], "stratum": it["stratum"]}
    t0 = time.time()
    try:
        r = vision(tt.TRIAGE_PROMPT.format(table=tt.slim(ocr)), png, max_tokens,
                   reasoning=tt.TRIAGE_REASONING, temperature=None)  # fmt: skip
        text = r.get("text") or ""
        if not text.strip():
            # EMPTY IS RETRYABLE, never a verdict: on a busy server the engine
            # can admit the stream with a shrunk budget and thinking eats it
            # (5 of 48 first reads, 2026-10-03). One more read.
            r = vision(tt.TRIAGE_PROMPT.format(table=tt.slim(ocr)), png, max_tokens,
                       reasoning=tt.TRIAGE_REASONING, temperature=None)  # fmt: skip
            text = r.get("text") or ""
            row["retried_empty"] = True
        d = tt.parse_triage(text)
        row.update(gen=r.get("generatedTokens"), raw=text, **d)
        g = tt.gate(d, ocr)
        row.update(apply=g["apply"], gate_reason=g["reason"],
                   numbers=(g["numbers"] or {}).get("kind"))  # fmt: skip
    except Exception as e:  # noqa: BLE001
        row.update(verdict="error", error=str(e)[:300])
    row["seconds"] = round(time.time() - t0)
    return row


def cmd_run2(a) -> int:
    V2.mkdir(parents=True, exist_ok=True)
    items = json.loads(ITEMS.read_text())[: a.limit or None]
    path = V2 / "results.jsonl"
    done = {json.loads(x)["id"] for x in path.read_text().splitlines() if x.strip()} if path.exists() else set()
    todo = [it for it in items if it["id"] not in done]
    with ThreadPoolExecutor(max_workers=a.concurrency) as pool:
        for fut in as_completed(pool.submit(_one_v2, it, a.max_tokens) for it in todo):
            row = fut.result()
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
            brief = {k: row.get(k) for k in ("id", "verdict", "numbers", "verify_choice", "apply", "seconds", "gen")}
            print(json.dumps(brief), flush=True)
    return 0


def _round1_truth() -> dict:
    """The page-judged state of each OCR table (round 1; a later batch wins)."""
    by_id = {}
    for b in sorted(OUT.glob("judge_result_*.json"), key=lambda f: int(f.stem.rsplit("_", 1)[1])):
        for r in json.loads(b.read_text()):
            by_id[r["id"]] = r
    return by_id


def _v2_rows() -> dict:
    rows = {}
    for x in (V2 / "results.jsonl").read_text().splitlines():
        if x.strip():
            r = json.loads(x)
            rows[r["id"]] = r
    return rows


def cmd_score2(a) -> int:
    truth, res = _round1_truth(), _v2_rows()
    grid = collections.Counter()
    for k, r in res.items():
        bad = truth.get(k, {}).get("ocr_had_errors")
        flagged = r.get("verdict") == "fixed" and r.get("gate_reason") != "no-op fix"
        grid[("damaged" if bad else "clean", r.get("verdict") if r.get("verdict") in ("error", "unparsed") else ("flagged" if flagged or r.get("verdict") == "unfixable" else "passed"))] += 1
    print(f"{len(res)} tables. OCR truth (round 1) x v2 call:")
    for (t, v), n in sorted(grid.items()):
        print(f"  {t:<8} {v:<9} {n}")
    print("gate:", dict(collections.Counter(r.get("gate_reason") for r in res.values())))
    print("verify choices on fixes:", dict(collections.Counter(r.get("prefers_fix") for r in res.values() if "verify_choice" in r)))
    secs = sorted(r.get("seconds", 0) for r in res.values() if r.get("seconds"))
    gens = sorted(r.get("gen") or 0 for r in res.values() if r.get("gen"))
    if secs:
        print(f"median {secs[len(secs) // 2]} s per table (triage + verify), median {gens[len(gens) // 2]} generated tokens")
    return 0


def cmd_judge_batches2(a) -> int:
    """Reviewer rows for every v2 fix that changed the table (gate hidden)."""
    items = {i["id"]: i for i in json.loads(ITEMS.read_text())}
    truth, res = _round1_truth(), _v2_rows()
    papers = {}
    for line in (ROOT / "databank" / "papers.jsonl").read_text().splitlines():
        try:
            d = json.loads(line)
            papers[d["paper_key"]] = d
        except (ValueError, KeyError):
            pass
    (V2 / "muse_html").mkdir(parents=True, exist_ok=True)
    rows = []
    for k, r in sorted(res.items()):
        if not r.get("numbers"):  # no candidate fix: nothing new to judge
            continue
        it = items[k]
        mh = V2 / "muse_html" / f"{k}.html"
        mh.write_text(r["html"])
        t = truth.get(k, {})
        rows.append({
            "id": k, "page_image": it["page_image"], "pdf_page": it["page"],
            "pdf": str(ROOT / papers[it["paper_key"]]["pdf_path"]),
            "ocr_table": it["ocr_table"], "muse_table": str(mh), "muse_problems": r.get("problems", []),
            "numbers_outcome": r.get("numbers"),
            "earlier_review_of_ocr": {kk: t.get(kk) for kk in ("ocr_had_errors", "rows_wrong_before", "note")},
        })  # fmt: skip
    n = a.batches
    for i in range(n):
        (V2 / f"judge_batch_{i + 1}.json").write_text(json.dumps(rows[i::n], indent=1))
    print(f"{len(rows)} v2 fixes in {n} batches")
    return 0


def cmd_judge_score2(a) -> int:
    truth, res = _round1_truth(), _v2_rows()
    judged = {}
    for b in sorted(V2.glob("judge_result_*.json")):
        for r in json.loads(b.read_text()):
            judged[r["id"]] = r
    print(f"{len(res)} tables; {len(judged)} v2 fixes judged against the page")

    def policy(name, take):
        c, before, after = collections.Counter(), 0, 0
        for k, r in res.items():
            t = truth.get(k, {})
            j = judged.get(k)
            if j and take(r):
                c[j.get("correction")] += 1
                before += j.get("rows_wrong_before") or 0
                after += j.get("rows_wrong_after") or 0
            else:
                c["kept OCR"] += 1
                w = (j or t).get("rows_wrong_before") or 0
                before += w
                after += w
        worse = [k for k, r in res.items() if k in judged and take(r) and judged[k].get("correction") == "worse"]
        print(f"  {name:<34} {dict(c)}; rows wrong {before} -> {after}; worse {worse}")

    print("policies:")
    policy("apply every fix", lambda r: bool(r.get("numbers")))
    policy("v2 gate", lambda r: bool(r.get("apply")))
    policy("verify every fix (not lost)", lambda r: r.get("numbers") in ("from-image", "structure-only") and r.get("prefers_fix") is True)

    from agent import table_triage as tt

    items = {i["id"]: i for i in json.loads(ITEMS.read_text())}

    def _final_gate(r):
        return tt.gate(r, Path(items[r["id"]]["ocr_table"]).read_text())["apply"]

    policy("final gate (lost + grid shape)", _final_gate)
    by = collections.defaultdict(collections.Counter)
    for k, j in judged.items():
        r = res[k]
        by[(r.get("numbers"), r.get("prefers_fix"))][j.get("correction")] += 1
    print("judged fixes by numbers outcome x blind preference:")
    for key, c in sorted(by.items(), key=str):
        print(f"  {str(key):<34} {dict(c)}")
    edits = [e for j in judged.values() for e in (j.get("digit_edits") or [])]
    print(f"number edits judged {len(edits)}: right {sum(bool(e.get('right')) for e in edits)}")
    return 0


# ── fresh-sample validation of the final gate (2026-10-03) ─────────────
#
# PRE-REGISTERED BAR (written before the run): the gate passes if at most 1
# of the corrections it APPLIES is worse than the OCR table, and applied
# corrections cut rows with a wrong or misplaced value overall. Sample: tables
# drawn uniformly (seeded) from accepted papers -- the production scope --
# excluding every paper of the 48-table bench the gate was fitted on.

FRESH = OUT / "fresh"


def cmd_sample(a) -> int:
    import asyncio
    import random

    from agent.actions.scholarly_actions import read_databank
    from agent.effects.local import LocalEffects

    FRESH.mkdir(parents=True, exist_ok=True)
    (FRESH / "tables").mkdir(exist_ok=True)
    (FRESH / "pages").mkdir(exist_ok=True)
    bench_papers = {i["paper_key"] for i in json.loads((OUT / "items.json").read_text())}
    db = asyncio.run(read_databank(LocalEffects(working_directory=str(ROOT))))
    pool = []
    for k, r in sorted(db.items()):
        if r.get("review_status") != "accepted" or k in bench_papers:
            continue
        md, pdf = r.get("md_path"), r.get("pdf_path")
        if not md or not pdf or not (ROOT / md).exists() or not (ROOT / pdf).exists():
            continue
        text = (ROOT / md).read_text(encoding="utf-8", errors="replace")
        for t in re.findall(r"<table\b.*?</table>", text, re.S | re.I):
            if t.lower().count("<tr") >= 2:
                pool.append((k, t))
    rng = random.Random(a.seed)
    rng.shuffle(pool)
    print(f"pool: {len(pool)} tables in accepted papers outside the bench")
    items, skipped = [], collections.Counter()
    for k, t in pool:
        if len(items) >= a.n:
            break
        pdf = ROOT / db[k]["pdf_path"]
        pages = subprocess.run(["pdftotext", "-layout", str(pdf), "-"],
                               capture_output=True, text=True).stdout.split("\f")  # fmt: skip
        want = _toks(re.sub(r"<[^>]+>", " ", t))
        bag = _toks
        if sum(want.values()) < 5:
            want, bag = _words(re.sub(r"<[^>]+>", " ", t)), _words
        if not pages or sum(want.values()) < 3:
            skipped["too little text to place"] += 1
            continue
        scores = [sum((bag(pg) & want).values()) for pg in pages]
        best = max(range(len(pages)), key=lambda i: scores[i])
        if scores[best] < 0.5 * sum(want.values()):
            skipped["page not found (scan or weak text layer)"] += 1
            continue
        tid = f"fresh-{len(items) + 1:02d}"
        (FRESH / "tables" / f"{tid}.html").write_text(t)
        png = FRESH / "pages" / tid
        subprocess.run(["pdftoppm", "-r", str(DPI), "-png", "-f", str(best + 1), "-l", str(best + 1),
                        "-singlefile", str(pdf), str(png)], capture_output=True)  # fmt: skip
        items.append({"id": tid, "stratum": "fresh", "paper_key": k, "page": best + 1,
                      "page_image": str(png) + ".png", "ocr_table": str(FRESH / "tables" / f"{tid}.html"),
                      "page_score": scores[best]})  # fmt: skip
    (FRESH / "items.json").write_text(json.dumps(items, indent=1))
    print(f"{len(items)} sampled (seed {a.seed}); skipped: {dict(skipped)}")
    return 0


def cmd_judge_batches_fresh(a) -> int:
    """Reviewer rows for EVERY fresh table: the OCR's own state is the truth
    the detection is scored on, and a changed correction is judged too."""
    items = {i["id"]: i for i in json.loads(ITEMS.read_text())}
    res = _v2_rows()
    papers = {}
    for line in (ROOT / "databank" / "papers.jsonl").read_text().splitlines():
        try:
            d = json.loads(line)
            papers[d["paper_key"]] = d
        except (ValueError, KeyError):
            pass
    (V2 / "muse_html").mkdir(parents=True, exist_ok=True)
    rows = []
    for k, r in sorted(res.items()):
        it = items[k]
        changed = bool(r.get("numbers"))
        mh = V2 / "muse_html" / f"{k}.html"
        if changed:
            mh.write_text(r["html"])
        rows.append({
            "id": k, "page_image": it["page_image"], "pdf_page": it["page"],
            "pdf": str(ROOT / papers[it["paper_key"]]["pdf_path"]),
            "ocr_table": it["ocr_table"], "muse_table": str(mh) if changed else None,
            "muse_verdict": r.get("verdict"), "muse_problems": r.get("problems", []),
            "numbers_outcome": r.get("numbers"),
        })  # fmt: skip
    n = a.batches
    for i in range(n):
        (V2 / f"judge_batch_{i + 1}.json").write_text(json.dumps(rows[i::n], indent=1))
    print(f"{len(rows)} fresh tables in {n} batches ({sum(1 for r in rows if r['muse_table'])} with a changed table)")
    return 0


def cmd_score_fresh(a) -> int:
    from agent import table_triage as tt

    items = {i["id"]: i for i in json.loads(ITEMS.read_text())}
    res = _v2_rows()
    judged = {}
    for b in sorted(V2.glob("judge_result_*.json")):
        for r in json.loads(b.read_text()):
            judged[r["id"]] = r
    print(f"{len(res)} fresh tables run, {len(judged)} judged against the page")
    det = collections.Counter()
    for k, r in res.items():
        j = judged.get(k)
        if not j:
            continue
        bad = bool(j.get("ocr_had_errors"))
        flagged = r.get("verdict") in ("fixed", "unfixable") and r.get("gate_reason") != "no-op fix"
        det[("damaged" if bad else "clean", "flagged" if flagged else r.get("verdict") if r.get("verdict") in ("error", "unparsed") else "passed")] += 1
    print("OCR truth x call:", dict(sorted(det.items())))
    applied = collections.Counter()
    before = after = 0
    worse = []
    for k, r in res.items():
        j = judged.get(k)
        if not j or not r.get("numbers"):
            continue
        g = tt.gate(r, Path(items[k]["ocr_table"]).read_text())
        corr = j.get("correction")
        applied[("applied" if g["apply"] else "kept OCR", corr)] += 1
        if g["apply"]:
            before += j.get("rows_wrong_before") or 0
            after += j.get("rows_wrong_after") or 0
            if corr == "worse":
                worse.append(k)
    print("gate x correction:", dict(sorted(applied.items())))
    print(f"applied: rows wrong {before} -> {after}; worse among applied: {len(worse)} {worse}")
    n_applied = sum(v for (g, _), v in applied.items() if g == "applied")
    verdict = "PASS" if len(worse) <= 1 and after < before else "FAIL"
    print(f"PRE-REGISTERED BAR (<= 1 worse of {n_applied} applied, rows wrong falls): {verdict}")
    edits = [e for j in judged.values() for e in (j.get("digit_edits") or [])]
    print(f"number edits judged {len(edits)}: right {sum(bool(e.get('right')) for e in edits)}")
    secs = sorted(r.get("seconds", 0) for r in res.values() if r.get("seconds"))
    print(f"retried empties: {sum(1 for r in res.values() if r.get('retried_empty'))}; "
          f"median {secs[len(secs) // 2] if secs else 0} s per table")  # fmt: skip
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=("pages", "run", "score", "judge-batches", "judge-score",
                                    "run2", "score2", "judge-batches2", "judge-score2",
                                    "sample", "judge-batches-fresh", "score-fresh"))
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--max-tokens", type=int, default=12000)
    ap.add_argument("--concurrency", type=int, default=2)
    ap.add_argument("--batches", type=int, default=4)
    ap.add_argument("--set", choices=("v2", "fresh"), default="v2", help="which item set the v2 commands use")
    ap.add_argument("--n", type=int, default=60)
    ap.add_argument("--seed", type=int, default=20261003)
    ap.add_argument("--first-batch", type=int, default=1)
    ap.add_argument("--ids", default="", help="judge-batches: only these ids (comma list)")
    ap.add_argument("--page", action="append", default=[], help="id=page (pin a page the reviewers found)")
    a = ap.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    if a.set == "fresh":
        global V2, ITEMS
        V2, ITEMS = FRESH, FRESH / "items.json"
    cmds = {"pages": cmd_pages, "run": cmd_run, "score": cmd_score,
            "judge-batches": cmd_judge_batches, "judge-score": cmd_judge_score,
            "run2": cmd_run2, "score2": cmd_score2, "judge-batches2": cmd_judge_batches2,
            "judge-score2": cmd_judge_score2, "sample": cmd_sample,
            "judge-batches-fresh": cmd_judge_batches_fresh, "score-fresh": cmd_score_fresh}
    return cmds[a.cmd](a)


if __name__ == "__main__":
    raise SystemExit(main())
