"""SQLite storage, shared by every broker (tenant) on one server.

Each broker's inventory, leads, appointments, approvals, campaigns and opt-outs carry a
tenant_id, and every function here takes tenant_id (default "default", the broker that
existed before multi-tenancy) so one broker can never read another broker's rows.

One file, path from DB_PATH (default backend/data/real_estate_data.db).
`python -m src.util.db` reloads the default broker's listings from data/india_listings.csv.
"""
from __future__ import annotations

import csv
import io
import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path
from typing import Any, Iterator, Optional

from src.util.india import now_ist

BACKEND_DIR = Path(__file__).resolve().parents[2]
DEFAULT_DB = BACKEND_DIR / "data" / "real_estate_data.db"
DEFAULT_CSV = BACKEND_DIR / "data" / "india_listings.csv"
DEFAULT_TENANT = "default"

LISTING_COLUMNS = ["listing_id", "project", "builder", "locality", "city", "state", "bhk", "carpet_sqft", "price_inr",
                   "ready_to_move", "possession", "rera_id", "amenities", "highlight"]

SCHEMA = """
CREATE TABLE IF NOT EXISTS tenants (
    id TEXT PRIMARY KEY, name TEXT, assistant_name TEXT, broker_whatsapp TEXT, whatsapp_phone_number_id TEXT,
    whatsapp_token TEXT, whatsapp_business_account_id TEXT, whatsapp_app_secret TEXT, daily_limit INTEGER,
    ai_monthly_limit INTEGER, campaign_hours TEXT, status TEXT, api_key_hash TEXT, notes TEXT, created_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_tenants_key ON tenants (api_key_hash);
CREATE TABLE IF NOT EXISTS inventory (
    tenant_id TEXT, listing_id TEXT, project TEXT, builder TEXT, locality TEXT, city TEXT, state TEXT,
    bhk INTEGER, carpet_sqft INTEGER, price_inr INTEGER, ready_to_move INTEGER, possession TEXT,
    rera_id TEXT, amenities TEXT, highlight TEXT, PRIMARY KEY (tenant_id, listing_id)
);
CREATE TABLE IF NOT EXISTS leads (
    thread_id TEXT PRIMARY KEY, name TEXT, phone TEXT, source TEXT, city TEXT, locality TEXT, bhk INTEGER,
    min_price INTEGER, max_price INTEGER, timeline TEXT, financing TEXT, purpose TEXT, nri INTEGER,
    language TEXT, score INTEGER, band TEXT, score_detail TEXT, broker_alerted INTEGER DEFAULT 0,
    created_at TEXT, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS appointments (
    id INTEGER PRIMARY KEY AUTOINCREMENT, thread_id TEXT, customer_name TEXT, customer_phone TEXT,
    listing_id TEXT, start_ist TEXT, duration_min INTEGER, mode TEXT, status TEXT, notes TEXT,
    google_event_id TEXT, created_at TEXT
);
CREATE TABLE IF NOT EXISTS approvals (
    code TEXT PRIMARY KEY, thread_id TEXT, summary TEXT, status TEXT, created_at TEXT, decided_at TEXT
);
CREATE TABLE IF NOT EXISTS processed_messages (
    message_id TEXT PRIMARY KEY, received_at TEXT
);
CREATE TABLE IF NOT EXISTS routine_runs (
    routine TEXT, period TEXT, ran_at TEXT, items INTEGER, summary TEXT, PRIMARY KEY (routine, period)
);
CREATE TABLE IF NOT EXISTS campaigns (
    id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, template_name TEXT, language TEXT, params_json TEXT,
    header_image_url TEXT, status TEXT, note TEXT, source_file TEXT, created_at TEXT, started_at TEXT, finished_at TEXT
);
CREATE TABLE IF NOT EXISTS campaign_recipients (
    id INTEGER PRIMARY KEY AUTOINCREMENT, campaign_id INTEGER, phone TEXT, name TEXT, data_json TEXT,
    params_json TEXT, status TEXT, wamid TEXT, error TEXT, attempts INTEGER DEFAULT 0, next_attempt_at TEXT,
    sent_at TEXT, delivered_at TEXT, read_at TEXT, replied_at TEXT, UNIQUE (campaign_id, phone)
);
CREATE INDEX IF NOT EXISTS idx_recipients_wamid ON campaign_recipients (wamid);
CREATE INDEX IF NOT EXISTS idx_recipients_queue ON campaign_recipients (campaign_id, status);
CREATE TABLE IF NOT EXISTS tenant_opt_outs (
    tenant_id TEXT, phone TEXT, reason TEXT, created_at TEXT, PRIMARY KEY (tenant_id, phone)
);
CREATE TABLE IF NOT EXISTS usage (
    tenant_id TEXT, month TEXT, ai_turns INTEGER DEFAULT 0, template_messages INTEGER DEFAULT 0,
    PRIMARY KEY (tenant_id, month)
);
"""

