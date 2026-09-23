from dataclasses import asdict
import json
from pathlib import Path

import pytest

from junhyunbank.config import StrategyConfig
from junhyunbank.replay import (
    ReplayClock,
    ReplayInputError,
    ReplayMicroFlowStrategy,
    ReplayOptions,
    recording_sessions,
    replay_files,
    replay_records,
    select_recording_session,
    strategy_event,
)


def _config() -> StrategyConfig:
    return StrategyConfig(
        baseline_seconds=120,
        min_warmup_seconds=5,
        orderbook_depth=1,
        activity_window_seconds=2,
        aggression_window_seconds=2,
        momentum_window_seconds=2,
        long_momentum_window_seconds=3,
        expected_move_window_seconds=2,
        ignition_quality=0.0,
        pullback_quality=0.0,
    )


def _record(seq: int, kind: str, second: int, data: dict, market: str = "KRW-X") -> dict:
    base_wall = 1_800_000_000_000_000_000
    base_mono = 9_000_000_000_000
    return {
        "schema_version": 1,
        "seq": seq,
        "kind": kind,
        "market": market if kind != "meta" else "",
        "received_ns": base_wall + second * 1_000_000_000,
        "received_monotonic_ns": base_mono + second * 1_000_000_000,
        "exchange_timestamp_ms": 1_800_000_000_000 + second * 1000,
        "data": data,
    }


def _synthetic_session() -> list[dict]:
    config = _config()
    records = [
        _record(
            1,
            "meta",
            0,
            {
                "event": "session_start",
                "safe_krw_markets": ["KRW-X"],
                "strategy_config": asdict(config),
                "junhyunbank_version": "4.0.4",
                "orders_submitted": 0,
            },
        )
    ]
    seq = 2
    for second in range(1, 31):
        price = 100.0 + second
        records.append(
            _record(
                seq,
                "trade",
                second,
                {
                    "trade_price": price,
                    "trade_volume": 2.0,
                    "ask_bid": "BID",
                    "trade_timestamp": 1_800_000_000_000 + second * 1000,
                },
            )
        )
        seq += 1
        records.append(
            _record(
                seq,
                "orderbook",
                second,
                {
                    "timestamp": 1_800_000_000_000 + second * 1000,
                    "orderbook_units": [
                        {
                            "bid_price": price * 0.9995,
                            "ask_price": price * 1.0005,
                            "bid_size": 3.0 + second * 0.1,
                            "ask_size": 1.0,
                        }
                    ],
                },
            )
        )
        seq += 1
    records.append(
        _record(
            seq,
            "meta",
            31,
            {"event": "session_end", "orders_submitted": 0},
        )
    )
    return records


def test_replay_clock_clamps_small_retrograde_receive_order():
    clock = ReplayClock()
    clock.advance_record(_record(1, "meta", 10, {"event": "x"}))
    assert clock.monotonic() == 0.0

    later = _record(2, "meta", 12, {"event": "x"})
    clock.advance_record(later)
    assert clock.monotonic() == pytest.approx(2.0)

    retrograde = _record(3, "meta", 11, {"event": "x"})
    clock.advance_record(retrograde)
    assert clock.monotonic() == pytest.approx(2.0)
    assert clock.retrograde_events == 1


def test_replay_strategy_freshness_uses_logical_clock_not_real_time():
    clock = ReplayClock()
    strategy = ReplayMicroFlowStrategy(_config(), clock)
    trade_record = _record(
        1,
        "trade",
        1,
        {
            "trade_price": 100.0,
            "trade_volume": 1.0,
            "ask_bid": "BID",
            "trade_timestamp": 1_800_000_001_000,
        },
    )
    clock.advance_record(trade_record)
    _, trade = strategy_event(trade_record)
    strategy.on_trade(trade)

    book_record = _record(
        2,
        "orderbook",
        1,
        {
            "timestamp": 1_800_000_001_000,
            "orderbook_units": [
                {"bid_price": 99.0, "ask_price": 101.0, "bid_size": 1.0, "ask_size": 1.0}
            ],
        },
    )
    clock.advance_record(book_record)
    _, book = strategy_event(book_record)
    strategy.on_orderbook(book)
    assert strategy.trade_age("KRW-X") == pytest.approx(0.0)
    assert strategy.book_age("KRW-X") == pytest.approx(0.0)

    clock.advance_record(_record(3, "meta", 6, {"event": "tick"}))
    assert strategy.trade_age("KRW-X") == pytest.approx(5.0)
    assert strategy.book_age("KRW-X") == pytest.approx(5.0)


