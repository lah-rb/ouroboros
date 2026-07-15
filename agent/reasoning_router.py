"""Per-turn reasoning-level routing (adaptive_thinking, Phase F wiring).

Chooses the gpt-oss reasoning effort (low/medium/high) for SESSION-path
inference steps, so mechanical turns stop paying for long CoT. The server
applies the level via the validated reasoning head-swap
(``config.model.reasoning_head_swap``); the field only exists on the session
request, so stateless steps always resolve to None here.

Resolution order (first hit wins):

1. Explicit ``reasoning`` in the step's config (cue-authored, e.g. a charter
   step declaring ``reasoning: "high"``).
2. Step name listed in ``OURO_REASONING_HIGH_STEPS`` — the structural "high
   sprinkle" for important planning steps (comma-separated).
3. Step name listed in ``OURO_ROUTER_STEPS`` (default ``plan_interaction``) —
   the trained low/medium router: word+char TF-IDF union + logistic regression
   over the rendered turn prompt. Champion of the 2026-07 bake-off on
   task-held-out data (macro-F1 .622; route-low precision .976 at 38% coverage
   at the default threshold; see dev/ADAPTIVE_REASONING_DECISION_LAYER.md).
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
        logger.warning("reasoning_router: cannot load %s (%s); router disabled", path, exc)
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
    if not _enabled():
        return None
    if not session:
        return None

    explicit = (step_config or {}).get("reasoning")
    if isinstance(explicit, str) and explicit.lower() in VALID_LEVELS:
        return explicit.lower()

    if step_name in _csv_env("OURO_REASONING_HIGH_STEPS"):
        return "high"

    if step_name in _csv_env("OURO_ROUTER_STEPS", "plan_interaction"):
        artifact = _load_artifact(os.environ.get("OURO_REASONING_ROUTER", DEFAULT_ARTIFACT))
        if artifact is None:
            return None
        p_medium = float(
            artifact["clf"].predict_proba(
                artifact["vectorizer"].transform([prompt])
            )[0][artifact["medium_idx"]]
        )
        level = "medium" if p_medium >= _threshold() else "low"
        logger.info(
            "reasoning_router: step=%s p_medium=%.3f -> %s", step_name, p_medium, level
        )
        return level

    return None
