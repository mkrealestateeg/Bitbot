
from __future__ import annotations
import logging,time
from .config import Settings
from .database import Database
from .market import Market
from .strategy import analyze
from .risk import risk_check,position_size
from .paper import PaperBroker
from .execution import ExecutionEngine
from .ai_review import review_signal
from .backtest import run_backtest
log=logging.getLogger(__name__)
class KhtbLab:
    def __init__(self,settings):
        settings.validate(); self.settings=settings; self.db=Database(settings.db_path); self.db.seed_symbols(settings.symbols)
        self.market=Market(settings); self.mode=settings.trading_mode; self.profile='SAFE'; self.paper=PaperBroker(settings,self.db); self.execution=ExecutionEngine(settings); self.last_scan='Never'; self.last_result='Not scanned'; self.market.load()
    def paper_qty(self,entry,stop): return position_size(self.paper.capital,entry,stop,self.settings.risk_per_trade*(.5 if self.profile=='SAFE' else 1.0))
    def get_prices(self,symbols):
        out={}
        for s in symbols:
            try: out[s]=float(self.market.ticker(s).get('last') or 0)
            except Exception: log.exception('Ticker failed %s',s)
        return out
    def scan_once(self,allow_auto=True):
        messages=[]; prices={}
        for symbol in self.db.enabled_symbols():
            try:
                sig=analyze(self.market.candles(symbol,self.settings.timeframe_main,250),self.market.candles(symbol,self.settings.timeframe_trend,250),self.market.candles(symbol,self.settings.timeframe_context,250)); prices[symbol]=sig.get('price',0)
                self.db.save_signal(symbol,sig['direction'],sig['price'],sig['score'],sig.get('setup',''),sig.get('reason',''),sig.get('snapshot',{}))
                messages.append(f"{symbol}: {sig['direction']} score={sig['score']:.0f} price={sig['price']:.6g}")
                note=review_signal(self.settings,symbol,sig)
                if note: messages.append('  Gemini: '+note)
                if allow_auto and self.mode=='AUTO' and self.settings.trading_env=='PAPER':
                    ok,why=risk_check(self.settings,self.db,sig,self.paper.capital)
                    if ok and not any(t['symbol']==symbol for t in self.db.open_trades()):
                        tid=self.paper.open(symbol,sig,'AUTO'); messages.append(f"  PAPER OPEN #{tid}") if tid else None
                    elif not ok: messages.append('  Skip: '+why)
                elif allow_auto and self.mode=='AUTO' and self.settings.trading_env in {'DEMO','LIVE'}:
                    capital=self.execution.equity() or self.settings.starting_capital
                    ok,why=risk_check(self.settings,self.db,sig,capital)
                    if ok and not any(t['symbol']==symbol for t in self.db.open_trades()):
                        qty=min(self.paper_qty(sig['price'],sig['stop']), self.settings.max_margin_per_trade*self.settings.leverage/max(sig['price'],1e-12))
                        plan=self.execution.prepare(symbol,sig['direction'],qty,sig['price'],sig['stop'],sig['target'])
                        result=self.execution.execute(plan) if plan['ready'] else {"executed":False,"success":False,"error":plan['error']}
                        messages.append(f"  {self.settings.trading_env} EXEC={result.get('executed')} {result.get('error','')}")
                    elif not ok: messages.append('  Skip: '+why)
            except Exception as e: log.exception('Scan failed %s',symbol); messages.append(f"{symbol}: ERROR {type(e).__name__}: {e}")
        if self.settings.trading_env=='PAPER':
            for tid,reason,pnl in self.paper.mark(prices): messages.append(f"PAPER CLOSE #{tid}: {reason} PnL={pnl:.4f}")
        self.last_scan=time.strftime('%Y-%m-%d %H:%M:%S'); self.last_result=' | '.join(messages)[-1000:]
        return '\\n'.join(messages) or 'لا توجد عملات مفعلة.'
    def status_text(self):
        equity = self.execution.equity() if self.settings.trading_env in {"DEMO", "LIVE"} else self.paper.capital
        armed = "YES" if ((self.settings.trading_env == "DEMO" and self.settings.demo_armed) or (self.settings.trading_env == "LIVE" and self.settings.live_armed)) else "NO"
        return (
            f"🤖 Khtb Lab {self.settings.bot_version}\n\n"
            f"Exchange: {self.settings.exchange_name}\n"
            f"Environment: {self.settings.trading_env}\n"
            f"Mode: {self.mode}\n"
            f"Profile: {self.profile}\n"
            f"Execution Armed: {armed}\n"
            f"Order Sending: {'ON' if self.settings.enable_exchange_orders else 'OFF'}\n"
            f"Symbols: {len(self.db.enabled_symbols())}\n"
            f"Open Trades: {len(self.db.open_trades())}\n"
            f"Equity: {equity if equity is not None else 0:.2f}\n"
            f"Daily PnL: {self.db.daily_pnl():.4f}\n"
            f"Last Scan: {self.last_scan}\n\n"
            "Use the buttons below to control the bot."
        )
    def backtest_symbol(self,s):
        s=self.market.validate_symbol(s); return run_backtest(self.market.candles(s,self.settings.timeframe_main,1000),self.market.candles(s,self.settings.timeframe_trend,1000),self.market.candles(s,self.settings.timeframe_context,1000))
    def run(self,telegram=None):
        log.info('Khtb Lab started | env=%s mode=%s exchange=%s',self.settings.trading_env,self.mode,self.settings.exchange_name)
        while True:
            if telegram:
                telegram.poll_once()
            result = self.scan_once(allow_auto=True)
            log.info("SCAN CYCLE COMPLETE | %s", result.replace("\n", " | ")[:1200])
            time.sleep(max(5, self.settings.scan_seconds))