def test_same_recording_produces_same_decision_fingerprint():
    records = _synthetic_session()
    options = ReplayOptions(evaluate_every_seconds=1.0, bid_fee=0.0005, ask_fee=0.0005)
    first = replay_records(
        records,
        strategy_config=_config(),
        markets=["KRW-X"],
        options=options,
    )
    second = replay_records(
        records,
        strategy_config=_config(),
        markets=["KRW-X"],
        options=options,
    )

    assert first["summary"]["evaluations"] > 0
    assert first["summary"]["decision_fingerprint_sha256"] == second["summary"]["decision_fingerprint_sha256"]
    assert first["decisions"] == second["decisions"]
    assert first["input"]["complete_session"] is True
    assert first["input"]["data_integrity_ok"] is True
    assert first["replay"]["orders_submitted"] == 0


def test_strategy_event_reconstructs_market_and_exchange_timestamp():
    record = _record(
        1,
        "trade",
        1,
        {"trade_price": 100.0, "trade_volume": 1.0, "ask_bid": "BID"},
    )
    kind, event = strategy_event(record)
    assert kind == "trade"
    assert event["code"] == "KRW-X"
    assert event["trade_timestamp"] == record["exchange_timestamp_ms"]


def test_recording_session_selection_uses_latest_complete_filename_group(tmp_path: Path):
    older = "upbit-public-20260910T010101-aaaaaaaa"
    newer = "upbit-public-20260911T010101-bbbbbbbb"
    for name in (
        f"{older}-000001.jsonl",
        f"{older}-000002.jsonl.gz",
        f"{newer}-000001.jsonl",
        f"{newer}-000002.jsonl",
    ):
        (tmp_path / name).write_text("", encoding="utf-8")
    (tmp_path / f"{newer}-000003.jsonl.part").write_text("", encoding="utf-8")

    grouped = recording_sessions(tmp_path)
    assert list(grouped) == [older, newer]
    session, files = select_recording_session(tmp_path, session="latest")
    assert session == newer
    assert [path.name for path in files] == [
        f"{newer}-000001.jsonl",
        f"{newer}-000002.jsonl",
    ]


def test_replay_files_restores_recorded_config_and_universe(tmp_path: Path):
    path = tmp_path / "upbit-public-20260911T010101-aaaaaaaa-000001.jsonl"
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in _synthetic_session()),
        encoding="utf-8",
    )

    result = replay_files([path])

    assert result["source"]["recorded_junhyunbank_version"] == "4.0.4"
    assert result["source"]["files"] == [path.name]
    assert result["replay"]["strategy_config"]["min_warmup_seconds"] == 5
    assert result["summary"]["markets"] == 1
    assert result["summary"]["evaluations"] > 0
    assert result["input"]["complete_session"] is True


def test_replay_rejects_invalid_fee():
    with pytest.raises(ReplayInputError):
        ReplayOptions(bid_fee=-0.1).normalized()


def test_strategy_event_rejects_missing_exchange_timestamp():
    record = _record(
        1,
        "trade",
        1,
        {"trade_price": 100.0, "trade_volume": 1.0, "ask_bid": "BID"},
    )
    record["exchange_timestamp_ms"] = None

    with pytest.raises(ReplayInputError, match="exchange timestamp"):
        strategy_event(record)


def test_rejected_duplicate_trade_does_not_refresh_replay_freshness():
    clock = ReplayClock()
    strategy = ReplayMicroFlowStrategy(_config(), clock)
    first = _record(
        1,
        "trade",
        1,
        {
            "trade_price": 100.0,
            "trade_volume": 1.0,
            "ask_bid": "BID",
            "trade_timestamp": 1_800_000_001_000,
        },
    )
    clock.advance_record(first)
    _, event = strategy_event(first)
    assert strategy.on_trade(event) is True

    duplicate = dict(first, seq=2)
    duplicate["received_ns"] += 4_000_000_000
    duplicate["received_monotonic_ns"] += 4_000_000_000
    clock.advance_record(duplicate)
    _, event = strategy_event(duplicate)

    assert strategy.on_trade(event) is False
    assert strategy.trade_age("KRW-X") == pytest.approx(4.0)


