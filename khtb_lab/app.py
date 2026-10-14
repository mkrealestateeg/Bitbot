from __future__ import annotations
import logging, time
from .config import Settings
from .database import Database
from .market import Market
from .strategy import analyze
from .risk import risk_check, position_size
from .paper import PaperBroker
from .ai_review import review_signal
from .backtest import run_backtest

log=logging.getLogger(__name__)

class KhtbLab:
    def __init__(self, settings: Settings):
        settings.validate(); self.settings=settings; self.db=Database(settings.db_path); self.db.seed_symbols(settings.symbols)
        self.market=Market(settings); self.mode=settings.trading_mode if settings.trading_mode in {'MANUAL','AUTO'} else 'MANUAL'
        if settings.trading_env!='paper': self.mode='MANUAL'
        self.profile='SAFE'; self.paper=PaperBroker(settings,self.db); self.paper.risk_fraction=settings.risk_per_trade*0.5; self.market.load()
    def paper_qty(self, entry, stop):
        fraction=self.settings.risk_per_trade*(0.5 if self.profile=='SAFE' else 1.0)
        return position_size(self.paper.capital,entry,stop,fraction)
    def get_prices(self,symbols):
        prices={}
        for s in symbols:
            try: prices[s]=float(self.market.ticker(s).get('last') or 0)
            except Exception: log.exception('Ticker fetch failed for %s',s)
        return prices
    def scan_once(self,allow_auto=True):
        messages=[]; prices={}
        for symbol in self.db.enabled_symbols():
            try:
                main=self.market.candles(symbol,self.settings.timeframe_main,250)
                trend=self.market.candles(symbol,self.settings.timeframe_trend,250)
                context=self.market.candles(symbol,self.settings.timeframe_context,250)
                signal=analyze(main,trend,context); ai_note=review_signal(self.settings,symbol,signal); prices[symbol]=signal['price']
                self.db.save_signal(symbol,signal['direction'],signal['price'],signal['score'],signal.get('setup',''),signal.get('reason',''),signal.get('snapshot',{}))
                messages.append(f"{symbol}: {signal['direction']} score={signal['score']:.0f} price={signal['price']:.6g}")
                if ai_note: messages.append(f"  Gemini review (commentary only): {ai_note}")
                if allow_auto and self.mode=='AUTO' and self.settings.trading_env=='paper':
                    ok,why=risk_check(self.settings,self.db,signal,self.paper.capital)
                    if ok and not any(t['symbol']==symbol for t in self.db.open_trades()):
                        tid=self.paper.open(symbol,signal,'AUTO')
                        if tid: messages.append(f"  PAPER OPEN #{tid}; SL={signal['stop']:.6g}; TP={signal['target']:.6g}")
                    elif not ok: messages.append(f"  Skip: {why}")
            except Exception as e:
                log.exception('Scan failed for %s',symbol); messages.append(f"{symbol}: ERROR {type(e).__name__}: {e}")
        closed=self.paper.mark(prices)
        for trade_id,reason,pnl in closed: messages.append(f"PAPER CLOSE #{trade_id}: {reason} PnL={pnl:.4f}")
        return '\n'.join(messages) or 'لا توجد عملات مفعلة.'
    def backtest_symbol(self, symbol):
        symbol=self.market.validate_symbol(symbol)
        main=self.market.candles(symbol,self.settings.timeframe_main,1000)
        trend=self.market.candles(symbol,self.settings.timeframe_trend,1000)
        context=self.market.candles(symbol,self.settings.timeframe_context,1000)
        result=run_backtest(main,trend,context)
        return result

    def status_text(self):
        return (f"Khtb Lab v{__import__('khtb_lab').__version__}\nEnvironment: {self.settings.trading_env}\nMode: {self.mode}\nProfile: {self.profile}\nSymbols: {len(self.db.enabled_symbols())}\nOpen paper trades: {len(self.db.open_trades())}\nPaper equity: {self.paper.capital:.2f}\nToday's realized PnL: {self.db.daily_pnl():.4f}\nExchange order sending: DISABLED in this release")
    def run(self, telegram=None):
        log.info('Khtb Lab started; env=%s mode=%s',self.settings.trading_env,self.mode)
        while True:
            if telegram:
                telegram.poll_once()
            else:
                log.info('\n%s',self.scan_once(allow_auto=True)); time.sleep(max(15,self.settings.scan_seconds))
