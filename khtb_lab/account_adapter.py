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

        if self.environment == "DEMO":
            headers["paptrading"] = "1"

        url = self.base_url + request_path

        try:
            response = self.session.request(
                method,
                url,
                headers=headers,
                data=body_text if method != "GET" else None,
                timeout=self.settings.request_timeout,
            )
        except requests.RequestException:
            log.exception(
                "Bitget request failed: method=%s path=%s",
                method,
                path,
            )
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

            if path == "/api/v3/trade/place-order":
                order_context = {
                    key: body[key]
                    for key in (
                        "category",
                        "symbol",
                        "qty",
                        "side",
                        "orderType",
                        "clientOid",
                        "reduceOnly",
                        "marginMode",
                        "posSide",
                    )
                    if key in body
                }
                log.error(
                    "BITGET ENTRY ORDER REJECTED | http_status=%s | "
                    "code=%s | message=%s | params=%s",
                    response.status_code,
                    code,
                    message,
                    order_context,
                )

            raise RuntimeError(
                f"Bitget HTTP {response.status_code}: "
                f"{code} {message}"
            )

        if not isinstance(payload, dict):
            raise RuntimeError(
                "Bitget returned an unexpected response format"
            )

        if str(payload.get("code")) != "00000":
            if path == "/api/v3/trade/place-order":
                order_context = {
                    key: body[key]
                    for key in (
                        "category",
                        "symbol",
                        "qty",
                        "side",
                        "orderType",
                        "clientOid",
                        "reduceOnly",
                        "marginMode",
                        "posSide",
                    )
                    if key in body
                }
                log.error(
                    "BITGET ENTRY ORDER REJECTED | http_status=%s | "
                    "code=%s | message=%s | params=%s",
                    response.status_code,
                    payload.get("code"),
                    payload.get("msg"),
                    order_context,
                )

            raise RuntimeError(
                f"Bitget API {payload.get('code')}: "
                f"{payload.get('msg')}"
            )

        return payload.get("data")

    # ---------------------------------------------------------
    # Helpers
    # ---------------------------------------------------------

    @staticmethod
    def _number(value):
        try:
            result = float(value)

            if result != float("inf") and result == result:
                return result

        except (TypeError, ValueError):
            pass

        return None

    @staticmethod
    def _safe_keys(value):
        if isinstance(value, dict):
            return list(value.keys())

        if isinstance(value, list):
            return [
                f"list[{index}]"
                for index in range(min(len(value), 5))
            ]

        return [type(value).__name__]

    @classmethod
    def _safe_response_shape(cls, value):
        """Describe response structure without logging response values."""
        shape = {"type": type(value).__name__}
        if isinstance(value, dict):
            shape["keys"] = sorted(str(key) for key in value)
            for key in ("list", "positions", "positionList"):
                if key not in value:
                    continue
                nested = value[key]
                shape[key] = {
                    "type": type(nested).__name__,
                    "length": len(nested) if isinstance(nested, list) else None,
                    "item_keys": (
                        sorted(str(item_key) for item_key in nested[0])
                        if isinstance(nested, list)
                        and nested
                        and isinstance(nested[0], dict)
                        else None
                    ),
                }
        elif isinstance(value, list):
            shape["length"] = len(value)
            shape["item_keys"] = (
                sorted(str(key) for key in value[0])
                if value and isinstance(value[0], dict)
                else None
            )
        return shape

    @staticmethod
    def _find_usdt_asset(data):
        """
        Find the USDT asset regardless of whether Bitget returns
        assets directly, nested under data, or in a list.
        """

        if isinstance(data, dict):
            assets = data.get("assets")

            if isinstance(assets, list):
                for asset in assets:
                    if not isinstance(asset, dict):
                        continue

                    if str(
                        asset.get("coin", "")
                    ).upper() == "USDT":
                        return asset

            # Some wrappers may return a single asset object.
            if str(data.get("coin", "")).upper() == "USDT":
                return data

        elif isinstance(data, list):
            for item in data:
                if not isinstance(item, dict):
                    continue

                if str(
                    item.get("coin", "")
                ).upper() == "USDT":
                    return item

                nested = item.get("assets")

                if isinstance(nested, list):
                    for asset in nested:
                        if not isinstance(asset, dict):
                            continue

                        if str(
                            asset.get("coin", "")
                        ).upper() == "USDT":
                            return asset

        return {}

    @staticmethod
    def _account_object(data):
        """
        Normalize the account-assets response into the dictionary
        containing account-level equity fields.
        """

        if isinstance(data, dict):
            return data

        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict):
                    if any(
                        key in item
                        for key in (
                            "accountEquity",
                            "usdtEquity",
                            "effEquity",
                            "assets",
                        )
                    ):
                        return item

            if data and isinstance(data[0], dict):
                return data[0]

        return {}

    # ---------------------------------------------------------
    # Account balance and equity
    # ---------------------------------------------------------

    def assets(self):
        data = self._signed_request(
            "GET",
            "/api/v3/account/assets",
        )

        if not isinstance(data, (dict, list)):
            raise RuntimeError(
                "Unexpected Bitget account assets response: "
                f"{type(data).__name__}"
            )

        account = self._account_object(data)

        log.debug(
            "BITGET UTA ASSETS RESPONSE | type=%s | account_keys=%s",
            type(data).__name__,
            self._safe_keys(account),
        )

        return data

    def get_equity(self):
        data = self.assets()

        account = self._account_object(data)
        usdt = self._find_usdt_asset(data)

        # Official UTA account-level fields.
        for key in (
            "usdtEquity",
            "accountEquity",
            "effEquity",
        ):
            value = self._number(account.get(key))

            if value is not None and value >= 0:
                log.info(
                    "BITGET EQUITY FOUND | field=%s | equity=%.8f",
                    key,
                    value,
                )
                return value

        # Fallback to the USDT asset itself.
        for key in (
            "equity",
            "balance",
            "balanceOriginal",
            "available",
        ):
            value = self._number(usdt.get(key))

            if value is not None and value >= 0:
                log.info(
                    "BITGET USDT EQUITY FOUND | field=%s | equity=%.8f",
                    key,
                    value,
                )
                return value

        log.error(
            "BITGET EQUITY PARSE FAILED | "
            "response_type=%s | account_keys=%s | usdt_keys=%s",
            type(data).__name__,
            self._safe_keys(account),
            self._safe_keys(usdt),
        )

        raise RuntimeError(
            "USDT/account equity not present in Bitget UTA response"
        )

    def get_available(self):
        data = self.assets()

        account = self._account_object(data)
        usdt = self._find_usdt_asset(data)

        # USDT asset available balance is the preferred value.
        for key in (
            "available",
            "equity",
            "balance",
            "balanceOriginal",
        ):
            value = self._number(usdt.get(key))

            if value is not None and value >= 0:
                log.info(
                    "BITGET AVAILABLE FOUND | field=%s | available=%.8f",
                    key,
                    value,
                )
                return value

        # Account-level fallbacks.
        for key in (
            "usdtEquity",
            "accountEquity",
            "effEquity",
        ):
            value = self._number(account.get(key))

            if value is not None and value >= 0:
                log.info(
                    "BITGET AVAILABLE FALLBACK | field=%s | available=%.8f",
                    key,
                    value,
                )
                return value

        log.error(
            "BITGET AVAILABLE PARSE FAILED | "
            "response_type=%s | account_keys=%s | usdt_keys=%s",
            type(data).__name__,
            self._safe_keys(account),
            self._safe_keys(usdt),
        )

        raise RuntimeError(
            "USDT available balance not present in Bitget UTA response"
        )

    def state(self):
        data = self.assets()

        account = self._account_object(data)
        usdt = self._find_usdt_asset(data)

        equity = (
            self._number(account.get("usdtEquity"))
            or self._number(account.get("accountEquity"))
            or self._number(account.get("effEquity"))
            or self._number(usdt.get("equity"))
            or self._number(usdt.get("balance"))
            or 0.0
        )

        available = (
            self._number(usdt.get("available"))
            if self._number(usdt.get("available")) is not None
            else self._number(usdt.get("equity"))
            if self._number(usdt.get("equity")) is not None
            else self._number(usdt.get("balance"))
            if self._number(usdt.get("balance")) is not None
            else equity
        )

        return {
            "environment": self.environment,
            "account_type": "UTA",
            "currency": "USDT",
            "equity": equity,
            "available": available or 0.0,
            "unrealised_pnl": (
                self._number(account.get("usdtUnrealisedPnl"))
                or self._number(account.get("unrealisedPnl"))
                or 0.0
            ),
            "position_value": (
                self._number(account.get("positionValue"))
                or 0.0
            ),
            "leverage": (
                self._number(account.get("leverage"))
                or 0.0
            ),
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
        try:
            leverage_value = int(leverage)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "Leverage must be a positive integer"
            ) from exc

        if leverage_value < 1:
            raise ValueError(
                "Leverage must be at least 1"
            )

        margin_mode = str(
            self.settings.margin_mode
        ).lower()

        if margin_mode not in (
            "isolated",
            "crossed",
        ):
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
                results = {}

                for pos_side in (
                    "long",
                    "short",
                ):
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
    ):
        if not client_oid:
            raise ValueError(
                "client_oid is required for order reconciliation"
            )

        try:
            quantity = float(qty)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "Order quantity must be numeric"
            ) from exc

        if quantity <= 0:
            raise ValueError(
                "Order quantity must be positive"
            )

        side = str(side).lower()

        if side not in (
            "buy",
            "sell",
        ):
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
                "long"
                if side == "buy"
                else "short"
            )

        return (
            self._signed_request(
                "POST",
                "/api/v3/trade/place-order",
                body=body,
            )
            or {}
        )

    def place_tpsl_order(
        self,
        symbol,
        side,
        client_oid,
        stop,
        target,
        pos_side=None,
    ):
        if not client_oid:
            raise ValueError(
                "client_oid is required for TPSL order reconciliation"
            )

        side = str(side).lower()
        if side not in {"buy", "sell"}:
            raise ValueError("TPSL side must be 'buy' or 'sell'")

        stop_value = self._number(stop)
        target_value = self._number(target) if target is not None else None
        if stop_value is None or stop_value <= 0:
            raise ValueError("Invalid stop-loss price")
        if target is not None and (
            target_value is None or target_value <= 0
        ):
            raise ValueError("Invalid take-profit price")

        body = {
            "category": self.settings.product_category,
            "symbol": self._symbol(symbol),
            "type": "tpsl",
            "tpslMode": "full",
            "side": side,
            "clientOid": str(client_oid),
            "marginMode": self.settings.margin_mode,
            "takeProfit": (
                str(target) if target_value is not None else ""
            ),
            "tpTriggerBy": (
                "mark" if target_value is not None else ""
            ),
            "tpOrderType": (
                "market" if target_value is not None else ""
            ),
            "stopLoss": str(stop),
            "slTriggerBy": "mark",
            "slOrderType": "market",
        }

        if self.settings.position_mode == "hedge":
            normalized_pos_side = str(pos_side or "").lower()
            if normalized_pos_side not in {"long", "short"}:
                raise ValueError(
                    "pos_side must be 'long' or 'short' in hedge mode"
                )
            body["posSide"] = normalized_pos_side
        else:
            body["reduceOnly"] = "yes"

        return (
            self._signed_request(
                "POST",
                "/api/v3/trade/place-strategy-order",
                body=body,
            )
            or {}
        )

    def modify_tpsl_order(
        self,
        symbol,
        order_id,
        client_oid,
        quantity,
        stop,
        target,
        pos_side=None,
    ):
        if not order_id and not client_oid:
            raise ValueError(
                "order_id or client_oid is required to modify TPSL"
            )

        stop_value = self._number(stop)
        if stop_value is None or stop_value <= 0:
            raise ValueError("Invalid stop-loss price")

        target_value = self._number(target) if target is not None else None
        if target is not None and (
            target_value is None or target_value <= 0
        ):
            raise ValueError("Invalid take-profit price")

        quantity_value = self._number(quantity)
        if quantity_value is None or quantity_value <= 0:
            raise ValueError("Invalid TPSL quantity")

        body = {
            "category": self.settings.product_category,
            "symbol": self._symbol(symbol),
            "orderId": str(order_id or ""),
            "clientOid": str(client_oid or ""),
            "qty": str(quantity),
            "tpTriggerBy": "mark" if target_value is not None else "",
            "slTriggerBy": "mark",
            "takeProfit": (
                str(target) if target_value is not None else ""
            ),
            "stopLoss": str(stop),
            "tpOrderType": "market" if target_value is not None else "",
            "slOrderType": "market",
            "tpLimitPrice": "",
            "slLimitPrice": "",
        }
        if self.settings.position_mode == "hedge":
            normalized_pos_side = str(pos_side or "").lower()
            if normalized_pos_side not in {"long", "short"}:
                raise ValueError(
                    "pos_side must be 'long' or 'short' in hedge mode"
                )
            body["posSide"] = normalized_pos_side

        return (
            self._signed_request(
                "POST",
                "/api/v3/trade/modify-strategy-order",
                body=body,
            )
            or {}
        )

    def unfilled_strategy_orders(self):
        data = self._signed_request(
            "GET",
            "/api/v3/trade/unfilled-strategy-orders",
            params={
                "category": self.settings.product_category,
                "type": "tpsl",
            },
        )

        if data is None:
            return []
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            orders = data.get("list")
            if isinstance(orders, list):
                return orders
        log.error(
            "BITGET UNFILLED STRATEGY ORDERS RESPONSE SHAPE INVALID | "
            "shape=%s",
            self._safe_response_shape(data),
        )
        raise RuntimeError(
            "Bitget returned an unexpected strategy-order response"
        )

    def order_info(
        self,
        symbol,
        order_id=None,
        client_oid=None,
    ):
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
        )

        # A successful UTA response may have a null data field when there
        # are no positions. _signed_request has already validated code=00000.
        if data is None:
            return []
        if isinstance(data, list):
            return data

        if isinstance(data, dict):
            for key in ("list", "positions", "positionList"):
                if key not in data:
                    continue
                positions = data[key]
                if positions is None:
                    return []
                if isinstance(positions, list):
                    return positions

        log.error(
            "BITGET CURRENT POSITIONS RESPONSE SHAPE INVALID | shape=%s",
            self._safe_response_shape(data),
        )
        raise RuntimeError(
            "Bitget returned an unexpected current-position response"
        )

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
        return (
            str(symbol)
            .split(":")[0]
            .replace("/", "")
            .upper()
        )


class CCXTAccountAdapter(PlatformAccountAdapter):
    """Compatibility adapter for other platforms in future revisions."""

    def __init__(self, exchange, settings):
        self.exchange = exchange
        self.settings = settings

    def get_equity(self):
        balance = self.exchange.fetch_balance()
        total = balance.get("total") or {}

        for key in (
            "USDT",
            "usdt",
        ):
            try:
                value = float(
                    total.get(key) or 0
                )

                if value > 0:
                    return value

            except (TypeError, ValueError):
                pass

        return None

    def get_available(self):
        balance = self.exchange.fetch_balance()
        free = balance.get("free") or {}

        for key in (
            "USDT",
            "usdt",
        ):
            try:
                value = float(
                    free.get(key) or 0
                )

                if value >= 0:
                    return value

            except (TypeError, ValueError):
                pass

        return None
