"""Gmail API uchun bir martalik OAuth avtorizatsiya.

Ishga tushiring (brauzer ochiladi, Google hisobingizga kirib ruxsat bering):

    python -m app.authorize

Natijada token fayli (token.json) yaratiladi. Uni serverga ko'chirib qo'ying.
"""

import os

from google_auth_oauthlib.flow import InstalledAppFlow

from app import gmail_auth


def main() -> None:
    cred = gmail_auth.credentials_file()
    if not os.path.exists(cred):
        raise SystemExit(
            f"❌ OAuth client fayli topilmadi: {cred}\n"
            "Google Cloud Console'dan OAuth client (Desktop app) yaratib, "
            "client_secret_*.json faylini loyiha papkasiga qo'ying "
            "(yoki GMAIL_CREDENTIALS_FILE'da yo'lini ko'rsating)."
        )

    flow = InstalledAppFlow.from_client_secrets_file(cred, gmail_auth.SCOPES)
    creds = flow.run_local_server(port=0)
    gmail_auth.save_credentials(creds)
    print(f"✅ Avtorizatsiya muvaffaqiyatli. Token saqlandi: {gmail_auth.TOKEN_FILE}")


if __name__ == "__main__":
    main()
