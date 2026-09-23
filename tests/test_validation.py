import pytest

from junhyunbank.validation import (
    absolute_mid_move,
    chronological_split,
    count_role_buy_samples,
    net_round_trip_return,
    purged_chronological_split,
    rotating_control_markets,
    sampling_role_entries,
    summarize_samples,
    validation_integrity_report,
)


def test_net_round_trip_return_pays_spread_and_both_fees():
    result = net_round_trip_return(
        101.0, 102.0, bid_fee=0.001, ask_fee=0.001
    )
    expected = (102.0 * 0.999) / (101.0 * 1.001) - 1.0
    assert result == pytest.approx(expected)


def test_invalid_prices_never_become_edge():
    assert (
        net_round_trip_return(0, 100, bid_fee=0.001, ask_fee=0.001)
        is None
    )
    assert (
        net_round_trip_return(100, -1, bid_fee=0.001, ask_fee=0.001)
        is None
    )
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


def test_purged_split_removes_training_labels_that_overlap_holdout():
    rows = [
        {
            "created_epoch_ms": index * 10_000,
            "market": f"KRW-{index}",
        }
        for index in range(10)
    ]
    train, holdout = purged_chronological_split(
        rows, horizon_seconds=30, fraction=0.6
    )
    assert [row["created_epoch_ms"] for row in train] == [
        0,
        10_000,
        20_000,
    ]
    assert holdout[0]["created_epoch_ms"] == 60_000


