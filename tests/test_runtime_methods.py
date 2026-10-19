import threading
from types import SimpleNamespace

import pytest

import khtb_lab.app as app_module
from khtb_lab.app import KhtbLab
from khtb_lab.database import Database
from khtb_lab.execution import ExecutionEngine


def test_runtime_methods_are_available_on_their_classes():
    assert callable(KhtbLab.run)
    assert callable(KhtbLab.scan_once)
    assert callable(KhtbLab._process_symbol)
    assert callable(ExecutionEngine.execute)


def test_run_polls_telegram_in_background_and_stops_worker_on_exit():
    app = KhtbLab.__new__(KhtbLab)
    app.settings = SimpleNamespace(
        trading_env="DEMO",
        exchange_name="Bitget",
        scan_seconds=5,
    )
    app.mode = "MANUAL"
    poll_finished = threading.Event()
    poll_threads = []

    class _Telegram:
        started = False

        def startup(self):
            self.started = True
            return True

        def poll_once(self):
            poll_threads.append(threading.current_thread().name)
            poll_finished.set()

    def stop_after_poll(allow_auto):
        if not poll_finished.wait(timeout=2):
            raise AssertionError("Telegram polling stalled")
        raise KeyboardInterrupt()

    app.scan_once = stop_after_poll

    with pytest.raises(KeyboardInterrupt):
        app.run(_Telegram())

    assert poll_threads == ["TelegramWorker"]
    assert app.telegram.started is True
    assert not any(
        thread.name == "TelegramWorker" and thread.is_alive()
        for thread in threading.enumerate()
    )


def test_scan_once_processes_enabled_symbols_and_updates_status():
    app = KhtbLab.__new__(KhtbLab)
    app.db = SimpleNamespace(
        enabled_symbols=lambda: ["BTC/USDT", "ETH/USDT"],
        open_trades=lambda source=None: [],
    )
    app.settings = SimpleNamespace(trading_env="DEMO")
    app.mode = "MANUAL"
    app.telegram = None
    app.last_scan = "Never"
    app.last_result = "Not scanned"
    app.execution = SimpleNamespace(
        reconcile_positions=lambda trades: {"ok": True, "live_positions": 0}
    )
    processed = []

    def process_symbol(symbol, prices, allow_auto):
        processed.append((symbol, allow_auto))

    app._process_symbol = process_symbol

    result = app.scan_once(allow_auto=False)

    assert processed == [("BTC/USDT", False), ("ETH/USDT", False)]
    assert result == "Completed scan; symbols=2; errors=0"
    assert app.last_scan != "Never"
    assert app.last_result == result


def test_scan_once_blocks_auto_entries_when_exchange_state_is_unreconciled():
    app = KhtbLab.__new__(KhtbLab)
    app.db = SimpleNamespace(
        enabled_symbols=lambda: ["BTC/USDT"],
        open_trades=lambda source=None: [],
    )
    app.settings = SimpleNamespace(trading_env="DEMO")
    app.mode = "AUTO"
    app.telegram = None
    app.last_scan = "Never"
    app.last_result = "Not scanned"
    app.execution = SimpleNamespace(
        reconcile_positions=lambda trades: {
            "ok": False,
            "reason": "exchange_state_mismatch",
            "mismatches": ["untracked_exchange_position:BTCUSDT:LONG"],
        }
    )
    processed = []
    app._process_symbol = lambda symbol, prices, allow_auto: processed.append(
        allow_auto
    )

    result = app.scan_once(allow_auto=True)

    assert processed == [False]
    assert result == "Completed scan; symbols=1; errors=1"


def test_telegram_close_records_trade_only_after_exchange_confirmation():
    app = KhtbLab.__new__(KhtbLab)
    app.db = Database(":memory:")
    app.settings = SimpleNamespace(trading_env="DEMO")
    trade_id = app.db.open_trade(
        "BTC/USDT", "LONG", 100, 95, 110, 2, source="DEMO"
    )
    close_calls = []
    app.execution = SimpleNamespace(
        close_position=lambda trade: close_calls.append(trade["id"])
        or {"success": True, "already_closed": False}
    )
    app.current_price = lambda symbol: 104

    result = app.close_trade_from_telegram(trade_id)

    assert result["success"] is True
    assert close_calls == [trade_id]
    assert result["pnl"] == 8
    assert app.db.open_trades("DEMO") == []
    assert result["trade"]["close_reason"] == "MANUAL_CLOSE_ESTIMATED"
    app.db.close()


