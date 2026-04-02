"""Structural atom featurizer for model output sequences.

Converts raw model output text into a sequence of discrete observation
symbols suitable for HMM/CRF training and inference. The featurizer
recognizes structural atoms from multiple model families:

  - Harmony (GPT-OSS): <|channel|>, <|start|>, <|end|>, <|message|>, <|return|>, <|call|>
  - ChatML (Qwen):     <|im_start|>, <|im_end|>, <think>, </think>
  - Mistral:           [INST], [/INST], </s>, [END], [/END]

Each atom is classified into an observation category. The HMM/CRF
learns which sequences of categories correspond to which structural
phases (thinking, delimiter, content, terminal).

Design: the featurizer operates on text (post-detokenization), not
token IDs. This keeps it decoupled from any specific tokenizer and
usable across model families. Token ID features can be added later
as an enrichment layer for CRF supervised training.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum, auto
from typing import Iterator


class ObsCategory(str, Enum):
    """Observation categories for structural atoms.

    These are the discrete symbols the HMM/CRF observes.
    Kept deliberately coarse — the model learns fine-grained
    distinctions from sequences of these categories.
    """

    # Structural markers
    ANGLE_PIPE_OPEN = "APO"    # <|
    ANGLE_PIPE_CLOSE = "APC"   # |>
    ANGLE_OPEN = "AO"          # < (not followed by |)
    ANGLE_CLOSE = "AC"         # > (not preceded by |)
    BRACKET_OPEN = "BO"        # [
    BRACKET_CLOSE = "BC"       # ]
    SLASH = "SL"               # / (inside tags)

    # Known marker words (between delimiters)
    MARKER_CHANNEL = "M_CH"    # channel
    MARKER_START = "M_ST"      # start
    MARKER_END = "M_EN"        # end
    MARKER_MESSAGE = "M_MG"    # message
    MARKER_RETURN = "M_RT"     # return
    MARKER_CALL = "M_CL"       # call
    MARKER_CONSTRAIN = "M_CN"  # constrain
    MARKER_THINK = "M_TK"      # think
    MARKER_IM_START = "M_IS"   # im_start
    MARKER_IM_END = "M_IE"     # im_end
    MARKER_INST = "M_IN"       # INST
    MARKER_END_TAG = "M_ET"    # END, /END, /INST

    # Channel names (after channel marker)
    CHAN_ANALYSIS = "C_AN"     # analysis
    CHAN_FINAL = "C_FI"        # final
    CHAN_TOOL = "C_TL"         # tool, functions

    # Content
    WORD = "W"                 # normal text word
    NEWLINE = "NL"             # \n
    WHITESPACE = "WS"          # spaces/tabs (collapsed)
    PUNCTUATION = "PN"         # .,;:!? etc
    NUMBER = "NM"              # numeric content
    CODE_CHAR = "CC"           # braces, parens, equals, etc

    # Boundary
    EOS = "EOS"                # end of sequence


@dataclass(frozen=True, slots=True)
class Atom:
    """A single structural atom with its observation category."""
    text: str
    category: ObsCategory
    offset: int      # character offset in original text
    length: int      # character length in original text


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

    Marker words (start, end, channel, return, etc.) are only classified
    as markers when they appear inside an angle-pipe sequence (i.e., the
    previous non-whitespace atom was ``<|``).  The bare word "return" in
    Python code is classified as a regular WORD, not MARKER_RETURN.

    Args:
        text: Raw model output (before any delimiter stripping).

    Returns:
        List of Atom objects, each with a text span and observation category.
    """
    atoms: list[Atom] = []
    # Track whether the previous significant atom was <| so we know
    # if a word like "return" is a structural marker or content.
    in_marker_context = False
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
            in_marker_context = True
        elif kind == "angle_pipe_close":
            cat = ObsCategory.ANGLE_PIPE_CLOSE
            # Check if we just closed a <|channel|> — next word is a channel name
            if (len(atoms) >= 2
                    and atoms[-1].category == ObsCategory.MARKER_CHANNEL):
                after_channel_marker = True
            in_marker_context = False
        elif kind == "bracket_open":
            cat = ObsCategory.BRACKET_OPEN
            in_marker_context = True  # Mistral [INST] style
        elif kind == "bracket_close":
            cat = ObsCategory.BRACKET_CLOSE
            in_marker_context = False
        elif kind == "angle_slash":
            cat = ObsCategory.ANGLE_OPEN
            in_marker_context = True  # </think> style
        elif kind == "angle_open":
            cat = ObsCategory.ANGLE_OPEN
            in_marker_context = True  # <think> style
        elif kind == "angle_close":
            cat = ObsCategory.ANGLE_CLOSE
            in_marker_context = False
        elif kind == "slash":
            cat = ObsCategory.SLASH
            # Don't reset context — slash can appear inside markers like [/INST]
        elif kind == "newline":
            cat = ObsCategory.NEWLINE
            in_marker_context = False  # Newline breaks marker context
        elif kind == "whitespace":
            cat = ObsCategory.WHITESPACE
            # Don't reset — whitespace between <| and marker word is ok
            # e.g. <| channel |> shouldn't happen but be safe
        elif kind == "number":
            cat = ObsCategory.NUMBER
            in_marker_context = False
        elif kind == "punctuation":
            cat = ObsCategory.PUNCTUATION
            in_marker_context = False
        elif kind == "code_char":
            cat = ObsCategory.CODE_CHAR
            in_marker_context = False
        elif kind == "word":
            lower = raw.lower()
            # Only classify as marker when inside a structural
            # delimiter context (after <|, <, or [).  The bare word
            # "return" in Python code is just a WORD.
            if in_marker_context and lower in _MARKER_WORDS:
                cat = _MARKER_WORDS[lower]
            elif in_marker_context and lower in _CHANNEL_NAMES:
                cat = _CHANNEL_NAMES[lower]
            elif in_marker_context and raw == "INST":
                cat = ObsCategory.MARKER_INST
            elif in_marker_context and raw == "END":
                cat = ObsCategory.MARKER_END_TAG
            elif after_channel_marker and lower in _CHANNEL_NAMES:
                # Channel name right after <|channel|> — e.g. "final", "analysis"
                cat = _CHANNEL_NAMES[lower]
            else:
                cat = ObsCategory.WORD
            in_marker_context = False
            after_channel_marker = False
        else:
            cat = ObsCategory.PUNCTUATION  # fallback for 'other'
            in_marker_context = False

        atoms.append(Atom(text=raw, category=cat, offset=offset, length=length))

    # Append EOS marker
    atoms.append(Atom(
        text="",
        category=ObsCategory.EOS,
        offset=len(text),
        length=0,
    ))

    return atoms


def atoms_to_obs_sequence(atoms: list[Atom]) -> list[str]:
    """Extract just the observation category strings from atoms.

    This is the input format for HMM/CRF training.
    """
    return [a.category.value for a in atoms]


def atoms_to_obs_ids(atoms: list[Atom]) -> list[int]:
    """Convert atoms to integer observation IDs for HMM training.

    Returns a list of integers where each integer maps to an
    ObsCategory enum member. The mapping is stable (based on
    enum definition order).
    """
    categories = list(ObsCategory)
    cat_to_id = {cat: i for i, cat in enumerate(categories)}
    return [cat_to_id[a.category] for a in atoms]


def obs_vocabulary_size() -> int:
    """Return the number of distinct observation categories."""
    return len(ObsCategory)


def obs_id_to_name(obs_id: int) -> str:
    """Convert an observation ID back to its category name."""
    categories = list(ObsCategory)
    if 0 <= obs_id < len(categories):
        return categories[obs_id].value
    return f"UNK_{obs_id}"
