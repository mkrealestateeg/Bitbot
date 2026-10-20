from khtb_lab.database import Database
from khtb_lab.local_learning import (
    FEATURE_NAMES,
    MIN_SAMPLES,
    LocalTradeLearner,
    capture_entry_snapshot,
)


def _signal(direction, score=80):
    return {
        "direction": direction,
        "price": 100.0,
        "stop": 98.0 if direction == "LONG" else 102.0,
        "target": 104.0 if direction == "LONG" else 96.0,
        "score": score,
        "setup": "MOMENTUM",
        "snapshot": {
            "rsi": 65 if direction == "LONG" else 35,
            "macd": 1 if direction == "LONG" else -1,
            "macd_signal": 0.5 if direction == "LONG" else -0.5,
            "adx": 25,
            "atr": 2,
            "volume_ratio": 1.5,
            "trend_1h": "UP" if direction == "LONG" else "DOWN",
            "trend_4h": "UP" if direction == "LONG" else "DOWN",
        },
    }


def _record_trade(db, direction, pnl, source="PAPER"):
    signal = _signal(direction)
    trade_id = db.open_trade(
        "BTC/USDT",
        direction,
        signal["price"],
        signal["stop"],
        signal["target"],
        1,
        source=source,
        snapshot=capture_entry_snapshot(signal, source),
    )
    exit_price = 102 if pnl > 0 and direction == "LONG" else (
        98 if pnl > 0 else 99 if direction == "LONG" else 101
    )
    db.close_trade(trade_id, exit_price, pnl, "test")


def test_entry_snapshot_contains_versioned_features():
    snapshot = capture_entry_snapshot(_signal("LONG"), "PAPER")

    learning = snapshot["local_learning"]
    assert learning["schema_version"] == 1
    assert learning["source"] == "PAPER"
    assert learning["feature_names"] == list(FEATURE_NAMES)
    assert len(learning["features"]) == len(FEATURE_NAMES)
    assert snapshot["rsi"] == 65


def test_local_learner_waits_for_minimum_samples_and_balanced_classes(tmp_path):
    db = Database(str(tmp_path / "trades.db"))
    try:
        for _ in range(MIN_SAMPLES - 1):
            _record_trade(db, "LONG", 1)

        learner = LocalTradeLearner(db)
        state = learner.refresh("PAPER")

        assert state["status"] == "WARMING"
        assert state["samples"] == MIN_SAMPLES - 1
        assert learner.predict("PAPER", _signal("LONG")) is None

        _record_trade(db, "LONG", 1)
        state = learner.refresh("PAPER")
        assert state["status"] == "WARMING"
        assert state["samples"] == MIN_SAMPLES
        assert state["losses"] == 0
    finally:
        db.close()


def test_local_learner_excludes_break_even_trades(tmp_path):
    db = Database(str(tmp_path / "trades.db"))
    try:
        _record_trade(db, "LONG", 0)
        assert db.local_learning_examples("PAPER") == []
    finally:
        db.close()


def test_local_learner_trains_and_persists_separately_by_environment(
    tmp_path,
):
    db_path = str(tmp_path / "trades.db")
    db = Database(db_path)
    for _ in range(15):
        _record_trade(db, "LONG", 1, source="PAPER")
        _record_trade(db, "SHORT", -1, source="PAPER")

    learner = LocalTradeLearner(db)
    state = learner.refresh("PAPER")
    long_prediction = learner.predict("PAPER", _signal("LONG"))
    short_prediction = learner.predict("PAPER", _signal("SHORT"))
    assert state["status"] == "READY"
    assert long_prediction["advisory_only"] is True
    assert long_prediction["win_probability"] > short_prediction["win_probability"]
    assert learner.predict("DEMO", _signal("LONG")) is None
    db.close()

    reopened_db = Database(db_path)
    try:
        persisted = LocalTradeLearner(reopened_db).predict(
            "PAPER", _signal("LONG")
        )
        assert persisted == long_prediction
    finally:
        reopened_db.close()
