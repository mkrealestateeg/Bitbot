from __future__ import annotations

import logging
import time

from .config import Settings
from .database import Database
from .market import Market
from .strategy import analyze
from .risk import risk_check, position_size
from .paper import PaperBroker
from .execution import ExecutionEngine
from .ai_review import AIReviewer
from .backtest import run_backtest

log = logging.getLogger(__name__)


class KhtbLab:
    def __init__(self, settings):
        settings.validate()
        self.settings = settings
        self.db = Database(settings.db_path)
        self.db.seed_symbols(settings.symbols)

        self.market = Market(settings)
        self.mode = settings.trading_mode
        self.profile = "SAFE"
        self.paper = PaperBroker(settings, self.db)
        self.execution = ExecutionEngine(settings)
        self.ai = AIReviewer(settings)

        self.last_scan = "Never"
        self.last_result = "Not scanned"
        self.telegram = None

        self.market.load()

        log.info(
            "BOT INITIALIZED | env=%s | mode=%s | symbols=%s | "
            "AI_enabled=%s | technical_threshold=%s",
            self.settings.trading_env,
            self.mode,
            len(self.db.enabled_symbols()),
            self.settings.enable_ai_review,
            self.settings.ai_min_tech_score,
        )

    def paper_qty(self, entry, stop):
        risk_fraction = self.settings.risk_per_trade * (
            0.5 if self.profile == "SAFE" else 1
        )
        return position_size(
            self.paper.capital,
            entry,
            stop,
            risk_fraction,
        )

    def get_prices(self, symbols):
        prices = {}
        for symbol in symbols:
            try:
                ticker = self.market.ticker(symbol)
                prices[symbol] = float(ticker.get("last") or 0)
            except Exception:
                log.exception("TICKER FAILED | symbol=%s", symbol)
        return prices

    def notify(self, message):
        if self.telegram:
            try:
                self.telegram.broadcast(message)
            except Exception:
                log.exception("TELEGRAM NOTIFICATION FAILED")

    def opportunity_text(self, symbol, signal, ai=None):
        ai = ai or {}
        decision = ai.get("decision", "NOT_REVIEWED")
        confidence = ai.get("confidence", 0)
        comment = ai.get("comment", "")
        ai_status = ai.get("status", "UNKNOWN")

        return (
            f"🟡 OPPORTUNITY READY\n"
            f"{symbol}\n"
            f"Direction: {signal['direction']}\n"
            f"Technical: {signal['score']:.0f}\n"
            f"Setup: {signal.get('setup', '')}\n"
            f"AI: {decision} ({confidence:.0f}%)\n"
            f"AI status: {ai_status}\n"
            f"Entry: {signal['price']:.8g}\n"
            f"SL: {signal['stop']:.8g}\n"
            f"TP: {signal['target']:.8g}\n"
            f"{comment}"
        )

    def _review_ai(self, symbol, signal):
        log.info(
            "AI REVIEW START | symbol=%s | setup=%s | score=%.1f",
            symbol,
            signal.get("setup", ""),
            float(signal.get("score", 0)),
        )

        result = self.ai.review(symbol, signal)

        if not isinstance(result, dict):
            log.error(
                "AI REVIEW INVALID RESULT | symbol=%s | result_type=%s",
                symbol,
                type(result).__name__,
            )
            return {
                "decision": "NOT_REVIEWED",
                "confidence": 0,
                "comment": "AI returned an invalid result; trade not approved.",
                "status": "ERROR",
            }

        log.info(
            "AI REVIEW RESULT | symbol=%s | decision=%s | status=%s | "
            "confidence=%s",
            symbol,
            result.get("decision", "NOT_REVIEWED"),
            result.get("status", "UNKNOWN"),
            result.get("confidence", 0),
        )
        return result

    def _record_confirmed_fill(self, symbol, direction, signal, result):
        """Persist an exchange-confirmed fill before reporting it to Telegram."""

        average = float(result["average"])
        filled_quantity = float(result["filled_quantity"])

        if average <= 0 or filled_quantity <= 0:
            raise ValueError(
                "Cannot record a confirmed fill without positive "
                "average price and filled quantity"
            )

        trade_id = self.db.open_trade(
            symbol,
            direction,
            average,
            result.get("stop", signal["stop"]),
            result.get("target", signal["target"]),
            filled_quantity,
            mode="AUTO",
            source=self.settings.trading_env,
            snapshot=signal.get("snapshot"),
        )

        log.info(
            "EXCHANGE FILL RECORDED | symbol=%s | trade_id=%s | "
            "order_id=%s | average=%s | quantity=%s",
            symbol,
            trade_id,
            result.get("order_id"),
            average,
            filled_quantity,
        )
        return trade_id

    def _notify_execution_result(self, symbol, direction, signal, result):
        """Send truthful notifications about fills and protection status."""

        protection = result.get("protection") or {}
        protection_ok = protection.get("ok") is True

        try:
            trade_id = self._record_confirmed_fill(
                symbol,
                direction,
                signal,
                result,
            )
        except Exception:
            log.exception(
                "CRITICAL: EXCHANGE FILL COULD NOT BE RECORDED | "
                "symbol=%s | order_id=%s",
                symbol,
                result.get("order_id"),
            )
            self.notify(
                f"🚨 CRITICAL: EXCHANGE POSITION FILLED\n"
                f"{symbol} {direction}\n"
                f"Order: {result.get('order_id', 'unknown')}\n"
                f"Average: {result.get('average', 'unknown')}\n"
                f"Quantity: {result.get('filled_quantity', 'unknown')}\n"
                f"Database recording failed. Check Bitget positions "
                f"and the bot database immediately."
            )
            return

        if protection_ok:
            self.notify(
                f"✅ TRADE FILLED — SL/TP CONFIRMED\n"
                f"{symbol} {direction}\n"
                f"Order: {result.get('order_id', 'unknown')}\n"
                f"Protection order: "
                f"{result.get('protection_order_id', 'unknown')}\n"
                f"Entry fill: {result['average']:.8g}\n"
                f"Filled quantity: {result['filled_quantity']:.8g}\n"
                f"SL: {result.get('stop', signal['stop'])}\n"
                f"TP: {result.get('target', signal['target'])}\n"
                f"Trade ID: {trade_id}\n"
                f"Environment: {self.settings.trading_env}"
            )
            return

        log.critical(
            "POSITION OPEN; SL/TP UNCONFIRMED | symbol=%s | "
            "order_id=%s | trade_id=%s | protection=%s",
            symbol,
            result.get("order_id"),
            trade_id,
            protection,
        )

        self.notify(
            f"🚨 WARNING: POSITION OPEN — PROTECTION UNCONFIRMED\n"
            f"{symbol} {direction}\n"
            f"Order: {result.get('order_id', 'unknown')}\n"
            f"Protection order: "
            f"{protection.get('order_id', 'unknown')}\n"
            f"Entry fill: {result['average']:.8g}\n"
            f"Filled quantity: {result['filled_quantity']:.8g}\n"
            f"Trade ID: {trade_id}\n"
            f"SL confirmed: {protection.get('stop_confirmed', False)}\n"
            f"TP confirmed: {protection.get('target_confirmed', False)}\n"
            f"Protection status: {protection.get('status', 'unknown')}\n"
            f"Reason: {protection.get('reason', 'unknown')}\n"
            f"Check the open position and set/verify SL and TP "
            f"on Bitget immediately. Do not assume it is protected."
        )

    def _process_symbol(self, symbol, prices, allow_auto):
        log.info("SCAN START | symbol=%s", symbol)

        main_frame = self.market.candles(
            symbol, self.settings.timeframe_main, 250
        )
        trend_frame = self.market.candles(
            symbol, self.settings.timeframe_trend, 250
        )
        context_frame = self.market.candles(
            symbol, self.settings.timeframe_context, 250
        )

        log.info(
            "MARKET DATA READY | symbol=%s | main=%s | trend=%s | context=%s",
            symbol,
            len(main_frame),
            len(trend_frame),
            len(context_frame),
        )

        signal = analyze(main_frame, trend_frame, context_frame)

        direction = str(signal.get("direction", "NEUTRAL")).upper()
        score = float(signal.get("score", 0) or 0)
        threshold = float(self.settings.ai_min_tech_score)

        entry = float(signal.get("price", 0) or 0)

        # Refresh the entry only for a directional signal that passes
        # the technical threshold, avoiding unnecessary ticker requests.
        if direction in {"LONG", "SHORT"} and score >= threshold:
            try:
                original_entry = float(signal.get("price", 0) or 0)
                original_stop = float(signal.get("stop", 0) or 0)
                original_target = float(signal.get("target", 0) or 0)

                if min(original_entry, original_stop, original_target) <= 0:
                    raise ValueError("Original entry/SL/TP must be positive")

                if direction == "LONG" and not (
                    original_stop < original_entry < original_target
                ):
                    raise ValueError("Original LONG levels are invalid")

                if direction == "SHORT" and not (
                    original_target < original_entry < original_stop
                ):
                    raise ValueError("Original SHORT levels are invalid")

                ticker = self.market.ticker(symbol)
                live_price = float(ticker.get("last") or 0)

                if live_price <= 0 or live_price == float("inf"):
                    raise ValueError("Live ticker price is invalid")

                # Move the planned entry to the current market price while
                # preserving the original absolute SL/TP distances.
                shift = live_price - original_entry

                new_stop = original_stop + shift
                new_target = original_target + shift

                if direction == "LONG" and not (
                    0 < new_stop < live_price < new_target
                ):
                    raise ValueError(
                        "Rebased LONG levels are invalid at live price"
                    )

                if direction == "SHORT" and not (
                    0 < new_target < live_price < new_stop
                ):
                    raise ValueError(
                        "Rebased SHORT levels are invalid at live price"
                    )

                signal["price"] = live_price
                signal["stop"] = new_stop
                signal["target"] = new_target
                entry = live_price
                prices[symbol] = live_price

                log.info(
                    "LIVE ENTRY REFRESHED | symbol=%s | direction=%s | "
                    "old_entry=%s | live_entry=%s | SL=%s | TP=%s",
                    symbol,
                    direction,
                    original_entry,
                    live_price,
                    new_stop,
                    new_target,
                )

            except Exception as exc:
                log.warning(
                    "SIGNAL BLOCKED | symbol=%s | "
                    "reason=live_price_or_levels_invalid | error=%s",
                    symbol,
                    exc,
                )
                return
        else:
            # Keep the analyzed candle price for non-qualified signals.
            prices[symbol] = entry

        self.db.save_signal(
            symbol,
            direction,
            entry,
            score,
            signal.get("setup", ""),
            signal.get("reason", ""),
            signal.get("snapshot", {}),
        )

        log.info(
            "EVALUATION COMPLETE | symbol=%s | direction=%s | score=%.1f | "
            "setup=%s | entry=%s",
            symbol,
            direction,
            score,
            signal.get("setup", ""),
            entry,
        )

        if direction not in {"LONG", "SHORT"}:
            log.info(
                "EVALUATION FILTER | symbol=%s | reason=no_directional_signal",
                symbol,
            )
            return

        if score < threshold:
            log.info(
                "EVALUATION FILTER | symbol=%s | score=%.1f | threshold=%.1f",
                symbol,
                score,
                threshold,
            )
            return

        log.info(
            "OPPORTUNITY QUALIFIED | symbol=%s | direction=%s | score=%.1f",
            symbol,
            direction,
            score,
        )

        ai = self._review_ai(symbol, signal)

        if self.settings.notify_technical_opportunity:
            self.notify(self.opportunity_text(symbol, signal, ai))

        decision = str(ai.get("decision", "NOT_REVIEWED")).upper()
        ai_status = str(ai.get("status", "UNKNOWN")).upper()

        if decision not in {"TAKE", "CONSIDER"} or ai_status != "OK":
            log.info(
                "TRADE NOT APPROVED | symbol=%s | decision=%s | AI_status=%s",
                symbol,
                decision,
                ai_status,
            )
            return

        if not allow_auto or self.mode != "AUTO":
            log.info(
                "NO AUTOMATIC ORDER | symbol=%s | allow_auto=%s | mode=%s",
                symbol,
                allow_auto,
                self.mode,
            )
            return

        if self.settings.trading_env == "LIVE":
            log.warning(
                "LIVE ENVIRONMENT SELECTED | symbol=%s | "
                "execution subject to LIVE configuration gates",
                symbol,
            )

        if self.settings.trading_env == "PAPER":
            capital = self.paper.capital
        else:
            can_execute, execute_reason = self.execution.can_execute()
            if not can_execute:
                log.warning(
                    "EXECUTION GATE BLOCKED | symbol=%s | reason=%s",
                    symbol,
                    execute_reason,
                )
                return
            capital = self.execution.equity()

        if capital is None or capital <= 0:
            log.warning(
                "RISK BLOCK | symbol=%s | reason=equity_unavailable",
                symbol,
            )
            if self.mode == "AUTO":
                self.notify(
                    f"⚠️ {symbol} BLOCKED\n"
                    f"Account equity unavailable. No order was sent."
                )
            return

        ok, reason = risk_check(
            self.settings,
            self.db,
            signal,
            capital,
        )

        if not ok:
            log.warning(
                "RISK BLOCK | symbol=%s | reason=%s",
                symbol,
                reason,
            )
            return

        log.info(
            "RISK CHECK PASSED | symbol=%s | capital=%.4f",
            symbol,
            capital,
        )

        if any(
            trade["symbol"] == symbol
            for trade in self.db.open_trades()
        ):
            log.info(
                "ORDER SKIPPED | symbol=%s | reason=position_already_open",
                symbol,
            )
            return

        risk_fraction = self.settings.risk_per_trade * (
            0.5 if self.profile == "SAFE" else 1
        )

        quantity = position_size(
            capital,
            signal["price"],
            signal["stop"],
            risk_fraction,
        )

        if self.settings.trading_env != "PAPER":
            max_quantity_by_margin = (
                self.settings.max_margin_per_trade
                * self.settings.leverage
                / max(signal["price"], 1e-12)
            )
            quantity = min(quantity, max_quantity_by_margin)

        if quantity <= 0:
            log.warning(
                "ORDER BLOCKED | symbol=%s | reason=quantity_not_positive",
                symbol,
            )
            return

        if self.settings.trading_env == "PAPER":
            trade_id = self.paper.open(symbol, signal, "AUTO")
            if trade_id:
                log.info(
                    "PAPER TRADE OPENED | symbol=%s | trade_id=%s",
                    symbol,
                    trade_id,
                )
                self.notify(
                    f"🟢 PAPER TRADE OPENED\n"
                    f"{symbol} {direction}\n"
                    f"Entry: {signal['price']:.8g}\n"
                    f"SL: {signal['stop']:.8g}\n"
                    f"TP: {signal['target']:.8g}"
                )
            else:
                log.warning(
                    "PAPER TRADE NOT OPENED | symbol=%s",
                    symbol,
                )
            return

        log.info(
            "ORDER PREPARATION START | symbol=%s | quantity=%s | env=%s",
            symbol,
            quantity,
            self.settings.trading_env,
        )

        plan = self.execution.prepare(
            symbol,
            direction,
            quantity,
            signal["price"],
            signal["stop"],
            signal["target"],
        )

        if not plan.get("ready"):
            log.warning(
                "ORDER PREPARATION BLOCKED | symbol=%s | reason=%s",
                symbol,
                plan.get("error", "unknown"),
            )
            return

        if self.settings.notify_auto_ready:
            self.notify(
                f"🟢 AUTO TRADE READY\n"
                f"{symbol} {direction}\n"
                f"Entry: {signal['price']:.8g}\n"
                f"SL: {signal['stop']:.8g}\n"
                f"TP: {signal['target']:.8g}\n"
                f"Qty: {quantity:.8g}\n"
                f"Environment: {self.settings.trading_env}\n"
                f"Submitting order..."
            )

        log.info(
            "ORDER SUBMISSION START | symbol=%s | direction=%s | quantity=%s",
            symbol,
            direction,
            quantity,
        )

        result = self.execution.execute(plan)

        if result.get("success"):
            self._notify_execution_result(
                symbol,
                direction,
                signal,
                result,
            )
            return

        # A partial fill or an uncertain order outcome may still represent
        # exchange exposure. Do not claim no order was executed.
        executed = bool(result.get("executed"))
        reconcile_required = bool(result.get("reconcile_required"))
        filled_quantity = float(result.get("filled_quantity") or 0)
        average = float(result.get("average") or 0)

        log.error(
            "ORDER NOT CONFIRMED SUCCESSFUL | symbol=%s | error=%s | "
            "executed=%s | filled_quantity=%s | reconcile_required=%s",
            symbol,
            result.get("error", "unknown"),
            executed,
            filled_quantity,
            reconcile_required,
        )

        if executed and filled_quantity > 0 and average > 0:
            self.notify(
                f"🚨 PARTIAL/UNCONFIRMED POSITION\n"
                f"{symbol} {direction}\n"
                f"Order: {result.get('order_id', 'unknown')}\n"
                f"Reported filled quantity: {filled_quantity:.8g}\n"
                f"Average price: {average:.8g}\n"
                f"Status: {result.get('order_status', 'unknown')}\n"
                f"Reconciliation required: {reconcile_required}\n"
                f"Check Bitget orders and positions immediately. "
                f"Do not assume the order is cancelled or protected."
            )
        else:
            self.notify(
                f"⚠️ ORDER NOT CONFIRMED\n"
                f"{symbol}\n"
                f"Error: {result.get('error', 'unknown')}\n"
                f"Reconciliation required: {reconcile_required}\n"
                f"Check Bitget order history before any manual retry."
            )

    def scan_once(self, allow_auto=True):
        symbols = self.db.enabled_symbols()
        prices = {}
        errors = 0

        log.info(
            "SCAN CYCLE START | symbols=%s | env=%s | mode=%s | "
            "allow_auto=%s",
            len(symbols),
            self.settings.trading_env,
            self.mode,
            allow_auto,
        )

        for symbol in symbols:
            try:
                self._process_symbol(symbol, prices, allow_auto)
            except Exception:
                errors += 1
                log.exception("SCAN FAILED | symbol=%s", symbol)

        if self.settings.trading_env == "PAPER":
            try:
                closed_trades = self.paper.mark(prices)
                for trade_id, reason, pnl in closed_trades:
                    log.info(
                        "PAPER TRADE CLOSED | trade_id=%s | reason=%s | pnl=%s",
                        trade_id,
                        reason,
                        pnl,
                    )
                    self.notify(
                        f"🔴 PAPER TRADE CLOSED\n"
                        f"Trade #{trade_id}\n"
                        f"{reason}\n"
                        f"PnL: {pnl:.6f}"
                    )
            except Exception:
                errors += 1
                log.exception("PAPER POSITION MONITOR FAILED")

        self.last_scan = time.strftime("%Y-%m-%d %H:%M:%S")
        self.last_result = (
            f"Completed scan; symbols={len(symbols)}; errors={errors}"
        )

        log.info(
            "SCAN CYCLE COMPLETE | symbols=%s | errors=%s | result=%s",
            len(symbols),
            errors,
            self.last_result,
        )
        return self.last_result

    def status_text(self):
        if self.settings.trading_env == "PAPER":
            equity = self.paper.capital
            available = self.paper.capital
        else:
            equity = self.execution.equity()
            available = self.execution.available()

        if self.settings.trading_env == "DEMO":
            armed = "YES" if self.settings.demo_armed else "NO"
        elif self.settings.trading_env == "LIVE":
            armed = "YES" if self.settings.live_armed else "NO"
        else:
            armed = "N/A (PAPER)"

        can_execute, execute_reason = (
            self.execution.can_execute()
            if self.settings.trading_env != "PAPER"
            else (False, "PAPER environment")
        )

        return (
            f"🤖 Khtb Lab {self.settings.bot_version}\n\n"
            f"Exchange: {self.settings.exchange_name}\n"
            f"Environment: {self.settings.trading_env}\n"
            f"Account: {self.settings.account_type} / "
            f"{self.settings.product_category}\n"
            f"Mode: {self.mode}\n"
            f"Profile: {self.profile}\n"
            f"Margin: {self.settings.margin_mode} | "
            f"Position: {self.settings.position_mode}\n"
            f"Execution Armed: {armed}\n"
            f"Order Sending: "
            f"{'ON' if self.settings.enable_exchange_orders else 'OFF'}\n"
            f"Execution Gate: "
            f"{'READY' if can_execute else execute_reason}\n"
            f"AI Review: "
            f"{'ON' if self.settings.enable_ai_review else 'OFF'}\n"
            f"AI Model: {self.settings.gemini_model}\n"
            f"Technical Threshold: {self.settings.ai_min_tech_score}\n"
            f"Symbols: {len(self.db.enabled_symbols())}\n"
            f"Open Trades: {len(self.db.open_trades())}\n"
            f"Equity: {equity if equity is not None else 0:.2f} USDT\n"
            f"Available: {available if available is not None else 0:.2f} USDT\n"
            f"Daily PnL: {self.db.daily_pnl():.4f}\n"
            f"Last Scan: {self.last_scan}\n"
            f"Last Result: {self.last_result}"
        )

    def backtest_symbol(self, symbol):
        symbol = self.market.validate_symbol(symbol)

        return run_backtest(
            self.market.candles(
                symbol, self.settings.timeframe_main, 1000
            ),
            self.market.candles(
                symbol, self.settings.timeframe_trend, 1000
            ),
            self.market.candles(
                symbol, self.settings.timeframe_context, 1000
            ),
        )

    def run(self, telegram=None):
        self.telegram = telegram

        log.info(
            "KHTB LAB STARTED | env=%s | mode=%s | exchange=%s",
            self.settings.trading_env,
            self.mode,
            self.settings.exchange_name,
        )

        while True:
            try:
                if telegram:
                    telegram.poll_once()
            except Exception:
                log.exception("TELEGRAM POLLING FAILED")

            try:
                self.scan_once(allow_auto=True)
            except Exception:
                log.exception("SCAN CYCLE FAILED")

            time.sleep(max(5, self.settings.scan_seconds))
