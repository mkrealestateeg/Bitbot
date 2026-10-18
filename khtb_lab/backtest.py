from .strategy import analyze


def run_backtest(
    df,
    trend_df,
    context_df,
    risk_reward=1.5,
    risk_per_trade=0.005,
):
    """Backtest strategy-provided structural exits with a minimum R:R filter."""
    if len(df) < 220:
        raise ValueError("Need at least 220 main timeframe candles")

    trades = []
    for index in range(200, len(df) - 1):
        timestamp = df.iloc[index]["timestamp"]
        signal = analyze(
            df.iloc[: index + 1].copy(),
            trend_df[trend_df["timestamp"] <= timestamp],
            context_df[context_df["timestamp"] <= timestamp],
        )
        if signal["direction"] == "NEUTRAL" or signal["score"] < 65:
            continue

        entry = float(df.iloc[index + 1]["open"])
        stop = float(signal["stop"])
        target = float(signal["target"])
        direction = 1 if signal["direction"] == "LONG" else -1
        risk = abs(entry - stop)
        if risk <= 0 or abs(target - entry) / risk < risk_reward:
            continue
        if (direction == 1 and not stop < entry < target) or (
            direction == -1 and not target < entry < stop
        ):
            continue

        exit_price = None
        reason = "END"
        for forward_index in range(
            index + 1, min(index + 51, len(df))
        ):
            candle = df.iloc[forward_index]
            if direction == 1 and candle["low"] <= stop:
                exit_price = stop
                reason = "SL"
                break
            if direction == -1 and candle["high"] >= stop:
                exit_price = stop
                reason = "SL"
                break
            if direction == 1 and candle["high"] >= target:
                exit_price = target
                reason = "TP"
                break
            if direction == -1 and candle["low"] <= target:
                exit_price = target
                reason = "TP"
                break

        if exit_price is None:
            exit_price = float(
                df.iloc[min(index + 50, len(df) - 1)]["close"]
            )

        multiple = (exit_price - entry) * direction / risk
        trades.append(
            {
                "index": index,
                "direction": signal["direction"],
                "entry": entry,
                "exit": exit_price,
                "reason": reason,
                "r_multiple": multiple,
                "return_on_risk": multiple * risk_per_trade,
            }
        )

    if not trades:
        return {
            "trades": [],
            "count": 0,
            "win_rate": 0.0,
            "avg_r": 0.0,
            "profit_factor": 0.0,
            "max_drawdown_r": 0.0,
        }

    curve = peak = drawdown = 0.0
    for trade in trades:
        curve += trade["r_multiple"]
        peak = max(peak, curve)
        drawdown = max(drawdown, peak - curve)

    wins = sum(
        trade["r_multiple"]
        for trade in trades
        if trade["r_multiple"] > 0
    )
    losses = abs(
        sum(
            trade["r_multiple"]
            for trade in trades
            if trade["r_multiple"] < 0
        )
    )
    return {
        "trades": trades,
        "count": len(trades),
        "win_rate": (
            sum(trade["r_multiple"] > 0 for trade in trades)
            / len(trades)
            * 100
        ),
        "avg_r": sum(trade["r_multiple"] for trade in trades) / len(trades),
        "profit_factor": wins / losses if losses else (999.0 if wins else 0.0),
        "max_drawdown_r": drawdown,
    }
