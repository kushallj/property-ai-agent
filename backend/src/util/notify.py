"""Outbound WhatsApp messages through the WhatsApp Business Cloud API.

Without WHATSAPP_TOKEN / WHATSAPP_PHONE_NUMBER_ID the message is logged instead,
so local runs and tests never send anything.
"""
import logging
import os

import httpx

logger = logging.getLogger(__name__)
sent_log: list[tuple[str, str]] = []  # (to, text), for local runs and tests


def whatsapp_configured() -> bool:
    return bool(os.getenv("WHATSAPP_TOKEN") and os.getenv("WHATSAPP_PHONE_NUMBER_ID"))


def send_whatsapp(to: str, text: str) -> bool:
    sent_log.append((to, text))
    if not whatsapp_configured():
        logger.info("[WhatsApp not configured] to %s: %s", to, text)
        return False
    version = os.getenv("WHATSAPP_API_VERSION", "v21.0")
    url = f"https://graph.facebook.com/{version}/{os.environ['WHATSAPP_PHONE_NUMBER_ID']}/messages"
    payload = {"messaging_product": "whatsapp", "to": to.lstrip("+"), "type": "text",
               "text": {"preview_url": False, "body": text[:4096]}}
    try:
        r = httpx.post(url, json=payload, headers={"Authorization": f"Bearer {os.environ['WHATSAPP_TOKEN']}"}, timeout=15)
        r.raise_for_status()
        return True
    except httpx.HTTPError as exc:
        logger.error("WhatsApp send to %s failed: %s", to, exc)
        return False


def notify_broker(text: str) -> bool:
    broker = os.getenv("BROKER_WHATSAPP")
    if not broker:
        sent_log.append(("broker", text))
        logger.info("[Broker alert, BROKER_WHATSAPP not set] %s", text)
        return False
    return send_whatsapp(broker, text)