def test_sequence_gap_is_reported_and_fails_integrity_gate():
    records = _synthetic_session()
    records.pop(5)

    result = replay_records(
        records,
        strategy_config=_config(),
        markets=["KRW-X"],
    )

    assert result["input"]["complete_session"] is True
    assert result["input"]["seq_gaps"] == 1
    assert result["input"]["data_integrity_ok"] is False


def test_metadata_only_session_fails_zero_market_event_and_evaluation_gates():
    result = replay_records(
        [
            _record(1, "meta", 0, {"event": "session_start"}),
            _record(2, "meta", 1, {"event": "session_end"}),
        ],
        strategy_config=_config(),
        markets=["KRW-X"],
    )

    assert result["input"]["complete_session"] is True
    assert result["input"]["data_integrity_ok"] is False
    assert result["input"]["integrity_reasons"] == [
        "zero_market_events",
        "zero_evaluations",
    ]
    assert result["input"]["integrity_counts"] == {
        "market_events": 0,
        "trade_events": 0,
        "orderbook_events": 0,
        "accepted_market_events": 0,
        "accepted_trade_events": 0,
        "accepted_orderbook_events": 0,
        "evaluations": 0,
        "control_evaluations": 0,
    }


def test_trade_only_session_fails_zero_production_evaluations_gate():
    result = replay_records(
        [
            _record(1, "meta", 0, {"event": "session_start"}),
            _record(
                2,
                "trade",
                1,
                {
                    "trade_price": 100.0,
                    "trade_volume": 1.0,
                    "ask_bid": "BID",
                    "trade_timestamp": 1_800_000_001_000,
                },
            ),
            _record(3, "meta", 2, {"event": "session_end"}),
        ],
        strategy_config=_config(),
        markets=["KRW-X"],
    )

    assert result["input"]["market_events"] == 1
    assert result["input"]["accepted_trade_events"] == 1
    assert result["input"]["evaluations"] == 0
    assert result["input"]["integrity_reasons"] == ["zero_evaluations"]
    assert result["input"]["data_integrity_ok"] is False


def test_event_after_session_end_is_not_replayed_and_fails_integrity():
    records = _synthetic_session()
    end_seq = records[-1]["seq"]
    records.append(
        _record(
            end_seq + 1,
            "trade",
            32,
            {
                "trade_price": 999.0,
                "trade_volume": 1.0,
                "ask_bid": "BID",
                "trade_timestamp": 1_800_000_032_000,
            },
        )
    )

    result = replay_records(
        records,
        strategy_config=_config(),
        markets=["KRW-X"],
    )

    assert result["input"]["records_after_session_end"] == 1
    assert result["input"]["complete_session"] is False
    assert result["input"]["data_integrity_ok"] is False


def test_unknown_market_does_not_expand_recorded_universe():
    records = [
        _record(1, "meta", 0, {"event": "session_start"}),
        _record(
            2,
            "trade",
            1,
            {
                "trade_price": 100.0,
                "trade_volume": 1.0,
                "ask_bid": "BID",
                "trade_timestamp": 1_800_000_001_000,
            },
            market="KRW-UNKNOWN",
        ),
        _record(3, "meta", 2, {"event": "session_end"}),
    ]

    result = replay_records(
        records,
        strategy_config=_config(),
        markets=["KRW-X"],
    )

    assert result["summary"]["markets"] == 1
    assert result["input"]["unknown_market_events"] == 1
    assert result["input"]["data_integrity_ok"] is False


