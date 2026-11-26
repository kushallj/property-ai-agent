"""Brokers (tenants) sharing one server.

Each broker has their own firm name, assistant name, WhatsApp number, inventory, leads,
campaigns and an API key that only ever sees their data. The platform owner manages
brokers with ADMIN_API_KEY (the platform key).

The "default" broker is the one configured through environment variables (FIRM_NAME,
WHATSAPP_*, BROKER_WHATSAPP ...), so a single-broker install keeps working unchanged.
"""
from __future__ import annotations

import hashlib
import os
import re
import secrets
from dataclasses import asdict, dataclass, field
from typing import Optional

from src.util import db
from src.util.india import now_ist

DEFAULT = db.DEFAULT_TENANT
EDITABLE = ("name", "assistant_name", "broker_whatsapp", "whatsapp_phone_number_id", "whatsapp_token",
            "whatsapp_business_account_id", "whatsapp_app_secret", "daily_limit", "ai_monthly_limit",
            "campaign_hours", "status", "notes")
SECRETS = ("whatsapp_token", "whatsapp_app_secret")


@dataclass
class Tenant:
    id: str
    name: str
    assistant_name: str = "Riya"
    broker_whatsapp: Optional[str] = None
    whatsapp_phone_number_id: Optional[str] = None
    whatsapp_token: Optional[str] = field(default=None, repr=False)
    whatsapp_business_account_id: Optional[str] = None
    whatsapp_app_secret: Optional[str] = field(default=None, repr=False)
    daily_limit: Optional[int] = None
    ai_monthly_limit: Optional[int] = None
    campaign_hours: Optional[str] = None
    status: str = "active"
    notes: Optional[str] = None
    created_at: Optional[str] = None

    @property
    def is_default(self) -> bool:
        return self.id == DEFAULT

    # The default broker falls back to environment variables; other brokers only use their own number.
    def wa_phone_number_id(self) -> Optional[str]:
        return self.whatsapp_phone_number_id or (os.getenv("WHATSAPP_PHONE_NUMBER_ID") if self.is_default else None)

    def wa_token(self) -> Optional[str]:
        # One Meta app (as a Tech Provider) can send for many numbers with one token, so others may share it.
        return self.whatsapp_token or os.getenv("WHATSAPP_TOKEN")

    def wa_business_account_id(self) -> Optional[str]:
        return self.whatsapp_business_account_id or (os.getenv("WHATSAPP_BUSINESS_ACCOUNT_ID") if self.is_default else None)

    def broker_number(self) -> Optional[str]:
        return self.broker_whatsapp or (os.getenv("BROKER_WHATSAPP") if self.is_default else None)

    def whatsapp_ready(self) -> bool:
        return bool(self.wa_phone_number_id() and self.wa_token())

    def limit_per_day(self) -> int:
        return int(self.daily_limit or os.getenv("WHATSAPP_DAILY_LIMIT", "250"))

    def hours(self) -> tuple[int, int]:
        start, end = (self.campaign_hours or os.getenv("CAMPAIGN_HOURS", "9-21")).split("-")
        return int(start), int(end)

    def public(self) -> dict:
        d = {k: v for k, v in asdict(self).items() if k not in SECRETS}
        d["whatsapp_connected"] = self.whatsapp_ready()
        d["has_own_token"] = bool(self.whatsapp_token)
        return d


def _hash(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def _from_row(row) -> Tenant:
    data = {k: row[k] for k in row.keys() if k != "api_key_hash"}
    return Tenant(**data)


def ensure_default() -> Tenant:
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM tenants WHERE id = ?", (DEFAULT,)).fetchone()
        if not row:
            conn.execute("INSERT INTO tenants (id, name, assistant_name, status, created_at) VALUES (?,?,?,?,?)",
                         (DEFAULT, os.getenv("FIRM_NAME", "our real estate team"), os.getenv("ASSISTANT_NAME", "Riya"),
                          "active", now_ist().isoformat(timespec="seconds")))
            row = conn.execute("SELECT * FROM tenants WHERE id = ?", (DEFAULT,)).fetchone()
    return _from_row(row)


def get(tenant_id: str) -> Optional[Tenant]:
    if tenant_id == DEFAULT:
        return ensure_default()
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM tenants WHERE id = ?", (tenant_id,)).fetchone()
    return _from_row(row) if row else None


def from_config(config) -> Tenant:
    """The broker a graph run belongs to (config['configurable']['tenant'])."""
    tid = ((config or {}).get("configurable") or {}).get("tenant") or DEFAULT
    return get(tid) or ensure_default()


def by_api_key(key: str) -> Optional[Tenant]:
    if not key:
        return None
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM tenants WHERE api_key_hash = ?", (_hash(key),)).fetchone()
    return _from_row(row) if row else None


def by_phone_number_id(phone_number_id: Optional[str]) -> Optional[Tenant]:
    """Which broker a WhatsApp message was sent to (Meta includes the business number's id)."""
    if phone_number_id:
        with db.connect() as conn:
            row = conn.execute("SELECT * FROM tenants WHERE whatsapp_phone_number_id = ?", (phone_number_id,)).fetchone()
        if row:
            return _from_row(row)
        if os.getenv("WHATSAPP_PHONE_NUMBER_ID") == phone_number_id:
            return ensure_default()
        return None
    return ensure_default()  # payloads without a number id (tests, older setups) go to the default broker


def list_all() -> list[Tenant]:
    ensure_default()
    with db.connect() as conn:
        return [_from_row(r) for r in conn.execute("SELECT * FROM tenants ORDER BY id != 'default', created_at, id")]


def active() -> list[Tenant]:
    return [t for t in list_all() if t.status == "active"]


def webhook_secrets() -> list[str]:
    found = [os.getenv("WHATSAPP_APP_SECRET")] + [t.whatsapp_app_secret for t in list_all()]
    return [s for s in found if s]


def any_whatsapp_live() -> bool:
    return bool(os.getenv("WHATSAPP_TOKEN")) or any(t.whatsapp_token for t in list_all())


def _slug(name: str) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:30] or "broker"
    slug, n = base, 2
    while get(slug) or slug == DEFAULT:
        slug, n = f"{base}-{n}", n + 1
    return slug


