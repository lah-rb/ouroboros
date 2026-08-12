"""Structural atom featurizer for model output sequences.

Converts raw model output text into a sequence of discrete observation
atoms. The featurizer recognizes structural atoms from multiple model
families:

  - Harmony (GPT-OSS): <|channel|>, <|start|>, <|end|>, <|message|>, <|return|>, <|call|>
  - ChatML (Qwen):     <|im_start|>, <|im_end|>, <think>, </think>
  - Mistral:           [INST], [/INST], </s>, [END], [/END]

Each atom is classified into an observation category. The FSM labeller
in core.fsm_labeller walks the atom stream and emits D/T/C/E labels
based on current phase and family-specific transition rules.

Design: the featurizer operates on text (post-detokenization), not
token IDs. This keeps it decoupled from any specific tokenizer and
usable across model families.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum


class ObsCategory(str, Enum):
    """Observation categories for structural atoms.

    These are the discrete atom types the FSM labeller observes.
    Kept deliberately coarse — phase decisions depend on sequences of
    these categories, not fine-grained subtypes.
    """

    # Structural markers
    ANGLE_PIPE_OPEN = "APO"  # <|
    ANGLE_PIPE_CLOSE = "APC"  # |>
    ANGLE_OPEN = "AO"  # < (not followed by |)
    ANGLE_CLOSE = "AC"  # > (not preceded by |)
    BRACKET_OPEN = "BO"  # [
    BRACKET_CLOSE = "BC"  # ]
    SLASH = "SL"  # / (inside tags)

    # Known marker words (between delimiters)
    MARKER_CHANNEL = "M_CH"  # channel
    MARKER_START = "M_ST"  # start
    MARKER_END = "M_EN"  # end
    MARKER_MESSAGE = "M_MG"  # message
    MARKER_RETURN = "M_RT"  # return
    MARKER_CALL = "M_CL"  # call
    MARKER_CONSTRAIN = "M_CN"  # constrain
    MARKER_THINK = "M_TK"  # think
    MARKER_IM_START = "M_IS"  # im_start
    MARKER_IM_END = "M_IE"  # im_end
    MARKER_INST = "M_IN"  # INST
    MARKER_END_TAG = "M_ET"  # END, /END, /INST

    # Channel names (after channel marker)
    CHAN_ANALYSIS = "C_AN"  # analysis
    CHAN_FINAL = "C_FI"  # final
    CHAN_TOOL = "C_TL"  # tool, functions

    # Content
    WORD = "W"  # normal text word
    NEWLINE = "NL"  # \n
    WHITESPACE = "WS"  # spaces/tabs (collapsed)
    PUNCTUATION = "PN"  # .,;:!? etc
    NUMBER = "NM"  # numeric content
    CODE_CHAR = "CC"  # braces, parens, equals, etc

    # Boundary
    EOS = "EOS"  # end of sequence


@dataclass(frozen=True, slots=True)
class Atom:
    """A single structural atom with its observation category."""

    text: str
    category: ObsCategory
    offset: int  # character offset in original text
    length: int  # character length in original text


# ── Known marker words ────────────────────────────────────────────────
# Map lowercase marker text to observation category.

_MARKER_WORDS: dict[str, ObsCategory] = {
    "channel": ObsCategory.MARKER_CHANNEL,
    "start": ObsCategory.MARKER_START,
    "end": ObsCategory.MARKER_END,
    "message": ObsCategory.MARKER_MESSAGE,
    "return": ObsCategory.MARKER_RETURN,
    "call": ObsCategory.MARKER_CALL,
    "constrain": ObsCategory.MARKER_CONSTRAIN,
    "think": ObsCategory.MARKER_THINK,
    "im_start": ObsCategory.MARKER_IM_START,
    "im_end": ObsCategory.MARKER_IM_END,
    "inst": ObsCategory.MARKER_INST,
    "/inst": ObsCategory.MARKER_END_TAG,
    "/end": ObsCategory.MARKER_END_TAG,
}

_CHANNEL_NAMES: dict[str, ObsCategory] = {
    "analysis": ObsCategory.CHAN_ANALYSIS,
    "final": ObsCategory.CHAN_FINAL,
    "tool": ObsCategory.CHAN_TOOL,
    "functions": ObsCategory.CHAN_TOOL,
}


def _recipient_form(channel_token: str) -> tuple[str, str]:
    """Split a BARE-TEXT channel token into (word, separator).

    Harmony names its channel with a special token — ``<|channel|>final`` —
    and the atom pattern hands that to us pre-delimited. Muse-Glimmer names
    the same concept as a RECIPIENT in plain text: ``<|start|>assistant
    to=self<|message|>``. There is no ``<|`` to open a marker context, so the
    delimiter-aware recognition below never fires and the channel name is
    just another content word.

    Returns ("", "") for any token carrying ``<|`` (i.e. every family that
    already worked), which is what keeps this a no-op for them.
    """
    tok = (channel_token or "").strip()
    if not tok or "<|" in tok:
        return "", ""
    # Trailing non-word char is the separator; the leading word is the anchor.
    if not tok[-1].isalnum() and tok[:-1].isalnum():
        return tok[:-1].lower(), tok[-1]
    return "", ""


def _at_recipient_head(atoms: list["Atom"]) -> bool:
    """Is the recipient separator sitting at a genuine MESSAGE HEAD?

    ``to=`` is not a special token, so unlike ``<|channel|>`` it can occur in
    ordinary generated code (``call(to=user)``, ``return to==x``). Treating it
    as structural everywhere would label the identifier D and DELETE it from
    written files — the same class of corruption the bare-``<`` note above
    records. So the recipient form only counts where the grammar can actually
    put it: immediately after ``<|start|>ROLE`` , or at the head of the stream
    (our generation prompt ends with ``<|start|>assistant``, so the model's
    first emitted atoms are `` to=self``).
    """
    if len(atoms) <= 2:
        return True
    tail = atoms[-5:]
    if len(tail) < 5:
        return False
    return [a.category for a in tail] == [
        ObsCategory.MARKER_START,
        ObsCategory.ANGLE_PIPE_CLOSE,
        ObsCategory.WORD,  # role, e.g. "assistant"
        ObsCategory.WHITESPACE,
        ObsCategory.WORD,  # the recipient anchor, e.g. "to"
    ]


def _channel_vocab(
    family: str | None,
) -> tuple[dict[str, ObsCategory], str, str, set[str]]:
    """(channel names, recipient word, recipient separator, close words).

    DERIVED from ``formats/<family>.yaml``, never hardcoded. The channel
    *shape* was already derived (see fsm_labeller._ThinkShape); the channel
    *vocabulary* was not, and that gap is what shipped Muse-Glimmer with
    every completion empty: the FSM starts in DELIM and leaves it only when a
    channel name it recognises is followed by ``<|message|>``. Muse's names
    are "self"/"user", so no transition ever fired and the whole stream —
    reasoning AND the correct answer — was labelled D and discarded.

    Falls back to the Harmony table for unknown/absent families, so callers
    that pass no family behave exactly as before.
    """
    names = dict(_CHANNEL_NAMES)
    if not family:
        return names, "", "", set()
    try:
        from formats.registry import load_schema

        s = load_schema(family)
    except Exception:  # noqa: BLE001 — extraction must survive a bad family
        return names, "", "", set()
    think = s.thinking
    if think.channel_name:
        names[think.channel_name.lower()] = ObsCategory.CHAN_ANALYSIS
    if think.content_channel:
        names[think.content_channel.lower()] = ObsCategory.CHAN_FINAL
    word, sep = _recipient_form(think.channel_token)
    # Every terminator the family declares is a phase close, exactly like
    # Harmony's <|end|>. Muse needs BOTH: <|eom|> closes the reasoning message
    # and <|eot|> closes the turn — neither is in _MARKER_WORDS, so without
    # this the turn never resets, `eot` leaks into content as a bare word, and
    # the single_turn seal never fires (the model's post-answer orbit then
    # concatenates onto the real answer).
    closes = {
        (getattr(s.tokens, name, "") or "").strip("<|>").lower()
        for name in ("thinking_close", "msg_close", "gen_stop", "history_close")
    }
    closes.discard("")
    return names, word, sep, closes - set(_MARKER_WORDS)


# ── Tokenization regex ────────────────────────────────────────────────
# Splits text into structural atoms. Order matters — more specific
# patterns must come before general ones.

_ATOM_PATTERN = re.compile(
    r"""
    (?P<angle_pipe_open><\|)        |  # <| (Harmony/ChatML opener)
    (?P<angle_pipe_close>\|>)       |  # |> (Harmony/ChatML closer)
    (?P<bracket_open>\[)            |  # [ (Mistral opener)
    (?P<bracket_close>\])           |  # ] (Mistral closer)
    (?P<angle_slash></(?=\w))       |  # </ (closing tag start, e.g. </think>)
    (?P<angle_open><(?!\|))         |  # < not followed by |
    (?P<angle_close>(?<!\|)>)       |  # > not preceded by |
    (?P<slash>/)                    |  # standalone /
    (?P<newline>\n)                 |  # newline
    (?P<whitespace>[ \t]+)          |  # whitespace (collapsed)
    (?P<number>\d+(?:\.\d+)?)       |  # numbers
    (?P<punctuation>[.,;:!?])       |  # common punctuation
    (?P<code_char>[{}()\[\]=+\-*&^%$@~`\\|]) |  # code characters
    (?P<word>\w+)                   |  # word (letters, digits, underscore)
    (?P<other>.)                       # anything else
    """,
    re.VERBOSE,
)


def featurize(text: str, family: str | None = None) -> list[Atom]:
    """Convert raw model output text into a sequence of structural atoms.

    Marker words are only classified as markers when they appear inside
    a structural delimiter context. The recognition is delimiter-aware:

      - Inside ``<|...|>`` (Harmony) or ``<...>`` (ChatML), lowercase
        marker words like ``start``, ``end``, ``message``, ``channel``,
        ``return``, ``call``, ``think`` are valid — these are how the
        respective grammars name their structural tokens.
      - Inside ``[...]`` (Mistral), ONLY case-sensitive uppercase
        ``INST`` / ``END`` are recognized. Lowercase identifiers
        ``[start]`` / ``[end]`` / ``[message]`` etc. pass through as
        content. This is required because Python and JSON commonly
        contain expressions like ``tokens[start:]``, ``state[message]``,
        ``arr[end]`` — and Mistral's actual markers are uppercase.
      - Outside any delimiter, every word is content.

    The bare word ``return`` in Python code is classified as a regular
    WORD; only ``<|return|>`` is MARKER_RETURN.

    Channel names are family-derived (see ``_channel_vocab``). Families whose
    channel is a RECIPIENT in plain text (``to=self``) rather than a special
    token (``<|channel|>analysis``) are recognised through the same
    ``after_channel_marker`` path, set by the recipient separator instead of
    by ``<|channel|>``.

    Args:
        text: Raw model output (before any delimiter stripping).
        family: Format family driving the channel vocabulary. ``None`` keeps
            the Harmony table — the historical behaviour for every caller.

    Returns:
        List of Atom objects, each with a text span and observation category.
    """
    chan_names, recip_word, recip_sep, close_words = _channel_vocab(family)
    atoms: list[Atom] = []
    # Track which delimiter opened the current marker context.
    # The trigger matters because Mistral's bracket-delimited markers
    # ([INST], [/INST], [END], [/END]) use uppercase identifiers, while
    # Harmony's angle-pipe markers (<|start|>, <|end|>, <|message|>...)
    # and ChatML's angle-bracket markers (<think>, </think>) use
    # lowercase. Conflating these into one bool caused lowercase
    # identifiers in brackets — `tokens[start:]`, `state[message]`,
    # `arr[end]` — to be misclassified as Mistral markers and stripped
    # as structural by the FSM. The 17b challenge run hit this on
    # `tokens[start:]` and stalled an entire mission on a "fix" that
    # the model wrote correctly but LLMVP corrupted before it reached
    # disk.
    #
    # State values:
    #   None         — outside any marker context
    #   "angle_pipe" — last opener was <|, lowercase markers valid
    #   "angle"      — last opener was < or </, lowercase markers valid
    #   "bracket"    — last opener was [, ONLY uppercase Mistral
    #                  markers (INST/END) valid; lowercase passes
    #                  through as content
    marker_context: str | None = None
    # Track whether we just closed a <|channel|> sequence, so the
    # next word can be recognized as a channel name (e.g. "final").
    after_channel_marker = False

    for match in _ATOM_PATTERN.finditer(text):
        raw = match.group()
        offset = match.start()
        length = len(raw)
        kind = match.lastgroup

        if kind == "angle_pipe_open":
            cat = ObsCategory.ANGLE_PIPE_OPEN
            marker_context = "angle_pipe"
        elif kind == "angle_pipe_close":
            cat = ObsCategory.ANGLE_PIPE_CLOSE
            # Check if we just closed a <|channel|> — next word is a channel name
            if len(atoms) >= 2 and atoms[-1].category == ObsCategory.MARKER_CHANNEL:
                after_channel_marker = True
            marker_context = None
        elif kind == "bracket_open":
            cat = ObsCategory.BRACKET_OPEN
            marker_context = "bracket"  # Mistral [INST] / [END] style
        elif kind == "bracket_close":
            cat = ObsCategory.BRACKET_CLOSE
            marker_context = None
        elif kind == "angle_slash":
            cat = ObsCategory.ANGLE_OPEN
            marker_context = "angle"  # </think> style
        elif kind == "angle_open":
            cat = ObsCategory.ANGLE_OPEN
            marker_context = "angle"  # <think> style
        elif kind == "angle_close":
            cat = ObsCategory.ANGLE_CLOSE
            marker_context = None
        elif kind == "slash":
            cat = ObsCategory.SLASH
            # Don't reset context — slash can appear inside markers like [/INST]
        elif kind == "newline":
            cat = ObsCategory.NEWLINE
            marker_context = None  # Newline breaks marker context
        elif kind == "whitespace":
            cat = ObsCategory.WHITESPACE
            # Don't reset — whitespace between <| and marker word is ok
            # e.g. <| channel |> shouldn't happen but be safe
        elif kind == "number":
            cat = ObsCategory.NUMBER
            marker_context = None
        elif kind == "punctuation":
            cat = ObsCategory.PUNCTUATION
            marker_context = None
        elif kind == "code_char":
            cat = ObsCategory.CODE_CHAR
            marker_context = None
            # Recipient form (`to=`): the separator plays the structural role
            # <|channel|> plays for Harmony, so the NEXT word is a channel
            # name. Anchored on the preceding word so a bare `=` in code is
            # untouched, and never set for families with a `<|`-delimited
            # channel token (recip_sep is "" for them).
            if (
                recip_sep
                and raw == recip_sep
                and atoms
                and atoms[-1].text.lower() == recip_word
                and _at_recipient_head(atoms)
            ):
                after_channel_marker = True
        elif kind == "word":
            lower = raw.lower()
            cat = ObsCategory.WORD  # default — most words are content

            if marker_context == "bracket":
                # Strict uppercase recognition for Mistral. Lowercase
                # identifiers like 'start', 'end', 'return', 'message'
                # appearing in `arr[start:]` / `state[message]` etc.
                # are content, not markers. If Mistral's grammar ever
                # changes to accept lowercase, this is the line to
                # update.
                if raw == "INST":
                    cat = ObsCategory.MARKER_INST
                elif raw == "END":
                    cat = ObsCategory.MARKER_END_TAG
                elif raw == "THINK":
                    # Mistral / Tekken Magistral-style [THINK] / [/THINK]
                    # — uppercase between brackets, just like INST / END.
                    cat = ObsCategory.MARKER_THINK
            elif marker_context == "angle_pipe":
                # Genuine Harmony delimiter territory (<|start|> <|end|>
                # <|message|> <|channel|> ...): full marker + channel lookup.
                if lower in _MARKER_WORDS:
                    cat = _MARKER_WORDS[lower]
                elif lower in close_words:
                    # Family-declared terminator (<|eom|> / <|eot|>) — a phase
                    # close, same role as Harmony's <|end|>.
                    cat = ObsCategory.MARKER_END
                elif lower in chan_names:
                    cat = chan_names[lower]
            elif marker_context == "angle":
                # Bare '<' / '</': the ONLY marker that legitimately uses a bare
                # angle bracket is the ChatML inline tag <think>/</think>. The
                # Harmony pipe-markers (start/end/message/return/channel/call/
                # constrain) REQUIRE '<|'. A bare '<' before one of those words
                # is a comparison in generated code — `x < end`, `if k < start:`
                # — NOT a delimiter. Tagging it structural made the FSM strip/
                # truncate valid code before it reached disk (e.g. a B+tree
                # range method: `start <= key < end`). See the module-top
                # corruption note.
                if lower == "think":
                    cat = ObsCategory.MARKER_THINK
            elif after_channel_marker and lower in chan_names:
                # Channel name right after <|channel|> (Harmony: "final",
                # "analysis") or after the recipient separator (Muse: "self",
                # "user").
                cat = chan_names[lower]

            marker_context = None
            after_channel_marker = False
        else:
            cat = ObsCategory.PUNCTUATION  # fallback for 'other'
            marker_context = None

        atoms.append(Atom(text=raw, category=cat, offset=offset, length=length))

    # Append EOS marker
    atoms.append(
        Atom(
            text="",
            category=ObsCategory.EOS,
            offset=len(text),
            length=0,
        )
    )

    return atoms
