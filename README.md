# RingCentral SMS (Gmail) → Telegram Bot

RingCentral SMS-bildirishnomalarini **Gmail API** orqali o'qib, tasdiqlangan
Telegram **guruh(lar)iga** uzatadigan bot.

RingCentral kelgan SMS'larni `service@ringcentral.com` manzilidan Gmail'ga yuboradi.
Bot pochta qutisini Gmail API orqali muntazam tekshiradi, xatdan ma'lumotlarni ajratadi
va Telegram'ga uzatadi.

- 📧 Gmail **API (OAuth2)** orqali ishlaydi — ochiq HTTPS server yoki RingCentral API kerak emas.
- 🗄 Maqsad guruhlar **SQLite** bazasida saqlanadi va bot orqali boshqariladi.
- 🔐 Bot bilan muloqot faqat **adminlar** uchun; barcha amallar **tugmalar** orqali.
- 🚫 Belgilangan raqamlardan (masalan `(833) 963-2500`) kelgan SMS'lar o'tkazib yuboriladi.
- 🔁 Xato bo'lsa avtomatik qayta ulanadi; takror xatlar (xabar ID) filtrlanadi.
- 🛟 Tasdiqlangan guruh bo'lmasa, SMS adminning shaxsiy chatiga yuboriladi (yo'qolmaydi).

---

## 1. O'rnatish

```bash
cd "sms_reader_bot"
python3 -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

## 2. Sozlash (`.env`)

`.env.example` faylidan nusxa oling va to'ldiring:

```bash
cp .env.example .env
```

| O'zgaruvchi | Tavsif |
|---|---|
| `TELEGRAM_BOT_TOKEN` | @BotFather'dan olingan token |
| `ADMIN_IDS` | Admin Telegram ID'lari, vergul bilan (masalan `12345,67890`) |
| `GMAIL_CREDENTIALS_FILE` | OAuth client fayli yo'li (bo'sh — `client_secret*.json` avto-topiladi) |
| `GMAIL_TOKEN_FILE` | OAuth token fayli (standart `token.json`) |
| `RINGCENTRAL_SENDER` | Jo'natuvchi manzil (standart `service@ringcentral.com`) |
| `POLL_INTERVAL` | Pochtani necha soniyada tekshirish (standart `15`) |
| `SKIP_NUMBERS` | O'tkazib yuboriladigan raqamlar, vergul bilan |
| `DATABASE_PATH` | (ixtiyoriy) baza fayli, standart `bot.db` |

### Telegram tomoni
1. [@BotFather](https://t.me/BotFather)'da `/newbot` → tokenni oling.
2. Botga shaxsiy chatda `/start` yuboring → bot sizning ID'ingizni ko'rsatadi → `ADMIN_IDS`ga yozing.

### Gmail tomoni (API / OAuth)
1. [Google Cloud Console](https://console.cloud.google.com/) → loyiha yarating.
2. **APIs & Services → Library → Gmail API → Enable**.
3. **OAuth consent screen** sozlang; o'zingizni **Test user** sifatida qo'shing.
4. **Credentials → Create Credentials → OAuth client ID → Application type: Desktop app**.
5. `client_secret_*.json` faylini yuklab, loyiha papkasiga qo'ying
   (yoki `GMAIL_CREDENTIALS_FILE`da yo'lini ko'rsating).
6. **Avtorizatsiya** — ikki usuldan biri:
   - **Telegram orqali (server uchun qulay):** botni ishga tushiring va admin `/start`
     bossin. Token bo'lmasa, bot avtomatik **avtorizatsiya havolasini** yuboradi. Havolani
     oching, ruxsat bering, so'ng `http://localhost/?code=...` manzilidagi **code** qiymatini
     (yoki butun havolani) botga qaytaring. Bot `token.json`ni o'zi saqlaydi.
   - **Lokal skript orqali:**
     ```bash
     python -m app.authorize
     ```
     Brauzer ochiladi va `token.json` yaratiladi.

> Token muddati tugasa, bot adminlarga yangi avtorizatsiya havolasini avtomatik yuboradi
> (yoki menyudagi «🔑 Gmail avtorizatsiya» tugmasini bosing).

> RingCentral tomonida: SMS → email bildirishnoma (notification) yoqilgan bo'lishi va xatlar
> ushbu Gmail manziliga kelishi kerak.

> ⚠️ `client_secret_*.json` va `token.json` — **maxfiy** fayllar, git'ga tushmaydi.

