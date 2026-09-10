"""Read-only live strategy diagnosis; never loads credentials or submits orders."""
import argparse
from collections import Counter
import json
from pathlib import Path
import threading
import time

from junhyunbank.config import StrategyConfig
from junhyunbank.market_stream import MarketStream
from junhyunbank.runtime_engine import TradingEngine
from junhyunbank.strategy import MicroFlowStrategy
from junhyunbank.upbit import UpbitClient


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--seconds', type=float, default=300)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    client = UpbitClient()
    strategy = MicroFlowStrategy(StrategyConfig())
    lock = threading.RLock()
    errors = Counter()
    reasons = Counter()
    samples = {}
    def trade(event):
        with lock:
            strategy.on_trade(event)
    def book(event):
        with lock:
            strategy.on_orderbook(event)
    def error(message):
        errors[message] += 1
    rows = client.get_markets()
    markets = sorted(r['market'] for r in rows if r['market'].startswith('KRW-') and not TradingEngine._is_warning_market(r))
    streams = [MarketStream(markets[i:i+100], on_trade=trade, on_error=error) for i in range(0, len(markets), 100)]
    deep = None
    previous = []
    started = time.monotonic()
    evaluations = 0
    try:
        for stream in streams:
            stream.start()
        while time.monotonic() - started < args.seconds:
            with lock:
                ranked = strategy.rank_markets(markets, 24)
                selected = sorted(m for m, _ in ranked)
                if selected != previous:
                    if deep is None and selected:
                        deep = MarketStream(selected, on_orderbook=book, on_error=error)
                        deep.start()
                    elif deep and selected:
                        deep.update_markets(selected)
                    previous = selected
                for market in selected:
                    decision = strategy.evaluate_entry(market, bid_fee=0.0005, ask_fee=0.0005, health=0.65, regime_factor=0.75)
                    reasons[decision.reason] += 1
                    evaluations += 1
                    features = strategy._feature_set(market)
                    if features:
                        samples[market] = features
                warmed = sum(strategy.warmup_ratio(m) >= 1 for m in markets)
            print(json.dumps({'elapsed': round(time.monotonic()-started), 'markets': len(markets), 'warmed': warmed, 'candidates': len(selected), 'reasons': dict(reasons)}, ensure_ascii=True), flush=True)
            time.sleep(3)
    finally:
        for stream in streams + ([deep] if deep else []):
            stream.stop()
        client.close()
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps({'seconds': time.monotonic()-started, 'markets': len(markets), 'messages': sum(s.message_count for s in streams), 'evaluations': evaluations, 'reasons': dict(reasons), 'errors': dict(errors), 'last_features': samples, 'assumed_fee_each_side': 0.0005, 'orders_submitted': 0}, ensure_ascii=False, indent=2), encoding='utf-8')


if __name__ == '__main__':
    main()
