"""Look at a PDF's first page before spending OCR on the whole thing.

WHY. Measured on the live queue with a hand-labelled 40-paper sample:
**only 15 of 40 queued papers (38%) are about minerals, rocks or pigments at
all**, and ~23% are reviews, prefaces or annual reports that carry no
measurements. OCR costs ~290 s a paper. Reading page one costs ~16 s.

WHY NOT A TITLE REGEX. That was tried first and scored against the same
labels: it catches **3 of 9 reviews**. What it misses is what keywords
structurally cannot see — "Array programming with NumPy", two Japanese
nuclear-institute annual reports, "Gale crater: the MSL Landing Site" (a
geological overview), "MSL Mast cameras and Descent imager" (an instrument
description). None contain the word "review".

WHY PADDLE THEN TEXT, NOT MUSE VISION. Two reasons, both measured on a
QUIET machine (a first attempt measured under load and reported 60 s/page
for vision — 3x the real figure, and nearly killed this design on it):

  paddle OCR -> muse text   3.2 s + 13.3 s = 16.5 s   verdicts clean
  muse vision              20.6 s                     verdicts leak reasoning

The leak is structural, not a cap: `run_completion` splits reasoning from
the answer (core/inference.py), and `run_vision_completion` does not — it
returns the raw text. figtext never hit it because a "describe this figure"
prompt invites no deliberation, whereas "research or review?" does. Routing
the DECISION through the text path avoids the whole problem rather than
working around it.

AUTHORITY, and the two operator rulings that shape it:

  * **Reviews are NOT skipped.** Their reference lists feed citation mining,
    and a review's bibliography is denser than a research paper's. A review
    is not waste, it is lower-priority work.
  * **Off-topic papers ARE skipped.** A polymer FTIR study or a
    sensor-network paper is a pipeline burden that does not warrant the
    in-process position, and the skips remain reviewable by hand.

  page will not render     -> extract_failed      (genuine corruption)
  confidently NOT geological -> extract_off_topic (a REVIEW QUEUE, not a
                                                   rejection: distinct
                                                   status, reason recorded,
                                                   clearable by hand)
  everything else          -> a bin + a priority tier

`geological: unclear` NEVER skips. Only a confident "no" does — doubt costs
queue position, never a paper, because a terminal status is not re-selected
by the pending sweep.
"""

from __future__ import annotations

import logging
import os
import re

logger = logging.getLogger(__name__)

#: Render target lives INSIDE the workspace: the server reads image paths
#: only under model.vision_image_roots (~/corpora, ~/ouroboros-runs), so a
#: /tmp render would be refused — correctly, it is an SSRF guard.
SHOT_DIR = "databank/_preocr"
#: 160 dpi was measured against 220: 220 costs ~1.5x for no accuracy gain on
#: a title block, which is all this reads.
RENDER_DPI = int(os.environ.get("OUROBOROS_PREOCR_DPI", "160"))
#: Paddle is a hot secondary in the same LLMVP; this routes to it.
OCR_MODEL = os.environ.get("OUROBOROS_PREOCR_OCR_MODEL", "paddle-ocr-vl")
#: 0 disables triage entirely and the queue keeps its existing order.
TRIAGE_BUDGET = int(os.environ.get("OUROBOROS_PREOCR_PAPERS", "4"))

#: Priority tiers, low number first — the same convention _aspect_priority
#: already uses so the two can be compared directly.
PRIO_THIN_BIN = 0  # research, geological, in a coverage bin we are short on
PRIO_NORMAL = 1  # research, on topic
PRIO_LOW = 2  # a review/report: still processed, just later

_OCR_PROMPT = "Transcribe all text on this page in reading order. Text only."

