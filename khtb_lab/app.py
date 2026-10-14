from __future__ import annotations
import logging, time
from .config import Settings
from .database import Database
from .market import Market
from .strategy import analyze
from .risk import risk_check, position_size
from .paper import PaperBroker
from .execution import DemoExecutionEngine
from .ai_review import review_signal
from .backtest import run_backtest

log = logging.getLogger(__name__)


class KhtbLab:
    def __init__(self, settings: Settings):
        settings.validate()
        self.settings = settings
        self.db = Database(settings.db_path)
        self.db.seed_symbols(settings.symbols)
        self.market = Market(settings)
        self.mode = settings.trading_mode
        self.profile = "SAFE"
        self.paper = PaperBroker(settings, self.db)
        self.paper.risk_fraction = settings.risk_per_trade * 0.5
        self.execution = DemoExecutionEngine(settings, self.market, self.db)
        self.market.load()

    def paper_qty(self, entry, stop):
        fraction = self.settings.risk_per_trade * (0.5 if self.profile == "SAFE" else 1.0)
        return position_size(self.paper.capital, entry, stop, fraction)

    def get_prices(self, symbols):
        prices = {}
        for symbol in symbols:
            try:
                prices[symbol] = float(self.market.ticker(symbol).get("last") or 0)
            except Exception:
                log.exception("Ticker fetch failed for %s", symbol)
        return prices

    def _auto_open(self, symbol, signal, messages):
        ok, why = risk_check(self.settings, self.db, signal, self.paper.capital)
        if not ok:
            messages.append(f"  Skip {symbol}: {why}")
            return
        if any(t["symbol"] == symbol for t in self.db.open_trades()):
            messages.append(f"  Skip {symbol}: position already open")
            return

        if self.settings.trading_env == "paper":
            tid = self.paper.open(symbol, signal, "AUTO")
            if tid:
                messages.append(f"  PAPER OPEN #{tid}; SL={signal['stop']:.8g}; TP={signal['target']:.8g}")
        elif self.settings.trading_env == "bitget_demo":
            tid, result = self.execution.open(symbol, signal, mode="AUTO", profile=self.profile)
            if tid:
                messages.append(f"  {result}")
            else:
                messages.append(f"  Demo skip {symbol}: {result}")

    def scan_once(self, allow_auto=True):
        messages = []
        prices = {}
        log.info("SCAN START mode=%s env=%s profile=%s", self.mode, self.settings.trading_env, self.profile)
        for symbol in self.db.enabled_symbols():
            log.info("Evaluating symbol %s", symbol)
            try:
                main = self.market.candles(symbol, self.settings.timeframe_main, 250)
                trend = self.market.candles(symbol, self.settings.timeframe_trend, 250)
                context = self.market.candles(symbol, self.settings.timeframe_context, 250)
                signal = analyze(main, trend, context)
                prices[symbol] = signal["price"]
                ai_note = review_signal(self.settings, symbol, signal)
                self.db.save_signal(symbol, signal["direction"], signal["price"], signal["score"], signal.get("setup", ""), signal.get("reason", ""), signal.get("snapshot", {}))
                messages.append(f"{symbol}: {signal['direction']} score={signal['score']:.0f} price={signal['price']:.8g} setup={signal.get('setup','')}")
                if ai_note:
                    messages.append(f"  Gemini review (commentary only): {ai_note}")
                if allow_auto and self.mode == "AUTO" and self.settings.trading_env in {"paper", "bitget_demo"}:
                    self._auto_open(symbol, signal, messages)
            except Exception as exc:
                log.exception("Scan failed for %s", symbol)
                messages.append(f"{symbol}: ERROR {type(exc).__name__}: {exc}")
            finally:
                log.info("Finished evaluating %s", symbol)

        if self.settings.trading_env == "paper":
            closed = self.paper.mark(prices)
            for trade_id, reason, pnl in closed:
                messages.append(f"PAPER CLOSE #{trade_id}: {reason} PnL={pnl:.4f}")
        else:
            closed = self.execution.reconcile()
            for trade_id, reason, pnl, exit_price in closed:
                messages.append(f"DEMO CLOSE #{trade_id}: {reason} exit={exit_price:.8g} PnL={pnl:.4f}")
        log.info("SCAN END")
        return "\n".join(messages) or "لا توجد عملات مفعلة."

    def manual_open(self, symbol, direction, entry, stop, target):
        signal = {
            "direction": direction,
            "score": self.settings.min_technical_score,
            "price": entry,
            "stop": stop,
            "target": target,
            "setup": "MANUAL",
            "snapshot": {"source": "telegram_manual"},
        }
        ok, why = risk_check(self.settings, self.db, signal, self.paper.capital)
        if not ok:
            raise ValueError(why)
        if self.settings.trading_env == "paper":
            qty = self.paper_qty(entry, stop)
            tid = self.db.open_trade(symbol, direction, entry, stop, target, qty, mode="MANUAL", source="PAPER", snapshot={"source": "telegram_manual"})
            return f"PAPER OPEN #{tid} — {symbol} {direction} qty={qty:.8g} SL={stop:.8g} TP={target:.8g}"
        tid, result = self.execution.open(symbol, signal, mode="MANUAL", profile=self.profile)
        if not tid:
            raise ValueError(result)
        return result

    def close_all(self):
        if self.settings.trading_env == "paper":
            prices = self.get_prices(self.db.enabled_symbols())
            return self.paper.close_all(prices)
        return self.execution.close_all()

    def backtest_symbol(self, symbol):
        symbol = self.market.validate_symbol(symbol)
        main = self.market.candles(symbol, self.settings.timeframe_main, 1000)
        trend = self.market.candles(symbol, self.settings.timeframe_trend, 1000)
        context = self.market.candles(symbol, self.settings.timeframe_context, 1000)
        return run_backtest(main, trend, context, risk_per_trade=self.settings.risk_per_trade)

    def status_text(self):
        exchange_status = "LOCAL PAPER" if self.settings.trading_env == "paper" else (
            "BITGET DEMO ARMED" if self.execution.enabled else "BITGET DEMO READ-ONLY"
        )
        return (
            f"Khtb Lab v{__import__('khtb_lab').__version__}\n"
            f"Environment: {self.settings.trading_env}\n"
            f"Mode: {self.mode}\n"
            f"Profile: {self.profile}\n"
            f"Symbols: {len(self.db.enabled_symbols())}\n"
            f"Open trades: {len(self.db.open_trades())}\n"
            f"Paper equity: {self.paper.capital:.2f}\n"
            f"Today's realized PnL: {self.db.daily_pnl():.4f}\n"
            f"Leverage: {self.settings.leverage}x (max {self.settings.max_leverage}x)\n"
            f"Risk/trade: {self.settings.risk_per_trade:.2%}\n"
            f"Max margin/trade: {self.settings.max_margin_per_trade:.2f} USDT\n"
            f"Exchange: {exchange_status}"
        )

    def run(self, telegram=None):
        log.info("Khtb Lab started; env=%s mode=%s profile=%s", self.settings.trading_env, self.mode, self.profile)
        while True:
            if telegram:
                telegram.poll_once()
            else:
                log.info("\n%s", self.scan_once(allow_auto=True))
                time.sleep(max(15, self.settings.scan_seconds))
