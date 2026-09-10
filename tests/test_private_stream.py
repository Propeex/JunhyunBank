import json

from junhyunbank.private_stream import PrivateAccountStream


def test_private_subscription_uses_one_connection_for_orders_and_assets():
    payload = PrivateAccountStream._subscription()

    assert payload[1] == {"type": "myOrder"}
    assert payload[2] == {"type": "myAsset"}
    assert "codes" not in payload[2]
    assert payload[-1] == {"format": "DEFAULT"}
    json.dumps(payload)


def test_private_connection_builds_fresh_bearer_header_and_suppresses_origin(monkeypatch):
    calls = []
    tokens = iter(["Bearer first-token", "Bearer second-token"])

    def fake_create_connection(url, **kwargs):
        calls.append((url, kwargs))
        return object()

    monkeypatch.setattr("junhyunbank.private_stream.websocket.create_connection", fake_create_connection)
    stream = PrivateAccountStream(lambda: next(tokens))

    stream._open_connection()
    stream._open_connection()

    assert calls[0][0] == PrivateAccountStream.URL
    assert calls[0][1]["header"] == ["Authorization: Bearer first-token"]
    assert calls[1][1]["header"] == ["Authorization: Bearer second-token"]
    assert calls[0][1]["suppress_origin"] is True


def test_private_stream_redacts_authorization_from_errors():
    stream = PrivateAccountStream(lambda: "Bearer unused")
    stream._active_auth = "Bearer super-secret-jwt"

    safe = stream._redact("handshake failed: Bearer super-secret-jwt / super-secret-jwt")

    assert "super-secret-jwt" not in safe
    assert "redacted" in safe


def test_private_stream_rejects_non_bearer_authorization(monkeypatch):
    monkeypatch.setattr(
        "junhyunbank.private_stream.websocket.create_connection",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("must not connect")),
    )
    stream = PrivateAccountStream(lambda: "not-a-bearer")

    try:
        stream._open_connection()
    except RuntimeError as exc:
        assert "Authorization" in str(exc)
    else:
        raise AssertionError("expected authorization failure")
