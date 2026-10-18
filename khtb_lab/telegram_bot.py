from __future__ import annotations

import hashlib
import json
import logging
import time

import requests

log = logging.getLogger(__name__)


class TelegramBot:
    def __init__(self, settings, app):
        self.settings = settings
        self.app = app
        self.offset = 0
        self.base = f"https://api.telegram.org/bot{settings.telegram_token}"
        self.started = False

    def _request(self, method, payload, timeout=15):
        response = requests.post(
            f"{self.base}/{method}",
            json=payload,
            timeout=timeout,
        )
        response.raise_for_status()
        result = response.json()
        if not result.get("ok"):
            raise RuntimeError(
                f"Telegram {method} failed: {result.get('description', 'unknown error')}"
            )
        return result.get("result")

    def send(self, chat_id, text, keyboard=None):
        if not self.settings.telegram_token:
            return False
        payload = {
            "chat_id": chat_id,
            "text": text,
            "disable_web_page_preview": True,
        }
        if keyboard:
            payload["reply_markup"] = {"inline_keyboard": keyboard}
        try:
            self._request("sendMessage", payload)
            return True
        except Exception:
            log.exception("Telegram sendMessage failed | chat_id=%s", chat_id)
            return False

    def broadcast(self, text):
        ok = False
        for user_id in self.settings.allowed_users:
            ok = self.send(user_id, text, self.keyboard()) or ok
        return ok

    def _authorized(self, user_id):
        return (
            bool(self.settings.allowed_users)
            and user_id is not None
            and int(user_id) in self.settings.allowed_users
        )

    def startup(self):
        if not self.settings.telegram_token:
            return False
        try:
            me = requests.get(f"{self.base}/getMe", timeout=10)
            me.raise_for_status()
            webhook = requests.post(
                f"{self.base}/deleteWebhook",
                json={"drop_pending_updates": False},
                timeout=10,
            )
            webhook.raise_for_status()
            log.info(
                "Telegram authenticated as @%s",
                me.json().get("result", {}).get("username", "unknown"),
            )
            self.started = True
            self.broadcast(self.app.status_text())
            self.sync_open_trade_statuses()
            log.info("Telegram startup message sent")
            return True
        except Exception:
            log.exception("Telegram startup failed")
            return False

    def keyboard(self):
        return [
            [
                {"text": "📊 STATUS", "callback_data": "status"},
                {"text": "🔄 SCAN NOW", "callback_data": "scan"},
            ],
            [
                {"text": "🟢 AUTO", "callback_data": "auto"},
                {"text": "🔴 MANUAL", "callback_data": "manual"},
            ],
            [
                {"text": "🛡 SAFE", "callback_data": "safe"},
                {"text": "🚀 ADVENTURER", "callback_data": "adventurer"},
            ],
            [
                {"text": "🪙 COINS", "callback_data": "coins"},
                {"text": "📈 TRADES", "callback_data": "trades"},
            ],
            [{"text": "🛑 CLOSE ALL", "callback_data": "closeall"}],
        ]

    @staticmethod
    def _trade_text(trade, price, pnl, closed=False, reason=None):
        status = "🔴 CLOSED" if closed else "🟢 POSITION MONITORING"
        text = (
            f"{status}\n"
            f"Trade: #{trade['id']} | {trade['symbol']} {trade['direction']}\n"
            f"Entry: {float(trade['entry']):.8g}\n"
            f"Current: {float(price):.8g}\n"
            f"Unrealized PnL: {float(pnl):+.6f} USDT "
            "(estimate, before fees/funding)\n"
            f"SL: {float(trade['stop']):.8g}\n"
            f"TP: {float(trade['target']):.8g}\n"
            f"Quantity: {float(trade['qty']):.8g}"
        )
        if closed:
            text += f"\nClose: {reason or 'Exchange position closed'}"
            text += f"\nEstimated result: {float(pnl):+.6f} USDT"
        return text

    def _publish_trade_status(
        self,
        trade,
        price,
        pnl,
        events=None,
        closed=False,
        reason=None,
    ):
        events = events or []
        text = self._trade_text(trade, price, pnl, closed, reason)
        text_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
        keyboard = None
        individual_close_supported = (
            str(getattr(self.settings, "trading_env", "PAPER")).upper()
            == "PAPER"
            or str(getattr(self.settings, "position_mode", "one_way")).lower()
            != "hedge"
        )
        if not closed and individual_close_supported:
            keyboard = [[{
                "text": "إغلاق الصفقة (Close Position)",
                "callback_data": f"close_trade:{int(trade['id'])}",
            }]]

        for chat_id in self.settings.allowed_users:
            state = self.app.db.trade_followup(trade["id"], chat_id)
            message_id = state.get("message_id") if state else None
            if message_id is None:
                try:
                    result = self._request(
                        "sendMessage",
                        {
                            "chat_id": chat_id,
                            "text": text,
                            "disable_web_page_preview": True,
                            **(
                                {"reply_markup": {"inline_keyboard": keyboard}}
                                if keyboard
                                else {}
                            ),
                        },
                    )
                    message_id = result.get("message_id")
                    if message_id is None:
                        raise RuntimeError(
                            "Telegram sendMessage response has no message_id"
                        )
                    self.app.db.save_trade_followup(
                        trade["id"], chat_id, message_id, text_hash
                    )
                    state = self.app.db.trade_followup(trade["id"], chat_id)
                except Exception:
                    log.exception(
                        "Telegram trade status send failed | trade_id=%s | "
                        "chat_id=%s",
                        trade["id"],
                        chat_id,
                    )
                    continue
            elif state.get("text_hash") != text_hash:
                payload = {
                    "chat_id": chat_id,
                    "message_id": message_id,
                    "text": text,
                    "disable_web_page_preview": True,
                    "reply_markup": {
                        "inline_keyboard": keyboard or [],
                    },
                }
                try:
                    self._request("editMessageText", payload)
                    self.app.db.save_trade_followup(
                        trade["id"], chat_id, message_id, text_hash
                    )
                except Exception:
                    log.exception(
                        "Telegram trade status edit failed | trade_id=%s | "
                        "chat_id=%s",
                        trade["id"],
                        chat_id,
                    )

            state = self.app.db.trade_followup(trade["id"], chat_id) or {}
            for event, message in events:
                if state.get(event):
                    continue
                if self.send(chat_id, message, self.keyboard()):
                    self.app.db.mark_trade_followup_event(
                        trade["id"], chat_id, event
                    )

    def publish_trade_status(
        self,
        trade,
        price,
        pnl,
        events=None,
        closed=False,
        reason=None,
    ):
        self._publish_trade_status(
            trade,
            price,
            pnl,
            events=events,
            closed=closed,
            reason=reason,
        )

    def sync_open_trade_statuses(self):
        for trade in self.app.db.open_trades(
            self.settings.trading_env
        ):
            try:
                price = self.app.current_price(trade["symbol"])
                pnl = self.app.trade_pnl(trade, price)
                events = self.app.trade_milestones(trade, price)
                self._publish_trade_status(trade, price, pnl, events=events)
            except Exception:
                log.exception(
                    "Trade status refresh failed | trade_id=%s",
                    trade.get("id"),
                )

    def poll_once(self):
        if not self.started:
            self.startup()
        if not self.settings.telegram_token:
            return
        if not self.settings.allowed_users:
            log.error("Telegram polling disabled: allowlist empty")
            return
        try:
            params = {
                "timeout": 25,
                "offset": self.offset,
                "allowed_updates": json.dumps(
                    ["message", "callback_query"]
                ),
            }
            response = requests.get(
                f"{self.base}/getUpdates",
                params=params,
                timeout=35,
            )
            if response.status_code == 409:
                log.error(
                    "Telegram 409: another getUpdates consumer is active"
                )
                time.sleep(5)
                return
            if response.status_code == 400:
                log.error(
                    "Telegram 400: %s | params=%s",
                    response.text,
                    params,
                )
                time.sleep(3)
                return
            response.raise_for_status()
            data = response.json()
            for update in data.get("result", []):
                self.offset = max(
                    self.offset, update["update_id"] + 1
                )
                self.dispatch(update)
        except requests.RequestException:
            log.exception("Telegram polling error")
            time.sleep(3)

    def _answer_callback(self, query, text=None, alert=False):
        payload = {"callback_query_id": query.get("id")}
        if text:
            payload.update({"text": text, "show_alert": alert})
        try:
            self._request("answerCallbackQuery", payload, timeout=8)
        except Exception:
            log.exception("Telegram callback acknowledgement failed")

    def _dispatch_callback(self, query):
        user_id = query.get("from", {}).get("id")
        message = query.get("message", {})
        chat_id = message.get("chat", {}).get("id")
        data = str(query.get("data") or "")
        if not self._authorized(user_id) or not chat_id:
            self._answer_callback(query, "غير مصرح.", alert=True)
            return

        if data.startswith("close_trade:"):
            try:
                trade_id = int(data.split(":", 1)[1])
            except ValueError:
                self._answer_callback(query, "معرّف الصفقة غير صالح.", alert=True)
                return
            result = self.app.close_trade_from_telegram(trade_id)
            if result.get("success"):
                trade = result["trade"]
                self.publish_trade_status(
                    trade,
                    result["price"],
                    result["pnl"],
                    closed=True,
                    reason=result.get("reason", "MANUAL_CLOSE"),
                )
                self._answer_callback(query, "تم تأكيد إغلاق الصفقة.")
            else:
                self.send(
                    chat_id,
                    "⚠️ لم يتأكد إغلاق الصفقة.\n"
                    f"السبب: {result.get('error', 'unknown')}\n"
                    "تحقق من المركز في Bitget قبل إعادة المحاولة.",
                    self.keyboard(),
                )
                self._answer_callback(
                    query, "لم يتأكد الإغلاق؛ راجع رسالة البوت.", alert=True
                )
            return

        if data == "status":
            self.send(chat_id, self.app.status_text(), self.keyboard())
        elif data == "scan":
            self.send(chat_id, self.app.scan_once(False), self.keyboard())
        elif data == "auto":
            self.app.mode = "AUTO"
            self.send(
                chat_id,
                "🟢 AUTO selected. Execution remains gated by environment, "
                "arming, risk and equity.",
                self.keyboard(),
            )
        elif data == "manual":
            self.app.mode = "MANUAL"
            self.send(chat_id, "🔴 MANUAL selected.", self.keyboard())
        elif data == "safe":
            self.app.profile = "SAFE"
            self.send(chat_id, "🛡 SAFE selected.", self.keyboard())
        elif data == "adventurer":
            self.app.profile = "ADVENTURER"
            self.send(chat_id, "🚀 ADVENTURER selected.", self.keyboard())
        elif data == "coins":
            text = "\n".join(
                f"{'🟢' if item['enabled'] else '⚪'} {item['symbol']}"
                for item in self.app.db.list_symbols()
            ) or "No symbols"
            self.send(chat_id, text, self.keyboard())
        elif data == "trades":
            text = "\n".join(
                f"#{item['id']} {item['symbol']} {item['direction']} "
                f"{item['status']} pnl={item['pnl']}"
                for item in self.app.db.recent_trades(10)
            ) or "No trades"
            self.send(chat_id, text, self.keyboard())
        elif data == "closeall":
            self.send(
                chat_id,
                "⚠️ CLOSE ALL requires /closeall confirm",
                self.keyboard(),
            )
        self._answer_callback(query)

    def dispatch(self, update):
        if "callback_query" in update:
            self._dispatch_callback(update["callback_query"])
            return

        message = update.get("message", {})
        user_id = message.get("from", {}).get("id")
        chat_id = message.get("chat", {}).get("id")
        text = (message.get("text") or "").strip()
        if not chat_id or not text or not self._authorized(user_id):
            return

        parts = text.split()
        command = parts[0].split("@")[0].lower()
        args = parts[1:]
        if command in {"/start", "/help", "/status"}:
            self.send(chat_id, self.app.status_text(), self.keyboard())
        elif command == "/scan":
            self.send(chat_id, self.app.scan_once(False), self.keyboard())
        elif command == "/mode" and args and args[0].upper() in {
            "AUTO",
            "MANUAL",
        }:
            self.app.mode = args[0].upper()
            self.send(chat_id, self.app.status_text(), self.keyboard())
        elif command == "/profile" and args and args[0].upper() in {
            "SAFE",
            "ADVENTURER",
        }:
            self.app.profile = args[0].upper()
            self.send(chat_id, self.app.status_text(), self.keyboard())
        elif command == "/coins":
            text = "\n".join(
                f"{'🟢' if item['enabled'] else '⚪'} {item['symbol']}"
                for item in self.app.db.list_symbols()
            ) or "No symbols"
            self.send(chat_id, text, self.keyboard())
        elif command == "/trades":
            text = "\n".join(
                f"#{item['id']} {item['symbol']} {item['direction']} "
                f"{item['status']} pnl={item['pnl']}"
                for item in self.app.db.recent_trades(10)
            ) or "No trades"
            self.send(chat_id, text, self.keyboard())
        elif command == "/closeall" and args == ["confirm"]:
            if self.app.settings.trading_env == "PAPER":
                prices = self.app.get_prices(
                    self.app.db.enabled_symbols()
                )
                closed = self.app.paper.close_all(prices)
                self.send(
                    chat_id,
                    f"Closed {len(closed)} paper trades.",
                    self.keyboard(),
                )
            else:
                result = self.app.execution.close_all()
                if result.get("success"):
                    self.send(
                        chat_id,
                        "🛑 CLOSE ALL request sent to Bitget UTA.",
                        self.keyboard(),
                    )
                else:
                    self.send(
                        chat_id,
                        "⚠️ CLOSE ALL blocked/failed: "
                        f"{result.get('error', 'unknown')}",
                        self.keyboard(),
                    )
        else:
            self.send(chat_id, "Unknown command. Use /start.", self.keyboard())
