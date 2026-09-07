"""What the corpus already knows about a figure, before anything is measured.

Every figure in the corpus already carries a vision model's prose description
in ``databank/figtext/<key>.json``. That text is far too loose to calibrate an
axis from — a naive parse recovers two numeric ranges for only 28.9% of
nm-bearing figures, and "ranging from 378 to 390" never says WHICH axis — but
it is free, it is already on disk for ~48,700 figures, and it is more than
good enough to answer "is this a plot at all".

So figtext is used as a PREFILTER, not as the metadata source: it removes the
micrographs, maps and apparatus schematics before anything expensive runs, and
later serves as an independent second reading to cross-check the structured
ask against. The structured vision ask (Phase 1c) is what actually supplies
tick labels, and only for figures that survive here.
"""

from __future__ import annotations

import base64
import dataclasses
import json
import os
import re
import urllib.request

# Spectral x-axis units. A figure that names none of these is not a spectrum
# plot, whatever else it may be.
_UNITS = {
    "libs": re.compile(r"\bnm\b|\bnanomet|\bwavelength\b", re.I),
    "raman": re.compile(r"cm\s*[-−]\s*1|cm⁻¹|\braman shift\b|\bwavenumber", re.I),
    "xrd": re.compile(r"\b2\s*θ|\b2\s*theta\b|\bd[- ]spacing\b", re.I),
}

# How much of the description counts as the model stating what the image is.
_IDENTITY_CHARS = 240

_AXIS = re.compile(
    r"\baxis\b|\baxes\b|\bx[- ]axis\b|\by[- ]axis\b|\btick\b|\bhorizontal axis\b",
    re.I,
)
_PLOTLIKE = re.compile(
    r"\bspectr(?:um|a|al)\b|\bintensity\b|\bpeak(?:s)?\b|\bplot\b|\bcurve\b"
    r"|\bcounts\b|\ba\.?u\.?\b|\bemission line",
    re.I,
)
# Vocabulary that says "this is an image of a thing", not a plot — and it is
# read ONLY against the identity zone (caption plus the opening of the
# description, where the model states what the image IS). Matching it against
# the whole body rejected 1,929 otherwise-qualifying figures, because a long
# description of a real spectrum routinely mentions a micrograph, a map or a
# photograph in passing: "LIBS/ChemCam targets displaying Ca-sulfate signature
# (solid spectra)" was killed by the word "micrograph" appearing later in the
# same paragraph.
_NOT_A_PLOT = re.compile(
    r"\bmicrograph\b|\bSEM image\b|\bTEM image\b|\bphotograph\b|\bphoto of\b"
    r"|\bmap of\b|\btopographic\b|\bschematic (?:diagram|of)\b|\bapparatus\b"
    r"|\bflow ?chart\b|\bsetup\b|\bthin section\b|\boutcrop\b",
    re.I,
)
# figtext's own self-reported junk, the same classes the caption-repair pass
# refuses to ask about.
_JUNK = re.compile(
    r"\bfirst page\b|\bcover page\b|\bjournal logo\b|\bpublisher\b|\bQR code\b"
    r"|\btable\b.{0,20}\bimage\b|\bentirely text\b|\bpage of text\b",
    re.I,
)


@dataclasses.dataclass(frozen=True)
class FigtextHint:
    """The prefilter's reading of one figure's existing description."""

    fig: str
    caption: str
    text: str
    technique: str | None
    plot_like: bool
    reject_reason: str | None

    @property
    def is_candidate(self) -> bool:
        return self.plot_like and self.technique is not None


def _technique(text: str) -> str | None:
    for name, rx in _UNITS.items():
        if rx.search(text):
            return name
    return None


def read_figtext(working_dir: str, key: str) -> dict:
    """The stored figtext record for a paper, or an empty one."""
    path = os.path.join(working_dir, "databank", "figtext", f"{key}.json")
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:  # noqa: BLE001 — a paper without figtext is not an error
        return {}


def hints(working_dir: str, key: str) -> list[FigtextHint]:
    """Prefilter every figure of a paper by what figtext already says.

    Cheap and deliberately permissive on the positive side: a figure that
    survives still has to pass axis detection and calibration, so a false
    positive here costs one CV pass, while a false negative silently drops a
    real spectrum from the corpus.
    """
    rec = read_figtext(working_dir, key)
    out: list[FigtextHint] = []
    for fig in rec.get("figs", []):
        body = fig.get("figtext", "") or ""
        caption = fig.get("caption", "") or ""
        text = f"{caption}\n{body}"
        identity = f"{caption}\n{body[:_IDENTITY_CHARS]}"
        reason = None
        if _JUNK.search(identity):
            reason = "figtext_junk"
        elif _NOT_A_PLOT.search(identity) and not _PLOTLIKE.search(identity):
            reason = "not_a_plot"
        elif not _PLOTLIKE.search(text):
            reason = "no_plot_vocabulary"
        elif not _AXIS.search(text):
            reason = "no_axis_mentioned"
        tech = _technique(text)
        if reason is None and tech is None:
            reason = "no_spectral_unit"
        out.append(
            FigtextHint(
                fig=fig.get("fig", ""),
                caption=fig.get("caption", "") or "",
                text=fig.get("figtext", "") or "",
                technique=tech,
                plot_like=reason is None,
                reject_reason=reason,
            )
        )
    return out