#: The geological question names MATERIALS, not object types, because the
#: first live batch removed three papers whose subject was a mineral phase
#: inside a non-geological object: two Vermeer pigment studies from Heritage
#: Science and an FTIR crystallinity study of burned bone (apatite). The
#: model was answering on the OBJECT — a painting, a skeleton — rather than
#: what was measured. 3 of 19 removals, all in that one class; cultural
#: heritage is the corpus's LARGEST bin at 28%, so it is the worst class to
#: be wrong about.
_TRIAGE_PROMPT = """You are triaging a scientific paper before it is processed.

Below is the text of its FIRST PAGE.

Decide three things:
- Is it ORIGINAL RESEARCH reporting new measurements the authors made, or a
  REVIEW / overview / preface / editorial / annual report / instrument
  description that reports no new measurements of its own?
- What is the main measurement technique, if any?
- Does the paper measure a MINERAL OR INORGANIC MATERIAL? Judge the
  MATERIAL BEING MEASURED, not the kind of object it came from. Say yes for
  minerals, rocks, ores, soils, meteorites and planetary surfaces, and ALSO
  for the mineral content of other objects:
    * pigments and paint layers in paintings, murals or manuscripts
    * ceramics, porcelain, glass, glazes, plaster, mortar and building stone
    * bone, teeth, shell and coral (apatite, calcite — mineral phases)
    * corrosion products, patinas, ores and slags
  Say no when the material itself is organic or synthetic with no mineral
  phase — polymers, textiles, dyes, oils, biological tissue, pharmaceuticals
  — and no for software, statistics, instrumentation and networking papers.

End your reply with exactly these three lines and nothing after them:
TYPE: research|review|unclear
TECHNIQUE: raman|ftir|libs|xrd|reflectance|xrf|other|none
GEOLOGICAL: yes|no|unclear

FIRST PAGE TEXT:
{page}"""

#: The coverage bins the corpus is thin on, mirroring
#: curation_actions._PRIORITY_ASPECTS at the technique level.
_THIN_BIN_TECHNIQUES = frozenset({"libs", "raman", "ftir"})

_LINE = re.compile(r"^\s*(TYPE|TECHNIQUE|GEOLOGICAL)\s*:\s*(.+?)\s*$", re.I | re.M)
_VALID = {
    "type": {"research", "review", "unclear"},
    "technique": {
        "raman",
        "ftir",
        "libs",
        "xrd",
        "reflectance",
        "xrf",
        "other",
        "none",
    },
    "geological": {"yes", "no", "unclear"},
}


def parse_verdict(text: str) -> dict:
    """Read the verdict off the TAIL, and only accept declared values.

    Take the LAST occurrence of each key: this family restates the question
    while reasoning, so an early "TYPE: research|review" is the template
    being echoed, not an answer. An earlier version of this parser demanded
    JSON, got prose, returned {} for every paper, and scored a perfect 5/5
    on non-primary purely because an empty dict reads as "not research".
    """
    out: dict[str, str] = {}
    for key, raw in _LINE.findall(text or ""):
        val = raw.strip().lower().split()[0] if raw.strip() else ""
        val = val.strip(".,;*`")
        if val in _VALID[key.lower()]:
            out[key.lower()] = val  # last wins
    return out


async def render_first_page(effects, pdf_rel: str, out_rel: str) -> tuple[bool, str]:
    """Page 1 -> PNG inside the workspace. In-process: pymupdf is a root
    dependency, so this needs no subprocess and costs ~0.05 s."""
    import asyncio

    root = getattr(effects, "working_directory", "") or ""
    pdf_abs = pdf_rel if os.path.isabs(pdf_rel) else os.path.join(root, pdf_rel)
    out_abs = os.path.join(root, out_rel)

    def _render() -> tuple[bool, str]:
        import pymupdf

        try:
            os.makedirs(os.path.dirname(out_abs) or ".", exist_ok=True)
            doc = pymupdf.open(pdf_abs)
            if doc.page_count == 0:
                return False, "no pages"
            doc[0].get_pixmap(dpi=RENDER_DPI).save(out_abs)
            return True, ""
        except Exception as e:  # noqa: BLE001 — a bad PDF is data, not a crash
            return False, str(e)[:140]

    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _render)


