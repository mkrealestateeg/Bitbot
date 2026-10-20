
# Khtb Lab V3 — Unified Final

Single reference build for PAPER, Bitget/CCXT DEMO, and separately gated LIVE AUTO trading.

## Safety
- Starts MANUAL.
- Exchange order submission is OFF unless `ENABLE_EXCHANGE_ORDERS=true`.
- DEMO additionally requires `DEMO_TRADING_ARMED=true`.
- LIVE additionally requires `LIVE_TRADING_ARMED=true` and `TRADING_ENV=LIVE`.
- Demo and Live credentials are separate.
- Gemini is commentary only; it never sends orders or overrides risk.

## Telegram
The polling request serializes `allowed_updates` as JSON. This avoids the HTTP 400 caused by sending the list as repeated query parameters. Startup authenticates with Telegram, removes stale webhook state, and sends a status/UI panel.

## Exchange
Set `EXCHANGE_ID` to a CCXT-supported exchange id and `EXCHANGE_NAME` to the display name. Use separate DEMO/LIVE keys.
For Bitget UTA, market entries are precision-adjusted and confirmed filled before a separate full-position TPSL strategy order is submitted and checked against active strategy orders. Before each DEMO/LIVE scan, tracked positions, quantities, and active protection are reconciled against Bitget; any mismatch or unknown response blocks automatic entries for that scan. If protection is not confirmed, the bot reports the open position as unprotected; verify all behavior in DEMO before enabling LIVE.

## Strategy exits
Directional signals place stops beyond a recent 15m swing level with a 0.25 ATR safety buffer; candidate structure must leave at least 0.5 ATR between entry and the level. Targets use the next swing support/resistance, require at least 1.5R of room, and use a measured-range projection only for a breakout without a usable opposing swing. Signals without a valid structural stop or enough target room are discarded. Range/retest entries recognize deterministic rejection-wick and engulfing candles; no external machine-learning candle library is used. Live entry refreshes keep the chart-derived stop and target fixed and reject prices that have invalidated those levels.

Telegram maintains one editable follow-up message per open trade and authorized chat, with an individual close button. A trade suppresses new opportunity alerts/entries only for its own symbol; other symbols continue to be scanned. Status refreshes follow the scan interval. PnL and exchange-close summaries are estimated gross values and exclude fees/funding. TP1 is 1R (the entry-to-initial-stop distance); no partial exit occurs. For reconciled DEMO/LIVE trades, approximately 10% gross margin ROE first moves the full-position Bitget stop to secure approximately 10% gross ROE. After the price extends another 10% gross margin ROE beyond TP1, the stop is moved to TP1 even if no swing structure has formed. The exchange TP2 is removed near 90% of its path only after an existing profitable stop is confirmed, so the position can extend beyond TP2. Structural trailing starts only after price extends 10% gross margin ROE beyond TP2, and then ratchets behind confirmed closed-candle swing pivots with an ATR safety buffer. If the market has already retraced through a target stop before an exchange update can be made, the bot retains the existing stop rather than submitting an invalid stop; it retries on later scans. Protection updates are persisted across restarts, and ambiguous or unconfirmed exchange state blocks new AUTO entries. Untracked positions are never managed automatically. Individual close buttons are disabled in hedge mode because the current close endpoint is symbol-scoped.

## Local trade learning
The bot stores a versioned feature snapshot for new entries and trains a small logistic-regression model locally from closed, non-break-even trades in SQLite. PAPER, DEMO, and LIVE datasets/models are isolated. At least 30 eligible closed trades, including 8 wins and 8 losses, are required before the model can show an estimate; older trades without the versioned feature snapshot are ignored. The estimate is not calibrated or validated out of sample and must not be treated as a reliable forecast. It is advisory only: it does not approve or block entries, alter sizing, or send trade history to Gemini or another service. Persist `DB_PATH` on a durable Railway volume to retain both trade data and the trained model across restarts.

## Deployment
Railway should run `python main.py`. Keep `TRADING_ENV=DEMO`, `DEMO_TRADING_ARMED=false`, and `LIVE_TRADING_ARMED=false` until a controlled DEMO test is explicitly started.