## 3. Ishga tushirish

```bash
python -m app.main
```

Loglar quyidagicha bo'lishi kerak:
```
Ma'lumotlar bazasi tayyor: bot.db
Telegram bot ishga tushdi: @your_bot
Gmail API'ga ulanildi. Har 15s da tekshiriladi.
```

## 4. Foydalanish (tugmalar orqali)

Botda **faqat bitta komanda** — `/start`. Qolgan barcha amallar inline **tugmalar** orqali.

`/start` → asosiy menyu:
- **📋 Guruhlar** — guruhlar ro'yxati; har bir guruh yonida «✅ Tasdiqlash» yoki «🗑 O'chirish» tugmasi
- **🧪 Test xabar** — barcha active guruhlarga test xabar yuboradi
- **ℹ️ Yordam** — qisqa qo'llanma

### Guruh qo'shish
1. Botni kerakli Telegram guruhiga qo'shing va **admin** qiling.
2. Bot avtomatik aniqlaydi va sizga (adminga) **«✅ Tasdiqlash» tugmasi** bilan xabar yuboradi.
   (Yoki `/start` → «📋 Guruhlar» — guruh `pending` holatda ko'rinadi.)
3. **«✅ Tasdiqlash»** tugmasini bosing. Bot guruhda admin ekanini tekshiradi va `active` qiladi.
   Endi SMS'lar shu guruhga keladi. ID terish shart emas.

> Tasdiqlangan guruh bo'lmasa, SMS'lar to'g'ridan-to'g'ri adminning shaxsiy chatiga keladi.

## 5. Tekshirish

1. `/start` → «🧪 Test xabar» → xabar active guruhga (yoki admin chatiga) tushishi kerak.
2. RingCentral raqamiga tashqaridan SMS yuboring → u Gmail'ga kelgach (≤ `POLL_INTERVAL`),
   bot uni guruhga uzatadi.
3. `(833) 963-2500` (yoki `SKIP_NUMBERS`dagi) raqamdan kelgan SMS uzatilmasligini tekshiring.

## Qanday ishlaydi

1. `gmail_listener` har `POLL_INTERVAL` soniyada Gmail API orqali tekshiradi:
   `from:service@ringcentral.com is:unread` so'rovi bilan **o'qilmagan** xatlarni qidiradi.
2. Har bir xat parse qilinadi: **From** (raqam), **To** (qabul qiluvchi), **Received** (vaqt),
   **Message** (SMS matni). Subject ham zaxira sifatida ishlatiladi.
3. `SKIP_NUMBERS`dagi raqam bo'lsa — o'tkazib yuboriladi.
4. Gmail xabar ID'si bo'yicha takror tekshiriladi (dublikat yuborilmaydi).
5. Matn formatlanib, DB'dagi `active` guruhlarga (yoki admin chatiga) uzatiladi.
6. Xat «o'qilgan» (UNREAD olib tashlanadi) deb belgilanadi.

> Eslatma: bot xatlarni «o'qilmagan» holati bo'yicha topadi. Agar xatni Gmail'da bot'dan
> oldin o'zingiz ochib qo'ysangiz, u o'qilgan bo'lib qoladi va uzatilmasligi mumkin.

## Loyiha tuzilishi

```
app/
├── main.py            # entrypoint (bot + Gmail listener parallel)
├── config.py          # .env yuklash va validatsiya
├── db.py              # SQLite: guruhlar + dedup
├── bot.py             # aiogram bot, IsAdmin, tugmalar, forward_sms
├── gmail_auth.py      # Gmail API OAuth (credential/token)
├── authorize.py       # bir martalik OAuth avtorizatsiya skripti
└── gmail_listener.py  # Gmail API: xat o'qish, parse, skip, dedup, reconnect
```

## Eslatmalar

- Uzoq ishlash uchun botni `systemd`, `pm2`, `supervisor` yoki Docker ostida ishlating
  (qarang: [DEPLOY.md](DEPLOY.md)).
- Bir vaqtning o'zida **faqat bitta nusxa** ishlasin (Telegram `getUpdates` konflikti).
- `bot.db`, `.env`, `client_secret*.json`, `token.json` — `.gitignore`da, git'ga qo'shmang.
- OAuth consent "Testing" rejimida bo'lsa, refresh token ~7 kunda eskirishi mumkin —
  ilovani "In production" qiling yoki o'zingizni test user sifatida qo'shing.
