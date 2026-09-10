from __future__ import annotations

import argparse
import time

from junhyunbank.private_stream import PrivateAccountStream
from junhyunbank.security import KeyStore
from junhyunbank.upbit import UpbitClient


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Stored keyring credentials로 Upbit Private WebSocket 인증/구독만 검증합니다. "
            "주문을 생성·취소하지 않습니다."
        )
    )
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument("--stable", type=float, default=5.0)
    args = parser.parse_args()

    access, secret = KeyStore().load()
    if not access or not secret:
        print("[FAIL] OS keyring에 JunhyunBank Upbit API Key가 없습니다.", flush=True)
        return 2

    client = UpbitClient(access, secret)
    errors: list[str] = []
    states: list[str] = []

    def on_error(message: str) -> None:
        errors.append(str(message))

    def on_status(payload: dict) -> None:
        state = str(payload.get("state") or "")
        if not states or states[-1] != state:
            states.append(state)

    stream = PrivateAccountStream(
        lambda: client._authorization(None),
        on_error=on_error,
        on_status=on_status,
        name="smoke-upbit-private-account",
    )
    stream.start()
    deadline = time.monotonic() + max(5.0, args.timeout)
    stable = max(1.0, args.stable)
    try:
        while time.monotonic() < deadline:
            if stream.connected and stream.connected_age_seconds >= stable and not errors:
                print(
                    "[OK] Private WS authenticated subscription stable; "
                    f"connected_for={stream.connected_age_seconds:.1f}s "
                    f"messages={stream.message_count} "
                    "(myOrder/myAsset 이벤트 0건이어도 정상)",
                    flush=True,
                )
                return 0
            if errors and not stream.connected:
                # Give reconnect a short opportunity. Persistent auth/format
                # failures will remain disconnected and produce a clear result.
                time.sleep(0.25)
            else:
                time.sleep(0.10)
        detail = errors[-1] if errors else stream.last_error or "stable connection timeout"
        print(
            f"[FAIL] Private WS 검증 실패: connected={stream.connected} "
            f"states={states[-5:]} error={detail}",
            flush=True,
        )
        return 1
    finally:
        stream.stop()
        client.close()


if __name__ == "__main__":
    raise SystemExit(main())
