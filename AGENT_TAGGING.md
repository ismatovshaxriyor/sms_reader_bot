# Agent Tagging — G'oya va Implementatsiya

## Muammo

Bot guruhga order xabari yuborganda, o'sha order qaysi agentga tegishli ekanligi ko'rsatilmaydi. Natijada guruh a'zolari javobgarni bilmaydi.

## Yechim

Har bir order xabarida:
1. `order_id` ajratib olinadi
2. Tashqi API orqali o'sha orderga biriktirilgan agent ismi olinadi
3. Kontaktlar jadvalidan agent ismiga mos Telegram username topiladi
4. Xabar oxiriga `Agent: @username` qo'shib yuboriladi

---

## Arxitektura

```
Email keldi
    │
    ▼
order_id ajratib olinadi (email body dan regex/label orqali)
    │
    ▼
API ga POST so'rov: order_id → user_name (masalan: "Albert")
    │
    ▼
Contact jadvalida qidirish: "Albert" → "@albert_tg"
    │
    ├── topilsa  → "Agent: @albert_tg"   (Telegram tag ishlaydi)
    └── topilmasa → "Agent: Albert"      (oddiy matn, fallback)
    │
    ▼
Telegram xabari oxiriga (1 qator oralatib) qo'shib yuboriladi
```

---

## API

**Endpoint:** `POST https://usst.msgplane.com/api/rest/get/user_by_order_number/`

**So'rov parametrlari:**
| Parametr   | Qiymat               |
|------------|----------------------|
| subaction  | `get`                |
| api_key    | (MsgPlane API kalit) |
| record     | order raqami         |

**Javob:**
```json
{"result": "success", "user_id": "fb58458e-...", "user_name": "Albert"}
```

**Muhim:** Server self-signed SSL sertifikat ishlatadi — SSL tekshiruvi o'chirilishi kerak.

---

## Ma'lumotlar bazasi

### Contact jadvali

| Ustun              | Turi    | Izoh                          |
|--------------------|---------|-------------------------------|
| id                 | int     | avtomatik                     |
| msgplane_username  | string  | API dan keladigan ism (unique)|
| telegram_username  | string  | @username ko'rinishida        |
| created_at         | datetime|                               |

### CRUD funksiyalar

```python
list_contacts()                              # barchasi
get_contact_by_msgplane(username: str)       # bitta qidirish
upsert_contact(msgplane_username, tg_username)  # qo'shish / yangilash
delete_contact(contact_id: int)              # o'chirish
```

---

## API Client (msgplane.py)

```python
import asyncio, json, ssl, urllib.parse, urllib.request
from typing import Optional

_SSL_CTX = ssl.create_default_context()
_SSL_CTX.check_hostname = False
_SSL_CTX.verify_mode = ssl.CERT_NONE

def _fetch_sync(api_key, order_id, api_url):
    payload = urllib.parse.urlencode(
        {"subaction": "get", "api_key": api_key, "record": order_id}
    ).encode()
    req = urllib.request.Request(api_url, data=payload, method="POST")
    with urllib.request.urlopen(req, context=_SSL_CTX, timeout=10) as resp:
        data = json.loads(resp.read())
        if data.get("result") == "success":
            return data.get("user_name")
    return None

async def get_agent_name(api_key, order_id, api_url):
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _fetch_sync, api_key, order_id, api_url)
```

> `run_in_executor` ishlatiladi — sinxron `urllib` ni async bot ichida bloklashsiz ishlatish uchun.

---

## Xabar formati

```
NEW SD REQUEST

Order ID: 32015499-US
Price: $850
Pickup: Dallas, TX 75201 (Jan 15, 2025)
Dropoff: Los Angeles, CA 90001 (Jan 20, 2025)
Vehicle: 2020 Toyota Camry
Carrier: ABC Transport

Agent: @albert_tg
```

Xabar oxiriga **1 bo'sh qator** tashlab, keyin agent ko'rsatiladi.

---

## Kontakt boshqaruvi (Telegram bot UI)

Kontaktlarni command emas, **inline tugmalar** orqali boshqarish:

```
Asosiy menyu → "Kontaktlar" tugmasi
    │
    ▼
┌─────────────────────────────────┐
│ Kontaktlar: 2 ta                │
│                                 │
│ 1. Albert → @albert_tg          │
│ 2. John   → @john_driver        │
│                                 │
│ [➖ O'chirish: Albert]          │
│ [➖ O'chirish: John]            │
│ [➕ Kontakt qo'shish]           │
│ [🔄 Yangilash]  [Back]          │
└─────────────────────────────────┘
```

### Kontakt qo'shish jarayoni (2 qadam)

```
"➕ Kontakt qo'shish" bosiladi
    │
    ▼
Bot: "MsgPlane dagi agent ismini kiriting (masalan: Albert):"
    │
Admin yozadi: Albert
    │
    ▼
Bot: "Telegram username kiriting (@username):"
    │
Admin yozadi: @albert_tg
    │
    ▼
✅ Saqlandi: Albert → @albert_tg
```

---

## .env sozlamalari

```env
MSGPLANE_API_KEY=your_api_key_here
MSGPLANE_API_URL=https://usst.msgplane.com/api/rest/get/user_by_order_number/
```

---

## Logika (poll/yangi xabar kelganda)

```python
agent_mention = ""
if settings.msgplane_api_key:
    order_id = parse_order_id(email_body)        # emaildan ajratib olish
    if order_id:
        user_name = await get_agent_name(...)    # API so'rov
        if user_name:
            contact = get_contact_by_msgplane(user_name)
            agent_mention = contact.telegram_username if contact else user_name

message = format_message(email, agent_mention=agent_mention)
```

---

## Boshqa botga ko'chirish uchun kerakli narsalar

1. `Contact` DB modeli + CRUD funksiyalar
2. `msgplane.py` API client
3. `.env` ga `MSGPLANE_API_KEY` va `MSGPLANE_API_URL`
4. `Settings` ga ikki yangi field
5. Xabar formatida `agent_mention` parametri
6. Xabar yuborishdan oldin API call + contact lookup
7. Bot UI da kontakt paneli (inline keyboard)
