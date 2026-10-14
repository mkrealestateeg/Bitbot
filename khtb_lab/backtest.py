from __future__ import annotations
import pandas as pd
from .strategy import analyze

def run_backtest(df: pd.DataFrame, trend_df: pd.DataFrame, context_df: pd.DataFrame, risk_reward=2.0, risk_per_trade=0.005):
    """Simple next-bar OHLC backtest. No fees/slippage; results are research-only, not production evidence."""
    if len(df)<220: raise ValueError('Need at least 220 main timeframe candles')
    trades=[]; enriched_start=200
    for i in range(enriched_start,len(df)-1):
        # Only data available up to i is passed to the strategy to reduce look-ahead leakage.
        s=analyze(df.iloc[:i+1].copy(),trend_df[trend_df['timestamp']<=df.iloc[i]['timestamp']],context_df[context_df['timestamp']<=df.iloc[i]['timestamp']])
        if s['direction']=='NEUTRAL' or s['score']<65: continue
        entry=float(df.iloc[i+1]['open']); stop=float(s['stop']); direction=1 if s['direction']=='LONG' else -1
        risk=abs(entry-stop)
        if risk<=0: continue
        target=entry+direction*risk*risk_reward; exit_price=None; reason='END'
        for j in range(i+1,min(i+51,len(df))):
            row=df.iloc[j]
            if direction==1 and row['low']<=stop: exit_price=stop; reason='SL'; break
            if direction==-1 and row['high']>=stop: exit_price=stop; reason='SL'; break
            if direction==1 and row['high']>=target: exit_price=target; reason='TP'; break
            if direction==-1 and row['low']<=target: exit_price=target; reason='TP'; break
        if exit_price is None: exit_price=float(df.iloc[min(i+50,len(df)-1)]['close'])
        pnl_r=(exit_price-entry)*direction/risk
        trades.append({'index':i,'direction':s['direction'],'entry':entry,'exit':exit_price,'reason':reason,'r_multiple':pnl_r,'return_on_risk':pnl_r*risk_per_trade})
    if not trades: return {'trades':[],'count':0,'win_rate':0.0,'avg_r':0.0,'profit_factor':0.0,'max_drawdown_r':0.0}
    curve=0.0; peak=0.0; max_dd=0.0
    for t in trades: curve+=t['r_multiple']; peak=max(peak,curve); max_dd=max(max_dd,peak-curve)
    wins=sum(t['r_multiple'] for t in trades if t['r_multiple']>0); losses=abs(sum(t['r_multiple'] for t in trades if t['r_multiple']<0))
    return {'trades':trades,'count':len(trades),'win_rate':sum(t['r_multiple']>0 for t in trades)/len(trades)*100,'avg_r':sum(t['r_multiple'] for t in trades)/len(trades),'profit_factor':wins/losses if losses else (999.0 if wins else 0.0),'max_drawdown_r':max_dd}
