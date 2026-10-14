import pandas as pd
import numpy as np
from khtb_lab.indicators import enrich

def test_indicators_add_expected_columns():
    n=260; close=np.linspace(1,3,n)
    df=pd.DataFrame({'open':close,'high':close*1.01,'low':close*.99,'close':close,'volume':np.ones(n)*100})
    result=enrich(df)
    for col in ['ema20','ema50','ema200','rsi','macd','atr','adx','volume_ratio','swing_low','swing_high']:
        assert col in result.columns
    assert result['ema20'].iloc[-1] > result['ema50'].iloc[-1]
