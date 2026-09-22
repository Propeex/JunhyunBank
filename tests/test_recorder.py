from __future__ import annotations

import gzip
import json
from pathlib import Path
import queue

from junhyunbank.recorder import (
    MarketDataRecorder,
    RecorderConfig,
    iter_records,
    recording_integrity_ok,
    recording_integrity_reasons,
    recording_files,
    subscription_metadata_changed,
)


def _config(tmp_path: Path, **overrides) -> RecorderConfig:
    values = dict(
        directory=tmp_path,
        prefix="test",
        queue_max=100,
        rotate_bytes=220,
        rotate_seconds=3600,
        flush_seconds=0.01,
        fsync_seconds=0.01,
        compress=True,
        max_total_bytes=10_000_000,
        max_files=100,
    )
    values.update(overrides)
    return RecorderConfig(**values)


def test_subscription_metadata_records_role_only_transition():
    assert subscription_metadata_changed(
        current_markets=["KRW-A", "KRW-B"],
        current_hot={"KRW-A"},
        current_controls={"KRW-B"},
        next_markets=["KRW-A", "KRW-B"],
        next_hot={"KRW-B"},
        next_controls={"KRW-A"},
    )
    assert not subscription_metadata_changed(
        current_markets=["KRW-A", "KRW-B"],
        current_hot={"KRW-A"},
        current_controls={"KRW-B"},
        next_markets=["KRW-A", "KRW-B"],
        next_hot={"KRW-A"},
        next_controls={"KRW-B"},
    )


def test_recording_rotates_compresses_and_preserves_sequence(tmp_path):
    recorder = MarketDataRecorder(_config(tmp_path))
    recorder.start()
    for index in range(20):
        assert recorder.record_meta("sample", index=index)
    assert recorder.stop(timeout=10)

    files = recording_files(tmp_path, "test")
    assert len(files) >= 2
    assert all(path.name.endswith(".jsonl.gz") for path in files)
    assert not list(tmp_path.glob("*.part"))

    records = list(iter_records(tmp_path, prefix="test"))
    assert len(records) == 20
    assert [row["seq"] for row in records] == list(range(1, 21))
    assert [row["data"]["index"] for row in records] == list(range(20))
    stats = recorder.stats()
    assert stats.written == 20
    assert stats.dropped == 0
    assert stats.compressed_segments == len(files)


def test_abandoned_part_recovers_complete_lines_and_discards_torn_tail(tmp_path):
    part = tmp_path / "test-old-000001.jsonl.part"
    rows = [
        {"schema_version": 1, "seq": 1, "kind": "meta", "data": {"n": 1}},
        {"schema_version": 1, "seq": 2, "kind": "meta", "data": {"n": 2}},
    ]
    part.write_bytes(
        b"".join(
            json.dumps(row).encode("utf-8") + b"\n" for row in rows
        )
        + b'{"schema_version":1,"seq":3'
    )

    recorder = MarketDataRecorder(_config(tmp_path))
    recorder.start()
    assert recorder.stop(timeout=10)

    assert not part.exists()
    recovered = list(iter_records(tmp_path, prefix="test"))
    assert [row["seq"] for row in recovered] == [1, 2]
    assert recorder.stats().recovered_parts == 1


def test_queue_full_drops_instead_of_blocking_callback(monkeypatch, tmp_path):
    recorder = MarketDataRecorder(_config(tmp_path, compress=False))
    recorder.start()

    def full(_item):
        raise queue.Full

    monkeypatch.setattr(recorder._queue, "put_nowait", full)
    assert recorder.record_meta("will-drop") is False
    assert recorder.stats().dropped == 1
    assert recorder.stop(timeout=10)


def test_trade_and_orderbook_normalization_is_replayable(tmp_path):
    recorder = MarketDataRecorder(
        _config(tmp_path, compress=False, rotate_bytes=10_000)
    )
    recorder.start()
    assert recorder.record_trade(
        {
            "code": "KRW-BTC",
            "trade_price": 100.0,
            "trade_volume": 2.0,
            "ask_bid": "BID",
            "trade_timestamp": 1234,
            "sequential_id": 77,
            "ignored": "secret-noise",
        },
        received_ns=10,
        received_monotonic_ns=20,
    )
    assert recorder.record_orderbook(
        {
            "code": "KRW-BTC",
            "timestamp": 1300,
            "total_ask_size": 3.0,
            "total_bid_size": 4.0,
            "orderbook_units": [
                {
                    "ask_price": 101.0,
                    "bid_price": 100.0,
                    "ask_size": 1.5,
                    "bid_size": 2.5,
                    "extra": "discard",
                }
            ],
        },
        received_ns=11,
        received_monotonic_ns=21,
    )
    assert recorder.stop(timeout=10)

    rows = list(iter_records(tmp_path, prefix="test"))
    assert [row["kind"] for row in rows] == ["trade", "orderbook"]
    trade, book = rows
    assert trade["market"] == "KRW-BTC"
    assert trade["exchange_timestamp_ms"] == 1234
    assert trade["received_ns"] == 10
    assert trade["data"]["sequential_id"] == 77
    assert "ignored" not in trade["data"]
    assert book["data"]["orderbook_units"] == [
        {
            "ask_price": 101.0,
            "bid_price": 100.0,
            "ask_size": 1.5,
            "bid_size": 2.5,
        }
    ]
    stats = recorder.stats()
    assert stats.trade_events == 1
    assert stats.orderbook_events == 1


