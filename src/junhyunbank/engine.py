from __future__ import annotations

import queue
import threading
import time
from dataclasses import dataclass
from typing import Any

from .config import AppConfig, TradingMode
from .market_stream import MarketStream
from .models import Position, Signal
from .risk import RiskManager
from .storage import Storage
from .strategy import TrendRSIStrategy
from .upbit import UpbitAPIError, UpbitClient


@dataclass(slots=True)
class PaperPortfolio:
    cash: float
    position: Position | None = None


class TradingEngine:
    def __init__(
        self,
        client: UpbitClient,
        config: AppConfig | None = None,
        storage: Storage | None = None,
    ) -> None:
        self.client = client
        self.config = config or AppConfig()
        self.storage = storage or Storage()
        self.strategy = TrendRSIStrategy(self.config.strategy)
        self.risk = RiskManager(self.config.risk)
        self.events: queue.Queue[dict[str, Any]] = queue.Queue()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._stream: MarketStream | None = None
        self._latest_prices: dict[str, float] = {}
        self._candidates: list[str] = []
        self._mode = TradingMode.PAPER
        self._paper = PaperPortfolio(self.config.paper_starting_cash)
        self._order_lock = threading.Lock()

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def start(self, mode: TradingMode) -> None:
        if self.running:
            return
        self._mode = mode
        self._stop.clear()
        if mode == TradingMode.PAPER:
            equity = self._paper_equity()
        else:
            equity, _, _ = self._live_portfolio()
        self.risk.reset_session(equity)
        self._thread = threading.Thread(target=self._run, name="trading-engine", daemon=True)
        self._thread.start()
        self._emit("status", f"{mode.value} 자동매매 시작")

    def request_stop(self) -> None:
        self._stop.set()
        self._emit("status", "정상 종료 요청")

    def emergency_stop(self) -> None:
        self.risk.trigger_emergency()
        self._stop.set()
        self._emit(
            "emergency",
            "긴급 정지: 신규 주문과 전략 실행을 즉시 차단했습니다. 기존 보유 자산은 자동 청산하지 않습니다.",
        )

    def drain_events(self, limit: int = 100) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for _ in range(limit):
            try:
                items.append(self.events.get_nowait())
            except queue.Empty:
                break
        return items

    def _emit(self, event_type: str, message: str, **payload: Any) -> None:
        event = {"type": event_type, "message": message, **payload}
        self.events.put(event)
        try:
            self.storage.event("INFO", event_type, message, payload or None)
        except Exception:
            pass

    def _on_price(self, market: str, price: float) -> None:
        self._latest_prices[market] = price
        self.events.put({"type": "price", "market": market, "price": price})

    def _restart_stream(self, markets: list[str]) -> None:
        if self._stream:
            self._stream.stop()
        self._stream = MarketStream(
            markets,
            on_price=self._on_price,
            on_error=lambda message: self._emit("warning", message),
        )
        self._stream.start()

    @staticmethod
    def _is_warning_market(row: dict[str, Any]) -> bool:
        event = row.get("market_event")
        if isinstance(event, dict) and bool(event.get("warning")):
            return True
        return str(row.get("market_warning", "")).upper() == "CAUTION"

    def _select_candidates(self) -> list[str]:
        market_rows = self.client.get_markets()
        allowed = {
            row["market"]
            for row in market_rows
            if str(row.get("market", "")).startswith("KRW-")
            and not self._is_warning_market(row)
        }
        tickers = self.client.get_all_krw_tickers()
        ranked = [
            row
            for row in tickers
            if row.get("market") in allowed
            and float(row.get("acc_trade_price_24h") or 0.0)
            >= self.config.strategy.min_24h_trade_value
        ]
        ranked.sort(
            key=lambda row: float(row.get("acc_trade_price_24h") or 0.0),
            reverse=True,
        )
        candidates = [str(row["market"]) for row in ranked[: self.config.strategy.candidate_count]]
        self._emit("candidates", "후보 종목 갱신", markets=candidates)
        return candidates

    def _run(self) -> None:
        next_candidate_refresh = 0.0
        try:
            while not self._stop.is_set():
                now = time.monotonic()
                if now >= next_candidate_refresh or not self._candidates:
                    self._candidates = self._select_candidates()
                    self._restart_stream(self._candidates)
                    next_candidate_refresh = now + self.config.strategy.candidate_refresh_seconds

                self._evaluate_cycle()
                self._publish_portfolio()
                self._stop.wait(self.config.strategy.evaluation_seconds)
        except Exception as exc:
            self._emit("error", f"자동매매 엔진 중단: {exc}")
        finally:
            if self._stream:
                self._stream.stop()
                self._stream = None
            self._emit("stopped", "자동매매 엔진 종료")

    def _evaluate_cycle(self) -> None:
        decisions: dict[str, Any] = {}
        evaluation_markets = list(self._candidates)
        if self._mode == TradingMode.LIVE:
            for market in sorted(self.storage.managed_markets()):
                if market not in evaluation_markets:
                    evaluation_markets.append(market)

        for market in evaluation_markets:
            if self._stop.is_set():
                return
            try:
                candles = self.client.get_minute_candles(
                    market,
                    unit=self.config.strategy.candle_unit,
                    count=self.config.strategy.candle_count,
                )
                decisions[market] = self.strategy.evaluate(candles)
            except UpbitAPIError as exc:
                self._emit("warning", f"{market} 분석 실패: {exc}")
            time.sleep(0.12)

        if self._mode == TradingMode.PAPER:
            self._trade_paper(decisions)
        else:
            self._trade_live(decisions)

    def _paper_equity(self) -> float:
        value = self._paper.cash
        if self._paper.position:
            price = self._latest_prices.get(
                self._paper.position.market, self._paper.position.avg_price
            )
            value += self._paper.position.market_value(price)
        return value

    def _trade_paper(self, decisions: dict[str, Any]) -> None:
        if self._stop.is_set() or self.risk.emergency:
            return
        position = self._paper.position
        if position:
            price = self._latest_prices.get(position.market)
            if not price:
                return
            risk_exit = self.risk.exit_reason(avg_price=position.avg_price, current_price=price)
            decision = decisions.get(position.market)
            strategy_exit = bool(decision and decision.signal == Signal.SELL)
            if risk_exit or strategy_exit:
                reason = risk_exit or decision.reason
                proceeds = position.quantity * price * (1.0 - self.config.risk.fee_rate)
                quantity = position.quantity
                self._paper.cash += proceeds
                self._paper.position = None
                self.storage.trade(
                    mode=self._mode.value,
                    market=position.market,
                    side="SELL",
                    quantity=quantity,
                    amount_krw=proceeds,
                    price=price,
                    reason=reason,
                )
                self._emit("trade", f"[PAPER] {position.market} 매도: {reason}")
            return

        buys = [
            (market, decision)
            for market, decision in decisions.items()
            if decision.signal == Signal.BUY
        ]
        if not buys:
            return
        market, decision = max(buys, key=lambda item: item[1].score)
        price = self._latest_prices.get(market)
        if not price:
            tickers = self.client.get_tickers([market])
            if not tickers:
                return
            price = float(tickers[0]["trade_price"])
        equity = self._paper_equity()
        check = self.risk.can_open(
            current_equity=equity,
            available_cash=self._paper.cash,
            open_position_count=0,
        )
        if not check.allowed:
            self._emit("risk", f"매수 차단: {check.reason}")
            return

        amount = self.config.risk.order_krw
        fee = amount * self.config.risk.fee_rate
        quantity = amount / price
        self._paper.cash -= amount + fee
        self._paper.position = Position(market, quantity, price)
        self.storage.trade(
            mode=self._mode.value,
            market=market,
            side="BUY",
            quantity=quantity,
            amount_krw=amount,
            price=price,
            reason=decision.reason,
        )
        self._emit("trade", f"[PAPER] {market} 매수: {decision.reason}")

    def _live_portfolio(self) -> tuple[float, float, list[Position]]:
        accounts = self.client.get_accounts()
        cash = 0.0
        positions: list[Position] = []
        for row in accounts:
            currency = str(row.get("currency", ""))
            balance = float(row.get("balance") or 0.0)
            locked = float(row.get("locked") or 0.0)
            total = balance + locked
            if currency == "KRW":
                cash = total
                continue
            if total <= 0:
                continue
            unit = str(row.get("unit_currency") or "KRW")
            if unit != "KRW":
                continue
            positions.append(
                Position(
                    market=f"KRW-{currency}",
                    quantity=balance,
                    avg_price=float(row.get("avg_buy_price") or 0.0),
                )
            )
        markets = [p.market for p in positions]
        prices = dict(self._latest_prices)
        missing = [m for m in markets if m not in prices]
        if missing:
            for row in self.client.get_tickers(missing):
                market = str(row["market"])
                price = float(row["trade_price"])
                prices[market] = price
                self._latest_prices[market] = price
        equity = cash + sum(p.market_value(prices.get(p.market, p.avg_price)) for p in positions)
        return equity, cash, positions

    def _trade_live(self, decisions: dict[str, Any]) -> None:
        if self._stop.is_set() or self.risk.emergency:
            return

        with self._order_lock:
            equity, cash, positions = self._live_portfolio()
            positions_by_market = {p.market: p for p in positions}
            managed = self.storage.managed_markets()

            # Clean up persisted state only when the exchange confirms the asset is gone.
            for market in managed - set(positions_by_market):
                self.storage.unmark_managed_position(market)
            managed &= set(positions_by_market)

            # Never auto-sell assets that were not opened by JunhyunBank.
            for market in sorted(managed):
                if self._stop.is_set() or self.risk.emergency:
                    return
                position = positions_by_market[market]
                price = self._latest_prices.get(position.market)
                if not price or position.quantity <= 0:
                    continue
                risk_exit = self.risk.exit_reason(
                    avg_price=position.avg_price, current_price=price
                )
                decision = decisions.get(position.market)
                strategy_exit = bool(decision and decision.signal == Signal.SELL)
                if risk_exit or strategy_exit:
                    reason = risk_exit or decision.reason
                    if self._stop.is_set() or self.risk.emergency:
                        return
                    order = self.client.place_market_sell(position.market, position.quantity)
                    self.storage.trade(
                        mode=self._mode.value,
                        market=position.market,
                        side="SELL",
                        quantity=position.quantity,
                        amount_krw=None,
                        price=price,
                        reason=reason,
                        exchange_order_id=order.get("uuid"),
                    )
                    self._emit("trade", f"[LIVE] {position.market} 매도 주문: {reason}")
                    return

            held = set(positions_by_market)
            buys = [
                (market, decision)
                for market, decision in decisions.items()
                if decision.signal == Signal.BUY and market not in held
            ]
            if not buys:
                return
            market, decision = max(buys, key=lambda item: item[1].score)
            check = self.risk.can_open(
                current_equity=equity,
                available_cash=cash,
                open_position_count=len(managed),
            )
            if not check.allowed:
                self._emit("risk", f"매수 차단: {check.reason}")
                return
            if self._stop.is_set() or self.risk.emergency:
                return
            order = self.client.place_market_buy(market, self.config.risk.order_krw)
            self.storage.mark_managed_position(market)
            self.storage.trade(
                mode=self._mode.value,
                market=market,
                side="BUY",
                quantity=None,
                amount_krw=self.config.risk.order_krw,
                price=self._latest_prices.get(market),
                reason=decision.reason,
                exchange_order_id=order.get("uuid"),
            )
            self._emit("trade", f"[LIVE] {market} 매수 주문: {decision.reason}")

    def _publish_portfolio(self) -> None:
        if self._mode == TradingMode.PAPER:
            position = self._paper.position
            rows = []
            if position:
                price = self._latest_prices.get(position.market, position.avg_price)
                rows.append(
                    {
                        "market": position.market,
                        "quantity": position.quantity,
                        "avg_price": position.avg_price,
                        "current_price": price,
                        "value": position.market_value(price),
                    }
                )
            equity = self._paper_equity()
            cash = self._paper.cash
        else:
            equity, cash, positions = self._live_portfolio()
            rows = []
            for position in positions:
                price = self._latest_prices.get(position.market, position.avg_price)
                rows.append(
                    {
                        "market": position.market,
                        "quantity": position.quantity,
                        "avg_price": position.avg_price,
                        "current_price": price,
                        "value": position.market_value(price),
                    }
                )
        start = self.risk.daily_start_equity or equity
        pnl_pct = (equity / start - 1.0) * 100.0 if start else 0.0
        self.events.put(
            {
                "type": "portfolio",
                "equity": equity,
                "cash": cash,
                "pnl_pct": pnl_pct,
                "positions": rows,
            }
        )
