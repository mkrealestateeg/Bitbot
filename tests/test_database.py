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

def test_open_trade_queries_can_be_scoped_to_trading_environment():
    db=Database(':memory:')
    db.open_trade('BTC/USDT','LONG',100,95,110,1,source='DEMO')
    db.open_trade('ETH/USDT','SHORT',200,210,180,1,source='PAPER')

    assert len(db.open_trades())==2
    assert [trade['source'] for trade in db.open_trades('DEMO')]==['DEMO']
    assert [trade['source'] for trade in db.open_trades('PAPER')]==['PAPER']
    db.close()

def test_trade_followup_is_persisted_per_trade_and_chat():
    db=Database(':memory:')
    first=db.open_trade('BTC/USDT','LONG',100,95,110,1)
    second=db.open_trade('ETH/USDT','SHORT',200,210,180,2)

    db.save_trade_followup(first,123,1001,'first-hash')
    db.mark_trade_followup_event(first,123,'tp1_reached')
    db.save_trade_followup(second,123,1002,'second-hash')

    assert db.trade_followup(first,123)['message_id']==1001
    assert db.trade_followup(first,123)['tp1_reached']==1
    assert db.trade_followup(second,123)['message_id']==1002
    db.close()

def test_trade_stop_sync_only_allows_monotonic_risk_reduction():
    db=Database(':memory:')
    long_id=db.open_trade('BTC/USDT','LONG',100,95,110,1)
    short_id=db.open_trade('ETH/USDT','SHORT',200,210,180,1)

    assert db.update_trade_stop(long_id,100)
    assert not db.update_trade_stop(long_id,99)
    assert db.update_trade_stop(short_id,200)
    assert not db.update_trade_stop(short_id,201)
    db.close()
