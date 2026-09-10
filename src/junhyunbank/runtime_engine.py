from __future__ import annotations

import time
from typing import Any

from .engine import TradingEngine as BaseTradingEngine
from .market_stream import MarketStream


class TradingEngine(BaseTradingEngine):
    """V3 runtime-hardened engine.

    The trading/order/risk implementation remains in the V2 base engine. This
    layer changes runtime market-data lifecycle and compatibility handling only,
    so order sizing, entries, exits, and managed-position protection remain in
    the base engine.
    """

    _ALERT_TRUE = {
        "1",
        "true",
        "yes",
        "on",
        "active",
        "warning",
        "caution",
        "risk",
    }
    _ALERT_FALSE = {
        "",
        "0",
        "false",
        "no",
        "off",
        "inactive",
        "none",
        "normal",
    }

    @classmethod
    def _coerce_alert_flag(cls, value: Any) -> bool | None:
        """Parse Upbit alert fields without Python truthiness surprises.

        A previous implementation used ``bool(value)`` for compatibility. That
        is unsafe for wire values such as the string ``"false"`` because every
        non-empty string becomes True and can wipe the entire market universe.
        Only explicit known true/false forms are accepted here.
        """
        if isinstance(value, bool):
            return value
        if value is None:
            return False
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return value != 0
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in cls._ALERT_TRUE:
                return True
            if normalized in cls._ALERT_FALSE:
                return False
        return None

    @classmethod
    def _market_alert_status(cls, row: dict[str, Any]) -> tuple[bool, bool]:
        """Return (is_flagged, schema_understood).

        Unknown alert shapes are kept out of the live trading universe rather
        than guessed. The caller can then surface a clear diagnostic instead of
        silently ending up at ``Trade WS 0/0``.
        """
        event = row.get("market_event")
        if isinstance(event, dict):
            warning_known = True
            warning = False
            if "warning" in event:
                parsed_warning = cls._coerce_alert_flag(event.get("warning"))
                if parsed_warning is None:
                    warning_known = False
                else:
                    warning = parsed_warning
            else:
                # market_event without warning is not the documented shape.
                warning_known = False

            caution_known = True
            caution_flagged = False
            caution = event.get("caution")
            if isinstance(caution, dict):
                for value in caution.values():
                    parsed = cls._coerce_alert_flag(value)
                    if parsed is None:
                        caution_known = False
                    elif parsed:
                        caution_flagged = True
            else:
                parsed = cls._coerce_alert_flag(caution)
                if parsed is None:
                    caution_known = False
                else:
                    caution_flagged = parsed

            return warning or caution_flagged, warning_known and caution_known

        # Legacy fallback. market_warning was removed from current Upbit market
        # responses, but old/proxied responses may still expose it.
        if "market_warning" in row:
            legacy = str(row.get("market_warning") or "").strip().upper()
            if legacy in {"CAUTION", "WARNING", "WARN", "TRUE", "1"}:
                return True, True
            if legacy in {"", "NONE", "NORMAL", "FALSE", "0"}:
                return False, True
            return False, False

        return False, False

    @classmethod
    def _is_warning_market(cls, row: dict[str, Any]) -> bool:
        flagged, understood = cls._market_alert_status(row)
        return flagged or not understood

    def _refresh_markets(self) -> None:
        # Base V2 calls market refresh on every loop while the universe is empty.
        # Back off failed discovery attempts here so an upstream/schema problem
        # cannot turn into a self-inflicted public REST rate-limit storm.
        now = time.monotonic()
        retry_at = float(getattr(self, "_market_discovery_retry_at", 0.0))
        if not self._allowed_markets and now < retry_at:
            return

        try:
            rows = self.client.get_markets()
        except Exception:
            self._market_discovery_ready = False
            self._market_discovery_retry_at = now + 10.0
            raise

        raw_krw: list[str] = []
        allowed: list[str] = []
        flagged_count = 0
        unknown_count = 0
        unknown_samples: list[str] = []

        for row in rows:
            if not isinstance(row, dict):
                continue
            market = str(row.get("market") or "").strip().upper()
            if not market.startswith("KRW-"):
                continue
            raw_krw.append(market)
            flagged, understood = self._market_alert_status(row)
            if flagged:
                flagged_count += 1
                continue
            if not understood:
                unknown_count += 1
                if len(unknown_samples) < 3:
                    unknown_samples.append(market)
                continue
            allowed.append(market)

        raw_krw = sorted(dict.fromkeys(raw_krw))
        allowed = sorted(dict.fromkeys(allowed))

        if not raw_krw:
            self._market_discovery_ready = False
            self._market_discovery_retry_at = now + 10.0
            sample_codes = [
                str(row.get("market") or "")
                for row in rows[:5]
                if isinstance(row, dict)
            ]
            raise RuntimeError(
                "업비트 페어 목록에서 KRW 마켓을 찾지 못했습니다. "
                f"응답 {len(rows)}건, 예시={sample_codes}"
            )

        if not allowed:
            self._market_discovery_ready = False
            self._market_discovery_retry_at = now + 10.0
            raise RuntimeError(
                "KRW 마켓은 수신했지만 안전하게 해석 가능한 종목이 0개입니다. "
                f"KRW={len(raw_krw)}, 경보제외={flagged_count}, "
                f"경보형식미확인={unknown_count}, 예시={unknown_samples}. "
                "자동매매는 안전을 위해 시작하지 않습니다."
            )

        self._market_discovery_retry_at = 0.0
        self._market_discovery_ready = True
        if allowed == self._allowed_markets:
            return

        self._allowed_markets = allowed
        self._restart_global_streams()
        self._emit(
            "market_universe",
            (
                f"KRW 실시간 감시 종목 {len(allowed)}개 "
                f"(전체 KRW {len(raw_krw)} / 경보 제외 {flagged_count} / "
                f"형식 미확인 제외 {unknown_count})"
            ),
            count=len(allowed),
            krw_total=len(raw_krw),
            flagged=flagged_count,
            unknown=unknown_count,
        )

    def _reset_orderbook_state(self, market: str) -> None:
        self.strategy.reset_orderbook(market)

    def _restart_deep_stream(self, markets: list[str]) -> None:
        markets = sorted(dict.fromkeys(markets))
        if markets == self._deep_markets:
            return

        previous = set(self._deep_markets)
        current = set(markets)
        added = sorted(current - previous)
        removed = sorted(previous - current)

        for market in added:
            self._reset_orderbook_state(market)

        self._deep_markets = markets
        now = time.monotonic()
        self._deep_entered_at = {
            market: self._deep_entered_at.get(market, now)
            for market in markets
        }

        if not markets:
            if self._deep_stream:
                self._deep_stream.stop()
                self._deep_stream = None
        elif self._deep_stream is None:
            self._deep_stream = MarketStream(
                markets,
                on_orderbook=self._on_orderbook,
                on_error=lambda message: self._emit(
                    "warning", f"호가 스트림: {message}"
                ),
                on_status=self._stream_status,
                orderbook_depth=max(5, self.config.strategy.orderbook_depth),
                name="upbit-orderbook-deep",
            )
            self._deep_stream.start()
        else:
            # Upbit supports replacing the active subscription by sending a new
            # subscription message on the existing socket. MarketStream keeps
            # the new list for the next reconnect even if this send races with
            # a reconnect, so there is no need to open a second connection.
            self._deep_stream.update_markets(markets)

        if added or removed:
            self.events.put(
                {
                    "type": "deep_set",
                    "added": added,
                    "removed": removed,
                    "count": len(markets),
                }
            )
