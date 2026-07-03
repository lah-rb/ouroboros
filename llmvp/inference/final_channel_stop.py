"""Final-channel completion detector — the Harmony session terminator.

Harmony has an asymmetry the substring stop set cannot express: ``<|return|>``
is the response terminator, but it is CONVERTED to ``<|end|>`` the moment a turn
becomes history (schema ``history_close``; the session turn-transition appends
``<|end|>\\n``). So deep in a memoryful session, EVERY assistant message the model
sees in its KV ends with ``<|end|>`` and ``<|return|>`` appears nowhere in
context — and the max-likelihood continuation after the final answer is to close
with the history-form ``<|end|>`` and keep going (the astropy-2 runaway: a
perfect final answer, then a hallucinated next-observation that degenerated).

A stateless substring stop can't fix this: the legal analysis→final reopen uses
the identical ``<|end|><|start|>assistant`` sequence, and stopping on ``<|end|>``
truncates the analysis channel at zero content (the e75 46%-empty regression).

The terminator is STATEFUL: stop when a NON-EMPTY *final*-channel message closes
(its ``<|end|>``). This mirrors the FSM labeller's single_turn seal, applied live
to the token stream so generation ENDS at the answer instead of extraction
merely discarding the ramble afterward. Analysis/commentary closes never stop; an
empty final never stops (a truncated/empty final may precede the real one).

Harmony-only. Other families keep their native ``gen_stop`` (``<|im_end|>``,
``<end_of_turn>``, ``</s>``) which is a true response terminator with no
history-form collision, so they need no dynamic stop.
"""

from __future__ import annotations

# Byte markers (UTF-8). Matching on the cumulative byte accumulator's tail keeps
# this whitespace-insensitive and split-token safe, exactly like the backend's
# substring stop machinery.
_CHANNEL = b"<|channel|>"
_MESSAGE = b"<|message|>"
_END = b"<|end|>"
_FINAL = b"final"


class FinalChannelStop:
    """Incremental detector: has a non-empty final-channel message just closed?

    Fed the cumulative decoded byte accumulator after each token (the same
    ``acc_bytes`` the backend scans for substring stops). Tracks the Harmony
    channel grammar by scanning for the next structural marker past a cursor, so
    cost is amortized O(bytes) across the turn — no re-scan of the whole buffer.
    """

    def __init__(self) -> None:
        self._cursor = 0  # bytes consumed into channel-state tracking
        self._in_final = False  # inside a final-channel message body
        self._final_body_start = -1  # acc index where the final body began
        self.triggered = False

    def update(self, acc: bytes) -> bool:
        """Advance over any new bytes in ``acc``; return True once a non-empty
        final message has closed. Latches: stays True after the first trigger."""
        if self.triggered:
            return True
        n = len(acc)
        while self._cursor < n:
            if not self._in_final:
                # Look for the NEXT channel header from the cursor. A final
                # message is `<|channel|>final<|message|> … <|end|>`; commentary
                # can carry a `<|constrain|>` between the name and <|message|>,
                # so we locate <|message|> and check the channel name lies
                # between this <|channel|> and it, == "final".
                ch = acc.find(_CHANNEL, self._cursor)
                if ch < 0:
                    self._cursor = max(self._cursor, n - len(_CHANNEL) + 1)
                    break
                msg = acc.find(_MESSAGE, ch + len(_CHANNEL))
                if msg < 0:
                    self._cursor = ch  # header incomplete — resume here later
                    break
                header = acc[ch + len(_CHANNEL) : msg]
                # The channel name is the leading token of the header; `final`
                # must be present and no other channel name precede it.
                if header.strip().split(b"<|")[0].strip() == _FINAL:
                    self._in_final = True
                    self._final_body_start = msg + len(_MESSAGE)
                    self._cursor = self._final_body_start
                else:
                    self._cursor = msg + len(_MESSAGE)
            else:
                end = acc.find(_END, self._cursor)
                if end < 0:
                    self._cursor = max(self._cursor, n - len(_END) + 1)
                    break
                body = acc[self._final_body_start : end]
                if body.strip():  # non-empty final closed → terminate
                    self.triggered = True
                    return True
                # Empty final (e75): reopen search past this close.
                self._in_final = False
                self._cursor = end + len(_END)
        return False
