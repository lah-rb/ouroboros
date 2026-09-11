"""Deterministic gate for LLM-written placeholder TEMPLATES (stdlib only).

WHAT A TEMPLATE IS. The synthesis flow set (flows/synth) asks a text model
for prompt/completion pairs whose every VALUE is a ``{slot}`` placeholder --
``"{species} shows its strongest Raman band at {band_strongest}"`` -- and a
deterministic filler (dev/rock_olmo/synth_render.py) later fills the slots
from the facts layer. The model writes wording; it never writes numbers,
mineral names or formulas. That division is what makes a synthetic corpus
trustworthy: no document can state a value the reference data does not hold.

THIS MODULE IS THE CONTRACT, checked by construction in both places that
consume templates: the flow action banks only templates that pass, and the
filler re-runs the same checks before rendering (defence in depth across the
two venvs -- this module is stdlib so both can import it).

WHY EACH CHECK EXISTS
  * digits            a literal number is a fact the model invented; every
                      number belongs to a slot ("532 nm" -> "{laser}")
  * unknown slot      the filler would raise KeyError; the spec (spec.json)
                      lists every slot a kind can fill
  * direction         forward: the prompt names the SUBJECT and the
                      completion carries the ANSWER slots; backward: the
                      prompt carries the value slots and the completion names
                      the species -- the mirrored frames are what fix the
                      reversal curse, so a "backward" template that still
                      names the species in its prompt teaches nothing
  * literal entity    a mineral name in the template would bind wording to
                      one species (and leak it into every fill)
  * duplicate /       Allen-Zhu & Li: gains come from STRUCTURALLY distinct
    near-duplicate    framings; a synonym swap is one framing, not two
  * forbidden         the v4 frames and the never-trained PROBE frames must
                      not re-enter the bank (the probe frames are the
                      instrument that measures template collapse)
  * render            ``str.format`` on real sample fills, exactly as the
                      filler does, so a template that passes here cannot
                      raise later
"""

from __future__ import annotations

import hashlib
import re
from typing import Any, Iterable, Optional

# Slot names are Python identifiers (fields() has `T`, `libs_T`, `top3`):
# case and digits are allowed INSIDE a name; the spec decides which names exist.
SLOT_RE = re.compile(r"#?\{([A-Za-z_][A-Za-z0-9_]*)\}")
# Any brace group that is NOT a well-formed slot: "{ }", "{0}", "{a b}".
BAD_BRACE_RE = re.compile(r"#?\{(?![A-Za-z_][A-Za-z0-9_]*\})[^{}]*\}")
DIGIT_RE = re.compile(r"[0-9⁰-⁹₀-₉²³¹]")
# Unit spellings whose digits are not values: a template may say "g/cm³" or
# "cm-1" around a slot. Removed before the digit check (round-5 rejects).
UNIT_RE = re.compile(r"g/cm(?:3|³|\^3)|cm(?:-1|⁻¹|\^-1|\^\{-1\})|µm|μm|nm\b|Å")
_TOKEN_RE = re.compile(r"<[a-z][a-z0-9_]*>|[a-z]+")
_NUMBER_RE = re.compile(r"\d+(?:\.\d+)?")
_NON_LATIN_RE = re.compile(r"[Ѐ-ӿ぀-ヿ一-鿿가-힯؀-ۿ]")

DIRECTIONS = ("forward", "backward")
FORMS = ("statement", "question")


def normalise_slots(text: str) -> str:
    """``#{slot}`` -> ``{slot}``. The prompt tells the model either spelling
    is fine; the bank holds one."""
    return SLOT_RE.sub(lambda m: "{" + m.group(1) + "}", text or "")


def slots_in(text: str) -> list[str]:
    """Slot names in order of first appearance, unique."""
    out: list[str] = []
    for m in SLOT_RE.finditer(text or ""):
        if m.group(1) not in out:
            out.append(m.group(1))
    return out


