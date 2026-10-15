from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import time
from typing import Any, Dict, Optional
from urllib.parse import urlencode

import requests

log = logging.getLogger(__name__)


class PlatformAccountAdapter:
    """Platform-neutral account contract used by risk/execution."""

    def get_equity(self) -> Optional[float]:
        raise NotImplementedError

    def get_available(self) -> Optional[float]:
        raise NotImplementedError

    def state(self) -> Dict[str, Any]:
        return {
            "equity": self.get_equity(),
            "available": self.get_available(),
        }


class BitgetUTAAdapter(PlatformAccountAdapter):
    """Bitget Unified Trading Account adapter for DEMO and LIVE."""

    def __init__(self, settings):
        self.settings = settings
        self.base_url = settings.bitget_base_url.rstrip("/")
        self.environment = str(settings.trading_env).upper()

        if self.environment == "DEMO":
            self.api_key = settings.demo_api_key
            self.api_secret = settings.demo_api_secret
            self.passphrase = settings.demo_api_password
        elif self.environment == "LIVE":
            self.api_key = settings.live_api_key
            self.api_secret = settings.live_api_secret
            self.passphrase = settings.live_api_password
        else:
            self.api_key = ""
            self.api_secret = ""
            self.passphrase = ""

        self.session = requests.Session()

    @property
    def enabled(self) -> bool:
        return bool(
            self.api_key
            and self.api_secret
            and self.passphrase
        )

    def _signed_request(
        self,
        method: str,
        path: str,
        params=None,
        body=None,
    ):
        if not self.enabled:
            raise RuntimeError(
                f"Bitget {self.environment} credentials are missing"
            )

        method = method.upper()
        params = params or {}
        body = body or {}

        query = urlencode(
            sorted(
                (str(key), str(value))
                for key, value in params.items()
                if value is not None
            )
        )

        request_path = path + (f"?{query}" if query else "")

        body_text = (
            json.dumps(
                body,
                separators=(",", ":"),
                ensure_ascii=False,
            )
            if method != "GET"
            else ""
        )

        timestamp = str(int(time.time() * 1000))
        prehash = timestamp + method + request_path + body_text

        digest = hmac.new(
            self.api_secret.encode("utf-8"),
            prehash.encode("utf-8"),
            hashlib.sha256,
        ).digest()

        sign = base64.b64encode(digest).decode("utf-8")

        headers = {
            "ACCESS-KEY": self.api_key,
            "ACCESS-SIGN": sign,
            "ACCESS-PASSPHRASE": self.passphrase,
            "ACCESS-TIMESTAMP": timestamp,
            "locale": "en-US",
            "Content-Type": "application/json",
        }

        # Bitget demo trading requires this header.
        if self.environment == "DEMO":
            headers["paptrading"] = "1"

        url = self.base_url + request_path

        try:
            response = self.session.request(
                method,
                url,
                headers=headers,
                json=body if method != "GET" else None,
                timeout=self.settings.request_timeout,
            )
        except requests.RequestException:
            log.exception(
                "Bitget request failed: method=%s path=%s",
                method,
                path,
            )
            # Do not retry order submissions here. The exchange may
            # have received an order even if the client timed out.
            raise

        try:
            payload = response.json()
        except ValueError as exc:
            response.raise_for_status()
            raise RuntimeError(
                "Bitget returned non-JSON HTTP "
                f"{response.status_code}"
            ) from exc

        if response.status_code >= 400:
            if isinstance(payload, dict):
                code = payload.get("code")
                message = payload.get("msg")
            else:
                code = None
                message = str(payload)

            raise RuntimeError(
                f"Bitget HTTP {response.status_code}: "
                f"{code} {message}"
            )

        if not isinstance(payload, dict):
            raise RuntimeError(
                "Bitget returned an unexpected response format"
            )

        if str(payload.get("code")) != "00000":
            raise RuntimeError(
                f"Bitget API {payload.get('code')}: "
                f"{payload.get('msg')}"
            )

        return payload.get("data")

    # ---------------------------------------------------------
    # Account balance and equity
    # ---------------------------------------------------------

    def assets(self):
        data = self._signed_request(
            "GET",
            "/api/v3/account/assets",
        )

        if not isinstance(data, dict):
            raise RuntimeError(
                "Unexpected Bitget account assets response"
            )

        return data

    @staticmethod
    def _number(value):
        try:
            result = float(value)
            if result != float("inf") and result == result:
                return result
        except (TypeError, ValueError):
            pass
        return None

    def get_equity(self):
        data = self.assets()

        for key in ("usdtEquity", "accountEquity"):
            value = self._number(data.get(key))
            if value is not None and value > 0:
                return value

        for asset in data.get("assets") or []:
            if str(asset.get("coin", "")).upper() == "USDT":
                for key in ("equity", "balance"):
                    value = self._number(asset.get(key))
                    if value is not None and value > 0:
                        return value

        raise RuntimeError(
            "USDT equity not present in Bitget UTA response"
        )

    def get_available(self):
        data = self.assets()

        for asset in data.get("assets") or []:
            if str(asset.get("coin", "")).upper() == "USDT":
                for key in ("available", "equity", "balance"):
                    value = self._number(asset.get(key))
                    if value is not None and value >= 0:
                        return value

        for key in ("usdtEquity", "accountEquity"):
            value = self._number(data.get(key))
            if value is not None and value >= 0:
                return value

        raise RuntimeError(
            "USDT available balance not present in Bitget UTA response"
        )

    def state(self):
        data = self.assets()

        usdt = next(
            (
                asset
                for asset in data.get("assets") or []
                if str(asset.get("coin", "")).upper() == "USDT"
            ),
            {},
        )

        return {
            "environment": self.environment,
            "account_type": "UTA",
            "currency": "USDT",
            "equity": self._number(
                data.get("usdtEquity")
                or data.get("accountEquity")
                or usdt.get("equity")
            ) or 0.0,
            "available": self._number(
                usdt.get("available")
                if usdt.get("available") is not None
                else usdt.get("equity")
                if usdt.get("equity") is not None
                else usdt.get("balance")
            ) or 0.0,
            "unrealised_pnl": self._number(
                data.get("usdtUnrealisedPnl")
                or data.get("unrealisedPnl")
            ) or 0.0,
            "position_value": self._number(
                data.get("positionValue")
            ) or 0.0,
            "leverage": self._number(
                data.get("leverage")
            ) or 0.0,
        }

    def account_settings(self):
        return (
            self._signed_request(
                "GET",
                "/api/v3/account/settings",
            )
            or {}
        )

    # ---------------------------------------------------------
    # Leverage
    # ---------------------------------------------------------

    def set_leverage(self, symbol: str, leverage: int):
        """Set leverage and fail if the exchange rejects the request."""

        try:
            leverage_value = int(leverage)
        except (TypeError, ValueError) as exc:
            raise ValueError("Leverage must be a positive integer") from exc

        if leverage_value < 1:
            raise ValueError("Leverage must be at least 1")

        margin_mode = str(
            self.settings.margin_mode
        ).lower()

        if margin_mode not in ("isolated", "crossed"):
            raise ValueError(
                "Unsupported margin_mode: "
                f"{self.settings.margin_mode!r}"
            )

        body = {
            "category": self.settings.product_category,
            "symbol": self._symbol(symbol),
            "leverage": str(leverage_value),
            "marginMode": margin_mode,
        }

        if margin_mode == "isolated":
            if self.settings.position_mode == "hedge":
                # Hedge mode can require separate leverage requests
                # for long and short positions.
                results = {}

                for pos_side in ("long", "short"):
                    side_body = {
                        **body,
                        "posSide": pos_side,
                    }

                    results[pos_side] = self._signed_request(
                        "POST",
                        "/api/v3/account/set-leverage",
                        body=side_body,
                    )

                return results

            # Isolated one-way mode.
            body["posSide"] = "long"

        return self._signed_request(
            "POST",
            "/api/v3/account/set-leverage",
            body=body,
        )

    # ---------------------------------------------------------
    # Orders
    # ---------------------------------------------------------

    def place_order(
        self,
        symbol,
        side,
        qty,
        client_oid,
        stop=None,
        target=None,
    ):
        if not client_oid:
            raise ValueError(
                "client_oid is required for order reconciliation"
            )

        try:
            quantity = float(qty)
        except (TypeError, ValueError) as exc:
            raise ValueError("Order quantity must be numeric") from exc

        if quantity <= 0:
            raise ValueError("Order quantity must be positive")

        side = str(side).lower()
        if side not in ("buy", "sell"):
            raise ValueError(
                "Order side must be 'buy' or 'sell'"
            )

        body = {
            "category": self.settings.product_category,
            "symbol": self._symbol(symbol),
            "qty": str(qty),
            "side": side,
            "orderType": "market",
            "clientOid": str(client_oid),
            "reduceOnly": "no",
            "marginMode": self.settings.margin_mode,
        }

        if self.settings.position_mode == "hedge":
            body["posSide"] = (
                "long" if side == "buy" else "short"
            )

        if target is not None:
            target_value = self._number(target)
            if target_value is None or target_value <= 0:
                raise ValueError("Invalid take-profit price")

            body.update({
                "takeProfit": str(target),
                "tpTriggerBy": "mark",
                "tpOrderType": "market",
            })

        if stop is not None:
            stop_value = self._number(stop)
            if stop_value is None or stop_value <= 0:
                raise ValueError("Invalid stop-loss price")

            body.update({
                "stopLoss": str(stop),
                "slTriggerBy": "mark",
                "slOrderType": "market",
            })

        return (
            self._signed_request(
                "POST",
                "/api/v3/trade/place-order",
                body=body,
            )
            or {}
        )

    def order_info(
        self,
        symbol,
        order_id=None,
        client_oid=None,
    ):
        """Query a known order; never submit or resubmit an order here."""

        if not order_id and not client_oid:
            raise ValueError(
                "Either order_id or client_oid is required"
            )

        params = {
            "category": self.settings.product_category,
            "symbol": self._symbol(symbol),
        }

        if order_id:
            params["orderId"] = str(order_id)
        else:
            params["clientOid"] = str(client_oid)

        data = self._signed_request(
            "GET",
            "/api/v3/trade/order-info",
            params=params,
        )

        return data or {}

    # ---------------------------------------------------------
    # Positions
    # ---------------------------------------------------------

    def current_positions(self, symbol=None):
        params = {
            "category": self.settings.product_category,
        }

        if symbol:
            params["symbol"] = self._symbol(symbol)

        data = self._signed_request(
            "GET",
            "/api/v3/position/current-position",
            params=params,
        ) or {}

        if isinstance(data, list):
            return data

        if isinstance(data, dict):
            return data.get("list", [])

        return []

    def close_all(self, symbol=None):
        body = {
            "category": self.settings.product_category,
        }

        if symbol:
            body["symbol"] = self._symbol(symbol)

        return self._signed_request(
            "POST",
            "/api/v3/trade/close-positions",
            body=body,
        )

    @staticmethod
    def _symbol(symbol):
        return str(symbol).split(":")[0].replace("/", "").upper()


class CCXTAccountAdapter(PlatformAccountAdapter):
    """Compatibility adapter for other platforms in future revisions."""

    def __init__(self, exchange, settings):
        self.exchange = exchange
        self.settings = settings

    def get_equity(self):
        balance = self.exchange.fetch_balance()
        total = balance.get("total") or {}

        for key in ("USDT", "usdt"):
            try:
                value = float(total.get(key) or 0)
                if value > 0:
                    return value
            except (TypeError, ValueError):
                pass

        return None

    def get_available(self):
        balance = self.exchange.fetch_balance()
        free = balance.get("free") or {}

        for key in ("USDT", "usdt"):
            try:
                value = float(free.get(key) or 0)
                if value >= 0:
                    return value
            except (TypeError, ValueError):
                pass

        return None
