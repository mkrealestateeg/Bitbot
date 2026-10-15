from __future__ import annotations
import logging,time
from .config import Settings
from .database import Database
from .market import Market
from .strategy import analyze
from .risk import risk_check,position_size
from .paper import PaperBroker
from .execution import ExecutionEngine
from .ai_review import AIReviewer
from .backtest import run_backtest
log=logging.getLogger(__name__)
class KhtbLab:
    def __init__(self,settings):
        settings.validate();self.settings=settings;self.db=Database(settings.db_path);self.db.seed_symbols(settings.symbols)
        self.market=Market(settings);self.mode=settings.trading_mode;self.profile="SAFE";self.paper=PaperBroker(settings,self.db);self.execution=ExecutionEngine(settings)
        self.ai=AIReviewer(settings);self.last_scan="Never";self.last_result="Not scanned";self.telegram=None;self.market.load()
    def paper_qty(self,entry,stop):
        return position_size(self.paper.capital,entry,stop,self.settings.risk_per_trade*(.5 if self.profile=="SAFE" else 1))
    def get_prices(self,symbols):
        out={}
        for s in symbols:
            try:out[s]=float(self.market.ticker(s).get("last") or 0)
            except Exception:log.exception("Ticker failed %s",s)
        return out
    def notify(self,text):
        if self.telegram:self.telegram.broadcast(text)
    def opportunity_text(self,symbol,sig,ai=None):
        decision=(ai or {}).get("decision","NOT_REVIEWED");conf=(ai or {}).get("confidence",0);comment=(ai or {}).get("comment","")
        return (f"🟡 OPPORTUNITY READY\n{symbol}\nDirection: {sig['direction']}\nTechnical: {sig['score']:.0f}\n"
                f"AI: {decision} ({conf:.0f}%)\nEntry: {sig['price']:.8g}\nSL: {sig['stop']:.8g}\nTP: {sig['target']:.8g}\n"
                f"Setup: {sig.get('setup','')}\n{comment}")
    def scan_once(self,allow_auto=True):
        messages=[];prices={}
        for symbol in self.db.enabled_symbols():
            try:
                sig=analyze(self.market.candles(symbol,self.settings.timeframe_main,250),self.market.candles(symbol,self.settings.timeframe_trend,250),self.market.candles(symbol,self.settings.timeframe_context,250));prices[symbol]=sig.get("price",0)
                self.db.save_signal(symbol,sig["direction"],sig["price"],sig["score"],sig.get("setup",""),sig.get("reason",""),sig.get("snapshot",{}))
                if sig.get("direction") not in {"LONG","SHORT"} or sig.get("score",0)<self.settings.ai_min_tech_score:
                    log.info("FILTER %s score=%s direction=%s | Gemini skipped",symbol,sig.get("score"),sig.get("direction"));continue
                log.info("OPPORTUNITY %s score=%s | sending to Gemini",symbol,sig["score"])
                ai=self.ai.review(symbol,sig)
                if ai is None:
                    log.info("AI review skipped/cooldown/disabled for %s",symbol);continue
                self.notify(self.opportunity_text(symbol,sig,ai)) if self.settings.notify_technical_opportunity else None
                if ai.get("decision") not in {"TAKE","CONSIDER"}:
                    continue
                capital=self.paper.capital if self.settings.trading_env=="PAPER" else self.execution.equity()
                if capital is None:
                    self.notify(f"⚠️ {symbol} BLOCKED\nEquity unavailable; no order sent.")
                    continue
                ok,why=risk_check(self.settings,self.db,sig,capital)
                if not ok:
                    log.info("RISK BLOCK %s: %s",symbol,why);continue
                if not allow_auto or self.mode!="AUTO":
                    continue
                if self.settings.trading_env=="PAPER":
                    if not any(t["symbol"]==symbol for t in self.db.open_trades()):
                        tid=self.paper.open(symbol,sig,"AUTO")
                        if tid:self.notify(f"🟢 PAPER TRADE OPENED\n{symbol} {sig['direction']}\nEntry: {sig['price']:.8g}\nSL: {sig['stop']:.8g}\nTP: {sig['target']:.8g}")
                    continue
                if any(t["symbol"]==symbol for t in self.db.open_trades()):continue
                qty=min(position_size(capital,sig["price"],sig["stop"],self.settings.risk_per_trade*(.5 if self.profile=="SAFE" else 1)),self.settings.max_margin_per_trade*self.settings.leverage/max(sig["price"],1e-12))
                plan=self.execution.prepare(symbol,sig["direction"],qty,sig["price"],sig["stop"],sig["target"])
                if not plan["ready"]:
                    log.info("AUTO BLOCK %s: %s",symbol,plan["error"]);continue
                if self.settings.notify_auto_ready:self.notify(f"🟢 AUTO TRADE READY\n{symbol} {sig['direction']}\nEntry: {sig['price']:.8g}\nSL: {sig['stop']:.8g}\nTP: {sig['target']:.8g}\nQty: {qty:.8g}\nSending order...")
                result=self.execution.execute(plan)
                if result.get("success"):
                    tid=self.db.open_trade(symbol,sig["direction"],result["average"],sig["stop"],sig["target"],result["filled_quantity"],mode="AUTO",source=self.settings.trading_env,snapshot=sig.get("snapshot"))
                    self.notify(f"✅ TRADE OPENED\n{symbol} {sig['direction']}\nOrder: {result['order_id']}\nEntry fill: {result['average']:.8g}\nSL: {sig['stop']:.8g}\nTP: {sig['target']:.8g}\nTrade ID: {tid}")
                else:
                    self.notify(f"⚠️ EXECUTION FAILED\n{symbol}\n{result.get('error','unknown')}")
            except Exception as e:
                log.exception("Scan failed %s",symbol)
        if self.settings.trading_env=="PAPER":
            for tid,reason,pnl in self.paper.mark(prices):
                self.notify(f"🔴 PAPER TRADE CLOSED\nTrade #{tid}\n{reason}\nPnL: {pnl:.6f}")
        self.last_scan=time.strftime("%Y-%m-%d %H:%M:%S");self.last_result=f"Completed scan; symbols={len(self.db.enabled_symbols())}"
        return self.last_result
    def status_text(self):
        equity=self.execution.equity() if self.settings.trading_env in {"DEMO","LIVE"} else self.paper.capital
        armed="YES" if ((self.settings.trading_env=="DEMO" and self.settings.demo_armed) or(self.settings.trading_env=="LIVE" and self.settings.live_armed)) else "NO"
        return (f"🤖 Khtb Lab {self.settings.bot_version}\n\nExchange: {self.settings.exchange_name}\nEnvironment: {self.settings.trading_env}\n"
                f"Mode: {self.mode}\nProfile: {self.profile}\nExecution Armed: {armed}\nOrder Sending: {'ON' if self.settings.enable_exchange_orders else 'OFF'}\n"
                f"Symbols: {len(self.db.enabled_symbols())}\nOpen Trades: {len(self.db.open_trades())}\nEquity: {equity if equity is not None else 0:.2f}\n"
                f"Daily PnL: {self.db.daily_pnl():.4f}\nLast Scan: {self.last_scan}")
    def backtest_symbol(self,s):
        s=self.market.validate_symbol(s);return run_backtest(self.market.candles(s,self.settings.timeframe_main,1000),self.market.candles(s,self.settings.timeframe_trend,1000),self.market.candles(s,self.settings.timeframe_context,1000))
    def run(self,telegram=None):
        self.telegram=telegram
        log.info("Khtb Lab started | env=%s mode=%s exchange=%s",self.settings.trading_env,self.mode,self.settings.exchange_name)
        while True:
            if telegram:telegram.poll_once()
            self.scan_once(True)
            time.sleep(max(5,self.settings.scan_seconds))
