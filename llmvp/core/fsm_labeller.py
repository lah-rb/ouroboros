"""FSM-based delimiter/content labeller.

Encodes the known grammar of each model family's output as an explicit
state machine over the atom stream from core.featurizer. For each atom,
the FSM emits a phase label (D/T/C/E) based on current state and the
atom's category.

The atom stream produced by core.featurizer.featurize() has already
resolved the "is this a marker or content" ambiguity — marker words
inside angle-pipes or brackets are tagged as structural, content words
are tagged as W/NL/CODE_CHAR/etc. The FSM only needs to track phase
transitions, not re-disambiguate.

Labels emitted:
    D — delimiter (structural atom)
    T — thinking (content atoms inside an analysis/think region)
    C — content  (content atoms inside a final/content region)
    E — terminal (EOS atom)

The output is a list[tuple[str, str]] of (atom_text, label) pairs.
"""

from __future__ import annotations

from enum import Enum
from core.featurizer import Atom, ObsCategory, featurize


class Phase(str, Enum):
    """Current semantic phase of the atom stream."""

    # Structural / between-phase — emit D
    DELIM = "D"
    # Inside an analysis or commentary channel — emit T for content atoms
    THINKING = "T"
    # Inside a final channel — emit C for content atoms
    CONTENT = "C"


# Atom categories that are ALWAYS structural regardless of family.
# These are composite tokens (<|, |>) and marker words that the
# featurizer only emits when they appear in genuine structural
# context — e.g. MARKER_MESSAGE only fires for "message" inside
# <|...|>, not for the English word. Safe to always strip.
_ALWAYS_STRUCTURAL_CATS = {
    ObsCategory.ANGLE_PIPE_OPEN,
    ObsCategory.ANGLE_PIPE_CLOSE,
    ObsCategory.MARKER_CHANNEL,
    ObsCategory.MARKER_START,
    ObsCategory.MARKER_END,
    ObsCategory.MARKER_MESSAGE,
    ObsCategory.MARKER_RETURN,
    ObsCategory.MARKER_CALL,
    ObsCategory.MARKER_CONSTRAIN,
    ObsCategory.MARKER_IM_START,
    ObsCategory.MARKER_IM_END,
    ObsCategory.CHAN_ANALYSIS,
    ObsCategory.CHAN_FINAL,
    ObsCategory.CHAN_TOOL,
}

# Atom categories that are structural ONLY for specific families.
#
# Bare single-character atoms (ANGLE_OPEN '<', ANGLE_CLOSE '>',
# BRACKET_OPEN '[', BRACKET_CLOSE ']', SLASH '/') are NOT in these
# sets — they're ambiguous. A '>' is structural inside </think> but
# content in "if x > 0". A '[' is structural inside [INST] but
# content in "a = [1, 2]". These chars get labelled by the current
# phase and are retroactively relabelled to D when the compound
# marker (MARKER_THINK, MARKER_INST, MARKER_END_TAG) that contains
# them is recognized.
#
# Only unambiguous marker-words belong here — atoms the featurizer
# emits only inside a structural <|...|> or [...] context.
# ── Family behaviour: DERIVED from the format spec, not hardcoded ─────────
#
# Registering a family used to mean editing TWO places here — this table and
# the phase dispatch below — with nothing enforcing that you did both. The
# OLMo incident is what that costs: it was added to one and not the other, fell
# to the unknown-family default, and left a "think>" residue at the head of
# extracted CONTENT (a SyntaxError as line 1 of combat.py, 2026-07-23) while
# capturing thinking_content=0 on every call.
#
# Both behaviours are derivable from what formats/<family>.yaml ALREADY
# declares, so a new family now needs ZERO edits in this file:
#
#   thinking.style == "channel"      -> harmony shape (start DELIM)
#   thinking.style == "inline_tags"  -> tag shape, angle vs bracket read from
#                                       the literal open_tag
#   framing contains "[INST]"        -> mistral INST markers are structural
#
# Overrides below are for families whose SPEC and FSM behaviour genuinely
# disagree, each with the reason stated. Do not add one to avoid understanding
# a family — that is how the parallel table started.


