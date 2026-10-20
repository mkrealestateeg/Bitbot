import sqlite3

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


def test_trade_management_persists_virtual_target_stage_and_order_identity():
    db=Database(':memory:')
    trade_id=db.open_trade(
        'BTC/USDT',
        'LONG',
        100,
        95,
        120,
        2,
        source='DEMO',
        protection_order_id='tpsl-1',
        protection_client_oid='tpsl-client',
    )
    trade=db.open_trades('DEMO')[0]

    assert trade['initial_risk']==5
    assert trade['favorable_price']==100
    assert trade['protection_order_id']=='tpsl-1'
    assert db.save_trade_management(trade_id,1,112,106,'tpsl-1','tpsl-client')
    assert db.save_trade_management(trade_id,2,118,110)
    assert not db.save_trade_management(trade_id,1,117,109)
    assert not db.save_trade_management(trade_id,3,119,108)

    updated=db.open_trades('DEMO')[0]
    assert updated['management_stage']==2
    assert updated['favorable_price']==118
    assert updated['stop']==110
    assert updated['target']==120
    assert updated['protection_order_id']=='tpsl-1'
    db.close()


def test_existing_trade_database_is_migrated_with_safe_management_defaults(
    tmp_path,
):
    database_path=tmp_path/'legacy.sqlite'
    connection=sqlite3.connect(database_path)
    connection.execute(
        "CREATE TABLE trades("
        "id INTEGER PRIMARY KEY,ts_open TEXT NOT NULL,ts_close TEXT,"
        "symbol TEXT NOT NULL,direction TEXT NOT NULL,entry REAL NOT NULL,"
        "stop REAL NOT NULL,target REAL NOT NULL,qty REAL NOT NULL,"
        "status TEXT NOT NULL,mode TEXT NOT NULL,pnl REAL,r_multiple REAL,"
        "source TEXT NOT NULL DEFAULT 'PAPER',close_reason TEXT,snapshot TEXT)"
    )
    connection.execute(
        "INSERT INTO trades("
        "id,ts_open,symbol,direction,entry,stop,target,qty,status,mode,source"
        ") VALUES(1,'2025-01-01T00:00:00+00:00','BTC/USDT','LONG',100,95,"
        "120,1,'OPEN','AUTO','DEMO')"
    )
    connection.commit()
    connection.close()

    db=Database(str(database_path))
    trade=db.open_trades('DEMO')[0]

    assert trade['initial_risk']==5
    assert trade['management_stage']==0
    assert trade['favorable_price']==100
    assert trade['entry_leverage'] is None
    assert trade['protection_order_id'] is None
    db.close()
