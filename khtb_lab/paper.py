from .risk import position_size
class PaperBroker:
    def __init__(self,settings,db):self.settings=settings;self.db=db;self.capital=settings.starting_capital;self.risk_fraction=settings.risk_per_trade
    def open(self,symbol,signal,mode="AUTO"):
        qty=position_size(self.capital,signal["price"],signal["stop"],self.risk_fraction)
        return self.db.open_trade(symbol,signal["direction"],signal["price"],signal["stop"],signal["target"],qty,mode=mode,source="PAPER",snapshot=signal.get("snapshot")) if qty>0 else None
    def mark(self,prices):
        closed=[]
        for trade in self.db.open_trades("PAPER"):
            price=prices.get(trade["symbol"])
            if price is None:continue
            d=1 if trade["direction"]=="LONG" else -1;reason=None;exit_price=None
            if (d==1 and price<=trade["stop"]) or(d==-1 and price>=trade["stop"]):reason="STOP_LOSS";exit_price=trade["stop"]
            elif(d==1 and price>=trade["target"]) or(d==-1 and price<=trade["target"]):reason="TAKE_PROFIT";exit_price=trade["target"]
            if reason:
                pnl=(exit_price-trade["entry"])*trade["qty"]*d;self.db.close_trade(trade["id"],exit_price,pnl,reason);self.capital+=pnl;closed.append((trade["id"],reason,pnl))
        return closed
    def close_trade(self,trade,price,reason="MANUAL_CLOSE"):
        direction=1 if trade["direction"]=="LONG" else -1
        pnl=(price-trade["entry"])*trade["qty"]*direction
        if self.db.close_trade(trade["id"],price,pnl,reason):
            self.capital+=pnl
            return pnl
        return None
    def close_all(self,prices,reason="MANUAL_CLOSE"):
        closed=[]
        for trade in self.db.open_trades("PAPER"):
            p=prices.get(trade["symbol"])
            if p is None:continue
            d=1 if trade["direction"]=="LONG" else -1;pnl=(p-trade["entry"])*trade["qty"]*d;self.db.close_trade(trade["id"],p,pnl,reason);self.capital+=pnl;closed.append((trade["id"],pnl))
        return closed
