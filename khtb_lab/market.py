from __future__ import annotations
import ccxt
import pandas as pd


class Market:
    def __init__(self, settings):
        self.settings = settings
        opts = {"enableRateLimit": True, "options": {"defaultType": "swap"}}
        if settings.bitget_key and settings.bitget_secret and settings.bitget_passphrase:
            opts.update({"apiKey": settings.bitget_key, "secret": settings.bitget_secret, "password": settings.bitget_passphrase})
        self.exchange = ccxt.bitget(opts)
        # Bitget Demo is selected by the PAPTRADING header. CCXT exposes this through sandbox mode.
        if settings.trading_env == "bitget_demo":
            self.exchange.set_sandbox_mode(True)
        self.markets_loaded = False

    def load(self):
        self.exchange.load_markets()
        self.markets_loaded = True

    def validate_symbol(self, symbol):
        if not self.markets_loaded:
            self.load()
        if symbol in self.exchange.markets:
            return symbol
        candidates = [
            k for k, m in self.exchange.markets.items()
            if m.get('swap') and m.get('base') and m.get('quote') == 'USDT'
            and (k == symbol or k.replace(':USDT', '') == symbol.replace(':USDT', ''))
        ]
        if candidates:
            return candidates[0]
        raise ValueError(f"Bitget CCXT market not found for {symbol}; no symbol is silently substituted.")

    def market(self, symbol):
        return self.exchange.market(self.validate_symbol(symbol))

    def candles(self, symbol, timeframe, limit=250):
        actual = self.validate_symbol(symbol)
        rows = self.exchange.fetch_ohlcv(actual, timeframe=timeframe, limit=limit)
        if not rows:
            raise RuntimeError(f"No OHLCV data for {actual} {timeframe}")
        return pd.DataFrame(rows, columns=['timestamp','open','high','low','close','volume']).assign(
            timestamp=lambda d: pd.to_datetime(d.timestamp, unit='ms', utc=True)
        )

    def ticker(self, symbol):
        return self.exchange.fetch_ticker(self.validate_symbol(symbol))

    def fetch_positions(self):
        return self.exchange.fetch_positions(params={"productType": "USDT-FUTURES", "marginCoin": "USDT"})

    def fetch_position(self, symbol):
        actual = self.validate_symbol(symbol)
        try:
            return self.exchange.fetch_position(actual, params={"productType": "USDT-FUTURES", "marginCoin": "USDT"})
        except TypeError:
            return self.exchange.fetch_position(actual)

    def fetch_position_history(self, symbol, since_ms=None):
        actual = self.validate_symbol(symbol)
        params = {"productType": "USDT-FUTURES"}
        return self.exchange.fetch_positions_history([actual], since=since_ms, limit=20, params=params)

    def set_leverage(self, symbol, leverage, margin_mode):
        actual = self.validate_symbol(symbol)
        return self.exchange.set_leverage(int(leverage), actual, {"marginMode": margin_mode})

    def set_margin_mode(self, symbol, margin_mode):
        actual = self.validate_symbol(symbol)
        return self.exchange.set_margin_mode(margin_mode, actual)

    def set_one_way_mode(self):
        # Bitget can reject this when the account is already in the requested mode; that is harmless.
        try:
            return self.exchange.set_position_mode(False)
        except Exception:
            return None

    def amount_to_precision(self, symbol, amount):
        return float(self.exchange.amount_to_precision(self.validate_symbol(symbol), amount))

    def price_to_precision(self, symbol, price):
        return float(self.exchange.price_to_precision(self.validate_symbol(symbol), price))

    def close_position(self, symbol):
        actual = self.validate_symbol(symbol)
        return self.exchange.close_position(actual, params={"marginCoin": "USDT"})
