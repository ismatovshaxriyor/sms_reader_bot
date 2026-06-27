"""Gmail API listener: reads RingCentral SMS / voicemail notifications.

RingCentral emails (direct or forwarded) arrive in Gmail.
This module periodically checks the mailbox via the Gmail API, parses each
message to extract relevant data (text, time, number) and attachments
(image / audio / document), then passes them to on_sms for forwarding to Telegram.

Since the Gmail API client is blocking (sync), network operations are run
in a separate thread via asyncio.to_thread.
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


# ===== Text extraction helpers =====

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
    """Extracts text from the email: first text/plain (asterisk format), otherwise HTML."""
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

# Fields in RingCentral body text marked with asterisks: "*From:*", "*Message:*", ...
# (to avoid confusion with plain "Date:/To:/Subject:" in the forward envelope)
_AST_LABEL_RE = re.compile(r"\*\s*([A-Za-z][A-Za-z ]*?)\s*:\s*\*")

# Fallback: text extracted from HTML may not have asterisks.
# We only search for known RingCentral labels (to prevent false matches).
_PLAIN_LABEL_RE = re.compile(
    r"\b(From|To|Received|Message|Voicemail Preview|Length)\s*:\s*",
    re.IGNORECASE,
)

# Boilerplate text following the SMS/preview content
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
    """Extracts fields in \"*Label:* value\" or plain \"Label: value\" format.

    First tries the asterisk format. If not found, falls back to plain format
    with known label names (for text extracted from HTML).
    """
    fields: dict[str, str] = {}
    matches = list(_AST_LABEL_RE.finditer(text))

    # No asterisk labels found — fall back to plain label format
    if not matches:
        matches = list(_PLAIN_LABEL_RE.finditer(text))

    for i, mt in enumerate(matches):
        label = mt.group(1).strip().lower()
        start = mt.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        value = text[start:end].strip()
        value = _BOILERPLATE_RE.split(value, maxsplit=1)[0].strip()
        fields.setdefault(label, value)
    return fields


def parse_message(raw: bytes) -> dict:
    """Parses a raw (RFC822) email and returns the message fields."""
    msg = email.message_from_bytes(raw)
    message_id = (msg.get("Message-ID") or "").strip()
    subject = _decode(msg.get("Subject"))

    # Subject is the most reliable source (kind + from + time)
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
        text = fields.get("voicemail preview", "").strip()
        # Remove plain and smart/curly quotation marks
        text = text.strip('"').strip('\u201c\u201d\u00ab\u00bb').strip()
    else:
        text = fields.get("message", "")

    # Fallback: Agar "Message:" degan label bo'lmasa, butun email tekstidan xabarni ajratib olamiz
    if not text and kind != "voice":
        lines = body_text.split('\n')
        start_idx = -1
        end_idx = len(lines)
        
        for i, line in enumerate(lines):
            if re.search(r'\bReceived\s*:', line, re.IGNORECASE):
                start_idx = i + 1
            elif start_idx != -1 and _BOILERPLATE_RE.search(line):
                end_idx = i
                break
                
        if start_idx != -1 and start_idx < end_idx:
            extracted_lines = []
            for line in lines[start_idx:end_idx]:
                if not re.search(r'^\*?Attachment:\*?', line.strip(), re.IGNORECASE):
                    extracted_lines.append(line)
            fallback_text = "\n".join(extracted_lines).strip()
            if fallback_text:
                text = fallback_text

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
    """Extracts actual attachments (filename, content_type, bytes).

    Skips inline logos (those with Content-ID but no attachment disposition).
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
            continue  # inline (logo/banner) — skip
        data = part.get_payload(decode=True)
        if not data:
            continue
        out.append((filename or "file", ctype, data))
    return out


def _dump_debug_email(raw: bytes, subject: str) -> None:
    """Saves an email that parsed empty for later analysis."""
    try:
        os.makedirs("debug_emails", exist_ok=True)
        safe = re.sub(r"[^A-Za-z0-9]+", "_", subject or "no_subject")[:50]
        path = os.path.join("debug_emails", f"{safe}_{int(time.time())}.eml")
        with open(path, "wb") as f:
            f.write(raw)
        logger.warning("Parse empty — email saved for analysis: %s", path)
    except Exception as exc:
        logger.error("Error saving debug email: %s", exc)


# ===== Gmail API (blocking) operations =====

def _fetch_new(service) -> list[tuple[str, bytes]]:
    """Returns a list of emails (id, raw) matching GMAIL_QUERY."""
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


# ===== Main loop =====

