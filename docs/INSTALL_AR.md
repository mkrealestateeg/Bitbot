# دليل تثبيت Khtb Lab بالعربي

## قبل البدء
هذه نسخة مستقلة عن مستودع Khtp الحالي. لا تستبدل ملفات المشروع القديم. الوضع الافتراضي محاكاة محلية PAPER، ولا يتم إرسال أوامر تداول للمنصة في هذه النسخة.

## التشغيل على جهازك أو سيرفر Linux

1. ثبّت Python 3.11 أو أحدث.
2. ارفع المجلد إلى مستودع GitHub جديد باسم `khtb-lab`.
3. على السيرفر استنسخ المستودع وثبّت الاعتماديات:

   ```bash
   python -m venv .venv
   source .venv/bin/activate
   pip install -e '.[test]'
   cp .env.example .env
   ```

4. أنشئ بوت Telegram جديدًا عبر `@BotFather`، ثم ضع التوكن في `.env`.
5. ضع رقم المستخدم الرقمي في `TELEGRAM_ALLOWED_USER_IDS`. إذا تركته فارغًا فلن يقبل البوت أوامر التحكم.
6. اترك الإعدادات التالية كما هي في البداية:

   ```dotenv
   TRADING_ENV=paper
   TRADING_MODE=MANUAL
   ENABLE_EXCHANGE_ORDERS=false
   DEMO_TRADING_ARMED=false
   LIVE_TRADING_ARMED=false
   ```

7. اختبر وشغّل:

   ```bash
   pytest -q
   python main.py
   ```

## الأوامر الأساسية

- `/start` للمساعدة
- `/status` للحالة
- `/coins` للعملات
- `/coin_add FET/USDT:USDT` لإضافة عملة
- `/scan` لتحليل السوق
- `/mode AUTO` لتفعيل المحاكاة التلقائية فقط
- `/profile SAFE` أو `/profile ADVENTURER`
- `/open SYMBOL LONG ENTRY STOP TARGET` لتسجيل صفقة ورقية يدوية
- `/closeall` ثم `/closeall confirm` لإغلاق الصفقات الورقية
- `/trades` و`/stats`

## تنبيه مهم
لا يدعم الإصدار الحالي إرسال أوامر حقيقية إلى Bitget Demo أو Live. اتصال السوق العام وتحليل الأسعار لا يثبت أن أوامر الدخول أو وقف الخسارة وجني الأرباح تعمل. سنضيف موصل التداول التجريبي فقط بعد اختبار موثوق من Bitget والتحقق المستقل من حماية الصفقة.
