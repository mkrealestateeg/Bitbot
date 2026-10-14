from khtb_lab.database import Database

def test_paper_trade_lifecycle_and_stats():
    db=Database(':memory:')
    db.seed_symbols(['FET/USDT:USDT'])
    assert db.enabled_symbols()==['FET/USDT:USDT']
    tid=db.open_trade('FET/USDT:USDT','LONG',100,95,110,2)
    assert len(db.open_trades())==1
    assert db.close_trade(tid,110,20,'TAKE_PROFIT')
    s=db.stats()
    assert s['closed']==1 and s['pnl']==20 and s['win_rate']==100
    db.close()

def test_cannot_remove_symbol_with_open_trade():
    db=Database(':memory:'); db.seed_symbols(['FET/USDT:USDT'])
    db.open_trade('FET/USDT:USDT','LONG',100,95,110,2)
    assert not db.remove_symbol('FET/USDT:USDT')
