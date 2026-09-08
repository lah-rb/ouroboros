"""The two arithmetic traps in run_status: cumulative tokens, hours vs seconds."""

from run_status import TOKENS_PER_EPOCH, rate_and_eta, series

LOG = (
    "{'loss': '1.4', 'epoch': '0.280', 'num_input_tokens_seen': 39321600, 'train_runtime': '10.0'}\n"
    "{'eval_replay_loss': '2.186', 'epoch': '0.280', 'num_input_tokens_seen': 39321600}\n"
    "{'loss': '1.3', 'epoch': '0.607', 'num_input_tokens_seen': 85196800, 'train_runtime': '23508'}\n"
    "{'eval_replay_loss': '2.174', 'epoch': '0.607', 'num_input_tokens_seen': 85196800}\n"
)


def test_rate_uses_the_segment_delta_not_the_cumulative_counter():
    st = rate_and_eta(LOG, 2.0)
    # 85,196,800 - 39,321,600 = 45,875,200 tokens in 23,508 s = 7.03M/h.
    # The cumulative counter would have said 13.0M/h.
    assert 6.9 < st["rate_M_per_h"] < 7.2
    assert st["segment_tokens"] == 45_875_200


def test_remaining_is_hours_and_eta_converts_once():
    st = rate_and_eta(LOG, 2.0)
    expected = (2.0 - 0.607) * TOKENS_PER_EPOCH / (st["rate_M_per_h"] * 1e6)
    assert abs(st["remaining_h"] - expected) < 0.01
    assert 27 < st["remaining_h"] < 29  # not 0.0, the hours/3600 bug


def test_series_reads_epoch_and_value_pairs():
    s = series(LOG, "replay")
    assert s == [(0.280, 2.186), (0.607, 2.174)]
