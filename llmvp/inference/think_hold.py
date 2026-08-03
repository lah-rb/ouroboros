"""Think-hold: ban the reasoning close tag for the first N generated tokens.

WHY (laguna, 2026-08-02). Families whose template prefills the ``<think>``
opener can still skip thinking: laguna treats the opener as ADVISORY and
decides from context — under the SOUL.md agent-framework persona it closed
the prefilled opener at p(</think>)=0.96, while a vanilla chat persona
thinks at p=0.994. The veto is one token deep: with ``</think>`` banned for
the first 10 tokens, the greedy probe deflected exactly ONE close attempt,
fell into its thinking basin, produced 115 tokens of clean CoT, closed the
block ON ITS OWN well past the ban window, and answered correctly (84,
where the un-held run answered 102/144 wrong). See
configs/laguna-xs-2.1-bf16.yaml for the full isolation trail.

MECHANISM. A ``CustomSampler`` in the llama sampler chain (the same seam
the fork's ReasoningBudgetSampler and logits_processor use): while fewer
than ``hold_tokens`` tokens have been accepted, the close tag's logit is
forced to -1e30 in ``apply``; after the window it is a permanent
passthrough. One instance per stream — the counter is stream state.

ACTIVATION. Driven by the family spec (``thinking.force_open_hold_tokens``)
through ``FormatRenderer.think_hold``, which engages exactly when the
rendered generation prompt prefills the opener — so a thinking-off config,
a gate-closed level, or a family that never prefills (gemma) never holds.
"""

from __future__ import annotations

import logging
from typing import Optional

log = logging.getLogger("llm-mvp")

# Effectively -inf without risking NaN arithmetic in downstream samplers.
_BAN_LOGIT = -1e30


class ThinkHoldSampler:
    """Ban ``ban_token_id`` for the first ``hold_tokens`` accepted tokens.

    Thin composition over llama_cpp's CustomSampler (imported lazily so the
    module stays importable without the binding, e.g. in unit tests that
    drive ``_apply``/``_accept`` directly).
    """

    def __init__(self, ban_token_id: int, hold_tokens: int):
        self.ban_token_id = int(ban_token_id)
        self.hold_tokens = int(hold_tokens)
        self._count = 0
        self._custom = None  # built on demand by as_custom_sampler()

    # ── chain callbacks (also unit-testable directly) ─────────────────
    def _apply(self, token_data_array) -> None:
        if self._count >= self.hold_tokens:
            return
        data = token_data_array.data
        for i in range(int(token_data_array.size)):
            if int(data[i].id) == self.ban_token_id:
                data[i].logit = _BAN_LOGIT
                break

    def _accept(self, _token: int) -> None:
        self._count += 1

    def _reset(self) -> None:
        self._count = 0

    def as_custom_sampler(self):
        """The llama_cpp CustomSampler wrapping this instance's callbacks."""
        if self._custom is None:
            from llama_cpp._internals import CustomSampler

            self._custom = CustomSampler(
                apply_func=self._apply,
                accept_func=self._accept,
                reset_func=self._reset,
                name="think-hold",
            )
        return self._custom


def resolve_think_hold_kwargs(
    renderer, tokenizer, reasoning: Optional[str]
) -> Optional[dict]:
    """The request-level think_hold payload, or None.

    Asks the renderer whether THIS request's generation prompt prefills the
    think opener (``FormatRenderer.think_hold``), then resolves the close
    tag to a single token id. Multi-token close tags are refused loudly —
    banning only a fragment would corrupt legitimate text.
    """
    hold = getattr(renderer, "think_hold", None)
    if hold is None:
        return None
    resolved = renderer.think_hold(reasoning=reasoning)
    if not resolved:
        return None
    close_tag, n = resolved
    try:
        from inference.tokenizer import tokenize_text

        ids = tokenize_text(tokenizer, close_tag, special=True)
    except Exception:  # noqa: BLE001 — a hold must never break a request
        log.warning("think_hold: close-tag tokenization failed", exc_info=True)
        return None
    if len(ids) != 1:
        log.warning(
            "think_hold refused: close tag %r is %d tokens (need exactly 1)",
            close_tag,
            len(ids),
        )
        return None
    return {"token_id": int(ids[0]), "n": int(n)}
