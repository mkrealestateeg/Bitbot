
from __future__ import annotations
import ccxt, pandas as pd
class Market:
    def __init__(self,settings):
        self.settings=settings
        klass=getattr(ccxt,settings.exchange_id)
        self.exchange=klass({"enableRateLimit":True,"options":{"defaultType":"swap"}})
        self.markets_loaded=False
    def load(self): self.exchange.load_markets(); self.markets_loaded=True
    def validate_symbol(self,symbol):
        if not self.markets_loaded: self.load()
        if symbol in self.exchange.markets: return symbol
        c=[k for k,m in self.exchange.markets.items() if m.get('swap') and m.get('quote')=='USDT' and k.replace(':USDT','')==symbol.replace(':USDT','')]
        if c:return c[0]
        raise ValueError(f"Market not found on {self.settings.exchange_name}: {symbol}")
    def candles(self,symbol,timeframe,limit=250):
        actual=self.validate_symbol(symbol); rows=self.exchange.fetch_ohlcv(actual,timeframe=timeframe,limit=limit)
        return pd.DataFrame(rows,columns=['timestamp','open','high','low','close','volume']).assign(timestamp=lambda d:pd.to_datetime(d.timestamp,unit='ms',utc=True))
    def ticker(self,symbol): return self.exchange.fetch_ticker(self.validate_symbol(symbol))