class _ThinkShape(str, Enum):
    CHANNEL = "channel"      # harmony: <|channel|>name<|message|>
    ANGLE = "angle"          # chatml/olmo/laguna: <think></think>
    BRACKET = "bracket"      # tekken/mistral: [THINK][/THINK]
    NONE = "none"            # pure content from the first token


# family -> (shape, reason). ONLY for real spec/FSM disagreements.
_SHAPE_OVERRIDES: dict[str, tuple["_ThinkShape", str]] = {
    # gemma declares style: inline_tags with <|channel>thought / <channel|>,
    # but its template PRE-SUPPLIES an already-closed empty thought block, so
    # in practice generation is pure content from the first token and the
    # featurizer emits no think markers for those literals. Deriving ANGLE
    # here would change a working family's start phase on a technicality.
    "gemma": (_ThinkShape.NONE, "template pre-closes the thought block"),
}


# Family names the FSM is called with that have NO format spec of their own.
# "mistral" is the legacy name for the tekken family (formats/tekken.yaml) —
# there is no formats/mistral.yaml, and callers still pass it (see the
# test_mistral_* cases). This alias was previously encoded only IMPLICITLY, by
# repeating tekken's values under a "mistral" key in the hardcoded table; the
# derivation refactor surfaced it. Keep it explicit rather than reintroducing
# a parallel table.
_FAMILY_ALIASES: dict[str, str] = {"mistral": "tekken"}


def _spec_for(family: str):
    """The format spec, or None when it cannot be loaded.

    None is not a failure path to fix — the FSM must keep labelling if the
    format package is unavailable (it is imported by tooling that does not
    always have the server's config). Callers fall back to the safe default.
    """
    try:
        from formats.registry import load_schema

        return load_schema(_FAMILY_ALIASES.get(family, family))
    except Exception:  # noqa: BLE001 — labelling must not depend on formats
        return None


def _shape_for(family: str) -> "_ThinkShape":
    if family in _SHAPE_OVERRIDES:
        return _SHAPE_OVERRIDES[family][0]
    spec = _spec_for(family)
    if spec is None:
        return _ThinkShape.CHANNEL  # unknown -> DELIM, the safe default
    style = getattr(spec.thinking, "style", "")
    if style == "channel":
        return _ThinkShape.CHANNEL
    if style == "none":
        return _ThinkShape.NONE
    open_tag = getattr(spec.thinking, "open_tag", "") or ""
    return _ThinkShape.BRACKET if open_tag.startswith("[") else _ThinkShape.ANGLE


def _tag_tail_for(family: str) -> str:
    """Text a family puts BETWEEN the marker word and the tag's closing char.

    ``<think>`` has none. Hunyuan-3's tags are ``<think:opensource>`` — every
    special token in that family carries a build-variant suffix — so ``think``
    is followed by ``:opensource`` before the ``>``.

    That broke extraction on the first live generation (2026-07-28): the
    labeller arms its closer lookahead for the atom IMMEDIATELY after the
    marker word, ``:opensource`` is not that closer, and the fragment fell
    through as CONTENT. Every answer came back as
    ``:opensource>def reverse_string(s): ...`` — which written to disk is a
    SyntaxError on line 1. That is the OLMo residue incident (2026-07-23)
    reproduced in a new family, and the reason this is derived from the spec
    rather than special-cased: the next family with a suffixed tag gets it
    free, and a family without one is unaffected (empty tail).
    """
    spec = _spec_for(family)
    if spec is None:
        return ""
    for attr in ("open_tag", "close_tag"):
        tag = (getattr(spec.thinking, attr, "") or "").strip()
        if not tag or len(tag) < 3:
            continue
        inner = tag[1:-1].lstrip("/")  # drop the brackets and any leading slash
        low = inner.lower()
        if low.startswith("think") and len(inner) > 5:
            return inner[5:]  # everything after the marker word
    return ""