def test_summary_separates_buy_and_holdout_without_optimizing_thresholds():
    samples = []
    for index in range(10):
        signal = "BUY" if index in {1, 4, 8, 9} else "HOLD"
        net = 0.01 if index in {1, 8, 9} else -0.01
        samples.append(
            {
                "created_epoch_ms": index * 60_000,
                "market": f"KRW-{index}",
                "sample_role": "candidate",
                "signal": signal,
                "expected_move_pct": 0.02,
                "labels": {
                    "30": {
                        "status": "labeled",
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
    assert horizon["purged_from_train_count"] == 0


def test_summary_reports_missed_labels_instead_of_treating_them_as_returns():
    samples = [
        {
            "created_epoch_ms": 1,
            "market": "KRW-A",
            "sample_role": "candidate",
            "signal": "BUY",
            "expected_move_pct": 0.02,
            "labels": {
                "30": {
                    "status": "labeled",
                    "net_return": 0.01,
                    "absolute_mid_move": 0.015,
                }
            },
        },
        {
            "created_epoch_ms": 2,
            "market": "KRW-B",
            "sample_role": "candidate",
            "signal": "BUY",
            "expected_move_pct": 0.02,
            "labels": {
                "30": {
                    "status": "missed",
                    "reason": "no fresh quote",
                }
            },
        },
    ]

    row = summarize_samples(samples, [30])["horizons"]["30"]
    assert row["labeled_sample_count"] == 1
    assert row["missed_label_count"] == 1
    assert row["label_completion_rate"] == pytest.approx(0.5)
    assert row["buy_labeled_sample_count"] == 1
    assert row["buy_missed_label_count"] == 1
    assert row["buy"]["count"] == 1


def test_hold_compact_decision_can_use_feature_expected_move_for_calibration():
    samples = [
        {
            "created_epoch_ms": 1,
            "market": "KRW-X",
            "sample_role": "candidate",
            "signal": "HOLD",
            "expected_move_pct": 0.0,
            "features": {"expected_move": 0.02},
            "labels": {
                "30": {
                    "status": "labeled",
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


def test_rotating_controls_are_deterministic_exclude_candidates_and_wrap():
    markets = ["KRW-D", "KRW-A", "KRW-C", "KRW-B"]
    first, cursor = rotating_control_markets(
        markets, excluded=["KRW-B"], count=2, cursor=0
    )
    second, next_cursor = rotating_control_markets(
        markets, excluded=["KRW-B"], count=2, cursor=cursor
    )

    assert first == ["KRW-A", "KRW-C"]
    assert second == ["KRW-D", "KRW-A"]
    assert next_cursor == 1
    assert "KRW-B" not in first + second


def test_sampling_role_entry_detects_control_to_candidate_without_union_change():
    changed = sampling_role_entries(
        previous_candidates=["KRW-A"],
        previous_controls=["KRW-B"],
        next_candidates=["KRW-B"],
        next_controls=["KRW-A"],
    )

    assert changed == ["KRW-A", "KRW-B"]
    assert sampling_role_entries(
        previous_candidates=["KRW-A"],
        previous_controls=["KRW-B"],
        next_candidates=["KRW-A"],
        next_controls=["KRW-B"],
    ) == []

    assert sampling_role_entries(
        next_candidates=["KRW-LABEL-RETAINED"]
    ) == ["KRW-LABEL-RETAINED"]


def test_summary_reports_candidate_and_control_forward_results_separately():
    samples = []
    for index, (role, net) in enumerate(
        [("candidate", 0.01), ("candidate", 0.02), ("control", -0.01)]
    ):
        samples.append(
            {
                "created_epoch_ms": index * 60_000,
                "market": f"KRW-{index}",
                "sample_role": role,
                "signal": "HOLD",
                "labels": {
                    "30": {
                        "status": "labeled",
                        "net_return": net,
                        "absolute_mid_move": abs(net),
                    }
                },
            }
        )

    report = summarize_samples(samples, [30])
    horizon = report["horizons"]["30"]

    assert report["candidate_sample_count"] == 2
    assert report["control_sample_count"] == 1
    assert horizon["all"]["mean_net_return"] == pytest.approx(0.015)
    assert horizon["all_roles"]["mean_net_return"] == pytest.approx(
        0.02 / 3
    )
    assert horizon["candidate"]["mean_net_return"] == pytest.approx(0.015)
    assert horizon["control"]["mean_net_return"] == pytest.approx(-0.01)


def test_headline_buy_statistics_exclude_control_buy_signals():
    samples = [
        {
            "created_epoch_ms": 1,
            "market": "KRW-CANDIDATE",
            "sample_role": "candidate",
            "signal": "BUY",
            "labels": {
                "30": {
                    "status": "labeled",
                    "net_return": 0.02,
                    "absolute_mid_move": 0.02,
                }
            },
        },
        {
            "created_epoch_ms": 2,
            "market": "KRW-CONTROL",
            "sample_role": "control",
            "signal": "BUY",
            "labels": {
                "30": {
                    "status": "labeled",
                    "net_return": -0.03,
                    "absolute_mid_move": 0.03,
                }
            },
        },
        {
            "created_epoch_ms": 3,
            "market": "KRW-UNDECLARED",
            "signal": "BUY",
            "labels": {
                "30": {
                    "status": "labeled",
                    "net_return": 0.50,
                    "absolute_mid_move": 0.50,
                }
            },
        },
    ]

    report = summarize_samples(samples, [30])
    horizon = report["horizons"]["30"]

    assert report["buy_sample_count"] == 1
    assert report["control_buy_sample_count"] == 1
    assert horizon["buy"]["count"] == 1
    assert horizon["buy"]["mean_net_return"] == pytest.approx(0.02)
    assert horizon["candidate_buy"] == horizon["buy"]
    assert horizon["control_buy"]["count"] == 1
    assert horizon["control_buy"]["mean_net_return"] == pytest.approx(-0.03)


def test_role_buy_counter_requires_an_explicit_matching_role():
    samples = [
        {"sample_role": "candidate", "signal": "BUY"},
        {"sample_role": "control", "signal": "BUY"},
        {"signal": "BUY"},
        {"sample_role": "candidate", "signal": "HOLD"},
    ]

    assert count_role_buy_samples(samples, role="candidate") == 1
    assert count_role_buy_samples(samples, role="control") == 1
    with pytest.raises(ValueError):
        count_role_buy_samples(samples, role="unknown")


def test_train_and_holdout_headline_buys_remain_candidate_only():
    samples = []
    roles = [
        "candidate",
        "candidate",
        "control",
        "control",
        "candidate",
        "candidate",
        "control",
        "control",
    ]
    for index, role in enumerate(roles):
        samples.append(
            {
                "created_epoch_ms": index * 60_000,
                "market": f"KRW-{index}",
                "sample_role": role,
                "signal": "BUY",
                "labels": {
                    "30": {
                        "status": "labeled",
                        "net_return": 0.01,
                        "absolute_mid_move": 0.01,
                    }
                },
            }
        )

    horizon = summarize_samples(
        samples, [30], split_fraction=0.5
    )["horizons"]["30"]

    assert horizon["buy"]["count"] == 4
    assert horizon["train_buy"]["count"] == 2
    assert horizon["holdout_buy"]["count"] == 2
    assert horizon["train_control_buy"]["count"] == 2
    assert horizon["holdout_control_buy"]["count"] == 2


def test_validation_integrity_gate_accepts_only_complete_clean_labeled_data():
    complete = [
        {
            "market": "KRW-A",
            "sample_role": "candidate",
            "labels": {
                "30": {
                    "status": "labeled",
                    "net_return": 0.01,
                    "absolute_mid_move": 0.02,
                }
            },
        }
    ]

    ok = validation_integrity_report(
        complete,
        [30],
        completed_cleanly=True,
        websocket_errors={},
    )
    assert ok["data_integrity_ok"] is True
    assert ok["reasons"] == []

    failed = validation_integrity_report(
        [
            {
                "market": "KRW-A",
                "sample_role": "candidate",
                "labels": {"30": {"status": "missed"}},
            }
        ],
        [30],
        completed_cleanly=False,
        websocket_errors={"disconnect": 1},
        rejected_trade_events=2,
        rejected_orderbook_events=3,
        invalid_orderbook_quotes=4,
        dropped_new_samples_due_book_cap=5,
    )
    assert failed["data_integrity_ok"] is False
    assert set(failed["reasons"]) == {
        "run_incomplete",
        "websocket_errors",
        "rejected_trade_events",
        "rejected_orderbook_events",
        "invalid_orderbook_quotes",
        "missed_labels",
        "book_capacity_drops",
    }
    assert failed["counts"]["incomplete_labels"] == 1
    assert failed["counts"]["candidate_samples"] == 1

    empty = validation_integrity_report(
        [], [30], completed_cleanly=True
    )
    assert empty["data_integrity_ok"] is False
    assert empty["reasons"] == ["zero_candidate_samples"]


def test_integrity_gate_rejects_control_only_and_pending_candidate_data():
    labeled_control = {
        "market": "KRW-CONTROL",
        "sample_role": "control",
        "labels": {
            "30": {
                "status": "labeled",
                "net_return": 0.01,
                "absolute_mid_move": 0.02,
            }
        },
    }
    control_only = validation_integrity_report(
        [labeled_control], [30], completed_cleanly=True
    )
    assert control_only["reasons"] == ["zero_candidate_samples"]
    assert control_only["counts"]["candidate_samples"] == 0
    assert control_only["counts"]["control_samples"] == 1

    pending = validation_integrity_report(
        [
            {
                "market": "KRW-CANDIDATE",
                "sample_role": "candidate",
                "labels": {},
            }
        ],
        [30],
        completed_cleanly=True,
    )
    assert pending["data_integrity_ok"] is False
    assert pending["reasons"] == ["pending_labels"]
    assert pending["counts"]["pending_labels"] == 1


def test_integrity_gate_requires_control_samples_only_when_configured():
    candidate = {
        "market": "KRW-CANDIDATE",
        "sample_role": "candidate",
        "labels": {
            "30": {
                "status": "labeled",
                "net_return": 0.01,
                "absolute_mid_move": 0.02,
            }
        },
    }

    optional = validation_integrity_report(
        [candidate], [30], completed_cleanly=True
    )
    required = validation_integrity_report(
        [candidate],
        [30],
        completed_cleanly=True,
        controls_required=True,
    )

    assert optional["data_integrity_ok"] is True
    assert required["data_integrity_ok"] is False
    assert required["reasons"] == ["zero_control_samples"]
    assert required["counts"]["control_samples"] == 0
