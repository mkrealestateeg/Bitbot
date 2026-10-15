from __future__ import annotations
import json,logging,time,requests
log=logging.getLogger(__name__)
class TelegramBot:
    def __init__(self,settings,app):
        self.settings=settings;self.app=app;self.offset=0;self.base=f"https://api.telegram.org/bot{settings.telegram_token}";self.started=False
    def send(self,chat_id,text,keyboard=None):
        if not self.settings.telegram_token:return False
        p={"chat_id":chat_id,"text":text,"disable_web_page_preview":True}
        if keyboard:p["reply_markup"]={"inline_keyboard":keyboard}
        try:r=requests.post(self.base+"/sendMessage",json=p,timeout=15);r.raise_for_status();return True
        except Exception:log.exception("Telegram sendMessage failed");return False
    def broadcast(self,text):
        ok=False
        for uid in self.settings.allowed_users:ok=self.send(uid,text,self.keyboard()) or ok
        return ok
    def _authorized(self,u):return bool(self.settings.allowed_users) and int(u) in self.settings.allowed_users
    def startup(self):
        if not self.settings.telegram_token:return False
        try:
            me=requests.get(self.base+"/getMe",timeout=10);me.raise_for_status()
            wh=requests.post(self.base+"/deleteWebhook",json={"drop_pending_updates":False},timeout=10);wh.raise_for_status()
            log.info("Telegram authenticated as @%s",me.json().get("result",{}).get("username","unknown"))
            self.started=True
            self.broadcast(self.app.status_text())
            log.info("Telegram startup message sent");return True
        except Exception:log.exception("Telegram startup failed");return False
    def keyboard(self):
        return [[{"text":"📊 STATUS","callback_data":"status"},{"text":"🔄 SCAN NOW","callback_data":"scan"}],[{"text":"🟢 AUTO","callback_data":"auto"},{"text":"🔴 MANUAL","callback_data":"manual"}],[{"text":"🛡 SAFE","callback_data":"safe"},{"text":"🚀 ADVENTURER","callback_data":"adventurer"}],[{"text":"🪙 COINS","callback_data":"coins"},{"text":"📈 TRADES","callback_data":"trades"}],[{"text":"🛑 CLOSE ALL","callback_data":"closeall"}]]
    def poll_once(self):
        if not self.started:self.startup()
        if not self.settings.telegram_token:return
        if not self.settings.allowed_users:log.error("Telegram polling disabled: allowlist empty");return
        try:
            params={"timeout":25,"offset":self.offset,"allowed_updates":json.dumps(["message","callback_query"])}
            r=requests.get(self.base+"/getUpdates",params=params,timeout=35)
            if r.status_code==409:log.error("Telegram 409: another getUpdates consumer is active");time.sleep(5);return
            if r.status_code==400:log.error("Telegram 400: %s | params=%s",r.text,params);time.sleep(3);return
            r.raise_for_status();data=r.json()
            for u in data.get("result",[]):self.offset=max(self.offset,u["update_id"]+1);self.dispatch(u)
        except requests.RequestException:log.exception("Telegram polling error");time.sleep(3)
    def dispatch(self,u):
        if "callback_query" in u:
            q=u["callback_query"];user=q.get("from",{}).get("id");chat=q.get("message",{}).get("chat",{}).get("id");data=q.get("data")
            if self._authorized(user) and chat:
                if data=="status":self.send(chat,self.app.status_text(),self.keyboard())
                elif data=="scan":self.send(chat,self.app.scan_once(False),self.keyboard())
                elif data=="auto":self.app.mode="AUTO";self.send(chat,"🟢 AUTO selected. Execution remains gated by environment, arming, risk and equity.",self.keyboard())
                elif data=="manual":self.app.mode="MANUAL";self.send(chat,"🔴 MANUAL selected.",self.keyboard())
                elif data=="safe":self.app.profile="SAFE";self.send(chat,"🛡 SAFE selected.",self.keyboard())
                elif data=="adventurer":self.app.profile="ADVENTURER";self.send(chat,"🚀 ADVENTURER selected.",self.keyboard())
                elif data=="coins":self.send(chat,"\n".join(f"{'🟢' if x['enabled'] else '⚪'} {x['symbol']}" for x in self.app.db.list_symbols()) or "No symbols",self.keyboard())
                elif data=="trades":self.send(chat,"\n".join(f"#{x['id']} {x['symbol']} {x['direction']} {x['status']} pnl={x['pnl']}" for x in self.app.db.recent_trades(10)) or "No trades",self.keyboard())
                elif data=="closeall":self.send(chat,"⚠️ CLOSE ALL requires /closeall confirm",self.keyboard())
            try:requests.post(self.base+"/answerCallbackQuery",json={"callback_query_id":q.get("id")},timeout=8)
            except Exception:pass
            return
        m=u.get("message",{});user=m.get("from",{}).get("id");chat=m.get("chat",{}).get("id");text=(m.get("text") or "").strip()
        if not chat or not text or not self._authorized(user):return
        cmd=text.split()[0].split("@")[0].lower();args=text.split()[1:]
        if cmd in {"/start","/help","/status"}:self.send(chat,self.app.status_text(),self.keyboard())
        elif cmd=="/scan":self.send(chat,self.app.scan_once(False),self.keyboard())
        elif cmd=="/mode" and args and args[0].upper() in {"AUTO","MANUAL"}:self.app.mode=args[0].upper();self.send(chat,self.app.status_text(),self.keyboard())
        elif cmd=="/profile" and args and args[0].upper() in {"SAFE","ADVENTURER"}:self.app.profile=args[0].upper();self.send(chat,self.app.status_text(),self.keyboard())
        elif cmd=="/coins":self.send(chat,"\n".join(f"{'🟢' if x['enabled'] else '⚪'} {x['symbol']}" for x in self.app.db.list_symbols()) or "No symbols",self.keyboard())
        elif cmd=="/trades":self.send(chat,"\n".join(f"#{x['id']} {x['symbol']} {x['direction']} {x['status']} pnl={x['pnl']}" for x in self.app.db.recent_trades(10)) or "No trades",self.keyboard())
        elif cmd=="/closeall" and args==["confirm"]:
            if self.app.settings.trading_env=="PAPER":
                closed=self.app.paper.close_all(self.app.get_prices(self.app.db.enabled_symbols()));self.send(chat,f"Closed {len(closed)} paper trades.",self.keyboard())
            else:
                result=self.app.execution.close_all()
                if result.get("success"):
                    self.send(chat,"🛑 CLOSE ALL request sent to Bitget UTA.",self.keyboard())
                else:
                    self.send(chat,f"⚠️ CLOSE ALL blocked/failed: {result.get('error','unknown')}",self.keyboard())
        else:self.send(chat,"Unknown command. Use /start.",self.keyboard())