def _uses_inst_framing(family: str) -> bool:
    """Whether [INST]-style framing markers are structural for this family —
    read from the framing tokens rather than inferred from the thinking tags,
    because they are independent properties that only happen to coincide in
    the mistral family today."""
    spec = _spec_for(family)
    if spec is None:
        return False
    framing = (getattr(spec.tokens, "msg_open", "") or "") + (
        getattr(spec.tokens, "msg_close", "") or ""
    )
    for rt in (getattr(spec, "role_tokens", {}) or {}).values():
        framing += (getattr(rt, "msg_open", "") or "") + (
            getattr(rt, "msg_close", "") or ""
        )
    return "[INST]" in framing


def _structural_cats_for(family: str) -> set:
    """Atom categories treated as structural (→ D label) for this family.

    Derived from the format spec — see the note above _ThinkShape.
    """
    cats = set(_ALWAYS_STRUCTURAL_CATS)
    shape = _shape_for(family)
    if shape in (_ThinkShape.ANGLE, _ThinkShape.BRACKET):
        cats.add(ObsCategory.MARKER_THINK)
    if _uses_inst_framing(family):
        cats |= {ObsCategory.MARKER_INST, ObsCategory.MARKER_END_TAG}
    return cats


def _chatml_start_phase(atoms: list["Atom"]) -> "Phase":
    """Choose the initial phase for ChatML based on stream content.

    ChatML has three operating modes depending on the chat template:
        (a) pure content — no <think> markers anywhere
        (b) explicit thinking — both <think> and </think> emitted
        (c) prefilled thinking — only </think> in the stream; the
            opening <think> was injected into the prompt by the
            template and lives in the KV cache, not the output.

    We pre-scan the atom stream once to find where <think> vs </think>
    first appear. The featurizer always emits <think> and </think> as
    the sequence [ANGLE_OPEN, MARKER_THINK, ANGLE_CLOSE]; the opener
    differs only in ANGLE_OPEN.text length ('<' = 1, '</' = 2).

    Returns the correct starting phase:
        - CONTENT for case (a) — will stay there with no markers.
        - CONTENT for case (b) — <think> will flip to THINKING.
        - THINKING for case (c) — </think> will flip back to CONTENT.
    """
    first_open_think_idx = -1
    first_close_think_idx = -1
    for i, atom in enumerate(atoms):
        if atom.category != ObsCategory.MARKER_THINK:
            continue
        if i == 0:
            continue
        prev = atoms[i - 1]
        if prev.category != ObsCategory.ANGLE_OPEN:
            continue
        is_close = len(prev.text) >= 2  # '</' is length 2, '<' is length 1
        if is_close and first_close_think_idx < 0:
            first_close_think_idx = i
        elif not is_close and first_open_think_idx < 0:
            first_open_think_idx = i
        # Keep scanning in case both exist — we need the relative order
        if first_open_think_idx >= 0 and first_close_think_idx >= 0:
            break

    # Case (c): </think> appears before any <think>, or no <think> at all
    # AND </think> is present → prefilled thinking, start in THINKING.
    if first_close_think_idx >= 0 and (
        first_open_think_idx < 0 or first_close_think_idx < first_open_think_idx
    ):
        return Phase.THINKING

    # Cases (a) and (b): either no markers, or <think> comes first.
    # Start in CONTENT; the <think> marker (if any) will flip the phase.
    return Phase.CONTENT


