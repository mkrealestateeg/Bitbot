
from __future__ import annotations

import math

from .indicators import enrich


MIN_TECH_SCORE = 65.0
MIN_DIRECTION_GAP = 10.0
STRUCTURE_LOOKBACK = 40
STOP_BUFFER_ATR = 0.25
MIN_STOP_DISTANCE_ATR = 0.5
TARGET_BUFFER_ATR = 0.1
MIN_REWARD_RISK = 1.5


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


def _candle_pattern(main_df, direction):
    """Return a simple, deterministic confirmation pattern for the signal candle."""
    if len(main_df) < 3:
        return None

    candle = main_df.iloc[-2]
    if _candle_rejection(candle, direction):
        return f"{direction}_REJECTION"

    previous = main_df.iloc[-3]
    previous_open = _row_value(previous, "open")
    previous_close = _row_value(previous, "close")
    open_price = _row_value(candle, "open")
    close_price = _row_value(candle, "close")

    if None in (previous_open, previous_close, open_price, close_price):
        return None

    if direction == "LONG":
        engulfing = (
            previous_close < previous_open
            and close_price > open_price
            and open_price <= previous_close
            and close_price >= previous_open
        )
    else:
        engulfing = (
            previous_close > previous_open
            and close_price < open_price
            and open_price >= previous_close
            and close_price <= previous_open
        )

    return f"{direction}_ENGULFING" if engulfing else None


def _swing_levels(history, column, side):
    """Find recent confirmed pivots, retaining the range extreme as a fallback."""
    values = [_number(value) for value in history[column].tolist()]
    levels = []

    for index in range(2, len(values) - 2):
        value = values[index]
        window = values[index - 2 : index + 3]
        if value is None or any(item is None for item in window):
            continue
        if side == "low":
            is_pivot = value <= min(window) and value < max(window)
        else:
            is_pivot = value >= max(window) and value > min(window)
        if is_pivot:
            levels.append(value)

    valid_values = [value for value in values if value is not None]
    if valid_values:
        extreme = min(valid_values) if side == "low" else max(valid_values)
        levels.append(extreme)
    return sorted(set(levels))


def _structural_levels(main_df, direction, price, atr, candle_pattern):
    """Place exits beyond nearby chart structure and require room to the target."""
    history = main_df.iloc[:-2].tail(STRUCTURE_LOOKBACK)
    if len(history) < 20:
        return None

    supports = _swing_levels(history, "low", "low")
    resistances = _swing_levels(history, "high", "high")

    if candle_pattern:
        signal_candle = main_df.iloc[-2]
        pattern_level = _row_value(
            signal_candle,
            "low" if direction == "LONG" else "high",
        )
        if pattern_level is not None:
            if direction == "LONG":
                supports.append(pattern_level)
            else:
                resistances.append(pattern_level)

    if direction == "LONG":
        eligible_supports = [
            level for level in supports
            if level <= price - MIN_STOP_DISTANCE_ATR * atr
        ]
        if not eligible_supports:
            return None
        support = max(eligible_supports)
        stop = support - STOP_BUFFER_ATR * atr
        risk = price - stop
        eligible_targets = sorted(
            level - TARGET_BUFFER_ATR * atr
            for level in resistances
            if level - TARGET_BUFFER_ATR * atr
            >= price + MIN_REWARD_RISK * risk
        )
        if eligible_targets:
            target = eligible_targets[0]
            target_basis = "swing resistance"
        else:
            range_low = _number(history["low"].min())
            range_high = _number(history["high"].max())
            range_width = (
                range_high - range_low
                if range_low is not None and range_high is not None
                else 0
            )
            if range_high is None or price <= range_high or range_width <= 0:
                return None
            target = range_high + range_width - TARGET_BUFFER_ATR * atr
            if target < price + MIN_REWARD_RISK * risk:
                return None
            target_basis = "measured range projection"
        stop_basis = f"below swing low {support:.8g}"
    else:
        eligible_resistances = [
            level for level in resistances
            if level >= price + MIN_STOP_DISTANCE_ATR * atr
        ]
        if not eligible_resistances:
            return None
        resistance = min(eligible_resistances)
        stop = resistance + STOP_BUFFER_ATR * atr
        risk = stop - price
        eligible_targets = sorted(
            level + TARGET_BUFFER_ATR * atr
            for level in supports
            if level + TARGET_BUFFER_ATR * atr
            <= price - MIN_REWARD_RISK * risk
        )
        if eligible_targets:
            target = eligible_targets[-1]
            target_basis = "swing support"
        else:
            range_low = _number(history["low"].min())
            range_high = _number(history["high"].max())
            range_width = (
                range_high - range_low
                if range_low is not None and range_high is not None
                else 0
            )
            if range_low is None or price >= range_low or range_width <= 0:
                return None
            target = range_low - range_width + TARGET_BUFFER_ATR * atr
            if target > price - MIN_REWARD_RISK * risk:
                return None
            target_basis = "measured range projection"
        stop_basis = f"above swing high {resistance:.8g}"

    if risk <= 0:
        return None
    return {
        "stop": float(stop),
        "target": float(target),
        "risk_reward": float(abs(target - price) / risk),
        "stop_basis": stop_basis,
        "target_basis": target_basis,
    }


