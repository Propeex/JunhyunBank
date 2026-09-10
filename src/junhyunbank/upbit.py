from __future__ import annotations

import hashlib
import time
import uuid
from typing import Any
from urllib.parse import unquote, urlencode

import httpx
import jwt

from .models import Candle


class UpbitAPIError(RuntimeError):
    pass


def build_query_string(values: dict[str, Any] | None) -> str:
    if not values:
        return ""
    pairs: list[tuple[str, Any]] = []
    for key, value in values.items():
        if value is None:
            continue
        if isinstance(value, (list, tuple)):
            pairs.extend((key, item) for item in value)
        else:
            pairs.append((key, value))
    return unquote(urlencode(pairs, doseq=True, safe=","))


class UpbitClient:
    BASE_URL = "https://api.upbit.com"

    def __init__(
        self,
        access_key: str | None = None,
        secret_key: str | None = None,
        timeout: float = 10.0,
    ) -> None:
        self.access_key = access_key
        self.secret_key = secret_key
        self.http = httpx.Client(
            base_url=self.BASE_URL,
            timeout=timeout,
            headers={"Accept": "application/json", "User-Agent": "JunhyunBank/1.0"},
        )

    def close(self) -> None:
        self.http.close()

    def _authorization(self, values: dict[str, Any] | None = None) -> str:
        if not self.access_key or not self.secret_key:
            raise UpbitAPIError("업비트 API 키가 설정되지 않았습니다.")

        payload: dict[str, str] = {
            "access_key": self.access_key,
            "nonce": str(uuid.uuid4()),
        }
        query_string = build_query_string(values)
        if query_string:
            payload["query_hash"] = hashlib.sha512(query_string.encode("utf-8")).hexdigest()
            payload["query_hash_alg"] = "SHA512"
        token = jwt.encode(payload, self.secret_key, algorithm="HS512")
        return f"Bearer {token}"

    @staticmethod
    def _raise_for_error(response: httpx.Response) -> None:
        if response.is_success:
            return
        try:
            payload = response.json()
            if isinstance(payload, dict) and isinstance(payload.get("error"), dict):
                err = payload["error"]
                raise UpbitAPIError(
                    f"{response.status_code} {err.get('name', 'upbit_error')}: "
                    f"{err.get('message', '요청에 실패했습니다.')}"
                )
        except ValueError:
            pass
        raise UpbitAPIError(f"업비트 API 오류: HTTP {response.status_code}")

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        private: bool = False,
    ) -> Any:
        auth_values = json_body if json_body is not None else params
        headers: dict[str, str] = {}
        if private:
            headers["Authorization"] = self._authorization(auth_values)
        if json_body is not None:
            headers["Content-Type"] = "application/json"

        for attempt in range(3):
            response = self.http.request(
                method,
                path,
                params=params,
                json=json_body,
                headers=headers,
            )
            if response.status_code == 429:
                time.sleep(1.05 * (attempt + 1))
                continue
            if response.status_code == 418:
                raise UpbitAPIError("업비트 요청 제한으로 IP가 일시 차단되었습니다. 잠시 후 다시 시도하세요.")
            self._raise_for_error(response)
            return response.json()
        raise UpbitAPIError("업비트 요청 제한(429)이 반복되어 요청을 중단했습니다.")

    def get_markets(self) -> list[dict[str, Any]]:
        return self._request("GET", "/v1/market/all", params={"is_details": "true"})

    def get_all_krw_tickers(self) -> list[dict[str, Any]]:
        return self._request(
            "GET", "/v1/ticker/all", params={"quote_currencies": "KRW"}
        )

    def get_tickers(self, markets: list[str]) -> list[dict[str, Any]]:
        if not markets:
            return []
        return self._request(
            "GET", "/v1/ticker", params={"markets": ",".join(markets)}
        )

    def get_minute_candles(
        self, market: str, unit: int = 5, count: int = 120
    ) -> list[Candle]:
        raw = self._request(
            "GET",
            f"/v1/candles/minutes/{unit}",
            params={"market": market, "count": count},
        )
        candles = [
            Candle(
                market=market,
                timestamp=row["candle_date_time_kst"],
                open=float(row["opening_price"]),
                high=float(row["high_price"]),
                low=float(row["low_price"]),
                close=float(row["trade_price"]),
                volume=float(row["candle_acc_trade_volume"]),
                trade_value=float(row["candle_acc_trade_price"]),
            )
            for row in raw
        ]
        candles.reverse()
        return candles

    def get_accounts(self) -> list[dict[str, Any]]:
        return self._request("GET", "/v1/accounts", private=True)

    def test_credentials(self) -> None:
        self.get_accounts()

    def place_market_buy(self, market: str, krw_amount: float) -> dict[str, Any]:
        body = {
            "market": market,
            "side": "bid",
            "price": f"{krw_amount:.0f}",
            "ord_type": "price",
            "identifier": f"junhyunbank-{uuid.uuid4()}",
        }
        return self._request("POST", "/v1/orders", json_body=body, private=True)

    def place_market_sell(self, market: str, volume: float) -> dict[str, Any]:
        body = {
            "market": market,
            "side": "ask",
            "volume": format(volume, ".16g"),
            "ord_type": "market",
            "identifier": f"junhyunbank-{uuid.uuid4()}",
        }
        return self._request("POST", "/v1/orders", json_body=body, private=True)
