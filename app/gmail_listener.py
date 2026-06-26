"""Gmail API tinglovchisi: RingCentral SMS / ovozli xabar bildirishnomalarini o'qiydi.

RingCentral xatlari (to'g'ridan-to'g'ri yoki forward qilingan) Gmail'ga keladi.
Bu modul Gmail API orqali pochta qutisini muntazam tekshiradi, har bir xatni parse
qilib, kerakli ma'lumotlarni (matn, vaqt, raqam) va biriktirmalarni (rasm / audio /
hujjat) ajratadi va Telegram'ga uzatish uchun on_sms'ga beradi.

Gmail API mijozi bloklovchi (sync) bo'lgani uchun tarmoq amallari asyncio.to_thread
orqali alohida ipda bajariladi.
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


# ===== Matn ajratish yordamchilari =====

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
    return BeautifulSoup(html, "html.parser").get_text(" ")


def _get_text(msg: Message) -> str:
    """Xatdan matnni oladi: avval text/plain (yulduzchali format), bo'lmasa HTML."""
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


# ===== Parse =====

# Subject: "New Text Message from (931) 272-7090 on 06/26/2026 9:18 AM"
#          "New Voice Message from Update 4 - REYNA (956) 529-6576 on ... AM"
_SUBJECT_RE = re.compile(
    r"(?P<kind>text|voice|voicemail|fax|mms)\s+message\s+from\s+"
    r"(?P<from>.+?)\s+on\s+(?P<on>.+?)\s*$",
    re.IGNORECASE,
)

# RingCentral matnida maydonlar yulduzcha bilan: "*From:*", "*Message:*", ...
# (forward konvertidagi oddiy "Date:/To:/Subject:" bilan adashmaslik uchun)
_AST_LABEL_RE = re.compile(r"\*\s*([A-Za-z][A-Za-z ]*?)\s*:\s*\*")

# SMS/preview matnidan keyingi keraksiz qism
_BOILERPLATE_RE = re.compile(
    r"\s*(?:To reply using"
    r"|To listen to this message"
    r"|open the attachment"
    r"|Thank you for using RingCentral"
    r"|Hello AI Receptionist"
    r"|AIR turns"
    r"|Learn more"
    r"|This (?:message|email) was sent"
    r"|-{3,})",
    re.IGNORECASE,
)


def _parse_ast_fields(text: str) -> dict[str, str]:
    """\"*Label:* value\" ko'rinishidagi maydonlarni ajratadi."""
    fields: dict[str, str] = {}
    matches = list(_AST_LABEL_RE.finditer(text))
    for i, mt in enumerate(matches):
        label = mt.group(1).strip().lower()
        start = mt.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        value = text[start:end].strip()
        value = _BOILERPLATE_RE.split(value, maxsplit=1)[0].strip()
        fields.setdefault(label, value)
    return fields


def parse_message(raw: bytes) -> dict:
    """Xom (RFC822) xatni parse qilib, xabar maydonlarini qaytaradi."""
    msg = email.message_from_bytes(raw)
    message_id = (msg.get("Message-ID") or "").strip()
    subject = _decode(msg.get("Subject"))

    # Subject — eng ishonchli manba (kind + from + vaqt)
    kind = None
    subj_from = subj_time = ""
    sm = _SUBJECT_RE.search(subject)
    if sm:
        k = sm.group("kind").lower()
        kind = "voice" if k in ("voice", "voicemail") else ("fax" if k == "fax" else "sms")
        subj_from = sm.group("from").strip()
        subj_time = sm.group("on").strip()

    body_text = _get_text(msg)
    norm = " ".join(body_text.split())
    fields = _parse_ast_fields(norm)

    from_number = subj_from or fields.get("from", "")
    to_name = fields.get("to", "")
    received = subj_time or fields.get("received", "")
    length = fields.get("length", "")

    if kind == "voice":
        text = fields.get("voicemail preview", "").strip().strip('"').strip()
    else:
        text = fields.get("message", "")

    return {
        "message_id": message_id,
        "kind": kind or "sms",
        "from_number": from_number or "—",
        "to_name": to_name or "—",
        "time": received,
        "length": length,
        "text": text,
        "subject": subject,
        "is_ringcentral": kind is not None,
    }


def extract_attachments(raw: bytes) -> list[tuple[str, str, bytes]]:
    """Haqiqiy biriktirmalarni (filename, content_type, bytes) ajratadi.

    Inline logolarni (Content-ID'li, attachment bo'lmagan) o'tkazib yuboradi.
    """
    msg = email.message_from_bytes(raw)
    out: list[tuple[str, str, bytes]] = []
    for part in msg.walk():
        if part.is_multipart():
            continue
        ctype = (part.get_content_type() or "").lower()
        if ctype in ("text/plain", "text/html"):
            continue
        disp = str(part.get("Content-Disposition") or "").lower()
        filename = part.get_filename()
        cid = part.get("Content-ID")
        if "attachment" in disp:
            pass
        elif filename and not cid:
            pass
        else:
            continue  # inline (logo/banner) — o'tkazib yuboramiz
        data = part.get_payload(decode=True)
        if not data:
            continue
        out.append((filename or "file", ctype, data))
    return out


def _dump_debug_email(raw: bytes, subject: str) -> None:
    """Parse bo'sh chiqqan xatni keyinroq tahlil qilish uchun saqlaydi."""
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
    """GMAIL_QUERY bo'yicha xatlarni (id, raw) ro'yxatini qaytaradi."""
    resp = (
        service.users()
        .messages()
        .list(userId="me", q=config.GMAIL_QUERY, maxResults=25)
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

        # 0) RingCentral xabari bo'lmasa — tegmaymiz (o'qilmagan holatda qoldiramiz)
        if not parsed["is_ringcentral"]:
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

        attachments = extract_attachments(raw)

        # Matn ham, biriktirma ham yo'q bo'lsa — keyinroq tahlil uchun saqlaymiz
        if not parsed["text"] and not attachments:
            _dump_debug_email(raw, parsed["subject"])

        sms = {
            "kind": parsed["kind"],
            "from_number": parsed["from_number"],
            "to_name": parsed["to_name"],
            "time": parsed["time"],
            "length": parsed["length"],
            "text": parsed["text"],
            "attachments": attachments,
        }
        logger.info(
            "Yangi %s: %s → %s | matn=%d belgi, biriktirma=%d",
            parsed["kind"], parsed["from_number"], parsed["to_name"],
            len(parsed["text"]), len(attachments),
        )
        try:
            await on_sms(sms)
        except Exception as exc:
            logger.error("Uzatishda xato: %s", exc)

        await asyncio.to_thread(_mark_read, service, mid)
