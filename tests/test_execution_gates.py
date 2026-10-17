from dataclasses import replace

from khtb_lab.config import Settings
from khtb_lab.execution import ExecutionEngine

def test_demo_is_fail_closed():
    settings = replace(
        Settings(),
        trading_env="DEMO",
        enable_exchange_orders=False,
        demo_armed=False,
    )
    e=ExecutionEngine(settings)
    ok,why=e.can_execute()
    assert ok is False
    assert why == "ENABLE_EXCHANGE_ORDERS=false"
