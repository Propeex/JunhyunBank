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
    if (
        entry_ask <= 0
        or future_bid <= 0
        or not (0 <= bid_fee < 1)
        or not (0 <= ask_fee < 1)
    ):
        return None
    return (future_bid * (1.0 - ask_fee)) / (
        entry_ask * (1.0 + bid_fee)
    ) - 1.0


def absolute_mid_move(
    entry_bid: float,
    entry_ask: float,
    future_bid: float,
    future_ask: float,
) -> float | None:
    values = (entry_bid, entry_ask, future_bid, future_ask)
    if any(
        not math.isfinite(float(value)) or float(value) <= 0
        for value in values
    ):
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


def _created_epoch_ms(row: dict[str, Any]) -> float:
    value = _finite_float(row.get("created_epoch_ms"))
    return value if value is not None else 0.0


def _ordered(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        rows,
        key=lambda row: (
            _created_epoch_ms(row),
            str(row.get("market") or ""),
            str(row.get("sample_id") or ""),
        ),
    )


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
        normalized.append(
            (
                net,
                move,
                expected if expected is not None and expected > 0 else None,
            )
        )

    net_values = [row[0] for row in normalized]
    moves = [row[1] for row in normalized]
    expected_pairs = [
        (row[2], row[1]) for row in normalized if row[2] is not None
    ]
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
        "expected_move_abs_correlation": _pearson(
            expected_values, expected_moves
        ),
        "expected_move_coverage_rate": (
            sum(coverage) / len(coverage) if coverage else None
        ),
        "median_abs_to_expected_ratio": _median(calibration_ratios),
    }


def chronological_split(
    rows: Iterable[dict[str, Any]], fraction: float = 0.60
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Chronologically split observations without shuffling.

    This generic helper does not purge overlapping forward labels. Use
    :func:`purged_chronological_split` for time-series validation metrics.
    """
    ordered = _ordered(rows)
    if not ordered:
        return [], []
    fraction = max(0.05, min(0.95, float(fraction)))
    cut = (
        max(1, min(len(ordered) - 1, int(len(ordered) * fraction)))
        if len(ordered) > 1
        else 1
    )
    return ordered[:cut], ordered[cut:]


def purged_chronological_split(
    rows: Iterable[dict[str, Any]],
    *,
    horizon_seconds: float,
    fraction: float = 0.60,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Chronological split with a forward-label purge gap.

    A training observation is removed when its label horizon reaches into the
    holdout period. This prevents a training label from using the same future
    price interval that belongs to the beginning of the holdout set.
    """
    train, holdout = chronological_split(rows, fraction)
    if not train or not holdout:
        return train, holdout
    horizon_ms = max(0.0, float(horizon_seconds)) * 1000.0
    holdout_start = _created_epoch_ms(holdout[0])
    purged_train = [
        row
        for row in train
        if _created_epoch_ms(row) + horizon_ms < holdout_start
    ]
    return purged_train, holdout


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


def _label_status(sample: dict[str, Any], horizon: int) -> str:
    labels = sample.get("labels") or {}
    label = labels.get(str(int(horizon))) if isinstance(labels, dict) else None
    if not isinstance(label, dict):
        return "pending"
    net = _finite_float(label.get("net_return"))
    move = _finite_float(label.get("absolute_mid_move"))
    if net is not None and move is not None:
        return "labeled"
    if str(label.get("status") or "").lower() == "missed":
        return "missed"
    return "invalid"


def summarize_samples(
    samples: Iterable[dict[str, Any]],
    horizons: Iterable[int],
    *,
    split_fraction: float = 0.60,
) -> dict[str, Any]:
    """Build descriptive forward-edge reports for the current strategy.

    BUY and all-candidate observations are reported separately. For each
    horizon, train/holdout statistics use a purge gap equal to that horizon so
    forward labels from the training set cannot overlap the beginning of the
    holdout period. No thresholds are fitted or optimized here.
    """
    source = _ordered(list(samples))
    raw_train, raw_holdout = chronological_split(source, split_fraction)

    def labeled(
        rows: list[dict[str, Any]], horizon: int, only_buy: bool
    ) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        key = str(int(horizon))
        for sample in rows:
            if (
                only_buy
                and str(sample.get("signal") or "").upper() != "BUY"
            ):
                continue
            labels = sample.get("labels") or {}
            label = labels.get(key) if isinstance(labels, dict) else None
            if not isinstance(label, dict):
                continue
            merged = dict(label)
            merged["expected_move_pct"] = _sample_expected_move(sample)
            result.append(merged)
        return result

    report: dict[str, Any] = {
        "sample_count": len(source),
        "buy_sample_count": sum(
            str(sample.get("signal") or "").upper() == "BUY"
            for sample in source
        ),
        "chronological_split_fraction": split_fraction,
        "raw_train_sample_count": len(raw_train),
        "raw_holdout_sample_count": len(raw_holdout),
        "horizons": {},
    }
    for horizon in sorted(
        {int(value) for value in horizons if int(value) > 0}
    ):
        train, holdout = purged_chronological_split(
            source,
            horizon_seconds=horizon,
            fraction=split_fraction,
        )
        statuses = [_label_status(sample, horizon) for sample in source]
        buy_source = [
            sample
            for sample in source
            if str(sample.get("signal") or "").upper() == "BUY"
        ]
        buy_statuses = [
            _label_status(sample, horizon) for sample in buy_source
        ]
        labeled_count = statuses.count("labeled")
        buy_labeled_count = buy_statuses.count("labeled")
        report["horizons"][str(horizon)] = {
            "sample_count": len(source),
            "labeled_sample_count": labeled_count,
            "missed_label_count": statuses.count("missed"),
            "pending_label_count": statuses.count("pending"),
            "invalid_label_count": statuses.count("invalid"),
            "label_completion_rate": (
                labeled_count / len(source) if source else None
            ),
            "buy_sample_count": len(buy_source),
            "buy_labeled_sample_count": buy_labeled_count,
            "buy_missed_label_count": buy_statuses.count("missed"),
            "buy_label_completion_rate": (
                buy_labeled_count / len(buy_source) if buy_source else None
            ),
            "purged_train_sample_count": len(train),
            "purged_from_train_count": max(0, len(raw_train) - len(train)),
            "holdout_sample_count": len(holdout),
            "all": summarize_labeled_rows(labeled(source, horizon, False)),
            "buy": summarize_labeled_rows(labeled(source, horizon, True)),
            "train_buy": summarize_labeled_rows(
                labeled(train, horizon, True)
            ),
            "holdout_buy": summarize_labeled_rows(
                labeled(holdout, horizon, True)
            ),
        }
    return report


def reason_counts(samples: Iterable[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = defaultdict(int)
    for sample in samples:
        counts[str(sample.get("reason") or "-")] += 1
    return dict(
        sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    )
