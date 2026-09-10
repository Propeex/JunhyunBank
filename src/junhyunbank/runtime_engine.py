from __future__ import annotations

import threading
import time
from typing import Any

from .engine import TradingEngine as BaseTradingEngine
from .market_stream import MarketStream
from .private_stream import PrivateAccountStream


class TradingEngine(BaseTradingEngine):
    """Runtime-hardened engine.

    The core trading/order/risk implementation remains in the base engine. This
    layer owns runtime market-data compatibility plus the authenticated account
    event stream used to accelerate durable order reconciliation. Private
    WebSocket data is deliberately auxiliary: canonical fills still come from
    REST ``GET /v1/order`` and account balances are never used to infer managed
    quantity.
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

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._private_stream: PrivateAccountStream | None = None
        self._private_reconcile_lock = threading.Lock()
        self._private_reconcile_requested: set[str] = set()
        self._order_reconcile_retry_at: dict[str, float] = {}
        self._order_reconcile_failures: dict[str, int] = {}
        self._private_status_state = "stopped"
        self._private_order_events = 0
        self._private_asset_events = 0

    def start(self) -> None:
        if self.running:
            return
        with self._private_reconcile_lock:
            self._private_reconcile_requested.clear()
        self._order_reconcile_retry_at.clear()
        self._order_reconcile_failures.clear()
        self._private_order_events = 0
        self._private_asset_events = 0
        super().start()
        self._start_private_account_stream()

    def _start_private_account_stream(self) -> None:
        # Test/dummy clients intentionally do not expose the package-internal
        # authorization helper. Real UpbitClient does. Failure to bring up this
        # auxiliary stream must never stop the REST reconciliation path.
        authorization = getattr(self.client, "_authorization", None)
        if not callable(authorization):
            return
        stream = PrivateAccountStream(
            lambda: authorization(None),
            on_order=self._on_private_order,
            on_asset=self._on_private_asset,
            on_error=lambda message: self._emit("warning", message),
            on_status=self._on_private_status,
        )
        self._private_stream = stream
        stream.start()
        engine_thread = self._thread
        if engine_thread is not None:
            threading.Thread(
                target=self._watch_private_lifecycle,
                args=(engine_thread, stream),
                name="private-account-lifecycle",
                daemon=True,
            ).start()

    def _watch_private_lifecycle(
        self, engine_thread: threading.Thread, stream: PrivateAccountStream
    ) -> None:
        engine_thread.join()
        stream.stop()
        if self._private_stream is stream:
            self._private_stream = None

    def _on_private_status(self, payload: dict[str, Any]) -> None:
        state = str(payload.get("state") or "")
        previous = self._private_status_state
        self._private_status_state = state
        # Surface lifecycle transitions, but don't persist a log every periodic
        # ping/alive callback during normal private-stream silence.
        if state == "connected" and previous != "connected":
            self._emit(
                "private_account",
                "Private WS 연결 · myOrder/myAsset 보조 감시 시작",
                connected=True,
            )
        elif state == "stopped" and previous != "stopped":
            self.events.put(
                {
                    "type": "private_account",
                    "message": "Private WS 종료",
                    "connected": False,
                }
            )

    def _on_private_order(self, event: dict[str, Any]) -> None:
        self._private_order_events += 1
        identifier = str(event.get("identifier") or "").strip()
        state = str(event.get("state") or "").strip().lower()
        market = str(event.get("code") or "").strip().upper()

        # Ignore manual/third-party orders for state mutation. JunhyunBank order
        # intents always use this prefix, and only those identifiers can request
        # an accelerated REST reconciliation.
        if identifier.startswith("junhyunbank-"):
            with self._private_reconcile_lock:
                self._private_reconcile_requested.add(identifier)
            self.events.put(
                {
                    "type": "private_order",
                    "message": f"{market or '주문'} Private 이벤트 {state or '-'} · REST 체결확인 예약",
                    "market": market,
                    "state": state,
                    "identifier": identifier,
                }
            )

    def _on_private_asset(self, event: dict[str, Any]) -> None:
        self._private_asset_events += 1
        # myAsset is an anomaly/refresh signal only. In particular, never copy
        # account balance into Storage.managed_quantity; unrelated user holdings
        # must remain outside JunhyunBank automated management.
        assets = event.get("assets")
        count = len(assets) if isinstance(assets, list) else 0
        self.events.put(
            {
                "type": "private_asset",
                "message": "Private 자산 변동 감지 · 정기 REST 잔고와 교차확인",
                "asset_count": count,
            }
        )

    def _consume_private_reconcile_requests(self) -> set[str]:
        with self._private_reconcile_lock:
            requested = set(self._private_reconcile_requested)
            self._private_reconcile_requested.clear()
        return requested

    def _reconcile_orders(self) -> None:
        """Reconcile durable intents with event acceleration and REST fallback.

        ``myOrder`` events only wake this method up sooner. We still fetch the
        canonical order object by our client identifier and feed the unchanged
        atomic ``complete_order_intent`` path. This avoids building accounting
        state from a transient WebSocket event and keeps restart recovery valid
        even when the private stream is unavailable.
        """
        forced = self._consume_private_reconcile_requests()
        now = time.monotonic()
        pending = self.storage.pending_orders()
        live_identifiers = {str(row.get("identifier") or "") for row in pending}
        for stale in set(self._order_reconcile_retry_at) - live_identifiers:
            self._order_reconcile_retry_at.pop(stale, None)
            self._order_reconcile_failures.pop(stale, None)

        for intent in pending:
            if self._hard_stop.is_set():
                return
            identifier = str(intent.get("identifier") or "")
            if not identifier:
                continue
            if identifier not in forced and now < self._order_reconcile_retry_at.get(identifier, 0.0):
                continue
            try:
                detail = self.client.get_order(identifier=identifier)
                if self.storage.complete_order_intent(identifier, detail):
                    self.risk.report_api_success()
                    self._order_reconcile_retry_at.pop(identifier, None)
                    self._order_reconcile_failures.pop(identifier, None)
                    if (
                        intent["side"] == "SELL"
                        and intent["market"] not in self.storage.managed_markets()
                    ):
                        self.strategy.notify_exit(intent["market"])
                    self._emit(
                        "trade",
                        f"{intent['market']} {intent['side']} 주문 확인 완료 · 체결수량 {detail.get('executed_volume', '0')}",
                    )
                    continue

                # A valid but non-terminal response is normal immediately after
                # an IOC event. Poll again soon, while allowing another myOrder
                # event to override this delay.
                self.risk.report_api_success()
                self._order_reconcile_failures[identifier] = 0
                self._order_reconcile_retry_at[identifier] = time.monotonic() + 1.0
            except Exception as exc:
                # 404 can be transient just after an ambiguous POST. Repeated
                # lookup failures back off so pending intents cannot monopolize
                # the Exchange REST quota or stall the evaluation loop.
                self.risk.report_api_failure()
                failures = self._order_reconcile_failures.get(identifier, 0) + 1
                self._order_reconcile_failures[identifier] = failures
                delay = min(15.0, float(2 ** min(failures, 4)))
                self._order_reconcile_retry_at[identifier] = time.monotonic() + delay
                self._entry_status(
                    intent["market"],
                    f"주문 결과 확인 대기: {exc} · {delay:.0f}초 후 REST 재확인",
                )

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
