from __future__ import annotations
import ccxt
import pandas as pd

class Market:
    def __init__(self, settings):
        self.settings=settings
        opts={"enableRateLimit":True,"options":{"defaultType":"swap"}}
        if settings.bitget_key and settings.bitget_secret and settings.bitget_passphrase:
            opts.update({"apiKey":settings.bitget_key,"secret":settings.bitget_secret,"password":settings.bitget_passphrase})
        self.exchange=ccxt.bitget(opts)
        self.markets_loaded=False
    def load(self):
        self.exchange.load_markets(); self.markets_loaded=True
    def validate_symbol(self,symbol):
        if not self.markets_loaded: self.load()
        if symbol in self.exchange.markets: return symbol
        # CCXT versions may expose the USDT perpetual under a normalized key.
        candidates=[k for k,m in self.exchange.markets.items() if m.get('swap') and m.get('base') and m.get('quote')=='USDT' and (k==symbol or (k.replace(':USDT','')==symbol.replace(':USDT','')))]
        if candidates: return candidates[0]
        raise ValueError(f"Bitget CCXT market not found for {symbol}. Check exchange market metadata; no symbol is silently substituted.")
    def candles(self,symbol,timeframe,limit=250):
        actual=self.validate_symbol(symbol)
        rows=self.exchange.fetch_ohlcv(actual,timeframe=timeframe,limit=limit)
        if not rows: raise RuntimeError(f"No OHLCV data for {actual} {timeframe}")
        return pd.DataFrame(rows,columns=['timestamp','open','high','low','close','volume']).assign(timestamp=lambda d:pd.to_datetime(d.timestamp,unit='ms',utc=True))
    def ticker(self,symbol): return self.exchange.fetch_ticker(self.validate_symbol(symbol))
