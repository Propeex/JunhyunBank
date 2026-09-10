from __future__ import annotations

import math
import statistics
from collections import defaultdict
from typing import Any, Iterable


def net_round_trip_return(
    entry_ask: float,
    future_bid: float,
    *,
    bid_fee: float,
    ask_fee: float,
) -> float | None:
    """Return executable top-of-book round-trip return after fees.

    This intentionally uses the entry *ask* and future *bid* rather than mid
    prices so spread is paid in the label. It is still size-free and therefore
    does not claim to model depth slippage or queue/transport latency.
    """
    values = (entry_ask, future_bid, bid_fee, ask_fee)
    if any(not math.isfinite(float(value)) for value in values):
        return None
    if entry_ask <= 0 or future_bid <= 0 or not (0 <= bid_fee < 1) or not (0 <= ask_fee < 1):
        return None
    return (future_bid * (1.0 - ask_fee)) / (entry_ask * (1.0 + bid_fee)) - 1.0


def absolute_mid_move(
    entry_bid: float,
    entry_ask: float,
    future_bid: float,
    future_ask: float,
) -> float | None:
    values = (entry_bid, entry_ask, future_bid, future_ask)
    if any(not math.isfinite(float(value)) or float(value) <= 0 for value in values):
        return None
    entry_mid = (entry_bid + entry_ask) / 2.0
    future_mid = (future_bid + future_ask) / 2.0
    if entry_mid <= 0:
        return None
    return abs(future_mid / entry_mid - 1.0)


def _mean(values: list[float]) -> float | None:
    return statistics.fmean(values) if values else None


def _median(values: list[float]) -> float | None:
    return statistics.median(values) if values else None


def _pearson(xs: list[float], ys: list[float]) -> float | None:
    if len(xs) < 3 or len(xs) != len(ys):
        return None
    mx = statistics.fmean(xs)
    my = statistics.fmean(ys)
    dx = [x - mx for x in xs]
    dy = [y - my for y in ys]
    denom = math.sqrt(sum(x * x for x in dx) * sum(y * y for y in dy))
    if denom <= 0:
        return None
    return sum(x * y for x, y in zip(dx, dy)) / denom


def _finite_float(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def summarize_labeled_rows(rows: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Summarize already-labeled shadow observations without fitting a model.

    Required row fields are ``net_return`` and ``absolute_mid_move``. Optional
    ``expected_move_pct`` is used only for calibration diagnostics. No threshold
    is optimized on the same data; this function is descriptive by design.
    """
    normalized: list[tuple[float, float, float | None]] = []
    for row in rows:
        net = _finite_float(row.get("net_return"))
        move = _finite_float(row.get("absolute_mid_move"))
        expected = _finite_float(row.get("expected_move_pct"))
        if net is None or move is None:
            continue
        normalized.append((net, move, expected if expected is not None and expected > 0 else None))

    net_values = [row[0] for row in normalized]
    moves = [row[1] for row in normalized]
    expected_pairs = [(row[2], row[1]) for row in normalized if row[2] is not None]
    expected_values = [float(pair[0]) for pair in expected_pairs]
    expected_moves = [pair[1] for pair in expected_pairs]
    calibration_ratios = [
        observed / expected
        for expected, observed in expected_pairs
        if expected > 0
    ]
    coverage = [observed <= expected for expected, observed in expected_pairs]

    return {
        "count": len(normalized),
        "mean_net_return": _mean(net_values),
        "median_net_return": _median(net_values),
        "positive_net_rate": (
            sum(value > 0 for value in net_values) / len(net_values)
            if net_values
            else None
        ),
        "mean_absolute_mid_move": _mean(moves),
        "median_absolute_mid_move": _median(moves),
        "mean_expected_move": _mean(expected_values),
        "expected_move_abs_correlation": _pearson(expected_values, expected_moves),
        "expected_move_coverage_rate": (
            sum(coverage) / len(coverage) if coverage else None
        ),
        "median_abs_to_expected_ratio": _median(calibration_ratios),
    }


def chronological_split(
    rows: Iterable[dict[str, Any]], fraction: float = 0.60
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    ordered = sorted(
        rows,
        key=lambda row: (
            float(row.get("created_epoch_ms") or 0.0),
            str(row.get("market") or ""),
        ),
    )
    if not ordered:
        return [], []
    fraction = max(0.05, min(0.95, float(fraction)))
    cut = max(1, min(len(ordered) - 1, int(len(ordered) * fraction))) if len(ordered) > 1 else 1
    return ordered[:cut], ordered[cut:]


def _sample_expected_move(sample: dict[str, Any]) -> Any:
    direct = _finite_float(sample.get("expected_move_pct"))
    if direct is not None and direct > 0:
        return direct
    features = sample.get("features")
    if isinstance(features, dict):
        feature_value = _finite_float(features.get("expected_move"))
        if feature_value is not None and feature_value > 0:
            return feature_value
    return None


def summarize_samples(
    samples: Iterable[dict[str, Any]],
    horizons: Iterable[int],
    *,
    split_fraction: float = 0.60,
) -> dict[str, Any]:
    """Build current-strategy forward-edge reports for each horizon.

    Samples are grouped as all observed candidate evaluations and BUY decisions.
    The chronological holdout section is not a trained model/OOS claim; it keeps
    the later observations separate so future calibration work can avoid using a
    single pooled statistic as if it were prospective evidence.
    """
    source = list(samples)
    train, holdout = chronological_split(source, split_fraction)

    def labeled(rows: list[dict[str, Any]], horizon: int, only_buy: bool) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        key = str(int(horizon))
        for sample in rows:
            if only_buy and str(sample.get("signal") or "").upper() != "BUY":
                continue
            labels = sample.get("labels") or {}
            label = labels.get(key) if isinstance(labels, dict) else None
            if not isinstance(label, dict):
                continue
            merged = dict(label)
            # Several HOLD branches intentionally return a compact decision and
            # do not repeat expected_move_pct. The feature snapshot is the
            # canonical value for ExpectedMove calibration across both BUY and
            # rejected candidate observations.
            merged["expected_move_pct"] = _sample_expected_move(sample)
            result.append(merged)
        return result

    report: dict[str, Any] = {
        "sample_count": len(source),
        "buy_sample_count": sum(
            str(sample.get("signal") or "").upper() == "BUY" for sample in source
        ),
        "chronological_split_fraction": split_fraction,
        "train_sample_count": len(train),
        "holdout_sample_count": len(holdout),
        "horizons": {},
    }
    for horizon in sorted({int(value) for value in horizons if int(value) > 0}):
        report["horizons"][str(horizon)] = {
            "all": summarize_labeled_rows(labeled(source, horizon, False)),
            "buy": summarize_labeled_rows(labeled(source, horizon, True)),
            "train_buy": summarize_labeled_rows(labeled(train, horizon, True)),
            "holdout_buy": summarize_labeled_rows(labeled(holdout, horizon, True)),
        }
    return report


def reason_counts(samples: Iterable[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = defaultdict(int)
    for sample in samples:
        counts[str(sample.get("reason") or "-")] += 1
    return dict(sorted(counts.items(), key=lambda item: (-item[1], item[0])))