def test_orderbook_outside_recorded_subscription_is_not_evaluated():
    book = {
        "timestamp": 1_800_000_001_000,
        "orderbook_units": [
            {
                "bid_price": 99.0,
                "ask_price": 101.0,
                "bid_size": 1.0,
                "ask_size": 1.0,
            }
        ],
    }
    records = [
        _record(1, "meta", 0, {"event": "session_start"}),
        _record(
            2,
            "meta",
            0,
            {"event": "orderbook_subscription", "markets": ["KRW-X"]},
        ),
        _record(3, "orderbook", 1, book),
        _record(
            4,
            "meta",
            2,
            {"event": "orderbook_subscription", "markets": []},
        ),
        _record(5, "orderbook", 3, dict(book, timestamp=1_800_000_003_000)),
        _record(6, "meta", 4, {"event": "session_end"}),
    ]

    result = replay_records(
        records,
        strategy_config=_config(),
        markets=["KRW-X"],
    )

    assert result["summary"]["evaluations"] == 1
    assert result["input"]["subscription_violations"] == 1
    assert result["input"]["data_integrity_ok"] is False


def _book(second: int, price: float = 100.0) -> dict:
    return {
        "timestamp": 1_800_000_000_000 + second * 1000,
        "orderbook_units": [
            {
                "bid_price": price - 1.0,
                "ask_price": price + 1.0,
                "bid_size": 1.0,
                "ask_size": 1.0,
            }
        ],
    }


def _role_aware_session(*, control_price: float = 200.0) -> list[dict]:
    return [
        _record(1, "meta", 0, {"event": "session_start"}),
        _record(
            2,
            "meta",
            0,
            {
                "event": "orderbook_subscription",
                "markets": ["KRW-X", "KRW-Y"],
                "hot_markets": ["KRW-X"],
                "control_markets": ["KRW-Y"],
            },
        ),
        _record(3, "orderbook", 1, _book(1, 100.0), market="KRW-X"),
        _record(4, "orderbook", 1, _book(1, control_price), market="KRW-Y"),
        _record(5, "meta", 2, {"event": "session_end"}),
    ]


def test_replay_keeps_control_books_out_of_production_summary_and_fingerprint():
    first = replay_records(
        _role_aware_session(control_price=200.0),
        strategy_config=_config(),
        markets=["KRW-X", "KRW-Y"],
    )
    changed_control = replay_records(
        _role_aware_session(control_price=900.0),
        strategy_config=_config(),
        markets=["KRW-X", "KRW-Y"],
    )

    assert [row["market"] for row in first["decisions"]] == ["KRW-X"]
    assert [row["market"] for row in first["control_decisions"]] == ["KRW-Y"]
    assert first["summary"]["evaluations"] == 1
    assert first["control_diagnostics"]["evaluations"] == 1
    assert (
        first["summary"]["decision_fingerprint_sha256"]
        == changed_control["summary"]["decision_fingerprint_sha256"]
    )
    assert (
        first["control_diagnostics"]["decision_fingerprint_sha256"]
        != changed_control["control_diagnostics"]["decision_fingerprint_sha256"]
    )
    assert first["input"]["role_aware_subscription_metadata"] == 1
    assert first["input"]["data_integrity_ok"] is True


def test_role_only_subscription_change_reclassifies_and_resets_books():
    records = _role_aware_session()
    records.pop()
    records.extend(
        [
            _record(
                5,
                "meta",
                2,
                {
                    "event": "orderbook_subscription",
                    "markets": ["KRW-X", "KRW-Y"],
                    "hot_markets": ["KRW-Y"],
                    "control_markets": ["KRW-X"],
                },
            ),
            _record(6, "orderbook", 3, _book(3, 101.0), market="KRW-X"),
            _record(7, "orderbook", 3, _book(3, 201.0), market="KRW-Y"),
            _record(8, "meta", 4, {"event": "session_end"}),
        ]
    )

    result = replay_records(
        records,
        strategy_config=_config(),
        markets=["KRW-X", "KRW-Y"],
    )

    assert [row["market"] for row in result["decisions"]] == ["KRW-X", "KRW-Y"]
    assert [row["market"] for row in result["control_decisions"]] == [
        "KRW-Y",
        "KRW-X",
    ]
    assert result["input"]["role_aware_subscription_metadata"] == 2
    assert result["input"]["data_integrity_ok"] is True


