import logging
from khtb_lab.config import Settings
from khtb_lab.app import KhtbLab
from khtb_lab.telegram_bot import TelegramBot

def main():
    s=Settings()
    logging.basicConfig(level=getattr(logging,s.log_level,logging.INFO),
                        format="%(asctime)s | %(levelname)s | %(message)s")
    app=KhtbLab(s)
    tg=TelegramBot(s,app)
    log=logging.getLogger(__name__)
    log.info("Telegram polling enabled")
    app.run(tg)

if __name__=="__main__":
    main()
