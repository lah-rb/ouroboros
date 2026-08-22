#!/usr/bin/env python3
"""Vision caption pass: muse reads captions off the figure crops.

Escalation over tools/figtext_caption_repair.py (run THAT first): after
the mechanical pass, ~1,030 caption-less records remain that genuinely
depict spectra/plots — their captions were unpairable from the markdown
(anchorless crops, caption-count mismatch), but many crops embed the
caption in the image itself. muse vision transcribes it.

STRICT BY DESIGN — a wrong caption is worse than an empty one:
  - junk crops (figtext self-reports: first-page shots, logos, maps,
    tables-as-images) are skipped, not asked;
  - only replies matching the Figure/Fig./Abb/Рис/图 caption pattern are
    accepted (a crop with a caption virtually always shows the prefix);
  - refusal phrasings and NO_CAPTION are refusals, never captions.
Accepted captions stamp caption_repaired="vision-transcription" — the
serializer can weigh transcriptions below markdown-paired captions.

Idempotent; additive; safe to re-run. Requires the LLMVP server.

    .venv/bin/python tools/figtext_caption_vision.py [--dry-run]
        [--limit N] [--root ...]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.request

URL = "http://127.0.0.1:8008/v1/vision"
CAPTION_RE = re.compile(
    r"^\s*(?:Figure|Fig\.?|Figura|Abbildung|Abb\.?|Рис(?:унок)?\.?|图|図)\s*\.?\s*\d+",
    re.IGNORECASE,
)
JUNK_RE = re.compile(
    r"i cannot see a figure|first page of the paper|\blogo\b|graphical abstract"
    r"|photograph of the (?:authors|laboratory)|\bmap\b|rows and columns",
    re.IGNORECASE,
)
REFUSAL_RE = re.compile(
    r"no_caption|no caption|not visible|cannot (?:see|find|read)|unable to",
    re.IGNORECASE,
)
PROMPT = (
    "This image is a figure cropped from a scientific paper. If a figure "
    "caption (text beginning like 'Figure 3.' or 'Fig. 2') is visible INSIDE "
    "this image, transcribe it verbatim and completely. Reply with ONLY the "
    "caption text, nothing else. If no caption text is visible in the image, "
    "reply with exactly: NO_CAPTION"
)


def ask(img_path: str) -> str:
    payload = json.dumps(
        {
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": PROMPT},
                        {"type": "image_path", "path": img_path},
                    ],
                }
            ],
            "max_tokens": 700,
            "temperature": 0.1,
        }
    ).encode()
    req = urllib.request.Request(
        URL, data=payload, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=300) as r:
        d = json.loads(r.read())
    text = (d.get("choices") or [{}])[0].get("message", {}).get("content", "") or ""
    text = re.sub(r"<\|[^|]{1,30}\|>", "", text)
    # SINGLE-TURN SEAL GUARD: the vision endpoint can continue past the
    # answer into a fabricated "USER:" turn (known defect) — the real
    # reply is everything before it.
    text = re.split(r"\n\s*(?:USER|ASSISTANT)\s*:", text)[0]
    return text.strip()


_ECHO_PREFIX = "This image is a figure cropped from a scientific paper"

# Unanchored: the caption may sit anywhere inside a prose wrapper.
CAPTION_ANY_RE = re.compile(
    r"(?:Figure|Fig\.?|Figura|Abbildung|Abb\.?|Рис(?:унок)?\.?|图|図)\s*\.?\s*\d+",
    re.IGNORECASE,
)


def extract_caption(reply: str) -> str:
    """The transcribed caption from a vision reply, or ''.

    Live sample findings (2026-08-22): muse often ANSWERS but wraps —
    'The image shows a figure with caption at bottom right: "FIGURE 1
    Thin section..."' — so the caption pattern is searched anywhere in
    the reply, not just at the start. Prompt echoes (the seal defect)
    and NO_CAPTION/refusal phrasings refuse. Deliberation without a
    >=15-char caption body ("Figure 7" alone) refuses.
    """
    if not reply or reply.startswith(_ECHO_PREFIX):
        return ""
    head = reply.splitlines()[0] if reply.splitlines() else ""
    if REFUSAL_RE.search(head) or REFUSAL_RE.search(reply[:80]):
        return ""
    m = CAPTION_ANY_RE.search(reply)
    if not m:
        return ""
    cap = reply[m.start() :]
    # Trim a closing quotation the wrapper opened, and wrapper tails.
    cap = re.split(r'["\u201d\u00bb]\s*(?:$|[.,]?\s*The image|\s*This )', cap)[0]
    cap = re.sub(r"\s+", " ", cap).strip(" \"'\u201c\u201d")
    if not (15 <= len(cap) <= 1500):
        return ""
    # Deliberation, not transcription: a real caption never asks questions,
    # and a quote closing within the first few words means the model quoted
    # a bare label ('Figure 7" at top left...'), not a caption body.
    if "?" in cap or '"' in cap[:15] or "\u201d" in cap[:15]:
        return ""
    return cap


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=os.path.expanduser("~/corpora/ouroboros-spectra"))
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()
    root = args.root
    ftdir = os.path.join(root, "databank", "figtext")
    asked = accepted = refused = junk = missing = 0
    for f in sorted(os.listdir(ftdir)):
        if not f.endswith(".json"):
            continue
        path = os.path.join(ftdir, f)
        art = json.load(open(path, encoding="utf-8"))
        figs = art.get("figs") or []
        changed = False
        for fig in figs:
            if len(fig.get("caption") or "") >= 5:
                continue
            if fig.get("vision_caption_attempted"):
                continue
            if JUNK_RE.search(fig.get("figtext") or ""):
                junk += 1
                continue
            img = os.path.join(
                root,
                "databank",
                "figures",
                art.get("paper_key", ""),
                fig.get("fig", ""),
            )
            if not os.path.isfile(img):
                missing += 1
                continue
            if args.limit and asked >= args.limit:
                break
            asked += 1
            if args.dry_run:
                continue
            try:
                reply = ask(img)
            except Exception as e:
                print(f"  ask failed ({fig.get('fig')}): {e}", flush=True)
                time.sleep(5)
                continue
            fig["vision_caption_attempted"] = True
            changed = True
            cap = extract_caption(reply)
            if cap:
                fig["caption"] = cap
                fig["caption_repaired"] = "vision-transcription"
                accepted += 1
            else:
                refused += 1
                if args.verbose:
                    print(f"  REFUSED [{fig.get('fig')}]: {reply[:140]!r}", flush=True)
            time.sleep(0.5)
        if changed and not args.dry_run:
            json.dump(
                art, open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=1
            )
        if args.limit and asked >= args.limit:
            break
    print(
        f"{'DRY-RUN ' if args.dry_run else ''}asked={asked} accepted={accepted} "
        f"refused={refused} junk-skipped={junk} image-missing={missing}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
