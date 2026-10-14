from __future__ import annotations

def position_size(capital: float, entry: float, stop: float, risk_fraction: float) -> float:
    distance=abs(entry-stop)
    if capital<=0 or entry<=0 or distance<=0: return 0.0
    risk_cash=capital*risk_fraction
    return risk_cash/distance

def risk_check(settings, db, signal, capital):
    if signal.get('direction') not in {'LONG','SHORT'}: return False,'No directional setup'
    if signal.get('score',0)<65: return False,'Technical score below 65'
    if len(db.open_trades())>=settings.max_open_positions: return False,'Maximum open positions reached'
    if db.daily_pnl() <= -capital*settings.max_daily_loss: return False,'Daily loss limit reached'
    if not signal.get('stop') or not signal.get('target'): return False,'Missing stop or target'
    if signal['direction']=='LONG' and not signal['stop']<signal['price']<signal['target']: return False,'Invalid LONG levels'
    if signal['direction']=='SHORT' and not signal['target']<signal['price']<signal['stop']: return False,'Invalid SHORT levels'
    return True,'Risk checks passed'
