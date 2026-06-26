"""Working with Gmail API OAuth credentials.

This module does not depend on config (works without Telegram settings),
so the authorization script (app/authorize.py) also uses it.
"""

import glob
import os

from dotenv import load_dotenv
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build

load_dotenv()

# gmail.modify is needed to read messages and mark them as "read"
SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]

TOKEN_FILE = (os.environ.get("GMAIL_TOKEN_FILE") or "token.json").strip()

# Loopback redirect for browserless (via Telegram) authorization.
# After the user grants permission, the browser redirects to http://localhost/?code=...
# (the page won't open) and the code is extracted from that URL.
REDIRECT_URI = "http://localhost"


def credentials_file() -> str:
    """Finds the OAuth client (client_secret) file.

    Order: GMAIL_CREDENTIALS_FILE env -> credentials.json -> client_secret*.json
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
    """Loads credentials from token.json, refreshing if needed."""
    if not os.path.exists(TOKEN_FILE):
        raise RuntimeError(
            f"Gmail token not found: {TOKEN_FILE}. "
            "Please authorize first:  python -m app.authorize"
        )
    creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
    if creds.valid:
        return creds
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        save_credentials(creds)
        return creds
    raise RuntimeError(
        f"Gmail token is invalid: {TOKEN_FILE}. "
        "Please re-authorize:  python -m app.authorize"
    )


def build_service():
    """Returns a Gmail API service object."""
    return build("gmail", "v1", credentials=load_credentials(), cache_discovery=False)


def has_valid_token() -> bool:
    """Returns True if token.json exists and is valid (or can be refreshed)."""
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


# ===== OAuth via Telegram (for browserless server) =====

def create_auth_flow() -> Flow:
    """Creates an OAuth Flow (with loopback redirect)."""
    return Flow.from_client_secrets_file(
        credentials_file(), scopes=SCOPES, redirect_uri=REDIRECT_URI
    )


def authorization_url(flow: Flow) -> str:
    """Returns a URL for the user to grant permission."""
    url, _ = flow.authorization_url(
        access_type="offline", prompt="consent", include_granted_scopes="true"
    )
    return url


def finish_auth(flow: Flow, code: str) -> None:
    """Exchanges the user-provided code for a token and saves it."""
    flow.fetch_token(code=code)
    save_credentials(flow.credentials)
