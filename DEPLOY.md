# Serverga deploy (Linux + systemd)

Ubuntu/Debian server uchun qo'llanma. Bot `systemd` xizmati sifatida ishlaydi:
serverda doimiy ishlaydi, yiqilsa avtomatik qayta tushadi, server qayta yuklansa
o'zi ishga tushadi.

Quyida `/opt/sms_reader_bot` papkasi va `botuser` foydalanuvchisi misol qilingan —
o'zingizникiga moslang.

## 1. Tayyorgarlik (server)

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip git
```

Bot uchun alohida foydalanuvchi (root'da ishlatmang):

```bash
sudo useradd -r -m -d /opt/sms_reader_bot -s /usr/sbin/nologin botuser
```

## 2. Kodni klonlash

```bash
sudo git clone https://github.com/ismatovshaxriyor/sms_reader_bot.git /opt/sms_reader_bot
sudo chown -R botuser:botuser /opt/sms_reader_bot
```

## 3. Virtual muhit va kutubxonalar

```bash
cd /opt/sms_reader_bot
sudo -u botuser python3 -m venv venv
sudo -u botuser venv/bin/pip install --upgrade pip
sudo -u botuser venv/bin/pip install -r requirements.txt
```

## 4. `.env` faylini yaratish

```bash
sudo -u botuser cp .env.example .env
sudo -u botuser nano .env      # yoki vim
```

To'ldiring: `TELEGRAM_BOT_TOKEN`, `ADMIN_IDS`
(va kerak bo'lsa `SKIP_NUMBERS`, `POLL_INTERVAL`).

> `.env` faqat serverda turadi va git'ga tushmaydi. Ruxsatlarni cheklang:
> ```bash
> sudo chmod 600 /opt/sms_reader_bot/.env
> ```

## 5. Gmail OAuth (`client_secret` va avtorizatsiya)

OAuth client faylini (`client_secret_*.json`) serverga ko'chiring:
```bash
scp client_secret_*.json USER@SERVER:/opt/sms_reader_bot/
sudo chown botuser:botuser /opt/sms_reader_bot/client_secret_*.json
sudo chmod 600 /opt/sms_reader_bot/client_secret_*.json
```

So'ng avtorizatsiya — **ikki usuldan biri**:

- **Telegram orqali (tavsiya, server uchun qulay):** botni ishga tushiring (6-bo'lim).
  Token bo'lmagani uchun bot adminlarga avtorizatsiya havolasini yuboradi. Havolani oching,
  ruxsat bering va `code` qiymatini botga qaytaring — `token.json` serverda o'zi yaratiladi.
  (Bu holda token.json'ni ko'chirish shart emas.)

- **Lokalda yaratib ko'chirish:** lokal kompyuterda `python -m app.authorize` (brauzer
  ochiladi) → hosil bo'lgan `token.json`ni serverga ko'chiring:
  ```bash
  scp token.json USER@SERVER:/opt/sms_reader_bot/
  sudo chown botuser:botuser /opt/sms_reader_bot/token.json
  sudo chmod 600 /opt/sms_reader_bot/token.json
  ```

Tekshirish (ixtiyoriy — qo'lda bir marta ishga tushirib ko'rish):
```bash
cd /opt/sms_reader_bot
sudo -u botuser venv/bin/python -m app.main
# "Gmail API'ga ulanildi" chiqsa, Ctrl+C bilan to'xtating
```

## 6. systemd xizmatini o'rnatish

```bash
sudo cp /opt/sms_reader_bot/deploy/sms-reader-bot.service /etc/systemd/system/
```

Agar papka yoki foydalanuvchi boshqacha bo'lsa, faylni tahrirlang:
```bash
sudo nano /etc/systemd/system/sms-reader-bot.service
# User=, Group=, WorkingDirectory=, ExecStart= ni moslang
```

Ishga tushirish:
```bash
sudo systemctl daemon-reload
sudo systemctl enable --now sms-reader-bot
```

## 7. Holat va loglar

```bash
# Holat
sudo systemctl status sms-reader-bot

# Jonli loglar
sudo journalctl -u sms-reader-bot -f

# Oxirgi 100 qator
sudo journalctl -u sms-reader-bot -n 100 --no-pager
```

Muvaffaqiyatli ishga tushsa, loglarda:
```
Telegram bot ishga tushdi: @...
Gmail API'ga ulanildi. Har 15s da tekshiriladi.
```

## 8. Boshqaruv komandalar

```bash
sudo systemctl restart sms-reader-bot   # qayta ishga tushirish
sudo systemctl stop sms-reader-bot      # to'xtatish
sudo systemctl start sms-reader-bot     # ishga tushirish
sudo systemctl disable sms-reader-bot   # avtomatik ishga tushishni o'chirish
```

## 9. Yangilanish (kod o'zgarganda)

```bash
cd /opt/sms_reader_bot
sudo -u botuser git pull
sudo -u botuser venv/bin/pip install -r requirements.txt   # kerak bo'lsa
sudo systemctl restart sms-reader-bot
```

## Eslatmalar

- Bir vaqtning o'zida **faqat bitta nusxa** ishlasin (Telegram `getUpdates` konflikti).
  Lokal kompyuteringizda test uchun ishlatib turgan bo'lsangiz, uni to'xtating.
- Server vaqti (timezone) to'g'ri bo'lsin — loglar va vaqtlar uchun.
- `token.json` muddati tugasa (masalan OAuth "Testing" rejimida ~7 kun), lokalda qayta
  `python -m app.authorize` qilib, yangi `token.json`ni serverga ko'chiring.
- `client_secret*.json` va `token.json` faqat serverda turadi — git'ga tushmaydi.
