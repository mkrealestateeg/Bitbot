
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

## Deployment
Railway should run `python main.py`. Keep `TRADING_ENV=DEMO`, `DEMO_TRADING_ARMED=false`, and `LIVE_TRADING_ARMED=false` until a controlled DEMO test is explicitly started.
