from __future__ import annotations
import logging,uuid
from .account_adapter import CCXTAccountAdapter
log=logging.getLogger(__name__)
class ExecutionEngine:
    def __init__(self,settings):
        self.settings=settings;self.environment=settings.trading_env;self.exchange=None;self.account=None;self.ready=False
        if self.environment=="PAPER":return
        key=secret=password=""
        if self.environment=="DEMO":key,secret,password=settings.demo_api_key,settings.demo_api_secret,settings.demo_api_password
        elif self.environment=="LIVE":key,secret,password=settings.live_api_key,settings.live_api_secret,settings.live_api_password
        if not key or not secret:log.warning("Exchange credentials missing for %s; execution disabled",self.environment);return
        try:
            import ccxt
            klass=getattr(ccxt,settings.exchange_id)
            params={"apiKey":key,"secret":secret,"enableRateLimit":True,"options":{"defaultType":"swap"}}
            if password:params["password"]=password
            self.exchange=klass(params)
            if self.environment=="DEMO":
                self.exchange.set_sandbox_mode(True)
            self.exchange.load_markets()
            self.account=CCXTAccountAdapter(self.exchange,settings)
            self.ready=True
            log.info("Execution ready: exchange=%s env=%s",settings.exchange_name,self.environment)
        except Exception:log.exception("Exchange initialization failed; execution remains disabled")
    def can_execute(self):
        s=self.settings
        if not s.enable_exchange_orders:return False,"ENABLE_EXCHANGE_ORDERS=false"
        if self.environment=="DEMO" and not s.demo_armed:return False,"DEMO_TRADING_ARMED=false"
        if self.environment=="LIVE" and not s.live_armed:return False,"LIVE_TRADING_ARMED=false"
        if not self.ready or self.exchange is None:return False,"exchange_not_ready"
        return True,"ready"
    def equity(self):
        return self.account.get_equity() if self.account else None
    def prepare(self,symbol,direction,quantity,entry,stop,target):
        ok,why=self.can_execute()
        return {"ready":ok,"error":None if ok else why,"symbol":symbol,"direction":direction,"quantity":quantity,"entry":entry,"stop":stop,"target":target,"environment":self.environment,"leverage":self.settings.leverage}
    def execute(self,plan):
        ok,why=self.can_execute()
        if not ok:return {"success":False,"executed":False,"error":why}
        s=plan["symbol"];side="buy" if plan["direction"]=="LONG" else "sell";client_id="khtb-"+uuid.uuid4().hex[:20]
        try:
            try:self.exchange.set_leverage(self.settings.leverage,s)
            except Exception:log.warning("set_leverage failed for %s",s,exc_info=True)
            order=self.exchange.create_order(s,"market",side,plan["quantity"],None,{"clientOrderId":client_id})
            oid=order.get("id")
            filled=float(order.get("filled") or plan["quantity"]);avg=float(order.get("average") or order.get("price") or plan["entry"])
            if not oid:return {"success":False,"executed":False,"error":"missing_exchange_order_id","reconcile_required":True}
            protection=self._place_protection(plan,filled,avg)
            if not protection["ok"]:return {"success":False,"executed":True,"order_id":oid,"average":avg,"filled_quantity":filled,"error":protection["error"],"protection_failed":True,"reconcile_required":True}
            return {"success":True,"executed":True,"order_id":oid,"average":avg,"filled_quantity":filled,"environment":self.environment,"protection":protection}
        except Exception as e:log.exception("Exchange order execution failed");return {"success":False,"executed":False,"error":f"{type(e).__name__}:{e}","reconcile_required":True}
    def _place_protection(self,plan,qty,entry):
        s=plan["symbol"];exit_side="sell" if plan["direction"]=="LONG" else "buy"
        try:
            self.exchange.create_order(s,"stop_market",exit_side,qty,None,{"stopPrice":plan["stop"],"reduceOnly":True})
            self.exchange.create_order(s,"take_profit_market",exit_side,qty,None,{"stopPrice":plan["target"],"reduceOnly":True})
            return {"ok":True}
        except Exception as e:log.exception("Protection placement failed");return {"ok":False,"error":f"protection_failed:{e}"}
