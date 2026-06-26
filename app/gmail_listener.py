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
import os
import re
import time
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


# "New Text Message from (929) 454-5969 on 06/26/2026 9:58 AM"
_SUBJECT_RE = re.compile(
    r"(?:new\s+)?text\s+message\s+from\s+(?P<from>.+?)\s+on\s+(?P<on>.+)$",
    re.IGNORECASE,
)

# Maydon yorliqlari (From/To/Received/Message) joylashuvini topish uchun
_LABEL_RE = re.compile(r"\b(From|To|Received|Sent|Date|Message)\s*:", re.IGNORECASE)

# Xat oxiridagi keraksiz matnlar (SMS matnidan keyin keladi)
_BOILERPLATE_RE = re.compile(
    r"\s*(?:To reply using"
    r"|Thank you for using RingCentral"
    r"|Hello AI Receptionist"
    r"|AIR turns"
    r"|Learn more"
    r"|This (?:message|email) was sent"
    r"|Reply\s+Forward)",
    re.IGNORECASE,
)

# Yorliq nomlarini ichki kalitga moslash
_LABEL_MAP = {
    "from": "from",
    "to": "to",
    "received": "received",
    "sent": "received",
    "date": "received",
    "message": "message",
}


def is_sms_notification(subject: str) -> bool:
    """Mavzuga qarab xat SMS bildirishnomasi ekanini aniqlaydi."""
    s = (subject or "").lower()
    return ("text message" in s) or bool(_SUBJECT_RE.search(subject or ""))


def _parse_fields(text: str) -> dict[str, str]:
    """Matndan From/To/Received/Message maydonlarini mustaqil ajratadi.

    Yorliqlar joylashuvini topib, qiymatlarni ketma-ket yorliqlar orasidan oladi —
    tartib yoki ba'zi maydonlar yo'qligidan qat'i nazar ishlaydi.
    """
    fields: dict[str, str] = {}
    matches = list(_LABEL_RE.finditer(text))
    for i, mt in enumerate(matches):
        key = _LABEL_MAP.get(mt.group(1).lower())
        if not key:
            continue
        start = mt.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        value = text[start:end].strip(" \t:-")
        value = _BOILERPLATE_RE.split(value, maxsplit=1)[0].strip(" \t:-")
        # "Message" so'zi kirish matnida ham uchraydi ("...new text message:"),
        # shuning uchun asl maydon — oxirgi moslik. Qolganlari uchun birinchisi.
        if key == "message":
            fields[key] = value
        else:
            fields.setdefault(key, value)
    return fields


def parse_message(raw: bytes) -> dict:
    """Xom (RFC822) xatni parse qilib, SMS maydonlarini qaytaradi."""
    msg = email.message_from_bytes(raw)
    message_id = (msg.get("Message-ID") or "").strip()
    subject = _decode(msg.get("Subject"))

    body_text = _get_text(msg)
    norm = " ".join(body_text.split())  # barcha bo'shliqlarni bitta qatorga keltiramiz

    fields = _parse_fields(norm)
    from_number = fields.get("from", "")
    to_name = fields.get("to", "")
    received = fields.get("received", "")
    text = fields.get("message", "")
    if text:
        text = _BOILERPLATE_RE.split(text, maxsplit=1)[0].strip()

    # Subjectdan zaxira (eng ishonchli manba): "...from <raqam> on <sana>"
    sm = _SUBJECT_RE.search(subject)
    if sm:
        if not from_number:
            from_number = sm.group("from").strip()
        if not received:
            received = sm.group("on").strip()

    return {
        "message_id": message_id,
        "from_number": from_number or "—",
        "to_name": to_name or "—",
        "time": received,
        "text": text,
        "subject": subject,
        "is_sms": is_sms_notification(subject),
    }


def extract_images(raw: bytes) -> list[tuple[str, bytes]]:
    """Xatdan haqiqiy rasm biriktirmalarini ajratadi (inline logolar emas)."""
    msg = email.message_from_bytes(raw)
    images: list[tuple[str, bytes]] = []
    for part in msg.walk():
        ctype = (part.get_content_type() or "").lower()
        if not ctype.startswith("image/"):
            continue
        disp = str(part.get("Content-Disposition") or "").lower()
        filename = part.get_filename()
        cid = part.get("Content-ID")
        # Inline (Content-ID'li) rasmlar — odatda logo/banner; o'tkazib yuboramiz
        is_attachment = "attachment" in disp or bool(filename)
        if not is_attachment or (cid and "attachment" not in disp):
            continue
        data = part.get_payload(decode=True)
        if not data:
            continue
        images.append((filename or "image.jpg", data))
    return images


def _dump_debug_email(raw: bytes, subject: str) -> None:
    """Parse bo'sh chiqqan SMS xatini keyinroq tahlil qilish uchun saqlaydi."""
    try:
        os.makedirs("debug_emails", exist_ok=True)
        safe = re.sub(r"[^A-Za-z0-9]+", "_", subject or "no_subject")[:50]
        path = os.path.join("debug_emails", f"{safe}_{int(time.time())}.eml")
        with open(path, "wb") as f:
            f.write(raw)
        logger.warning("Parse bo'sh — xat tahlil uchun saqlandi: %s", path)
    except Exception as exc:
        logger.error("debug email saqlashda xato: %s", exc)


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

        # 0) SMS bo'lmagan RingCentral xatlari (ovozli xabar, reklama, ...) — o'tkazamiz
        if not parsed["is_sms"]:
            logger.info("SMS emas, o'tkazib yuborildi: %s", parsed["subject"])
            await asyncio.to_thread(_mark_read, service, mid)
            continue

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

        images = extract_images(raw)

        # Matn ham, rasm ham topilmasa — keyinroq tahlil uchun xatni saqlab qo'yamiz
        if not parsed["text"] and not images:
            _dump_debug_email(raw, parsed["subject"])

        sms = {
            "from_number": parsed["from_number"],
            "to_name": parsed["to_name"],
            "time": parsed["time"],
            "text": parsed["text"],
            "images": images,
        }
        logger.info(
            "Yangi SMS (Gmail): %s → %s | matn=%d belgi, rasm=%d ta",
            parsed["from_number"], parsed["to_name"], len(parsed["text"]), len(images),
        )
        try:
            await on_sms(sms)
        except Exception as exc:
            logger.error("SMS uzatishda xato: %s", exc)

        # 3) Xatni o'qilgan deb belgilaymiz (qayta ishlanmasligi uchun)
        await asyncio.to_thread(_mark_read, service, mid)
