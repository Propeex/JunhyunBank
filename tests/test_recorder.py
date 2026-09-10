from __future__ import annotations

import gzip
import json
from pathlib import Path
import queue

from junhyunbank.recorder import (
    MarketDataRecorder,
    RecorderConfig,
    iter_records,
    recording_files,
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


def test_retention_keeps_only_newest_configured_file_count(tmp_path):
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
    assert 1 <= len(files) <= 2
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
