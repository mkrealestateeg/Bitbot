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
    """Bitget Unified Trading Account adapter for both DEMO and LIVE.

    Demo uses the same REST domain but requires the Demo API key and
    paptrading: 1 on every private request. Live omits that header.
    """

    def __init__(self, settings):
        self.settings = settings
        self.base_url = settings.bitget_base_url
        self.environment = settings.trading_env
        if self.environment == "DEMO":
            self.api_key = settings.demo_api_key
            self.api_secret = settings.demo_api_secret
            self.passphrase = settings.demo_api_password
        elif self.environment == "LIVE":
            self.api_key = settings.live_api_key
            self.api_secret = settings.live_api_secret
            self.passphrase = settings.live_api_password
        else:
            self.api_key = self.api_secret = self.passphrase = ""
        self.session = requests.Session()

    @property
    def enabled(self) -> bool:
        return bool(self.api_key and self.api_secret and self.passphrase)

    def _signed_request(self, method: str, path: str, params=None, body=None):
        if not self.enabled:
            raise RuntimeError(f"Bitget {self.environment} credentials are missing")
        method = method.upper()
        params = params or {}
        body = body or {}
        query = urlencode(sorted((str(k), str(v)) for k, v in params.items()))
        request_path = path + (f"?{query}" if query else "")
        body_text = json.dumps(body, separators=(",", ":"), ensure_ascii=False) if method != "GET" else ""
        timestamp = str(int(time.time() * 1000))
        prehash = timestamp + method + request_path + body_text
        digest = hmac.new(
            self.api_secret.encode("utf-8"),
            prehash.encode("utf-8"),
            hashlib.sha256,
        ).digest()
        sign = base64.b64encode(digest).decode()
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
        response = self.session.request(
            method,
            url,
            headers=headers,
            json=body if method != "GET" else None,
            timeout=self.settings.request_timeout,
        )
        try:
            payload = response.json()
        except ValueError:
            response.raise_for_status()
            raise RuntimeError(f"Bitget returned non-JSON HTTP {response.status_code}")
        if response.status_code >= 400:
            raise RuntimeError(
                f"Bitget HTTP {response.status_code}: {payload.get('code')} {payload.get('msg')}"
            )
        if str(payload.get("code")) != "00000":
            raise RuntimeError(f"Bitget API {payload.get('code')}: {payload.get('msg')}")
        return payload.get("data")

    def assets(self):
        return self._signed_request("GET", "/api/v3/account/assets") or {}

    def get_equity(self):
        data = self.assets()
        for key in ("usdtEquity", "accountEquity"):
            try:
                value = float(data.get(key))
                if value > 0:
                    return value
            except (TypeError, ValueError):
                pass
        for asset in data.get("assets") or []:
            if str(asset.get("coin", "")).upper() == "USDT":
                for key in ("equity", "balance"):
                    try:
                        value = float(asset.get(key))
                        if value > 0:
                            return value
                    except (TypeError, ValueError):
                        pass
        raise RuntimeError("USDT equity not present in Bitget UTA response")

    def get_available(self):
        data = self.assets()
        for asset in data.get("assets") or []:
            if str(asset.get("coin", "")).upper() == "USDT":
                for key in ("available", "equity", "balance"):
                    try:
                        value = float(asset.get(key))
                        if value >= 0:
                            return value
                    except (TypeError, ValueError):
                        pass
        # In some account states the top-level equity is the only usable value.
        for key in ("usdtEquity", "accountEquity"):
            try:
                value = float(data.get(key))
                if value >= 0:
                    return value
            except (TypeError, ValueError):
                pass
        raise RuntimeError("USDT available balance not present in Bitget UTA response")

    def state(self):
        data = self.assets()
        usdt = next(
            (a for a in data.get("assets") or []
             if str(a.get("coin", "")).upper() == "USDT"),
            {},
        )
        return {
            "environment": self.environment,
            "account_type": "UTA",
            "currency": "USDT",
            "equity": float(data.get("usdtEquity") or data.get("accountEquity") or usdt.get("equity") or 0),
            "available": float(usdt.get("available") or usdt.get("equity") or usdt.get("balance") or 0),
            "unrealised_pnl": float(data.get("usdtUnrealisedPnl") or data.get("unrealisedPnl") or 0),
            "position_value": float(data.get("positionValue") or 0),
            "leverage": float(data.get("leverage") or 0),
        }

    def account_settings(self):
        return self._signed_request("GET", "/api/v3/account/settings") or {}

    def set_leverage(self, symbol: str, leverage: int):
        body = {
            "leverage": str(leverage),
            "symbol": self._symbol(symbol),
            "productType": "usdt-futures",
            "marginMode": self.settings.margin_mode,
        }
        if self.settings.position_mode == "hedge" and self.settings.margin_mode == "isolated":
            body["posSide"] = "long"
        return self._signed_request("POST", "/api/v3/account/set-leverage", body=body)

    def place_order(self, symbol, side, qty, client_oid, stop=None, target=None):
        body = {
            "category": self.settings.product_category,
            "symbol": self._symbol(symbol),
            "qty": str(qty),
            "side": side,
            "orderType": "market",
            "clientOid": client_oid,
            "reduceOnly": "no",
            "marginMode": self.settings.margin_mode,
        }
        if self.settings.position_mode == "hedge":
            body["posSide"] = "long" if side == "buy" else "short"
        if target is not None:
            body.update({
                "takeProfit": str(target),
                "tpTriggerBy": "mark",
                "tpOrderType": "market",
            })
        if stop is not None:
            body.update({
                "stopLoss": str(stop),
                "slTriggerBy": "mark",
                "slOrderType": "market",
            })
        return self._signed_request("POST", "/api/v3/trade/place-order", body=body) or {}

    def order_info(self, symbol, order_id=None, client_oid=None):
        params = {
            "category": self.settings.product_category,
            "symbol": self._symbol(symbol),
        }
        if order_id:
            params["orderId"] = order_id
        if client_oid:
            params["clientOid"] = client_oid
        return self._signed_request("GET", "/api/v3/trade/order-info", params=params) or {}

    def current_positions(self, symbol=None):
        params = {"category": self.settings.product_category}
        if symbol:
            params["symbol"] = self._symbol(symbol)
        data = self._signed_request("GET", "/api/v3/position/current-position", params=params) or {}
        return data.get("list", data if isinstance(data, list) else [])

    def close_all(self, symbol=None):
        body = {"category": self.settings.product_category}
        if symbol:
            body["symbol"] = self._symbol(symbol)
        return self._signed_request("POST", "/api/v3/trade/close-positions", body=body)

    @staticmethod
    def _symbol(symbol):
        return symbol.split(":")[0].replace("/", "").upper()


class CCXTAccountAdapter(PlatformAccountAdapter):
    """Compatibility adapter for non-Bitget platforms in future revisions."""

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
