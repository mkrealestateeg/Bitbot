# Changelog

## V3-UNIFIED-FINAL
- Unified V3 reference build based on the saved Khtb Lab V3 project.
- Fixed Telegram `allowed_updates` serialization: it is now one JSON parameter, avoiding the HTTP 400 caused by repeated query parameters.
- Added Telegram startup authentication, webhook cleanup, startup notification, and inline control UI.
- Added PAPER / DEMO / LIVE environment separation.
- Added separate DEMO and LIVE credentials.
- Added configurable CCXT exchange id and display name.
- Added explicit exchange-order gate plus independent DEMO/LIVE arming gates.
- Added AUTO/MANUAL and SAFE/ADVENTURER controls.
- Added exchange execution adapter with client order id and protective stop/target attempt.
- Kept Gemini commentary-only.
- Kept MANUAL startup default.
- Added tests for Telegram request serialization and execution fail-closed behavior.

### Important validation note
The LIVE path is deliberately gated and should not be considered production-ready merely because the code compiles or unit tests pass. Exchange-specific trigger-order semantics, fills, stop/target verification, restart reconciliation, and timeout/duplicate-order behavior must be validated on the exchange DEMO environment before enabling LIVE.
