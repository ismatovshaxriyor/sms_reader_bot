"""MsgPlane API client: fetches the agent name assigned to a given order ID."""

import asyncio
import json
import ssl
import urllib.parse
import urllib.request
from typing import Optional

_SSL_CTX = ssl.create_default_context()
_SSL_CTX.check_hostname = False
_SSL_CTX.verify_mode = ssl.CERT_NONE


def _fetch_sync(api_key: str, order_id: str, api_url: str) -> Optional[str]:
    payload = urllib.parse.urlencode(
        {"subaction": "get", "api_key": api_key, "record": order_id}
    ).encode()
    req = urllib.request.Request(api_url, data=payload, method="POST")
    with urllib.request.urlopen(req, context=_SSL_CTX, timeout=10) as resp:
        data = json.loads(resp.read())
        if data.get("result") == "success":
            return data.get("user_name")
    return None


async def get_agent_name(api_key: str, order_id: str, api_url: str) -> Optional[str]:
    """Returns the MsgPlane agent name for the given order ID, or None on failure."""
    loop = asyncio.get_event_loop()
    try:
        return await loop.run_in_executor(None, _fetch_sync, api_key, order_id, api_url)
    except Exception:
        return None
