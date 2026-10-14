from dataclasses import replace
import pytest
from khtb_lab.config import Settings

def test_default_safe_configuration():
    s=Settings()
    assert s.trading_env=='paper'
    assert s.trading_mode=='MANUAL'
    assert s.enable_exchange_orders is False

def test_live_environment_is_rejected():
    s=replace(Settings(),trading_env='live',live_armed=True)
    with pytest.raises(ValueError,match='not supported'):
        s.validate()

def test_exchange_orders_are_rejected_even_if_armed():
    s=replace(Settings(),trading_env='bitget_demo',bitget_demo=True,enable_exchange_orders=True,demo_armed=True)
    with pytest.raises(ValueError,match='not implemented'):
        s.validate()