# Columns added after the first release. Older databases get them on first connect;
# rows that existed before multi-tenancy belong to the "default" broker.
MIGRATIONS = {
    "leads": {"last_nurture_at": "TEXT", "shortlist_sent": "TEXT DEFAULT ''", "tenant_id": "TEXT DEFAULT 'default'"},
    "appointments": {"reminded_at": "TEXT", "tenant_id": "TEXT DEFAULT 'default'"},
    "approvals": {"tenant_id": "TEXT DEFAULT 'default'"},
    "campaigns": {"tenant_id": "TEXT DEFAULT 'default'"},
}


def _migrate(conn: sqlite3.Connection) -> None:
    for table, cols in MIGRATIONS.items():
        have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        for col, decl in cols.items():
            if col not in have:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    # Pre-multi-tenancy tables: copy their rows to the default broker once, then rename them out of the way.
    if "opt_outs" in tables:
        conn.execute("INSERT OR IGNORE INTO tenant_opt_outs SELECT 'default', phone, reason, created_at FROM opt_outs")
        conn.execute("ALTER TABLE opt_outs RENAME TO opt_outs_premigration")
    if "listings" in tables:
        conn.execute(f"INSERT OR IGNORE INTO inventory SELECT 'default', {', '.join(LISTING_COLUMNS)} FROM listings")
        conn.execute("ALTER TABLE listings RENAME TO listings_premigration")


def db_path() -> str:
    return os.getenv("DB_PATH") or str(DEFAULT_DB)


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(db_path())
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(SCHEMA)
        _migrate(conn)
        yield conn
        conn.commit()
    finally:
        conn.close()


def _now() -> str:
    return now_ist().isoformat(timespec="seconds")


# ---------- inventory ----------
def _read_listing_rows(text: str) -> list[dict]:
    reader = csv.DictReader(io.StringIO(text.lstrip("﻿")))
    missing = [c for c in ("listing_id", "project", "locality", "city", "bhk", "price_inr") if c not in (reader.fieldnames or [])]
    if missing:
        raise ValueError(f"The inventory CSV is missing columns: {', '.join(missing)}. "
                         f"Use the same columns as data/india_listings.csv.")
    rows = []
    for r in reader:
        if not (r.get("listing_id") or "").strip():
            continue
        row = {c: (r.get(c) or "").strip() for c in LISTING_COLUMNS}
        row["ready_to_move"] = 1 if str(row["ready_to_move"]).lower() in ("1", "true", "yes", "ready", "ready to move") else 0
        rows.append(row)
    return rows


def replace_listings(tenant_id: str, csv_text: str) -> int:
    """Replace a broker's whole inventory with the rows in a CSV (same columns as data/india_listings.csv)."""
    rows = _read_listing_rows(csv_text)
    with connect() as conn:
        conn.execute("DELETE FROM inventory WHERE tenant_id = ?", (tenant_id,))
        conn.executemany(
            f"INSERT INTO inventory (tenant_id, {', '.join(LISTING_COLUMNS)}) VALUES (?, {', '.join('?' * len(LISTING_COLUMNS))})",
            [(tenant_id, *[r[c] for c in LISTING_COLUMNS]) for r in rows])
    return len(rows)


def load_listings(csv_path: Optional[str] = None, tenant_id: str = DEFAULT_TENANT) -> int:
    return replace_listings(tenant_id, Path(csv_path or DEFAULT_CSV).read_text(encoding="utf-8"))


def ensure_listings(tenant_id: str = DEFAULT_TENANT) -> None:
    """The default broker starts with the sample listings so a fresh server can be demoed."""
    if tenant_id != DEFAULT_TENANT:
        return
    with connect() as conn:
        count = conn.execute("SELECT COUNT(*) FROM inventory WHERE tenant_id = ?", (tenant_id,)).fetchone()[0]
    if count == 0:
        load_listings(tenant_id=tenant_id)


def list_listings(tenant_id: str = DEFAULT_TENANT) -> list[dict]:
    with connect() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM inventory WHERE tenant_id = ? ORDER BY city, locality, price_inr", (tenant_id,))]


