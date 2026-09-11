import hashlib

import jwt

from junhyunbank.upbit import UpbitClient, build_query_string


def test_query_string_keeps_comma_unescaped():
    assert build_query_string({"markets": "KRW-BTC,KRW-ETH"}) == "markets=KRW-BTC,KRW-ETH"


def test_authorization_contains_query_hash():
    client = UpbitClient("access", "secret")
    try:
        header = client._authorization({"market": "KRW-BTC", "count": 10})
    finally:
        client.close()
    token = header.removeprefix("Bearer ")
    payload = jwt.decode(token, "secret", algorithms=["HS512"])
    expected = hashlib.sha512(b"market=KRW-BTC&count=10").hexdigest()
    assert payload["query_hash"] == expected
    assert payload["query_hash_alg"] == "SHA512"


def test_get_markets_retries_legacy_details_spelling_when_needed():
    client = UpbitClient()
    calls = []
    payloads = [
        [{"market": "KRW-BTC", "korean_name": "BTC"}],
        [
            {
                "market": "KRW-BTC",
                "korean_name": "BTC",
                "market_event": {"warning": False, "caution": {}},
            }
        ],
    ]

    def fake_request(method, path, **kwargs):
        calls.append(kwargs.get("params"))
        return payloads[len(calls) - 1]

    client._request = fake_request
    try:
        rows = client.get_markets()
    finally:
        client.close()

    assert rows[0]["market_event"]["warning"] is False
    assert calls == [{"is_details": "true"}, {"isDetails": "true"}]


def test_get_markets_uses_current_details_response_without_second_call():
    client = UpbitClient()
    calls = []

    def fake_request(method, path, **kwargs):
        calls.append(kwargs.get("params"))
        return [
            {
                "market": "KRW-BTC",
                "market_event": {"warning": False, "caution": {}},
            }
        ]

    client._request = fake_request
    try:
        rows = client.get_markets()
    finally:
        client.close()

    assert rows[0]["market"] == "KRW-BTC"
    assert calls == [{"is_details": "true"}]


def test_live_buy_order_remains_best_ioc_not_market_or_depth_walking_limit():
    client = UpbitClient("access", "secret")
    calls = []

    def fake_request(method, path, **kwargs):
        calls.append((method, path, kwargs))
        return {"uuid": "order-1"}

    client._request = fake_request
    try:
        client.place_best_ioc_buy("KRW-BTC", 12345, identifier="junhyunbank-test-buy")
    finally:
        client.close()

    method, path, kwargs = calls[0]
    body = kwargs["json_body"]
    assert (method, path) == ("POST", "/v1/orders")
    assert body["side"] == "bid"
    assert body["ord_type"] == "best"
    assert body["time_in_force"] == "ioc"
    assert body["price"] == "12345"
    assert "volume" not in body


def test_live_sell_order_remains_best_ioc():
    client = UpbitClient("access", "secret")
    calls = []

    def fake_request(method, path, **kwargs):
        calls.append((method, path, kwargs))
        return {"uuid": "order-2"}

    client._request = fake_request
    try:
        client.place_best_ioc_sell("KRW-BTC", 0.123, identifier="junhyunbank-test-sell")
    finally:
        client.close()

    method, path, kwargs = calls[0]
    body = kwargs["json_body"]
    assert (method, path) == ("POST", "/v1/orders")
    assert body["side"] == "ask"
    assert body["ord_type"] == "best"
    assert body["time_in_force"] == "ioc"
    assert body["volume"] == "0.123"
    assert "price" not in body