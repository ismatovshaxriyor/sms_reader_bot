"""Gmail (IMAP) tinglovchisi: RingCentral SMS-bildirishnoma xatlarini o'qiydi.

RingCentral SMS'larni `service@ringcentral.com` manzilidan Gmail'ga yuboradi.
Bu modul pochta qutisini muntazam tekshirib, yangi xatlarni parse qiladi,
kerakli ma'lumotlarni ajratadi va Telegram'ga uzatish uchun on_sms'ga beradi.

IMAP bloklovchi (sync) bo'lgani uchun barcha tarmoq amallari asyncio.to_thread
orqali alohida ipda bajariladi.
"""

import asyncio
import email
import imaplib
import logging
import re
from email.header import decode_header, make_header
from email.message import Message
from typing import Awaitable, Callable

from bs4 import BeautifulSoup

from app import config, db

logger = logging.getLogger(__name__)

OnSms = Callable[[dict], Awaitable[None]]


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
    """Xom xatni parse qilib, SMS maydonlarini qaytaradi."""
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


# ===== IMAP (bloklovchi) amallar =====

def _connect() -> imaplib.IMAP4_SSL:
    imap = imaplib.IMAP4_SSL(config.IMAP_HOST, config.IMAP_PORT)
    imap.login(config.GMAIL_ADDRESS, config.GMAIL_APP_PASSWORD)
    imap.select("INBOX")
    return imap


def _fetch_new(imap: imaplib.IMAP4_SSL) -> list[tuple[bytes, bytes]]:
    """service@ringcentral.com'dan kelgan o'qilmagan xatlarni (uid, raw) qaytaradi."""
    criteria = f'(FROM "{config.RINGCENTRAL_SENDER}" UNSEEN)'
    typ, data = imap.uid("search", None, criteria)
    if typ != "OK" or not data or data[0] is None:
        return []
    results: list[tuple[bytes, bytes]] = []
    for uid in data[0].split():
        typ, msg_data = imap.uid("fetch", uid, "(RFC822)")
        if typ != "OK" or not msg_data or msg_data[0] is None:
            continue
        results.append((uid, msg_data[0][1]))
    return results


def _mark_seen(imap: imaplib.IMAP4_SSL, uid: bytes) -> None:
    imap.uid("store", uid, "+FLAGS", "(\\Seen)")


# ===== Asosiy tsikl =====

async def run_gmail_listener(on_sms: OnSms) -> None:
    """Doimiy ishlaydi: ulanadi, muntazam tekshiradi, uzilsa qayta ulanadi."""
    backoff = 5
    while True:
        imap: imaplib.IMAP4_SSL | None = None
        try:
            imap = await asyncio.to_thread(_connect)
            logger.info(
                "Gmail IMAP'ga ulanildi (%s). Har %ss da tekshiriladi.",
                config.GMAIL_ADDRESS,
                config.POLL_INTERVAL,
            )
            backoff = 5
            while True:
                await _poll_once(imap, on_sms)
                await asyncio.sleep(config.POLL_INTERVAL)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error(
                "Gmail listener xatosi: %s. %ss dan keyin qayta ulanish...", exc, backoff
            )
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)
        finally:
            if imap is not None:
                try:
                    await asyncio.to_thread(imap.logout)
                except Exception:
                    pass


async def _poll_once(imap: imaplib.IMAP4_SSL, on_sms: OnSms) -> None:
    items = await asyncio.to_thread(_fetch_new, imap)
    for uid, raw in items:
        parsed = parse_message(raw)

        # 1) Skip ro'yxatidagi raqamlar
        if config.SKIP_NUMBERS and config.normalize_number(parsed["from_number"]) in config.SKIP_NUMBERS:
            logger.info("O'tkazib yuborildi (skip raqam): %s", parsed["from_number"])
            await asyncio.to_thread(_mark_seen, imap, uid)
            continue

        # 2) Dublikat (Message-ID bo'yicha)
        msg_id = parsed["message_id"]
        if msg_id and await db.is_processed(msg_id):
            await asyncio.to_thread(_mark_seen, imap, uid)
            continue
        if msg_id:
            await db.mark_processed(msg_id)

        sms = {
            "from_number": parsed["from_number"],
            "to_name": parsed["to_name"],
            "time": parsed["time"],
            "text": parsed["text"],
        }
        logger.info("Yangi SMS (email): %s → %s", parsed["from_number"], parsed["to_name"])
        try:
            await on_sms(sms)
        except Exception as exc:
            logger.error("SMS uzatishda xato: %s", exc)

        # 3) Xatni o'qilgan deb belgilaymiz (qayta ishlanmasligi uchun)
        await asyncio.to_thread(_mark_seen, imap, uid)
