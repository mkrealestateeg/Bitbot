# Khtb Lab — Demo Trading Build

Khtb Lab is an independent crypto perpetuals research/trading bot. It is separate from the older Khtp project.

## Current build

- Bitget USDT perpetual market data through CCXT.
- 15m entry + 1h trend/levels + 4h context.
- Deterministic technical engine using EMA20/50/200, RSI, MACD, ATR, ADX, volume ratio and support/resistance.
- Agreed setup paths: Momentum, Horizontal Range rejection, and BRR (1h breakout/retest with 15m confirmation).
- Risk-based sizing with configurable risk, leverage and a hard per-trade margin cap.
- SAFE and ADVENTURER profiles change risk sizing; AI does not choose leverage or order size.
- Telegram AUTO/MANUAL control, status, symbol management, manual order entry, scan, trade history, stats and close-all confirmation.
- Local PAPER execution remains available.
- Bitget DEMO execution is now supported when explicitly armed.
- Entry orders include exchange-side preset stop-loss and take-profit; the bot verifies both protections before recording the trade locally.
- Demo reconciliation reads exchange positions/history so exchange-side SL/TP closes can be reflected in the journal.
- LIVE order execution remains intentionally unsupported in this release.

## Bitget Demo requirements

Bitget states that Demo API trading requires a Demo API Key created while the account is in Demo mode and the demo-trading header (`paptrading: 1`). CCXT exposes this through `set_sandbox_mode(True)`. urlBitget Demo Trading APIhttps://www.bitget.com/docs/classic/demo-trading/rest-api

The app therefore requires all of these for exchange execution:

```text
TRADING_ENV=bitget_demo
TRADING_MODE=AUTO
ENABLE_EXCHANGE_ORDERS=true
DEMO_TRADING_ARMED=true
BITGET_DEMO=true
BITGET_API_KEY=<Demo API key>
BITGET_API_SECRET=<Demo API secret>
BITGET_API_PASSPHRASE=<Demo API passphrase>
```

If any execution gate is missing, the app stays read-only and refuses AUTO Demo execution.

## Risk defaults

```text
STARTING_CAPITAL=1000
RISK_PER_TRADE=0.01
MAX_DAILY_LOSS=0.03
MAX_OPEN_POSITIONS=3
LEVERAGE=10
MAX_LEVERAGE=20
MAX_MARGIN_PER_TRADE=5
MARGIN_MODE=isolated
MIN_TECH_SCORE=65
SCAN_SECONDS=60
```

The leverage value is configuration, not an AI decision. The per-trade margin cap is an additional guard for the small-account/demo test.

## Telegram

- `/start` — help + control panel
- `/status`
- `/coins`
- `/coin_add SYMBOL`
- `/coin_enable SYMBOL`
- `/coin_disable SYMBOL`
- `/coin_remove SYMBOL`
- `/mode MANUAL` or `/mode AUTO`
- `/profile SAFE` or `/profile ADVENTURER`
- `/scan` — analysis only, no order
- `/open SYMBOL LONG|SHORT ENTRY STOP TARGET` — manual order in the current environment
- `/closeall` then `/closeall confirm`
- `/trades`
- `/stats`

## Important execution behavior

AUTO is allowed in PAPER or armed BITGET DEMO. MANUAL is always available. `/scan` never opens a trade. Demo entries are not considered successful until the exchange returns a fill and the bot can verify exchange-side stop-loss and take-profit protection.

## Tests

Run:

```bash
pytest -q
```

The repository test suite covers configuration gates, indicators/strategy, risk, database lifecycle and the Demo execution adapter with mocked exchange responses. Passing tests are not a substitute for the first real Bitget Demo end-to-end test.
