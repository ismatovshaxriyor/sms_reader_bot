"""Gmail API OAuth hisob ma'lumotlari bilan ishlash.

Bu modul config'ga bog'liq emas (Telegram sozlamalarisiz ham ishlaydi),
shuning uchun avtorizatsiya skripti (app/authorize.py) ham undan foydalanadi.
"""

import glob
import os

from dotenv import load_dotenv
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build

load_dotenv()

# Xatlarni o'qish va "o'qilgan" deb belgilash uchun gmail.modify kerak
SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]

TOKEN_FILE = (os.environ.get("GMAIL_TOKEN_FILE") or "token.json").strip()

# Brauzersiz (Telegram orqali) avtorizatsiya uchun loopback redirect.
# Foydalanuvchi ruxsat bergach, brauzer http://localhost/?code=... ga o'tadi
# (sahifa ochilmaydi) va kod o'sha manzildan olinadi.
REDIRECT_URI = "http://localhost"


def credentials_file() -> str:
    """OAuth client (client_secret) faylini topadi.

    Tartib: GMAIL_CREDENTIALS_FILE env -> credentials.json -> client_secret*.json
    """
    explicit = (os.environ.get("GMAIL_CREDENTIALS_FILE") or "").strip()
    if explicit:
        return explicit
    if os.path.exists("credentials.json"):
        return "credentials.json"
    matches = sorted(glob.glob("client_secret*.json"))
    return matches[0] if matches else "credentials.json"


def save_credentials(creds: Credentials) -> None:
    with open(TOKEN_FILE, "w", encoding="utf-8") as f:
        f.write(creds.to_json())


def load_credentials() -> Credentials:
    """token.json'dan credential yuklaydi, kerak bo'lsa yangilaydi."""
    if not os.path.exists(TOKEN_FILE):
        raise RuntimeError(
            f"Gmail token topilmadi: {TOKEN_FILE}. "
            "Avval avtorizatsiya qiling:  python -m app.authorize"
        )
    creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
    if creds.valid:
        return creds
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        save_credentials(creds)
        return creds
    raise RuntimeError(
        f"Gmail token yaroqsiz: {TOKEN_FILE}. "
        "Qayta avtorizatsiya qiling:  python -m app.authorize"
    )


def build_service():
    """Gmail API xizmat (service) obyektini qaytaradi."""
    return build("gmail", "v1", credentials=load_credentials(), cache_discovery=False)


def has_valid_token() -> bool:
    """token.json mavjud va yaroqli (yoki yangilab bo'ladigan) bo'lsa True."""
    if not os.path.exists(TOKEN_FILE):
        return False
    try:
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
    except Exception:
        return False
    if creds.valid:
        return True
    if creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            save_credentials(creds)
            return True
        except Exception:
            return False
    return False


# ===== Telegram orqali OAuth (brauzersiz server uchun) =====

def create_auth_flow() -> Flow:
    """OAuth Flow yaratadi (loopback redirect bilan)."""
    return Flow.from_client_secrets_file(
        credentials_file(), scopes=SCOPES, redirect_uri=REDIRECT_URI
    )


def authorization_url(flow: Flow) -> str:
    """Foydalanuvchi ruxsat berishi uchun havola qaytaradi."""
    url, _ = flow.authorization_url(
        access_type="offline", prompt="consent", include_granted_scopes="true"
    )
    return url


def finish_auth(flow: Flow, code: str) -> None:
    """Foydalanuvchi bergan kodni token'ga almashtirib, saqlaydi."""
    flow.fetch_token(code=code)
    save_credentials(flow.credentials)
