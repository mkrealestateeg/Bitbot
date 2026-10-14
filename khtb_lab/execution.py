from __future__ import annotations
import logging
import time
import uuid

log = logging.getLogger(__name__)


class DemoExecutionEngine:
    """Bitget USDT-perpetual execution, hard-gated to the Bitget Demo account."""
    def __init__(self, settings, market, db):
        self.settings = settings
        self.market = market
        self.db = db

    @property
    def enabled(self):
        return (
            self.settings.trading_env == "bitget_demo"
            and self.settings.enable_exchange_orders
            and self.settings.demo_armed
        )

    def _assert_enabled(self):
        if not self.enabled:
            raise RuntimeError("Bitget Demo execution is not armed")

    def _client_oid(self, prefix="khtb"):
        return f"{prefix}-{uuid.uuid4().hex[:20]}"

    def _filled_order(self, symbol, order):
        order_id = order.get("id")
        if not order_id:
            raise RuntimeError("Bitget returned no order id")
        current = order
        for _ in range(5):
            status = str(current.get("status") or "").lower()
            if status in {"closed", "filled"} or (current.get("filled") and float(current.get("filled") or 0) > 0):
                return current
            try:
                current = self.market.exchange.fetch_order(order_id, symbol)
            except Exception:
                pass
            time.sleep(0.6)
        return current

    def _extract_fill(self, order, fallback_price):
        average = order.get("average") or order.get("price") or fallback_price
        filled = order.get("filled") or order.get("amount")
        return float(average), float(filled or 0)

    def open(self, symbol, signal, mode="AUTO", profile="SAFE"):
        self._assert_enabled()
        direction = signal.get("direction")
        if direction not in {"LONG", "SHORT"}:
            return None, "No directional setup"

        open_local = [t for t in self.db.open_trades() if t["symbol"] == symbol and t["source"] == "BITGET_DEMO"]
        if open_local:
            return None, "Local Demo position already open"

        actual = self.market.validate_symbol(symbol)
        self.market.set_one_way_mode()
        try:
            self.market.set_margin_mode(actual, self.settings.margin_mode)
        except Exception as exc:
            log.warning("Bitget margin mode setup returned: %s", exc)
        self.market.set_leverage(actual, self.settings.leverage, self.settings.margin_mode)

        ticker = self.market.ticker(actual)
        entry_reference = float(ticker.get("last") or signal["price"])
        stop = float(signal["stop"])
        target = float(signal["target"])
        risk_fraction = self.settings.risk_per_trade * (0.5 if profile == "SAFE" else 1.0)
        distance = abs(entry_reference - stop)
        if distance <= 0:
            return None, "Invalid stop distance"

        # Risk sizing, then a hard margin cap so the demo bot stays in the intended small-account range.
        risk_cash = self.settings.starting_capital * risk_fraction
        base_qty = risk_cash / distance
        max_notional = self.settings.max_margin_per_trade * self.settings.leverage
        base_qty = min(base_qty, max_notional / entry_reference)

        market_info = self.market.market(actual)
        contract_size = float(market_info.get("contractSize") or 1.0)
        contracts = base_qty / contract_size
        contracts = self.market.amount_to_precision(actual, contracts)
        if contracts <= 0:
            return None, "Calculated order quantity is below exchange precision"

        min_amount = ((market_info.get("limits") or {}).get("amount") or {}).get("min")
        if min_amount is not None and contracts < float(min_amount):
            return None, f"Quantity {contracts} is below Bitget minimum {min_amount}"

        side = "buy" if direction == "LONG" else "sell"
        params = {
            "marginMode": self.settings.margin_mode,
            "reduceOnly": False,
            "clientOid": self._client_oid("khtb-open"),
            # Bitget futures supports preset SL/TP on the opening order.
            "presetStopLossPrice": str(self.market.price_to_precision(actual, stop)),
            "presetStopSurplusPrice": str(self.market.price_to_precision(actual, target)),
            "presetStopLossType": "mark_price",
            "presetStopSurplusType": "mark_price",
        }

        log.info(
            "DEMO ORDER SUBMIT symbol=%s side=%s qty=%s leverage=%s SL=%s TP=%s",
            actual, side, contracts, self.settings.leverage, stop, target,
        )
        order = self.market.exchange.create_order(actual, "market", side, contracts, None, params)
        order = self._filled_order(actual, order)
        entry, filled = self._extract_fill(order, entry_reference)
        if filled <= 0:
            raise RuntimeError(f"Demo entry order {order.get('id')} was not filled")

        # Verify that the position exists and Bitget reports both protections.
        position = None
        for _ in range(5):
            try:
                position = self.market.fetch_position(actual)
                if position and float(position.get("contracts") or position.get("info", {}).get("total") or 0) > 0:
                    break
            except Exception:
                pass
            time.sleep(0.6)

        info = (position or {}).get("info") or {}
        stop_id = info.get("stopLossId") or (position or {}).get("stopLossId")
        tp_id = info.get("takeProfitId") or (position or {}).get("takeProfitId")
        stop_value = info.get("stopLoss") or (position or {}).get("stopLoss")
        tp_value = info.get("takeProfit") or (position or {}).get("takeProfit")

        if not stop_id or not tp_id or not stop_value or not tp_value:
            # Never leave a Demo position naked if the exchange did not confirm both protections.
            log.error("DEMO PROTECTION VERIFICATION FAILED order=%s position=%s", order.get("id"), position)
            try:
                self.market.close_position(actual)
            finally:
                raise RuntimeError("Bitget Demo entry was filled but exchange-side SL/TP could not be verified; position was closed for safety")

        trade_id = self.db.open_trade(
            symbol, direction, entry, stop, target, filled,
            mode=mode, source="BITGET_DEMO", snapshot=signal.get("snapshot"),
            exchange_order_id=str(order.get("id")), exchange_client_order_id=str(order.get("clientOrderId") or params["clientOid"]),
            exchange_stop_id=str(stop_id), exchange_take_profit_id=str(tp_id), exchange_entry_price=entry,
        )
        return trade_id, f"DEMO OPEN #{trade_id} order={order.get('id')} entry={entry:.8g} qty={filled:.8g} SL={stop:.8g} TP={target:.8g}"

    def reconcile(self):
        if self.settings.trading_env != "bitget_demo":
            return []
        closed = []
        for trade in self.db.open_trades():
            if trade.get("source") != "BITGET_DEMO":
                continue
            try:
                position = self.market.fetch_position(trade["symbol"])
                contracts = float(position.get("contracts") or position.get("info", {}).get("total") or 0) if position else 0
                if contracts > 0:
                    continue

                since = int(__import__("datetime").datetime.fromisoformat(trade["ts_open"].replace("Z", "+00:00")).timestamp() * 1000)
                history = self.market.fetch_position_history(trade["symbol"], since_ms=since)
                candidates = []
                for item in history or []:
                    info = item.get("info") or {}
                    symbol = item.get("symbol") or info.get("symbol")
                    if symbol == trade["symbol"] or str(symbol).replace("USDT", "/USDT:USDT").upper() == trade["symbol"].upper():
                        candidates.append(item)
                if not candidates:
                    continue
                item = candidates[0]
                info = item.get("info") or {}
                exit_price = float(item.get("closeAvgPrice") or info.get("closeAvgPrice") or trade["entry"])
                pnl = float(item.get("realizedPnl") or item.get("pnl") or info.get("pnl") or info.get("netProfit") or 0)
                reason = "EXCHANGE_CLOSE"
                if trade.get("exchange_stop_id") and str(trade["exchange_stop_id"]) in str(info):
                    reason = "STOP_LOSS_OR_EXCHANGE"
                self.db.close_trade(trade["id"], exit_price, pnl, reason)
                closed.append((trade["id"], reason, pnl, exit_price))
            except Exception:
                log.exception("Demo reconciliation failed for trade #%s", trade["id"])
        return closed

    def close_all(self):
        self._assert_enabled()
        results = []
        for trade in self.db.open_trades():
            if trade.get("source") != "BITGET_DEMO":
                continue
            try:
                result = self.market.close_position(trade["symbol"])
                results.append((trade["id"], trade["symbol"], result))
            except Exception:
                log.exception("Demo close failed for trade #%s", trade["id"])
        return results