def _bracket_think_start_phase(atoms: list["Atom"]) -> "Phase":
    """Choose the initial phase for Tekken/Mistral based on stream content.

    Mirrors :func:`_chatml_start_phase` for families that use bracket-
    tagged thinking markers. Four operating modes, ordered by priority:

        (1) stream includes a user turn — [INST]...[/INST]... pattern
            means we've been handed prompt+response together. The
            user-prompt prose must be stripped: start in DELIM and let
            the existing [/INST] handler flip us to CONTENT.
        (2) prefilled thinking — only [/THINK] in the stream with no
            [INST] opener; the opening [THINK] was primed by the
            template. Start in THINKING.
        (3) explicit thinking — [THINK] appears; start in CONTENT and
            let the inline-tag handler flip to THINKING on [THINK].
        (4) pure content (historical Mistral Instruct default and
            typical Tekken): no markers at all. Start in CONTENT.

    Atom patterns from the featurizer:
        [INST]   = BRACKET_OPEN + MARKER_INST + BRACKET_CLOSE
        [/INST]  = BRACKET_OPEN + SLASH + MARKER_INST + BRACKET_CLOSE
        [THINK]  = BRACKET_OPEN + MARKER_THINK + BRACKET_CLOSE
        [/THINK] = BRACKET_OPEN + SLASH + MARKER_THINK + BRACKET_CLOSE

    The distinguishing atom immediately before MARKER_THINK:
        - BRACKET_OPEN directly → opening  [THINK]
        - SLASH (preceded by BRACKET_OPEN) → closing [/THINK]
    """
    has_inst = any(atom.category == ObsCategory.MARKER_INST for atom in atoms)
    if has_inst:
        return Phase.DELIM

    first_open_think_idx = -1
    first_close_think_idx = -1
    for i, atom in enumerate(atoms):
        if atom.category != ObsCategory.MARKER_THINK:
            continue
        if i == 0:
            continue
        prev = atoms[i - 1]
        if prev.category == ObsCategory.SLASH and i >= 2:
            # Check that it's a genuine [/THINK] sequence: BRACKET_OPEN
            # must precede the SLASH. Otherwise a stray slash next to
            # the word "think" in content would falsely trigger.
            prev2 = atoms[i - 2]
            if prev2.category != ObsCategory.BRACKET_OPEN:
                continue
            if first_close_think_idx < 0:
                first_close_think_idx = i
        elif prev.category == ObsCategory.BRACKET_OPEN:
            if first_open_think_idx < 0:
                first_open_think_idx = i
        if first_open_think_idx >= 0 and first_close_think_idx >= 0:
            break

    if first_close_think_idx >= 0 and (
        first_open_think_idx < 0 or first_close_think_idx < first_open_think_idx
    ):
        return Phase.THINKING

    return Phase.CONTENT


