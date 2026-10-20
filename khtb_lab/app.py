from __future__ import annotations

import logging
import math
import threading
import time
from decimal import Decimal

import pandas as pd

from .config import Settings
from .database import Database
from .indicators import enrich
from .market import Market
from .strategy import MIN_REWARD_RISK, STOP_BUFFER_ATR, analyze
from .risk import risk_check, position_size
from .paper import PaperBroker
from .execution import ExecutionEngine
from .ai_review import AIReviewer
from .backtest import run_backtest
from .local_learning import LocalTradeLearner, capture_entry_snapshot

log = logging.getLogger(__name__)


class KhtbLab:
    PROFIT_LOCK_STAGE = 1
    TARGET_REMOVED_STAGE = 2
    TARGET_CROSSED_STAGE = 3

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
        self.local_learner = LocalTradeLearner(self.db)

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

    def current_price(self, symbol):
        ticker = self.market.ticker(symbol)
        price = float(ticker.get("last") or 0)
        if not math.isfinite(price) or price <= 0:
            raise ValueError(f"Invalid live price for {symbol}")
        return price

    @staticmethod
    def trade_pnl(trade, price):
        direction = 1 if trade["direction"] == "LONG" else -1
        return (float(price) - float(trade["entry"])) * float(
            trade["qty"]
        ) * direction

    def trade_milestones(self, trade, price):
        direction = 1 if trade["direction"] == "LONG" else -1
        entry = float(trade["entry"])
        stop = float(trade["stop"])
        target = float(trade["target"])
        risk = float(trade.get("initial_risk") or abs(entry - stop))
        progress = (float(price) - entry) * direction
        events = []
        target_distance = abs(target - entry)
        if target_distance > 0 and progress >= target_distance * 0.9:
            events.append((
                "near_target",
                f"🎯 {trade['symbol']} اقترب من الهدف: "
                f"السعر {price:.8g}، الهدف {target:.8g}.",
            ))
        if risk > 0 and progress >= risk:
            events.append((
                "tp1_reached",
                f"✅ {trade['symbol']} وصل إلى TP1 (1R) عند "
                f"{price:.8g}.",
            ))
        if (direction > 0 and stop >= entry) or (
            direction < 0 and stop <= entry
        ):
            events.append((
                "breakeven",
                f"🛡 وقف {trade['symbol']} أصبح عند نقطة الدخول أو "
                "في منطقة ربح.",
            ))
        return events

    @staticmethod
    def _favorable_price(direction, previous, current):
        if direction == "LONG":
            return max(float(previous or current), float(current))
        return min(float(previous or current), float(current))

    def _favorable_price_from_candles(
        self, trade, frame, current_price, previous
    ):
        direction = str(trade.get("direction") or "").upper()
        favorable = self._favorable_price(
            direction, previous, current_price
        )
        opened_at = trade.get("ts_open")
        if not opened_at:
            return favorable

        try:
            opened_at = pd.to_datetime(opened_at, utc=True)
            post_entry = frame.loc[frame["timestamp"] > opened_at]
            if post_entry.empty:
                return favorable
            if direction == "LONG":
                candle_extreme = float(post_entry["high"].max())
                return max(favorable, candle_extreme)
            if direction == "SHORT":
                candle_extreme = float(post_entry["low"].min())
                return min(favorable, candle_extreme)
        except (KeyError, TypeError, ValueError):
            log.exception(
                "Could not derive favorable price from candles | "
                "trade_id=%s",
                trade.get("id"),
            )
        return favorable

    def _structural_trailing_stop(self, trade, frame, current_price, atr):
        direction = str(trade.get("direction") or "").upper()
        if direction not in {"LONG", "SHORT"} or atr <= 0:
            return None

        opened_at = trade.get("ts_open")
        if not opened_at:
            return None
        try:
            opened_at = pd.to_datetime(opened_at, utc=True)
            closed = frame.iloc[:-1]
            history = closed.loc[closed["timestamp"] >= opened_at].tail(80)
        except (KeyError, TypeError, ValueError):
            log.exception(
                "Cannot locate closed candles for structural trailing | "
                "trade_id=%s",
                trade.get("id"),
            )
            return None

        if len(history) < 5:
            return None

        entry = float(trade["entry"])
        current_stop = float(trade["stop"])
        candidates = []
        for index in range(2, len(history) - 2):
            window = history.iloc[index - 2:index + 3]
            candle = history.iloc[index]
            if direction == "LONG":
                level = float(candle["low"])
                is_pivot = (
                    level <= float(window["low"].min())
                    and level < float(window["low"].max())
                )
                stop = level - STOP_BUFFER_ATR * atr
                eligible = (
                    is_pivot
                    and stop > current_stop
                    and stop > entry
                    and stop < current_price
                )
            else:
                level = float(candle["high"])
                is_pivot = (
                    level >= float(window["high"].max())
                    and level > float(window["high"].min())
                )
                stop = level + STOP_BUFFER_ATR * atr
                eligible = (
                    is_pivot
                    and stop < current_stop
                    and stop < entry
                    and stop > current_price
                )
            if not eligible:
                continue

            try:
                rounded_text = self.execution._precision_price(
                    trade["symbol"], stop
                )
                rounded_decimal = Decimal(str(rounded_text))
                stop_decimal = Decimal(str(stop))
                tick = Decimal(1).scaleb(
                    rounded_decimal.as_tuple().exponent
                )
                if direction == "LONG" and rounded_decimal > stop_decimal:
                    rounded_decimal -= tick
                elif direction == "SHORT" and rounded_decimal < stop_decimal:
                    rounded_decimal += tick
                rounded_stop = float(
                    self.execution._precision_price(
                        trade["symbol"], float(rounded_decimal)
                    )
                )
            except (AttributeError, TypeError, ValueError):
                log.exception(
                    "Could not apply exchange price precision to trailing "
                    "stop | trade_id=%s",
                    trade.get("id"),
                )
                return None

            conservative = (
                rounded_stop <= stop
                if direction == "LONG"
                else rounded_stop >= stop
            )
            still_valid = (
                current_stop < rounded_stop < current_price
                and rounded_stop > entry
                if direction == "LONG"
                else current_price < rounded_stop < current_stop
                and rounded_stop < entry
            )
            if conservative and still_valid:
                candidates.append((candle["timestamp"], rounded_stop))

        if not candidates:
            return None
        return max(candidates, key=lambda item: item[0])[1]

    def _manage_exchange_trades(self):
        """Advance protection only for positions confirmed by reconciliation."""
        trades = self.db.open_trades(self.settings.trading_env)
        if not trades:
            return True
        if not getattr(self.execution, "positions_reconciled", False):
            return False

        all_ok = True
        for trade in trades:
            try:
                frame = self.market.candles(
                    trade["symbol"], self.settings.timeframe_main, 120
                )
                if len(frame) < 8:
                    raise ValueError("insufficient_closed_candles")

                enriched = enrich(frame)
                closed = enriched.iloc[:-1]
                atr = float(closed.iloc[-1]["atr"])
                if not math.isfinite(atr) or atr <= 0:
                    raise ValueError("invalid_trailing_atr")

                price = self.current_price(trade["symbol"])
                direction = str(trade["direction"]).upper()
                if direction not in {"LONG", "SHORT"}:
                    raise ValueError("invalid_position_direction")
                entry = float(trade["entry"])
                initial_risk = float(
                    trade.get("initial_risk")
                    or abs(entry - float(trade["stop"]))
                )
                if not math.isfinite(initial_risk) or initial_risk <= 0:
                    raise ValueError("invalid_initial_trade_risk")

                stage = int(trade.get("management_stage") or 0)
                favorable = self._favorable_price_from_candles(
                    trade,
                    frame,
                    price,
                    trade.get("favorable_price") or entry,
                )
                signed_direction = 1 if direction == "LONG" else -1
                favorable_progress = (
                    (favorable - entry) * signed_direction
                )
                risk_multiple = favorable_progress / initial_risk
                target_distance = (
                    (float(trade["target"]) - entry)
                    * (1 if direction == "LONG" else -1)
                )
                if target_distance <= 0:
                    raise ValueError("invalid_virtual_tp2")

                stop = float(trade["stop"])
                just_activated = False
                if stage == 0 and risk_multiple >= 1.0:
                    stage = self.PROFIT_LOCK_STAGE
                    just_activated = True
                    log.info(
                        "STRUCTURAL TRAILING ACTIVATED | trade_id=%s | "
                        "symbol=%s | favorable_r=%.4f",
                        trade["id"],
                        trade["symbol"],
                        risk_multiple,
                    )
                    self.notify(
                        f"🛡 {trade['symbol']} وصل إلى "
                        f"{risk_multiple:.2f}R من المخاطرة الأصلية؛ بدأ تتبع "
                        "الوقف خلف القمم/القيعان المؤكدة."
                    )

                candidate_stop = None
                if stage >= self.PROFIT_LOCK_STAGE:
                    candidate_stop = self._structural_trailing_stop(
                        trade, frame, price, atr
                    )

                target_near = (
                    favorable_progress >= target_distance * 0.90
                )
                stop_now = candidate_stop or stop
                locked_profit = (
                    stop_now > entry
                    if direction == "LONG"
                    else stop_now < entry
                )
                remove_target = (
                    stage >= self.PROFIT_LOCK_STAGE
                    and stage < self.TARGET_REMOVED_STAGE
                    and target_near
                    and locked_profit
                )
                stop_changed = candidate_stop is not None
                if stop_changed or remove_target:
                    trade_for_update = {
                        **trade,
                        "management_stage": stage,
                    }
                    result = self.execution.update_tpsl_protection(
                        trade_for_update,
                        stop_now,
                        (
                            None
                            if remove_target
                            or stage >= self.TARGET_REMOVED_STAGE
                            else trade["target"]
                        ),
                    )
                    if not result.get("ok"):
                        all_ok = False
                        self.execution.positions_reconciled = False
                        if not self.db.save_trade_management(
                            trade["id"],
                            stage,
                            favorable,
                            stop,
                        ):
                            log.error(
                                "Could not persist pending protection "
                                "management state | trade_id=%s",
                                trade["id"],
                            )
                        log.critical(
                            "STRUCTURAL PROTECTION UPDATE FAILED | "
                            "trade_id=%s | symbol=%s | reason=%s",
                            trade["id"],
                            trade["symbol"],
                            result.get("reason"),
                        )
                        self.notify(
                            f"🚨 تعذّر تأكيد تعديل حماية "
                            f"{trade['symbol']} على Bitget. بقيت الحماية "
                            "السابقة كما هي؛ أوقفت أوامر AUTO الجديدة "
                            "حتى نجاح المصالحة."
                        )
                        continue

                    stop = stop_now
                    if remove_target:
                        stage = self.TARGET_REMOVED_STAGE
                        log.info(
                            "VIRTUAL TP2 ACTIVATED | exchange TP removed "
                            "and SL confirmed | trade_id=%s | symbol=%s | "
                            "stop=%s | virtual_tp2=%s",
                            trade["id"],
                            trade["symbol"],
                            stop,
                            trade["target"],
                        )
                        self.notify(
                            f"🎯 {trade['symbol']} وصل إلى 90% من مسافة "
                            "TP2 البنيوي: أزيل هدف المنصة بعد تأكيد بقاء "
                            f"وقف الحماية عند {stop:.8g}. سيبقى المركز "
                            "كاملًا مفتوحًا ويستمر التتبع البنيوي."
                        )
                    elif stop_changed:
                        log.info(
                            "STRUCTURAL STOP ADVANCED | trade_id=%s | "
                            "symbol=%s | old_stop=%s | new_stop=%s | "
                            "virtual_tp2=%s",
                            trade["id"],
                            trade["symbol"],
                            trade["stop"],
                            stop,
                            trade["target"],
                        )

                if (
                    stage >= self.TARGET_REMOVED_STAGE
                    and stage < self.TARGET_CROSSED_STAGE
                    and favorable_progress >= target_distance
                ):
                    stage = self.TARGET_CROSSED_STAGE
                    log.info(
                        "VIRTUAL TP2 CROSSED | trade_id=%s | symbol=%s | "
                        "price=%s | trailing_continues=true",
                        trade["id"],
                        trade["symbol"],
                        price,
                    )
                    self.notify(
                        f"🚀 {trade['symbol']} تجاوز TP2 البنيوي الافتراضي "
                        f"عند {trade['target']:.8g}. المركز ما زال كاملًا "
                        "مفتوحًا والوقف يتبع القيعان/القمم المؤكدة."
                    )

                if just_activated and candidate_stop is None:
                    self.notify(
                        f"ℹ️ {trade['symbol']}: لم يتكوّن بعد ارتداد "
                        "بنيوي مؤكد يتيح رفع الوقف بأمان؛ وقف Bitget الحالي "
                        "لم يتغير."
                    )

                if not self.db.save_trade_management(
                    trade["id"],
                    stage,
                    favorable,
                    stop,
                    result.get("order_id") if stop_changed or remove_target else None,
                    result.get("client_oid") if stop_changed or remove_target else None,
                ):
                    raise RuntimeError("trade_management_state_not_saved")

            except Exception:
                all_ok = False
                log.exception(
                    "OPEN TRADE MANAGEMENT FAILED | trade_id=%s | symbol=%s",
                    trade.get("id"),
                    trade.get("symbol"),
                )
                self.notify(
                    f"🚨 تعذّرت متابعة إدارة الحماية للصفقة "
                    f"{trade.get('symbol', 'unknown')}. لم يُرسل تعديل "
                    "غير مؤكد، وتم إيقاف أوامر AUTO الجديدة لهذه الدورة."
                )
        return all_ok

    def close_trade_from_telegram(self, trade_id):
        trade = next(
            (
                item
                for item in self.db.open_trades(
                    self.settings.trading_env
                )
                if int(item["id"]) == int(trade_id)
            ),
            None,
        )
        if trade is None:
            return {"success": False, "error": "trade_is_not_open"}

        try:
            price = self.current_price(trade["symbol"])
            if self.settings.trading_env == "PAPER":
                pnl = self.paper.close_trade(trade, price)
                if pnl is None:
                    return {"success": False, "error": "trade_is_not_open"}
            else:
                result = self.execution.close_position(trade)
                if not result.get("success"):
                    return result
                pnl = self.trade_pnl(trade, price)
                close_reason = (
                    "EXCHANGE_CLOSED_ESTIMATED"
                    if result.get("already_closed")
                    else "MANUAL_CLOSE_ESTIMATED"
                )
                if not self.db.close_trade(
                    trade["id"],
                    price,
                    pnl,
                    close_reason,
                ):
                    return {
                        "success": False,
                        "error": "trade_database_close_failed",
                    }
            closed_trade = next(
                item
                for item in self.db.recent_trades(10)
                if int(item["id"]) == int(trade_id)
            )
            return {
                "success": True,
                "trade": closed_trade,
                "price": price,
                "pnl": pnl,
                "reason": closed_trade.get("close_reason"),
            }
        except Exception as exc:
            log.exception(
                "Telegram individual trade close failed | trade_id=%s",
                trade_id,
            )
            return {
                "success": False,
                "error": f"{type(exc).__name__}:{exc}",
                "reconcile_required": True,
            }

    def _settle_missing_exchange_trades(self, reconciliation):
        live_keys = reconciliation.get("live_position_keys")
        if not isinstance(live_keys, list):
            return 0
        live_keys = {tuple(key) for key in live_keys}
        settled = 0
        for trade in self.db.open_trades(self.settings.trading_env):
            key = (
                self.execution._normalized_symbol(trade["symbol"]),
                str(trade["direction"]).upper(),
            )
            if key in live_keys:
                continue
            try:
                price = self.current_price(trade["symbol"])
                pnl = self.trade_pnl(trade, price)
                if not self.db.close_trade(
                    trade["id"],
                    price,
                    pnl,
                    "EXCHANGE_CLOSED_ESTIMATED",
                ):
                    continue
                settled += 1
                closed_trade = {
                    **trade,
                    "status": "CLOSED",
                    "pnl": pnl,
                    "close_reason": "EXCHANGE_CLOSED_ESTIMATED",
                }
                if self.telegram:
                    self.telegram.publish_trade_status(
                        closed_trade,
                        price,
                        pnl,
                        closed=True,
                        reason="Exchange position closed; estimated result",
                    )
                log.info(
                    "EXCHANGE POSITION CLOSED; ESTIMATE RECORDED | "
                    "trade_id=%s | symbol=%s | price=%s | pnl=%s",
                    trade["id"],
                    trade["symbol"],
                    price,
                    pnl,
                )
            except Exception:
                log.exception(
                    "Could not settle missing exchange position | "
                    "trade_id=%s",
                    trade["id"],
                )
        return settled

    def _sync_exchange_protection(self, reconciliation):
        trades = {
            int(trade["id"]): trade
            for trade in self.db.open_trades(self.settings.trading_env)
        }
        for update in reconciliation.get("protection_updates", []):
            trade = trades.get(int(update["trade_id"]))
            if trade and self.db.save_trade_management(
                trade["id"],
                int(
                    update.get("management_stage")
                    or trade.get("management_stage")
                    or 0
                ),
                float(
                    update.get("favorable_price")
                    or trade.get("favorable_price")
                    or trade["entry"]
                ),
                update["stop"],
                update.get("order_id"),
                update.get("client_oid"),
            ):
                log.info(
                    "SAFER EXCHANGE STOP SYNCHRONIZED | trade_id=%s | "
                    "stop=%s | protection_order_id=%s",
                    update["trade_id"],
                    update["stop"],
                    update.get("order_id"),
                )

    def opportunity_text(self, symbol, signal, ai=None):
        ai = ai or {}
        decision = ai.get("decision", "NOT_REVIEWED")
        confidence = ai.get("confidence", 0)
        comment = ai.get("comment", "")
        ai_status = ai.get("status", "UNKNOWN")
        local = signal.get("local_learning_prediction")
        local_text = (
            f"Unvalidated local estimate: "
            f"{local['win_probability']:.0%} "
            f"({local['samples']} closed trades; advisory only, "
            f"not calibrated)\n"
            if isinstance(local, dict)
            else "Local model: gathering completed trade data\n"
        )

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
            f"{local_text}"
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

        learning_signal = {
            **signal,
            "price": average,
            "stop": result.get("stop", signal["stop"]),
            "target": result.get("target", signal["target"]),
        }
        trade_id = self.db.open_trade(
            symbol,
            direction,
            average,
            result.get("stop", signal["stop"]),
            result.get("target", signal["target"]),
            filled_quantity,
            mode="AUTO",
            source=self.settings.trading_env,
            snapshot=capture_entry_snapshot(
                learning_signal, self.settings.trading_env
            ),
            protection_order_id=(result.get("protection") or {}).get(
                "order_id"
            ),
            protection_client_oid=(result.get("protection") or {}).get(
                "client_oid"
            ),
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

                # Keep chart-derived levels fixed; reject entries that have
                # already invalidated the structure or consumed target room.
                new_stop = original_stop
                new_target = original_target

                if direction == "LONG" and not (
                    0 < new_stop < live_price < new_target
                ):
                    raise ValueError(
                        "Chart-based LONG levels are invalid at live price"
                    )

                if direction == "SHORT" and not (
                    0 < new_target < live_price < new_stop
                ):
                    raise ValueError(
                        "Chart-based SHORT levels are invalid at live price"
                    )

                risk_distance = abs(live_price - new_stop)
                reward_distance = abs(new_target - live_price)
                if (
                    risk_distance <= 0
                    or reward_distance / risk_distance < MIN_REWARD_RISK
                ):
                    raise ValueError(
                        "Live entry leaves insufficient structural target room"
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

        local_prediction = self.local_learner.predict(
            self.settings.trading_env, signal
        )

        log.info(
            "OPPORTUNITY QUALIFIED | symbol=%s | direction=%s | score=%.1f",
            symbol,
            direction,
            score,
        )

        if any(
            self.execution._normalized_symbol(trade["symbol"])
            == self.execution._normalized_symbol(symbol)
            for trade in self.db.open_trades(self.settings.trading_env)
        ):
            log.info(
                "OPPORTUNITY SUPPRESSED | symbol=%s | "
                "reason=position_lifecycle_active",
                symbol,
            )
            return

        ai = self._review_ai(symbol, signal)

        if self.settings.notify_technical_opportunity:
            opportunity_signal = dict(signal)
            if local_prediction:
                opportunity_signal["local_learning_prediction"] = (
                    local_prediction
                )
            self.notify(
                self.opportunity_text(symbol, opportunity_signal, ai)
            )

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
            self.execution._normalized_symbol(trade["symbol"])
            == self.execution._normalized_symbol(symbol)
            for trade in self.db.open_trades(self.settings.trading_env)
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

        try:
            self.local_learner.refresh(self.settings.trading_env)
        except Exception:
            errors += 1
            log.exception("LOCAL TRADE MODEL REFRESH FAILED")

        if self.settings.trading_env != "PAPER":
            reconciliation = self.execution.reconcile_positions(
                self.db.open_trades(self.settings.trading_env)
            )
            self._sync_exchange_protection(reconciliation)
            if not reconciliation.get("ok") and self._settle_missing_exchange_trades(
                reconciliation
            ):
                reconciliation = self.execution.reconcile_positions(
                    self.db.open_trades(self.settings.trading_env)
                )
                self._sync_exchange_protection(reconciliation)
            if not reconciliation.get("ok"):
                errors += 1
                allow_auto = False
                log.critical(
                    "AUTO EXECUTION BLOCKED | exchange_state=%s | "
                    "mismatches=%s",
                    reconciliation.get("reason"),
                    reconciliation.get("mismatches"),
                )
                if self.mode == "AUTO":
                    self.notify(
                        "🚨 AUTO TRADING PAUSED FOR THIS SCAN\n"
                        "Bitget positions/protection do not match the "
                        "bot's recorded trades, or reconciliation failed.\n"
                        f"Details: {reconciliation.get('mismatches')}\n"
                        "Check open positions and SL/TP on Bitget. "
                        "No new automatic entries will be submitted."
                    )
            else:
                log.info(
                    "EXCHANGE STATE RECONCILED | positions=%s",
                    reconciliation.get("live_positions", 0),
                )
                if not self._manage_exchange_trades():
                    errors += 1
                    allow_auto = False
                    log.critical(
                        "AUTO EXECUTION BLOCKED | reason="
                        "open_trade_protection_management_failed"
                    )

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
                    closed_trade = next(
                        (
                            trade
                            for trade in self.db.recent_trades(10)
                            if int(trade["id"]) == int(trade_id)
                        ),
                        None,
                    )
                    log.info(
                        "PAPER TRADE CLOSED | trade_id=%s | reason=%s | pnl=%s",
                        trade_id,
                        reason,
                        pnl,
                    )
                    if self.telegram and closed_trade:
                        self.telegram.publish_trade_status(
                            closed_trade,
                            prices[closed_trade["symbol"]],
                            pnl,
                            closed=True,
                            reason=reason,
                        )
                    else:
                        self.notify(
                            f"🔴 PAPER TRADE CLOSED\n"
                            f"Trade #{trade_id}\n"
                            f"{reason}\n"
                            f"PnL: {pnl:.6f}"
                        )
            except Exception:
                errors += 1
                log.exception("PAPER POSITION MONITOR FAILED")

        if self.telegram:
            self.telegram.sync_open_trade_statuses()

        try:
            learning_state = self.local_learner.refresh(
                self.settings.trading_env
            )
            log.info(
                "LOCAL TRADE MODEL STATUS | source=%s | status=%s | "
                "samples=%s",
                self.settings.trading_env,
                learning_state.get("status"),
                learning_state.get("samples"),
            )
        except Exception:
            errors += 1
            log.exception("LOCAL TRADE MODEL REFRESH FAILED")

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
        learning_status = self.local_learner.status(
            self.settings.trading_env
        )
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
            f"Open Trades: "
            f"{len(self.db.open_trades(self.settings.trading_env))}\n"
            f"Local Learning: "
            f"{learning_status.get('status')} "
            f"({learning_status.get('samples', 0)} samples)\n"
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
        stop_event = threading.Event()
        telegram_thread = None

        log.info(
            "KHTB LAB STARTED | env=%s | mode=%s | exchange=%s",
            self.settings.trading_env,
            self.mode,
            self.settings.exchange_name,
        )

        if telegram:
            def telegram_worker():
                while not stop_event.is_set():
                    try:
                        if not getattr(telegram, "started", False):
                            if not telegram.startup():
                                stop_event.wait(3)
                                continue
                        telegram.poll_once()
                    except Exception:
                        log.exception("TELEGRAM POLLING FAILED")
                        stop_event.wait(1)
                    else:
                        stop_event.wait(0.3)

            telegram_thread = threading.Thread(
                target=telegram_worker,
                name="TelegramWorker",
                daemon=True,
            )
            telegram_thread.start()
            log.info("Telegram background polling thread launched")

        try:
            while not stop_event.is_set():
                try:
                    self.scan_once(allow_auto=True)
                except Exception:
                    log.exception("SCAN CYCLE FAILED")

                stop_event.wait(max(5, self.settings.scan_seconds))
        finally:
            stop_event.set()
            if telegram_thread:
                telegram_thread.join(timeout=2)
                if telegram_thread.is_alive():
                    log.warning(
                        "Telegram polling thread is still stopping after "
                        "shutdown request"
                    )
