from types import SimpleNamespace

from khtb_lab.execution import ExecutionEngine


def _ready_demo_engine():
    engine = ExecutionEngine.__new__(ExecutionEngine)
    engine.settings = SimpleNamespace(
        trading_env="DEMO",
        enable_exchange_orders=True,
        demo_armed=True,
        live_armed=False,
        leverage=2,
    )
    engine.environment = "DEMO"
    engine.ready = True
    engine.account = object()
    engine.exchange = None
    return engine


def test_prepare_accepts_a_valid_order_plan():
    engine = _ready_demo_engine()

    plan = engine.prepare("BTC/USDT", "LONG", 1, 100, 95, 110)

    assert plan["ready"] is True
    assert plan["error"] is None
    assert plan["leverage"] == 2


def test_prepare_rejects_invalid_long_levels():
    engine = _ready_demo_engine()

    plan = engine.prepare("BTC/USDT", "LONG", 1, 100, 105, 110)

    assert plan["ready"] is False
    assert plan["error"] == "invalid_long_stop_or_target"