def skeleton(text: str) -> list[str]:
    """Lower-cased word tokens with slots kept as ``<slot>`` markers --
    the structural skeleton two templates are compared on."""
    t = SLOT_RE.sub(lambda m: " <" + m.group(1) + "> ", text or "").lower()
    return _TOKEN_RE.findall(t)


def signature(prompt: str, completion: str) -> str:
    """Opening (first three skeleton tokens of the prompt) plus slot order.

    Two templates with the same signature start the same way and lay their
    slots out the same way: that is a duplicate framing however the middle
    is worded.
    """
    head = " ".join(skeleton(prompt)[:3])
    order = ",".join(slots_in((prompt or "") + " " + (completion or "")))
    return f"{head}|{order}"


def _ngrams(tokens: list[str], n: int) -> set[tuple[str, ...]]:
    if len(tokens) < n:
        return {tuple(tokens)} if tokens else set()
    return {tuple(tokens[i : i + n]) for i in range(len(tokens) - n + 1)}


def skeleton_jaccard(a: str, b: str, n: int = 2) -> float:
    """Jaccard similarity of word n-grams over the two skeletons.

    Bigrams by default: a one-word synonym swap in a twelve-token template
    keeps ~10/14 bigrams (0.71) but only ~6/12 trigrams (0.50), and the swap
    is exactly the non-diversity the gate must catch. A structurally new
    framing shares almost no bigrams with the banked ones.
    """
    ga, gb = _ngrams(skeleton(a), n), _ngrams(skeleton(b), n)
    if not ga or not gb:
        return 0.0
    return len(ga & gb) / len(ga | gb)


def trigram_jaccard(a: str, b: str) -> float:
    return skeleton_jaccard(a, b, 3)


def template_id(
    kind: str, family: str, direction: str, prompt: str, completion: str
) -> str:
    h = hashlib.sha256(
        "|".join(
            [
                kind,
                family,
                direction,
                normalise_slots(prompt).strip(),
                normalise_slots(completion).strip(),
            ]
        ).encode("utf-8")
    ).hexdigest()
    return h[:16]


def looks_english(text: str) -> bool:
    """A cheap script check: no CJK/Cyrillic/Arabic/Hangul letters."""
    return not _NON_LATIN_RE.search(text or "")


def literal_entities(text: str, known_entities: Iterable[str]) -> list[str]:
    """Known entity names appearing literally (outside slots), lower-cased.

    Single-word names only: multi-word IMA names are rare and a two-word scan
    would start matching ordinary phrases. The export drops element-metal
    names (iron, copper, ...) from the entity list because those words are
    legitimate chemistry vocabulary.
    """
    known = (
        known_entities
        if isinstance(known_entities, (set, frozenset))
        else set(known_entities)
    )
    if not known:
        return []
    stripped = SLOT_RE.sub(" ", text or "").lower()
    words = set(re.findall(r"[a-z][a-z\-]+", stripped))
    return sorted(w for w in words if w in known)


def render_check(
    prompt: str, completion: str, sample_fills: list[dict]
) -> Optional[str]:
    """``str.format`` both halves on every sample fill -- what the filler does.
    Returns the first error text, or None."""
    for fill in sample_fills or []:
        try:
            normalise_slots(prompt).format(**fill)
            normalise_slots(completion).format(**fill)
        except (KeyError, IndexError, ValueError, AttributeError) as exc:
            return f"render failed: {type(exc).__name__}: {exc}"
    return None


def numbers_survive(rendered: str, fill: dict) -> bool:
    """Every number carried by the fill's values appears verbatim in the
    rendered text. The filler asserts this per document."""
    for v in (fill or {}).values():
        for num in _NUMBER_RE.findall(str(v)):
            if num not in rendered:
                return False
    return True


