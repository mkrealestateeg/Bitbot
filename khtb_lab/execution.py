from __future__ import annotations

import logging
import math
import time
import uuid
from decimal import Decimal, InvalidOperation

import ccxt

from .account_adapter import BitgetUTAAdapter

log = logging.getLogger(__name__)


class ExecutionEngine:
    """Bitget UTA execution engine with fail-closed safety checks."""

    TERMINAL_FILLED = {"filled"}
    TERMINAL_FAILED = {"cancelled", "canceled", "rejected"}
    ACTIVE_STRATEGY_STATUSES = {
        "live",
        "not_trigger",
        "pending",
        "submitting",
    }

    def __init__(self, settings):
        self.settings = settings
        self.environment = str(settings.trading_env).upper()
        self.exchange = None
        self.account = None
        self.ready = False
        self.positions_reconciled = False

        if self.environment == "PAPER":
            return

        if settings.exchange_id != "bitget":
            log.error(
                "Unsupported direct execution exchange: %s",
                settings.exchange_id,
            )
            return

        self.account = BitgetUTAAdapter(settings)

        if not self.account.enabled:
            log.warning(
                "Bitget %s credentials missing; execution disabled",
                self.environment,
            )
            return

        try:
            self.exchange = ccxt.bitget({
                "enableRateLimit": True,
                "options": {"defaultType": "swap"},
            })
            self.exchange.load_markets()

            account_cfg = self.account.account_settings()
            actual_hold = str(
                account_cfg.get("holdMode") or ""
            ).lower()

            expected_hold = (
                "hedge_mode"
                if settings.position_mode == "hedge"
                else "one_way_mode"
            )

            if actual_hold and actual_hold != expected_hold:
                log.error(
                    "Position-mode mismatch: configured=%s exchange=%s",
                    expected_hold,
                    actual_hold,
                )
                return

            self.ready = True

            log.info(
                "UTA EXECUTION READY | env=%s | holdMode=%s",
                self.environment,
                actual_hold or expected_hold,
            )

        except Exception:
            log.exception(
                "Execution initialization failed; execution disabled"
            )

    # ---------------------------------------------------------
    # Execution gates
    # ---------------------------------------------------------

    def can_execute(self):
        s = self.settings

        if self.environment not in {"DEMO", "LIVE"}:
            return False, "unsupported_execution_environment"

        if not s.enable_exchange_orders:
            return False, "ENABLE_EXCHANGE_ORDERS=false"

        if self.environment == "DEMO" and not s.demo_armed:
            return False, "DEMO_TRADING_ARMED=false"

        if self.environment == "LIVE":
            if not s.live_armed:
                return False, "LIVE_TRADING_ARMED=false"

        if not self.ready or self.account is None:
            return False, "exchange_not_ready"

        if not self.positions_reconciled:
            return False, "account_state_not_reconciled"

        return True, "ready"

    # ---------------------------------------------------------
    # Account information
    # ---------------------------------------------------------

    def equity(self):
        if not self.account:
            return None

        try:
            return self.account.get_equity()
        except Exception:
            log.exception("Account equity fetch failed")
            return None

    def available(self):
        if not self.account:
            return None

        try:
            return self.account.get_available()
        except Exception:
            log.exception("Available balance fetch failed")
            return None

    def account_state(self):
        if not self.account:
            return None

        try:
            return self.account.state()
        except Exception:
            log.exception("Account state fetch failed")
            return None

    @staticmethod
    def _normalized_symbol(symbol):
        return str(symbol or "").split(":")[0].replace("/", "").upper()

    @classmethod
    def _exchange_position(cls, position):
        if not isinstance(position, dict):
            raise ValueError("exchange_position_not_an_object")

        symbol = cls._normalized_symbol(position.get("symbol"))
        if not symbol:
            raise ValueError("exchange_position_missing_symbol")

        quantity = None
        for key in ("total", "size", "qty", "positionAmt", "positionSize"):
            value = position.get(key)
            if value is None:
                continue
            try:
                quantity = float(value)
            except (TypeError, ValueError):
                raise ValueError("exchange_position_invalid_quantity")
            break

        if quantity is None:
            raise ValueError("exchange_position_missing_quantity")
        if not math.isfinite(quantity):
            raise ValueError("exchange_position_invalid_quantity")
        quantity = abs(quantity)
        if quantity == 0:
            return None

        direction = str(
            position.get("holdSide")
            or position.get("posSide")
            or position.get("side")
            or ""
        ).lower()
        if direction in {"buy", "long"}:
            direction = "LONG"
        elif direction in {"sell", "short"}:
            direction = "SHORT"
        else:
            raise ValueError("exchange_position_missing_direction")

        return {
            "symbol": symbol,
            "direction": direction,
            "quantity": quantity,
        }

    def reconcile_positions(self, local_trades):
        """Fail closed unless recorded trades match exchange positions and protection."""
        self.positions_reconciled = False
        if not self.account or not self.ready:
            return {
                "ok": False,
                "reason": "exchange_account_unavailable",
                "mismatches": ["exchange_account_unavailable"],
            }

        try:
            positions = self.account.current_positions()
            strategy_orders = self.account.unfilled_strategy_orders()
            if not isinstance(positions, list) or not isinstance(
                strategy_orders, list
            ):
                raise ValueError("exchange_state_response_invalid")

            live_positions = {}
            for raw_position in positions:
                position = self._exchange_position(raw_position)
                if position is None:
                    continue
                key = (position["symbol"], position["direction"])
                if key in live_positions:
                    raise ValueError("duplicate_exchange_position")
                live_positions[key] = position

            tracked_trades = {}
            environment = self.environment
            for trade in local_trades:
                if str(trade.get("source", "")).upper() != environment:
                    continue
                key = (
                    self._normalized_symbol(trade.get("symbol")),
                    str(trade.get("direction", "")).upper(),
                )
                if not key[0] or key[1] not in {"LONG", "SHORT"}:
                    raise ValueError("local_trade_identity_invalid")
                if key in tracked_trades:
                    raise ValueError("duplicate_local_exchange_trade")
                tracked_trades[key] = trade

            mismatches = []
            protection_updates = []
            for key, position in live_positions.items():
                trade = tracked_trades.get(key)
                if trade is None:
                    mismatches.append(
                        f"untracked_exchange_position:{key[0]}:{key[1]}"
                    )
                    continue

                expected_quantity = self._as_float(trade.get("qty"))
                if expected_quantity <= 0 or abs(
                    position["quantity"] - expected_quantity
                ) > max(expected_quantity * 1e-6, 1e-8):
                    mismatches.append(
                        f"position_quantity_mismatch:{key[0]}:{key[1]}"
                    )
                    continue

                matching_protection = [
                    order
                    for order in strategy_orders
                    if isinstance(order, dict)
                    and self._normalized_symbol(order.get("symbol")) == key[0]
                    and str(order.get("status", "")).lower()
                    in self.ACTIVE_STRATEGY_STATUSES
                    and (
                        str(
                            getattr(self.settings, "position_mode", "one_way")
                        ).lower()
                        != "hedge"
                        or str(
                            order.get("posSide")
                            or order.get("holdSide")
                            or ""
                        ).lower()
                        == key[1].lower()
                    )
                    and (
                        (
                            not trade.get("protection_order_id")
                            and not trade.get("protection_client_oid")
                        )
                        or (
                            (
                                trade.get("protection_order_id")
                                and str(order.get("orderId") or "")
                                == str(trade["protection_order_id"])
                            )
                            or (
                                trade.get("protection_client_oid")
                                and str(order.get("clientOid") or "")
                                == str(trade["protection_client_oid"])
                            )
                        )
                    )
                    and (
                        (
                            int(trade.get("management_stage") or 0) >= 2
                            and self._number_or_none(
                                order.get("takeProfit")
                            ) is None
                        )
                        or (
                            int(trade.get("management_stage") or 0) < 2
                            and self._same_decimal(
                                order.get("takeProfit"),
                                trade.get("target"),
                            )
                        )
                        or (
                            int(trade.get("management_stage") or 0) == 1
                            and self._number_or_none(
                                order.get("takeProfit")
                            ) is None
                            and self._profit_locked(
                                key[1],
                                self._as_float(trade.get("entry")),
                                self._as_float(order.get("stopLoss")),
                            )
                        )
                    )
                ]
                safe_protection = []
                old_stop = self._as_float(trade.get("stop"))
                for order in matching_protection:
                    new_stop = self._as_float(order.get("stopLoss"))
                    if new_stop <= 0:
                        continue
                    if key[1] == "LONG" and new_stop >= old_stop:
                        safe_protection.append((new_stop, order))
                    elif key[1] == "SHORT" and new_stop <= old_stop:
                        safe_protection.append((new_stop, order))

                if len(safe_protection) != 1:
                    mismatches.append(
                        (
                            "protection_unconfirmed"
                            if not safe_protection
                            else "multiple_matching_protection_orders"
                        )
                        + f":{key[0]}:{key[1]}"
                    )
                else:
                    actual_stop, active_order = safe_protection[0]
                    recovered_stage = int(
                        trade.get("management_stage") or 0
                    )
                    if (
                        recovered_stage == 1
                        and self._number_or_none(
                            active_order.get("takeProfit")
                        ) is None
                    ):
                        recovered_stage = 2
                    if (
                        not self._same_decimal(actual_stop, old_stop)
                        or recovered_stage
                        > int(trade.get("management_stage") or 0)
                        or not trade.get("protection_order_id")
                        or not trade.get("protection_client_oid")
                    ):
                        if trade.get("id") is None:
                            mismatches.append(
                                f"local_trade_id_missing:{key[0]}:{key[1]}"
                            )
                        else:
                            protection_updates.append({
                                "trade_id": trade["id"],
                                "stop": actual_stop,
                                "management_stage": recovered_stage,
                                "favorable_price": (
                                    self._as_float(
                                        trade.get("favorable_price")
                                    )
                                    or self._as_float(trade.get("entry"))
                                ),
                                "order_id": active_order.get("orderId"),
                                "client_oid": active_order.get("clientOid"),
                            })

            for key in tracked_trades:
                if key not in live_positions:
                    mismatches.append(
                        f"recorded_position_missing_on_exchange:"
                        f"{key[0]}:{key[1]}"
                    )

            result = {
                "ok": not mismatches,
                "reason": (
                    "exchange_state_matches"
                    if not mismatches
                    else "exchange_state_mismatch"
                ),
                "mismatches": mismatches,
                "protection_updates": protection_updates,
                "live_positions": len(live_positions),
                "live_position_keys": [
                    [symbol, direction]
                    for symbol, direction in live_positions
                ],
                "tracked_positions": len(tracked_trades),
            }
            self.positions_reconciled = result["ok"]
            return result
        except Exception as exc:
            self.positions_reconciled = False
            log.exception("Exchange position reconciliation failed")
            reason = f"exchange_reconciliation_error:{type(exc).__name__}"
            return {
                "ok": False,
                "reason": reason,
                "mismatches": [reason],
            }

    def close_position(self, trade, timeout=10):
        """Close one one-way-mode symbol position and verify its disappearance."""
        if (
            self.environment not in {"DEMO", "LIVE"}
            or not self.ready
            or self.account is None
        ):
            return {"success": False, "error": "exchange_not_ready"}
        if str(getattr(self.settings, "position_mode", "")).lower() == "hedge":
            return {
                "success": False,
                "error": "individual_close_not_supported_in_hedge_mode",
            }

        symbol = trade.get("symbol")
        direction = str(trade.get("direction", "")).upper()
        key = (self._normalized_symbol(symbol), direction)
        if not key[0] or direction not in {"LONG", "SHORT"}:
            return {"success": False, "error": "invalid_trade_identity"}

        try:
            positions = self.account.current_positions(symbol)
            if not isinstance(positions, list):
                raise ValueError("exchange_position_response_invalid")
            normalized_positions = [
                position
                for position in (
                    self._exchange_position(raw) for raw in positions
                )
                if position is not None
            ]
            same_symbol = [
                position
                for position in normalized_positions
                if position["symbol"] == key[0]
            ]
            matching = [
                position
                for position in same_symbol
                if position["direction"] == key[1]
            ]
            if not matching:
                return {"success": True, "already_closed": True}
            if len(matching) != 1 or len(same_symbol) != 1:
                return {
                    "success": False,
                    "error": "symbol_position_is_ambiguous",
                }

            expected_quantity = self._as_float(trade.get("qty"))
            actual_quantity = matching[0]["quantity"]
            if expected_quantity <= 0 or abs(
                expected_quantity - actual_quantity
            ) > max(expected_quantity * 1e-6, 1e-8):
                return {
                    "success": False,
                    "error": "position_quantity_mismatch",
                }

            self.account.close_all(symbol)
            deadline = time.time() + timeout
            while time.time() < deadline:
                remaining = self.account.current_positions(symbol)
                if not isinstance(remaining, list):
                    raise ValueError("exchange_position_response_invalid")
                remaining_positions = [
                    position
                    for position in (
                        self._exchange_position(raw) for raw in remaining
                    )
                    if position is not None
                ]
                if not any(
                    position["symbol"] == key[0]
                    and position["direction"] == key[1]
                    for position in remaining_positions
                ):
                    return {"success": True, "already_closed": False}
                time.sleep(1)

            return {
                "success": False,
                "error": "close_submitted_but_position_still_open",
                "reconcile_required": True,
            }
        except Exception as exc:
            log.exception(
                "INDIVIDUAL POSITION CLOSE FAILED | trade_id=%s | symbol=%s",
                trade.get("id"),
                symbol,
            )
            return {
                "success": False,
                "error": f"{type(exc).__name__}:{exc}",
                "reconcile_required": True,
            }

    # ---------------------------------------------------------
    # Order preparation
    # ---------------------------------------------------------

    def prepare(
        self,
        symbol,
        direction,
        quantity,
        entry,
        stop,
        target,
    ):
        ok, why = self.can_execute()

        try:
            quantity = float(quantity)
            entry = float(entry)
            stop = float(stop)
            target = float(target)
        except (TypeError, ValueError):
            ok, why = False, "invalid_numeric_order_parameters"

        if ok and quantity <= 0:
            ok, why = False, "quantity<=0"

        if ok and min(entry, stop, target) <= 0:
            ok, why = False, "entry_stop_target_must_be_positive"

        if ok and direction not in {"LONG", "SHORT"}:
            ok, why = False, "invalid_direction"

        if ok and direction == "LONG":
            if not stop < entry < target:
                ok, why = False, "invalid_long_stop_or_target"

        if ok and direction == "SHORT":
            if not target < entry < stop:
                ok, why = False, "invalid_short_stop_or_target"

        return {
            "ready": ok,
            "error": None if ok else why,
            "symbol": symbol,
            "direction": direction,
            "quantity": quantity,
            "entry": entry,
            "stop": stop,
            "target": target,
            "environment": self.environment,
            "leverage": self.settings.leverage,
        }

    def _precision_quantity(self, symbol, quantity):
        if self.exchange:
            return self.exchange.amount_to_precision(symbol, quantity)

        return format(Decimal(str(quantity)).normalize(), "f")

    def _precision_price(self, symbol, price):
        if self.exchange:
            return self.exchange.price_to_precision(symbol, price)

        return format(Decimal(str(price)).normalize(), "f")

    # ---------------------------------------------------------
    # Order reconciliation helpers
    # ---------------------------------------------------------

    @staticmethod
    def _as_float(value):
        try:
            number = float(value)
            if number != float("inf") and number == number:
                return number
        except (TypeError, ValueError):
            pass

        return 0.0

    @staticmethod
    def _number_or_none(value):
        try:
            number = float(value)
            if math.isfinite(number) and number > 0:
                return number
        except (TypeError, ValueError):
            pass
        return None

    @staticmethod
    def _profit_locked(direction, entry, stop):
        if direction == "LONG":
            return stop > entry
        if direction == "SHORT":
            return stop < entry
        return False

    def _mark_price(self, symbol):
        if self.exchange is None:
            return None
        ticker = self.exchange.fetch_ticker(symbol)
        info = ticker.get("info") if isinstance(ticker, dict) else None
        mark = (
            self._number_or_none(ticker.get("mark"))
            if isinstance(ticker, dict)
            else None
        )
        if mark is None and isinstance(info, dict):
            mark = self._number_or_none(info.get("markPrice"))
        return mark

    def _active_trade_strategy_orders(self, symbol, direction, orders):
        normalized_symbol = self._normalized_symbol(symbol)
        hedge_mode = (
            str(
                getattr(self.settings, "position_mode", "one_way")
            ).lower()
            == "hedge"
        )
        matches = []
        for order in orders:
            if not isinstance(order, dict):
                continue
            if self._normalized_symbol(order.get("symbol")) != normalized_symbol:
                continue
            if str(order.get("status") or "").lower() not in (
                self.ACTIVE_STRATEGY_STATUSES
            ):
                continue
            if hedge_mode and str(
                order.get("posSide") or order.get("holdSide") or ""
            ).lower() != direction.lower():
                continue
            matches.append(order)
        return matches

    def _strategy_order_matches(self, order, stop, target, quantity):
        if not self._same_decimal(order.get("stopLoss"), stop):
            return False
        actual_target = self._number_or_none(order.get("takeProfit"))
        if target is None:
            if actual_target is not None:
                return False
        elif not self._same_decimal(actual_target, target):
            return False
        actual_quantity = self._number_or_none(
            order.get("qty") or order.get("size")
        )
        return (
            actual_quantity is None
            or abs(actual_quantity - quantity)
            <= max(quantity * 1e-6, 1e-8)
        )

    def _preserve_protection_after_update_failure(
        self,
        trade,
        order_id,
        client_oid,
        requested_stop,
        requested_target,
        old_stop,
        old_target,
        failure_reason,
    ):
        symbol = trade["symbol"]
        direction = str(trade["direction"]).upper()
        quantity = self._as_float(trade.get("qty"))
        try:
            positions = self.account.current_positions(symbol)
            if not isinstance(positions, list):
                raise ValueError("position_response_invalid")
            matching_positions = [
                position
                for position in (
                    self._exchange_position(raw) for raw in positions
                )
                if position is not None
                and position["symbol"] == self._normalized_symbol(symbol)
                and position["direction"] == direction
            ]
            if len(matching_positions) != 1 or quantity <= 0:
                raise ValueError("position_not_unique_after_tpsl_failure")
            actual_quantity = matching_positions[0]["quantity"]
            if abs(actual_quantity - quantity) > max(
                quantity * 1e-6, 1e-8
            ):
                raise ValueError("position_quantity_changed_after_tpsl_failure")
        except Exception as exc:
            self.positions_reconciled = False
            log.exception(
                "Could not verify exchange position after failed TPSL "
                "update | trade_id=%s",
                trade.get("id"),
            )
            return {
                "ok": False,
                "protection_preserved": False,
                "reason": (
                    f"{failure_reason}; position_check_failed:"
                    f"{type(exc).__name__}:{exc}"
                ),
            }

        try:
            orders = self.account.unfilled_strategy_orders()
            if not isinstance(orders, list):
                raise ValueError("strategy_order_response_invalid")
        except Exception as exc:
            self.positions_reconciled = False
            log.exception(
                "Could not verify protection after failed TPSL update | "
                "trade_id=%s",
                trade.get("id"),
            )
            return {
                "ok": False,
                "protection_preserved": False,
                "reason": (
                    f"{failure_reason}; protection_check_failed:"
                    f"{type(exc).__name__}"
                ),
            }

        active = self._active_trade_strategy_orders(
            symbol, direction, orders
        )
        if len(active) > 1:
            self.positions_reconciled = False
            return {
                "ok": False,
                "protection_preserved": False,
                "reason": "multiple_active_trade_strategy_orders",
            }
        requested_matches = [
            order
            for order in active
            if self._strategy_order_matches(
                order, requested_stop, requested_target, quantity
            )
        ]
        if len(requested_matches) == 1:
            order = requested_matches[0]
            self.positions_reconciled = True
            return {
                "ok": True,
                "protection_preserved": True,
                "recovered_update": True,
                "stop_confirmed": True,
                "target_confirmed": True,
                "actual_stop": order.get("stopLoss"),
                "actual_target": order.get("takeProfit"),
                "order_id": order.get("orderId") or order_id,
                "client_oid": order.get("clientOid") or client_oid,
                "status": str(order.get("status") or "").lower(),
            }
        if len(requested_matches) > 1:
            self.positions_reconciled = False
            return {
                "ok": False,
                "protection_preserved": False,
                "reason": "multiple_matching_requested_protection_orders",
            }

        old_matches = [
            order
            for order in active
            if self._strategy_order_matches(
                order, old_stop, old_target, quantity
            )
        ]
        if len(old_matches) == 1:
            order = old_matches[0]
            self.positions_reconciled = True
            return {
                "ok": False,
                "protection_preserved": True,
                "protection_restored": False,
                "actual_stop": order.get("stopLoss"),
                "actual_target": order.get("takeProfit"),
                "order_id": order.get("orderId") or order_id,
                "client_oid": order.get("clientOid") or client_oid,
                "reason": failure_reason,
            }
        if active:
            self.positions_reconciled = False
            return {
                "ok": False,
                "protection_preserved": False,
                "reason": (
                    f"{failure_reason}; active_protection_unrecognized"
                ),
            }

        try:
            mark_price = self._mark_price(symbol)
            if mark_price is None:
                raise ValueError("mark_price_unavailable")
            if (
                direction == "LONG" and old_stop >= mark_price
            ) or (
                direction == "SHORT" and old_stop <= mark_price
            ):
                raise ValueError("existing_stop_invalid_vs_mark_price")

            replacement_client_oid = "tpsl-" + uuid.uuid4().hex[:20]
            replacement = self.account.place_tpsl_order(
                symbol,
                side="sell" if direction == "LONG" else "buy",
                client_oid=replacement_client_oid,
                stop=self._precision_price(symbol, old_stop),
                target=None,
                pos_side=direction.lower(),
            )
            replacement_order_id = self._extract_order_id(replacement)
            confirmation = self._reconcile_tpsl_order(
                symbol,
                replacement_order_id,
                replacement_client_oid,
                self._precision_price(symbol, old_stop),
                None,
            )
            if confirmation.get("ok"):
                self.positions_reconciled = True
                return {
                    **confirmation,
                    "ok": False,
                    "protection_preserved": True,
                    "protection_restored": True,
                    "actual_stop": confirmation.get("actual_stop") or old_stop,
                    "actual_target": None,
                    "order_id": confirmation.get("order_id")
                    or replacement_order_id,
                    "client_oid": confirmation.get("client_oid")
                    or replacement_client_oid,
                    "reason": failure_reason,
                }
        except Exception as exc:
            log.exception(
                "FAILED TO RESTORE ORIGINAL STOP AFTER TPSL UPDATE ERROR | "
                "trade_id=%s",
                trade.get("id"),
            )
            failure_reason = (
                f"{failure_reason}; stop_restore_failed:"
                f"{type(exc).__name__}:{exc}"
            )

        self.positions_reconciled = False
        return {
            "ok": False,
            "protection_preserved": False,
            "reason": failure_reason,
        }

    @staticmethod
    def _extract_order_id(data):
        if not isinstance(data, dict):
            return ""

        return str(
            data.get("orderId")
            or data.get("order_id")
            or ""
        )

    def _get_order_info(self, symbol, order_id=None, client_oid=None):
        return self.account.order_info(
            symbol,
            order_id=order_id or None,
            client_oid=client_oid or None,
        )

    @staticmethod
    def _same_decimal(value, expected):
        try:
            return Decimal(str(value)) == Decimal(str(expected))
        except (InvalidOperation, TypeError, ValueError):
            return False

    def _reconcile_tpsl_order(
        self,
        symbol,
        order_id,
        client_oid,
        stop,
        target,
        seconds=5,
    ):
        """Confirm the separate TPSL strategy order is active on Bitget."""

        deadline = time.time() + seconds
        last_match = None
        normalized_symbol = self._normalized_symbol(symbol)

        while time.time() < deadline:
            try:
                orders = self.account.unfilled_strategy_orders()
                if not isinstance(orders, list):
                    raise ValueError("strategy_order_response_invalid")

                matches = []
                for order in orders:
                    if not isinstance(order, dict):
                        continue
                    if self._normalized_symbol(order.get("symbol")) != normalized_symbol:
                        continue

                    matches_id = (
                        order_id
                        and str(order.get("orderId") or "") == str(order_id)
                    )
                    matches_client_oid = (
                        client_oid
                        and str(order.get("clientOid") or "") == client_oid
                    )
                    if not (matches_id or matches_client_oid):
                        continue
                    matches.append(order)

                if len(matches) > 1:
                    return {
                        "ok": False,
                        "reason": "multiple_matching_strategy_orders",
                    }
                if matches:
                    order = matches[0]
                    last_match = order
                    stop_confirmed = self._same_decimal(
                        order.get("stopLoss"),
                        stop,
                    )
                    if target is None:
                        target_confirmed = (
                            self._number_or_none(
                                order.get("takeProfit")
                            )
                            is None
                        )
                    else:
                        target_confirmed = self._same_decimal(
                            order.get("takeProfit"),
                            target,
                        )
                    status = str(order.get("status") or "").lower()
                    active = status in self.ACTIVE_STRATEGY_STATUSES
                    if active and stop_confirmed and target_confirmed:
                        return {
                            "ok": True,
                            "stop_confirmed": True,
                            "target_confirmed": True,
                            "actual_stop": order.get("stopLoss"),
                            "actual_target": order.get("takeProfit"),
                            "order_id": order.get("orderId") or order_id,
                            "client_oid": order.get("clientOid") or client_oid,
                            "status": status,
                            "reason": None,
                        }
            except Exception:
                log.warning(
                    "TPSL RECONCILIATION POLL FAILED | "
                    "symbol=%s | order_id=%s | client_oid=%s",
                    symbol,
                    order_id or "unknown",
                    client_oid,
                    exc_info=True,
                )

            time.sleep(1)

        return {
            "ok": False,
            "stop_confirmed": False,
            "target_confirmed": False,
            "actual_stop": (
                last_match.get("stopLoss") if last_match else None
            ),
            "actual_target": (
                last_match.get("takeProfit") if last_match else None
            ),
            "order_id": order_id or (
                last_match.get("orderId") if last_match else None
            ),
            "client_oid": client_oid,
            "status": (
                str(last_match.get("status") or "unknown").lower()
                if last_match
                else "not_found"
            ),
            "reason": (
                "TPSL strategy order was not found in active orders"
                if not last_match
                else "TPSL strategy order did not confirm both active levels"
            ),
        }

    def update_tpsl_protection(self, trade, stop, target):
        """Modify one reconciled full-position TPSL order and verify it."""
        ok, reason = self.can_execute()
        if not ok:
            return {"ok": False, "reason": reason}

        direction = str(trade.get("direction") or "").upper()
        if direction not in {"LONG", "SHORT"}:
            return {"ok": False, "reason": "invalid_trade_direction"}

        old_stop = self._as_float(trade.get("stop"))
        new_stop = self._as_float(stop)
        if old_stop <= 0 or new_stop <= 0:
            return {"ok": False, "reason": "invalid_stop_level"}
        if direction == "LONG" and new_stop < old_stop:
            return {"ok": False, "reason": "stop_must_only_move_up"}
        if direction == "SHORT" and new_stop > old_stop:
            return {"ok": False, "reason": "stop_must_only_move_down"}

        stage = int(trade.get("management_stage") or 0)
        expected_target = None if stage >= 2 else trade.get("target")
        target_changed = not (
            (target is None and expected_target is None)
            or (
                target is not None
                and expected_target is not None
                and self._same_decimal(target, expected_target)
            )
        )
        stop_changed = not self._same_decimal(new_stop, old_stop)
        if not stop_changed and not target_changed:
            return {"ok": False, "reason": "protection_update_has_no_change"}
        if target is not None and not self._same_decimal(
            target, trade.get("target")
        ):
            return {"ok": False, "reason": "unexpected_virtual_target"}
        if target is None and stage < 1:
            return {"ok": False, "reason": "target_removal_before_profit_lock"}
        if target is None and stage < 2 and not self._profit_locked(
            direction,
            self._as_float(trade.get("entry")),
            new_stop,
        ):
            return {
                "ok": False,
                "reason": "target_removal_without_profit_protection",
            }

        stored_order_id = str(trade.get("protection_order_id") or "")
        stored_client_oid = str(trade.get("protection_client_oid") or "")
        symbol = self._normalized_symbol(trade.get("symbol"))

        try:
            positions = self.account.current_positions(trade["symbol"])
            if not isinstance(positions, list):
                raise ValueError("position_response_invalid")
            matching_positions = []
            for raw_position in positions:
                position = self._exchange_position(raw_position)
                if position is None:
                    continue
                if (
                    position["symbol"] == symbol
                    and position["direction"] == direction
                ):
                    matching_positions.append(position)
            if len(matching_positions) != 1:
                self.positions_reconciled = False
                return {
                    "ok": False,
                    "reason": (
                        "position_not_found_before_protection_update"
                        if not matching_positions
                        else "position_ambiguous_before_protection_update"
                    ),
                }
            expected_quantity = self._as_float(trade.get("qty"))
            if (
                expected_quantity <= 0
                or abs(
                    matching_positions[0]["quantity"] - expected_quantity
                )
                > max(expected_quantity * 1e-6, 1e-8)
            ):
                self.positions_reconciled = False
                return {
                    "ok": False,
                    "reason": "position_quantity_changed_before_protection_update",
                }
        except Exception as exc:
            log.exception(
                "Could not verify live position before protection update | "
                "trade_id=%s",
                trade.get("id"),
            )
            self.positions_reconciled = False
            return {
                "ok": False,
                "reason": f"position_read_failed:{type(exc).__name__}",
            }

        try:
            orders = self.account.unfilled_strategy_orders()
            if not isinstance(orders, list):
                raise ValueError("strategy_order_response_invalid")
        except Exception as exc:
            log.exception(
                "Could not read active protection before update | trade_id=%s",
                trade.get("id"),
            )
            self.positions_reconciled = False
            return {
                "ok": False,
                "reason": f"active_protection_read_failed:{type(exc).__name__}",
            }

        candidates = []
        for order in orders:
            if not isinstance(order, dict):
                continue
            if self._normalized_symbol(order.get("symbol")) != symbol:
                continue
            if str(order.get("status") or "").lower() not in (
                self.ACTIVE_STRATEGY_STATUSES
            ):
                continue

            order_id = str(order.get("orderId") or "")
            client_oid = str(order.get("clientOid") or "")
            if stored_order_id or stored_client_oid:
                if not (
                    (stored_order_id and order_id == stored_order_id)
                    or (stored_client_oid and client_oid == stored_client_oid)
                ):
                    continue
            if expected_target is None:
                if self._number_or_none(order.get("takeProfit")) is not None:
                    continue
            elif not self._same_decimal(
                order.get("takeProfit"), expected_target
            ):
                continue

            actual_stop = self._number_or_none(order.get("stopLoss"))
            if actual_stop is None:
                continue
            if direction == "LONG" and actual_stop < old_stop:
                continue
            if direction == "SHORT" and actual_stop > old_stop:
                continue
            if (
                str(getattr(self.settings, "position_mode", "one_way")).lower()
                == "hedge"
                and str(
                    order.get("posSide") or order.get("holdSide") or ""
                ).lower()
                != direction.lower()
            ):
                continue
            candidates.append(order)

        if len(candidates) != 1:
            self.positions_reconciled = False
            return {
                "ok": False,
                "reason": (
                    "protection_order_not_found"
                    if not candidates
                    else "multiple_matching_protection_orders"
                ),
            }

        current_order = candidates[0]
        order_id = str(current_order.get("orderId") or stored_order_id)
        client_oid = str(current_order.get("clientOid") or stored_client_oid)
        if self.exchange is not None:
            try:
                mark_price = self._mark_price(trade["symbol"])
                if mark_price is None:
                    raise ValueError("mark_price_unavailable")
                invalid_stop_side = (
                    direction == "LONG" and new_stop >= mark_price
                ) or (
                    direction == "SHORT" and new_stop <= mark_price
                )
                if invalid_stop_side:
                    result = self._preserve_protection_after_update_failure(
                        trade,
                        order_id,
                        client_oid,
                        stop,
                        target,
                        old_stop,
                        expected_target,
                        (
                            "new_stop_invalid_vs_mark_price:"
                            f"stop={new_stop}:mark={mark_price}"
                        ),
                    )
                    log.warning(
                        "TPSL UPDATE SKIPPED; STOP IS NOT ON VALID SIDE OF "
                        "MARK PRICE | trade_id=%s | direction=%s | "
                        "new_stop=%s | mark=%s | protection_preserved=%s",
                        trade.get("id"),
                        direction,
                        new_stop,
                        mark_price,
                        result.get("protection_preserved"),
                    )
                    return result
            except Exception as exc:
                result = self._preserve_protection_after_update_failure(
                    trade,
                    order_id,
                    client_oid,
                    stop,
                    target,
                    old_stop,
                    expected_target,
                    f"mark_price_check_failed:{type(exc).__name__}:{exc}",
                )
                log.exception(
                    "TPSL UPDATE BLOCKED; MARK PRICE COULD NOT BE "
                    "VALIDATED | trade_id=%s",
                    trade.get("id"),
                )
                return result

        try:
            self.account.modify_tpsl_order(
                symbol=trade["symbol"],
                order_id=order_id,
                client_oid=client_oid,
                quantity=self._precision_quantity(
                    trade["symbol"], trade["qty"]
                ),
                stop=stop,
                target=target,
                pos_side=direction.lower(),
            )
        except Exception as exc:
            failure_reason = (
                f"protection_update_rejected:{type(exc).__name__}:{exc}"
            )
            log.exception(
                "TPSL modification request failed; verifying requested and "
                "previous protection | trade_id=%s | order_id=%s",
                trade.get("id"),
                order_id or "unknown",
            )
            return self._preserve_protection_after_update_failure(
                trade,
                order_id,
                client_oid,
                stop,
                target,
                old_stop,
                expected_target,
                failure_reason,
            )

        confirmation = self._reconcile_tpsl_order(
            trade["symbol"],
            order_id,
            client_oid,
            stop,
            target,
        )
        if not confirmation.get("ok"):
            return self._preserve_protection_after_update_failure(
                trade,
                order_id,
                client_oid,
                stop,
                target,
                old_stop,
                expected_target,
                confirmation.get("reason")
                or "updated_protection_not_confirmed",
            )

        return {
            **confirmation,
            "ok": True,
            "order_id": confirmation.get("order_id") or order_id,
            "client_oid": confirmation.get("client_oid") or client_oid,
        }

    @staticmethod
    def _is_parameter_rejection(error):
        message = str(error)
        return (
            "Bitget HTTP 400: 25200" in message
            or "Bitget API 25200:" in message
        )

    def _reconcile_order(
        self,
        symbol,
        order_id,
        client_oid,
        seconds=12,
    ):
        """Query an existing order; never resubmit it."""

        deadline = time.time() + seconds
        last_info = {}

        while time.time() < deadline:
            try:
                last_info = self._get_order_info(
                    symbol,
                    order_id=order_id,
                    client_oid=client_oid,
                )

                if not isinstance(last_info, dict):
                    last_info = {}

                discovered_id = self._extract_order_id(last_info)
                if discovered_id:
                    order_id = discovered_id

                status = str(
                    last_info.get("orderStatus") or ""
                ).lower()

                if status in self.TERMINAL_FILLED:
                    return order_id, last_info

                if status in self.TERMINAL_FAILED:
                    return order_id, last_info

            except Exception:
                log.warning(
                    "ORDER RECONCILIATION POLL FAILED | "
                    "symbol=%s | order_id=%s | client_oid=%s",
                    symbol,
                    order_id or "unknown",
                    client_oid,
                    exc_info=True,
                )

            time.sleep(1)

        return order_id, last_info

    # ---------------------------------------------------------
    # Execute order
    # ---------------------------------------------------------

    def execute(self, plan):
        ok, why = self.can_execute()

        if not ok:
            return {
                "success": False,
                "executed": False,
                "error": why,
                "reconcile_required": False,
            }

        if not isinstance(plan, dict) or not plan.get("ready"):
            return {
                "success": False,
                "executed": False,
                "error": "invalid_or_unready_plan",
                "reconcile_required": False,
            }

        if str(plan.get("environment", "")).upper() != self.environment:
            return {
                "success": False,
                "executed": False,
                "error": "plan_environment_mismatch",
                "reconcile_required": False,
            }

        symbol = plan["symbol"]
        direction = plan["direction"]

        if direction not in {"LONG", "SHORT"}:
            return {
                "success": False,
                "executed": False,
                "error": "invalid_direction",
                "reconcile_required": False,
            }

        # FINAL LIVE-MARKET SANITY CHECK.
        # This check runs immediately before any order submission.
        try:
            if self.exchange is None:
                raise RuntimeError("Market exchange client is unavailable")

            ticker = self.exchange.fetch_ticker(symbol)
            live_price = self._as_float(ticker.get("last"))

            entry = self._as_float(plan.get("entry"))
            stop = self._as_float(plan.get("stop"))
            target = self._as_float(plan.get("target"))

            if min(live_price, entry, stop, target) <= 0:
                raise ValueError(
                    "Live price, entry, SL and TP must all be positive"
                )

            # _as_float rejects NaN and positive infinity; explicitly
            # reject negative infinity as well.
            values = (live_price, entry, stop, target)
            if any(
                value != value or abs(value) == float("inf")
                for value in values
            ):
                raise ValueError("Non-finite live price or order level")

            max_deviation_pct = float(
                self.settings.max_entry_deviation_pct
            )

            if max_deviation_pct < 0:
                raise ValueError(
                    "MAX_ENTRY_DEVIATION_PCT must not be negative"
                )

            deviation_pct = abs(live_price - entry) / entry * 100

            if deviation_pct > max_deviation_pct:
                return {
                    "success": False,
                    "executed": False,
                    "error": (
                        "live_price_deviation_exceeded:"
                        f"{deviation_pct:.4f}%>"
                        f"{max_deviation_pct:.4f}%"
                    ),
                    "reconcile_required": False,
                }

            if direction == "LONG":
                if not stop < live_price < target:
                    raise ValueError(
                        "LONG blocked: live price is at/beyond SL or TP"
                    )

            elif direction == "SHORT":
                if not target < live_price < stop:
                    raise ValueError(
                        "SHORT blocked: live price is at/beyond TP or SL"
                    )

            log.info(
                "FINAL MARKET CHECK PASSED | symbol=%s | "
                "direction=%s | planned_entry=%s | live_price=%s | "
                "deviation_pct=%.4f | SL=%s | TP=%s",
                symbol,
                direction,
                entry,
                live_price,
                deviation_pct,
                stop,
                target,
            )

        except Exception as exc:
            log.warning(
                "FINAL MARKET CHECK BLOCKED | symbol=%s | error=%s",
                symbol,
                exc,
            )
            return {
                "success": False,
                "executed": False,
                "error": f"final_market_sanity_check_failed:{exc}",
                "reconcile_required": False,
            }

        try:
            qty = self._precision_quantity(
                symbol,
                float(plan["quantity"]),
            )
            stop = self._precision_price(symbol, plan["stop"])
            target = self._precision_price(symbol, plan["target"])
        except Exception as exc:
            log.exception("Order precision conversion failed")
            return {
                "success": False,
                "executed": False,
                "error": f"order_precision_error:{exc}",
                "reconcile_required": False,
            }

        if float(qty) <= 0:
            return {
                "success": False,
                "executed": False,
                "error": "quantity rounded to zero",
                "reconcile_required": False,
            }

        precise_stop = self._as_float(stop)
        precise_target = self._as_float(target)
        if direction == "LONG" and not (
            0 < precise_stop < live_price < precise_target
        ):
            return {
                "success": False,
                "executed": False,
                "error": "rounded_long_stop_or_target_invalid",
                "reconcile_required": False,
            }
        if direction == "SHORT" and not (
            0 < precise_target < live_price < precise_stop
        ):
            return {
                "success": False,
                "executed": False,
                "error": "rounded_short_stop_or_target_invalid",
                "reconcile_required": False,
            }

        if self.exchange:
            market = self.exchange.market(symbol)
            minimum_quantity = (
                (market.get("limits") or {})
                .get("amount", {})
                .get("min")
            )
            if (
                minimum_quantity is not None
                and Decimal(qty) < Decimal(str(minimum_quantity))
            ):
                return {
                    "success": False,
                    "executed": False,
                    "error": "quantity_below_exchange_minimum",
                    "reconcile_required": False,
                }

        side = "buy" if direction == "LONG" else "sell"
        client_oid = "khtb-" + uuid.uuid4().hex[:20]

        # Fail closed: do not submit an order if leverage setup fails.
        try:
            leverage_result = self.account.set_leverage(
                symbol,
                self.settings.leverage,
            )

            log.info(
                "LEVERAGE REQUEST ACCEPTED | symbol=%s | leverage=%s",
                symbol,
                self.settings.leverage,
            )

        except Exception as exc:
            log.exception(
                "LEVERAGE SETUP FAILED; ORDER BLOCKED | symbol=%s",
                symbol,
            )

            return {
                "success": False,
                "executed": False,
                "error": f"leverage_setup_failed:{type(exc).__name__}:{exc}",
                "reconcile_required": False,
            }

        # Never automatically retry place_order after a timeout.
        # The exchange might already have accepted the order.
        ack = None
        submission_error = None

        try:
            ack = self.account.place_order(
                symbol,
                side,
                qty,
                client_oid,
            )

        except Exception as exc:
            submission_error = f"{type(exc).__name__}:{exc}"

            if self._is_parameter_rejection(submission_error):
                log.error(
                    "ORDER SUBMISSION REJECTED | symbol=%s | "
                    "client_oid=%s | error=%s",
                    symbol,
                    client_oid,
                    submission_error,
                )
                return {
                    "success": False,
                    "executed": False,
                    "error": f"order_rejected:{submission_error}",
                    "submission_error": submission_error,
                    "client_oid": client_oid,
                    "reconcile_required": False,
                }

            log.exception(
                "ORDER SUBMISSION RESULT UNKNOWN; "
                "RECONCILING BY CLIENT OID | symbol=%s | client_oid=%s",
                symbol,
                client_oid,
            )

        order_id = self._extract_order_id(ack)

        if not order_id and not submission_error:
            log.warning(
                "ORDER ACK HAS NO ORDER ID; RECONCILING | "
                "symbol=%s | client_oid=%s",
                symbol,
                client_oid,
            )

        # Query by clientOid when order ID is missing or submission
        # raised an exception. Do not submit a second order.
        if submission_error or not order_id:
            try:
                order_id, info = self._reconcile_order(
                    symbol,
                    order_id,
                    client_oid,
                    seconds=12,
                )
            except Exception:
                info = {}
                log.exception(
                    "ORDER RECONCILIATION FAILED | symbol=%s",
                    symbol,
                )

            if not info:
                return {
                    "success": False,
                    "executed": False,
                    "order_id": order_id or None,
                    "client_oid": client_oid,
                    "error": (
                        "submission_outcome_unknown; "
                        "no automatic resubmission attempted"
                    ),
                    "submission_error": submission_error,
                    "reconcile_required": True,
                }

        else:
            info = {}

        # Poll order status when the order ID is known.
        if order_id:
            order_id, polled_info = self._reconcile_order(
                symbol,
                order_id,
                client_oid,
                seconds=12,
            )

            if polled_info:
                info = polled_info

        status = str(
            info.get("orderStatus") or ""
        ).lower()

        filled = self._as_float(info.get("cumExecQty"))
        average = self._as_float(info.get("avgPrice"))

        if status in self.TERMINAL_FAILED:
            return {
                "success": False,
                "executed": filled > 0,
                "order_id": order_id or None,
                "client_oid": client_oid,
                "filled_quantity": filled,
                "order_status": status,
                "error": f"exchange_order_{status}",
                "reconcile_required": filled > 0,
            }

        if status != "filled" or filled <= 0 or average <= 0:
            return {
                "success": False,
                "executed": filled > 0,
                "order_id": order_id or None,
                "client_oid": client_oid,
                "average": average or None,
                "filled_quantity": filled,
                "order_status": status or "unknown",
                "error": (
                    "order_not_confirmed_filled; "
                    "manual or automated reconciliation is required"
                ),
                "reconcile_required": True,
            }

        protection_client_oid = "tpsl-" + uuid.uuid4().hex[:20]
        protection_side = "sell" if direction == "LONG" else "buy"
        protection_ack = None
        protection_submission_error = None

        try:
            protection_ack = self.account.place_tpsl_order(
                symbol,
                side=protection_side,
                client_oid=protection_client_oid,
                stop=stop,
                target=target,
                pos_side=direction.lower(),
            )
        except Exception as exc:
            protection_submission_error = (
                f"{type(exc).__name__}:{exc}"
            )
            log.exception(
                "POSITION FILLED BUT TPSL SUBMISSION FAILED | "
                "symbol=%s | entry_order_id=%s | client_oid=%s",
                symbol,
                order_id,
                protection_client_oid,
            )

        protection_order_id = self._extract_order_id(protection_ack)
        protection = self._reconcile_tpsl_order(
            symbol,
            protection_order_id,
            protection_client_oid,
            stop,
            target,
        )
        if protection_submission_error and not protection["ok"]:
            protection["reason"] = (
                f"{protection['reason']}; submission error: "
                f"{protection_submission_error}"
            )

        if not protection["ok"]:
            log.critical(
                "POSITION FILLED BUT SL/TP NOT ACTIVE | "
                "symbol=%s | entry_order_id=%s | tpsl_order_id=%s | "
                "protection=%s",
                symbol,
                order_id,
                protection.get("order_id"),
                protection,
            )

        return {
            # A confirmed fill is a real position even if protection
            # is unverified. Keep success=True so callers can record it.
            "success": True,
            "executed": True,
            "order_id": order_id,
            "client_oid": client_oid,
            "average": average,
            "filled_quantity": filled,
            "order_status": status,
            "environment": self.environment,
            "leverage_result": leverage_result,
            "stop": stop,
            "target": target,
            "protection_order_id": protection.get("order_id"),
            "protection": protection,
            "warning": (
                None
                if protection["ok"]
                else "POSITION FILLED; SL/TP MUST BE CHECKED IMMEDIATELY"
            ),
            "reconcile_required": not protection["ok"],
        }
        # ---------------------------------------------------------
    # Close positions
    # ---------------------------------------------------------

    def close_all(self, symbol=None):
        ok, why = self.can_execute()

        if not ok:
            return {
                "success": False,
                "error": why,
            }

        try:
            result = self.account.close_all(symbol)

            log.warning(
                "CLOSE ALL REQUEST ACCEPTED | symbol=%s | response=%s",
                symbol or "ALL",
                result,
            )

            return {
                "success": True,
                "response": result,
                "warning": (
                    "Close request accepted; verify positions are closed "
                    "using the exchange position endpoint."
                ),
            }

        except Exception as exc:
            log.exception("UTA close-all request failed")

            return {
                "success": False,
                "error": f"{type(exc).__name__}:{exc}",
                "reconcile_required": True,
            }
