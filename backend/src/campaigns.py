"""WhatsApp campaigns: send an approved template to a list of contacts from a CSV.

How it works, start to finish:
  1. CREATE   Upload a CSV (name, phone, any other columns). Numbers are cleaned to international
              format (+91 assumed for 10-digit Indian mobiles), duplicates merged, opted-out people
              skipped, and each person's template variables filled from their columns.
              The campaign starts as a draft so you can preview it and send a test first.
  2. START    A background sender works through the queue:
                - only between CAMPAIGN_HOURS (default 9-21 IST)
                - at most WHATSAPP_DAILY_LIMIT people per day across all campaigns
                  (Meta's messaging limit for your number; new numbers start around 250)
                - at most CAMPAIGN_RATE_PER_SEC messages per second
              Temporary Meta errors (rate limits) are retried with backoff; template or account
              errors pause the campaign with the reason; other failures are recorded per person.
  3. TRACK    Meta's status webhooks move each person from sent -> delivered -> read.
  4. REPLIES  A reply goes to the AI agent like any other WhatsApp message, tagged with the
              campaign as the lead source. "STOP" opts the person out of all future campaigns.

WhatsApp rules this code follows: campaigns use approved templates only; opted-out numbers are
never messaged; sending stops at Meta's daily limit. You are responsible for having each contact's
consent to receive WhatsApp messages from the business.
"""
from __future__ import annotations

import csv
import io
import json
import logging
import os
import re
import threading
import time
from datetime import datetime, timedelta
from typing import Callable, Optional

from src.tenancy import Tenant, get as get_tenant
from src.util import db
from src.util.india import now_ist
from src.util.notify import send_template

DEFAULT = db.DEFAULT_TENANT

logger = logging.getLogger(__name__)

STOP_WORDS = {"stop", "unsubscribe", "stop promotions", "opt out", "optout", "band karo", "no more messages"}
START_WORDS = {"start", "subscribe", "start promotions"}
# Meta Cloud API error codes (https://developers.facebook.com/docs/whatsapp/cloud-api/support/error-codes)
RETRYABLE = {1, 2, 4, 80007, 130429, 131048, 131056}  # network, throttling, rate limits
PAUSE_CAMPAIGN = {10, 190, 200, 131031, 132000, 132001, 132005, 132007, 132012, 132015, 132016}  # auth / template problems
STATUS_RANK = {"queued": 0, "sent": 1, "delivered": 2, "read": 3, "replied": 4}
MAX_ATTEMPTS = 5


def _t(tenant_id: str) -> Tenant:
    return get_tenant(tenant_id) or get_tenant(DEFAULT)


def daily_limit(tenant_id: str = DEFAULT) -> int:
    return _t(tenant_id).limit_per_day()


def rate_per_sec() -> float:
    return max(0.2, float(os.getenv("CAMPAIGN_RATE_PER_SEC", "10")))


def sending_hours(tenant_id: str = DEFAULT) -> tuple[int, int]:
    return _t(tenant_id).hours()


# ---------- contacts ----------
def normalize_phone(raw: str, default_country: str = "91") -> Optional[str]:
    """Return digits in international format without '+', e.g. '919800004521', or None if unusable."""
    raw = str(raw or "").strip()
    digits = re.sub(r"\D", "", raw)
    if raw.startswith("00"):
        digits = digits[2:]
        raw = "+" + digits
    if raw.startswith("+"):
        return digits if 8 <= len(digits) <= 15 else None
    if len(digits) == 11 and digits.startswith("0"):
        digits = digits[1:]
    if len(digits) == 10 and digits[0] in "6789":
        return default_country + digits
    if len(digits) == 12 and digits.startswith("91") and digits[2] in "6789":
        return digits
    return None


