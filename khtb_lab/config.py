from __future__ import annotations
import os
from dataclasses import dataclass
from dotenv import load_dotenv

load_dotenv()


def _bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    return default if value is None else value.strip().lower() in {"1", "true", "yes", "on"}


def _float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class Settings:
    telegram_token: str = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    telegram_chat_id: str = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    allowed_users: frozenset[int] = frozenset(
        int(x.strip()) for x in os.getenv("TELEGRAM_ALLOWED_USER_IDS", "").split(",") if x.strip().isdigit()
    )

    bitget_key: str = os.getenv("BITGET_API_KEY", "").strip()
    bitget_secret: str = os.getenv("BITGET_API_SECRET", "").strip()
    bitget_passphrase: str = os.getenv("BITGET_API_PASSPHRASE", "").strip()
    bitget_demo: bool = _bool("BITGET_DEMO", True)

    # paper = local simulation; bitget_demo = real Bitget Demo account; live intentionally unsupported.
    trading_env: str = os.getenv("TRADING_ENV", "paper").strip().lower()
    trading_mode: str = os.getenv("TRADING_MODE", "MANUAL").strip().upper()
    enable_exchange_orders: bool = _bool("ENABLE_EXCHANGE_ORDERS", False)
    demo_armed: bool = _bool("DEMO_TRADING_ARMED", False)
    live_armed: bool = _bool("LIVE_TRADING_ARMED", False)

    starting_capital: float = _float("STARTING_CAPITAL", 1000.0)
    risk_per_trade: float = _float("RISK_PER_TRADE", 0.01)
    max_daily_loss: float = _float("MAX_DAILY_LOSS", 0.03)
    max_open_positions: int = _int("MAX_OPEN_POSITIONS", 3)
    leverage: int = _int("LEVERAGE", 10)
    max_leverage: int = _int("MAX_LEVERAGE", 20)
    max_margin_per_trade: float = _float("MAX_MARGIN_PER_TRADE", 5.0)
    margin_mode: str = os.getenv("MARGIN_MODE", "isolated").strip().lower()
    scan_seconds: int = _int("SCAN_SECONDS", 60)
    min_technical_score: float = _float("MIN_TECH_SCORE", 65.0)

    timeframe_main: str = os.getenv("TIMEFRAME_MAIN", "15m")
    timeframe_trend: str = os.getenv("TIMEFRAME_TREND", "1h")
    timeframe_context: str = os.getenv("TIMEFRAME_CONTEXT", "4h")
    symbols: tuple[str, ...] = tuple(
        x.strip().upper()
        for x in os.getenv("SYMBOLS", "FET/USDT:USDT,NEAR/USDT:USDT,RENDER/USDT:USDT").split(",")
        if x.strip()
    )

    db_path: str = os.getenv("DB_PATH", "data/khtb_lab.db")
    log_level: str = os.getenv("LOG_LEVEL", "INFO").upper()

    enable_ai_review: bool = _bool("ENABLE_AI_REVIEW", False)
    gemini_api_key: str = os.getenv("GEMINI_API_KEY", "").strip()
    gemini_model: str = os.getenv("GEMINI_MODEL", "gemini-2.5-flash").strip()

    def validate(self) -> None:
        if self.trading_mode not in {"MANUAL", "AUTO"}:
            raise ValueError("TRADING_MODE must be MANUAL or AUTO")
        if not 0 < self.risk_per_trade <= 0.02:
            raise ValueError("RISK_PER_TRADE must be >0 and <=0.02")
        if not 0 < self.max_daily_loss <= 0.10:
            raise ValueError("MAX_DAILY_LOSS must be >0 and <=0.10")
        if self.max_open_positions < 1:
            raise ValueError("MAX_OPEN_POSITIONS must be at least 1")
        if not 1 <= self.leverage <= self.max_leverage <= 100:
            raise ValueError("LEVERAGE/MAX_LEVERAGE must satisfy 1 <= LEVERAGE <= MAX_LEVERAGE <= 100")
        if self.max_margin_per_trade <= 0:
            raise ValueError("MAX_MARGIN_PER_TRADE must be >0")
        if self.margin_mode not in {"isolated", "cross"}:
            raise ValueError("MARGIN_MODE must be isolated or cross")
        if self.trading_env not in {"paper", "bitget_demo", "live"}:
            raise ValueError("TRADING_ENV must be paper, bitget_demo, or live")
        if self.trading_env == "live":
            raise ValueError("LIVE trading is intentionally not supported in this release")
        if self.trading_env == "bitget_demo":
            if not self.bitget_demo:
                raise ValueError("bitget_demo requires BITGET_DEMO=true")
            if not self.bitget_key or not self.bitget_secret or not self.bitget_passphrase:
                raise ValueError("bitget_demo execution requires BITGET_API_KEY, BITGET_API_SECRET and BITGET_API_PASSPHRASE")
            if self.enable_exchange_orders and not self.demo_armed:
                raise ValueError("DEMO_TRADING_ARMED=true is required when ENABLE_EXCHANGE_ORDERS=true")
            if self.trading_mode == "AUTO" and not (self.enable_exchange_orders and self.demo_armed):
                raise ValueError("AUTO on bitget_demo requires ENABLE_EXCHANGE_ORDERS=true and DEMO_TRADING_ARMED=true")
        if self.trading_env == "paper" and self.enable_exchange_orders:
            raise ValueError("ENABLE_EXCHANGE_ORDERS is only valid with TRADING_ENV=bitget_demo")
