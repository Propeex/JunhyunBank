from __future__ import annotations

import math


def clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


class StrategyHealthGovernor:
    """최근 위험 정규화 실전 성과를 0~1 노출 배수로 변환한다."""

    def __init__(self, minimum_samples: int = 10) -> None:
        self.minimum_samples = minimum_samples

    def score(self, outcomes: list[float]) -> float:
        clean = [float(x) for x in outcomes if math.isfinite(float(x))]
        if not clean:
            return 0.65
        recent = clean[-50:]
        weights = list(range(1, len(recent) + 1))
        weighted_mean = sum(x * w for x, w in zip(recent, weights)) / sum(weights)
        win_rate = sum(1 for x in recent if x > 0) / len(recent)
        if len(recent) < self.minimum_samples:
            confidence = len(recent) / self.minimum_samples
            observed = clamp(0.55 + 0.25 * math.tanh(weighted_mean))
            return 0.65 * (1.0 - confidence) + observed * confidence
        health = 0.50 + 0.35 * math.tanh(weighted_mean) + 0.30 * (win_rate - 0.50)
        tail = recent[-20:]
        if len(tail) >= 12:
            tail_mean = sum(tail) / len(tail)
            tail_wins = sum(1 for x in tail if x > 0) / len(tail)
            if tail_mean < -0.45 and tail_wins < 0.30:
                return 0.0
            if tail_mean < -0.20 and tail_wins < 0.40:
                health *= 0.25
        return clamp(health)
