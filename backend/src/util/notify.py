"""Outbound WhatsApp messages through the WhatsApp Business Cloud API.

Without WHATSAPP_TOKEN / WHATSAPP_PHONE_NUMBER_ID the message is logged instead,
so local runs and tests never send anything.
"""
import logging
import os
import re
import uuid

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


def _graph_url(path: str) -> str:
    return f"https://graph.facebook.com/{os.getenv('WHATSAPP_API_VERSION', 'v21.0')}/{path}"


def send_template(to: str, template_name: str, language: str, body_params: list[str],
                  header_image_url: str | None = None) -> tuple[bool, str | None, int | None, str]:
    """Send an approved WhatsApp template. Returns (ok, message_id, error_code, error_text).

    Business-initiated messages (campaigns) must use templates approved in WhatsApp Manager.
    Without WhatsApp credentials this simulates a successful send so campaigns can be tried locally.
    """
    sent_log.append((to, f"[template {template_name}] " + " | ".join(body_params)))
    if not whatsapp_configured():
        return True, f"sim-{uuid.uuid4().hex[:16]}", None, ""
    components = []
    if header_image_url:
        components.append({"type": "header", "parameters": [{"type": "image", "image": {"link": header_image_url}}]})
    if body_params:
        components.append({"type": "body", "parameters": [{"type": "text", "text": p} for p in body_params]})
    payload = {"messaging_product": "whatsapp", "to": to.lstrip("+"), "type": "template",
               "template": {"name": template_name, "language": {"code": language}, "components": components}}
    try:
        r = httpx.post(_graph_url(f"{os.environ['WHATSAPP_PHONE_NUMBER_ID']}/messages"), json=payload,
                       headers={"Authorization": f"Bearer {os.environ['WHATSAPP_TOKEN']}"}, timeout=20)
    except httpx.HTTPError as exc:
        return False, None, 1, f"Network error: {exc}"  # code 1 is treated as temporary and retried
    data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    if r.status_code == 200 and data.get("messages"):
        return True, data["messages"][0]["id"], None, ""
    err = data.get("error", {})
    return False, None, err.get("code") or r.status_code, err.get("error_data", {}).get("details") or err.get("message") or r.text[:300]


def list_templates() -> list[dict]:
    """Approved templates from WhatsApp Manager, with how many {{n}} variables each body needs."""
    waba = os.getenv("WHATSAPP_BUSINESS_ACCOUNT_ID")
    if not (waba and os.getenv("WHATSAPP_TOKEN")):
        return []
    r = httpx.get(_graph_url(f"{waba}/message_templates"), params={"fields": "name,language,status,category,components", "limit": 200},
                  headers={"Authorization": f"Bearer {os.environ['WHATSAPP_TOKEN']}"}, timeout=20)
    r.raise_for_status()
    out = []
    for t in r.json().get("data", []):
        body = next((c.get("text", "") for c in t.get("components", []) if c.get("type") == "BODY"), "")
        header = next((c.get("format") for c in t.get("components", []) if c.get("type") == "HEADER"), None)
        out.append({"name": t["name"], "language": t["language"], "status": t["status"], "category": t.get("category"),
                    "body": body, "variables": len(set(re.findall(r"\{\{(\d+)\}\}", body))), "header": header})
    return out


def notify_broker(text: str) -> bool:
    broker = os.getenv("BROKER_WHATSAPP")
    if not broker:
        sent_log.append(("broker", text))
        logger.info("[Broker alert, BROKER_WHATSAPP not set] %s", text)
        return False
    return send_whatsapp(broker, text)
