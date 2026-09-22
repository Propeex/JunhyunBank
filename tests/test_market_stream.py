import json
import time

import websocket

from junhyunbank.market_stream import MarketStream, TRANSPORT_AGE_SECONDS_KEY


def test_public_websocket_connection_suppresses_origin(monkeypatch):
    captured = {}
    sentinel = object()

    def fake_create_connection(url, **kwargs):
        captured["url"] = url
        captured.update(kwargs)
        return sentinel

    monkeypatch.setattr(websocket, "create_connection", fake_create_connection)
    MarketStream._last_connect_attempt = 0.0
    stream = MarketStream(["KRW-BTC"], on_trade=lambda event: None)

    assert stream._open_connection() is sentinel
    assert captured["url"] == MarketStream.URL
    assert captured["suppress_origin"] is True
    assert captured["timeout"] == 10


def test_orderbook_subscription_preserves_pair_code_and_depth_suffix():
    stream = MarketStream(
        ["KRW-BTC", "KRW-ETH"],
        on_orderbook=lambda event: None,
        orderbook_depth=5,
    )
    request = stream._subscription()
    orderbook = next(row for row in request if row.get("type") == "orderbook")

    assert orderbook["codes"] == ["KRW-BTC.5", "KRW-ETH.5"]
    assert orderbook["is_only_realtime"] is True


class _FakeSocket:
    def __init__(self):
        self.sent = []
        self.closed = False

    def send(self, payload):
        self.sent.append(json.loads(payload))

    def close(self):
        self.closed = True


def test_live_subscription_update_reuses_existing_socket():
    stream = MarketStream(["KRW-BTC"], on_orderbook=lambda event: None)
    socket = _FakeSocket()
    stream._ws = socket
    stream._connected = True

    assert stream.update_markets(["KRW-ETH", "KRW-XRP"]) is True
    assert socket.closed is False
    assert len(socket.sent) == 1
    orderbook = next(row for row in socket.sent[0] if row.get("type") == "orderbook")
    assert orderbook["codes"] == ["KRW-ETH.5", "KRW-XRP.5"]


def test_stop_closes_active_socket_to_interrupt_blocking_receive():
    stream = MarketStream(["KRW-BTC"], on_trade=lambda event: None)
    socket = _FakeSocket()
    stream._ws = socket
    stream._connected = True

    stream.stop()

    assert socket.closed is True


def test_upbit_error_payload_is_not_treated_as_market_data():
    assert (
        MarketStream._server_error(
            {"error": {"name": "Too Many Requests", "message": "rate limited"}}
        )
        == "Too Many Requests: rate limited"
    )
    assert MarketStream._server_error({"type": "trade"}) is None


def test_public_event_timestamp_classifier_fails_closed_for_old_future_and_invalid():
    now = 1_700_000_000.0

    assert MarketStream.classify_event_timestamp(
        {"type": "trade", "trade_timestamp": int((now - 2.9) * 1000)},
        now_wall=now,
        max_lag_seconds=3.0,
        max_future_seconds=2.0,
    ) == "ok"
    assert MarketStream.classify_event_timestamp(
        {"type": "trade", "trade_timestamp": int((now - 3.1) * 1000)},
        now_wall=now,
        max_lag_seconds=3.0,
        max_future_seconds=2.0,
    ) == "stale"
    assert MarketStream.classify_event_timestamp(
        {"type": "orderbook", "timestamp": int((now + 2.1) * 1000)},
        now_wall=now,
        max_lag_seconds=3.0,
        max_future_seconds=2.0,
    ) == "invalid"
    assert MarketStream.classify_event_timestamp(
        {"type": "trade", "trade_timestamp": "not-a-timestamp"},
        now_wall=now,
    ) == "invalid"
    assert MarketStream.classify_event_timestamp(
        {"type": "trade"}, now_wall=now
    ) == "invalid"


def test_dropped_public_frame_does_not_advance_freshness_or_callback(monkeypatch):
    received = []
    statuses = []
    errors = []
    stream = MarketStream(
        ["KRW-BTC"],
        on_trade=received.append,
        on_status=statuses.append,
        on_error=errors.append,
    )
    stream._last_message = 123.0
    monkeypatch.setattr(
        "junhyunbank.market_stream.time.time", lambda: 1_700_000_000.0
    )
    monkeypatch.setattr("junhyunbank.market_stream.time.monotonic", lambda: 500.0)

    stale = {
        "type": "trade",
        "code": "KRW-BTC",
        "trade_timestamp": 1_699_999_990_000,
    }
    assert stream._handle_market_event(stale) is False

    assert received == []
    assert stream._last_message == 123.0
    assert stream._message_count == 0
    assert stream._stale_drop_count == 1
    assert stream._invalid_drop_count == 0
    assert errors and "stale" in errors[-1]
    assert statuses[-1]["state"] == "data_dropped"
    assert statuses[-1]["market_codes"] == ["KRW-BTC"]
    assert statuses[-1]["stale_drops"] == 1
    assert statuses[-1]["invalid_drops"] == 0


def test_valid_public_frame_preserves_transport_age_and_overwrites_metadata(monkeypatch):
    received = []
    stream = MarketStream(["KRW-BTC"], on_trade=received.append)
    monkeypatch.setattr(
        "junhyunbank.market_stream.time.time", lambda: 1_700_000_000.0
    )
    monkeypatch.setattr("junhyunbank.market_stream.time.monotonic", lambda: 500.0)
    event = {
        "type": "trade",
        "code": "KRW-BTC",
        "trade_timestamp": 1_699_999_997_250,
        TRANSPORT_AGE_SECONDS_KEY: 0.0,
    }

    assert stream._handle_market_event(event) is True
    assert received == [event]
    assert abs(event[TRANSPORT_AGE_SECONDS_KEY] - 2.75) < 1e-9
    assert abs(stream._last_message - 497.25) < 1e-9
    assert abs(stream.age_seconds - 2.75) < 1e-9
    assert stream._message_count == 1


def test_stream_status_includes_market_codes_and_drop_counters():
    statuses = []
    stream = MarketStream(
        ["KRW-BTC", "KRW-ETH"],
        on_trade=lambda event: None,
        on_status=statuses.append,
    )
    stream._stale_drop_count = 2
    stream._invalid_drop_count = 3

    stream._emit_status("connecting")

    assert statuses[-1]["market_codes"] == ["KRW-BTC", "KRW-ETH"]
    assert statuses[-1]["stale_drops"] == 2
    assert statuses[-1]["invalid_drops"] == 3
