from types import SimpleNamespace

import pandas as pd

from khtb_lab.app import KhtbLab
from khtb_lab.database import Database
from khtb_lab.indicators import enrich


def _candles(monotonic_lows=False):
    lows = (
        [90 + index for index in range(13)]
        if monotonic_lows
        else [99, 100, 98, 101, 102, 100, 103, 104, 102, 105, 106, 107, 108]
    )
    return pd.DataFrame({
        "timestamp": pd.date_range(
            "2025-01-01T00:00:00Z", periods=13, freq="15min"
        ),
        "open": [low + 0.5 for low in lows],
        "high": [low + 2 for low in lows],
        "low": lows,
        "close": [low + 1 for low in lows],
        "volume": [100] * len(lows),
    })


def _app(
    price,
    target=120,
    monotonic_lows=False,
    direction="LONG",
    leverage=30,
):
    app = KhtbLab.__new__(KhtbLab)
    app.db = Database(":memory:")
    trade_id = app.db.open_trade(
        "BTC/USDT",
        direction,
        100,
        95 if direction == "LONG" else 105,
        target,
        2,
        source="DEMO",
        snapshot={"atr": 2},
        protection_order_id="tpsl-1",
        protection_client_oid="tpsl-client",
        entry_leverage=leverage,
    )
    app.db.conn.execute(
        "UPDATE trades SET ts_open=? WHERE id=?",
        ("2025-01-01T00:00:00+00:00", trade_id),
    )
    app.db.conn.commit()

    calls = []
    app.execution = SimpleNamespace(
        positions_reconciled=True,
        _precision_price=lambda symbol, value: f"{value:.4f}",
        update_tpsl_protection=lambda trade, stop, new_target: (
            calls.append((trade, stop, new_target))
            or {
                "ok": True,
                "order_id": "tpsl-1",
                "client_oid": "tpsl-client",
            }
        ),
    )
    app.market = SimpleNamespace(
        candles=lambda symbol, timeframe, limit: _candles(monotonic_lows)
    )
    app.settings = SimpleNamespace(
        trading_env="DEMO",
        timeframe_main="15m",
        leverage=leverage,
    )
    app.current_price = lambda symbol: price
    app.notify = lambda message: None
    return app, calls


def test_profit_lock_stays_inactive_before_ten_percent_margin_roe():
    app, calls = _app(price=100.32, monotonic_lows=True)
    frame = _candles(monotonic_lows=True)
    frame["high"] = frame["high"].clip(upper=100.32)
    app.market.candles = lambda symbol, timeframe, limit: frame

    assert app._manage_exchange_trades() is True

    trade = app.db.open_trades("DEMO")[0]
    assert trade["management_stage"] == 0
    assert trade["stop"] == 95
    assert calls == []
    app.db.close()


def test_profit_lock_secures_ten_percent_margin_roe_before_structure_forms():
    app, calls = _app(price=100.34, monotonic_lows=True)
    frame = _candles(monotonic_lows=True)
    frame["high"] = frame["high"].clip(upper=100.34)
    app.market.candles = lambda symbol, timeframe, limit: frame

    assert app._manage_exchange_trades() is True

    trade = app.db.open_trades("DEMO")[0]
    assert trade["management_stage"] == 1
    assert trade["favorable_price"] == 100.34
    assert 100.33 <= trade["stop"] < 100.34
    assert len(calls) == 1
    assert calls[0][1] == trade["stop"]
    assert calls[0][2] == 120
    app.db.close()


def test_profit_lock_uses_entry_leverage_and_does_not_loosen_safer_stop():
    app, calls = _app(
        price=100.6,
        monotonic_lows=True,
        leverage=20,
    )
    app.db.conn.execute(
        "UPDATE trades SET stop=100.55 WHERE status='OPEN'"
    )
    app.db.conn.commit()
    frame = _candles(monotonic_lows=True)
    frame["high"] = frame["high"].clip(upper=100.6)
    app.market.candles = lambda symbol, timeframe, limit: frame

    assert app._manage_exchange_trades() is True

    trade = app.db.open_trades("DEMO")[0]
    assert trade["entry_leverage"] == 20
    assert trade["management_stage"] == 1
    assert trade["stop"] == 100.55
    assert calls == []
    app.db.close()