# ── The structured ask ────────────────────────────────────────────────

# LLMVP's GraphQL endpoint, not the optional OpenAI shim.
LLMVP_URL = os.environ.get("OUROBOROS_LLMVP_URL", "http://127.0.0.1:8008").rstrip("/")
VISION_URL = f"{LLMVP_URL}/graphql"
_VISION_MUTATION = """
mutation VisionCompletion($request: VisionCompletionRequest!) {
    visionCompletion(request: $request) {
        text
        generatedTokens
        promptTokens
        imageCount
        visionModel
        decodeMs
    }
}
"""

# No bare count appears anywhere in this prompt. A number the task can
# contradict ("list 5-9 ticks") outranks the prose beside it, and the model
# optimises the number instead of the job.
STRUCTURED_PROMPT = (
    "This image is a plot from a scientific paper. Read ONLY its axes — not "
    "the data, not the caption.\n\n"
    "Reply with a single JSON object and nothing else:\n"
    "{\n"
    '  "is_plot": true or false,\n'
    '  "x_unit": the x-axis unit exactly as printed, or null,\n'
    '  "y_unit": the y-axis unit exactly as printed, or null,\n'
    '  "x_tick_labels": every numeric label printed along the x-axis, in '
    "left-to-right order, as numbers,\n"
    '  "y_tick_labels": every numeric label printed along the y-axis, in '
    "bottom-to-top order, as numbers,\n"
    '  "y_exponent": the power of ten printed at the top of the y-axis (as in '
    '"1e4" or "x10^4"), or null if none is shown,\n'
    '  "y_direction": "up" if larger values are higher on the page, else '
    '"down",\n'
    '  "axes_terminate_at_range": true if each axis line stops at its last '
    "tick, false if the line continues past it,\n"
    '  "n_traces": how many distinct data series are drawn,\n'
    '  "line_labels": every annotation printed ON the plot that names a '
    'spectral line, verbatim, such as "Ca II 393.37" or "Fe I 404.6 nm" '
    "-- an empty list if none are printed\n"
    "}\n\n"
    "Transcribe labels you can actually read. Omit any you cannot; do not "
    "infer a label from the spacing of its neighbours, and do not round.\n"
    "For line_labels, copy only what is printed on the figure. Do not supply "
    "a wavelength from your own knowledge of which element a peak belongs to."
)

_JSON_BLOCK = re.compile(r"\{.*\}", re.S)


def _coerce_numbers(seq) -> list[float]:
    out = []
    for v in seq if isinstance(seq, list) else []:
        try:
            out.append(float(str(v).replace(",", "").replace("−", "-")))
        except (TypeError, ValueError):
            continue
    return out


def parse_structured(reply: str) -> dict | None:
    """Pull the JSON object out of a model reply, however it was wrapped."""
    if not reply:
        return None
    m = _JSON_BLOCK.search(reply)
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    if not isinstance(obj, dict):
        return None
    obj["x_tick_labels"] = _coerce_numbers(obj.get("x_tick_labels"))
    obj["y_tick_labels"] = _coerce_numbers(obj.get("y_tick_labels"))
    raw = obj.get("line_labels")
    obj["line_labels"] = [
        (v if isinstance(v, str) else str((v or {}).get("text", "")))
        for v in (raw if isinstance(raw, list) else [])
    ]
    return obj


def ask_structured(
    image_path: str,
    *,
    url: str = VISION_URL,
    timeout: float = 180.0,
    max_tokens: int = 700,
) -> dict | None:
    """Ask the vision model to read a plot's axes, and nothing else.

    This is the ONLY place the digitiser talks to a model, and it is
    deliberately confined to reading printed tick labels — a small, checkable
    job. Nothing about the data is asked for, because that is the unbounded
    reading this whole tool exists to replace.
    """
    # Sent as a base64 data URI rather than a path: the server refuses any
    # path outside `model.vision_image_roots`, and this tool renders its own
    # crops to wherever the caller asked. A data URI needs no server config.
    with open(image_path, "rb") as fh:
        b64 = base64.b64encode(fh.read()).decode()
    ext = os.path.splitext(image_path)[1].lstrip(".").lower() or "png"
    mime = "jpeg" if ext in ("jpg", "jpeg") else ext
    payload = json.dumps(
        {
            "query": _VISION_MUTATION,
            "variables": {
                "request": {
                    "prompt": STRUCTURED_PROMPT,
                    "images": [{"url": f"data:image/{mime};base64,{b64}"}],
                    "maxTokens": max_tokens,
                    "temperature": 0.1,
                }
            },
        }
    ).encode()
    req = urllib.request.Request(
        url, data=payload, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode())
    except Exception:  # noqa: BLE001 — an unreachable server is a datum
        return None
    text = ""
    if isinstance(body, dict) and not body.get("errors"):
        text = ((body.get("data") or {}).get("visionCompletion") or {}).get(
            "text"
        ) or ""
    return parse_structured(text)
