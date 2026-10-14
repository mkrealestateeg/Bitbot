from dataclasses import replace
import pytest
from khtb_lab.config import Settings


def test_default_safe_configuration():
    s = Settings()
    assert s.trading_env == 'paper'
    assert s.trading_mode == 'MANUAL'
    assert s.enable_exchange_orders is False
    assert s.leverage == 10
    assert s.max_leverage == 20
    assert s.risk_per_trade == .01
    assert s.max_daily_loss == .03


def test_live_environment_is_rejected():
    s = replace(Settings(), trading_env='live', live_armed=True)
    with pytest.raises(ValueError, match='LIVE trading'):
        s.validate()


def test_demo_execution_requires_demo_credentials_and_arm():
    base = replace(Settings(), trading_env='bitget_demo', bitget_demo=True)
    with pytest.raises(ValueError, match='BITGET_API_KEY'):
        base.validate()


def test_demo_auto_is_allowed_when_armed():
    s = replace(
        Settings(),
        trading_env='bitget_demo',
        trading_mode='AUTO',
        bitget_demo=True,
        bitget_key='k',
        bitget_secret='s',
        bitget_passphrase='p',
        enable_exchange_orders=True,
        demo_armed=True,
    )
    s.validate()