def _key(header: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", header.strip().lower()).strip("_")


def parse_contacts(csv_text: str) -> tuple[list[dict], dict]:
    """Read a CSV into rows of {phone_raw, name, data}. Finds the phone and name columns by their headers."""
    text = csv_text.lstrip("﻿")
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    reader = csv.DictReader(io.StringIO(text), dialect=dialect)
    headers = [h for h in (reader.fieldnames or []) if h]
    phone_col = next((h for h in headers if re.search(r"phone|mobile|whatsapp|number|contact", h, re.I)
                      and not re.search(r"date|last|name", h, re.I)), None)
    name_col = next((h for h in headers if re.search(r"name", h, re.I)), None)
    if not phone_col:
        raise ValueError(f"No phone column found. Columns seen: {', '.join(headers) or 'none'}. Name one 'phone' or 'mobile'.")
    rows = []
    for rec in reader:
        data = {_key(k): (v or "").strip() for k, v in rec.items() if k}
        rows.append({"phone_raw": rec.get(phone_col, ""), "name": (rec.get(name_col) or "").strip() if name_col else "", "data": data})
    return rows, {"phone_column": phone_col, "name_column": name_col, "columns": [_key(h) for h in headers]}


_PLACEHOLDER = re.compile(r"\{([a-z0-9_]+)(?:\|([^}]*))?\}")


def render_params(params: list[str], name: str, data: dict) -> list[str]:
    """Fill template variables. '{first_name}', '{locality|your area}' (fallback after |), or fixed text."""
    values = {**data, "name": name or data.get("name", ""), "first_name": (name or data.get("name", "")).split(" ")[0]}
    out = []
    for p in params:
        def sub(m: re.Match) -> str:
            v = values.get(m.group(1), "").strip() or (m.group(2) or "").strip()
            if not v:
                raise ValueError(f"no value for {{{m.group(1)}}}")
            return v
        text = _PLACEHOLDER.sub(sub, p).strip()
        if not text:
            raise ValueError("empty variable")
        out.append(text[:1024])
    return out


# ---------- opt-outs ----------
def is_opted_out(phone: str, tenant_id: str = DEFAULT) -> bool:
    with db.connect() as conn:
        return conn.execute("SELECT 1 FROM tenant_opt_outs WHERE tenant_id = ? AND phone = ?", (tenant_id, phone)).fetchone() is not None


def opt_out(phone: str, reason: str = "replied STOP", tenant_id: str = DEFAULT) -> None:
    now = now_ist().isoformat(timespec="seconds")
    with db.connect() as conn:
        conn.execute("INSERT OR REPLACE INTO tenant_opt_outs VALUES (?,?,?,?)", (tenant_id, phone, reason, now))
        conn.execute("UPDATE campaign_recipients SET status = 'opted_out' WHERE phone = ? AND status = 'queued' "
                     "AND campaign_id IN (SELECT id FROM campaigns WHERE tenant_id = ?)", (phone, tenant_id))


def opt_in(phone: str, tenant_id: str = DEFAULT) -> None:
    with db.connect() as conn:
        conn.execute("DELETE FROM tenant_opt_outs WHERE tenant_id = ? AND phone = ?", (tenant_id, phone))


def list_opt_outs(tenant_id: str = DEFAULT) -> list[dict]:
    with db.connect() as conn:
        return [dict(r) for r in conn.execute("SELECT phone, reason, created_at FROM tenant_opt_outs WHERE tenant_id = ? ORDER BY created_at DESC", (tenant_id,))]


# ---------- campaigns ----------
def create_campaign(name: str, template_name: str, language: str, params: list[str], csv_text: str,
                    header_image_url: Optional[str] = None, source_file: str = "", tenant_id: str = DEFAULT) -> dict:
    rows, meta = parse_contacts(csv_text)
    now = now_ist().isoformat(timespec="seconds")
    seen, recipients = set(), []
    counts = {"rows": len(rows), "queued": 0, "invalid_phone": 0, "duplicates": 0, "opted_out": 0, "missing_values": 0}
    problems = []
    for i, row in enumerate(rows, start=2):  # row 1 is the header
        phone = normalize_phone(row["phone_raw"])
        if not phone:
            counts["invalid_phone"] += 1
            problems.append(f"Row {i}: '{row['phone_raw']}' is not a valid mobile number")
            continue
        if phone in seen:
            counts["duplicates"] += 1
            continue
        seen.add(phone)
        status, error, rendered = "queued", None, []
        if is_opted_out(phone, tenant_id):
            status, error = "opted_out", "opted out earlier"
            counts["opted_out"] += 1
        else:
            try:
                rendered = render_params(params, row["name"], row["data"])
                counts["queued"] += 1
            except ValueError as exc:
                status, error = "skipped", str(exc)
                counts["missing_values"] += 1
                problems.append(f"Row {i} ({row['name'] or phone}): {exc}")
        recipients.append((phone, row["name"], json.dumps(row["data"], ensure_ascii=False), json.dumps(rendered, ensure_ascii=False), status, error))

    with db.connect() as conn:
        cur = conn.execute(
            "INSERT INTO campaigns (name, template_name, language, params_json, header_image_url, status, note, source_file, created_at, tenant_id) "
            "VALUES (?,?,?,?,?,'draft','',?,?,?)",
            (name, template_name, language, json.dumps(params, ensure_ascii=False), header_image_url, source_file, now, tenant_id))
        cid = int(cur.lastrowid)
        conn.executemany(
            "INSERT INTO campaign_recipients (campaign_id, phone, name, data_json, params_json, status, error) VALUES (?,?,?,?,?,?,?)",
            [(cid, *r) for r in recipients])
    samples = [{"phone": "+" + r[0], "name": r[1], "variables": json.loads(r[3])} for r in recipients if r[4] == "queued"][:3]
    return {"id": cid, "counts": counts, "columns": meta, "samples": samples, "problems": problems[:20]}


def get_campaign(cid: int, tenant_id: Optional[str] = None) -> Optional[dict]:
    """A campaign by id; with tenant_id, only if it belongs to that broker."""
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM campaigns WHERE id = ?", (cid,)).fetchone()
    if not row or (tenant_id and row["tenant_id"] != tenant_id):
        return None
    return dict(row)


def stats(cid: int) -> dict:
    with db.connect() as conn:
        counts = {r["status"]: r["n"] for r in conn.execute(
            "SELECT status, COUNT(*) AS n FROM campaign_recipients WHERE campaign_id = ? GROUP BY status", (cid,))}
    reached = {s: sum(n for st, n in counts.items() if STATUS_RANK.get(st, -1) >= STATUS_RANK[s]) for s in ("sent", "delivered", "read", "replied")}
    return {"total": sum(counts.values()), "by_status": counts, "queued": counts.get("queued", 0),
            "sent": reached["sent"], "delivered": reached["delivered"], "read": reached["read"], "replied": reached["replied"],
            "failed": counts.get("failed", 0), "opted_out": counts.get("opted_out", 0), "skipped": counts.get("skipped", 0)}


def list_campaigns(tenant_id: str = DEFAULT) -> list[dict]:
    with db.connect() as conn:
        rows = [dict(r) for r in conn.execute("SELECT * FROM campaigns WHERE tenant_id = ? ORDER BY id DESC", (tenant_id,))]
    for r in rows:
        r["stats"] = stats(r["id"])
    return rows


def recipients(cid: int, status: Optional[str] = None, limit: int = 2000) -> list[dict]:
    q, p = "SELECT * FROM campaign_recipients WHERE campaign_id = ?", [cid]
    if status:
        q += " AND status = ?"
        p.append(status)
    with db.connect() as conn:
        return [dict(r) for r in conn.execute(q + " ORDER BY id LIMIT ?", p + [limit])]


def set_status(cid: int, status: str, note: str = "", tenant_id: Optional[str] = None) -> dict:
    c = get_campaign(cid, tenant_id)
    if not c:
        raise ValueError(f"No campaign {cid}")
    allowed = {"running": {"draft", "paused"}, "paused": {"running"}, "cancelled": {"draft", "running", "paused"}}
    if c["status"] not in allowed.get(status, set()):
        raise ValueError(f"Can't change a {c['status']} campaign to {status}")
    now = now_ist().isoformat(timespec="seconds")
    with db.connect() as conn:
        conn.execute("UPDATE campaigns SET status = ?, note = ?, started_at = coalesce(started_at, ?), "
                     "finished_at = CASE WHEN ? = 'cancelled' THEN ? ELSE finished_at END WHERE id = ?",
                     (status, note, now if status == "running" else None, status, now, cid))
    return get_campaign(cid)


def send_test(cid: int, phone_raw: str, sender: Callable = send_template, tenant_id: Optional[str] = None) -> dict:
    """Send the campaign's template to one number (yours) using the first queued person's variables."""
    c = get_campaign(cid, tenant_id)
    phone = normalize_phone(phone_raw)
    if not c or not phone:
        raise ValueError("Unknown campaign or invalid phone number")
    first = recipients(cid, "queued", limit=1)
    params = json.loads(first[0]["params_json"]) if first else []
    if sender is send_template:
        ok, wamid, code, err = send_template(phone, c["template_name"], c["language"], params, c["header_image_url"], tenant=_t(c["tenant_id"]))
    else:
        ok, wamid, code, err = sender(phone, c["template_name"], c["language"], params, c["header_image_url"])
    return {"ok": ok, "message_id": wamid, "error_code": code, "error": err, "variables": params}


# ---------- the sender ----------
def _sent_today(now: datetime, tenant_id: str) -> int:
    """People messaged today from this broker's number, across all of their campaigns."""
    day = now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    with db.connect() as conn:
        return conn.execute("SELECT COUNT(*) FROM campaign_recipients WHERE sent_at >= ? AND campaign_id IN "
                            "(SELECT id FROM campaigns WHERE tenant_id = ?)", (day, tenant_id)).fetchone()[0]


def _note(cid: int, note: str) -> None:
    with db.connect() as conn:
        conn.execute("UPDATE campaigns SET note = ? WHERE id = ?", (note, cid))


def tick(now: Optional[datetime] = None, sender: Callable = send_template, sleep: Callable[[float], None] = time.sleep,
         batch: int = 50) -> int:
    """Send the next batch for running campaigns of every broker. Returns how many went out. Safe to call often.

    Sending hours and the daily limit belong to each broker's own WhatsApp number.
    """
    now = now or now_ist()
    with db.connect() as conn:
        running = [dict(r) for r in conn.execute("SELECT * FROM campaigns WHERE status = 'running' ORDER BY started_at, id")]
    by_tenant: dict[str, list[dict]] = {}
    for c in running:
        by_tenant.setdefault(c["tenant_id"] or DEFAULT, []).append(c)
    total = 0
    for tenant_id, camps in by_tenant.items():
        total += _tick_tenant(_t(tenant_id), camps, now, sender, sleep, batch)
    return total


def _tick_tenant(t: Tenant, running: list[dict], now: datetime, sender: Callable, sleep: Callable, batch: int) -> int:
    start_h, end_h = t.hours()
    if not (start_h <= now.hour < end_h):
        for c in running:
            _note(c["id"], f"Waiting for sending hours ({start_h}:00 to {end_h}:00 IST)")
        return 0
    remaining = t.limit_per_day() - _sent_today(now, t.id)
    if remaining <= 0:
        for c in running:
            _note(c["id"], f"Daily limit of {t.limit_per_day()} reached; continues tomorrow at {start_h}:00")
        return 0
    send = (lambda *a: send_template(*a, tenant=t)) if sender is send_template else sender

    sent_total, pause_s = 0, 1.0 / rate_per_sec()
    for c in running:
        with db.connect() as conn:
            queue = [dict(r) for r in conn.execute(
                "SELECT * FROM campaign_recipients WHERE campaign_id = ? AND status = 'queued' "
                "AND (next_attempt_at IS NULL OR next_attempt_at <= ?) ORDER BY id LIMIT ?",
                (c["id"], now.isoformat(timespec="seconds"), min(batch, remaining)))]
            waiting = conn.execute("SELECT COUNT(*) FROM campaign_recipients WHERE campaign_id = ? AND status = 'queued'",
                                   (c["id"],)).fetchone()[0]
        if waiting == 0:
            with db.connect() as conn:
                conn.execute("UPDATE campaigns SET status = 'completed', note = '', finished_at = ? WHERE id = ?",
                             (now.isoformat(timespec="seconds"), c["id"]))
            continue
        _note(c["id"], "")
        for r in queue:
            if is_opted_out(r["phone"], t.id):
                _update(r["id"], status="opted_out")
                continue
            ok, wamid, code, err = send(r["phone"], c["template_name"], c["language"], json.loads(r["params_json"]), c["header_image_url"])
            attempts = (r["attempts"] or 0) + 1
            if ok:
                _update(r["id"], status="sent", wamid=wamid, sent_at=now.isoformat(timespec="seconds"), attempts=attempts, error=None)
                db.add_usage(t.id, "template_messages")
                sent_total += 1
                remaining -= 1
            elif code in PAUSE_CAMPAIGN:
                _update(r["id"], attempts=attempts, error=f"{code}: {err}")
                set_status(c["id"], "paused", f"Paused by Meta error {code}: {err}. Fix the template or token, then resume.")
                break
            elif code in RETRYABLE and attempts < MAX_ATTEMPTS:
                retry_at = now + timedelta(minutes=2 ** attempts)
                _update(r["id"], attempts=attempts, error=f"{code}: {err} (retrying)", next_attempt_at=retry_at.isoformat(timespec="seconds"))
                break  # Meta is throttling: stop this batch and let the next tick continue
            else:
                _update(r["id"], status="failed", attempts=attempts, error=f"{code}: {err}")
            if remaining <= 0:
                break
            sleep(pause_s)
        if remaining <= 0:
            break
    return sent_total


def _update(rid: int, **fields) -> None:
    sets = ", ".join(f"{k} = :{k}" for k in fields)
    with db.connect() as conn:
        conn.execute(f"UPDATE campaign_recipients SET {sets} WHERE id = :id", {**fields, "id": rid})


def start_worker(interval_s: float = 5) -> threading.Event:
    stop = threading.Event()

    def loop() -> None:
        while not stop.is_set():
            try:
                tick()
            except Exception:
                logger.exception("Campaign tick failed")
            stop.wait(interval_s)

    threading.Thread(target=loop, name="campaigns", daemon=True).start()
    return stop


# ---------- webhooks ----------
def apply_status(status: dict) -> None:
    """Handle one entry of Meta's 'statuses' webhook: sent / delivered / read / failed."""
    wamid, st = status.get("id"), status.get("status")
    if not wamid or st not in ("sent", "delivered", "read", "failed"):
        return
    with db.connect() as conn:
        row = conn.execute("SELECT id, status FROM campaign_recipients WHERE wamid = ?", (wamid,)).fetchone()
    if not row:
        return
    now = now_ist().isoformat(timespec="seconds")
    if st == "failed":
        if STATUS_RANK.get(row["status"], 0) < STATUS_RANK["delivered"]:
            e = (status.get("errors") or [{}])[0]
            _update(row["id"], status="failed", error=f"{e.get('code')}: {e.get('title') or e.get('message', '')}")
        return
    if STATUS_RANK[st] > STATUS_RANK.get(row["status"], -1):
        _update(row["id"], status=st, **({"delivered_at": now} if st == "delivered" else {"read_at": now} if st == "read" else {}))


def on_inbound(phone: str, text: str, tenant_id: str = DEFAULT) -> dict:
    """Check an incoming message to one broker for STOP/START and campaign replies. phone: digits, e.g. '919800004521'."""
    t = re.sub(r"[^a-z ]", "", (text or "").lower()).strip()
    if t in STOP_WORDS:
        opt_out(phone, tenant_id=tenant_id)
        return {"action": "opted_out"}
    if t in START_WORDS and is_opted_out(phone, tenant_id):
        opt_in(phone, tenant_id)
        return {"action": "opted_in"}
    since = (now_ist() - timedelta(days=7)).isoformat(timespec="seconds")
    with db.connect() as conn:
        row = conn.execute(
            "SELECT r.id, r.status, c.name FROM campaign_recipients r JOIN campaigns c ON c.id = r.campaign_id "
            "WHERE r.phone = ? AND c.tenant_id = ? AND r.sent_at >= ? AND r.status IN ('sent','delivered','read','replied') "
            "ORDER BY r.sent_at DESC LIMIT 1", (phone, tenant_id, since)).fetchone()
    if not row:
        return {"action": None}
    if row["status"] != "replied":
        _update(row["id"], status="replied", replied_at=now_ist().isoformat(timespec="seconds"))
    return {"action": "campaign_reply", "campaign": row["name"]}


def export_csv(cid: int) -> str:
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["phone", "name", "status", "error", "sent_at", "delivered_at", "read_at", "replied_at"])
    for r in recipients(cid, limit=1_000_000):
        w.writerow(["+" + r["phone"], r["name"], r["status"], r["error"] or "", r["sent_at"] or "", r["delivered_at"] or "", r["read_at"] or "", r["replied_at"] or ""])
    return out.getvalue()