async def triage_one(effects, paper_key: str, pdf_rel: str) -> dict:
    """{'verdict': ..., 'bin': ..., 'reason': ...} for one paper.

    Returns {'verdict', 'bin', 'priority', 'reason'}.

    verdict: 'ok' (triaged), 'corrupt' (page will not render — the one
    terminal case), 'unknown' (triage unavailable). NOTHING is ever removed
    from the queue; an unknown simply keeps normal priority.
    """
    out_rel = f"{SHOT_DIR}/{paper_key[:80].replace('/', '_')}.png"
    ok, note = await render_first_page(effects, pdf_rel, out_rel)
    if not ok:
        # The ONE terminal verdict, and only for a page that will not render
        # at all. Everything else is advisory.
        return {
            "verdict": "corrupt",
            "bin": "",
            "priority": PRIO_LOW,
            "reason": f"render failed: {note}",
        }

    png_abs = os.path.join(getattr(effects, "working_directory", ""), out_rel)
    ocr = None
    for attempt in (1, 2):
        try:
            ocr = await effects.run_vision(
                _OCR_PROMPT,
                png_abs,
                model=OCR_MODEL,
                max_tokens=1600,
                temperature=0.0,
            )
        except Exception as e:  # noqa: BLE001
            return {
                "verdict": "unknown",
                "bin": "",
                "priority": PRIO_NORMAL,
                "reason": f"ocr error: {e}"[:140],
            }
        err = str(getattr(ocr, "error", "") or "")
        # A RESIDENT SECONDARY IS COLD AFTER EVERY SERVER BOUNCE. paddle does
        # not come up with LLMVP: it loads on demand, and the FIRST request
        # warms it while itself failing "is not hot". Left unhandled that is
        # a silent window of no-ops after each restart — every paper falls
        # through as `unknown`, the gate looks like it is working, and it is
        # doing nothing. It cost a wrong conclusion about a prompt fix on
        # 2026-08-25. Retry once; the second call lands on a warm model.
        if attempt == 1 and "not hot" in err.lower():
            logger.info("🚦 %s was cold — that call warmed it, retrying", OCR_MODEL)
            continue
        break
    page = (getattr(ocr, "text", "") or "").strip()
    if getattr(ocr, "error", None) or len(page) < 120:
        # Too little text to judge on. Fall THROUGH to OCR: a page that
        # transcribes poorly is often a scan, which is exactly the kind of
        # paper the full pipeline exists for.
        return {
            "verdict": "unknown",
            "bin": "",
            "priority": PRIO_NORMAL,
            "reason": f"page text too short ({len(page)} chars)",
        }

    try:
        res = await effects.run_inference(
            _TRIAGE_PROMPT.format(page=page[:6000]),
            {"max_tokens": 700, "temperature": 0.1},
        )
    except Exception as e:  # noqa: BLE001
        return {
            "verdict": "unknown",
            "bin": "",
            "priority": PRIO_NORMAL,
            "reason": f"triage error: {e}"[:140],
        }
    if getattr(res, "error", None):
        return {
            "verdict": "unknown",
            "bin": "",
            "priority": PRIO_NORMAL,
            "reason": f"triage: {res.error}"[:140],
        }

    v = parse_verdict(getattr(res, "text", "") or "")
    if not v.get("type"):
        return {
            "verdict": "unknown",
            "bin": "",
            "priority": PRIO_NORMAL,
            "reason": "no parseable verdict",
        }

    tech = v.get("technique") or ""
    geo = v.get("geological", "unclear")
    is_review = v["type"] == "review"
    # OFF TOPIC = not geological AND not about one of the corpus's own
    # techniques. Widened 2026-08-29 with the foundations goals: a paper on
    # the PHYSICS of Raman/LIBS/XRD (scattering theory, plasma diagnostics,
    # diffraction physics, calibration theory) measures no mineral and used
    # to park off_topic terminally here — exactly the papers the
    # physics-of-technique aspect now hunts. A named corpus technique keeps
    # a non-geological paper in the queue at normal priority; the curator
    # remains the judge of whether its content earns acceptance.
    off_topic = geo == "no" and tech in ("", "none", "other")

    bin_ = tech
    if off_topic and tech in ("", "none", "other"):
        bin_ = "off_topic"
    elif is_review and tech in ("", "none"):
        bin_ = "review"

    # OFF TOPIC LEAVES THE QUEUE. Only on a CONFIDENT "no" — `unclear` falls
    # through, because the cost of doubt should be a queue position and not
    # a paper. The status is distinct and the reason is recorded so the
    # skips can be listed and cleared by hand.
    if off_topic:
        return {
            "verdict": "off_topic",
            "bin": bin_ or "off_topic",
            "priority": PRIO_LOW,
            "reason": (
                f"first page is not geological "
                f"(type={v['type']}, tech={tech or 'none'})"
            ),
        }

    # A review is not waste — the biblio lane mines its references. It goes to
    # the BACK of the queue, never out of it.
    if is_review:
        prio = PRIO_LOW
    elif tech in _THIN_BIN_TECHNIQUES and geo == "yes":
        prio = PRIO_THIN_BIN
    else:
        prio = PRIO_NORMAL

    return {
        "verdict": "ok",
        "bin": bin_,
        "priority": prio,
        "reason": (
            f"{v['type']}, tech={tech or 'none'}, geological={geo}"
            f" -> priority {prio}"
        ),
    }
