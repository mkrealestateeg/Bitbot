from __future__ import annotations
import os
from dataclasses import dataclass
from dotenv import load_dotenv
load_dotenv()

def _bool(name, default=False):
    v=os.getenv(name)
    return default if v is None else v.strip().lower() in {"1","true","yes","on"}

def _float(name, default):
    try: return float(os.getenv(name,str(default)))
    except: return default

def _int(name, default):
    try: return int(os.getenv(name,str(default)))
    except: return default

@dataclass(frozen=True)
class Settings:
    telegram_token: str=os.getenv("TELEGRAM_BOT_TOKEN","").strip()
    allowed_users: frozenset[int]=frozenset(int(x.strip()) for x in os.getenv("TELEGRAM_ALLOWED_USER_IDS","").split(",") if x.strip().isdigit())
    exchange_id: str=os.getenv("EXCHANGE_ID","bitget").strip().lower()
    exchange_name: str=os.getenv("EXCHANGE_NAME",os.getenv("EXCHANGE_ID","bitget")).strip()
    api_key: str=os.getenv("EXCHANGE_API_KEY","").strip()
    api_secret: str=os.getenv("EXCHANGE_API_SECRET","").strip()
    api_password: str=os.getenv("EXCHANGE_API_PASSWORD","").strip()
    demo_api_key: str=os.getenv("DEMO_API_KEY","").strip()
    demo_api_secret: str=os.getenv("DEMO_API_SECRET","").strip()
    demo_api_password: str=os.getenv("DEMO_API_PASSWORD","").strip()
    live_api_key: str=os.getenv("LIVE_API_KEY","").strip()
    live_api_secret: str=os.getenv("LIVE_API_SECRET","").strip()
    live_api_password: str=os.getenv("LIVE_API_PASSWORD","").strip()
    trading_env: str=os.getenv("TRADING_ENV","DEMO").strip().upper()
    trading_mode: str=os.getenv("TRADING_MODE","MANUAL").strip().upper()
    enable_exchange_orders: bool=_bool("ENABLE_EXCHANGE_ORDERS",False)
    demo_armed: bool=_bool("DEMO_TRADING_ARMED",False)
    live_armed: bool=_bool("LIVE_TRADING_ARMED",False)
    starting_capital: float=_float("STARTING_CAPITAL",1000.0)
    risk_per_trade: float=_float("RISK_PER_TRADE",0.005)
    max_daily_loss: float=_float("MAX_DAILY_LOSS",0.02)
    max_open_positions: int=_int("MAX_OPEN_POSITIONS",3)
    leverage: int=_int("LEVERAGE",1)
    max_margin_per_trade: float=_float("MAX_MARGIN_PER_TRADE",5.0)
    scan_seconds: int=_int("SCAN_SECONDS",60)
    timeframe_main: str=os.getenv("TIMEFRAME_MAIN","15m")
    timeframe_trend: str=os.getenv("TIMEFRAME_TREND","1h")
    timeframe_context: str=os.getenv("TIMEFRAME_CONTEXT","4h")
    timeframe_entry: str=os.getenv("TIMEFRAME_ENTRY","5m")
    symbols: tuple[str,...]=tuple(x.strip().upper() for x in os.getenv("SYMBOLS","FET/USDT:USDT,NEAR/USDT:USDT,RENDER/USDT:USDT").split(",") if x.strip())
    db_path: str=os.getenv("DB_PATH","data/khtb_lab.db")
    log_level: str=os.getenv("LOG_LEVEL","INFO").upper()
    enable_ai_review: bool=_bool("ENABLE_AI_REVIEW",False)
    gemini_api_pool: tuple[str,...]=tuple(x.strip() for x in os.getenv("GEMINI_API_POOL",os.getenv("GEMINI_API_KEY","")).split(",") if x.strip())
    gemini_model: str=os.getenv("GEMINI_MODEL","gemini-2.5-flash").strip()
    ai_min_tech_score: float=_float("AI_MIN_TECH_SCORE",70.0)
    ai_review_cooldown: int=_int("AI_REVIEW_COOLDOWN",900)
    notify_technical_opportunity: bool=_bool("NOTIFY_TECHNICAL_OPPORTUNITY",True)
    notify_auto_ready: bool=_bool("NOTIFY_AUTO_READY",True)
    bot_version: str="V3.1-UNIFIED-FINAL"

    def validate(self):
        if self.trading_env not in {"PAPER","DEMO","LIVE"}: raise ValueError("TRADING_ENV must be PAPER, DEMO, or LIVE")
        if self.trading_mode not in {"MANUAL","AUTO"}: raise ValueError("TRADING_MODE must be MANUAL or AUTO")
        if not 0 < self.risk_per_trade <= .02: raise ValueError("RISK_PER_TRADE must be >0 and <=0.02")
        if not 0 < self.max_daily_loss <= .10: raise ValueError("MAX_DAILY_LOSS must be >0 and <=0.10")
        if self.max_open_positions<1: raise ValueError("MAX_OPEN_POSITIONS must be >=1")
        if self.leverage<1: raise ValueError("LEVERAGE must be >=1")
