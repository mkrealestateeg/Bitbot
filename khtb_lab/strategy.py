from __future__ import annotations
from .indicators import enrich


def _near(a, b, tolerance):
    return abs(a - b) <= max(tolerance, abs(b) * 0.0025)


def _levels(df):
    x = enrich(df)
    # Use completed candles only for structural levels.
    if len(x) < 30:
        return None, None
    recent = x.iloc[-21:-1]
    return float(recent["swing_low"].min()), float(recent["swing_high"].max())


def _rejection(row, level, direction, atr):
    if direction == "LONG":
        return row.low <= level + 0.35 * atr and row.close > level and row.close > row.open
    return row.high >= level - 0.35 * atr and row.close < level and row.close < row.open


def analyze(main_df, trend_df, context_df):
    """Deterministic multi-timeframe strategy with Momentum, Horizontal Range and BRR setups.

    Entry timeframe: 15m. Structural levels: 1h. Context: 4h.
    BRR uses a completed 1h break followed by a first 15m retest/rejection.
    Gemini, when enabled, is commentary only and never changes this result.
    """
    m = enrich(main_df); t = enrich(trend_df); c = enrich(context_df)
    if len(m) < 30 or len(t) < 30 or len(c) < 30:
        return {"direction": "NEUTRAL", "score": 0, "reason": "Insufficient indicator data", "price": float(m.iloc[-1].close)}

    a = m.iloc[-2]; prev = m.iloc[-3]
    trend = t.iloc[-2]; trend_prev = t.iloc[-3]
    ctx = c.iloc[-2]
    price = float(a.close)
    atr = float(a.atr) if a.atr == a.atr and a.atr > 0 else price * 0.01

    score_long = score_short = 0.0
    reasons = []

    # 1) Existing multi-timeframe momentum core.
    for row, weight, label in [(a, 25, "15m"), (trend, 25, "1h"), (ctx, 15, "4h")]:
        if row.close > row.ema20 > row.ema50:
            score_long += weight
        if row.close < row.ema20 < row.ema50:
            score_short += weight
        reasons.append(f"{label}: EMA {'up' if row.close > row.ema50 else 'down'}")

    if a.rsi == a.rsi:
        if 50 <= a.rsi <= 70: score_long += 10
        if 30 <= a.rsi <= 50: score_short += 10
    if a.macd > a.macd_signal: score_long += 10
    if a.macd < a.macd_signal: score_short += 10
    if a.adx == a.adx and a.adx >= 18: score_long += 5; score_short += 5
    if a.volume_ratio == a.volume_ratio and a.volume_ratio >= 1.2:
        score_long += 5 if a.close > a.open else 0
        score_short += 5 if a.close < a.open else 0

    support, resistance = _levels(trend_df)
    setup = "MOMENTUM"
    structure_bonus_long = structure_bonus_short = 0.0

    # 2) Horizontal range / repeated high-low behavior.
    if support is not None and resistance is not None and resistance > support:
        range_width = resistance - support
        near_support = _near(price, support, 0.45 * atr)
        near_resistance = _near(price, resistance, 0.45 * atr)
        if near_support and _rejection(a, support, "LONG", atr):
            structure_bonus_long = 12
            setup = "HORIZONTAL_RANGE_LONG"
            reasons.append("1h range support + 15m rejection")
        if near_resistance and _rejection(a, resistance, "SHORT", atr):
            structure_bonus_short = 12
            setup = "HORIZONTAL_RANGE_SHORT"
            reasons.append("1h range resistance + 15m rejection")

        # Keep targets inside the range when the opposite level is too close.
        range_target_long = resistance
        range_target_short = support
    else:
        range_target_long = range_target_short = None

    score_long += structure_bonus_long
    score_short += structure_bonus_short

    # 3) BRR: completed 1h break, then first 15m retest/rejection.
    brr_long = brr_short = 0
    if resistance is not None and support is not None:
        # Break above the prior 1h resistance, then retest it from above.
        broke_up = trend_prev.close <= resistance and trend.close > resistance
        broke_down = trend_prev.close >= support and trend.close < support
        if broke_up and _rejection(a, resistance, "LONG", atr) and prev.low > resistance - 0.55 * atr:
            brr_long = 18
            setup = "BRR_LONG"
            reasons.append("1h resistance breakout + first 15m retest/rejection")
        if broke_down and _rejection(a, support, "SHORT", atr) and prev.high < support + 0.55 * atr:
            brr_short = 18
            setup = "BRR_SHORT"
            reasons.append("1h support breakdown + first 15m retest/rejection")
    score_long += brr_long
    score_short += brr_short

    direction = "LONG" if score_long >= 65 and score_long >= score_short + 10 else (
        "SHORT" if score_short >= 65 and score_short >= score_long + 10 else "NEUTRAL"
    )
    score = max(score_long, score_short)

    if direction == "LONG":
        stop = price - 1.5 * atr
        risk = price - stop
        candidates = [price + 1.5 * risk, price + 2.0 * risk]
        if setup == "HORIZONTAL_RANGE_LONG" and range_target_long and range_target_long > price:
            candidates.append(range_target_long)
        elif resistance and resistance > price:
            candidates.append(resistance)
        # Prefer a structural target when it lies between 1.5R and 2R; otherwise use 2R.
        structural = [x for x in candidates[2:] if 1.5 * risk <= x - price <= 2.0 * risk]
        target = min(structural) if structural else price + 2.0 * risk
    elif direction == "SHORT":
        stop = price + 1.5 * atr
        risk = stop - price
        candidates = [price - 1.5 * risk, price - 2.0 * risk]
        if setup == "HORIZONTAL_RANGE_SHORT" and range_target_short and range_target_short < price:
            candidates.append(range_target_short)
        elif support and support < price:
            candidates.append(support)
        structural = [x for x in candidates[2:] if 1.5 * risk <= price - x <= 2.0 * risk]
        target = max(structural) if structural else price - 2.0 * risk
    else:
        stop = price - 1.5 * atr
        target = price + 3.0 * atr

    return {
        "direction": direction,
        "score": float(score),
        "price": price,
        "atr": atr,
        "stop": float(stop),
        "target": float(target),
        "setup": setup if score >= 65 else "NO_SETUP",
        "reason": "; ".join(reasons),
        "snapshot": {
            "rsi": float(a.rsi) if a.rsi == a.rsi else None,
            "macd": float(a.macd) if a.macd == a.macd else None,
            "macd_signal": float(a.macd_signal) if a.macd_signal == a.macd_signal else None,
            "adx": float(a.adx) if a.adx == a.adx else None,
            "atr": atr,
            "volume_ratio": float(a.volume_ratio) if a.volume_ratio == a.volume_ratio else None,
            "ema20": float(a.ema20), "ema50": float(a.ema50), "ema200": float(a.ema200),
            "trend_1h": "UP" if trend.close > trend.ema50 else "DOWN",
            "trend_4h": "UP" if ctx.close > ctx.ema50 else "DOWN",
            "support_1h": support, "resistance_1h": resistance,
            "brr_long": bool(brr_long), "brr_short": bool(brr_short),
        },
    }
