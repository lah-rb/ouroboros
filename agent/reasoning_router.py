"""Per-turn reasoning-level routing (adaptive_thinking).

┌─ THE LEARNED ROUTER (rule 3) IS **EXPERIMENTAL** — demoted 2026-07-25 ──────┐
│ Do not enable it for production runs or treat its output as a quality win.  │
│ Rules 1-2 (cue-authored levels, high-steps list) are NOT demoted and stay   │
│ default-on: they RAISE thinking at known-hard steps and their evidence is   │
│ independent (fibonacci 5/6->6/6; judge deliberating 807 tok vs a 24-tok     │
│ rubber stamp).                                                              │
│                                                                            │
│ Three findings behind the demotion (full writeup + the tree-walk plan that  │
│ replaces this: dev/ADAPTIVE_THINKING_STATUS.md):                            │
│                                                                            │
│ 1. The shipped artifact has classes ['low','medium'] ONLY. The pilot DID    │
│    find highs (87/20/13) but JUDGE_STANDARD v1.0's pairwise gate            │
│    quarantined them pending panels that never ran, so training went binary. │
│    THE ROUTER CANNOT ESCALATE TO HIGH — no threshold or gate_levels change  │
│    can fix that, the class does not exist.                                  │
│ 2. It has been effectively INERT since ~2026-07-17. Replaying the fixed     │
│    artifact over 61k historical prompts: 25-70% activation through early    │
│    July, 0-3% after. The 2026-07-25 boss run measured 199 low / 1 medium    │
│    out of 200 live. (Confound: contract_swarm landed 07-17, so workload     │
│    mix moved too; not resolved.)                                            │
│ 3. The -42% decode/turn canary that justified shipping is WITHDRAWN. It was │
│    measured 07-15, right before the flattening, and "always routes low" is  │
│    observationally identical to a fixed low policy — i.e. gpt-oss being     │
│    FASTER at low, not smarter when adaptive. Nothing in the record          │
│    separates those hypotheses.                                              │
│                                                                            │
│ Blast radius differs by format: only chatml declares `gate_levels`, so      │
│ routed-low means "shallower" on harmony/gemma but "NO reasoning at all" on  │
│ step-3.7 — where a blind 3-judge panel priced it at 31.7/50 vs 24.7/50.     │
│                                                                            │
│ IF YOU ENABLE THIS: log the activation rate. An inert router and a          │
│ decisive one produce identical logs today, which is why 2 above went        │
│ unnoticed for nine days.                                                    │
└────────────────────────────────────────────────────────────────────────────┘

Chooses the gpt-oss reasoning effort (low/medium/high) per inference step, so
mechanical turns stop paying for long CoT. The server applies the level via
the validated reasoning head-swap (``config.model.reasoning_head_swap``):
sessions install at turn 0 and SPLICE mid-session; stateless completions get
the level head installed per request. The trained TF-IDF router (rung 3) is
session-domain only; explicit config and the high-steps list steer both paths.

Resolution order (first hit wins):

1. Explicit ``reasoning`` in the step's config (cue-authored, e.g. a charter
   step declaring ``reasoning: "high"``).
2. Step name listed in ``OURO_REASONING_HIGH_STEPS`` — the structural "high
   sprinkle" for important planning steps (comma-separated).
3. Step name listed in ``OURO_ROUTER_STEPS`` (default ``plan_interaction``) —
   the trained low/medium router: word+char TF-IDF union + logistic regression
   over the rendered turn prompt. Champion of the 2026-07 bake-off on
   task-held-out data (macro-F1 .622; route-low precision .976 at 38% coverage
   at the default threshold; see dev/archive/docs/ADAPTIVE_REASONING_DECISION_LAYER.md).
   ``P(medium) >= OURO_ROUTER_THR`` (default 0.4) -> medium, else low.
4. Otherwise None -> the server's default level (medium) applies.

Everything is INERT unless ``OURO_ADAPTIVE_REASONING=1``: no artifact load, no
behavior change (the dormant-flag pattern). A missing/broken artifact disables
rule 3 with a one-time warning rather than failing the mission.
"""

import logging
import os
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Running tally of router decisions for the activation-rate log line. Process
# -local and best-effort — it answers "is this router alive?", not accounting.
_decisions: dict[str, int] = {"low": 0, "medium": 0}

VALID_LEVELS = ("low", "medium", "high")
_REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ARTIFACT = "models/reasoning_router_v1.joblib"