def test_retention_never_truncates_the_active_session(tmp_path):
    recorder = MarketDataRecorder(
        _config(
            tmp_path,
            compress=False,
            rotate_bytes=120,
            max_files=2,
        )
    )
    recorder.start()
    for index in range(30):
        recorder.record_meta("segment", index=index, payload="x" * 50)
    assert recorder.stop(timeout=10)

    files = recording_files(tmp_path, "test")
    # A segment-level max-files deletion used to remove segment 1 (and its
    # session_start) while leaving an unreplayable tail. The active capture is
    # kept as a complete unit; old sessions are retired as whole units.
    assert len(files) > 2
    assert [row["seq"] for row in iter_records(files)] == list(range(1, 31))
    assert not list(tmp_path.glob("*.part"))


def test_iter_records_reads_plain_and_gzip_segments(tmp_path):
    plain = tmp_path / "test-a.jsonl"
    plain.write_text(json.dumps({"seq": 1}) + "\n", encoding="utf-8")
    compressed = tmp_path / "test-b.jsonl.gz"
    with gzip.open(compressed, "wt", encoding="utf-8") as handle:
        handle.write(json.dumps({"seq": 2}) + "\n")
    (tmp_path / "test-c.jsonl.part").write_text(
        json.dumps({"seq": 3}) + "\n", encoding="utf-8"
    )

    assert [row["seq"] for row in iter_records(tmp_path, prefix="test")] == [1, 2]


def test_duplicate_plain_and_gzip_artifact_is_read_once(tmp_path):
    plain = tmp_path / "test-session-000001.jsonl"
    row = {"schema_version": 1, "seq": 1, "kind": "meta", "data": {"n": 1}}
    payload = json.dumps(row) + "\n"
    plain.write_text(payload, encoding="utf-8")
    compressed = tmp_path / "test-session-000001.jsonl.gz"
    with gzip.open(compressed, "wt", encoding="utf-8") as handle:
        handle.write(payload)

    files = recording_files(tmp_path, "test")

    assert files == [compressed]
    assert list(iter_records([plain, compressed])) == [row]


def test_session_end_contains_recorder_quality_snapshot(tmp_path):
    recorder = MarketDataRecorder(
        _config(tmp_path, compress=False, rotate_bytes=10_000)
    )
    recorder.start()
    assert recorder.record_meta("session_start")
    assert recorder.record_session_end(websocket_errors={}, orders_submitted=0)
    assert recorder.stop(timeout=10)

    rows = list(iter_records(tmp_path, prefix="test"))
    end = rows[-1]["data"]
    assert end["event"] == "session_end"
    assert end["recorder_stats"]["enqueued_before_session_end"] == 1
    assert end["recorder_stats"]["dropped"] == 0


def test_websocket_error_fails_recording_automation_gate():
    assert recording_integrity_ok(
        stopped_cleanly=True,
        dropped=0,
        last_error="",
        websocket_errors={},
        trade_events=1,
    )
    assert not recording_integrity_ok(
        stopped_cleanly=True,
        dropped=0,
        last_error="",
        websocket_errors={"timestamp stale": 1},
        trade_events=1,
    )


def test_recording_gate_requires_trade_and_requested_orderbook_events():
    empty_reasons = recording_integrity_reasons(
        stopped_cleanly=True,
        dropped=0,
        last_error="",
        websocket_errors={},
        trade_events=0,
        orderbook_events=0,
        orderbook_requested=True,
    )
    assert empty_reasons == ["zero_trade_events", "zero_orderbook_events"]
    assert not recording_integrity_ok(
        stopped_cleanly=True,
        dropped=0,
        last_error="",
        websocket_errors={},
        trade_events=0,
        orderbook_events=0,
        orderbook_requested=False,
    )
    assert recording_integrity_ok(
        stopped_cleanly=True,
        dropped=0,
        last_error="",
        websocket_errors={},
        trade_events=1,
        orderbook_events=0,
        orderbook_requested=False,
    )
    assert not recording_integrity_ok(
        stopped_cleanly=True,
        dropped=0,
        last_error="",
        websocket_errors={},
        trade_events=1,
        orderbook_events=0,
        orderbook_requested=True,
    )