def test_telegram_close_does_not_close_record_when_exchange_rejects():
    app = KhtbLab.__new__(KhtbLab)
    app.db = Database(":memory:")
    app.settings = SimpleNamespace(trading_env="DEMO")
    trade_id = app.db.open_trade(
        "BTC/USDT", "LONG", 100, 95, 110, 2, source="DEMO"
    )
    app.execution = SimpleNamespace(
        close_position=lambda trade: {
            "success": False,
            "error": "close_submitted_but_position_still_open",
        }
    )
    app.current_price = lambda symbol: 104

    result = app.close_trade_from_telegram(trade_id)

    assert result["success"] is False
    assert len(app.db.open_trades("DEMO")) == 1
    app.db.close()


@pytest.mark.parametrize(
    (
        "demo_armed",
        "positions_reconciled",
        "live_price",
        "expect_execute",
    ),
    [
        (True, True, 101, True),
        (False, True, 101, False),
        (True, False, 101, False),
        (True, True, 102, False),
    ],
)
def test_take_reaches_demo_execution_only_when_demo_gates_pass(
    monkeypatch,
    demo_armed,
    positions_reconciled,
    live_price,
    expect_execute,
):
    symbol = "BTC/USDT:USDT"
    signal = {
        "direction": "LONG",
        "score": 80,
        "price": 100,
        "stop": 95,
        "target": 110,
        "setup": "TEST",
        "reason": "test signal",
        "snapshot": {},
    }
    monkeypatch.setattr(app_module, "analyze", lambda *args: signal.copy())
    monkeypatch.setattr(
        app_module, "risk_check", lambda *args: (True, "Risk checks passed")
    )

    settings = SimpleNamespace(
        trading_env="DEMO",
        timeframe_main="15m",
        timeframe_trend="1h",
        timeframe_context="4h",
        ai_min_tech_score=65,
        notify_technical_opportunity=False,
        notify_auto_ready=False,
        risk_per_trade=0.01,
        max_margin_per_trade=50,
        leverage=2,
        enable_exchange_orders=True,
        demo_armed=demo_armed,
        live_armed=False,
        position_mode="one_way",
    )
    db = SimpleNamespace(
        save_signal=lambda *args: None,
        open_trades=lambda source=None: [],
    )
    app = KhtbLab.__new__(KhtbLab)
    app.settings = settings
    app.db = db
    app.market = SimpleNamespace(
        candles=lambda *args: [object()],
        ticker=lambda symbol: {"last": live_price},
    )
    app.mode = "AUTO"
    app.profile = "SAFE"
    app.notify = lambda message: None
    app._review_ai = lambda symbol, candidate: {
        "decision": "TAKE",
        "status": "OK",
        "confidence": 90,
    }

    engine = ExecutionEngine.__new__(ExecutionEngine)
    engine.settings = settings
    engine.environment = "DEMO"
    engine.ready = True
    engine.account = object()
    engine.exchange = None
    engine.positions_reconciled = positions_reconciled
    gate_calls = []
    original_can_execute = engine.can_execute

    def can_execute():
        result = original_can_execute()
        gate_calls.append(result)
        return result

    engine.can_execute = can_execute
    engine.equity = lambda: 1000
    engine.execute_calls = []

    def execute(plan):
        engine.execute_calls.append(plan)
        return {
            "success": False,
            "executed": False,
            "error": "test stub; no order submitted",
            "reconcile_required": False,
        }

    engine.execute = execute
    app.execution = engine

    app._process_symbol(symbol, {}, allow_auto=True)

    assert bool(engine.execute_calls) is expect_execute
    if expect_execute:
        assert gate_calls
        assert engine.execute_calls[0]["environment"] == "DEMO"
        assert engine.execute_calls[0]["entry"] == 101
        assert engine.execute_calls[0]["stop"] == 95
        assert engine.execute_calls[0]["target"] == 110
    elif live_price == 102:
        assert not gate_calls
    elif not demo_armed:
        assert gate_calls
        assert gate_calls[0] == (False, "DEMO_TRADING_ARMED=false")
    elif not positions_reconciled:
        assert gate_calls
        assert gate_calls[0] == (False, "account_state_not_reconciled")