def validate_template(
    t: dict,
    kind_spec: dict,
    *,
    banked_sigs: Iterable[str] = (),
    banked_texts: Iterable[str] = (),
    known_entities: Iterable[str] = (),
    forbidden_sigs: Iterable[str] = (),
    near_dup: float = 0.6,
) -> list[str]:
    """Every problem with one candidate template; [] means it passes.

    ``kind_spec`` is one entry of spec.json's ``kinds``: ``slots`` (name ->
    description), ``subject_slots``, ``answer_slots`` ({"forward": [...],
    "backward": [...]}) and ``sample_fills``. ``banked_*`` describe templates
    already accepted for the SAME (kind, family, direction) cell.
    """
    problems: list[str] = []
    prompt = normalise_slots(str(t.get("prompt") or ""))
    completion = normalise_slots(str(t.get("completion") or ""))
    direction = str(t.get("direction") or "")
    if not prompt.strip():
        problems.append("empty prompt")
    if not completion.strip():
        problems.append("empty completion")
    if direction not in DIRECTIONS:
        problems.append(f"unknown direction {direction!r}")
    form = str(t.get("form") or "statement")
    if form not in FORMS:
        problems.append(f"unknown form {form!r}")
    if problems:
        return problems

    both = prompt + " " + completion
    # digits are judged on the WORDING only: a slot NAME may carry one
    # ({top3}, {libs_top3}); the 2026-09-11 round-1 gate rejected every
    # template that used those slots
    if DIGIT_RE.search(UNIT_RE.sub(" ", SLOT_RE.sub(" ", both))):
        problems.append("digit in template")
    if BAD_BRACE_RE.search(both):
        problems.append("malformed placeholder")
    if not looks_english(both):
        problems.append("non-Latin script")

    allowed = set((kind_spec.get("slots") or {}).keys())
    used = slots_in(both)
    unknown = [s for s in used if s not in allowed]
    if unknown:
        problems.append("unknown slot " + ", ".join(unknown))
    if not used:
        problems.append("no slots")

    subject = set(kind_spec.get("subject_slots") or ("species", "sp_f"))
    answers = kind_spec.get("answer_slots") or {}
    fwd_answers = set(answers.get("forward") or ())
    bwd_answers = set(answers.get("backward") or subject)
    p_slots, c_slots = set(slots_in(prompt)), set(slots_in(completion))
    if direction == "forward":
        if not p_slots & subject:
            problems.append("no subject slot in prompt (forward)")
        if fwd_answers and not c_slots & fwd_answers:
            problems.append("no answer slot in completion (forward)")
    else:
        value_slots = fwd_answers or (allowed - subject)
        if not p_slots & value_slots:
            problems.append("no value slot in prompt (backward)")
        if not c_slots & bwd_answers:
            problems.append("no species slot in completion (backward)")
        if p_slots & subject:
            problems.append("species slot in prompt (backward)")

    ents = literal_entities(both, known_entities)
    if ents:
        problems.append("literal entity " + ", ".join(ents[:3]))

    sig = signature(prompt, completion)
    if sig in set(forbidden_sigs):
        problems.append("reproduces a v4/probe frame")
    if sig in set(banked_sigs):
        problems.append("duplicate signature")
    else:
        worst = 0.0
        for other in banked_texts:
            j = skeleton_jaccard(both, other)
            if j > worst:
                worst = j
        if worst >= near_dup:
            problems.append(f"near-duplicate (jaccard {worst:.2f})")

    err = render_check(prompt, completion, kind_spec.get("sample_fills") or [])
    if err:
        problems.append(err)
    return problems


def bank_row(t: dict, kind: str, family: str, provenance: dict[str, Any]) -> dict:
    """The accepted-template row shape (bank/templates.jsonl)."""
    prompt = normalise_slots(str(t.get("prompt") or ""))
    completion = normalise_slots(str(t.get("completion") or ""))
    direction = str(t.get("direction") or "")
    return {
        "template_id": template_id(kind, family, direction, prompt, completion),
        "kind": kind,
        "family": family,
        "direction": direction,
        "form": str(t.get("form") or "statement"),
        "structure": str(t.get("structure") or ""),
        "prompt": prompt,
        "completion": completion,
        "slots": slots_in(prompt + " " + completion),
        "signature": signature(prompt, completion),
        "provenance": dict(provenance),
    }