def _make_candidate(
    direction, score, setup, reason, price, atr, main_df, candle_pattern=None
):
    """Build a candidate from nearby swing structure with an ATR safety buffer."""
    levels = _structural_levels(
        main_df, direction, price, atr, candle_pattern
    )
    if levels is None:
        return None

    return {
        "direction": direction,
        "score": float(score),
        "price": float(price),
        "atr": float(atr),
        **levels,
        "candle_pattern": candle_pattern,
        "setup": setup,
        "reason": (
            f"{reason}; SL {levels['stop_basis']}; "
            f"TP {levels['target_basis']}; "
            f"R:R {levels['risk_reward']:.2f}"
        ),
    }


def _momentum_candidate(main_df, main, trend, context, price, atr):
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

    pattern = _candle_pattern(main_df, direction)
    candidate = _make_candidate(
        direction,
        score,
        "MOMENTUM",
        "; ".join(reasons),
        price,
        atr,
        main_df,
        pattern,
    )
    return candidate, max(score_long, score_short), reasons


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
        and _candle_pattern(main_df, "LONG")
    ):
        pattern = _candle_pattern(main_df, "LONG")
        candidate = _make_candidate(
            "LONG",
            68,
            "HORIZONTAL_RANGE",
            (
                f"15m support rejection near {support:.8g}; "
                f"range resistance {resistance:.8g}"
            ),
            price,
            atr,
            main_df,
            pattern,
        )
        if candidate is not None:
            candidates.append(candidate)

    if (
        abs(resistance - price) <= tolerance
        and _candle_pattern(main_df, "SHORT")
    ):
        pattern = _candle_pattern(main_df, "SHORT")
        candidate = _make_candidate(
            "SHORT",
            68,
            "HORIZONTAL_RANGE",
            (
                f"15m resistance rejection near {resistance:.8g}; "
                f"range support {support:.8g}"
            ),
            price,
            atr,
            main_df,
            pattern,
        )
        if candidate is not None:
            candidates.append(candidate)

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

    long_pattern = _candle_pattern(main_df, "LONG")
    if (
        breakout_long
        and low is not None
        and abs(low - resistance) <= retest_tolerance
        and price > resistance
        and long_pattern
    ):
        candidate = _make_candidate(
            "LONG",
            72,
            "BRR",
            f"15m bullish retest of 1h breakout level {resistance:.8g}",
            price,
            atr,
            main_df,
            long_pattern,
        )
        if candidate is not None:
            candidates.append(candidate)

    # Short: recent breakdown below hourly support, then retest.
    breakout_short = any(
        close is not None and close < support
        for close in previous_closes.iloc[:-1]
    )
    high = _row_value(main, "high")

    short_pattern = _candle_pattern(main_df, "SHORT")
    if (
        breakout_short
        and high is not None
        and abs(high - support) <= retest_tolerance
        and price < support
        and short_pattern
    ):
        candidate = _make_candidate(
            "SHORT",
            72,
            "BRR",
            f"15m bearish retest of 1h breakdown level {support:.8g}",
            price,
            atr,
            main_df,
            short_pattern,
        )
        if candidate is not None:
            candidates.append(candidate)

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
        main_data, main, trend, context, price, atr
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
