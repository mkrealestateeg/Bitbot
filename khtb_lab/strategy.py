from __future__ import annotations
from .indicators import enrich
def analyze(main_df,trend_df,context_df):
    m=enrich(main_df); t=enrich(trend_df); c=enrich(context_df)
    a=m.iloc[-2] if len(m)>2 else m.iloc[-1]; trend=t.iloc[-2] if len(t)>2 else t.iloc[-1]; ctx=c.iloc[-2] if len(c)>2 else c.iloc[-1]
    price=float(a.close); atr=float(a.atr) if a.atr==a.atr else price*0.01
    if not price or atr<=0:return {"direction":"NEUTRAL","score":0,"reason":"Insufficient indicator data","price":price}
    score_long=score_short=0; reasons=[]
    for row,weight,label in [(a,25,"15m"),(trend,25,"1h"),(ctx,15,"4h")]:
        if row.close>row.ema20>row.ema50:score_long+=weight
        if row.close<row.ema20<row.ema50:score_short+=weight
        reasons.append(f"{label}: EMA trend {'up' if row.close>row.ema50 else 'down'}")
    if a.rsi==a.rsi:
        if 50<=a.rsi<=70:score_long+=10
        if 30<=a.rsi<=50:score_short+=10
    if a.macd>a.macd_signal:score_long+=10
    if a.macd<a.macd_signal:score_short+=10
    if a.adx==a.adx and a.adx>=18:score_long+=5;score_short+=5
    if a.volume_ratio==a.volume_ratio and a.volume_ratio>=1.2:
        score_long+=5 if a.close>a.open else 0;score_short+=5 if a.close<a.open else 0
    direction="LONG" if score_long>=65 and score_long>=score_short+10 else ("SHORT" if score_short>=65 and score_short>=score_long+10 else "NEUTRAL")
    score=max(score_long,score_short); stop=price-1.5*atr if direction=="LONG" else price+1.5*atr; target=price+3*atr if direction=="LONG" else price-3*atr
    return {"direction":direction,"score":float(score),"price":price,"atr":atr,"stop":stop,"target":target,"setup":"MOMENTUM" if score>=65 else "NO_SETUP","reason":"; ".join(reasons),"snapshot":{"rsi":float(a.rsi) if a.rsi==a.rsi else None,"macd":float(a.macd) if a.macd==a.macd else None,"macd_signal":float(a.macd_signal) if a.macd_signal==a.macd_signal else None,"adx":float(a.adx) if a.adx==a.adx else None,"atr":atr,"volume_ratio":float(a.volume_ratio) if a.volume_ratio==a.volume_ratio else None,"ema20":float(a.ema20),"ema50":float(a.ema50),"trend_1h":"UP" if trend.close>trend.ema50 else "DOWN","trend_4h":"UP" if ctx.close>ctx.ema50 else "DOWN"}}
