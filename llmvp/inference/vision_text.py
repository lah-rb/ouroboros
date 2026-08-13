"""Seal and clean a vision completion using the model's own format family.

WHY THIS EXISTS. The text path renders through ``formats/*.yaml`` and extracts
through the FSM labeller, so a family's channel markers and terminators never
reach a caller. The vision path cannot reuse either: ``MTMDChatHandler`` builds
its own prompt from the model's chat template and drives its own decode loop,
so ``/v1/vision`` was returning the raw stream.

Measured 2026-08-12, muse-glimmer through the endpoint: a blind judge scoring
the answers found the marker ``<|start|>assistant to=user<|message|>`` verbatim
mid-answer, followed by the model restarting and re-answering the same figure.
Both are exactly what the text path's terminators and single-turn seal prevent.

TWO MECHANISMS, and the order matters:

1. ``stop_strings()`` seals at GENERATION time. Handing the family's own
   turn-closer to the sampler stops a finished turn from restarting, which
   costs nothing when the model stops correctly on its own. This is NOT
   truncation — it is honouring the terminator the model itself emits, the
   same one the text path has always stopped on.
2. ``clean()`` repairs what still arrives, because a stop string only fires on
   an exact match and a family with two closers (muse ends reasoning with
   ``<|eom|>`` and the turn with ``<|eot|>``) has more than one way to leak.

Families without channels or with no declared markers pass through untouched,
so this is inert for every family that does not need it.
"""

from __future__ import annotations

import re

_MAX_MARKER = 64  # a "marker" longer than this is prose, not a control token


def _schema(family: str):
    """The family's format schema, or None when it has none."""
    if not family:
        return None
    try:
        from formats.registry import load_schema

        return load_schema(family)
    except Exception:  # noqa: BLE001 — an unknown family must not break vision
        return None


def stop_strings(family: str) -> list[str]:
    """Turn-terminators to hand the sampler, most specific first.

    Deliberately EXCLUDES the reasoning closer. For muse that is ``<|eom|>``,
    which ends the analysis message — stopping there would cut the answer off
    before the content block even opens, which is the exact failure the format
    file warns about ("must NEVER be a stop").
    """
    sch = _schema(family)
    if sch is None:
        return []
    tok = sch.tokens
    out: list[str] = []
    for cand in (
        tok.gen_stop,
        tok.msg_close,
        *(getattr(tok, "extra_gen_stops", []) or []),
    ):
        if cand and cand not in out:
            out.append(str(cand))
    return out


def _channel_heads(sch) -> tuple[str, str]:
    """(content-channel head, reasoning-channel head) for a channel family.

    Reconstructed from the same fields the renderer uses, so the strings match
    what the model actually emits byte for byte — e.g. muse's
    ``<|start|>assistant to=user<|message|>``.
    """
    th = getattr(sch, "thinking", None)
    if not th or getattr(th, "style", "") != "channel" or not th.channel_token:
        return "", ""
    role = sch.roles.get("assistant", "assistant")  # roles is dict[str, str]
    open_, content = sch.tokens.msg_open, sch.tokens.msg_content
    head = f"{open_}{role}{th.channel_token}"
    return (
        f"{head}{th.content_channel}{content}" if th.content_channel else "",
        f"{head}{th.channel_name}{content}" if th.channel_name else "",
    )


def clean(text: str, family: str) -> str:
    """Return the answer the caller asked for, with the family's scaffolding gone.

    Three passes, and THE ORDER IS LOAD-BEARING:

    1. Cut at the first turn-terminator. The turn ended there; anything after
       is a restart the sampler did not catch, and it must be dropped BEFORE
       the channel split — otherwise a re-answer that follows a completed turn
       would win over the real one. (Caught by
       test_content_after_a_terminator_is_cut.)
    2. Within what survives, if the stream opened a CONTENT channel, keep the
       LONGEST pass. Not the last: when the budget cuts a restart short, the
       last pass is a fragment and the complete answer is the earlier one.
       Not the first either: a pass interrupted BY a restart is the short one.
       Longest picks correctly in both directions.
    3. Strip any residual declared markers.

    Never returns empty when the input was non-empty: if stripping would erase
    everything, the original is returned instead. A caller that asked for a
    figure to be read is better served by a messy answer than by "".
    """
    if not text:
        return text
    sch = _schema(family)
    if sch is None:
        return text.strip()

    original = text
    content_head, reasoning_head = _channel_heads(sch)

    for stop in stop_strings(family):
        idx = text.find(stop)
        if idx != -1:
            text = text[:idx]

    if content_head and content_head in text:
        # LONGEST pass, not the last one. "Last" was wrong and cost real
        # answers: measured 2026-08-12, the model wrote a complete 2,360-char
        # reading, emitted the content head to start over, and max_tokens cut
        # the second pass at 193 chars — so "last" returned the fragment and
        # discarded the finished answer. Longest is right in both directions:
        # a first pass interrupted by a restart is short, and a final pass
        # truncated by the budget is short.
        parts = text.split(content_head)
        preamble, passes = parts[0], parts[1:]
        # Text before the first content head counts as an answer ONLY if it
        # was not marked as reasoning — otherwise a long deliberation would
        # out-measure a short but correct answer.
        if preamble.strip() and not (reasoning_head and reasoning_head in preamble):
            passes = [preamble] + passes
        if passes:
            text = max(passes, key=len)
    elif reasoning_head and reasoning_head in text:
        # Reasoning opened but the content channel never did. Everything after
        # the reasoning head is deliberation, not an answer — but dropping it
        # would leave nothing, so keep it and let the caller see the state.
        pass

    markers = {
        sch.tokens.msg_open,
        sch.tokens.msg_content,
        sch.tokens.msg_close,
        sch.tokens.gen_stop,
        getattr(sch.tokens, "thinking_close", ""),
        getattr(sch.tokens, "bos", ""),
        content_head,
        reasoning_head,
    }
    for m in sorted(
        (m for m in markers if m and len(m) <= _MAX_MARKER), key=len, reverse=True
    ):
        text = text.replace(m, "")

    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text or original.strip()