def test_ambiguous_subscription_roles_fail_closed():
    records = _role_aware_session()
    records[1]["data"]["hot_markets"] = ["KRW-X", "KRW-Y"]
    records[1]["data"]["control_markets"] = ["KRW-Y"]

    result = replay_records(
        records,
        strategy_config=_config(),
        markets=["KRW-X", "KRW-Y"],
    )

    assert result["decisions"] == []
    assert result["control_decisions"] == []
    assert result["input"]["subscription_role_violations"] == 1
    assert result["input"]["data_integrity_ok"] is False


def test_legacy_subscription_metadata_remains_all_production():
    records = _role_aware_session()
    records[1]["data"].pop("hot_markets")
    records[1]["data"].pop("control_markets")

    result = replay_records(
        records,
        strategy_config=_config(),
        markets=["KRW-X", "KRW-Y"],
    )

    assert [row["market"] for row in result["decisions"]] == ["KRW-X", "KRW-Y"]
    assert result["control_decisions"] == []
    assert result["input"]["legacy_subscription_metadata"] == 1
    assert result["input"]["data_integrity_ok"] is True


def test_declared_controls_require_role_partition_metadata():
    records = _role_aware_session()
    records[0]["data"]["orderbook_controls"] = 1
    records[1]["data"].pop("hot_markets")
    records[1]["data"].pop("control_markets")

    result = replay_records(
        records,
        strategy_config=_config(),
        markets=["KRW-X", "KRW-Y"],
    )

    assert result["decisions"] == []
    assert result["control_decisions"] == []
    assert result["input"]["declared_orderbook_controls"] == 1
    assert result["input"]["subscription_role_violations"] == 1
    assert result["input"]["data_integrity_ok"] is False


def test_declared_controls_reject_orderbook_before_any_role_map():
    records = _role_aware_session()
    records[0]["data"]["orderbook_controls"] = 1
    records[1]["data"] = {"event": "status"}

    result = replay_records(
        records,
        strategy_config=_config(),
        markets=["KRW-X", "KRW-Y"],
    )

    assert result["decisions"] == []
    assert result["control_decisions"] == []
    assert result["input"]["role_metadata_required"] is True
    assert result["input"]["subscription_role_violations"] == 2
    assert result["input"]["data_integrity_ok"] is False


def test_recorder_drop_snapshot_fails_integrity_gate():
    records = _synthetic_session()
    records[-1]["data"]["recorder_stats"] = {
        "dropped": 2,
        "last_error": "queue full",
    }

    result = replay_records(
        records,
        strategy_config=_config(),
        markets=["KRW-X"],
    )

    assert result["input"]["recorder_dropped"] == 2
    assert result["input"]["recorder_error_messages"] == 1
    assert result["input"]["data_integrity_ok"] is False


def test_missing_receive_clocks_fail_integrity_and_are_not_replayed():
    records = _synthetic_session()
    records[1].pop("received_ns")
    records[1].pop("received_monotonic_ns")

    result = replay_records(
        records,
        strategy_config=_config(),
        markets=["KRW-X"],
    )

    assert result["input"]["missing_received_ns_records"] == 1
    assert result["input"]["missing_received_monotonic_ns_records"] == 1
    assert result["input"]["unreplayable_clock_records"] == 1
    assert result["input"]["data_integrity_ok"] is False


def test_strategy_rejected_event_fails_integrity_gate():
    records = _synthetic_session()
    records[1]["data"]["trade_price"] = -1.0

    result = replay_records(
        records,
        strategy_config=_config(),
        markets=["KRW-X"],
    )

    assert result["input"]["rejected_trade_events"] == 1
    assert result["input"]["data_integrity_ok"] is False


def test_exchange_timestamp_stale_at_record_receive_fails_integrity():
    records = _synthetic_session()
    stale = records[1]
    stale_timestamp = int(stale["received_ns"] / 1_000_000) - 10_000
    stale["exchange_timestamp_ms"] = stale_timestamp
    stale["data"]["trade_timestamp"] = stale_timestamp

    result = replay_records(
        records,
        strategy_config=_config(),
        markets=["KRW-X"],
    )

    assert result["input"]["stale_exchange_timestamp_events"] == 1
    assert result["input"]["data_integrity_ok"] is False
