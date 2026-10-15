from __future__ import annotations

import logging
import time
import uuid

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
        if not self.exchange:
            return float(quantity)

        return float(
            self.exchange.amount_to_precision(symbol, quantity)
        )

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

    def _protection_status(self, info, stop, target):
        """Report whether the exchange response confirms both protections."""

        if not isinstance(info, dict):
            return {
                "ok": False,
                "stop_confirmed": False,
                "target_confirmed": False,
                "status": "UNVERIFIED",
                "reason": "order information unavailable",
            }

        actual_stop = self._as_float(info.get("stopLoss"))
        actual_target = self._as_float(info.get("takeProfit"))

        expected_stop = self._as_float(stop)
        expected_target = self._as_float(target)

        # Require the exchange to return actual protection values.
        # Missing values are not treated as confirmation.
        stop_confirmed = (
            actual_stop > 0
            and expected_stop > 0
            and abs(actual_stop - expected_stop)
            <= max(expected_stop * 0.0001, 1e-10)
        )

        target_confirmed = (
            actual_target > 0
            and expected_target > 0
            and abs(actual_target - expected_target)
            <= max(expected_target * 0.0001, 1e-10)
        )

        ok = stop_confirmed and target_confirmed

        return {
            "ok": ok,
            "stop_confirmed": stop_confirmed,
            "target_confirmed": target_confirmed,
            "actual_stop": actual_stop or None,
            "actual_target": actual_target or None,
            "status": "CONFIRMED" if ok else "UNVERIFIED",
            "reason": (
                None if ok
                else "Exchange response did not confirm both SL and TP"
            ),
        }

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

        try:
            qty = self._precision_quantity(
                symbol,
                float(plan["quantity"]),
            )
        except Exception as exc:
            log.exception("Quantity precision conversion failed")
            return {
                "success": False,
                "executed": False,
                "error": f"quantity_precision_error:{exc}",
                "reconcile_required": False,
            }

        if qty <= 0:
            return {
                "success": False,
                "executed": False,
                "error": "quantity rounded to zero",
                "reconcile_required": False,
            }

        if direction not in {"LONG", "SHORT"}:
            return {
                "success": False,
                "executed": False,
                "error": "invalid_direction",
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
                stop=plan["stop"],
                target=plan["target"],
            )

        except Exception as exc:
            submission_error = (
                f"{type(exc).__name__}:{exc}"
            )

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

        protection = self._protection_status(
            info,
            plan["stop"],
            plan["target"],
        )

        if not protection["ok"]:
            log.critical(
                "POSITION FILLED BUT SL/TP NOT CONFIRMED | "
                "symbol=%s | order_id=%s | protection=%s",
                symbol,
                order_id,
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
