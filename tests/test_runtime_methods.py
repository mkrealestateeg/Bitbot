from types import SimpleNamespace

from khtb_lab.app import KhtbLab
from khtb_lab.execution import ExecutionEngine


def test_runtime_methods_are_available_on_their_classes():
    assert callable(KhtbLab.run)
    assert callable(KhtbLab.scan_once)
    assert callable(KhtbLab._process_symbol)
    assert callable(ExecutionEngine.execute)


def test_scan_once_processes_enabled_symbols_and_updates_status():
    app = KhtbLab.__new__(KhtbLab)
    app.db = SimpleNamespace(
        enabled_symbols=lambda: ["BTC/USDT", "ETH/USDT"],
        open_trades=lambda: [],
    )
    app.settings = SimpleNamespace(trading_env="DEMO")
    app.mode = "MANUAL"
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
        open_trades=lambda: [],
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
