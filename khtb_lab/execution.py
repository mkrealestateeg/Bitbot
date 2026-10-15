from __future__ import annotations

import logging
import time
import uuid

import ccxt

from .account_adapter import BitgetUTAAdapter

log = logging.getLogger(__name__)


class ExecutionEngine:
    """UTA execution boundary. Supports MANUAL, DEMO AUTO and LIVE AUTO."""

    def __init__(self, settings):
        self.settings = settings
        self.environment = settings.trading_env
        self.exchange = None  # kept for market precision compatibility
        self.account = None
        self.ready = False

        if self.environment == "PAPER":
            return
        if settings.exchange_id != "bitget":
            log.warning("This build's direct execution adapter is Bitget UTA only")
            return

        self.account = BitgetUTAAdapter(settings)
        if not self.account.enabled:
            log.warning("Bitget %s credentials missing; exchange execution disabled", self.environment)
            return

        try:
            # CCXT is used only for market metadata/precision. Private account/trade
            # calls go through the UTA v3 adapter above.
            self.exchange = ccxt.bitget({
                "enableRateLimit": True,
                "options": {"defaultType": "swap"},
            })
            self.exchange.load_markets()
            account_cfg = self.account.account_settings()
            actual_hold = str(account_cfg.get("holdMode") or "").lower()
            expected_hold = "hedge_mode" if settings.position_mode == "hedge" else "one_way_mode"
            if actual_hold and actual_hold != expected_hold:
                log.error(
                    "UTA position-mode mismatch: configured=%s exchange=%s; execution disabled",
                    expected_hold, actual_hold,
                )
                return
            self.ready = True
            log.info(
                "UTA execution ready | exchange=%s env=%s account=%s holdMode=%s",
                settings.exchange_name, self.environment, settings.account_type,
                actual_hold or expected_hold,
            )
        except Exception:
            log.exception("Market metadata initialization failed; execution disabled")

    def can_execute(self):
        s = self.settings
        if not s.enable_exchange_orders:
            return False, "ENABLE_EXCHANGE_ORDERS=false"
        if self.environment == "DEMO" and not s.demo_armed:
            return False, "DEMO_TRADING_ARMED=false"
        if self.environment == "LIVE" and not s.live_armed:
            return False, "LIVE_TRADING_ARMED=false"
        if not self.ready or self.account is None:
            return False, "exchange_not_ready"
        return True, "ready"

    def equity(self):
        if not self.account:
            return None
        try:
            return self.account.get_equity()
        except Exception:
            log.exception("UTA equity fetch failed")
            return None

    def available(self):
        if not self.account:
            return None
        try:
            return self.account.get_available()
        except Exception:
            log.exception("UTA available balance fetch failed")
            return None

    def account_state(self):
        if not self.account:
            return None
        try:
            return self.account.state()
        except Exception:
            log.exception("UTA account state fetch failed")
            return None

    def prepare(self, symbol, direction, quantity, entry, stop, target):
        ok, why = self.can_execute()
        if quantity <= 0:
            ok, why = False, "quantity<=0"
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
            return quantity
        try:
            return float(self.exchange.amount_to_precision(symbol, quantity))
        except Exception:
            return float(quantity)

    def execute(self, plan):
        ok, why = self.can_execute()
        if not ok:
            return {"success": False, "executed": False, "error": why}

        symbol = plan["symbol"]
        qty = self._precision_quantity(symbol, float(plan["quantity"]))
        if qty <= 0:
            return {"success": False, "executed": False, "error": "quantity rounded to zero"}

        side = "buy" if plan["direction"] == "LONG" else "sell"
        client_oid = "khtb-" + uuid.uuid4().hex[:20]

        try:
            try:
                self.account.set_leverage(symbol, self.settings.leverage)
            except Exception:
                # Do not hide a failed leverage request in LIVE; if the exchange
                # rejects it, the subsequent order will also be logged/blocked.
                log.warning("UTA set_leverage failed for %s", symbol, exc_info=True)

            ack = self.account.place_order(
                symbol, side, qty, client_oid,
                stop=plan["stop"], target=plan["target"],
            )
            order_id = str(ack.get("orderId") or "")
            if not order_id:
                return {
                    "success": False, "executed": False,
                    "error": f"missing orderId from UTA response: {ack}",
                    "reconcile_required": True,
                }

            info = {}
            deadline = time.time() + 12
            while time.time() < deadline:
                try:
                    info = self.account.order_info(symbol, order_id=order_id)
                    status = str(info.get("orderStatus") or "").lower()
                    if status in {"filled", "cancelled", "canceled"}:
                        break
                except Exception:
                    log.warning("Order status poll failed for %s", order_id, exc_info=True)
                time.sleep(0.8)

            status = str(info.get("orderStatus") or "").lower()
            filled = float(info.get("cumExecQty") or 0)
            avg = float(info.get("avgPrice") or 0)
            if status != "filled" or filled <= 0 or avg <= 0:
                return {
                    "success": False,
                    "executed": status in {"new", "live", "partially_filled"},
                    "order_id": order_id,
                    "average": avg or plan["entry"],
                    "filled_quantity": filled,
                    "order_status": status or "unknown",
                    "error": f"order not confirmed filled: status={status or 'unknown'}",
                    "reconcile_required": True,
                }

            return {
                "success": True,
                "executed": True,
                "order_id": order_id,
                "client_oid": client_oid,
                "average": avg,
                "filled_quantity": filled,
                "order_status": status,
                "environment": self.environment,
                "protection": {
                    "ok": True,
                    "type": "preset_TP_SL",
                    "stop": plan["stop"],
                    "target": plan["target"],
                },
            }
        except Exception as exc:
            log.exception("UTA order execution failed")
            return {
                "success": False,
                "executed": False,
                "error": f"{type(exc).__name__}:{exc}",
                "reconcile_required": True,
            }

    def close_all(self, symbol=None):
        ok, why = self.can_execute()
        if not ok:
            return {"success": False, "error": why}
        try:
            self.account.close_all(symbol)
            return {"success": True}
        except Exception as exc:
            log.exception("UTA close-all failed")
            return {"success": False, "error": f"{type(exc).__name__}:{exc}"}
