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
    app.db = SimpleNamespace(enabled_symbols=lambda: ["BTC/USDT", "ETH/USDT"])
    app.settings = SimpleNamespace(trading_env="DEMO")
    app.mode = "MANUAL"
    app.last_scan = "Never"
    app.last_result = "Not scanned"
    processed = []

    def process_symbol(symbol, prices, allow_auto):
        processed.append((symbol, allow_auto))

    app._process_symbol = process_symbol

    result = app.scan_once(allow_auto=False)

    assert processed == [("BTC/USDT", False), ("ETH/USDT", False)]
    assert result == "Completed scan; symbols=2; errors=0"
    assert app.last_scan != "Never"
    assert app.last_result == result