def search_listings(criteria: dict, limit: int = 3, tenant_id: str = DEFAULT_TENANT) -> tuple[list[dict], str]:
    """Return (matches, note). Widens from locality to city, then to nearest prices above budget."""
    ensure_listings(tenant_id)
    base = "SELECT * FROM inventory WHERE tenant_id = ?"
    params: list[Any] = [tenant_id]
    if criteria.get("city"):
        base += " AND lower(city) = lower(?)"
        params.append(criteria["city"])
    if criteria.get("bhk"):
        base += " AND bhk = ?"
        params.append(int(criteria["bhk"]))
    if criteria.get("ready_to_move") is True:
        base += " AND ready_to_move = 1"

    max_price = criteria.get("max_price")
    min_price = criteria.get("min_price")
    target = ((min_price or 0) + max_price) / 2 if max_price else None

    def run(extra: str, extra_params: list, order: str) -> list[dict]:
        with connect() as conn:
            rows = conn.execute(f"{base}{extra} ORDER BY {order} LIMIT ?", params + extra_params + [limit]).fetchall()
        return [dict(r) for r in rows]

    budget, budget_params = "", []
    if max_price:
        budget = " AND price_inr <= ?"
        budget_params = [int(max_price * 1.05)]  # allow a 5% stretch
    order = f"ABS(price_inr - {int(target)})" if target else "price_inr"

    locality = (criteria.get("locality") or "").strip()
    if locality:
        rows = run(budget + " AND lower(locality) LIKE ?", budget_params + [f"%{locality.lower()}%"], order)
        if rows:
            return rows, f"in {locality}"
    rows = run(budget, budget_params, order)
    if rows:
        return rows, (f"nothing in {locality} right now, these are elsewhere in {criteria.get('city')}" if locality else "")
    if max_price:
        rows = run(" AND price_inr > ?", [int(max_price)], "price_inr")
        if rows:
            return rows, "nothing within budget, these are the closest above it"
    return [], "no listings match"


def get_listing(listing_id: str, tenant_id: str = DEFAULT_TENANT) -> Optional[dict]:
    ensure_listings(tenant_id)
    with connect() as conn:
        row = conn.execute("SELECT * FROM inventory WHERE tenant_id = ? AND listing_id = ?", (tenant_id, listing_id)).fetchone()
    return dict(row) if row else None


# ---------- leads ----------
def upsert_lead(thread_id: str, lead: dict, criteria: dict, score: dict, tenant_id: str = DEFAULT_TENANT) -> dict:
    values = {
        "thread_id": thread_id, "tenant_id": tenant_id, "name": lead.get("name"), "phone": lead.get("phone"),
        "source": lead.get("source"), "city": criteria.get("city"), "locality": criteria.get("locality"),
        "bhk": criteria.get("bhk"), "min_price": criteria.get("min_price"), "max_price": criteria.get("max_price"),
        "timeline": lead.get("timeline"), "financing": lead.get("financing"), "purpose": lead.get("purpose"),
        "nri": int(bool(lead.get("nri"))), "language": lead.get("language"),
        "score": score.get("total"), "band": score.get("band"), "score_detail": json.dumps(score.get("factors", [])),
        "now": _now(),
    }
    with connect() as conn:
        conn.execute(
            """INSERT INTO leads (thread_id,tenant_id,name,phone,source,city,locality,bhk,min_price,max_price,timeline,financing,
                   purpose,nri,language,score,band,score_detail,created_at,updated_at)
               VALUES (:thread_id,:tenant_id,:name,:phone,:source,:city,:locality,:bhk,:min_price,:max_price,:timeline,:financing,
                   :purpose,:nri,:language,:score,:band,:score_detail,:now,:now)
               ON CONFLICT(thread_id) DO UPDATE SET name=coalesce(excluded.name,name), phone=coalesce(excluded.phone,phone),
                   source=coalesce(excluded.source,source), city=excluded.city, locality=excluded.locality, bhk=excluded.bhk,
                   min_price=excluded.min_price, max_price=excluded.max_price, timeline=excluded.timeline,
                   financing=excluded.financing, purpose=excluded.purpose, nri=excluded.nri, language=excluded.language,
                   score=excluded.score, band=excluded.band, score_detail=excluded.score_detail, updated_at=excluded.updated_at""",
            values,
        )
        row = conn.execute("SELECT * FROM leads WHERE thread_id = ?", (thread_id,)).fetchone()
    return dict(row)


def mark_broker_alerted(thread_id: str) -> None:
    with connect() as conn:
        conn.execute("UPDATE leads SET broker_alerted = 1 WHERE thread_id = ?", (thread_id,))


