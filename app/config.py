"""Konfiguratsiya: .env faylidan sozlamalarni yuklaydi va tekshiradi."""

import os
import re

from dotenv import load_dotenv

load_dotenv(override=True)


def _get_required(name: str) -> str:
    """Majburiy o'zgaruvchini oladi; yo'q yoki bo'sh bo'lsa xato beradi."""
    value = (os.environ.get(name) or "").strip()
    if not value:
        raise RuntimeError(
            f"Majburiy .env o'zgaruvchisi yo'q yoki bo'sh: {name}. "
            f".env.example'dan nusxa olib, .env faylini to'ldiring."
        )
    return value


def _parse_admin_ids(raw: str) -> set[int]:
    """\"123, 456\" ko'rinishidagi qatorni {123, 456} to'plamiga aylantiradi."""
    ids: set[int] = set()
    for part in (raw or "").replace(" ", "").split(","):
        if not part:
            continue
        try:
            ids.add(int(part))
        except ValueError as exc:
            raise RuntimeError(
                f"ADMIN_IDS noto'g'ri formatda: '{part}' butun son emas."
            ) from exc
    return ids


def normalize_number(raw: str) -> str:
    """Telefon raqamini taqqoslash uchun normallashtiradi (oxirgi 10 raqam)."""
    digits = re.sub(r"\D", "", raw or "")
    return digits[-10:] if len(digits) >= 10 else digits


def _parse_skip_numbers(raw: str) -> set[str]:
    """Vergul bilan ajratilgan raqamlarni normallashtirilgan to'plamga aylantiradi."""
    nums: set[str] = set()
    for part in (raw or "").split(","):
        n = normalize_number(part)
        if n:
            nums.add(n)
    return nums


# ===== Telegram =====
TELEGRAM_BOT_TOKEN: str = _get_required("TELEGRAM_BOT_TOKEN")
ADMIN_IDS: set[int] = _parse_admin_ids(os.environ.get("ADMIN_IDS", ""))

# ===== Gmail (IMAP) =====
GMAIL_ADDRESS: str = _get_required("GMAIL_ADDRESS")
GMAIL_APP_PASSWORD: str = _get_required("GMAIL_APP_PASSWORD").replace(" ", "")
IMAP_HOST: str = (os.environ.get("IMAP_HOST") or "imap.gmail.com").strip()
IMAP_PORT: int = int(os.environ.get("IMAP_PORT") or "993")

# RingCentral SMS-bildirishnomalari shu manzildan keladi
RINGCENTRAL_SENDER: str = (
    os.environ.get("RINGCENTRAL_SENDER") or "service@ringcentral.com"
).strip()

# Pochta qutisini necha soniyada bir tekshirish
POLL_INTERVAL: int = int(os.environ.get("POLL_INTERVAL") or "15")

# Bu raqamlardan kelgan SMS'lar o'tkazib yuboriladi (normallashtirilgan, oxirgi 10 raqam)
SKIP_NUMBERS: set[str] = _parse_skip_numbers(
    os.environ.get("SKIP_NUMBERS") or "(833) 963-2500"
)

# ===== Ma'lumotlar bazasi =====
DATABASE_PATH: str = (os.environ.get("DATABASE_PATH") or "bot.db").strip()