def new_key(tenant_id: str) -> str:
    return f"bm_{tenant_id}_{secrets.token_urlsafe(24)}"


def create(name: str, tenant_id: Optional[str] = None, **fields) -> tuple[Tenant, str]:
    """Add a broker. Returns the broker and their API key (shown once; only its hash is stored)."""
    if not name.strip():
        raise ValueError("A firm name is required")
    tid = tenant_id or _slug(name)
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,39}", tid) or get(tid):
        raise ValueError(f"'{tid}' is taken or not a valid id (lowercase letters, digits and dashes)")
    pid = fields.get("whatsapp_phone_number_id")
    if pid and by_phone_number_id(pid) and by_phone_number_id(pid).id != tid and os.getenv("WHATSAPP_PHONE_NUMBER_ID") != pid:
        raise ValueError("That WhatsApp number is already connected to another broker")
    key = new_key(tid)
    values = {k: fields.get(k) for k in EDITABLE if k not in ("name", "status")}
    with db.connect() as conn:
        conn.execute(
            f"INSERT INTO tenants (id, name, status, api_key_hash, created_at, {', '.join(values)}) "
            f"VALUES (?, ?, 'active', ?, ?, {', '.join('?' * len(values))})",
            (tid, name.strip(), _hash(key), now_ist().isoformat(timespec="seconds"), *values.values()))
    return get(tid), key


def update(tenant_id: str, **fields) -> Tenant:
    t = get(tenant_id)
    if not t:
        raise KeyError(tenant_id)
    changes = {k: v for k, v in fields.items() if k in EDITABLE and v is not None}
    if changes.get("status") and changes["status"] not in ("active", "paused"):
        raise ValueError("status must be active or paused")
    pid = changes.get("whatsapp_phone_number_id")
    if pid:
        other = by_phone_number_id(pid)
        if other and other.id != tenant_id:
            raise ValueError("That WhatsApp number is already connected to another broker")
    if changes:
        with db.connect() as conn:
            conn.execute(f"UPDATE tenants SET {', '.join(f'{k} = ?' for k in changes)} WHERE id = ?", (*changes.values(), tenant_id))
    return get(tenant_id)


def rotate_key(tenant_id: str) -> str:
    if not get(tenant_id):
        raise KeyError(tenant_id)
    key = new_key(tenant_id)
    with db.connect() as conn:
        conn.execute("UPDATE tenants SET api_key_hash = ? WHERE id = ?", (_hash(key), tenant_id))
    return key


def summary(t: Tenant) -> dict:
    """Profile plus this month's usage and counts, for the platform owner and the broker's own dashboard."""
    with db.connect() as conn:
        leads = conn.execute("SELECT COUNT(*) FROM leads WHERE tenant_id = ?", (t.id,)).fetchone()[0]
        listings = conn.execute("SELECT COUNT(*) FROM inventory WHERE tenant_id = ?", (t.id,)).fetchone()[0]
        camps = conn.execute("SELECT COUNT(*) FROM campaigns WHERE tenant_id = ?", (t.id,)).fetchone()[0]
    usage = db.get_usage(t.id)
    return {**t.public(), "leads": leads, "listings": listings, "campaigns": camps,
            "usage_this_month": {"ai_turns": usage["ai_turns"], "template_messages": usage["template_messages"]},
            "ai_limit_reached": bool(t.ai_monthly_limit) and usage["ai_turns"] >= t.ai_monthly_limit}