def test_short_profit_lock_sets_stop_for_ten_percent_margin_roe():
    app, calls = _app(
        price=99.66,
        target=80,
        monotonic_lows=True,
        direction="SHORT",
    )
    frame = _candles(monotonic_lows=True)
    lows = [100.8 - index * 0.1 for index in range(13)]
    frame["low"] = lows
    frame["open"] = [low + 0.5 for low in lows]
    frame["high"] = [low + 2 for low in lows]
    frame["close"] = [low + 1 for low in lows]
    app.market.candles = lambda symbol, timeframe, limit: frame

    assert app._manage_exchange_trades() is True

    trade = app.db.open_trades("DEMO")[0]
    assert trade["management_stage"] == 1
    assert 99.66 < trade["stop"] <= 99.67
    assert len(calls) == 1
    assert calls[0][1] == trade["stop"]
    app.db.close()


def test_profit_lock_trigger_reached_but_market_has_crossed_lock_price():
    app, calls = _app(price=100.2, monotonic_lows=True)
    frame = _candles(monotonic_lows=True)
    frame["high"] = frame["high"].clip(upper=100.5).astype(float)
    frame.loc[5, "high"] = 100.5
    app.market.candles = lambda symbol, timeframe, limit: frame

    assert app._manage_exchange_trades() is True

    trade = app.db.open_trades("DEMO")[0]
    assert trade["management_stage"] == 1
    assert trade["favorable_price"] == 100.5
    assert trade["stop"] == 95
    assert calls == []
    app.db.close()


def test_rejected_lock_update_keeps_confirmed_original_protection_and_gate():
    app, calls = _app(price=100.34, monotonic_lows=True)
    frame = _candles(monotonic_lows=True)
    frame["high"] = frame["high"].clip(upper=100.34)
    app.market.candles = lambda symbol, timeframe, limit: frame
    app.execution.update_tpsl_protection = (
        lambda trade, stop, target: {
            "ok": False,
            "protection_preserved": True,
            "actual_stop": "95",
            "actual_target": "120",
            "order_id": "tpsl-1",
            "client_oid": "tpsl-client",
            "reason": "new_stop_invalid_vs_mark_price",
        }
    )

    assert app._manage_exchange_trades() is True

    trade = app.db.open_trades("DEMO")[0]
    assert trade["management_stage"] == 1
    assert trade["stop"] == 95
    assert trade["protection_order_id"] == "tpsl-1"
    assert app.execution.positions_reconciled is True
    assert calls == []
    app.db.close()


def test_tp1_lock_moves_stop_to_one_r_after_an_extra_ten_percent_roe():
    app, calls = _app(price=105.4, monotonic_lows=True)
    frame = _candles(monotonic_lows=True)
    frame["high"] = frame["high"].clip(upper=105.4)
    app.market.candles = lambda symbol, timeframe, limit: frame

    assert app._manage_exchange_trades() is True

    trade = app.db.open_trades("DEMO")[0]
    assert trade["management_stage"] == 1
    assert trade["stop"] == 105
    assert len(calls) == 1
    assert calls[0][1] == 105
    assert calls[0][2] == 120
    app.db.close()


def test_tp1_lock_waits_for_ten_percent_roe_extension_after_tp1():
    app, calls = _app(price=105.3, monotonic_lows=True)
    frame = _candles(monotonic_lows=True)
    frame["high"] = frame["high"].clip(upper=105.3)
    app.market.candles = lambda symbol, timeframe, limit: frame

    assert app._manage_exchange_trades() is True

    trade = app.db.open_trades("DEMO")[0]
    assert trade["management_stage"] == 1
    assert 100 < trade["stop"] < 105
    assert calls[0][1] == trade["stop"]
    app.db.close()


def test_unreconciled_position_is_never_managed():
    app, calls = _app(price=118)
    app.execution.positions_reconciled = False
    app.market.candles = lambda *args: (_ for _ in ()).throw(
        AssertionError("unreconciled positions must not be managed")
    )

    assert app._manage_exchange_trades() is False

    trade = app.db.open_trades("DEMO")[0]
    assert trade["management_stage"] == 0
    assert trade["stop"] == 95
    assert calls == []
    app.db.close()


