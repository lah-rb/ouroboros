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


def featurize(text: str) -> list[Atom]:
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

    Args:
        text: Raw model output (before any delimiter stripping).

    Returns:
        List of Atom objects, each with a text span and observation category.
    """
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
                elif lower in _CHANNEL_NAMES:
                    cat = _CHANNEL_NAMES[lower]
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
            elif after_channel_marker and lower in _CHANNEL_NAMES:
                # Channel name right after <|channel|> — e.g. "final", "analysis"
                cat = _CHANNEL_NAMES[lower]

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
