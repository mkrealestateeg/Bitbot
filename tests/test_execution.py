from types import SimpleNamespace
from dataclasses import replace

from khtb_lab.config import Settings
from khtb_lab.database import Database
from khtb_lab.execution import DemoExecutionEngine


class FakeExchange:
    def __init__(self):
        self.orders = []
    def create_order(self, *args, **kwargs):
        self.orders.append((args, kwargs))
        return {'id': 'demo-1', 'status': 'closed', 'filled': 10, 'average': 0.2, 'clientOrderId': 'cid'}
    def fetch_order(self, *args, **kwargs):
        return {'id': 'demo-1', 'status': 'closed', 'filled': 10, 'average': 0.2, 'clientOrderId': 'cid'}


class FakeMarket:
    def __init__(self):
        self.exchange = FakeExchange()
    def validate_symbol(self, s): return s
    def set_one_way_mode(self): return None
    def set_margin_mode(self, *a): return None
    def set_leverage(self, *a): return None
    def ticker(self, s): return {'last': 0.2}
    def market(self, s): return {'contractSize': 1, 'limits': {'amount': {'min': 1}}}
    def amount_to_precision(self, s, a): return float(a)
    def price_to_precision(self, s, p): return float(p)
    def fetch_position(self, s): return {'contracts': 10, 'info': {'total': '10', 'stopLossId': 'sl1', 'takeProfitId': 'tp1', 'stopLoss': '0.19', 'takeProfit': '0.23'}}
    def close_position(self, s): return {'id': 'close'}


def test_demo_engine_is_hard_gated_and_records_exchange_trade():
    settings = replace(
        Settings(), trading_env='bitget_demo', bitget_demo=True,
        bitget_key='k', bitget_secret='s', bitget_passphrase='p',
        enable_exchange_orders=True, demo_armed=True, max_margin_per_trade=5,
    )
    db = Database(':memory:')
    market = FakeMarket()
    engine = DemoExecutionEngine(settings, market, db)
    tid, msg = engine.open('FET/USDT:USDT', {
        'direction': 'LONG', 'price': .2, 'stop': .19, 'target': .23,
        'snapshot': {},
    })
    assert tid == 1
    assert 'DEMO OPEN' in msg
    trade = db.open_trades()[0]
    assert trade['source'] == 'BITGET_DEMO'
    assert trade['exchange_order_id'] == 'demo-1'
    params = market.exchange.orders[0][0][5]
    assert params['presetStopLossPrice'] == '0.19'
    assert params['presetStopSurplusPrice'] == '0.23'
