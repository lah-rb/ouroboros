#!/usr/bin/env python3
"""Ingest a hand-curated open-access reading list into the spectra databank.

Operator-directed (2026-09-06): five foundational reading lists in
~/Downloads/ResearchDocs (LIBS, Raman, UV-Vis-NIR, XRD, XRF) name the
papers the corpus is built on top of. Most are FOUNDATIONS-eligible under
the 2026-08-29 objective widening (see dev/add_foundations_goals.py).

WHY A HAND INGEST RATHER THAN A SEARCH QUERY. Two thirds of these papers
are pre-1970 and carry no DOI, no abstract and no OpenAlex record, so the
discovery lanes cannot find them and the OA resolver cannot route them.
Of the ones the corpus DID already hold, nearly all sat at
access_status=closed or oa_unresolved (HTTP 403) — present as metadata,
absent as text, which for a mining corpus is the same as absent. The
reading lists supply repository locations the resolver never had: HAL,
OSTI, Zenodo, eScholarship, the Bavarian Academy, the IAS repository,
Europe PMC's ?pdf=render route.

WHAT THIS DOES NOT DO. It does not defeat bot management. Every URL here
was fetched with a descriptive agent and one request; a 403 is recorded
as a burnt location in oa_attempted and the paper is left for the
pipeline's own recovery lane (Wayback / CORE / citation_pdf_url), not
retried harder.

The records land as status="acquired" with the PDF already on disk, which
is exactly the state action_download_papers leaves behind. The extraction
lane selects on access_status=="oa_pdf" and pdf_path (see
_extraction_pending), so OCR picks them up on the next round with the ocr
lane enabled; curate needs no tags.

Usage:
  .venv/bin/python dev/ingest_reading_list.py            # dry run
  .venv/bin/python dev/ingest_reading_list.py --apply
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.actions.scholarly_actions import (  # noqa: E402
    EXTRACTION_OWNED_FIELDS,
    paper_key,
)

CORPUS = os.path.expanduser("~/corpora/ouroboros-spectra")
DATABANK = os.path.join(CORPUS, "databank/papers.jsonl")
PDF_DIR = os.path.join(CORPUS, "pdfs")

PHYS = "Spectroscopy technique physics"

# (local_pdf, doi, arxiv_id, title, authors, year, venue, aspects, source_url
#  [, binder])  -- binder defaults True: these lists ARE the binder shelf. A
# single-mineral measurement that merely rode along is False.
MANIFEST = [
    # ── already in the databank, but with NO full text ────────────────
    (
        "gaudiuso2010c",
        "10.3390/s100807434",
        "",
        "Laser Induced Breakdown Spectroscopy for Elemental Analysis in "
        "Environmental, Cultural Heritage and Space Applications: A Review "
        "of Methods and Results",
        [
            "R. Gaudiuso",
            "M. Dell'Aglio",
            "O. De Pascale",
            "G. S. Senesi",
            "A. De Giacomo",
        ],
        2010,
        "Sensors",
        [PHYS, "LIBS mineral spectra"],
        "https://europepmc.org/articles/PMC3231154?pdf=render",
    ),
    (
        "konjevic2002",
        "10.1063/1.1486456",
        "",
        "Experimental Stark Widths and Shifts for Spectral Lines of Neutral "
        "and Ionized Atoms",
        ["N. Konjevic", "A. Lesage", "J. R. Fuhr", "W. L. Wiese"],
        2002,
        "Journal of Physical and Chemical Reference Data",
        [PHYS],
        "https://www.nist.gov/system/files/documents/srd/jpcrd622.pdf",
    ),
    (
        "clegg2009",
        "10.1016/j.sab.2008.10.045",
        "",
        "Multivariate Analysis of Remote Laser-Induced Breakdown "
        "Spectroscopy Spectra Using Partial Least Squares, Principal "
        "Component Analysis, and Related Techniques",
        ["S. M. Clegg", "E. Sklute", "M. D. Dyar", "J. E. Barefield", "R. C. Wiens"],
        2009,
        "Spectrochimica Acta Part B",
        [PHYS, "LIBS mineral spectra"],
        "https://www.osti.gov/servlets/purl/960604",
    ),
    (
        "elhaddad2014",
        "10.1016/j.sab.2014.08.039",
        "",
        "Good Practices in LIBS Analysis: Review and Advices",
        ["J. El Haddad", "L. Canioni", "B. Bousquet"],
        2014,
        "Spectrochimica Acta Part B",
        [PHYS],
        "https://hal.science/hal-01094344/document",
    ),
    (
        "ramankrishnan1928",
        "10.1038/121501c0",
        "",
        "A New Type of Secondary Radiation",
        ["C. V. Raman", "K. S. Krishnan"],
        1928,
        "Nature",
        [PHYS],
        "http://repository.ias.ac.in/28460/1/367.pdf",
    ),
    (
        "kubelkamunk1931",
        "",
        "",
        # The databank already holds the GERMAN ORIGINAL under this title,
        # with no full text. The PDF is Westin's English translation, so it
        # attaches to that record rather than minting a second identity for
        # the same work -- and it makes the work mineable, which the German
        # record never was.
        "Ein Beitrag zur Optik der Farbanstriche",
        ["P. Kubelka", "F. Munk"],
        1931,
        "Zeitschrift fur Technische Physik",
        [PHYS, "UVVis NIR reflectance"],
        "https://www.graphics.cornell.edu/~westin/pubs/kubelka.pdf",
    ),
    # ── absent from the databank entirely ─────────────────────────────
    (
        "raman1928b",
        "",
        "",
        "A New Radiation",
        ["C. V. Raman"],
        1928,
        "Indian Journal of Physics",
        [PHYS],
        "http://repository.ias.ac.in/70648/1/36-PUb.pdf",
    ),
    (
        "ferrari2006",
        "10.1103/PhysRevLett.97.187401",
        "cond-mat/0606284",
        "Raman Spectrum of Graphene and Graphene Layers",
        [
            "A. C. Ferrari",
            "J. C. Meyer",
            "V. Scardaci",
            "C. Casiraghi",
            "M. Lazzeri",
            "F. Mauri",
            "S. Piscanec",
            "D. Jiang",
            "K. S. Novoselov",
            "S. Roth",
            "A. K. Geim",
        ],
        2006,
        "Physical Review Letters",
        [PHYS],
        "https://arxiv.org/pdf/cond-mat/0606284",
    ),
    (
        "laue1912a",
        "",
        "",
        "Interferenz-Erscheinungen bei Roentgenstrahlen",
        ["W. Friedrich", "P. Knipping", "M. Laue"],
        1912,
        "Sitzungsberichte der Bayerischen Akademie der Wissenschaften",
        [PHYS, "XRD phase identification"],
        "https://publikationen.badw.de/de/003395746/003395746.pdf",
    ),
    (
        "laue1912b",
        "",
        "",
        "Eine quantitative Pruefung der Theorie fuer die "
        "Interferenz-Erscheinungen bei Roentgenstrahlen",
        ["M. Laue"],
        1912,
        "Sitzungsberichte der Bayerischen Akademie der Wissenschaften",
        [PHYS, "XRD phase identification"],
        "https://publikationen.badw.de/de/003395750/003395750.pdf",
    ),
    (
        "barkla1911",
        "10.1080/14786440908637137",
        "",
        "The Spectra of the Fluorescent Roentgen Radiations",
        ["C. G. Barkla"],
        1911,
        "Philosophical Magazine",
        [PHYS, "XRF EDS elemental mapping"],
        "https://zenodo.org/records/1430862/files/article.pdf",
    ),
    (
        "moseley1913",
        "10.1080/14786441308635052",
        "",
        "The High-Frequency Spectra of the Elements",
        ["H. G. J. Moseley"],
        1913,
        "Philosophical Magazine",
        [PHYS, "XRF EDS elemental mapping"],
        "https://zenodo.org/record/1430926/files/article.pdf",
    ),
    (
        "bearden1967",
        "10.6028/nbs.nsrds.14",
        "",
        "X-Ray Wavelengths and X-Ray Atomic Energy Levels (NSRDS-NBS 14)",
        ["J. A. Bearden"],
        1967,
        "National Standard Reference Data Series (NBS)",
        [PHYS, "XRF EDS elemental mapping"],
        "https://archive.org/download/xraywavelengthsx14bear/"
        "xraywavelengthsx14bear.pdf",
    ),
    (
        "gatti1984",
        "10.1016/0167-5087(84)90113-3",
        "",
        "Semiconductor Drift Chamber - An Application of a Novel Charge "
        "Transport Scheme",
        ["E. Gatti", "P. Rehak"],
        1984,
        "Nuclear Instruments and Methods in Physics Research",
        [PHYS],
        "https://www.osti.gov/servlets/purl/5549333",
    ),
    (
        "sitko2012",
        "10.5772/29367",
        "",
        "Quantification in X-Ray Fluorescence Spectrometry",
        ["R. Sitko", "B. Zawisza"],
        2012,
        "X-Ray Spectroscopy (IntechOpen)",
        [PHYS, "XRF EDS elemental mapping"],
        "https://cdn.intechopen.com/pdfs/27342/"
        "intech-quantification_in_x_ray_fluorescence_spectrometry.pdf",
    ),
    # ── second sweep (2026-09-06, evening): the families the five lists
    # left unanchored -- FTIR/IR (39% of packs), EDS/EPMA (28%), XPS (9%),
    # plus the Raman-of-minerals database chapter ────────────────────────
    (
        "coblentz1905",
        "",
        "",
        "Investigations of Infra-Red Spectra. Part I: Infra-Red Absorption "
        "Spectra; Part II: Infra-Red Emission Spectra",
        ["W. W. Coblentz"],
        1905,
        "Carnegie Institution of Washington Publication 35",
        [PHYS],
        "https://archive.org/download/investigationsi01coblgoog/"
        "investigationsi01coblgoog.pdf",
    ),
    (
        "coblentz1908",
        "",
        "",
        "Investigations of Infra-Red Spectra, Volume III: Parts V-VII "
        "(Reflection, Transmission and Emission Spectra of Solids)",
        ["W. W. Coblentz"],
        1908,
        "Carnegie Institution of Washington Publication 65",
        [PHYS],
        "https://archive.org/download/investigationsof03coblrich/"
        "investigationsof03coblrich.pdf",
    ),
    (
        "rruff2015",
        "10.1515/9783110417104-003",
        "",
        "The Power of Databases: The RRUFF Project",
        ["B. Lafuente", "R. T. Downs", "H. Yang", "N. Stone"],
        2015,
        "Highlights in Mineralogical Crystallography (De Gruyter)",
        [PHYS, "Raman FTIR cultural heritage", "XRD phase identification"],
        "https://www.rruff.net/wp-content/uploads/2023/04/HMC1-30.pdf",
    ),
    # The two Lyon reports share their first 80 title characters, and a
    # title key is the first 80 characters of the title -- so Part I would
    # silently overwrite the Part II record the databank already holds.
    # The report number leads the title for exactly that reason.
    (
        "lyon1963",
        "",
        "",
        "NASA TN D-1871: Evaluation of Infrared Spectrophotometry for "
        "Compositional Analysis of Lunar and Planetary Soils",
        ["R. J. P. Lyon"],
        1963,
        "NASA Technical Note",
        [PHYS, "UVVis NIR reflectance"],
        "https://ntrs.nasa.gov/api/citations/19630004530/downloads/" "19630004530.pdf",
    ),
    (
        "lyon1964",
        "",
        "",
        # verbatim: this MUST reproduce the databank's existing title key
        "Evaluation of infrared spectrophotometry for compositional analysis "
        "of lunar and planetary soils. part ii- rough and powdered surfaces",
        ["R. J. P. Lyon"],
        1964,
        "NASA Contractor Report CR-100",
        [PHYS, "UVVis NIR reflectance"],
        "https://ntrs.nasa.gov/api/citations/19650001173/downloads/" "19650001173.pdf",
    ),
    (
        "nasa_ir_oxides",
        "",
        "",
        "NASA TN D-3750: Infrared Spectra of Various Metal Oxides in the "
        "Region of 2 to 26 Microns",
        ["D. W. Sheibley", "M. H. Fowler"],
        1966,
        "NASA Technical Note",
        [PHYS],
        "https://ntrs.nasa.gov/api/citations/19670003469/downloads/" "19670003469.pdf",
    ),
    (
        "ammin45_990",
        "",
        "",
        "Infrared Spectra of Some Tectosilicates",
        ["R. G. Milkey"],
        1960,
        "American Mineralogist",
        [PHYS, "Raman FTIR cultural heritage"],
        "http://www.minsocam.org/ammin/AM45/AM45_990.pdf",
    ),
    (
        "ammin54_1062",
        "",
        "",
        "The Infrared and Raman Spectra of Realgar and Orpiment",
        ["R. Forneris"],
        1969,
        "American Mineralogist",
        ["Raman FTIR cultural heritage"],
        "http://www.minsocam.org/ammin/AM54/AM54_1062.pdf",
        False,  # one mineral pair, measured well -- corpus, not binder
    ),
    (
        "castaing1955",
        "",
        "",
        "Application of Electron Probes to Local Chemical and "
        "Crystallographic Analysis (Thesis, University of Paris, 1951; "
        "English translation by P. Duwez and D. B. Wittry, 1955)",
        ["R. Castaing"],
        1951,
        "ONERA Publication 55 / Caltech Special Technical Report",
        [PHYS, "XRF EDS elemental mapping"],
        "https://the-mas.org/wp-content/uploads/2020/06/"
        "Castaing-Thesis-clearscan.pdf",
    ),
    (
        "newbury2019",
        "10.1017/S143192761901482X",
        "",
        "Electron-Excited X-ray Microanalysis by Energy Dispersive "
        "Spectrometry at 50: Analytical Accuracy, Precision, Trace "
        "Sensitivity, and Quantitative Compositional Mapping",
        ["D. E. Newbury", "N. W. M. Ritchie"],
        2019,
        "Microscopy and Microanalysis",
        [PHYS, "XRF EDS elemental mapping"],
        "https://tsapps.nist.gov/publication/get_pdf.cfm?pub_id=927336",
    ),
    (
        "baer2019xps",
        "10.1116/1.5065501",
        "",
        "Practical Guides for X-Ray Photoelectron Spectroscopy: First Steps "
        "in Planning, Conducting, and Reporting XPS Measurements",
        [
            "D. R. Baer",
            "K. Artyushkova",
            "C. R. Brundle",
            "J. E. Castle",
            "M. H. Engelhard",
            "K. J. Gaskell",
            "J. T. Grant",
            "R. T. Haasch",
            "M. R. Linford",
            "C. J. Powell",
            "A. G. Shard",
            "P. M. A. Sherwood",
            "V. S. Smentkowski",
        ],
        2019,
        "Journal of Vacuum Science & Technology A",
        [PHYS],
        "https://www.osti.gov/servlets/purl/1567261",
    ),
]


# Papers the reading lists name that this pass could NOT fetch: every
# sanctioned location answered 403/404/50x to a single polite request. They
# are still holes, so they get RECORDS rather than silence — as candidates,
# with the reading-list location recorded AND burnt in oa_attempted. The
# resolver then either finds an untried location via Unpaywall (-> oa_pdf)
# or exhausts them (-> oa_unresolved), which is what makes them eligible
# for the recovery lane's Wayback / CORE / citation_pdf_url routes. Burning
# the URL here is the point: it spends no request re-hitting a wall this
# script already proved is up.
#
# Four of these are ALREADY in the databank at access_status="closed", which
# no stage ever re-works (is_stale_retry_candidate requires "oa_unresolved").
# The operator's list verifies them as open access, so "closed" is bad
# metadata that had made them permanently unreachable; re-arming them as
# candidates gives the resolver and the recovery lane a first real chance.
PENDING = [
    (
        "10.1155/2023/2502152",
        "",
        "Forty Years of Laser-Induced Breakdown Spectroscopy and Laser and "
        "Particle Beams",
        ["V. Palleschi"],
        2023,
        "Laser and Particle Beams",
        [PHYS],
        "https://onlinelibrary.wiley.com/doi/pdf/10.1155/2023/2502152",
    ),
    (
        "10.1126/science.1165758",
        "",
        "Label-Free Biomedical Imaging with High Sensitivity by Stimulated "
        "Raman Scattering Microscopy",
        [
            "C. W. Freudiger",
            "W. Min",
            "B. G. Saar",
            "S. Lu",
            "G. R. Holtom",
            "C. He",
            "J. C. Tsai",
            "J. X. Kang",
            "X. S. Xie",
        ],
        2008,
        "Science",
        [PHYS],
        "https://europepmc.org/articles/PMC3576036?pdf=render",
    ),
    (
        "10.1002/cphc.202000464",
        "",
        "The Bouguer-Beer-Lambert Law: Shining Light on the Obscure",
        ["T. G. Mayerhofer", "S. Pahlow", "J. Popp"],
        2020,
        "ChemPhysChem",
        [PHYS, "UVVis NIR reflectance"],
        "https://chemistry-europe.onlinelibrary.wiley.com/doi/pdfdirect/"
        "10.1002/cphc.202000464",
    ),
    (
        "10.1002/andp.19083300302",
        "",
        "Beitraege zur Optik trueber Medien, speziell kolloidaler " "Metalloesungen",
        ["G. Mie"],
        1908,
        "Annalen der Physik",
        [PHYS, "UVVis NIR reflectance"],
        "https://onlinelibrary.wiley.com/doi/pdf/10.1002/andp.19083300302",
    ),
    (
        "10.1098/rspa.1913.0083",
        "",
        "The Structure of Some Crystals as Indicated by Their Diffraction of " "X-rays",
        ["W. L. Bragg"],
        1913,
        "Proceedings of the Royal Society A",
        [PHYS, "XRD phase identification"],
        "https://royalsocietypublishing.org/doi/pdf/10.1098/rspa.1913.0083",
    ),
    (
        "",
        "",
        "Interferenzen an regellos orientierten Teilchen im Roentgenlicht I",
        ["P. Debye", "P. Scherrer"],
        1916,
        "Nachrichten von der Gesellschaft der Wissenschaften zu Goettingen",
        [PHYS, "XRD phase identification"],
        "https://gdz.sub.uni-goettingen.de/id/PPN252457811_1916",
    ),
    (
        "",
        "",
        "Bestimmung der Groesse und der inneren Struktur von Kolloidteilchen "
        "mittels Roentgenstrahlen",
        ["P. Scherrer"],
        1918,
        "Nachrichten von der Gesellschaft der Wissenschaften zu Goettingen",
        [PHYS, "XRD phase identification"],
        "https://gdz.sub.uni-goettingen.de/id/PPN252457811_1918",
    ),
    (
        "10.1063/1.555594",
        "",
        "Atomic Radiative and Radiationless Yields for K and L Shells",
        ["M. O. Krause"],
        1979,
        "Journal of Physical and Chemical Reference Data",
        [PHYS, "XRF EDS elemental mapping"],
        "https://srd.nist.gov/jpcrdreprint/1.555594.pdf",
    ),
    (
        "10.1006/adnd.1993.1013",
        "",
        "X-Ray Interactions: Photoabsorption, Scattering, Transmission, and "
        "Reflection at E = 50-30,000 eV, Z = 1-92",
        ["B. L. Henke", "E. M. Gullikson", "J. C. Davis"],
        1993,
        "Atomic Data and Nuclear Data Tables",
        [PHYS, "XRF EDS elemental mapping"],
        "https://escholarship.org/uc/item/9wh2w9rg",
    ),
    (
        "10.1126/science.151.3710.562",
        "",
        "Application of High-Resolution Semiconductor Detectors in X-Ray "
        "Emission Spectrography",
        ["H. R. Bowman", "E. K. Hyde", "S. G. Thompson", "R. C. Jared"],
        1966,
        "Science",
        [PHYS, "XRF EDS elemental mapping"],
        "https://escholarship.org/uc/item/6575m0wv",
    ),
    # ── already present, wrongly parked at access_status="closed" ─────
    (
        "10.1007/s11214-012-9912-2",
        "",
        "The ChemCam Instrument Suite on the Mars Science Laboratory (MSL) "
        "Rover: Science Objectives and Mast Unit Description",
        ["S. Maurice", "R. C. Wiens"],
        2012,
        "Space Science Reviews",
        [PHYS],
        "https://hal.science/hal-00732883",
    ),
    (
        "10.1103/PhysRevLett.78.1667",
        "",
        "Single Molecule Detection Using Surface-Enhanced Raman Scattering " "(SERS)",
        [
            "K. Kneipp",
            "Y. Wang",
            "H. Kneipp",
            "L. T. Perelman",
            "I. Itzkan",
            "R. R. Dasari",
            "M. S. Feld",
        ],
        1997,
        "Physical Review Letters",
        [PHYS],
        "https://scholar.harvard.edu/files/perelman/files/prl_78_1667_1997.pdf",
    ),
    (
        "10.1002/andp.18521620505",
        "",
        "Bestimmung der Absorption des rothen Lichts in farbigen " "Fluessigkeiten",
        ["A. Beer"],
        1852,
        "Annalen der Physik",
        [PHYS, "UVVis NIR reflectance"],
        "https://onlinelibrary.wiley.com/doi/pdf/10.1002/andp.18521620505",
    ),
    (
        "10.1098/rspa.1913.0040",
        "",
        "The Reflection of X-rays by Crystals",
        ["W. H. Bragg", "W. L. Bragg"],
        1913,
        "Proceedings of the Royal Society A",
        [PHYS, "XRD phase identification"],
        "https://royalsocietypublishing.org/doi/pdf/10.1098/rspa.1913.0040",
    ),
]


def _scraper_side(rec: dict) -> dict:
    """Drop extraction-owned fields — they live in the sidecar, which
    overlays this file. Carrying copies here shadows the other writer
    (see append_records / read_databank)."""
    shared = ("paper_key", "updated_at", "failure_reason", "language")
    return {
        k: v for k, v in rec.items() if k not in EXTRACTION_OWNED_FIELDS or k in shared
    }


def _last_rows(path: str) -> dict[str, dict]:
    rows: dict[str, dict] = {}
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            rows[rec.get("paper_key") or paper_key(rec)] = rec
    return rows


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument(
        "--stage",
        choices=("pdfs", "pending", "both"),
        default="both",
        help="pdfs = the fetched PDFs; pending = records for the ones no "
        "sanctioned location would serve",
    )
    ap.add_argument(
        "--src",
        default=os.path.join(os.environ.get("OUROBOROS_INGEST_SRC", ""), "").rstrip("/")
        or None,
        help="directory holding the verified <name>.pdf files",
    )
    args = ap.parse_args()
    src = args.src
    if args.stage in ("pdfs", "both") and (not src or not os.path.isdir(src)):
        print("--src must name the directory of fetched PDFs")
        return 2

    existing = _last_rows(DATABANK)
    now = datetime.now(timezone.utc).isoformat()

    planned: list[tuple[str, str, dict]] = []
    manifest = MANIFEST if args.stage in ("pdfs", "both") else []
    for entry in manifest:
        name, doi, arxiv, title, authors, year, venue, aspects, url = entry[:9]
        binder = entry[9] if len(entry) > 9 else True
        pdf = os.path.join(src, f"{name}.pdf")
        if not os.path.exists(pdf):
            print(f"  SKIP {name}: no fetched PDF")
            continue
        with open(pdf, "rb") as fh:
            if fh.read(5) != b"%PDF-":
                print(f"  SKIP {name}: not a PDF")
                continue
        ident = {"doi": doi, "arxiv_id": arxiv, "s2_id": "", "title": title}
        key = paper_key(ident)

        base = existing.get(key)
        verb = "UPDATE" if base else "NEW   "
        rec = (
            dict(base)
            if base
            else {
                "status": "candidate",
                "access_status": "",
                "source_aspects": aspects,
                "oa_pdf_url": "",
                "oa_pdf_urls": [],
                "oa_attempted": [],
                "pdf_path": "",
                "tags": [],
                "reference_dois": [],
                "referenced_works": [],
                "language": "",
                "license": "",
                "failure_reason": "",
                "title": title,
                "abstract": "",
                "year": year,
                "venue": venue,
                "authors": authors,
                "doi": doi,
                "arxiv_id": arxiv,
                "s2_id": "",
                "openalex_id": "",
            }
        )
        # Acquisition fields — exactly what action_download_papers writes.
        urls = [u for u in ([url] + list(rec.get("oa_pdf_urls") or []))]
        rec["paper_key"] = key
        rec["status"] = "acquired"
        rec["access_status"] = "oa_pdf"
        rec["oa_pdf_url"] = url
        rec["oa_pdf_urls"] = list(dict.fromkeys(urls))
        rec["oa_attempted"] = list(
            dict.fromkeys(list(rec.get("oa_attempted") or []) + [url])
        )
        rec["pdf_path"] = f"pdfs/{key}.pdf"
        rec["failure_reason"] = ""
        rec["updated_at"] = now
        # Provenance: which hand-curated list this came from.
        rec["ingest_source"] = "ResearchDocs reading list 2026-09-06"
        # THE BINDER FLAG. emit.py relabels a flagged paper's pack prose and
        # markdown to the 4x-weighted binder sources (see SOURCE_WEIGHT there).
        rec["binder"] = bool(binder)
        planned.append((verb, pdf, _scraper_side(rec)))

    if args.stage in ("pending", "both"):
        for entry in PENDING:
            doi, arxiv, title, authors, year, venue, aspects, url = entry[:8]
            binder = entry[8] if len(entry) > 8 else True
            key = paper_key(
                {"doi": doi, "arxiv_id": arxiv, "s2_id": "", "title": title}
            )
            base = existing.get(key)
            verb = "REARM " if base else "HOLE  "
            rec = (
                dict(base)
                if base
                else {
                    "status": "candidate",
                    "source_aspects": aspects,
                    "oa_pdf_url": "",
                    "oa_pdf_urls": [],
                    "oa_attempted": [],
                    "pdf_path": "",
                    "tags": [],
                    "reference_dois": [],
                    "referenced_works": [],
                    "language": "",
                    "license": "",
                    "title": title,
                    "abstract": "",
                    "year": year,
                    "venue": venue,
                    "authors": authors,
                    "doi": doi,
                    "arxiv_id": arxiv,
                    "s2_id": "",
                    "openalex_id": "",
                }
            )
            rec["paper_key"] = key
            rec["status"] = "candidate"
            # Blank, not "closed": the resolver re-decides from scratch. A
            # "closed" record is re-worked by NOTHING (see the note above).
            rec["access_status"] = ""
            rec["failure_reason"] = ""
            rec["oa_pdf_urls"] = list(
                dict.fromkeys([url] + list(rec.get("oa_pdf_urls") or []))
            )
            rec["oa_attempted"] = list(
                dict.fromkeys(list(rec.get("oa_attempted") or []) + [url])
            )
            rec["updated_at"] = now
            rec["ingest_source"] = "ResearchDocs reading list 2026-09-06"
            rec["binder"] = bool(binder)
            planned.append((verb, "", _scraper_side(rec)))

    for verb, pdf, rec in planned:
        tag = "B" if rec.get("binder") else "-"
        print(f"  {verb} {tag} {rec['paper_key'][:60]:62s} {rec['title'][:44]}")
    fresh = sum(1 for v, _, _ in planned if v.startswith(("NEW", "HOLE")))
    print(f"\n{len(planned)} record(s); {fresh} new to the databank")

    if not args.apply:
        print("DRY RUN — pass --apply to write.")
        return 0

    for _, pdf, rec in planned:
        if pdf:
            shutil.copy2(pdf, os.path.join(CORPUS, rec["pdf_path"]))

    # ONE atomic O_APPEND write. The mission's own append_file lock is
    # in-process only, so an out-of-process appender must not split its
    # payload across write() calls or it can interleave with a live
    # curate booking.
    payload = "".join(
        json.dumps(rec, ensure_ascii=False) + "\n" for _, _, rec in planned
    ).encode("utf-8")
    fd = os.open(DATABANK, os.O_WRONLY | os.O_APPEND)
    try:
        written = os.write(fd, payload)
    finally:
        os.close(fd)
    if written != len(payload):
        print(f"SHORT WRITE {written}/{len(payload)} — INSPECT THE TAIL")
        return 4
    print(f"appended {len(planned)} record(s), {written} bytes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
