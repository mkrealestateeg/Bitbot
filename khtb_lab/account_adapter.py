from __future__ import annotations
import logging
log=logging.getLogger(__name__)

class PlatformAccountAdapter:
    def __init__(self,exchange,settings): self.exchange=exchange;self.settings=settings
    def get_equity(self): raise NotImplementedError
    def get_available(self): raise NotImplementedError

class CCXTAccountAdapter(PlatformAccountAdapter):
    def _balance(self):
        params={"type":"swap"}
        if self.settings.exchange_id=="bitget":
            params["productType"]="USDT-FUTURES"
        return self.exchange.fetch_balance(params)
    def _value(self,balance,names):
        for bucket in ("total","free","used"):
            d=balance.get(bucket) or {}
            for name in names:
                try:
                    v=float(d.get(name) or 0)
                    if v>0:return v
                except:pass
        info=balance.get("info") or {}
        if isinstance(info,dict):
            for name in names+["accountEquity","usdtEquity","equity","available"]:
                try:
                    v=float(info.get(name) or 0)
                    if v>0:return v
                except:pass
        return None
    def get_equity(self):
        try:
            b=self._balance();v=self._value(b,["USDT","usdt"])
            if v is None: raise RuntimeError("USDT equity not present in exchange response")
            return v
        except Exception:
            log.exception("Exchange equity fetch failed")
            return None
    def get_available(self):
        try:
            b=self._balance()
            return self._value(b,["USDT","usdt"])
        except Exception:
            log.exception("Exchange available balance fetch failed")
            return None
