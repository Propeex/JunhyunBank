from __future__ import annotations

import time

from .execution_model import (
    best_ioc_round_trip_capacity_krw,
    simulate_best_ioc_buy,
    simulate_best_ioc_sell,
)
from .runtime_engine import TradingEngine as RuntimeTradingEngine
from .strategy import BookSnapshot


class TradingEngine(RuntimeTradingEngine):
    """Production engine with exchange-order semantics kept explicit.

    Besides the Upbit Best+IOC execution model, this layer keeps the scanner and
    the actual entry freshness gate aligned. A market whose last trade is already
    too old to pass ``SafetyConfig.market_data_stale_seconds`` must not occupy a
    scarce deep-orderbook slot merely because an older HotScore is still high.
    Managed positions are intentionally exempt so their exit monitoring is never
    removed by the entry-candidate freshness filter.
    """

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._actionable_ranked: tuple[tuple[str, float], ...] = ()
        self._candidate_stale_skipped = 0
        self._candidate_supplemented = 0

    def start(self) -> None:
        self._actionable_ranked = ()
        self._candidate_stale_skipped = 0
        self._candidate_supplemented = 0
        super().start()

    def _select_deep_markets(
        self, ranked: list[tuple[str, float]]
    ) -> list[str]:
        """Use only entry-fresh scanner candidates for scarce deep slots.

        ``MicroFlowStrategy.hot_score`` deliberately tolerates a somewhat wider
        trade-age window for scanner stability. The live entry gate is stricter
        (normally 3 seconds). Previously the top scanner list could therefore be
        filled with markets that were guaranteed to fail moments later with
        ``체결/호가 수신 대기 또는 3초 이상 지연``. Worse, the 30-second deep
        residency rule could keep those stale markets subscribed while fresher
        markets below the top-N cutoff never received orderbook analysis.

        We first remove candidates that cannot pass the live freshness gate, then
        fill the resulting holes from fresh markets outside the truncated scanner
        top-N. The strategy score/quality/cost thresholds themselves are not
        relaxed. Existing managed positions remain in the deep set regardless of
        entry freshness because exits must continue to be monitored.
        """
        now = time.monotonic()
        stale_limit = max(0.1, float(self.config.safety.market_data_stale_seconds))
        scanner_limit = max(1, int(self.config.strategy.scanner_candidate_count))
        deep_limit = max(1, int(self.config.strategy.deep_candidate_count))
        managed = set(self.storage.managed_markets())
        supplied = {market for market, _ in ranked}

        actionable: list[tuple[str, float]] = []
        stale_skipped = 0
        for market, score in ranked:
            # Managed positions are exit-monitoring targets, not entry slots.
            if market in managed:
                continue
            if self.strategy.trade_age(market) <= stale_limit:
                actionable.append((market, score))
            else:
                stale_skipped += 1

        supplemented = 0
        if len(actionable) < scanner_limit:
            extras: list[tuple[str, float]] = []
            for market in self._allowed_markets:
                if market in supplied or market in managed:
                    continue
                if self.strategy.trade_age(market) > stale_limit:
                    continue
                score = float(self.strategy.hot_score(market))
                if score > 0.0:
                    extras.append((market, score))
            extras.sort(key=lambda item: item[1], reverse=True)
            need = scanner_limit - len(actionable)
            chosen = extras[:need]
            actionable.extend(chosen)
            supplemented = len(chosen)

        actionable.sort(key=lambda item: item[1], reverse=True)
        deduped: list[tuple[str, float]] = []
        seen: set[str] = set()
        for market, score in actionable:
            if market in seen:
                continue
            seen.add(market)
            deduped.append((market, score))
            if len(deduped) >= scanner_limit:
                break

        self._actionable_ranked = tuple(deduped)
        self._candidate_stale_skipped = stale_skipped
        self._candidate_supplemented = supplemented

        desired = [
            market
            for market, _ in deduped[:deep_limit]
            if market in self._allowed_markets
        ]
        scores = dict(deduped)
        selected: list[str] = sorted(managed)

        # Preserve anti-churn residency only while an entry candidate is still
        # fresh enough to be actionable. Stale non-managed candidates are
        # deliberately not carried forward, even if their 30-second residency
        # has not elapsed.
        for market in self._deep_markets:
            if market in managed or market not in self._allowed_markets:
                continue
            if self.strategy.trade_age(market) > stale_limit:
                continue
            age = now - self._deep_entered_at.get(market, now)
            if market in desired or age < self.config.strategy.deep_min_residency_seconds:
                selected.append(market)

        def nonmanaged_count() -> int:
            return sum(1 for market in selected if market not in managed)

        for market in desired:
            if market in selected:
                continue
            if nonmanaged_count() < deep_limit:
                selected.append(market)
                continue
            replaceable = [
                current
                for current in selected
                if current not in managed
                and current not in desired
                and now - self._deep_entered_at.get(current, now)
                >= self.config.strategy.deep_min_residency_seconds
            ]
            if not replaceable:
                continue
            weakest = min(replaceable, key=lambda current: scores.get(current, 0.0))
            if (
                scores.get(market, 0.0)
                >= scores.get(weakest, 0.0) + self.config.strategy.deep_switch_margin
            ):
                selected.remove(weakest)
                selected.append(market)

        if (
            not deduped
            and now - self._run_started_at
            >= self.config.strategy.no_candidate_warning_seconds
        ):
            if now - self._last_no_candidate_warning >= 60.0:
                self._last_no_candidate_warning = now
                detail = (
                    f" · 상위 HotScore stale 제외 {stale_skipped}개"
                    if stale_skipped
                    else ""
                )
                self._emit(
                    "warning",
                    "5분 이상 실제 진입 가능한 신선 후보가 없습니다. "
                    "Trade WS/워밍업은 살아 있어도 3초 신선도 조건을 통과할 "
                    f"시장을 찾지 못한 상태입니다{detail}.",
                )

        return sorted(dict.fromkeys(selected))

    def _publish_runtime_health(self, ranked: list[tuple[str, float]]) -> None:
        # The UI's candidate count should describe markets that can actually
        # advance to deep entry analysis, not stale scanner-only remnants.
        super()._publish_runtime_health(list(self._actionable_ranked))

    def drain_events(self, limit: int = 200) -> list[dict]:
        """Rewrite candidate UI events to the same actionable set used live."""
        items = super().drain_events(limit)
        top = list(self._actionable_ranked[:12])
        for event in items:
            if event.get("type") != "candidates":
                continue
            event["markets"] = [market for market, _ in top]
            event["scores"] = {market: score for market, score in top}
            event["prices"] = {
                market: self._latest_prices.get(market) for market, _ in top
            }
            event["tracked"] = len(self._latest_prices)
            event["stale_skipped"] = self._candidate_stale_skipped
            event["supplemented"] = self._candidate_supplemented
        return items

    @staticmethod
    def _simulate_buy_slippage(
        book: BookSnapshot, amount_krw: float
    ) -> tuple[float, float]:
        return simulate_best_ioc_buy(book, amount_krw)

    @staticmethod
    def _simulate_sell_slippage(
        book: BookSnapshot, amount_krw: float
    ) -> tuple[float, float]:
        return simulate_best_ioc_sell(book, amount_krw)

    @staticmethod
    def _liquidity_capacity(
        book: BookSnapshot,
        *,
        expected_move_pct: float,
        bid_fee: float,
        ask_fee: float,
    ) -> float:
        # Expected edge can decide whether a trade is worth attempting, but it
        # cannot make a best+IOC order reach worse price levels. Keep the legacy
        # signature because `_evaluate_cycle()` calls it through polymorphism.
        _ = expected_move_pct, bid_fee, ask_fee
        return best_ioc_round_trip_capacity_krw(book)
