"""Gmail API tinglovchisi: RingCentral SMS-bildirishnoma xatlarini o'qiydi.

RingCentral kelgan SMS'larni `service@ringcentral.com` manzilidan Gmail'ga yuboradi.
Bu modul Gmail API orqali pochta qutisini muntazam tekshiradi, yangi (o'qilmagan)
xatlarni parse qiladi, kerakli ma'lumotlarni ajratadi va Telegram'ga uzatish uchun
on_sms'ga beradi.

Gmail API mijozi bloklovchi (sync) bo'lgani uchun barcha tarmoq amallari
asyncio.to_thread orqali alohida ipda bajariladi.
"""

import asyncio
import base64
import email
import logging
import re
from email.header import decode_header, make_header
from email.message import Message
from typing import Awaitable, Callable

from bs4 import BeautifulSoup

from app import config, db, gmail_auth

logger = logging.getLogger(__name__)

OnSms = Callable[[dict], Awaitable[None]]
OnAuthNeeded = Callable[[str], Awaitable[None]]


# ===== Yordamchi parse funksiyalari =====

def _decode(value: str | None) -> str:
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        return value


def _decode_payload(part: Message) -> str:
    payload = part.get_payload(decode=True)
    if payload is None:
        return ""
    charset = part.get_content_charset() or "utf-8"
    try:
        return payload.decode(charset, errors="replace")
    except (LookupError, AttributeError):
        return payload.decode("utf-8", errors="replace")


def _html_to_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    return soup.get_text(" ")


def _get_text(msg: Message) -> str:
    """Xatdan matnni oladi: avval text/plain, bo'lmasa HTML'dan."""
    plain = None
    html = None
    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            disp = str(part.get("Content-Disposition") or "")
            if "attachment" in disp.lower():
                continue
            if ctype == "text/plain" and plain is None:
                plain = _decode_payload(part)
            elif ctype == "text/html" and html is None:
                html = _decode_payload(part)
    else:
        if msg.get_content_type() == "text/html":
            html = _decode_payload(msg)
        else:
            plain = _decode_payload(msg)

    if plain and plain.strip():
        return plain
    if html:
        return _html_to_text(html)
    return ""


_FIELDS_RE = re.compile(
    r"From\s*:\s*(?P<from>.+?)\s+"
    r"To\s*:\s*(?P<to>.+?)\s+"
    r"Received\s*:\s*(?P<received>.+?)\s+"
    r"Message\s*:\s*(?P<message>.+?)"
    r"(?:\s+To reply|\s+Thank you for using RingCentral|$)",
    re.IGNORECASE,
)

_SUBJECT_RE = re.compile(r"from\s+(?P<from>.+?)\s+on\s+(?P<on>.+)$", re.IGNORECASE)


def parse_message(raw: bytes) -> dict:
    """Xom (RFC822) xatni parse qilib, SMS maydonlarini qaytaradi."""
    msg = email.message_from_bytes(raw)
    message_id = (msg.get("Message-ID") or "").strip()
    subject = _decode(msg.get("Subject"))

    body_text = _get_text(msg)
    norm = " ".join(body_text.split())  # barcha bo'shliqlarni bitta qatorga keltiramiz

    from_number = to_name = received = text = ""
    m = _FIELDS_RE.search(norm)
    if m:
        from_number = m.group("from").strip()
        to_name = m.group("to").strip()
        received = m.group("received").strip()
        text = m.group("message").strip()
    else:
        logger.warning("Xat parse qilinmadi (mavzu: %s)", subject)

    # Subjectdan zaxira ma'lumot ("New Text Message from <raqam> on <sana>")
    if not from_number or not received:
        sm = _SUBJECT_RE.search(subject)
        if sm:
            from_number = from_number or sm.group("from").strip()
            received = received or sm.group("on").strip()

    return {
        "message_id": message_id,
        "from_number": from_number or "—",
        "to_name": to_name or "—",
        "time": received,
        "text": text,
        "subject": subject,
    }


# ===== Gmail API (bloklovchi) amallar =====

def _fetch_new(service) -> list[tuple[str, bytes]]:
    """service@ringcentral.com'dan kelgan o'qilmagan xatlarni (id, raw) qaytaradi."""
    query = f"from:{config.RINGCENTRAL_SENDER} is:unread"
    resp = (
        service.users()
        .messages()
        .list(userId="me", q=query, maxResults=25)
        .execute()
    )
    results: list[tuple[str, bytes]] = []
    for item in resp.get("messages", []):
        mid = item["id"]
        full = (
            service.users()
            .messages()
            .get(userId="me", id=mid, format="raw")
            .execute()
        )
        raw = base64.urlsafe_b64decode(full["raw"].encode("utf-8"))
        results.append((mid, raw))
    return results


def _mark_read(service, mid: str) -> None:
    service.users().messages().modify(
        userId="me", id=mid, body={"removeLabelIds": ["UNREAD"]}
    ).execute()


# ===== Asosiy tsikl =====

async def run_gmail_listener(
    on_sms: OnSms, on_auth_needed: OnAuthNeeded | None = None
) -> None:
    """Doimiy ishlaydi: ulanadi, muntazam tekshiradi, xato bo'lsa qayta ulanadi.

    Token yo'q/yaroqsiz bo'lsa, on_auth_needed orqali adminlarga (bir marta) xabar beradi.
    """
    backoff = 5
    notified_auth = False
    while True:
        try:
            service = await asyncio.to_thread(gmail_auth.build_service)
            logger.info(
                "Gmail API'ga ulanildi. Har %ss da tekshiriladi.", config.POLL_INTERVAL
            )
            backoff = 5
            notified_auth = False
            while True:
                await _poll_once(service, on_sms)
                await asyncio.sleep(config.POLL_INTERVAL)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error(
                "Gmail listener xatosi: %s. %ss dan keyin qayta ulanish...", exc, backoff
            )
            # Token muammosi bo'lsa, adminlarga bir marta avtorizatsiya so'rovini yuboramiz
            if on_auth_needed and not notified_auth:
                token_ok = await asyncio.to_thread(gmail_auth.has_valid_token)
                if not token_ok:
                    try:
                        await on_auth_needed("⚠️ Gmail token yo'q yoki muddati tugagan.")
                    except Exception as e:
                        logger.error("on_auth_needed xatosi: %s", e)
                    notified_auth = True
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)


async def _poll_once(service, on_sms: OnSms) -> None:
    items = await asyncio.to_thread(_fetch_new, service)
    for mid, raw in items:
        parsed = parse_message(raw)

        # 1) Skip ro'yxatidagi raqamlar
        if (
            config.SKIP_NUMBERS
            and config.normalize_number(parsed["from_number"]) in config.SKIP_NUMBERS
        ):
            logger.info("O'tkazib yuborildi (skip raqam): %s", parsed["from_number"])
            await asyncio.to_thread(_mark_read, service, mid)
            continue

        # 2) Dublikat (Gmail xabar ID'si bo'yicha)
        if await db.is_processed(mid):
            await asyncio.to_thread(_mark_read, service, mid)
            continue
        await db.mark_processed(mid)

        sms = {
            "from_number": parsed["from_number"],
            "to_name": parsed["to_name"],
            "time": parsed["time"],
            "text": parsed["text"],
        }
        logger.info("Yangi SMS (Gmail): %s → %s", parsed["from_number"], parsed["to_name"])
        try:
            await on_sms(sms)
        except Exception as exc:
            logger.error("SMS uzatishda xato: %s", exc)

        # 3) Xatni o'qilgan deb belgilaymiz (qayta ishlanmasligi uchun)
        await asyncio.to_thread(_mark_read, service, mid)
