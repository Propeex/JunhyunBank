import json

import websocket

from junhyunbank.market_stream import MarketStream


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