async def run_gmail_listener(
    on_sms: OnSms, on_auth_needed: OnAuthNeeded | None = None
) -> None:
    """Runs continuously: connects, polls periodically, reconnects on error.

    If the token is missing/invalid, notifies admins (once) via on_auth_needed.
    """
    backoff = 5
    notified_auth = False
    is_first_start = True
    while True:
        try:
            service = await asyncio.to_thread(gmail_auth.build_service)
            logger.info(
                "Connected to Gmail API. Polling every %ss.", config.POLL_INTERVAL
            )
            
            if is_first_start:
                try:
                    await _send_startup_test_messages(service, on_sms)
                except Exception as e:
                    logger.error("Failed to send startup test messages: %s", e)
                is_first_start = False
                
            backoff = 5
            notified_auth = False
            while True:
                await _poll_once(service, on_sms)
                await asyncio.sleep(config.POLL_INTERVAL)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error(
                "Gmail listener error: %s. Reconnecting in %ss...", exc, backoff
            )
            if on_auth_needed and not notified_auth:
                token_ok = await asyncio.to_thread(gmail_auth.has_valid_token)
                if not token_ok:
                    try:
                        await on_auth_needed("⚠️ Gmail token is missing or expired.")
                    except Exception as e:
                        logger.error("on_auth_needed error: %s", e)
                    notified_auth = True
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)


async def _poll_once(service, on_sms: OnSms) -> None:
    items = await asyncio.to_thread(_fetch_new, service)
    for mid, raw in items:
        parsed = parse_message(raw)

        # 0) Not a RingCentral message — leave untouched (keep unread)
        if not parsed["is_ringcentral"]:
            continue

        # 1) Numbers in the skip list
        if (
            config.SKIP_NUMBERS
            and config.normalize_number(parsed["from_number"]) in config.SKIP_NUMBERS
        ):
            logger.info("Skipped (skip number): %s", parsed["from_number"])
            await asyncio.to_thread(_mark_read, service, mid)
            continue

        # 2) Duplicate (by Gmail message ID)
        if await db.is_processed(mid):
            await asyncio.to_thread(_mark_read, service, mid)
            continue
        await db.mark_processed(mid)

        # 3) Skip voicemails with "Unavailable" preview
        if parsed["kind"] == "voice" and parsed["text"].lower() in ("unavailable", ""):
            logger.info("Skipped (voicemail unavailable or empty): %s", parsed["from_number"])
            await asyncio.to_thread(_mark_read, service, mid)
            continue

        attachments = extract_attachments(raw)

        # 4) Skip short messages (< 5 words) if there are no attachments
        word_count = len(parsed["text"].split())
        if word_count < 5 and not attachments:
            logger.info("Skipped (< 5 words, no attachments): %s", parsed["from_number"])
            await asyncio.to_thread(_mark_read, service, mid)
            continue

        # If both text and attachments are empty — save for later analysis
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
            "New %s: %s → %s | text=%d chars, attachments=%d",
            parsed["kind"], parsed["from_number"], parsed["to_name"],
            len(parsed["text"]), len(attachments),
        )
        try:
            await on_sms(sms)
        except Exception as exc:
            logger.error("Forwarding error: %s", exc)

        await asyncio.to_thread(_mark_read, service, mid)


async def _send_startup_test_messages(service, on_sms: OnSms) -> None:
    """Fetches the 3 most recent VALID messages and sends them on startup for testing."""
    logger.info("Fetching the latest 3 matching messages for startup test...")
    # Fetch without 'is:unread' to get the absolute latest ones
    query = config.GMAIL_QUERY.replace("is:unread", "").strip()
    resp = await asyncio.to_thread(
        lambda: service.users().messages().list(userId="me", q=query, maxResults=20).execute()
    )
    
    valid_count = 0
    for item in resp.get("messages", []):
        if valid_count >= 3:
            break
            
        mid = item["id"]
        full = await asyncio.to_thread(
            lambda: service.users().messages().get(userId="me", id=mid, format="raw").execute()
        )
        raw = base64.urlsafe_b64decode(full["raw"].encode("utf-8"))
        parsed = parse_message(raw)
        
        if not parsed["is_ringcentral"]:
            continue
        if config.SKIP_NUMBERS and config.normalize_number(parsed["from_number"]) in config.SKIP_NUMBERS:
            continue
        if parsed["kind"] == "voice" and parsed["text"].lower() in ("unavailable", ""):
            continue
            
        attachments = extract_attachments(raw)
        word_count = len(parsed["text"].split())
        if word_count < 5 and not attachments:
            continue

        sms = {
            "kind": parsed["kind"],
            "from_number": parsed["from_number"],
            "to_name": parsed["to_name"],
            "time": parsed["time"],
            "length": parsed["length"],
            "text": parsed["text"],
            "attachments": attachments,
        }
        
        logger.info("Startup Test Sending -> %s: %s", parsed["kind"], parsed["from_number"])
        try:
            await on_sms(sms)
            valid_count += 1
        except Exception as exc:
            logger.error("Startup test forwarding error: %s", exc)
