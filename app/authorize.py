"""One-time OAuth authorization for the Gmail API.

Run this (a browser will open — sign in to your Google account and grant permission):

    python -m app.authorize

A token file (token.json) will be created. Copy it to the server.
"""

import os

from google_auth_oauthlib.flow import InstalledAppFlow

from app import gmail_auth


def main() -> None:
    cred = gmail_auth.credentials_file()
    if not os.path.exists(cred):
        raise SystemExit(
            f"❌ OAuth client file not found: {cred}\n"
            "Create an OAuth client (Desktop app) from the Google Cloud Console, "
            "place the client_secret_*.json file in the project directory "
            "(or specify its path in GMAIL_CREDENTIALS_FILE)."
        )

    flow = InstalledAppFlow.from_client_secrets_file(cred, gmail_auth.SCOPES)
    creds = flow.run_local_server(port=0)
    gmail_auth.save_credentials(creds)
    print(f"✅ Authorization successful. Token saved: {gmail_auth.TOKEN_FILE}")


if __name__ == "__main__":
    main()
