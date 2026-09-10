import pytest

from junhyunbank.validation import (
    absolute_mid_move,
    chronological_split,
    net_round_trip_return,
    summarize_samples,
)


def test_net_round_trip_return_pays_spread_and_both_fees():
    result = net_round_trip_return(101.0, 102.0, bid_fee=0.001, ask_fee=0.001)
    expected = (102.0 * 0.999) / (101.0 * 1.001) - 1.0
    assert result == pytest.approx(expected)


def test_invalid_prices_never_become_edge():
    assert net_round_trip_return(0, 100, bid_fee=0.001, ask_fee=0.001) is None
    assert net_round_trip_return(100, -1, bid_fee=0.001, ask_fee=0.001) is None
    assert absolute_mid_move(0, 100, 100, 101) is None


def test_absolute_mid_move_is_direction_agnostic():
    up = absolute_mid_move(99, 101, 109, 111)
    down = absolute_mid_move(99, 101, 89, 91)
    assert up == pytest.approx(0.10)
    assert down == pytest.approx(0.10)


def test_chronological_split_never_shuffles_future_into_earlier_set():
    rows = [
        {"created_epoch_ms": 30, "market": "KRW-C"},
        {"created_epoch_ms": 10, "market": "KRW-A"},
        {"created_epoch_ms": 40, "market": "KRW-D"},
        {"created_epoch_ms": 20, "market": "KRW-B"},
    ]
    train, holdout = chronological_split(rows, 0.5)
    assert [row["created_epoch_ms"] for row in train] == [10, 20]
    assert [row["created_epoch_ms"] for row in holdout] == [30, 40]


def test_summary_separates_buy_and_holdout_without_optimizing_thresholds():
    samples = []
    for index in range(10):
        signal = "BUY" if index in {1, 4, 8, 9} else "HOLD"
        net = 0.01 if index in {1, 8, 9} else -0.01
        samples.append(
            {
                "created_epoch_ms": index * 1000,
                "market": f"KRW-{index}",
                "signal": signal,
                "expected_move_pct": 0.02,
                "labels": {
                    "30": {
                        "net_return": net,
                        "absolute_mid_move": 0.015,
                    }
                },
            }
        )

    report = summarize_samples(samples, [30], split_fraction=0.6)

    assert report["sample_count"] == 10
    assert report["buy_sample_count"] == 4
    horizon = report["horizons"]["30"]
    assert horizon["buy"]["count"] == 4
    assert horizon["buy"]["positive_net_rate"] == pytest.approx(0.75)
    assert horizon["train_buy"]["count"] == 2
    assert horizon["holdout_buy"]["count"] == 2
    assert horizon["holdout_buy"]["positive_net_rate"] == pytest.approx(1.0)
    assert horizon["all"]["expected_move_coverage_rate"] == pytest.approx(1.0)
    assert horizon["all"]["median_abs_to_expected_ratio"] == pytest.approx(0.75)


def test_hold_compact_decision_can_use_feature_expected_move_for_calibration():
    samples = [
        {
            "created_epoch_ms": 1,
            "market": "KRW-X",
            "signal": "HOLD",
            "expected_move_pct": 0.0,
            "features": {"expected_move": 0.02},
            "labels": {
                "30": {
                    "net_return": -0.001,
                    "absolute_mid_move": 0.01,
                }
            },
        }
    ]

    report = summarize_samples(samples, [30])
    row = report["horizons"]["30"]["all"]
    assert row["mean_expected_move"] == pytest.approx(0.02)
    assert row["median_abs_to_expected_ratio"] == pytest.approx(0.5)
