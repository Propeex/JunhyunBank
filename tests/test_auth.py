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