def label_atoms(
    atoms: list[Atom], family: str = "harmony", single_turn: bool = True
) -> list[tuple[str, str]]:
    """Label each atom with D/T/C/E using an explicit FSM.

    Args:
        atoms: Atom stream from featurize().
        family: Model family — "harmony", "chatml", "tekken", "mistral".
                Determines the initial phase. Families with explicit
                thinking markers (harmony, chatml) start in DELIM and
                transition to CONTENT when a final-channel marker or
                </think> closer is seen. Families without thinking
                markers in the generation (tekken, mistral) start in
                CONTENT immediately since the whole generation is
                assistant output.
        single_turn: The stream is ONE logical generation (the default —
                every extraction call site labels a single model turn).
                Seals the labelling after the first NON-EMPTY content
                phase closes: everything past that close labels D. A
                model that keeps generating after its final answer is
                self-playing (the a7ff rambling pathology — it chains
                fake turns, hallucinates the next observation, and can
                spiral into a degenerate loop; see the astropy-2 runaway
                capture 20260703T152322). Without the seal, extraction
                CONCATENATED every "final" channel, so a completed
                ramble polluted the real answer with the hallucination.
                An EMPTY first content phase does not seal (the e75
                lesson: analysis→final legitimately reopens the
                assistant role mid-turn, and a truncated/empty final
                must not block a later real one). Pass False to label a
                multi-turn transcript verbatim.

    Returns:
        List of (atom_text, label) pairs suitable for downstream phase
        extraction (see extract_content, extract_phases).

    State transitions are triggered by specific structural sequences
    in the atom stream. Whitespace/newlines between markers don't
    change state; they're classified by the phase they fall inside
    (D between markers, T inside analysis, C inside final).
    """
    # Pick the starting phase based on family.
    #
    # Families split by how their generation stream relates to content:
    #
    # - harmony: stream is always <|channel|> ... <|message|> ... ;
    #   start in DELIM and transition into THINKING/CONTENT when the
    #   channel-name followed by <|message|> is seen.
    #
    # - chatml: may be pure-content (no markers), explicit <think>
    #   thinking, or prefilled thinking (</think> without opener).
    #   See _chatml_start_phase.
    #
    # - tekken / mistral: same three flavours as chatml but with
    #   bracket markers [THINK]/[/THINK]. The historical default is
    #   pure content, but Magistral etc. add inline thinking.
    #   See _bracket_think_start_phase.
    # DERIVED from the format spec (see _ThinkShape) — this used to be a
    # second hardcoded family list that had to be kept in sync with the
    # structural-category table by hand, which is exactly how OLMo broke.
    shape = _shape_for(family)
    if shape is _ThinkShape.CHANNEL:
        phase = Phase.DELIM
    elif shape is _ThinkShape.ANGLE:
        phase = _chatml_start_phase(atoms)
    elif shape is _ThinkShape.BRACKET:
        phase = _bracket_think_start_phase(atoms)
    elif shape is _ThinkShape.NONE:
        # Gemma has no thinking markers — generation is pure content from
        # the first token (like the no-think Tekken/Mistral case).
        phase = Phase.CONTENT
    else:
        # Unknown family — default to DELIM and rely on family-specific
        # markers to trigger content transitions. Safer than CONTENT
        # because a misrouted family won't silently leak structural
        # tokens into extracted content.
        phase = Phase.DELIM

    # Build the family-specific structural category set once.
    # Atoms in this set are always labelled D; everything else
    # takes its label from the current phase.
    structural_cats = _structural_cats_for(family)

    # Tracking sub-states for recognizing compound transitions.
    # Harmony: <|channel|> ... <|message|> needs both seen.
    saw_chan_final = False
    saw_chan_analysis_or_commentary = False
    # Qwen inline-tag: <think>, </think>. The featurizer categorizes
    # both <think> and </think> with a MARKER_THINK atom, preceded
    # by ANGLE_OPEN. We distinguish by the ANGLE_OPEN's text length.
    # Mistral: [INST]...[/INST]. [/INST] ends the user turn; everything
    # after is content.

    result: list[tuple[str, str]] = []

    # single_turn seal state: content_seen flips on the first non-whitespace
    # C-labelled atom; sealed flips when a non-empty content phase CLOSES
    # (MARKER_END / MARKER_IM_END). Once sealed, every later atom labels D —
    # post-answer self-play is structural noise, never content.
    content_seen = False
    sealed = False

    # When we recognize a compound marker like <think>, </think>, [INST],
    # [/INST], [THINK], [/THINK], the individual single-char atoms that
    # make up that compound must be labelled D even though they're not
    # in the always-structural set. Two mechanisms:
    #
    # 1. Retroactive: when the marker-word atom (MARKER_THINK,
    #    MARKER_INST, MARKER_END_TAG) is encountered at index i, we
    #    look back at the last 1–2 result entries and flip their
    #    labels to D if they are the compound's preceding chars.
    #
    # 2. Lookahead for the trailing close-char: after recognizing a
    #    compound, we set `pending_close_cats` — the set of atom
    #    categories that, if they appear as the VERY NEXT atom, get
    #    labelled D as the compound's closer (e.g. '>' after <think,
    #    ']' after [THINK).
    pending_close_cats: set = set()
    # Declared text between the marker word and the tag's closer,
    # e.g. ':opensource' for hunyuan3. Empty for every other family.
    tag_tail = _tag_tail_for(family)
    pending_tail = ""

    def _relabel(idx: int, new_label: str) -> None:
        """Flip a previously emitted atom's label."""
        if 0 <= idx < len(result):
            text, _old = result[idx]
            result[idx] = (text, new_label)

    for i, atom in enumerate(atoms):
        cat = atom.category

        # ── EOS is always E ───────────────────────────────────────
        if cat == ObsCategory.EOS:
            result.append((atom.text, "E"))
            pending_close_cats = set()
            continue

        # ── Sealed (single_turn): the answer already completed ────
        if sealed:
            result.append((atom.text, "D"))
            continue

        # ── Structural atoms always emit D, regardless of phase ──
        is_structural = cat in structural_cats

        # ── Harmony: channel-name hints upcoming phase ──────────
        if cat == ObsCategory.CHAN_FINAL:
            saw_chan_final = True
            saw_chan_analysis_or_commentary = False
        elif cat == ObsCategory.CHAN_ANALYSIS:
            saw_chan_analysis_or_commentary = True
            saw_chan_final = False
        elif cat == ObsCategory.CHAN_TOOL:
            # Treat tool channel like analysis (not user-facing content)
            saw_chan_analysis_or_commentary = True
            saw_chan_final = False

        # ── Harmony: <|message|> completes a channel transition ──
        if cat == ObsCategory.MARKER_MESSAGE:
            if saw_chan_final:
                phase = Phase.CONTENT
                saw_chan_final = False
            elif saw_chan_analysis_or_commentary:
                phase = Phase.THINKING
                saw_chan_analysis_or_commentary = False
            # else: bare <|message|> without channel — stay in current
            # phase; the subsequent <|end|> will eventually reset us

        # ── Harmony: <|end|> closes any phase → back to DELIM ────
        # (Handled AFTER the atom itself is labeled D below.)

        # ── Think marker inline transitions ──────────────────────
        # Angle form (ChatML):
        #   <think>  = ANGLE_OPEN(len=1) + MARKER_THINK + ANGLE_CLOSE
        #   </think> = ANGLE_OPEN(len=2) + MARKER_THINK + ANGLE_CLOSE
        # Bracket form (Tekken/Mistral):
        #   [THINK]  = BRACKET_OPEN + MARKER_THINK + BRACKET_CLOSE
        #   [/THINK] = BRACKET_OPEN + SLASH + MARKER_THINK + BRACKET_CLOSE
        #
        # When we recognize the compound, retroactively relabel its
        # prefix chars to D, and arm pending_close_cats for the closer.
        if cat == ObsCategory.MARKER_THINK and i > 0:
            prev = atoms[i - 1]
            if prev.category == ObsCategory.ANGLE_OPEN:
                if len(prev.text) >= 2:  # "</" closing
                    phase = Phase.CONTENT
                else:  # "<" opening
                    phase = Phase.THINKING
                _relabel(i - 1, "D")  # the '<' or '</'
                pending_close_cats = {ObsCategory.ANGLE_CLOSE}
                # Families whose tag carries a suffix (<think:opensource>)
                # put atoms between the marker word and the '>'. Consume
                # exactly the declared suffix — never more — so a family
                # without one is untouched.
                pending_tail = tag_tail
            elif prev.category == ObsCategory.BRACKET_OPEN:
                # [THINK] opening
                phase = Phase.THINKING
                _relabel(i - 1, "D")  # the '['
                pending_close_cats = {ObsCategory.BRACKET_CLOSE}
            elif prev.category == ObsCategory.SLASH and i >= 2:
                # [/THINK] closing — validate full pattern
                prev2 = atoms[i - 2]
                if prev2.category == ObsCategory.BRACKET_OPEN:
                    phase = Phase.CONTENT
                    _relabel(i - 2, "D")  # the '['
                    _relabel(i - 1, "D")  # the '/'
                    pending_close_cats = {ObsCategory.BRACKET_CLOSE}

        # ── Mistral [INST] / [/INST] transitions ─────────────────
        # [INST]  = BRACKET_OPEN + MARKER_INST + BRACKET_CLOSE
        # [/INST] = BRACKET_OPEN + SLASH + MARKER_INST + BRACKET_CLOSE
        if cat == ObsCategory.MARKER_INST and i > 0:
            prev = atoms[i - 1]
            if prev.category == ObsCategory.SLASH and i >= 2:
                # [/INST] ends user turn, assistant content begins
                prev2 = atoms[i - 2]
                if prev2.category == ObsCategory.BRACKET_OPEN:
                    phase = Phase.CONTENT
                    _relabel(i - 2, "D")  # the '['
                    _relabel(i - 1, "D")  # the '/'
                    pending_close_cats = {ObsCategory.BRACKET_CLOSE}
            elif prev.category == ObsCategory.BRACKET_OPEN:
                # [INST] opens a user turn — stay in DELIM until [/INST]
                _relabel(i - 1, "D")  # the '['
                pending_close_cats = {ObsCategory.BRACKET_CLOSE}

        # ── ChatML <|im_end|> → back to DELIM ────────────────────
        if cat == ObsCategory.MARKER_IM_END:
            if single_turn and phase == Phase.CONTENT and content_seen:
                sealed = True  # first non-empty content phase closed
            phase = Phase.DELIM

        # ── Emit label for this atom ─────────────────────────────
        # Compound-marker atoms (MARKER_THINK, MARKER_INST etc.) that
        # just set pending_close_cats above — we emit them as D but
        # must NOT clear the pending expectation, since the closer
        # arrives on the next iteration.
        compound_marker_cats = {
            ObsCategory.MARKER_THINK,
            ObsCategory.MARKER_INST,
            ObsCategory.MARKER_END_TAG,
        }
        if is_structural:
            result.append((atom.text, "D"))
            # Only reset pending_close_cats when the structural atom
            # is NOT the compound marker that just armed it.
            if cat not in compound_marker_cats:
                pending_close_cats = set()
                pending_tail = ""
        elif pending_tail and pending_tail.startswith(atom.text):
            # Part of the declared tag suffix, not content. Matching by TEXT
            # (not category) is what keeps this from swallowing real output:
            # it can only consume atoms the family said would be there.
            result.append((atom.text, "D"))
            pending_tail = pending_tail[len(atom.text):]
        elif cat in pending_close_cats:
            # This atom closes the compound marker we just recognized.
            result.append((atom.text, "D"))
            pending_close_cats = set()
            pending_tail = ""
        else:
            # Content atom: label by current phase
            if phase == Phase.THINKING:
                result.append((atom.text, "T"))
            elif phase == Phase.CONTENT:
                result.append((atom.text, "C"))
                if atom.text.strip():
                    content_seen = True
            else:
                # In DELIM phase, content atoms shouldn't normally
                # appear — but if they do (pre-amble text before any
                # channel marker, or stray text between turns), tag
                # them D so they get stripped out with the markers.
                result.append((atom.text, "D"))
            # Any non-close atom cancels the pending expectation.
            pending_close_cats = set()

        # ── Post-atom: <|end|> resets us to DELIM phase ──────────
        # This is the key fix over the CRF: <|end|> always brings us
        # back to structural territory, regardless of what came before.
        # The next <|channel|> will put us back in a content phase —
        # unless the single_turn seal fires: a NON-EMPTY content phase
        # closing here means the answer is complete, and any further
        # generation is self-play (see the docstring).
        if cat == ObsCategory.MARKER_END:
            if single_turn and phase == Phase.CONTENT and content_seen:
                sealed = True
            phase = Phase.DELIM
            saw_chan_final = False
            saw_chan_analysis_or_commentary = False

    return result


