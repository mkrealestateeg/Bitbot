# Changelog

## 0.2.0 — Demo execution build
- Added Bitget Demo execution through CCXT sandbox/demo mode.
- Added Demo AUTO and manual execution paths with explicit arming gates.
- Added exchange-side preset SL/TP on entry and protection verification before a local trade is recorded.
- Added Demo reconciliation from exchange positions/history after SL/TP/manual closes.
- Added configurable leverage (default 10x, max 20x) and a hard per-trade margin cap (default 5 USDT).
- Added Telegram startup notification and control buttons for AUTO/MANUAL, status, scan and close-all confirmation.
- Added visible evaluation logs for every symbol.
- Kept LIVE execution intentionally disabled.
- Expanded deterministic strategy with the agreed Momentum + Horizontal Range + BRR setup paths.
- Added migration-safe exchange-order fields to the SQLite trade journal.

## 0.1.0 — Baseline
- Independent Khtb Lab repository package.
- Multi-timeframe technical analysis, local paper trading, Telegram controls, SQLite journal and research backtest.
