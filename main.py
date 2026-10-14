import logging, time
from khtb_lab.config import Settings
from khtb_lab.app import KhtbLab
from khtb_lab.telegram_bot import TelegramBot

def main():
    settings = Settings()
    logging.basicConfig(level=getattr(logging, settings.log_level, logging.INFO), format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")
    app = KhtbLab(settings)
    bot = None
    if settings.telegram_token:
        if not settings.allowed_users:
            raise SystemExit("Set TELEGRAM_ALLOWED_USER_IDS. Commands fail closed without an allowlist.")
        bot = TelegramBot(settings, app)
        logging.getLogger(__name__).info("Telegram polling enabled")
        bot.send_startup()
        last_scan = 0.0
        while True:
            bot.poll_once()
            now = time.time()
            if now - last_scan >= max(15, settings.scan_seconds):
                try:
                    logging.getLogger(__name__).info("\n%s", app.scan_once(allow_auto=True))
                except Exception:
                    logging.getLogger(__name__).exception("Main scan cycle failed")
                last_scan = now
    else:
        logging.getLogger(__name__).warning("No Telegram token; running market scan loop only")
        app.run()

if __name__ == "__main__":
    main()