# path -> artifact dict, or None after a failed load (negative cache: one warning).
_artifact_cache: dict[str, Optional[dict]] = {}


def _enabled() -> bool:
    return os.environ.get("OURO_ADAPTIVE_REASONING", "") == "1"


def _csv_env(name: str, default: str = "") -> set[str]:
    return {s.strip() for s in os.environ.get(name, default).split(",") if s.strip()}


def _threshold() -> float:
    raw = os.environ.get("OURO_ROUTER_THR", "0.4")
    try:
        return float(raw)
    except ValueError:
        logger.warning("OURO_ROUTER_THR=%r is not a float; using 0.4", raw)
        return 0.4


def _load_artifact(path_str: str) -> Optional[dict]:
    if path_str in _artifact_cache:
        return _artifact_cache[path_str]
    path = Path(path_str)
    if not path.is_absolute():
        path = _REPO_ROOT / path
    artifact: Optional[dict] = None
    try:
        import joblib

        loaded = joblib.load(path)
        if not all(k in loaded for k in ("vectorizer", "clf", "medium_idx")):
            raise ValueError(f"artifact missing keys: {sorted(loaded)}")
        artifact = loaded
        logger.info(
            "reasoning_router: loaded %s (%s)",
            path,
            loaded.get("meta", {}).get("built", "no meta"),
        )
    except Exception as exc:  # noqa: BLE001 — router must never sink a mission
        logger.warning(
            "reasoning_router: cannot load %s (%s); router disabled", path, exc
        )
    _artifact_cache[path_str] = artifact
    return artifact


def resolve_reasoning(
    step_name: str,
    step_config: Optional[dict[str, Any]],
    prompt: str,
    session: bool,
) -> Optional[str]:
    """Return the reasoning level for this turn, or None for the server default.

    ``session`` must reflect whether the call actually routes through a
    memoryful inference session — the head-swap only exists on that path.
    """
    # Global kill-switch: OURO_REASONING_OFF=1 restores pre-feature behavior
    # exactly (every rung, including cue-authored highs) — the baseline arm of
    # A/B runs, and the operational escape hatch.
    if os.environ.get("OURO_REASONING_OFF", "") == "1":
        return None

    # Explicit cue-authored ``reasoning`` is static step config — honored like
    # temperature, NOT gated behind the adaptive flag (the flag gates the
    # adaptive machinery below, not flow-author intent). Works on BOTH paths:
    # sessions swap/splice the head; stateless completions carry the field too.
    explicit = (step_config or {}).get("reasoning")
    if isinstance(explicit, str) and explicit.lower() in VALID_LEVELS:
        logger.info(
            "reasoning_router: step=%s explicit -> %s", step_name, explicit.lower()
        )
        return explicit.lower()

    if not _enabled():
        return None

    if step_name in _csv_env("OURO_REASONING_HIGH_STEPS"):
        logger.info("reasoning_router: step=%s high-steps list -> high", step_name)
        return "high"

    # The trained router below is session-domain (plan_interaction turn
    # prompts); stateless steps stop here.
    if not session:
        return None

    if step_name in _csv_env("OURO_ROUTER_STEPS", "plan_interaction"):
        artifact = _load_artifact(
            os.environ.get("OURO_REASONING_ROUTER", DEFAULT_ARTIFACT)
        )
        if artifact is None:
            return None
        p_medium = float(
            artifact["clf"].predict_proba(artifact["vectorizer"].transform([prompt]))[
                0
            ][artifact["medium_idx"]]
        )
        level = "medium" if p_medium >= _threshold() else "low"
        # ACTIVATION RATE is the one number that tells a working router from a
        # dead one. Its absence is why the 2026-07-17 flattening went unnoticed
        # for nine days: "decided low" and "cannot decide anything" log
        # identically per call, and only the RATE separates them. Emitted as a
        # running tally so a single grep of any run answers "was the router
        # actually routing?" — see dev/ADAPTIVE_THINKING_STATUS.md §9.
        _decisions[level] += 1
        total = _decisions["low"] + _decisions["medium"]
        logger.info(
            "reasoning_router: step=%s p_medium=%.3f -> %s "
            "[activation %d/%d = %.1f%%]",
            step_name,
            p_medium,
            level,
            _decisions["medium"],
            total,
            100.0 * _decisions["medium"] / total,
        )
        return level

    return None
