"""Optional Google Calendar sync. Enabled when GOOGLE_CALENDAR_ID is set and
credentials.json (OAuth client) or token.json exists in the backend folder.
The local SQLite appointments table is always the source of truth."""
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)
SCOPES = ["https://www.googleapis.com/auth/calendar"]
BACKEND_DIR = Path(__file__).resolve().parents[2]


def enabled() -> bool:
    return bool(os.getenv("GOOGLE_CALENDAR_ID")) and (
        (BACKEND_DIR / "token.json").exists() or (BACKEND_DIR / "credentials.json").exists()
    )


def _service():
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build

    token = BACKEND_DIR / "token.json"
    creds = Credentials.from_authorized_user_file(str(token), SCOPES) if token.exists() else None
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(str(BACKEND_DIR / "credentials.json"), SCOPES)
            creds = flow.run_local_server(port=8080)
        token.write_text(creds.to_json())
    return build("calendar", "v3", credentials=creds)


def busy_periods(start: datetime, end: datetime) -> list[tuple[datetime, datetime]]:
    if not enabled():
        return []
    try:
        body = {"timeMin": start.isoformat(), "timeMax": end.isoformat(), "timeZone": "Asia/Kolkata",
                "items": [{"id": os.environ["GOOGLE_CALENDAR_ID"]}]}
        cal = _service().freebusy().query(body=body).execute()["calendars"][os.environ["GOOGLE_CALENDAR_ID"]]
        return [(datetime.fromisoformat(b["start"].replace("Z", "+00:00")), datetime.fromisoformat(b["end"].replace("Z", "+00:00")))
                for b in cal.get("busy", [])]
    except Exception as exc:
        logger.warning("Google free/busy failed, using local calendar only: %s", exc)
        return []


def create_event(summary: str, start: datetime, end: datetime, description: str) -> Optional[str]:
    if not enabled():
        return None
    try:
        body = {"summary": summary, "description": description,
                "start": {"dateTime": start.isoformat(), "timeZone": "Asia/Kolkata"},
                "end": {"dateTime": end.isoformat(), "timeZone": "Asia/Kolkata"}}
        return _service().events().insert(calendarId=os.environ["GOOGLE_CALENDAR_ID"], body=body).execute()["id"]
    except Exception as exc:
        logger.warning("Google event creation failed: %s", exc)
        return None


def move_event(event_id: str, start: datetime, end: datetime) -> None:
    if not (enabled() and event_id):
        return
    try:
        _service().events().patch(calendarId=os.environ["GOOGLE_CALENDAR_ID"], eventId=event_id, body={
            "start": {"dateTime": start.isoformat(), "timeZone": "Asia/Kolkata"},
            "end": {"dateTime": end.isoformat(), "timeZone": "Asia/Kolkata"}}).execute()
    except Exception as exc:
        logger.warning("Google event update failed: %s", exc)


def delete_event(event_id: str) -> None:
    if not (enabled() and event_id):
        return
    try:
        _service().events().delete(calendarId=os.environ["GOOGLE_CALENDAR_ID"], eventId=event_id).execute()
    except Exception as exc:
        logger.warning("Google event deletion failed: %s", exc)
