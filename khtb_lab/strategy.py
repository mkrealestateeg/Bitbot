
from __future__ import annotations

import math

from .indicators import enrich


MIN_TECH_SCORE = 65.0
MIN_DIRECTION_GAP = 10.0


def _number(value, default=None):
    """Convert a value to a finite float, otherwise return the default."""
    try:
        result = float(value)
        return result if math.isfinite(result) else default
    except (TypeError, ValueError):
        return default


def _row_value(row, name, default=None):
    return _number(row.get(name), default)


def _closed_row(df):
    """Prefer the last fully closed candle when available."""
    if len(df) >= 3:
        return df.iloc[-2]
    return df.iloc[-1]


def _candle_rejection(row, direction):
    """Check for a directional rejection wick."""
    o = _row_value(row, "open")
    h = _row_value(row, "high")
    l = _row_value(row, "low")
    c = _row_value(row, "close")

    if None in (o, h, l, c):
        return False

    body = max(abs(c - o), (h - l) * 0.05, 1e-12)
    upper_wick = h - max(o, c)
    lower_wick = min(o, c) - l

    if direction == "LONG":
        return c > o and lower_wick >= body * 0.8

    return c < o and upper_wick >= body * 0.8


def _make_candidate(direction, score, setup, reason, price, atr):
    """Build a candidate with independent stop-loss and take-profit levels."""
    if direction == "LONG":
        stop = price - 1.5 * atr
        target = price + 3.0 * atr
    else:
        stop = price + 1.5 * atr
        target = price - 3.0 * atr

    return {
        "direction": direction,
        "score": float(score),
        "price": float(price),
        "atr": float(atr),
        "stop": float(stop),
        "target": float(target),
        "setup": setup,
        "reason": reason,
    }


def _momentum_candidate(main, trend, context, price, atr):
    """Score the existing multi-timeframe momentum strategy."""
    score_long = 0.0
    score_short = 0.0
    reasons = []

    for row, weight, label in (
        (main, 25, "15m"),
        (trend, 25, "1h"),
        (context, 15, "4h"),
    ):
        close = _row_value(row, "close")
        ema20 = _row_value(row, "ema20")
        ema50 = _row_value(row, "ema50")

        if None not in (close, ema20, ema50):
            if close > ema20 > ema50:
                score_long += weight
            if close < ema20 < ema50:
                score_short += weight

            trend_name = "up" if close > ema50 else "down"
            reasons.append(f"{label}: EMA trend {trend_name}")

    rsi = _row_value(main, "rsi")
    if rsi is not None:
        if 50 <= rsi <= 70:
            score_long += 10
        if 30 <= rsi <= 50:
            score_short += 10

    macd = _row_value(main, "macd")
    macd_signal = _row_value(main, "macd_signal")
    if None not in (macd, macd_signal):
        if macd > macd_signal:
            score_long += 10
        elif macd < macd_signal:
            score_short += 10

    adx = _row_value(main, "adx")
    if adx is not None and adx >= 18:
        score_long += 5
        score_short += 5

    volume_ratio = _row_value(main, "volume_ratio")
    open_price = _row_value(main, "open")
    close_price = _row_value(main, "close")

    if (
        volume_ratio is not None
        and volume_ratio >= 1.2
        and open_price is not None
        and close_price is not None
    ):
        if close_price > open_price:
            score_long += 5
        elif close_price < open_price:
            score_short += 5

    if (
        score_long >= MIN_TECH_SCORE
        and score_long >= score_short + MIN_DIRECTION_GAP
    ):
        direction, score = "LONG", score_long
    elif (
        score_short >= MIN_TECH_SCORE
        and score_short >= score_long + MIN_DIRECTION_GAP
    ):
        direction, score = "SHORT", score_short
    else:
        return None, max(score_long, score_short), reasons

    return (
        _make_candidate(
            direction,
            score,
            "MOMENTUM",
            "; ".join(reasons),
            price,
            atr,
        ),
        max(score_long, score_short),
        reasons,
    )


def _range_candidates(main_df, main, price, atr):
    """Look for a rejection near a recent horizontal range boundary."""
    candidates = []

    # Exclude the currently forming candle and the signal candle.
    history = main_df.iloc[:-2].tail(40)
    if len(history) < 20:
        return candidates

    resistance = _number(history["high"].max())
    support = _number(history["low"].min())

    if resistance is None or support is None or resistance <= support:
        return candidates

    tolerance = 0.4 * atr

    if (
        abs(price - support) <= tolerance
        and _candle_rejection(main, "LONG")
    ):
        candidates.append(
            _make_candidate(
                "LONG",
                68,
                "HORIZONTAL_RANGE",
                (
                    f"15m support rejection near {support:.8g}; "
                    f"range resistance {resistance:.8g}"
                ),
                price,
                atr,
            )
        )

    if (
        abs(resistance - price) <= tolerance
        and _candle_rejection(main, "SHORT")
    ):
        candidates.append(
            _make_candidate(
                "SHORT",
                68,
                "HORIZONTAL_RANGE",
                (
                    f"15m resistance rejection near {resistance:.8g}; "
                    f"range support {support:.8g}"
                ),
                price,
                atr,
            )
        )

    return candidates


