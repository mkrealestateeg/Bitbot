
from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


def _bool(name, default=False):
    value = os.getenv(name)

    if value is None:
        return default

    return value.strip().lower() in {"1", "true", "yes", "on"}


def _float(name, default):
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _int(name, default):
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class Settings:
    # Telegram
    telegram_token: str = os.getenv(
        "TELEGRAM_BOT_TOKEN", ""
    ).strip()

    allowed_users: frozenset[int] = frozenset(
        int(value.strip())
        for value in os.getenv(
            "TELEGRAM_ALLOWED_USER_IDS", ""
        ).split(",")
        if value.strip().isdigit()
    )

    # Exchange
    exchange_id: str = os.getenv(
        "EXCHANGE_ID", "bitget"
    ).strip().lower()

    exchange_name: str = os.getenv(
        "EXCHANGE_NAME",
        os.getenv("EXCHANGE_ID", "bitget"),
    ).strip()

    api_key: str = os.getenv(
        "EXCHANGE_API_KEY", ""
    ).strip()

    api_secret: str = os.getenv(
        "EXCHANGE_API_SECRET", ""
    ).strip()

    api_password: str = os.getenv(
        "EXCHANGE_API_PASSWORD", ""
    ).strip()

    # Demo credentials are separate from live credentials.
    demo_api_key: str = os.getenv(
        "DEMO_API_KEY", ""
    ).strip()

    demo_api_secret: str = os.getenv(
        "DEMO_API_SECRET", ""
    ).strip()

    demo_api_password: str = os.getenv(
        "DEMO_API_PASSWORD", ""
    ).strip()

    live_api_key: str = os.getenv(
        "LIVE_API_KEY", ""
    ).strip()

    live_api_secret: str = os.getenv(
        "LIVE_API_SECRET", ""
    ).strip()

    live_api_password: str = os.getenv(
        "LIVE_API_PASSWORD", ""
    ).strip()

    # Environment and account
    trading_env: str = os.getenv(
        "TRADING_ENV", "DEMO"
    ).strip().upper()

    account_type: str = os.getenv(
        "ACCOUNT_TYPE", "UTA"
    ).strip().upper()

    product_category: str = os.getenv(
        "PRODUCT_CATEGORY", "USDT-FUTURES"
    ).strip().upper()

    margin_mode: str = os.getenv(
        "MARGIN_MODE", "crossed"
    ).strip().lower()

    position_mode: str = os.getenv(
        "POSITION_MODE", "one_way"
    ).strip().lower()

    bitget_base_url: str = os.getenv(
        "BITGET_BASE_URL", "https://api.bitget.com"
    ).strip().rstrip("/")

    request_timeout: int = _int(
        "EXCHANGE_REQUEST_TIMEOUT", 15
    )

    # Trading controls
    trading_mode: str = os.getenv(
        "TRADING_MODE", "MANUAL"
    ).strip().upper()

    enable_exchange_orders: bool = _bool(
        "ENABLE_EXCHANGE_ORDERS", False
    )

    demo_armed: bool = _bool(
        "DEMO_TRADING_ARMED", False
    )

    live_armed: bool = _bool(
        "LIVE_TRADING_ARMED", False
    )

    starting_capital: float = _float(
        "STARTING_CAPITAL", 1000.0
    )

    risk_per_trade: float = _float(
        "RISK_PER_TRADE", 0.005
    )

    max_daily_loss: float = _float(
        "MAX_DAILY_LOSS", 0.02
    )

    max_open_positions: int = _int(
        "MAX_OPEN_POSITIONS", 3
    )

    leverage: int = _int(
        "LEVERAGE", 1
    )

    max_margin_per_trade: float = _float(
        "MAX_MARGIN_PER_TRADE", 5.0
    )

    # Scan intervals and timeframes
    scan_seconds: int = _int(
        "SCAN_SECONDS", 60
    )

    timeframe_main: str = os.getenv(
        "TIMEFRAME_MAIN", "15m"
    )

    timeframe_trend: str = os.getenv(
        "TIMEFRAME_TREND", "1h"
    )

    timeframe_context: str = os.getenv(
        "TIMEFRAME_CONTEXT", "4h"
    )

    # Kept for possible future entry-timing improvements.
    # This timeframe is not activated by this setting alone.
    timeframe_entry: str = os.getenv(
        "TIMEFRAME_ENTRY", "5m"
    )

    # Trading symbols
    symbols: tuple[str, ...] = tuple(
        value.strip().upper()
        for value in os.getenv(
            "SYMBOLS",
            "FET/USDT:USDT,NEAR/USDT:USDT,RENDER/USDT:USDT",
        ).split(",")
        if value.strip()
    )

    # Database and logging
    db_path: str = os.getenv(
        "DB_PATH", "data/khtb_lab.db"
    )

    log_level: str = os.getenv(
        "LOG_LEVEL", "INFO"
    ).upper()

    # Gemini AI review
    enable_ai_review: bool = _bool(
        "ENABLE_AI_REVIEW", False
    )

    gemini_api_pool: tuple[str, ...] = tuple(
        value.strip()
        for value in os.getenv(
            "GEMINI_API_POOL",
            os.getenv("GEMINI_API_KEY", ""),
        ).split(",")
        if value.strip()
    )

    gemini_model: str = os.getenv(
        "GEMINI_MODEL", "gemini-3.5-flash-lite"
    ).strip()

    # Minimum technical score before requesting AI review.
    ai_min_tech_score: float = _float(
        "AI_MIN_TECH_SCORE", 65.0
    )

    # Default Gemini cooldown: 5 minutes.
    # An explicitly configured Railway variable overrides this value.
    ai_review_cooldown: int = _int(
        "AI_REVIEW_COOLDOWN", 300
    )

    notify_technical_opportunity: bool = _bool(
        "NOTIFY_TECHNICAL_OPPORTUNITY", True
    )

    notify_auto_ready: bool = _bool(
        "NOTIFY_AUTO_READY", True
    )

    bot_version: str = "V3.2-UTA-FINAL"

    def validate(self):
        if self.trading_env not in {
            "PAPER", "DEMO", "LIVE"
        }:
            raise ValueError(
                "TRADING_ENV must be PAPER, DEMO, or LIVE"
            )

        if self.trading_mode not in {
            "MANUAL", "AUTO"
        }:
            raise ValueError(
                "TRADING_MODE must be MANUAL or AUTO"
            )

        if self.account_type != "UTA":
            raise ValueError(
                "ACCOUNT_TYPE must be UTA for this build"
            )

        if self.product_category != "USDT-FUTURES":
            raise ValueError(
                "PRODUCT_CATEGORY must be USDT-FUTURES"
            )

        if self.margin_mode not in {
            "crossed", "isolated"
        }:
            raise ValueError(
                "MARGIN_MODE must be crossed or isolated"
            )

        if self.position_mode not in {
            "one_way", "hedge"
        }:
            raise ValueError(
                "POSITION_MODE must be one_way or hedge"
            )

        if not 0 < self.risk_per_trade <= 0.02:
            raise ValueError(
                "RISK_PER_TRADE must be >0 and <=0.02"
            )

        if not 0 < self.max_daily_loss <= 0.10:
            raise ValueError(
                "MAX_DAILY_LOSS must be >0 and <=0.10"
            )

        if self.max_open_positions < 1:
            raise ValueError(
                "MAX_OPEN_POSITIONS must be >=1"
            )

        if self.leverage < 1:
            raise ValueError(
                "LEVERAGE must be >=1"
            )

        if self.scan_seconds < 5:
            raise ValueError(
                "SCAN_SECONDS must be >=5"
            )

        if self.request_timeout < 1:
            raise ValueError(
                "EXCHANGE_REQUEST_TIMEOUT must be >=1"
            )

        if not 0 <= self.ai_min_tech_score <= 100:
            raise ValueError(
                "AI_MIN_TECH_SCORE must be between 0 and 100"
            )

        if self.ai_review_cooldown < 0:
            raise ValueError(
                "AI_REVIEW_COOLDOWN must be >=0"
            )
