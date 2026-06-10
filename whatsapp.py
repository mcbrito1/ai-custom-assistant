import os
import logging
import httpx

log = logging.getLogger(__name__)

WAHA_URL = os.getenv("WAHA_URL", "http://host.docker.internal:3000")
WAHA_API_KEY = os.getenv("WAHA_API_KEY", "")
WAHA_SESSION = os.getenv("WAHA_SESSION", "default")
WAHA_MY_NUMBER = os.getenv("WAHA_MY_NUMBER", "")  # e.g. 5511999999999


def _chat_id(number: str) -> str:
    """Normalize number to WhatsApp chatId format."""
    number = number.strip().replace("+", "").replace(" ", "").replace("-", "")
    if not number.endswith("@c.us"):
        number = f"{number}@c.us"
    return number


def send_text(to: str, text: str) -> dict:
    chat_id = _chat_id(to)
    url = f"{WAHA_URL}/api/sendText"
    payload = {"session": WAHA_SESSION, "chatId": chat_id, "text": text}
    headers = {"X-Api-Key": WAHA_API_KEY, "Content-Type": "application/json"}

    try:
        resp = httpx.post(url, json=payload, headers=headers, verify=False, timeout=15)
        resp.raise_for_status()
        log.info(f"WhatsApp sent to {chat_id}: {text[:60]}")
        return {"ok": True, "chat_id": chat_id}
    except Exception as e:
        log.error(f"WhatsApp send failed: {e}")
        return {"ok": False, "error": str(e)}


def send_to_owner(text: str) -> dict:
    """Send a message to the bot owner's WhatsApp."""
    if not WAHA_MY_NUMBER:
        return {"ok": False, "error": "WAHA_MY_NUMBER not configured"}
    return send_text(WAHA_MY_NUMBER, text)


def check_status() -> dict:
    url = f"{WAHA_URL}/api/sessions"
    headers = {"X-Api-Key": WAHA_API_KEY}
    try:
        resp = httpx.get(url, headers=headers, verify=False, timeout=10)
        return {"ok": True, "sessions": resp.json()}
    except Exception as e:
        return {"ok": False, "error": str(e)}
