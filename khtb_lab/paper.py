from __future__ import annotations
from .risk import position_size

class PaperBroker:
    """Simulated broker. Never sends exchange orders."""
    def __init__(self, settings, db): self.settings=settings; self.db=db; self.capital=settings.starting_capital; self.risk_fraction=settings.risk_per_trade
    def open(self, symbol, signal, mode='AUTO'):
        qty=position_size(self.capital,signal['price'],signal['stop'],self.risk_fraction)
        if qty<=0: return None
        return self.db.open_trade(symbol,signal['direction'],signal['price'],signal['stop'],signal['target'],qty,mode=mode,source='PAPER',snapshot=signal.get('snapshot'))
    def mark(self, prices):
        closed=[]
        for trade in self.db.open_trades():
            price=prices.get(trade['symbol'])
            if price is None: continue
            direction=1 if trade['direction']=='LONG' else -1
            reason=None; exit_price=None
            if (direction==1 and price<=trade['stop']) or (direction==-1 and price>=trade['stop']): reason='STOP_LOSS'; exit_price=trade['stop']
            elif (direction==1 and price>=trade['target']) or (direction==-1 and price<=trade['target']): reason='TAKE_PROFIT'; exit_price=trade['target']
            if reason:
                pnl=(exit_price-trade['entry'])*trade['qty']*direction
                self.db.close_trade(trade['id'],exit_price,pnl,reason); self.capital+=pnl; closed.append((trade['id'],reason,pnl))
        return closed
    def close_all(self, prices, reason='MANUAL_CLOSE'):
        closed=[]
        for trade in self.db.open_trades():
            exit_price=prices.get(trade['symbol'])
            if exit_price is None: continue
            direction=1 if trade['direction']=='LONG' else -1; pnl=(exit_price-trade['entry'])*trade['qty']*direction
            self.db.close_trade(trade['id'],exit_price,pnl,reason); self.capital+=pnl; closed.append((trade['id'],pnl))
        return closed
