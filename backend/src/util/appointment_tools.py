"""Site visit and video walkthrough tools, in IST, for the broker the conversation belongs to.

Safe tools run straight away. Sensitive tools (book, reschedule, cancel) pause the
graph until the broker approves (interrupt_before in graph.py).
"""
from datetime import datetime, timedelta
from typing import Annotated, Literal

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool
from langgraph.prebuilt import InjectedState

from src.util import db, google_calendar
from src.util.india import IST, fmt_ist, now_ist, parse_ist

OPEN_HOUR, CLOSE_HOUR, VISIT_MINUTES = 10, 19, 45


def _thread_id(config: RunnableConfig) -> str:
    return config.get("configurable", {}).get("thread_id", "unknown")


def _tenant_id(config: RunnableConfig) -> str:
    return (config.get("configurable") or {}).get("tenant") or db.DEFAULT_TENANT


def _calendar_on(tenant_id: str) -> bool:
    # Google Calendar sync is configured for the default broker through environment variables.
    return tenant_id == db.DEFAULT_TENANT


def _is_free(start: datetime, tenant_id: str, ignore_id: int | None = None) -> tuple[bool, str]:
    end = start + timedelta(minutes=VISIT_MINUTES)
    if start <= now_ist():
        return False, "That time has already passed."
    if start.hour < OPEN_HOUR or end > start.replace(hour=CLOSE_HOUR, minute=0):
        return False, f"Visits run {OPEN_HOUR}:00 to {CLOSE_HOUR}:00 IST."
    day = start.replace(hour=0, minute=0)
    for appt in db.appointments_between(day.isoformat(), (day + timedelta(days=1)).isoformat(), tenant_id):
        if appt["id"] == ignore_id:
            continue
        a_start = parse_ist(appt["start_ist"])
        if start < a_start + timedelta(minutes=appt["duration_min"]) and a_start < end:
            return False, "That slot is already booked."
    if _calendar_on(tenant_id):
        for b_start, b_end in google_calendar.busy_periods(start, end):
            if start < b_end and b_start < end:
                return False, "The advisor is busy then."
    return True, ""


@tool
def check_availability(date: str, config: RunnableConfig) -> str:
    """List free visit slots (IST) on a date. date: YYYY-MM-DD."""
    tenant_id = _tenant_id(config)
    day = datetime.fromisoformat(date.strip()[:10]).replace(tzinfo=IST)
    free = [day.replace(hour=h, minute=0).strftime("%H:%M") for h in range(OPEN_HOUR, CLOSE_HOUR)
            if _is_free(day.replace(hour=h, minute=0), tenant_id)[0]]
    if not free:
        return f"No free slots on {day.strftime('%a %d %b %Y')}. Suggest another day."
    return f"Free slots on {day.strftime('%a %d %b %Y')} (IST): " + ", ".join(free)


@tool
def list_my_appointments(config: RunnableConfig) -> str:
    """List this customer's confirmed upcoming visits."""
    tenant_id = _tenant_id(config)
    appts = db.appointments_for(thread_id=_thread_id(config), tenant_id=tenant_id)
    if not appts:
        return "This customer has no confirmed visits."
    lines = []
    for a in appts:
        listing = db.get_listing(a["listing_id"], tenant_id) or {}
        lines.append(f"#{a['id']}: {fmt_ist(parse_ist(a['start_ist']))}, {a['mode']} visit, {listing.get('project', a['listing_id'])}")
    return "\n".join(lines)


@tool
def book_site_visit(
    listing_id: str,
    start_time_ist: str,
    customer_name: str,
    state: Annotated[dict, InjectedState],
    config: RunnableConfig,
    mode: Literal["site", "video"] = "site",
    notes: str = "",
) -> str:
    """Book a site visit or video walkthrough. start_time_ist: YYYY-MM-DDTHH:MM in IST.
    Needs broker approval before it runs."""
    tenant_id = _tenant_id(config)
    listing = db.get_listing(listing_id.strip().upper(), tenant_id) or db.get_listing(listing_id.strip(), tenant_id)
    if not listing:
        raise ValueError(f"Unknown listing_id {listing_id}. Use one from the matched listings.")
    start = parse_ist(start_time_ist)
    ok, why = _is_free(start, tenant_id)
    if not ok:
        raise ValueError(why + " Call check_availability and offer other slots.")
    phone = (state.get("lead") or {}).get("phone")
    end = start + timedelta(minutes=VISIT_MINUTES)
    event_id = None
    if _calendar_on(tenant_id):
        event_id = google_calendar.create_event(
            f"{'Video tour' if mode == 'video' else 'Site visit'}: {customer_name} · {listing['project']}",
            start, end,
            f"Customer: {customer_name} {phone or ''}\nListing: {listing['listing_id']} {listing['project']}, {listing['locality']}\n{notes}",
        )
    appt_id = db.add_appointment(
        tenant_id=tenant_id, thread_id=_thread_id(config), customer_name=customer_name, customer_phone=phone,
        listing_id=listing["listing_id"], start_ist=start.isoformat(), duration_min=VISIT_MINUTES,
        mode=mode, notes=notes, google_event_id=event_id,
    )
    return (f"Booked #{appt_id}: {'video walkthrough' if mode == 'video' else 'site visit'} on {fmt_ist(start)} "
            f"at {listing['project']}, {listing['locality']}, {listing['city']}. Advisor assigned; reminder goes out the evening before.")


@tool
def reschedule_appointment(appointment_id: int, new_start_time_ist: str, config: RunnableConfig) -> str:
    """Move a confirmed visit to a new time. new_start_time_ist: YYYY-MM-DDTHH:MM in IST."""
    tenant_id = _tenant_id(config)
    appt = db.get_appointment(appointment_id, tenant_id)
    if not appt or appt["status"] != "confirmed":
        raise ValueError(f"No confirmed appointment #{appointment_id}.")
    start = parse_ist(new_start_time_ist)
    ok, why = _is_free(start, tenant_id, ignore_id=appointment_id)
    if not ok:
        raise ValueError(why)
    db.update_appointment(appointment_id, start_ist=start.isoformat())
    if _calendar_on(tenant_id):
        google_calendar.move_event(appt.get("google_event_id"), start, start + timedelta(minutes=appt["duration_min"]))
    return f"Moved #{appointment_id} to {fmt_ist(start)}."


@tool
def cancel_appointment(appointment_id: int, config: RunnableConfig, reason: str = "") -> str:
    """Cancel a confirmed visit."""
    tenant_id = _tenant_id(config)
    appt = db.get_appointment(appointment_id, tenant_id)
    if not appt or appt["status"] != "confirmed":
        raise ValueError(f"No confirmed appointment #{appointment_id}.")
    db.update_appointment(appointment_id, status="cancelled", notes=(appt.get("notes") or "") + f" Cancelled: {reason}")
    if _calendar_on(tenant_id):
        google_calendar.delete_event(appt.get("google_event_id"))
    return f"Cancelled #{appointment_id}."


safe_tools = [check_availability, list_my_appointments]
sensitive_tools = [book_site_visit, reschedule_appointment, cancel_appointment]
sensitive_tool_names = {t.name for t in sensitive_tools}
