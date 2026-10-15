from __future__ import annotations
import numpy as np
import pandas as pd
def enrich(df):
    x=df.copy(); c=x["close"]; h=x["high"]; l=x["low"]; v=x["volume"]
    x["ema20"]=c.ewm(span=20,adjust=False).mean(); x["ema50"]=c.ewm(span=50,adjust=False).mean(); x["ema200"]=c.ewm(span=200,adjust=False).mean()
    delta=c.diff(); gain=delta.clip(lower=0).ewm(alpha=1/14,adjust=False).mean(); loss=(-delta.clip(upper=0)).ewm(alpha=1/14,adjust=False).mean()
    x["rsi"]=100-100/(1+gain/loss.replace(0,np.nan))
    fast=c.ewm(span=12,adjust=False).mean(); slow=c.ewm(span=26,adjust=False).mean(); x["macd"]=fast-slow; x["macd_signal"]=x["macd"].ewm(span=9,adjust=False).mean()
    tr=pd.concat([h-l,(h-c.shift()).abs(),(l-c.shift()).abs()],axis=1).max(axis=1); x["atr"]=tr.ewm(alpha=1/14,adjust=False).mean()
    up=h.diff(); down=-l.diff(); plus=up.where((up>down)&(up>0),0.0); minus=down.where((down>up)&(down>0),0.0)
    plus_di=100*plus.ewm(alpha=1/14,adjust=False).mean()/x["atr"].replace(0,np.nan); minus_di=100*minus.ewm(alpha=1/14,adjust=False).mean()/x["atr"].replace(0,np.nan)
    dx=100*(plus_di-minus_di).abs()/(plus_di+minus_di).replace(0,np.nan); x["adx"]=dx.ewm(alpha=1/14,adjust=False).mean()
    x["volume_ma"]=v.rolling(20).mean(); x["volume_ratio"]=v/x["volume_ma"].replace(0,np.nan)
    x["swing_low"]=l.rolling(20).min(); x["swing_high"]=h.rolling(20).max()
    return x.replace([np.inf,-np.inf],np.nan)
