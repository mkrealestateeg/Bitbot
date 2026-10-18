import pandas as pd

import khtb_lab.backtest as backtest_module


def test_backtest_uses_strategy_target(monkeypatch):
    rows = [
        {
            "timestamp": index,
            "open": 100,
            "high": 100,
            "low": 100,
            "close": 100,
        }
        for index in range(220)
    ]
    rows[-1] = {
        "timestamp": 219,
        "open": 100,
        "high": 100,
        "low": 100,
        "close": 100,
    }
    rows[201]["high"] = 109
    rows[201]["low"] = 99
    rows[201]["close"] = 108
    frame = pd.DataFrame(rows)
    calls = 0

    def analyze_once(*args):
        nonlocal calls
        calls += 1
        if calls == 1:
            return {
                "direction": "LONG",
                "score": 75,
                "stop": 95,
                "target": 108,
            }
        return {"direction": "NEUTRAL", "score": 0}

    monkeypatch.setattr(
        backtest_module,
        "analyze",
        analyze_once,
    )

    result = backtest_module.run_backtest(frame, frame, frame)

    assert result["count"] == 1
    assert result["trades"][0]["reason"] == "TP"
    assert result["trades"][0]["exit"] == 108
