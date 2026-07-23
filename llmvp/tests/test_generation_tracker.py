"""GenerationTracker: pool-path finish semantics, batched quiet-finish, and
the per-stream report_completion path that keeps completion stats truthful
under concurrency (the 2026-07-21 blend: a 517-token stream logged as
generated=7361 @ 115.7 tok/s because interleaved start() resets let finish()
read another stream's counters)."""

from core.generation_tracker import GenerationTracker


def _run_one(tracker: GenerationTracker, request_id: str, tokens: int) -> None:
    tracker.start(request_id=request_id, prompt_tokens=100)
    for _ in range(tokens):
        tracker.record_token()
    tracker.finish()


def test_pool_finish_snapshot_and_trend():
    t = GenerationTracker()
    _run_one(t, "req-a", 50)
    snap = t.get_last_diagnostics()
    assert snap["request_id"] == "req-a"
    assert snap["tokens_generated"] == 50
    assert snap["prompt_tokens"] == 100
    # gen_time is sub-ms in-test → below the non-trivial trend guard.
    # The snapshot is always updated regardless.


def test_quiet_finish_updates_nothing_but_liveness():
    t = GenerationTracker()
    # A prior real completion to make pollution observable.
    t.report_completion(
        request_id="real", prompt_tokens=10, generated_tokens=100, eval_s=1.0, gen_s=2.0
    )
    before = dict(t.get_last_diagnostics())
    trend_before = t.get_trend()

    t.start(request_id="batched-1", prompt_tokens=5000)
    for _ in range(7):
        t.record_token()
    t.finish(quiet=True)

    assert t.get_last_diagnostics() == before  # snapshot untouched
    assert t.get_trend() == trend_before  # trend untouched
    assert t.get_status()["generation_active"] is False  # liveness closed


def test_report_completion_is_per_stream_true_under_interleaving():
    t = GenerationTracker()
    # Simulate batched interleaving: stream B's start() resets the shared
    # status while A is mid-flight; the shared counters are now a blend.
    t.start(request_id="A", prompt_tokens=4750)
    for _ in range(3):
        t.record_token()
    t.start(request_id="B", prompt_tokens=4938)  # reset mid-A
    for _ in range(7361):
        t.record_token()
    t.finish(quiet=True)  # batched wrappers are quiet

    # The engine reports A's TRUE numbers at retirement.
    t.report_completion(
        request_id="A",
        prompt_tokens=4750,
        generated_tokens=517,
        eval_s=3.0,
        gen_s=70.8,
    )
    snap = t.get_last_diagnostics()
    assert snap["request_id"] == "A"
    assert snap["tokens_generated"] == 517  # not the blended 7361
    assert snap["prompt_tokens"] == 4750
    assert abs(snap["tok_per_sec"] - 517 / 70.8) < 0.11


def test_report_completion_folds_trend_with_guard():
    t = GenerationTracker()
    # Non-trivial generation folds.
    t.report_completion(
        request_id="x", prompt_tokens=1000, generated_tokens=200, eval_s=1.0, gen_s=4.0
    )
    tr = t.get_trend()
    assert tr["trend_samples"] == 1
    # Trivial ones (tiny decode / few tokens) do not skew the trend.
    t.report_completion(
        request_id="y", prompt_tokens=10, generated_tokens=2, eval_s=0.0, gen_s=0.01
    )
    assert t.get_trend()["trend_samples"] == 1


def test_expected_eval_seconds_from_cold_seed():
    """The boot static-eval seed drives an UPPER eval estimate for the
    in-flight prompt — the field a timeout consumer needs (the 300s-vs-334.9s
    doom loop class)."""
    t = GenerationTracker()
    # No basis yet: no field.
    t.start("r0", prompt_tokens=14951)
    assert "expected_eval_seconds" not in t.get_status()
    t.finish(quiet=True)

    # Seed from a cold static eval: 1818 tokens in 39.46s ≈ 46 tok/s.
    t.seed_prefill_rate(1818, 39.46)
    t.start("r1", prompt_tokens=14951)
    st = t.get_status()
    expected = st["expected_eval_seconds"]
    assert abs(expected - 14951 / (1818 / 39.46)) < 1.0  # ~324s
    t.finish(quiet=True)

    # Degenerate seeds are ignored.
    t2 = GenerationTracker()
    t2.seed_prefill_rate(0, 10.0)
    t2.seed_prefill_rate(100, 0.05)
    t2.start("r2", prompt_tokens=5000)
    assert "expected_eval_seconds" not in t2.get_status()


def test_expected_eval_uses_conservative_minimum_rate():
    """Cache-assisted turns report optimistic effective prefill rates; the
    estimate must stay anchored to the slowest (cold) observation, and the
    slowest of multiple seeds wins (later boot evals can be cache-warmed)."""
    t = GenerationTracker()
    t.seed_prefill_rate(1800, 40.0)  # cold: 45 tok/s
    t.seed_prefill_rate(1800, 4.0)  # warmed second slot: 450 tok/s — ignored
    # A cache-assisted completion with a wildly optimistic effective rate.
    t.report_completion(
        request_id="warm",
        prompt_tokens=20000,
        generated_tokens=500,
        eval_s=20.0,  # effective 1000 tok/s
        gen_s=10.0,
    )
    t.start("r", prompt_tokens=9000)
    st = t.get_status()
    # 9000 / 45 = 200s (cold), NOT 9000 / 1000 = 9s.
    assert abs(st["expected_eval_seconds"] - 200.0) < 2.0
