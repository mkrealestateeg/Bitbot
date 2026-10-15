
from __future__ import annotations

import logging
import time

import ccxt
import pandas as pd


log = logging.getLogger(__name__)

CANDLE_COLUMNS = [
    "timestamp",
    "open",
    "high",
    "low",
    "close",
    "volume",
]


class Market:
    def __init__(self, settings):
        self.settings = settings

        klass = getattr(ccxt, settings.exchange_id)

        timeout_seconds = max(
            1,
            int(getattr(settings, "request_timeout", 15)),
        )

        self.exchange = klass(
            {
                "enableRateLimit": True,
                "timeout": timeout_seconds * 1000,
                "options": {
                    "defaultType": "swap",
                },
            }
        )

        self.markets_loaded = False
        self.max_retries = 3
        self.retry_delay = 1.0

    def _request_with_retry(self, operation, label):
        """
        Retry temporary network and exchange availability errors.
        Do not retry invalid symbols or other permanent errors.
        """
        retryable = (
            ccxt.NetworkError,
            ccxt.RequestTimeout,
            ccxt.ExchangeNotAvailable,
            ccxt.DDoSProtection,
            ccxt.RateLimitExceeded,
        )

        for attempt in range(1, self.max_retries + 1):
            try:
                return operation()

            except retryable as exc:
                if attempt >= self.max_retries:
                    log.error(
                        "MARKET REQUEST FAILED | %s | attempts=%s | %s",
                        label,
                        attempt,
                        exc,
                    )
                    raise

                delay = self.retry_delay * (2 ** (attempt - 1))

                log.warning(
                    "MARKET RETRY | %s | attempt=%s/%s | "
                    "wait=%.1fs | error=%s",
                    label,
                    attempt,
                    self.max_retries,
                    delay,
                    exc,
                )

                time.sleep(delay)

    def load(self):
        if self.markets_loaded:
            return

        markets = self._request_with_retry(
            self.exchange.load_markets,
            "load_markets",
        )

        if not markets:
            raise RuntimeError(
                f"Exchange returned no markets: "
                f"{self.settings.exchange_name}"
            )

        self.markets_loaded = True

        log.info(
            "MARKETS LOADED | exchange=%s | count=%s",
            self.settings.exchange_name,
            len(markets),
        )

    def validate_symbol(self, symbol):
        if not self.markets_loaded:
            self.load()

        if symbol in self.exchange.markets:
            return symbol

        normalized = symbol.replace(":USDT", "")

        candidates = [
            market_symbol
            for market_symbol, market in self.exchange.markets.items()
            if market.get("swap")
            and market.get("quote") == "USDT"
            and market_symbol.replace(":USDT", "") == normalized
        ]

        if candidates:
            return candidates[0]

        raise ValueError(
            f"Market not found on "
            f"{self.settings.exchange_name}: {symbol}"
        )

    def candles(self, symbol, timeframe, limit=250):
        actual = self.validate_symbol(symbol)

        log.info(
            "MARKET DATA REQUEST | symbol=%s | timeframe=%s | limit=%s",
            actual,
            timeframe,
            limit,
        )

        rows = self._request_with_retry(
            lambda: self.exchange.fetch_ohlcv(
                actual,
                timeframe=timeframe,
                limit=limit,
            ),
            f"fetch_ohlcv:{actual}:{timeframe}",
        )

        if not rows:
            raise ValueError(
                f"No candles returned for {actual} ({timeframe})"
            )

        frame = pd.DataFrame(rows, columns=CANDLE_COLUMNS)

        frame["timestamp"] = pd.to_datetime(
            frame["timestamp"],
            unit="ms",
            utc=True,
            errors="coerce",
        )

        numeric_columns = [
            "open",
            "high",
            "low",
            "close",
            "volume",
        ]

        for column in numeric_columns:
            frame[column] = pd.to_numeric(
                frame[column],
                errors="coerce",
            )

        frame = (
            frame.dropna(subset=CANDLE_COLUMNS)
            .drop_duplicates(subset=["timestamp"], keep="last")
            .sort_values("timestamp")
            .reset_index(drop=True)
        )

        # Reject malformed candles before they reach the strategy.
        valid_prices = (
            (frame["open"] > 0)
            & (frame["high"] > 0)
            & (frame["low"] > 0)
            & (frame["close"] > 0)
            & (frame["volume"] >= 0)
            & (frame["high"] >= frame["low"])
            & (frame["high"] >= frame["open"])
            & (frame["high"] >= frame["close"])
            & (frame["low"] <= frame["open"])
            & (frame["low"] <= frame["close"])
        )

        frame = frame.loc[valid_prices].reset_index(drop=True)

        if len(frame) < 3:
            raise ValueError(
                f"Insufficient valid candles for {actual} "
                f"({timeframe}): {len(frame)}"
            )

        log.info(
            "MARKET DATA READY | symbol=%s | timeframe=%s | candles=%s",
            actual,
            timeframe,
            len(frame),
        )

        return frame

    def ticker(self, symbol):
        actual = self.validate_symbol(symbol)

        ticker = self._request_with_retry(
            lambda: self.exchange.fetch_ticker(actual),
            f"fetch_ticker:{actual}",
        )

        if not isinstance(ticker, dict):
            raise ValueError(
                f"Invalid ticker response for {actual}"
            )

        return ticker
