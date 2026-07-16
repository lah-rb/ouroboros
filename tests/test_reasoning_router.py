"""Unit tests for agent.reasoning_router (adaptive_thinking Phase F wiring).

Covers the resolution ladder (explicit config > high-steps > router > None),
the dormant-flag gate, session-path-only behavior, threshold routing against a
real (tiny) artifact, and fail-open on a missing artifact.
"""
import joblib
import pytest
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import LogisticRegression

import agent.reasoning_router as rr


@pytest.fixture()
def tiny_artifact(tmp_path):
    """A real vectorizer+clf trained on separable toy data.

    'alpha'-heavy texts are low, 'omega'-heavy texts are medium — so
    P(medium | 'omega omega omega') is high and P(medium | 'alpha alpha') low.
    """
    texts = ["alpha beta run list", "alpha gamma cat file", "alpha beta print",
             "omega think strategy plan", "omega omega tradeoff design",
             "omega analyze compare plan"]
    labels = ["low", "low", "low", "medium", "medium", "medium"]
    vec = TfidfVectorizer()
    X = vec.fit_transform(texts)
    clf = LogisticRegression(class_weight="balanced").fit(X, labels)
    path = tmp_path / "router.joblib"
    joblib.dump(
        {"vectorizer": vec, "clf": clf,
         "medium_idx": list(clf.classes_).index("medium"),
         "meta": {"built": "test"}},
        path,
    )
    return str(path)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for var in ("OURO_ADAPTIVE_REASONING", "OURO_REASONING_ROUTER",
                "OURO_ROUTER_THR", "OURO_ROUTER_STEPS", "OURO_REASONING_HIGH_STEPS"):
        monkeypatch.delenv(var, raising=False)
    rr._artifact_cache.clear()
    yield
    rr._artifact_cache.clear()


def _on(monkeypatch, artifact=None, **env):
    monkeypatch.setenv("OURO_ADAPTIVE_REASONING", "1")
    if artifact:
        monkeypatch.setenv("OURO_REASONING_ROUTER", artifact)
    for k, v in env.items():
        monkeypatch.setenv(k, v)


def test_dormant_without_flag(tiny_artifact, monkeypatch):
    # the ADAPTIVE machinery (router + high-steps list) is flag-gated...
    monkeypatch.setenv("OURO_REASONING_ROUTER", tiny_artifact)
    monkeypatch.setenv("OURO_REASONING_HIGH_STEPS", "judge_step")
    assert rr.resolve_reasoning("plan_interaction", {}, "omega omega", True) is None
    assert rr.resolve_reasoning("judge_step", {}, "x", True) is None


def test_explicit_config_works_without_flag(monkeypatch):
    # ...but explicit cue-authored reasoning is static config, honored always —
    # on sessions AND stateless completions (server carries the field on both)
    assert rr.resolve_reasoning("verify_completion", {"reasoning": "high"}, "x", True) == "high"
    assert rr.resolve_reasoning("verify_completion", {"reasoning": "high"}, "x", False) == "high"


def test_stateless_gating(tiny_artifact, monkeypatch):
    _on(monkeypatch, tiny_artifact, OURO_REASONING_HIGH_STEPS="design_gate")
    # explicit + high-steps rungs work stateless (completion head-swap)...
    assert rr.resolve_reasoning("design_gate", {}, "anything", False) == "high"
    # ...but the TRAINED router is session-domain only
    assert rr.resolve_reasoning("plan_interaction", {}, "omega", False) is None


def test_explicit_step_config_wins(tiny_artifact, monkeypatch):
    _on(monkeypatch, tiny_artifact, OURO_REASONING_HIGH_STEPS="plan_interaction")
    # explicit config outranks even the high-steps list
    assert rr.resolve_reasoning("plan_interaction", {"reasoning": "LOW"}, "x", True) == "low"
    assert rr.resolve_reasoning("plan_interaction", {"reasoning": "bogus"}, "omega omega omega", True) == "high"


def test_high_steps_sprinkle(tiny_artifact, monkeypatch):
    _on(monkeypatch, tiny_artifact, OURO_REASONING_HIGH_STEPS="write_charter,design_gate")
    assert rr.resolve_reasoning("write_charter", {}, "trivial text", True) == "high"
    assert rr.resolve_reasoning("design_gate", None, "trivial text", True) == "high"


def test_router_threshold_routing(tiny_artifact, monkeypatch):
    _on(monkeypatch, tiny_artifact)
    assert rr.resolve_reasoning("plan_interaction", {}, "alpha alpha beta list", True) == "low"
    assert rr.resolve_reasoning("plan_interaction", {}, "omega omega tradeoff strategy", True) == "medium"


def test_threshold_env_moves_the_cut(tiny_artifact, monkeypatch):
    _on(monkeypatch, tiny_artifact, OURO_ROUTER_THR="0.999")
    # impossible bar -> everything routes low
    assert rr.resolve_reasoning("plan_interaction", {}, "omega omega tradeoff", True) == "low"
    monkeypatch.setenv("OURO_ROUTER_THR", "0.0")
    assert rr.resolve_reasoning("plan_interaction", {}, "alpha alpha", True) == "medium"


def test_unrouted_step_untouched(tiny_artifact, monkeypatch):
    _on(monkeypatch, tiny_artifact)
    assert rr.resolve_reasoning("execute_interaction", {}, "omega omega", True) is None


def test_missing_artifact_fails_open(monkeypatch):
    _on(monkeypatch, "/nonexistent/router.joblib")
    assert rr.resolve_reasoning("plan_interaction", {}, "omega omega", True) is None
    # negative-cached: second call also None, no exception
    assert rr.resolve_reasoning("plan_interaction", {}, "alpha", True) is None


def test_bad_threshold_falls_back(tiny_artifact, monkeypatch):
    _on(monkeypatch, tiny_artifact, OURO_ROUTER_THR="not-a-float")
    assert rr.resolve_reasoning("plan_interaction", {}, "omega omega tradeoff strategy", True) == "medium"
