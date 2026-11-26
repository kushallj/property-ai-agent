"""SQLite storage: property listings, leads (mini CRM), appointments, broker approvals.

One file, path from DB_PATH (default backend/data/real_estate_data.db).
Run `python -m src.util.db` to (re)load listings from data/india_listings.csv,
or point the CSV at your client's own inventory export with the same columns.
"""
from __future__ import annotations

import csv
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

SCHEMA = """
CREATE TABLE IF NOT EXISTS listings (
    listing_id TEXT PRIMARY KEY, project TEXT, builder TEXT, locality TEXT, city TEXT, state TEXT,
    bhk INTEGER, carpet_sqft INTEGER, price_inr INTEGER, ready_to_move INTEGER, possession TEXT,
    rera_id TEXT, amenities TEXT, highlight TEXT
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
"""

# Columns added after the first release. Older databases get them on first connect.
MIGRATIONS = {
    "leads": {"last_nurture_at": "TEXT", "shortlist_sent": "TEXT DEFAULT ''"},
    "appointments": {"reminded_at": "TEXT"},
}


def _migrate(conn: sqlite3.Connection) -> None:
    for table, cols in MIGRATIONS.items():
        have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        for col, decl in cols.items():
            if col not in have:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")


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


def load_listings(csv_path: Optional[str] = None) -> int:
    path = Path(csv_path or DEFAULT_CSV)
    with open(path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    with connect() as conn:
        conn.execute("DELETE FROM listings")
        conn.executemany(
            """INSERT INTO listings VALUES (:listing_id,:project,:builder,:locality,:city,:state,:bhk,
               :carpet_sqft,:price_inr,:ready_to_move,:possession,:rera_id,:amenities,:highlight)""",
            rows,
        )
    return len(rows)


def ensure_listings() -> None:
    with connect() as conn:
        count = conn.execute("SELECT COUNT(*) FROM listings").fetchone()[0]
    if count == 0:
        load_listings()


# ---------- listings ----------
def search_listings(criteria: dict, limit: int = 3) -> tuple[list[dict], str]:
    """Return (matches, note). Widens from locality to city, then to nearest prices above budget."""
    ensure_listings()
    base = "SELECT * FROM listings WHERE 1=1"
    params: list[Any] = []
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

    budget = ""
    budget_params: list[Any] = []
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


def get_listing(listing_id: str) -> Optional[dict]:
    ensure_listings()
    with connect() as conn:
        row = conn.execute("SELECT * FROM listings WHERE listing_id = ?", (listing_id,)).fetchone()
    return dict(row) if row else None


# ---------- leads ----------
def upsert_lead(thread_id: str, lead: dict, criteria: dict, score: dict) -> dict:
    now = now_ist().isoformat(timespec="seconds")
    values = {
        "thread_id": thread_id, "name": lead.get("name"), "phone": lead.get("phone"), "source": lead.get("source"),
        "city": criteria.get("city"), "locality": criteria.get("locality"), "bhk": criteria.get("bhk"),
        "min_price": criteria.get("min_price"), "max_price": criteria.get("max_price"),
        "timeline": lead.get("timeline"), "financing": lead.get("financing"), "purpose": lead.get("purpose"),
        "nri": int(bool(lead.get("nri"))), "language": lead.get("language"),
        "score": score.get("total"), "band": score.get("band"), "score_detail": json.dumps(score.get("factors", [])),
        "now": now,
    }
    with connect() as conn:
        conn.execute(
            """INSERT INTO leads (thread_id,name,phone,source,city,locality,bhk,min_price,max_price,timeline,financing,
                   purpose,nri,language,score,band,score_detail,created_at,updated_at)
               VALUES (:thread_id,:name,:phone,:source,:city,:locality,:bhk,:min_price,:max_price,:timeline,:financing,
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


def list_leads() -> list[dict]:
    with connect() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM leads ORDER BY score DESC, updated_at DESC")]


# ---------- appointments ----------
def add_appointment(**fields: Any) -> int:
    fields.setdefault("status", "confirmed")
    fields["created_at"] = now_ist().isoformat(timespec="seconds")
    cols = ",".join(fields)
    marks = ",".join(f":{k}" for k in fields)
    with connect() as conn:
        cur = conn.execute(f"INSERT INTO appointments ({cols}) VALUES ({marks})", fields)
        return int(cur.lastrowid)


def update_appointment(appointment_id: int, **fields: Any) -> None:
    sets = ",".join(f"{k} = :{k}" for k in fields)
    with connect() as conn:
        conn.execute(f"UPDATE appointments SET {sets} WHERE id = :id", {**fields, "id": appointment_id})


def get_appointment(appointment_id: int) -> Optional[dict]:
    with connect() as conn:
        row = conn.execute("SELECT * FROM appointments WHERE id = ?", (appointment_id,)).fetchone()
    return dict(row) if row else None


def appointments_between(start_iso: str, end_iso: str) -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM appointments WHERE status = 'confirmed' AND start_ist >= ? AND start_ist < ? ORDER BY start_ist",
            (start_iso, end_iso),
        ).fetchall()
    return [dict(r) for r in rows]


def appointments_for(phone: Optional[str] = None, thread_id: Optional[str] = None) -> list[dict]:
    q, p = "SELECT * FROM appointments WHERE status = 'confirmed'", []
    if phone:
        q += " AND customer_phone = ?"
        p.append(phone)
    if thread_id:
        q += " AND thread_id = ?"
        p.append(thread_id)
    with connect() as conn:
        return [dict(r) for r in conn.execute(q + " ORDER BY start_ist", p)]


# ---------- approvals ----------
def add_approval(code: str, thread_id: str, summary: str) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO approvals VALUES (?,?,?,?,?,NULL)",
            (code, thread_id, summary, "pending", now_ist().isoformat(timespec="seconds")),
        )


def get_approval(code: str) -> Optional[dict]:
    with connect() as conn:
        row = conn.execute("SELECT * FROM approvals WHERE upper(code) = upper(?)", (code,)).fetchone()
    return dict(row) if row else None


def decide_approval(code: str, status: str) -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE approvals SET status = ?, decided_at = ? WHERE upper(code) = upper(?)",
            (status, now_ist().isoformat(timespec="seconds"), code),
        )


# ---------- duplicate-safe webhooks ----------
def claim_message(message_id: str) -> bool:
    """True the first time a WhatsApp message id is seen, False on any repeat delivery.

    Meta retries a webhook when it doesn't get a fast 200, so the same message can
    arrive twice. INSERT OR IGNORE makes the check and the claim one atomic step.
    """
    now = now_ist()
    with connect() as conn:
        cur = conn.execute("INSERT OR IGNORE INTO processed_messages VALUES (?, ?)",
                           (message_id, now.isoformat(timespec="seconds")))
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
        conn.execute("INSERT OR REPLACE INTO routine_runs VALUES (?,?,?,?,?)",
                     (routine, period, now_ist().isoformat(timespec="seconds"), items, summary))


def routine_history(limit: int = 50) -> list[dict]:
    with connect() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM routine_runs ORDER BY ran_at DESC LIMIT ?", (limit,))]


def leads_created_since(iso: str) -> list[dict]:
    with connect() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM leads WHERE created_at >= ? ORDER BY score DESC", (iso,))]


def pending_approvals() -> list[dict]:
    with connect() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM approvals WHERE status = 'pending' ORDER BY created_at")]


def nurture_candidates(quiet_since: str, nurtured_before: str) -> list[dict]:
    """Warm/Cold leads with a number, quiet since `quiet_since`, not nurtured since `nurtured_before`."""
    with connect() as conn:
        return [dict(r) for r in conn.execute(
            """SELECT * FROM leads WHERE band IN ('Warm','Cold') AND phone IS NOT NULL AND city IS NOT NULL
               AND updated_at < ? AND (last_nurture_at IS NULL OR last_nurture_at < ?) ORDER BY score DESC""",
            (quiet_since, nurtured_before))]


def mark_nurtured(thread_id: str, listing_ids: list[str]) -> None:
    with connect() as conn:
        row = conn.execute("SELECT shortlist_sent FROM leads WHERE thread_id = ?", (thread_id,)).fetchone()
        sent = [x for x in ((row["shortlist_sent"] if row else "") or "").split(",") if x]
        conn.execute("UPDATE leads SET last_nurture_at = ?, shortlist_sent = ? WHERE thread_id = ?",
                     (now_ist().isoformat(timespec="seconds"), ",".join(sent + listing_ids), thread_id))


def appointments_to_remind(start_iso: str, end_iso: str) -> list[dict]:
    with connect() as conn:
        return [dict(r) for r in conn.execute(
            """SELECT * FROM appointments WHERE status = 'confirmed' AND reminded_at IS NULL
               AND start_ist >= ? AND start_ist < ? ORDER BY start_ist""", (start_iso, end_iso))]


def mark_reminded(appointment_id: int) -> None:
    with connect() as conn:
        conn.execute("UPDATE appointments SET reminded_at = ? WHERE id = ?",
                     (now_ist().isoformat(timespec="seconds"), appointment_id))


if __name__ == "__main__":
    n = load_listings()
    print(f"Loaded {n} listings into {db_path()}")
