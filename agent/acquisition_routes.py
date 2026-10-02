"""Who acquires a paper's PDF: the one table of acquisition routes (2026-10-02).

WHY. The answer used to live in three places that did not know about each
other: the crawler's WALLED_HOSTS (scholarly_actions), the manual intake
export's `--doi-prefix` filter, and the browser fetcher's MDPI URL pattern.
When the browser fetcher took over MDPI, the manual lists kept handing the
operator MDPI papers to click (35 % of the first three lists), because
nothing told the export that a machine now fetches them. Operator ruling
2026-10-02: rectify it against a single source of truth, not a flag patch.

WHAT. One row per publisher: the DOI prefixes it assigns, the hosts it serves
from, how it MEASURABLY answers the polite crawler, and whether the windowed
browser fetcher (tools/browser_fetch.py) may take it -- which is an operator
ruling per publisher, never inferred. Every consumer derives from the table:

  * the crawler skips `crawler_walled_hosts()` when harvesting alternate
    locations and when re-checking stored links (scholarly_actions.WALLED_HOSTS);
  * `acquirer(rec, browser_log)` says who should fetch an unresolved paper:
    "browser" while the fetcher may still try it, else "operator" (the manual
    intake lists). A paper leaves the browser route after
    BROWSER_ATTEMPTS failed tries with no success, so a refusal the fetcher
    cannot get past still reaches a human;
  * tools/browser_fetch.py fetches only papers whose acquirer is "browser".

Adding a publisher to the browser route is one row -- after a probe and an
operator ruling, as MDPI had. Stdlib only: the browser fetcher's own venv
imports this module.

THE CRAWLER COLUMN IS MEASURED, NOT COUNTED. Plain re-fetch yield per host,
sampled 2026-08-13: link.springer.com 4/6 (and 9/9, 4/4 in two earlier
samples), onlinelibrary.wiley.com 0/6, www.sciencedirect.com 0/6, doi.org 0/6,
www.mdpi.com 0/6. link.springer.com was once walled here from a table of
failure COUNTS (93 failures) without asking whether they were durable; they
were transient, and Springer serves PDFs on a retry more often than not.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

#: Where tools/browser_fetch.py logs every attempt, relative to the corpus root.
BROWSER_FETCH_LOG = "manual_intake/browser_fetch.jsonl"
#: Failed browser attempts (with no success) after which a paper is the
#: operator's. Two, so one transient failure (a network change, a browser
#: crash) is retried before a human is asked.
BROWSER_ATTEMPTS = 2


@dataclass(frozen=True)
class Publisher:
    name: str
    doi_prefixes: tuple[str, ...]
    #: Hosts matched by substring, exactly as the crawler always matched them
    #: ("sciencedirect.com" covers www.sciencedirect.com).
    hosts: tuple[str, ...]
    #: "walled": measured to refuse the polite crawler (403 / bot wall).
    #: "open": measured to serve it, if not always on the first try.
    crawler: str
    #: The windowed browser fetcher may fetch it (operator ruling).
    browser: bool = False
    evidence: str = ""


PUBLISHERS: tuple[Publisher, ...] = (
    Publisher(
        "mdpi",
        ("10.3390/",),
        # www.mdpi.com only: its PDF CDN, mdpi-res.com, serves plain clients.
        ("www.mdpi.com",),
        crawler="walled",
        browser=True,
        evidence="Akamai 403 to curl and headless Chromium; 0/6 plain re-fetch "
        "2026-08-13; windowed browser 221/221 (2026-10-01); browser route "
        "operator ruling 2026-10-01 (robots.txt silent on /pdf; 20-40 s, "
        "200/day)",
    ),
    Publisher(
        "elsevier",
        ("10.1016/",),
        ("sciencedirect.com",),
        crawler="walled",
        evidence="Cloudflare 403; 0/6 plain re-fetch 2026-08-13",
    ),
    Publisher(
        "wiley",
        ("10.1002/", "10.1111/", "10.1029/"),
        ("onlinelibrary.wiley.com",),
        crawler="walled",
        evidence="0/6 plain re-fetch 2026-08-13",
    ),
    Publisher(
        "iop",
        ("10.1088/",),
        ("iopscience.iop.org",),
        crawler="walled",
        evidence="Radware/perfdrive bot management (2026-08-11)",
    ),
    Publisher(
        "rsc",
        ("10.1039/",),
        ("pubs.rsc.org",),
        crawler="walled",
        evidence="bot wall on per-article fetch (2026-08-11)",
    ),
    Publisher(
        "springer-nature",
        ("10.1007/", "10.1038/", "10.1186/"),
        ("link.springer.com",),
        crawler="open",
        evidence="plain re-fetch 4/6, 9/9, 4/4 (2026-08-13): transient failures",
    ),
)

#: Not a publisher but a wall one hop away: doi.org lands on a meta-refresh
#: stub that forwards into the publisher (measured: linkinghub.elsevier.com ->
#: sciencedirect), so an unresolved record still pointing at it is walled.
RESOLVER_HOSTS: tuple[str, ...] = ("doi.org",)


def _host(url: str) -> str:
    return url.split("/")[2].lower() if "//" in (url or "") else ""


def crawler_walled_hosts() -> tuple[str, ...]:
    """Every host measured to refuse the polite crawler, resolvers included."""
    return (
        tuple(h for p in PUBLISHERS if p.crawler == "walled" for h in p.hosts)
        + RESOLVER_HOSTS
    )


def is_crawler_walled(url: str) -> bool:
    host = _host(url)
    return any(w in host for w in crawler_walled_hosts())


def _record_urls(rec: dict) -> list[str]:
    urls = [rec.get("oa_pdf_url") or "", *(rec.get("oa_pdf_urls") or [])]
    urls += list(rec.get("oa_attempted") or [])
    return [str(u) for u in urls if u]


def publisher_for(rec: dict) -> Publisher | None:
    """The record's publisher: by DOI prefix first, else by a stored URL's host."""
    doi = str(rec.get("doi") or "").strip().lower()
    for p in PUBLISHERS:
        if doi and doi.startswith(p.doi_prefixes):
            return p
    for url in _record_urls(rec):
        host = _host(url)
        for p in PUBLISHERS:
            if any(h in host for h in p.hosts):
                return p
    return None


def read_browser_log(corpus_root: str | Path) -> list[dict]:
    path = Path(corpus_root) / BROWSER_FETCH_LOG
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict) and entry.get("key"):
            out.append(entry)
    return out


def browser_exhausted(browser_log: list[dict]) -> set[str]:
    """Papers the browser fetcher has failed BROWSER_ATTEMPTS times, never fetched."""
    ok: set[str] = set()
    fails: dict[str, int] = {}
    for e in browser_log:
        if e.get("outcome") == "ok":
            ok.add(e["key"])
        else:
            fails[e["key"]] = fails.get(e["key"], 0) + 1
    return {k for k, n in fails.items() if n >= BROWSER_ATTEMPTS and k not in ok}


def acquirer(rec: dict, exhausted: set[str] = frozenset()) -> str:
    """Who should fetch this paper's PDF: "browser" or "operator".

    Only meaningful for a paper without one (access unresolved or closed);
    the crawler's own retries run regardless and are not a route a human
    waits on. `exhausted` is browser_exhausted() over the fetcher's log.
    """
    p = publisher_for(rec)
    if p is not None and p.browser and rec.get("paper_key") not in exhausted:
        return "browser"
    return "operator"
