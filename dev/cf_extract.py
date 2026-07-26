#!/usr/bin/env python3
"""Final-channel action extraction for counterfactual level runs.

THE BUG THIS REPLACES (found 2026-07-25, root cause of the whole
adaptive_thinking high-class failure — see dev/ADAPTIVE_THINKING_STATUS.md):

    cf_run_level.py:36
    "action": (fm.group(1).strip() if fm else raw.strip())[:1200]

When the final-channel regex missed, it fell back to ``raw`` — the entire
analysis CoT — and sliced it to 1200 chars. So a turn whose reasoning ran long
enough that the final channel never arrived silently became *1200 characters of
truncated chain-of-thought presented to the judges as the agent's action*.

`maxTokens` was 2500 and generous; the generation was fine. The damage was
entirely in extraction. Consequences, measured:

  - truncation rate scales with level: low 9.0% / medium 20.5% / high 47.0%
    (high reasoning writes the longest analysis, so it misses the final
    channel most often)
  - 93% of the quarantined `high` candidates have NO final channel at all
  - gold-label rate for `high` collapses 14.7% (intact) -> 1.8% (truncated),
    an 8x swing, because judges compared an actionless CoT fragment against a
    complete low action and rationally picked low

Same shape as the featurizer bare-`<` corruption: our extraction mutilates
valid model output, and the damage gets attributed to the model.

THE RULE HERE: a missing final channel yields ``action=None`` and a loud flag.
It NEVER silently substitutes reasoning text for an action. A record with
``action is None`` is not judgeable and must be excluded or regenerated —
never labeled.
"""

from __future__ import annotations

import re

# gpt-oss/harmony channel framing. `final` may terminate with <|end|>,
# <|return|>, or simply run to the end of the emission.
_COT_RE = re.compile(
    r"analysis<\|message\|>(.*?)(?:<\|end\|>|<\|channel\|>final)", re.DOTALL
)

# THE FINAL CHANNEL IS NOT ALWAYS `final<|message|>`. Grammar-constrained
# emissions interpose a constrain marker and a space:
#
#     <|channel|>final <|constrain|>json<|message|>{"choice": ...}
#
# The original harness matched only the adjacent form, so EVERY constrained
# JSON action — which is what the interaction menu produces — failed to match,
# fell into the `[:1200]` raw fallback, and was stored as truncated CoT. That
# is the trigger; the fallback was the amplifier.
#
# Parsed by index rather than one clever regex: find the final-channel header,
# then take the FIRST <|message|> after it, whatever markers sit in between.
_FINAL_HDR = re.compile(r"<\|channel\|>\s*final\b")
_MSG = "<|message|>"
_TERMINATORS = ("<|end|>", "<|return|>", "<|start|>")


def _find_final(raw: str) -> str | None:
    m = None
    for m in _FINAL_HDR.finditer(raw):  # last final channel wins
        pass
    if m is None:
        return None
    msg = raw.find(_MSG, m.end())
    if msg == -1:
        return None  # header with no message body — genuinely unusable
    body = raw[msg + len(_MSG) :]
    cut = min((i for i in (body.find(t) for t in _TERMINATORS) if i != -1), default=-1)
    return body if cut == -1 else body[:cut]


# Actions this long are almost certainly a framing failure rather than a real
# action; flagged (never silently cut) so the record can be inspected.
IMPLAUSIBLE_ACTION_CHARS = 20_000


def extract(raw: str, finished: bool = True) -> dict:
    """Pull the final-channel action out of a raw harmony emission.

    Returns a dict with:
      action      -- the final-channel text, or None when there is no final
                     channel (NEVER the CoT as a stand-in)
      cot_chars   -- length of the analysis channel, 0 if absent
      no_final    -- True when the model never reached the final channel
      truncated   -- True when generation hit the token budget
      usable      -- True only when there is a real action to judge
      flags       -- list of human-readable problems, for logs
    """
    flags: list[str] = []

    cot = _COT_RE.search(raw)
    cot_chars = len(cot.group(1)) if cot else 0

    fin = _find_final(raw)
    no_final = fin is None
    action = fin.strip() if fin is not None else None

    if not finished:
        flags.append("truncated: hit the token budget")
    if no_final:
        flags.append("no_final: model never reached the final channel")
    if action is not None and not action:
        flags.append("empty_final: final channel present but empty")
        action = None
    if action is not None and len(action) > IMPLAUSIBLE_ACTION_CHARS:
        flags.append(f"implausible_length: {len(action)} chars (kept, not cut)")

    return {
        "action": action,
        "cot_chars": cot_chars,
        "no_final": no_final,
        "truncated": not finished,
        "usable": action is not None and finished,
        "flags": flags,
    }


def audit(records: list[dict]) -> dict:
    """Summarize extraction health over a batch. Call this before labeling —
    a corpus with a high no_final rate is not labelable, it is a bug report."""
    n = len(records) or 1
    bad = sum(1 for r in records if not r.get("usable"))
    return {
        "n": len(records),
        "usable": n - bad,
        "no_final": sum(1 for r in records if r.get("no_final")),
        "truncated": sum(1 for r in records if r.get("truncated")),
        "unusable_pct": round(100.0 * bad / n, 1),
    }
