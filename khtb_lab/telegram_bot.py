from __future__ import annotations
import logging
import re
import time
import requests

log = logging.getLogger(__name__)
SYMBOL_RE = re.compile(r'^[A-Z0-9]{2,20}/USDT:USDT$')


class TelegramBot:
    def __init__(self, settings, app):
        self.settings = settings
        self.app = app
        self.offset = 0
        self.base = f"https://api.telegram.org/bot{settings.telegram_token}"

    def send(self, chat_id, text, keyboard=None):
        if not self.settings.telegram_token:
            return False
        payload = {"chat_id": chat_id, "text": text, "disable_web_page_preview": True}
        if keyboard:
            payload["reply_markup"] = {"inline_keyboard": keyboard}
        try:
            r = requests.post(self.base + "/sendMessage", json=payload, timeout=15)
            r.raise_for_status()
            return True
        except Exception:
            log.exception("Telegram sendMessage failed")
            return False

    def control_keyboard(self):
        mode = self.app.mode
        return [
            [
                {"text": f"🟢 AUTO ({'ON' if mode == 'AUTO' else 'OFF'})", "callback_data": "mode_auto"},
                {"text": f"🔴 MANUAL ({'ON' if mode == 'MANUAL' else 'OFF'})", "callback_data": "mode_manual"},
            ],
            [
                {"text": "📊 STATUS", "callback_data": "status"},
                {"text": "🔎 SCAN NOW", "callback_data": "scan"},
            ],
            [{"text": "🛑 CLOSE ALL", "callback_data": "closeall_request"}],
        ]

    def send_startup(self, chat_id=None):
        chat = chat_id or self.settings.telegram_chat_id if hasattr(self.settings, "telegram_chat_id") else None
        if not chat:
            # TELEGRAM_CHAT_ID is optional in this project; default to the first allowed user when supplied.
            chat = next(iter(self.settings.allowed_users), None)
        if not chat:
            log.warning("Startup Telegram message skipped: no chat id")
            return False
        text = (
            "🟢 Khtb Lab started\n\n"
            f"Environment: {self.app.settings.trading_env}\n"
            f"Mode: {self.app.mode}\n"
            f"Profile: {self.app.profile}\n"
            f"Symbols: {len(self.app.db.enabled_symbols())}\n"
            f"Exchange: {'BITGET DEMO ARMED' if self.app.execution.enabled else ('LOCAL PAPER' if self.app.settings.trading_env == 'paper' else 'BITGET DEMO READ-ONLY')}\n\n"
            "The main scanner is running every " + str(self.app.settings.scan_seconds) + "s."
        )
        return self.send(chat, text, self.control_keyboard())

    def poll_once(self):
        if not self.settings.telegram_token:
            raise RuntimeError("TELEGRAM_BOT_TOKEN is missing")
        if not self.settings.allowed_users:
            raise RuntimeError("TELEGRAM_ALLOWED_USER_IDS is empty; Telegram control fails closed")
        try:
            r = requests.get(
                self.base + "/getUpdates",
                params={"timeout": 25, "offset": self.offset, "allowed_updates": ["message", "callback_query"]},
                timeout=35,
            )
            r.raise_for_status()
            data = r.json()
            if not data.get("ok"):
                raise RuntimeError("Telegram getUpdates returned ok=false")
            for update in data.get("result", []):
                self.offset = max(self.offset, update["update_id"] + 1)
                if "callback_query" in update:
                    q = update["callback_query"]
                    user = q.get("from", {}).get("id")
                    chat = q.get("message", {}).get("chat", {}).get("id")
                    if self._authorized(user) and chat:
                        self.handle_callback(chat, q.get("data"))
                    try:
                        requests.post(self.base + "/answerCallbackQuery", json={"callback_query_id": q["id"]}, timeout=8)
                    except Exception:
                        pass
                    continue

                msg = update.get("message", {})
                user = msg.get("from", {}).get("id")
                chat = msg.get("chat", {}).get("id")
                text = (msg.get("text") or "").strip()
                if not chat or not text:
                    continue
                if not self._authorized(user):
                    self.send(chat, "غير مصرح لك باستخدام البوت.")
                    continue
                self.handle(chat, text)
        except requests.RequestException:
            log.exception("Telegram polling error")
            time.sleep(3)

    def _authorized(self, user_id):
        try:
            return bool(self.settings.allowed_users) and int(user_id) in self.settings.allowed_users
        except (TypeError, ValueError):
            return False

    def handle_callback(self, chat, data):
        try:
            if data == "status":
                self.send(chat, self.app.status_text(), self.control_keyboard())
            elif data == "scan":
                self.send(chat, self.app.scan_once(allow_auto=False), self.control_keyboard())
            elif data == "mode_auto":
                if self.app.settings.trading_env == "paper":
                    self.app.mode = "AUTO"
                elif self.app.settings.trading_env == "bitget_demo" and self.app.execution.enabled:
                    self.app.mode = "AUTO"
                else:
                    raise ValueError("AUTO يحتاج Bitget Demo execution مسلحًا أو PAPER mode")
                self.send(chat, "🟢 تم تفعيل AUTO", self.control_keyboard())
            elif data == "mode_manual":
                self.app.mode = "MANUAL"
                self.send(chat, "🔴 تم تفعيل MANUAL", self.control_keyboard())
            elif data == "closeall_request":
                self.send(chat, "⚠️ تأكيد إغلاق كل الصفقات المفتوحة؟", [[
                    {"text": "نعم — CLOSE ALL", "callback_data": "closeall_confirm"},
                    {"text": "إلغاء", "callback_data": "closeall_cancel"},
                ]])
            elif data == "closeall_confirm":
                result = self.app.close_all()
                self.send(chat, f"تم إرسال أمر إغلاق {len(result)} صفقة.", self.control_keyboard())
            elif data == "closeall_cancel":
                self.send(chat, "تم الإلغاء.", self.control_keyboard())
        except Exception as exc:
            log.exception("Callback failed: %s", data)
            self.send(chat, f"تعذر التنفيذ: {exc}", self.control_keyboard())

    def handle(self, chat, text):
        parts = text.split()
        cmd = parts[0].split("@")[0].lower()
        args = parts[1:]
        try:
            if cmd in {"/start", "/help"}:
                self.send(chat, "Khtb Lab\n\n/status الحالة\n/coins العملات\n/coin_add SYMBOL\n/coin_enable SYMBOL | /coin_disable SYMBOL | /coin_remove SYMBOL\n/mode MANUAL أو AUTO\n/profile SAFE أو ADVENTURER\n/scan تحليل الآن بدون تنفيذ\n/backtest SYMBOL\n/open SYMBOL LONG|SHORT ENTRY STOP TARGET — تنفيذ يدوي في البيئة الحالية\n/closeall ثم /closeall confirm\n/trades\n/stats", self.control_keyboard())
            elif cmd == "/status":
                self.send(chat, self.app.status_text(), self.control_keyboard())
            elif cmd == "/coins":
                self.send(chat, "\n".join(f"{'🟢' if x['enabled'] else '⚪'} {x['symbol']}" for x in self.app.db.list_symbols()) or "لا توجد عملات")
            elif cmd == "/coin_add" and len(args) == 1:
                s = args[0].upper()
                if not SYMBOL_RE.fullmatch(s):
                    raise ValueError("الصيغة المطلوبة مثل FET/USDT:USDT")
                self.app.market.validate_symbol(s)
                self.app.db.add_symbol(s)
                self.send(chat, f"تم تفعيل {s}")
            elif cmd in {"/coin_enable", "/coin_disable", "/coin_remove"} and len(args) == 1:
                s = args[0].upper()
                if cmd == "/coin_remove":
                    ok = self.app.db.remove_symbol(s)
                    result = "تم حذف العملة" if ok else "لم تُحذف: غير موجودة أو لها صفقة مفتوحة"
                else:
                    ok = self.app.db.set_symbol(s, cmd == "/coin_enable")
                    result = "تم تحديث العملة" if ok else "العملة غير موجودة"
                self.send(chat, result)
            elif cmd == "/mode" and len(args) == 1 and args[0].upper() in {"AUTO", "MANUAL"}:
                mode = args[0].upper()
                if mode == "AUTO":
                    if self.app.settings.trading_env == "paper":
                        pass
                    elif not self.app.execution.enabled:
                        raise ValueError("AUTO في Demo يحتاج ENABLE_EXCHANGE_ORDERS=true وDEMO_TRADING_ARMED=true")
                self.app.mode = mode
                self.send(chat, f"تم تغيير الوضع إلى {mode}.", self.control_keyboard())
            elif cmd == "/profile" and len(args) == 1 and args[0].upper() in {"SAFE", "ADVENTURER"}:
                self.app.profile = args[0].upper()
                self.app.paper.risk_fraction = self.app.settings.risk_per_trade * (0.5 if self.app.profile == "SAFE" else 1.0)
                self.send(chat, f"تم اختيار {self.app.profile}.")
            elif cmd == "/scan":
                self.send(chat, self.app.scan_once(allow_auto=False))
            elif cmd == "/backtest" and len(args) == 1:
                result = self.app.backtest_symbol(args[0].upper())
                self.send(chat, f"Backtest {args[0].upper()}\nTrades: {result['count']}\nWin rate: {result['win_rate']:.1f}%\nAverage R: {result['avg_r']:.2f}\nProfit factor: {result['profit_factor']:.2f}\nMax drawdown: {result['max_drawdown_r']:.2f}R")
            elif cmd == "/open" and len(args) == 5:
                s = args[0].upper(); d = args[1].upper(); entry, stop, target = map(float, args[2:5])
                if d not in {"LONG", "SHORT"}:
                    raise ValueError("الاتجاه LONG أو SHORT")
                self.app.market.validate_symbol(s)
                if not (stop < entry < target if d == "LONG" else target < entry < stop):
                    raise ValueError("مستويات الوقف والهدف غير صحيحة")
                self.send(chat, self.app.manual_open(s, d, entry, stop, target))
            elif cmd == "/closeall":
                if args != ["confirm"]:
                    self.send(chat, "تأكيد: أرسل /closeall confirm")
                else:
                    result = self.app.close_all()
                    self.send(chat, f"تم إرسال إغلاق {len(result)} صفقة.")
            elif cmd == "/trades":
                rows = self.app.db.recent_trades(10)
                self.send(chat, "\n".join(f"#{r['id']} {r['symbol']} {r['direction']} {r['status']} entry={r['entry']:.8g} pnl={r['pnl']} [{r['source']}]" for r in rows) or "لا توجد صفقات")
            elif cmd == "/stats":
                s = self.app.db.stats()
                self.send(chat, f"Closed: {s['closed']}\nWin rate: {s['win_rate']:.1f}%\nPnL: {s['pnl']:.4f}\nAvg R: {s['avg_r']:.2f}\nProfit factor: {s['profit_factor']:.2f}")
            else:
                self.send(chat, "أمر غير معروف. أرسل /help")
        except Exception as exc:
            log.exception("Command failed: %s", cmd)
            self.send(chat, f"تعذر تنفيذ الأمر: {exc}")
