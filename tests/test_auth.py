import hashlib

import httpx
import jwt
import pytest

from junhyunbank.upbit import UpbitAPIError, UpbitClient, build_query_string


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
    assert body["smp_type"] == "cancel_taker"
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
    assert body["smp_type"] == "cancel_taker"
    assert body["volume"] == "0.123"
    assert "price" not in body


def test_market_sell_uses_cancel_taker_self_match_protection():
    client = UpbitClient("access", "secret")
    calls = []

    def fake_request(method, path, **kwargs):
        calls.append((method, path, kwargs))
        return {"uuid": "order-3"}

    client._request = fake_request
    try:
        client.place_market_sell(
            "KRW-BTC", 0.123, identifier="junhyunbank-test-market-sell"
        )
    finally:
        client.close()

    method, path, kwargs = calls[0]
    assert (method, path) == ("POST", "/v1/orders")
    assert kwargs["json_body"]["smp_type"] == "cancel_taker"


def _response(status_code, payload):
    return httpx.Response(
        status_code,
        json=payload,
        request=httpx.Request("GET", "https://api.upbit.com/v1/test"),
    )


def test_get_retries_transient_server_error_then_returns_success(monkeypatch):
    client = UpbitClient()
    responses = [
        _response(503, {"error": {"name": "temporary", "message": "retry"}}),
        _response(200, {"ok": True}),
    ]
    calls = []
    sleeps = []

    def fake_request(*args, **kwargs):
        calls.append((args, kwargs))
        return responses.pop(0)

    monkeypatch.setattr(client.http, "request", fake_request)
    monkeypatch.setattr("junhyunbank.upbit.time.sleep", sleeps.append)
    try:
        result = client._request("GET", "/v1/test")
    finally:
        client.close()

    assert result == {"ok": True}
    assert len(calls) == 2
    assert sleeps == [client._GET_RETRY_BASE_SECONDS]


def test_get_retries_timeout_but_post_never_retries(monkeypatch):
    client = UpbitClient()
    get_calls = 0

    def timeout_then_success(*args, **kwargs):
        nonlocal get_calls
        get_calls += 1
        if get_calls == 1:
            raise httpx.ReadTimeout("temporary timeout")
        return _response(200, {"ok": True})

    monkeypatch.setattr(client.http, "request", timeout_then_success)
    monkeypatch.setattr("junhyunbank.upbit.time.sleep", lambda _: None)
    try:
        assert client._request("GET", "/v1/test") == {"ok": True}
        assert get_calls == 2

        post_calls = 0

        def post_failure(*args, **kwargs):
            nonlocal post_calls
            post_calls += 1
            return _response(
                503, {"error": {"name": "temporary", "message": "do not retry"}}
            )

        monkeypatch.setattr(client.http, "request", post_failure)
        with pytest.raises(UpbitAPIError) as error:
            client._request("POST", "/v1/orders", json_body={"market": "KRW-BTC"})
    finally:
        client.close()

    assert error.value.status_code == 503
    assert post_calls == 1


def test_post_timeout_is_propagated_without_retry(monkeypatch):
    client = UpbitClient()
    calls = 0

    def timeout(*args, **kwargs):
        nonlocal calls
        calls += 1
        raise httpx.ReadTimeout("ambiguous POST outcome")

    monkeypatch.setattr(client.http, "request", timeout)
    try:
        with pytest.raises(httpx.ReadTimeout):
            client._request("POST", "/v1/orders", json_body={"market": "KRW-BTC"})
    finally:
        client.close()

    assert calls == 1