def extract_content(
    atoms: list[Atom], family: str = "harmony", single_turn: bool = True
) -> str:
    """Extract only the content-phase text, joined back together."""
    labelled = label_atoms(atoms, family=family, single_turn=single_turn)
    content_parts = [text for text, lbl in labelled if lbl == "C"]
    return "".join(content_parts).strip()


def extract_phases(
    atoms: list[Atom], family: str = "harmony", single_turn: bool = True
) -> dict[str, str]:
    """Extract text grouped by phase label. Returns a dict keyed by
    D/T/C/E with the concatenated text for each label, stripped."""
    labelled = label_atoms(atoms, family=family, single_turn=single_turn)
    phases: dict[str, list[str]] = {"D": [], "T": [], "C": [], "E": []}
    for text, lbl in labelled:
        phases[lbl].append(text)
    return {lbl: "".join(parts).strip() for lbl, parts in phases.items()}


# ── Convenience wrappers that take raw text ──────────────────────────


def fsm_decode(
    raw_text: str, family: str = "harmony", single_turn: bool = True
) -> list[tuple[str, str]]:
    """Featurize raw text and return per-atom (text, label) pairs."""
    atoms = featurize(raw_text)
    return label_atoms(atoms, family=family, single_turn=single_turn)


def fsm_extract_content(
    raw_text: str, family: str = "harmony", single_turn: bool = True
) -> str:
    """Featurize raw text and return only the content-phase text."""
    atoms = featurize(raw_text)
    return extract_content(atoms, family=family, single_turn=single_turn)


def fsm_extract_phases(
    raw_text: str, family: str = "harmony", single_turn: bool = True
) -> dict[str, str]:
    """Featurize raw text and return all phases as a {D,T,C,E} dict."""
    atoms = featurize(raw_text)
    return extract_phases(atoms, family=family, single_turn=single_turn)
