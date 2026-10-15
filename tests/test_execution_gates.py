from khtb_lab.execution import ExecutionEngine

class S:
    trading_env="DEMO"; demo_armed=False; live_armed=False; enable_exchange_orders=False; exchange_id="bitget"; exchange_name="Bitget"; demo_api_key=""; demo_api_secret=""; demo_api_password=""; live_api_key=""; live_api_secret=""; live_api_password=""; leverage=1

def test_demo_is_fail_closed():
    e=ExecutionEngine(S())
    ok,why=e.can_execute()
    assert ok is False
    assert why == "ENABLE_EXCHANGE_ORDERS=false"
