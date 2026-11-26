"""Outbound WhatsApp messages through the WhatsApp Business Cloud API, per broker.

Each broker sends from their own number (tenant.wa_phone_number_id()). Without a number
and token the message is logged instead, so local runs and tests never send anything.
"""
from __future__ import annotations

import logging
import os
import re
import uuid
from typing import TYPE_CHECKING, Optional

import httpx

if TYPE_CHECKING:
    from src.tenancy import Tenant

logger = logging.getLogger(__name__)
sent_log: list[tuple[str, str]] = []  # (to, text), for local runs and tests


def _tenant(tenant: "Optional[Tenant]") -> "Tenant":
    if tenant is not None:
        return tenant
    from src.tenancy import ensure_default
    return ensure_default()


def whatsapp_configured(tenant: "Optional[Tenant]" = None) -> bool:
    return _tenant(tenant).whatsapp_ready()


def _graph_url(path: str) -> str:
    return f"https://graph.facebook.com/{os.getenv('WHATSAPP_API_VERSION', 'v21.0')}/{path}"


def send_whatsapp(to: str, text: str, tenant: "Optional[Tenant]" = None) -> bool:
    t = _tenant(tenant)
    sent_log.append((to, text))
    if not t.whatsapp_ready():
        logger.info("[WhatsApp not configured for %s] to %s: %s", t.id, to, text)
        return False
    payload = {"messaging_product": "whatsapp", "to": to.lstrip("+"), "type": "text",
               "text": {"preview_url": False, "body": text[:4096]}}
    try:
        r = httpx.post(_graph_url(f"{t.wa_phone_number_id()}/messages"), json=payload,
                       headers={"Authorization": f"Bearer {t.wa_token()}"}, timeout=15)
        r.raise_for_status()
        return True
    except httpx.HTTPError as exc:
        logger.error("WhatsApp send for %s to %s failed: %s", t.id, to, exc)
        return False


def send_template(to: str, template_name: str, language: str, body_params: list[str],
                  header_image_url: str | None = None, tenant: "Optional[Tenant]" = None) -> tuple[bool, str | None, int | None, str]:
    """Send an approved WhatsApp template. Returns (ok, message_id, error_code, error_text).

    Business-initiated messages (campaigns) must use templates approved in WhatsApp Manager.
    Without WhatsApp credentials this simulates a successful send so campaigns can be tried locally.
    """
    t = _tenant(tenant)
    sent_log.append((to, f"[template {template_name}] " + " | ".join(body_params)))
    if not t.whatsapp_ready():
        return True, f"sim-{uuid.uuid4().hex[:16]}", None, ""
    components = []
    if header_image_url:
        components.append({"type": "header", "parameters": [{"type": "image", "image": {"link": header_image_url}}]})
    if body_params:
        components.append({"type": "body", "parameters": [{"type": "text", "text": p} for p in body_params]})
    payload = {"messaging_product": "whatsapp", "to": to.lstrip("+"), "type": "template",
               "template": {"name": template_name, "language": {"code": language}, "components": components}}
    try:
        r = httpx.post(_graph_url(f"{t.wa_phone_number_id()}/messages"), json=payload,
                       headers={"Authorization": f"Bearer {t.wa_token()}"}, timeout=20)
    except httpx.HTTPError as exc:
        return False, None, 1, f"Network error: {exc}"  # code 1 is treated as temporary and retried
    data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    if r.status_code == 200 and data.get("messages"):
        return True, data["messages"][0]["id"], None, ""
    err = data.get("error", {})
    return False, None, err.get("code") or r.status_code, err.get("error_data", {}).get("details") or err.get("message") or r.text[:300]


def list_templates(tenant: "Optional[Tenant]" = None) -> list[dict]:
    """Approved templates from the broker's WhatsApp Manager, with how many {{n}} variables each body needs."""
    t = _tenant(tenant)
    waba = t.wa_business_account_id()
    if not (waba and t.wa_token()):
        return []
    r = httpx.get(_graph_url(f"{waba}/message_templates"), params={"fields": "name,language,status,category,components", "limit": 200},
                  headers={"Authorization": f"Bearer {t.wa_token()}"}, timeout=20)
    r.raise_for_status()
    out = []
    for tpl in r.json().get("data", []):
        body = next((c.get("text", "") for c in tpl.get("components", []) if c.get("type") == "BODY"), "")
        header = next((c.get("format") for c in tpl.get("components", []) if c.get("type") == "HEADER"), None)
        out.append({"name": tpl["name"], "language": tpl["language"], "status": tpl["status"], "category": tpl.get("category"),
                    "body": body, "variables": len(set(re.findall(r"\{\{(\d+)\}\}", body))), "header": header})
    return out


def notify_broker(text: str, tenant: "Optional[Tenant]" = None) -> bool:
    t = _tenant(tenant)
    broker = t.broker_number()
    if not broker:
        sent_log.append(("broker", text))
        logger.info("[Broker alert for %s, no broker number set] %s", t.id, text)
        return False
    return send_whatsapp(broker, text, t)
