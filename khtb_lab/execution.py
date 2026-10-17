from __future__ import annotations

import logging
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

    def __init__(self, settings):
        self.settings = settings
        self.environment = str(settings.trading_env).upper()
        self.exchange = None
        self.account = None
        self.ready = False

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

        while time.time() < deadline:
            try:
                orders = self.account.unfilled_strategy_orders()
                if not isinstance(orders, list):
                    orders = []

                for order in orders:
                    if not isinstance(order, dict):
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

                    last_match = order
                    stop_confirmed = self._same_decimal(
                        order.get("stopLoss"),
                        stop,
                    )
                    target_confirmed = self._same_decimal(
                        order.get("takeProfit"),
                        target,
                    )
                    status = str(order.get("status") or "").lower()
                    active = status in {"pending", "submitting"}
                    return {
                        "ok": active and stop_confirmed and target_confirmed,
                        "stop_confirmed": stop_confirmed,
                        "target_confirmed": target_confirmed,
                        "actual_stop": order.get("stopLoss"),
                        "actual_target": order.get("takeProfit"),
                        "order_id": order.get("orderId") or order_id,
                        "client_oid": order.get("clientOid") or client_oid,
                        "status": status or "unknown",
                        "reason": (
                            None
                            if active and stop_confirmed and target_confirmed
                            else "TPSL strategy order is not active or levels differ"
                        ),
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
