"""Scheduled routines: the agent acting without being messaged first, for every broker.

Idea borrowed from OpenMausBot's routines (docs/routine-schedules.md there):
  1. A schedule says WHEN a routine is due ("daily 08:30 IST").
  2. A cheap CHECK in plain code finds the work ("any visits tomorrow?").
     A quiet tick ends here: no AI call, no cost.
  3. Only when the check finds something does the routine ACT (send messages).
  4. A ledger row (routine, period) makes each run happen once, even across restarts.

Every active broker gets their own run, with their own firm name and WhatsApp number.

Routines:
  morning_briefing  daily 08:30 IST          broker's WhatsApp summary of the day
  visit_reminders   hourly 18:00-21:00 IST   remind customers of tomorrow's visits
  weekly_shortlist  Monday 10:00 IST         new matching listings to quiet Warm/Cold leads

Run once by hand:   python -m src.routines            (only what is due now)
                    python -m src.routines --force morning_briefing [--tenant <id>]
The WhatsApp server runs them every minute in the background (ROUTINES_ENABLED=0 turns that off).

Note: outside WhatsApp's 24-hour customer-service window, Meta only delivers
pre-approved template messages. Reminders and shortlists to customers need
approved templates in production; see README_INDIA.md.
"""
from __future__ import annotations

import argparse
import logging
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable, Optional

from src.tenancy import Tenant, active as active_tenants, get as get_tenant
from src.util import db
from src.util.india import fmt_ist, format_inr, now_ist, parse_ist
from src.util.notify import notify_broker, send_whatsapp

logger = logging.getLogger(__name__)


# ---------- schedules: return a period key when due, else None ----------
def daily_at(hour: int, minute: int = 0, window_min: int = 90) -> Callable[[datetime], Optional[str]]:
    def due(now: datetime) -> Optional[str]:
        start = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        return now.strftime("%Y-%m-%d") if start <= now < start + timedelta(minutes=window_min) else None
    return due


def weekly_at(weekday: int, hour: int, minute: int = 0, window_min: int = 180) -> Callable[[datetime], Optional[str]]:
    """weekday: Monday=0."""
    def due(now: datetime) -> Optional[str]:
        if now.weekday() != weekday:
            return None
        start = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if start <= now < start + timedelta(minutes=window_min):
            year, week, _ = now.isocalendar()
            return f"{year}-W{week:02d}"
        return None
    return due


def hourly_between(start_hour: int, end_hour: int) -> Callable[[datetime], Optional[str]]:
    def due(now: datetime) -> Optional[str]:
        return now.strftime("%Y-%m-%d %H") if start_hour <= now.hour < end_hour else None
    return due


@dataclass
class Routine:
    name: str
    due: Callable[[datetime], Optional[str]]
    check: Callable[[datetime, Tenant], list]  # cheap: plain code and SQL only
    act: Callable[[datetime, list, Tenant], str]  # runs only when check found work; returns a summary


# ---------- morning briefing ----------
def _briefing_check(now: datetime, t: Tenant) -> list:
    day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    new = db.leads_created_since((now - timedelta(hours=24)).isoformat(timespec="seconds"), t.id)
    visits = db.appointments_between(day.isoformat(), (day + timedelta(days=1)).isoformat(), t.id)
    hot = [l for l in db.list_leads(t.id) if l["band"] == "Hot"][:3]
    pending = db.pending_approvals(t.id)
    if not (new or visits or hot or pending):
        return []
    return [{"new": new, "visits": visits, "hot": hot, "pending": pending}]


def _briefing_act(now: datetime, items: list, t: Tenant) -> str:
    d = items[0]
    lines = [f"Good morning! Your day at {t.name} ({now.strftime('%a %d %b')})."]
    hot_new = sum(1 for l in d["new"] if l["band"] == "Hot")
    lines.append(f"New leads in the last 24 hours: {len(d['new'])} ({hot_new} hot).")
    if d["hot"]:
        lines.append("Call these first:")
        for l in d["hot"]:
            where = ", ".join(x for x in [l.get("locality"), l.get("city")] if x)
            lines.append(f"• {l.get('name') or 'Unknown'} {l.get('phone') or ''}: {l.get('bhk') or '?'}BHK {where}, "
                         f"up to {format_inr(l.get('max_price'))}. Score {l.get('score')}.")
    if d["visits"]:
        lines.append("Visits today:")
        for a in d["visits"]:
            listing = db.get_listing(a["listing_id"], t.id) or {}
            lines.append(f"• {parse_ist(a['start_ist']).strftime('%I:%M %p').lstrip('0')} · {a['customer_name']} · "
                         f"{listing.get('project', a['listing_id'])}{' (video)' if a['mode'] == 'video' else ''}")
    if d["pending"]:
        lines.append(f"{len(d['pending'])} visit request(s) waiting for your approval: "
                     + ", ".join(f"YES {p['code']}" for p in d["pending"][:3]))
    notify_broker("\n".join(lines), t)
    return f"briefing sent ({len(d['visits'])} visits, {len(d['hot'])} hot leads)"


# ---------- visit reminders ----------
def _reminder_check(now: datetime, t: Tenant) -> list:
    tomorrow = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return [a for a in db.appointments_to_remind(tomorrow.isoformat(), (tomorrow + timedelta(days=1)).isoformat(), t.id)
            if a.get("customer_phone")]


