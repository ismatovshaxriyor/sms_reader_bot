"""Configuration: loads and validates settings from the .env file."""

import os
import re

from dotenv import load_dotenv

load_dotenv(override=True)


def _get_required(name: str) -> str:
    """Gets a required variable; raises an error if missing or empty."""
    value = (os.environ.get(name) or "").strip()
    if not value:
        raise RuntimeError(
            f"Required .env variable is missing or empty: {name}. "
            f"Copy from .env.example and fill in the .env file."
        )
    return value


def _parse_admin_ids(raw: str) -> set[int]:
    """Converts a string like \"123, 456\" into the set {123, 456}."""
    ids: set[int] = set()
    for part in (raw or "").replace(" ", "").split(","):
        if not part:
            continue
        try:
            ids.add(int(part))
        except ValueError as exc:
            raise RuntimeError(
                f"ADMIN_IDS has invalid format: '{part}' is not an integer."
            ) from exc
    return ids


def normalize_number(raw: str) -> str:
    """Normalizes a phone number for comparison (last 10 digits)."""
    digits = re.sub(r"\D", "", raw or "")
    return digits[-10:] if len(digits) >= 10 else digits


def _parse_skip_numbers(raw: str) -> set[str]:
    """Converts comma-separated numbers into a normalized set."""
    nums: set[str] = set()
    for part in (raw or "").split(","):
        n = normalize_number(part)
        if n:
            nums.add(n)
    return nums


# ===== Telegram =====
TELEGRAM_BOT_TOKEN: str = _get_required("TELEGRAM_BOT_TOKEN")
ADMIN_IDS: set[int] = _parse_admin_ids(os.environ.get("ADMIN_IDS", ""))

# ===== Gmail (API / OAuth) =====
# OAuth credential and token files are managed in app/gmail_auth.py.

# RingCentral SMS notifications come from this address
RINGCENTRAL_SENDER: str = (
    os.environ.get("RINGCENTRAL_SENDER") or "service@ringcentral.com"
).strip()

# How often to check the mailbox (in seconds)
POLL_INTERVAL: int = int(os.environ.get("POLL_INTERVAL") or "15")

# SMS messages from these numbers will be skipped (normalized, last 10 digits)
SKIP_NUMBERS: set[str] = _parse_skip_numbers(
    os.environ.get("SKIP_NUMBERS") or "(833) 963-2500"
)

# Gmail search query. Finds RingCentral notifications by subject —
# matches both directly received and forwarded (Fwd:) messages.
GMAIL_QUERY: str = (
    os.environ.get("GMAIL_QUERY")
    or 'subject:("New Text Message" OR "New Voice Message" OR "New Voicemail" '
    'OR "New Fax" OR "New MMS") is:unread'
).strip()

# ===== Database =====
DATABASE_PATH: str = (os.environ.get("DATABASE_PATH") or "bot.db").strip()