def list_leads(tenant_id: str = DEFAULT_TENANT) -> list[dict]:
    with connect() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM leads WHERE tenant_id = ? ORDER BY score DESC, updated_at DESC", (tenant_id,))]


def leads_created_since(iso: str, tenant_id: str = DEFAULT_TENANT) -> list[dict]:
    with connect() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM leads WHERE tenant_id = ? AND created_at >= ? ORDER BY score DESC", (tenant_id, iso))]


def nurture_candidates(quiet_since: str, nurtured_before: str, tenant_id: str = DEFAULT_TENANT) -> list[dict]:
    """Warm/Cold leads with a number, quiet since `quiet_since`, not nurtured since `nurtured_before`, not opted out."""
    with connect() as conn:
        return [dict(r) for r in conn.execute(
            """SELECT * FROM leads WHERE tenant_id = ? AND band IN ('Warm','Cold') AND phone IS NOT NULL AND city IS NOT NULL
               AND updated_at < ? AND (last_nurture_at IS NULL OR last_nurture_at < ?)
               AND REPLACE(phone, '+', '') NOT IN (SELECT phone FROM tenant_opt_outs WHERE tenant_id = ?) ORDER BY score DESC""",
            (tenant_id, quiet_since, nurtured_before, tenant_id))]


def mark_nurtured(thread_id: str, listing_ids: list[str]) -> None:
    with connect() as conn:
        row = conn.execute("SELECT shortlist_sent FROM leads WHERE thread_id = ?", (thread_id,)).fetchone()
        sent = [x for x in ((row["shortlist_sent"] if row else "") or "").split(",") if x]
        conn.execute("UPDATE leads SET last_nurture_at = ?, shortlist_sent = ? WHERE thread_id = ?",
                     (_now(), ",".join(sent + listing_ids), thread_id))


# ---------- appointments ----------
def add_appointment(**fields: Any) -> int:
    fields.setdefault("status", "confirmed")
    fields.setdefault("tenant_id", DEFAULT_TENANT)
    fields["created_at"] = _now()
    cols = ",".join(fields)
    marks = ",".join(f":{k}" for k in fields)
    with connect() as conn:
        cur = conn.execute(f"INSERT INTO appointments ({cols}) VALUES ({marks})", fields)
        return int(cur.lastrowid)


def update_appointment(appointment_id: int, **fields: Any) -> None:
    sets = ",".join(f"{k} = :{k}" for k in fields)
    with connect() as conn:
        conn.execute(f"UPDATE appointments SET {sets} WHERE id = :id", {**fields, "id": appointment_id})


def get_appointment(appointment_id: int, tenant_id: Optional[str] = None) -> Optional[dict]:
    with connect() as conn:
        row = conn.execute("SELECT * FROM appointments WHERE id = ?", (appointment_id,)).fetchone()
    if not row or (tenant_id and row["tenant_id"] != tenant_id):
        return None
    return dict(row)


def appointments_between(start_iso: str, end_iso: str, tenant_id: str = DEFAULT_TENANT) -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM appointments WHERE tenant_id = ? AND status = 'confirmed' AND start_ist >= ? AND start_ist < ? ORDER BY start_ist",
            (tenant_id, start_iso, end_iso),
        ).fetchall()
    return [dict(r) for r in rows]


def appointments_for(phone: Optional[str] = None, thread_id: Optional[str] = None, tenant_id: Optional[str] = None) -> list[dict]:
    q, p = "SELECT * FROM appointments WHERE status = 'confirmed'", []
    for col, val in (("customer_phone", phone), ("thread_id", thread_id), ("tenant_id", tenant_id)):
        if val:
            q += f" AND {col} = ?"
            p.append(val)
    with connect() as conn:
        return [dict(r) for r in conn.execute(q + " ORDER BY start_ist", p)]


def list_appointments(tenant_id: str = DEFAULT_TENANT) -> list[dict]:
    with connect() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM appointments WHERE tenant_id = ? ORDER BY start_ist", (tenant_id,))]


def appointments_to_remind(start_iso: str, end_iso: str, tenant_id: str = DEFAULT_TENANT) -> list[dict]:
    with connect() as conn:
        return [dict(r) for r in conn.execute(
            """SELECT * FROM appointments WHERE tenant_id = ? AND status = 'confirmed' AND reminded_at IS NULL
               AND start_ist >= ? AND start_ist < ? ORDER BY start_ist""", (tenant_id, start_iso, end_iso))]


def mark_reminded(appointment_id: int) -> None:
    with connect() as conn:
        conn.execute("UPDATE appointments SET reminded_at = ? WHERE id = ?", (_now(), appointment_id))


