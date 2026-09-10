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