def test_confirmed_swing_ratchets_stop_without_changing_full_position_or_tp2():
    app, calls = _app(price=120.5)
    original = app.db.open_trades("DEMO")[0]

    assert app._manage_exchange_trades() is True

    trade = app.db.open_trades("DEMO")[0]
    assert trade["management_stage"] == 3
    assert 100 < trade["stop"] < 120.5
    assert trade["qty"] == original["qty"]
    assert trade["target"] == original["target"]
    assert len(calls) == 1
    assert calls[0][1] == trade["stop"]
    assert calls[0][2] is None
    app.db.close()


def test_virtual_tp2_removal_requires_profitable_structural_stop_confirmation():
    app, calls = _app(price=118)
    original = app.db.open_trades("DEMO")[0]

    assert app._manage_exchange_trades() is True

    trade = app.db.open_trades("DEMO")[0]
    assert trade["management_stage"] == 2
    assert 100 < trade["stop"] < 118
    assert trade["target"] == original["target"]
    assert trade["qty"] == original["qty"]
    assert calls[0][2] is None
    app.db.close()


def test_tp2_exchange_target_is_removed_before_price_can_extend_beyond_tp2():
    app, calls = _app(price=119, monotonic_lows=True)

    assert app._manage_exchange_trades() is True

    trade = app.db.open_trades("DEMO")[0]
    assert trade["management_stage"] == 2
    assert trade["stop"] == 105
    assert trade["target"] == 120
    assert len(calls) == 1
    assert calls[0][2] is None
    app.db.close()


def test_structural_trailing_waits_for_ten_percent_roe_beyond_tp2():
    app, calls = _app(price=120.2, monotonic_lows=True)
    structural_calls = []
    app._structural_trailing_stop = (
        lambda trade, frame, price, atr: structural_calls.append(price) or 110
    )

    assert app._manage_exchange_trades() is True
    trade = app.db.open_trades("DEMO")[0]
    assert trade["management_stage"] == 3
    assert structural_calls == []
    assert trade["stop"] == 105
    assert calls[0][2] is None
    app.db.close()


def test_structural_trailing_starts_after_ten_percent_roe_beyond_tp2():
    app, calls = _app(price=120.4, monotonic_lows=True)
    structural_calls = []
    app._structural_trailing_stop = (
        lambda trade, frame, price, atr: structural_calls.append(price) or 110
    )

    assert app._manage_exchange_trades() is True

    trade = app.db.open_trades("DEMO")[0]
    assert structural_calls == [120.4]
    assert trade["stop"] == 110
    assert calls[0][1] == 110
    app.db.close()


def test_unconfirmed_exchange_update_persists_trigger_and_blocks_auto():
    app, calls = _app(price=118)
    app.execution.update_tpsl_protection = (
        lambda trade, stop, target: calls.append((trade, stop, target))
        or {"ok": False, "reason": "exchange_state_unconfirmed"}
    )

    assert app._manage_exchange_trades() is False

    trade = app.db.open_trades("DEMO")[0]
    assert trade["management_stage"] == 1
    assert trade["stop"] == 95
    assert trade["target"] == 120
    assert app.execution.positions_reconciled is False
    assert calls[0][2] is None
    app.db.close()


def test_virtual_tp2_crossing_advances_persisted_trailing_stage():
    app, _ = _app(price=118)
    assert app._manage_exchange_trades() is True
    app.current_price = lambda symbol: 122

    assert app._manage_exchange_trades() is True

    trade = app.db.open_trades("DEMO")[0]
    assert trade["management_stage"] == 3
    assert trade["target"] == 120
    assert trade["qty"] == 2
    app.db.close()


def test_short_trailing_stop_uses_confirmed_swing_high_and_only_moves_down():
    app, _ = _app(price=85)
    highs = [101, 100, 102, 99, 98, 99, 97, 96, 98, 95, 94, 93, 92]
    frame = pd.DataFrame({
        "timestamp": pd.date_range(
            "2025-01-01T00:00:00Z", periods=len(highs), freq="15min"
        ),
        "open": [high - 1.5 for high in highs],
        "high": highs,
        "low": [high - 2 for high in highs],
        "close": [high - 1 for high in highs],
        "volume": [100] * len(highs),
    })
    trade = {
        "id": 3,
        "symbol": "BTC/USDT",
        "direction": "SHORT",
        "entry": 100,
        "stop": 105,
        "ts_open": "2025-01-01T00:00:00+00:00",
    }
    atr = float(enrich(frame).iloc[-2]["atr"])

    stop = app._structural_trailing_stop(trade, frame, 85, atr)

    assert 85 < stop < 100
    app.db.close()
