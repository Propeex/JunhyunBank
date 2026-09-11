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
