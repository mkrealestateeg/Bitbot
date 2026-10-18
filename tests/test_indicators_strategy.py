import pandas as pd
import numpy as np
from khtb_lab.indicators import enrich
from khtb_lab.strategy import _candle_pattern, _make_candidate

def test_indicators_add_expected_columns():
    n=260; close=np.linspace(1,3,n)
    df=pd.DataFrame({'open':close,'high':close*1.01,'low':close*.99,'close':close,'volume':np.ones(n)*100})
    result=enrich(df)
    for col in ['ema20','ema50','ema200','rsi','macd','atr','adx','volume_ratio','swing_low','swing_high']:
        assert col in result.columns
    assert result['ema20'].iloc[-1] > result['ema50'].iloc[-1]


def _structure_frame():
    rows = [
        {"open": 100, "high": 102, "low": 98, "close": 100, "volume": 1}
        for _ in range(45)
    ]
    rows[10]["low"] = 90
    rows[30]["high"] = 120
    rows[-2] = {
        "open": 100,
        "high": 103,
        "low": 97,
        "close": 100,
        "volume": 1,
    }
    return pd.DataFrame(rows)


def test_structural_levels_place_long_stop_below_swing_low_and_target_at_resistance():
    frame = _structure_frame()

    candidate = _make_candidate(
        "LONG", 75, "TEST", "test setup", 100, 2, frame
    )

    assert candidate["stop"] == 89.5
    assert candidate["target"] == 119.8
    assert candidate["risk_reward"] >= 1.5
    assert candidate["stop_basis"] == "below swing low 90"
    assert candidate["target_basis"] == "swing resistance"


def test_structural_levels_place_short_stop_above_swing_high_and_target_at_support():
    frame = _structure_frame()
    frame.loc[10, "high"] = 110
    frame.loc[30, "low"] = 80

    candidate = _make_candidate(
        "SHORT", 75, "TEST", "test setup", 100, 2, frame
    )

    assert candidate["stop"] == 110.5
    assert candidate["target"] == 80.2
    assert candidate["risk_reward"] >= 1.5
    assert candidate["stop_basis"] == "above swing high 110"
    assert candidate["target_basis"] == "swing support"


def test_structural_levels_reject_trade_without_enough_target_room():
    rows = [
        {"open": 100, "high": 102, "low": 98, "close": 100, "volume": 1}
        for _ in range(45)
    ]
    frame = pd.DataFrame(rows)

    candidate = _make_candidate(
        "LONG", 75, "TEST", "test setup", 100, 1, frame
    )

    assert candidate is None


def test_candle_pattern_recognizes_bullish_engulfing():
    frame = pd.DataFrame(
        [
            {"open": 101, "high": 102, "low": 98, "close": 99},
            {"open": 98, "high": 103, "low": 97, "close": 102},
            {"open": 102, "high": 104, "low": 101, "close": 103},
        ]
    )

    assert _candle_pattern(frame, "LONG") == "LONG_ENGULFING"