def _brr_candidates(main_df, trend_df, main, price, atr):
    """
    Look for a 15m retest of a recent 1h breakout level.

    A long setup requires a prior close above the 1h resistance,
    followed by a retest and bullish rejection.
    A short setup requires the inverse at 1h support.
    """
    candidates = []

    if len(main_df) < 10 or len(trend_df) < 20:
        return candidates

    # Use closed 1h candles to define the level.
    hourly = trend_df.iloc[:-2].tail(30)
    if len(hourly) < 15:
        return candidates

    resistance = _number(hourly["high"].max())
    support = _number(hourly["low"].min())

    if resistance is None or support is None or resistance <= support:
        return candidates

    # The signal candle is the last closed 15m candle.
    recent = main_df.iloc[:-2].tail(8)
    if len(recent) < 3:
        return candidates

    previous_closes = recent["close"].apply(_number)
    retest_tolerance = 0.35 * atr

    # Long: recent breakout above hourly resistance, then retest.
    breakout_long = any(
        close is not None and close > resistance
        for close in previous_closes.iloc[:-1]
    )
    low = _row_value(main, "low")

    if (
        breakout_long
        and low is not None
        and abs(low - resistance) <= retest_tolerance
        and price > resistance
        and _candle_rejection(main, "LONG")
    ):
        candidates.append(
            _make_candidate(
                "LONG",
                72,
                "BRR",
                f"15m bullish retest of 1h breakout level {resistance:.8g}",
                price,
                atr,
            )
        )

    # Short: recent breakdown below hourly support, then retest.
    breakout_short = any(
        close is not None and close < support
        for close in previous_closes.iloc[:-1]
    )
    high = _row_value(main, "high")

    if (
        breakout_short
        and high is not None
        and abs(high - support) <= retest_tolerance
        and price < support
        and _candle_rejection(main, "SHORT")
    ):
        candidates.append(
            _make_candidate(
                "SHORT",
                72,
                "BRR",
                f"15m bearish retest of 1h breakdown level {support:.8g}",
                price,
                atr,
            )
        )

    return candidates


def analyze(main_df, trend_df, context_df):
    """
    Analyze 15m, 1h and 4h market data.

    Returns the existing signal dictionary shape so app.py and
    backtest.py can continue consuming the result.
    """
    if (
        main_df is None
        or trend_df is None
        or context_df is None
        or len(main_df) < 3
        or len(trend_df) < 3
        or len(context_df) < 3
    ):
        return {
            "direction": "NEUTRAL",
            "score": 0.0,
            "price": 0.0,
            "setup": "NO_SETUP",
            "reason": "Insufficient candle data",
            "snapshot": {},
        }

    main_data = enrich(main_df)
    trend_data = enrich(trend_df)
    context_data = enrich(context_df)

    main = _closed_row(main_data)
    trend = _closed_row(trend_data)
    context = _closed_row(context_data)

    price = _row_value(main, "close", 0.0)
    atr = _row_value(main, "atr")

    if price <= 0:
        return {
            "direction": "NEUTRAL",
            "score": 0.0,
            "price": price,
            "setup": "NO_SETUP",
            "reason": "Invalid closing price",
            "snapshot": {},
        }

    if atr is None or atr <= 0:
        atr = price * 0.01

    candidates = []

    momentum, momentum_score, momentum_reasons = _momentum_candidate(
        main, trend, context, price, atr
    )
    if momentum is not None:
        candidates.append(momentum)

    candidates.extend(
        _range_candidates(main_data, main, price, atr)
    )
    candidates.extend(
        _brr_candidates(main_data, trend_data, main, price, atr)
    )

    snapshot = {
        "rsi": _row_value(main, "rsi"),
        "macd": _row_value(main, "macd"),
        "macd_signal": _row_value(main, "macd_signal"),
        "adx": _row_value(main, "adx"),
        "atr": atr,
        "volume_ratio": _row_value(main, "volume_ratio"),
        "ema20": _row_value(main, "ema20"),
        "ema50": _row_value(main, "ema50"),
        "trend_1h": (
            "UP"
            if _row_value(trend, "close", 0)
            > _row_value(trend, "ema50", 0)
            else "DOWN"
        ),
        "trend_4h": (
            "UP"
            if _row_value(context, "close", 0)
            > _row_value(context, "ema50", 0)
            else "DOWN"
        ),
    }

    if not candidates:
        return {
            "direction": "NEUTRAL",
            "score": float(momentum_score),
            "price": float(price),
            "atr": float(atr),
            "stop": float(price - 1.5 * atr),
            "target": float(price + 3.0 * atr),
            "setup": "NO_SETUP",
            "reason": (
                "No qualified setup. "
                + "; ".join(momentum_reasons)
            ),
            "snapshot": snapshot,
        }

    candidates.sort(key=lambda item: item["score"], reverse=True)
    best = candidates[0]

    # If equally scored strategies disagree, do not force a direction.
    tied = [
        item
        for item in candidates
        if item["score"] == best["score"]
    ]
    tied_directions = {item["direction"] for item in tied}

    if len(tied_directions) > 1:
        return {
            "direction": "NEUTRAL",
            "score": float(best["score"]),
            "price": float(price),
            "atr": float(atr),
            "stop": float(price - 1.5 * atr),
            "target": float(price + 3.0 * atr),
            "setup": "CONFLICTING_SETUPS",
            "reason": "Qualified strategies disagree on direction",
            "snapshot": snapshot,
        }

    best["snapshot"] = snapshot
    return best
