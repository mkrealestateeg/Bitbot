from khtb_lab.config import Settings
def test_defaults():
 s=Settings(); assert s.trading_env in {"PAPER","DEMO","LIVE"}; assert s.live_armed is False
