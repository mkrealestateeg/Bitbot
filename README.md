# Khtb Lab — Independent V3 Lab

Khtb Lab is a **separate repository-ready** Python project for crypto market analysis, Telegram control, local paper trading, trade journaling, and research backtesting. It does not modify or depend on the existing Khtp repository.

> **Safety status:** This release defaults to `TRADING_ENV=paper`, `TRADING_MODE=MANUAL`, and `ENABLE_EXCHANGE_ORDERS=false`. Paper trades are simulated locally. It reads Bitget public market data through CCXT. It does **not** submit exchange orders in this release, even if demo credentials are supplied. Do not interpret passing unit tests as proof of Bitget demo order protection.

## Features

- Bitget USDT perpetual market data via CCXT, with explicit market-symbol validation (no silent symbol substitutions).
- Multi-timeframe technical analysis: EMA 20/50/200, RSI, MACD, ATR, ADX, volume ratio and rolling support/resistance.
- Conservative signal threshold and separate risk checks.
- SQLite symbol watchlist and commands to add/enable/disable/remove symbols from Telegram.
- Telegram allowlist: both messages and callbacks fail closed if the user is not allowed; commands are disabled when the allowlist is empty.
- Paper trading: local entries, simulated stop/target monitoring, PnL, R multiple, daily loss guard, trade history and statistics.
- Manual paper-trade logging through Telegram.
- Research backtest module with win rate, average R, profit factor and max drawdown in R.
- SAFE and ADVENTURER profiles affect paper risk sizing only.
- Docker and systemd examples for a Linux VPS. Hosting provider specifics can be added after the target provider is confirmed.

## Quick start

1. Install Python 3.11+.
2. Clone this repository and enter the directory.
3. Create an environment and install dependencies:

   ```bash
   python -m venv .venv
   source .venv/bin/activate
   pip install -e '.[test]'
   cp .env.example .env
   ```

4. Create a **new** Telegram bot with `@BotFather`. Put its token in `.env`; add your numeric Telegram user ID to `TELEGRAM_ALLOWED_USER_IDS`.
5. Start with `TRADING_ENV=paper`, `TRADING_MODE=MANUAL`, and `ENABLE_EXCHANGE_ORDERS=false`.
6. Run tests: `pytest -q`
7. Run: `python main.py`
8. Telegram commands: `/start`, `/status`, `/coins`, `/scan`, `/mode MANUAL`, `/profile SAFE`, `/trades`, `/stats`.

## Telegram commands

- `/coins`
- `/coin_add FET/USDT:USDT`
- `/coin_enable FET/USDT:USDT`
- `/coin_disable FET/USDT:USDT`
- `/coin_remove FET/USDT:USDT` (refuses removal while a local paper trade is open)
- `/mode MANUAL` or `/mode AUTO` (AUTO is allowed only in PAPER mode)
- `/profile SAFE` or `/profile ADVENTURER`
- `/scan`
- `/open FET/USDT:USDT LONG 1.20 1.17 1.26` (manual **paper** trade; sample prices are illustrative)
- `/closeall`, then `/closeall confirm` (closes all local paper trades at the latest fetched ticker prices)
- `/trades`, `/stats`

## Configuration

See `.env.example`. Never commit `.env` or exchange credentials. Use a **separate** Telegram bot and a new database for this lab. Never reuse the old bot token if it has been exposed.

## Deploy on a Linux VPS

The repository includes a Dockerfile and `deploy/khtb-lab.service.example`. For a free VPS, create the server, create a non-root service user, install Docker or Python, upload/clone the repository, add `.env`, then enable the service. Free compute offers can have region, capacity, verification, and resource limits; no uptime guarantee is implied.

## What is not certified yet

- Bitget demo order placement and exchange-side SL/TP creation/read-back.
- Automatic live trading, live closing, and real account reconciliation. Live order sending is intentionally unsupported.
- AI commentary is not in the critical trading path; the current signal engine is deterministic technical analysis.
- Backtest is research-grade, omits fees/slippage/funding and needs walk-forward validation before strategy conclusions.
- A successful test suite is not an exchange end-to-end test.

Do not enable real trading based on this lab package. The next implementation gate for exchange-demo orders is a tested Bitget-specific order/protection adapter with independent verification of fill, stop-loss and take-profit after every entry.
