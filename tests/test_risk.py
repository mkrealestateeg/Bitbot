from khtb_lab.risk import position_size, risk_check
from types import SimpleNamespace

class DB:
    def open_trades(self): return []
    def daily_pnl(self): return 0

def test_position_size_respects_risk_budget():
    assert position_size(1000,100,95,.01)==2

def test_position_size_invalid_levels():
    assert position_size(1000,100,100,.01)==0

def test_neutral_signal_rejected():
    settings=SimpleNamespace(max_open_positions=3,max_daily_loss=.02)
    ok,reason=risk_check(settings,DB(),{'direction':'NEUTRAL','score':90,'price':100,'stop':95,'target':110},1000)
    assert not ok and 'directional' in reason

def test_valid_long_signal_passes():
    settings=SimpleNamespace(max_open_positions=3,max_daily_loss=.02)
    ok,_=risk_check(settings,DB(),{'direction':'LONG','score':75,'price':100,'stop':95,'target':110},1000)
    assert ok
