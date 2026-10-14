from __future__ import annotations
import os
from dataclasses import dataclass
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

def _bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    return default if value is None else value.strip().lower() in {"1", "true", "yes", "on"}

def _float(name: str, default: float) -> float:
    try: return float(os.getenv(name, str(default)))
    except (TypeError, ValueError): return default

def _int(name: str, default: int) -> int:
    try: return int(os.getenv(name, str(default)))
    except (TypeError, ValueError): return default

@dataclass(frozen=True)
class Settings:
    telegram_token: str = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    allowed_users: frozenset[int] = frozenset(
        int(x.strip()) for x in os.getenv("TELEGRAM_ALLOWED_USER_IDS", "").split(",")
        if x.strip().isdigit()
    )
    bitget_key: str = os.getenv("BITGET_API_KEY", "").strip()
    bitget_secret: str = os.getenv("BITGET_API_SECRET", "").strip()
    bitget_passphrase: str = os.getenv("BITGET_API_PASSPHRASE", "").strip()
    bitget_demo: bool = _bool("BITGET_DEMO", True)
    trading_env: str = os.getenv("TRADING_ENV", "paper").strip().lower()
    trading_mode: str = os.getenv("TRADING_MODE", "MANUAL").strip().upper()
    enable_exchange_orders: bool = _bool("ENABLE_EXCHANGE_ORDERS", False)
    demo_armed: bool = _bool("DEMO_TRADING_ARMED", False)
    live_armed: bool = _bool("LIVE_TRADING_ARMED", False)
    starting_capital: float = _float("STARTING_CAPITAL", 1000.0)
    risk_per_trade: float = _float("RISK_PER_TRADE", 0.005)
    max_daily_loss: float = _float("MAX_DAILY_LOSS", 0.02)
    max_open_positions: int = _int("MAX_OPEN_POSITIONS", 3)
    leverage: int = _int("LEVERAGE", 1)
    scan_seconds: int = _int("SCAN_SECONDS", 60)
    timeframe_main: str = os.getenv("TIMEFRAME_MAIN", "15m")
    timeframe_trend: str = os.getenv("TIMEFRAME_TREND", "1h")
    timeframe_context: str = os.getenv("TIMEFRAME_CONTEXT", "4h")
    symbols: tuple[str, ...] = tuple(x.strip().upper() for x in os.getenv(
        "SYMBOLS", "FET/USDT:USDT,NEAR/USDT:USDT,RENDER/USDT:USDT"
    ).split(",") if x.strip())
    db_path: str = os.getenv("DB_PATH", "data/khtb_lab.db")
    log_level: str = os.getenv("LOG_LEVEL", "INFO").upper()
    enable_ai_review: bool = _bool("ENABLE_AI_REVIEW", False)
    gemini_api_key: str = os.getenv("GEMINI_API_KEY", "").strip()
    gemini_model: str = os.getenv("GEMINI_MODEL", "gemini-2.5-flash").strip()

    def validate(self) -> None:
        if not 0 < self.risk_per_trade <= 0.02:
            raise ValueError("RISK_PER_TRADE must be >0 and <=0.02")
        if not 0 < self.max_daily_loss <= 0.10:
            raise ValueError("MAX_DAILY_LOSS must be >0 and <=0.10")
        if self.max_open_positions < 1:
            raise ValueError("MAX_OPEN_POSITIONS must be at least 1")
        if self.trading_env not in {"paper", "bitget_demo", "live"}:
            raise ValueError("TRADING_ENV must be paper, bitget_demo, or live")
        if self.trading_env == "live":
            raise ValueError("LIVE trading is not supported by Khtb Lab 0.1; use paper or read-only bitget_demo")
        if self.trading_env == "bitget_demo" and not self.bitget_demo:
            raise ValueError("bitget_demo requires BITGET_DEMO=true")
        if self.enable_exchange_orders:
            raise ValueError("Exchange order submission is not implemented in this release; keep ENABLE_EXCHANGE_ORDERS=false")
        if not self.allowed_users:
            # Deliberately allow process boot for local tests, but Telegram commands fail closed.
            pass