def _reminder_act(now: datetime, items: list, t: Tenant) -> str:
    sent = 0
    for a in items:
        listing = db.get_listing(a["listing_id"], t.id) or {}
        when = fmt_ist(parse_ist(a["start_ist"]))
        first = a["customer_name"].split()[0] if a.get("customer_name") else "there"
        if a["mode"] == "video":
            text = (f"Hi {first}, a reminder from {t.assistant_name} at {t.name}: your video "
                    f"walkthrough of {listing.get('project', 'the property')} is tomorrow, {when}. "
                    "I'll send the call link 30 minutes before. Reply 1 to confirm or 2 to reschedule.")
        else:
            text = (f"Hi {first}, a reminder from {t.assistant_name} at {t.name}: your site visit "
                    f"to {listing.get('project', 'the property')}, {listing.get('locality', '')} is tomorrow, {when}. "
                    "Reply 1 to confirm or 2 to reschedule.")
        send_whatsapp(a["customer_phone"], text, t)
        db.mark_reminded(a["id"])
        sent += 1
    return f"{sent} reminder(s) sent"


# ---------- weekly shortlist for quiet leads ----------
def _shortlist_check(now: datetime, t: Tenant) -> list:
    work = []
    candidates = db.nurture_candidates(quiet_since=(now - timedelta(days=7)).isoformat(timespec="seconds"),
                                       nurtured_before=(now - timedelta(days=6)).isoformat(timespec="seconds"), tenant_id=t.id)
    for lead in candidates:
        already = set(filter(None, (lead.get("shortlist_sent") or "").split(",")))
        criteria = {k: lead.get(k) for k in ("city", "locality", "bhk", "min_price", "max_price")}
        matches, note = db.search_listings(criteria, tenant_id=t.id)
        fresh = [m for m in matches if m["listing_id"] not in already and "above" not in note]
        if fresh:  # only message people we have something new for
            work.append({"lead": lead, "listings": fresh[:2]})
    return work


def _shortlist_act(now: datetime, items: list, t: Tenant) -> str:
    for item in items:
        lead, listings = item["lead"], item["listings"]
        first = (lead.get("name") or "there").split()[0]
        lines = "\n".join(f"• {m['project']}, {m['locality']}: {m['bhk']}BHK, {format_inr(m['price_inr'])}, "
                          f"{'ready to move' if m['ready_to_move'] else 'possession ' + m['possession']}" for m in listings)
        send_whatsapp(lead["phone"], f"Hi {first}, {t.assistant_name} from {t.name} here. New this week for you:\n"
                                     f"{lines}\nWant photos or a visit? Just reply here.", t)
        db.mark_nurtured(lead["thread_id"], [m["listing_id"] for m in listings])
    return f"{len(items)} shortlist(s) sent"


ROUTINES = [
    Routine("morning_briefing", daily_at(8, 30), _briefing_check, _briefing_act),
    Routine("visit_reminders", hourly_between(18, 21), _reminder_check, _reminder_act),
    Routine("weekly_shortlist", weekly_at(0, 10), _shortlist_check, _shortlist_act),
]


def run_due(now: Optional[datetime] = None, force: Optional[str] = None, tenant: Optional[str] = None) -> list[dict]:
    """Run every routine that is due and hasn't run for this period, for every active broker (or just one).

    Safe to call every minute. Ledger periods are '<tenant>|<period>', except the default broker's,
    which keep the plain period so single-broker installs keep the same history.
    """
    now = now or now_ist()
    tenants = [get_tenant(tenant)] if tenant else active_tenants()
    results = []
    for t in [t for t in tenants if t]:
        for r in ROUTINES:
            period = (now.strftime("%Y-%m-%d %H:%M") + " forced") if force == r.name else r.due(now)
            if not period:
                continue
            key = period if t.is_default else f"{t.id}|{period}"
            if db.routine_ran(r.name, key):
                continue
            try:
                items = r.check(now, t)
                summary = r.act(now, items, t) if items else "nothing to do"
            except Exception as exc:  # one broken routine must not stop the others; retried next tick
                logger.exception("Routine %s for %s failed", r.name, t.id)
                results.append({"tenant": t.id, "routine": r.name, "period": period, "error": str(exc)})
                continue
            db.record_routine(r.name, key, len(items), summary)
            results.append({"tenant": t.id, "routine": r.name, "period": period, "items": len(items), "summary": summary})
    return results


def start_background(interval_s: int = 60) -> threading.Event:
    """Tick every minute in a daemon thread. Returns an Event; set it to stop."""
    stop = threading.Event()

    def loop() -> None:
        while not stop.is_set():
            try:
                for res in run_due():
                    logger.info("Routine %s", res)
            except Exception:
                logger.exception("Routine tick failed")
            stop.wait(interval_s)

    threading.Thread(target=loop, name="routines", daemon=True).start()
    return stop


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    p = argparse.ArgumentParser(description="Run routines that are due now")
    p.add_argument("--force", choices=[r.name for r in ROUTINES], help="run this routine now, ignoring its schedule")
    p.add_argument("--tenant", help="only this broker (default: every active broker)")
    args = p.parse_args()
    for res in run_due(force=args.force, tenant=args.tenant) or [{"result": "nothing due right now"}]:
        print(res)