# ---------- approvals ----------
def add_approval(code: str, thread_id: str, summary: str, tenant_id: str = DEFAULT_TENANT) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO approvals (code, thread_id, summary, status, created_at, decided_at, tenant_id) VALUES (?,?,?,?,?,NULL,?)",
            (code, thread_id, summary, "pending", _now(), tenant_id))


def get_approval(code: str, tenant_id: Optional[str] = None) -> Optional[dict]:
    with connect() as conn:
        row = conn.execute("SELECT * FROM approvals WHERE upper(code) = upper(?)", (code,)).fetchone()
    if not row or (tenant_id and row["tenant_id"] != tenant_id):
        return None
    return dict(row)


def approval_code_taken(code: str) -> bool:
    return get_approval(code) is not None


def decide_approval(code: str, status: str) -> None:
    with connect() as conn:
        conn.execute("UPDATE approvals SET status = ?, decided_at = ? WHERE upper(code) = upper(?)", (status, _now(), code))


def list_approvals(tenant_id: str = DEFAULT_TENANT) -> list[dict]:
    with connect() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM approvals WHERE tenant_id = ? ORDER BY created_at DESC", (tenant_id,))]


def pending_approvals(tenant_id: str = DEFAULT_TENANT) -> list[dict]:
    with connect() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM approvals WHERE tenant_id = ? AND status = 'pending' ORDER BY created_at", (tenant_id,))]


# ---------- duplicate-safe webhooks ----------
def claim_message(message_id: str) -> bool:
    """True the first time a WhatsApp message id is seen, False on any repeat delivery.

    Meta retries a webhook when it doesn't get a fast 200, so the same message can
    arrive twice. INSERT OR IGNORE makes the check and the claim one atomic step.
    """
    now = now_ist()
    with connect() as conn:
        cur = conn.execute("INSERT OR IGNORE INTO processed_messages VALUES (?, ?)", (message_id, now.isoformat(timespec="seconds")))
        claimed = cur.rowcount == 1
        if claimed and now.minute == 0:  # tidy up now and then; Meta retries for hours, not weeks
            conn.execute("DELETE FROM processed_messages WHERE received_at < ?",
                         ((now - timedelta(days=7)).isoformat(timespec="seconds"),))
    return claimed


# ---------- routines ----------
def routine_ran(routine: str, period: str) -> bool:
    with connect() as conn:
        return conn.execute("SELECT 1 FROM routine_runs WHERE routine = ? AND period = ?", (routine, period)).fetchone() is not None


def record_routine(routine: str, period: str, items: int, summary: str) -> None:
    with connect() as conn:
        conn.execute("INSERT OR REPLACE INTO routine_runs VALUES (?,?,?,?,?)", (routine, period, _now(), items, summary))


def routine_history(tenant_id: str = DEFAULT_TENANT, limit: int = 50) -> list[dict]:
    """Runs for one broker. The default broker's periods have no prefix; others are '<tenant>|<period>'."""
    with connect() as conn:
        if tenant_id == DEFAULT_TENANT:
            rows = conn.execute("SELECT * FROM routine_runs WHERE period NOT LIKE '%|%' ORDER BY ran_at DESC LIMIT ?", (limit,))
        else:
            rows = conn.execute("SELECT * FROM routine_runs WHERE period LIKE ? ORDER BY ran_at DESC LIMIT ?", (f"{tenant_id}|%", limit))
        return [dict(r) for r in rows]


# ---------- usage ----------
def add_usage(tenant_id: str, field: str, n: int = 1) -> None:
    if field not in ("ai_turns", "template_messages"):
        raise ValueError(field)
    month = now_ist().strftime("%Y-%m")
    with connect() as conn:
        conn.execute("INSERT OR IGNORE INTO usage (tenant_id, month) VALUES (?, ?)", (tenant_id, month))
        conn.execute(f"UPDATE usage SET {field} = {field} + ? WHERE tenant_id = ? AND month = ?", (n, tenant_id, month))


def get_usage(tenant_id: str, month: Optional[str] = None) -> dict:
    month = month or now_ist().strftime("%Y-%m")
    with connect() as conn:
        row = conn.execute("SELECT * FROM usage WHERE tenant_id = ? AND month = ?", (tenant_id, month)).fetchone()
    return dict(row) if row else {"tenant_id": tenant_id, "month": month, "ai_turns": 0, "template_messages": 0}


if __name__ == "__main__":
    n = load_listings()
    print(f"Loaded {n} listings for the default broker into {db_path()}")
