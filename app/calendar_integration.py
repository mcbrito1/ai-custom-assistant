"""
Google Calendar integration for Hermes.

Setup (one-time, on Windows host):
1. Go to https://console.cloud.google.com → APIs & Services → Credentials
2. Create OAuth2 credentials (Desktop app) → download as credentials.json
3. Place credentials.json in C:\\Git\\Hermes\\ (or wherever GOOGLE_CREDS_PATH points)
4. First time Hermes uses calendar, it opens a browser for authorization
5. token.json is saved automatically for future runs

Required env vars (in .env):
  GOOGLE_CREDS_PATH=/app/data/google_credentials.json
  GOOGLE_TOKEN_PATH=/app/data/google_token.json
  GOOGLE_CALENDAR_ID=primary
"""
import json
import logging
import os
from datetime import datetime, timedelta
from pathlib import Path

log = logging.getLogger(__name__)

CREDS_PATH = os.getenv("GOOGLE_CREDS_PATH", "/app/data/google_credentials.json")
TOKEN_PATH = os.getenv("GOOGLE_TOKEN_PATH", "/app/data/google_token.json")
CALENDAR_ID = os.getenv("GOOGLE_CALENDAR_ID", "primary")

_service = None


def _get_service():
    global _service
    if _service:
        return _service
    try:
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
        from google.auth.transport.requests import Request
        from googleapiclient.discovery import build

        SCOPES = ["https://www.googleapis.com/auth/calendar"]
        creds = None

        if Path(TOKEN_PATH).exists():
            creds = Credentials.from_authorized_user_file(TOKEN_PATH, SCOPES)

        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            elif Path(CREDS_PATH).exists():
                flow = InstalledAppFlow.from_client_secrets_file(CREDS_PATH, SCOPES)
                # In Docker, use console flow (no browser)
                creds = flow.run_console()
            else:
                log.warning("Google Calendar: credentials file not found at %s", CREDS_PATH)
                return None

            Path(TOKEN_PATH).write_text(creds.to_json(), encoding="utf-8")

        _service = build("calendar", "v3", credentials=creds)
        log.info("Google Calendar service initialized.")
        return _service
    except ImportError:
        log.warning("Google Calendar: google-auth packages not installed.")
        return None
    except Exception as e:
        log.warning(f"Google Calendar init failed: {e}")
        return None


def is_available() -> bool:
    return _get_service() is not None


def create_event(summary: str, start: datetime, end: datetime = None,
                 description: str = "", location: str = "") -> dict | None:
    """Create a calendar event. Returns the created event dict or None on failure."""
    svc = _get_service()
    if not svc:
        return None

    if end is None:
        end = start + timedelta(hours=1)

    event = {
        "summary": summary,
        "description": description,
        "location": location,
        "start": {"dateTime": start.isoformat(), "timeZone": "America/Sao_Paulo"},
        "end": {"dateTime": end.isoformat(), "timeZone": "America/Sao_Paulo"},
    }
    try:
        result = svc.events().insert(calendarId=CALENDAR_ID, body=event).execute()
        log.info(f"Calendar event created: {summary} at {start}")
        return result
    except Exception as e:
        log.error(f"Calendar create_event failed: {e}")
        return None


def list_events(days_ahead: int = 7, max_results: int = 10) -> list[dict]:
    """Return upcoming events for the next N days."""
    svc = _get_service()
    if not svc:
        return []
    try:
        now = datetime.utcnow().isoformat() + "Z"
        end = (datetime.utcnow() + timedelta(days=days_ahead)).isoformat() + "Z"
        result = svc.events().list(
            calendarId=CALENDAR_ID,
            timeMin=now,
            timeMax=end,
            maxResults=max_results,
            singleEvents=True,
            orderBy="startTime",
        ).execute()
        events = result.get("items", [])
        return [
            {
                "summary": e.get("summary", "(sem título)"),
                "start": e.get("start", {}).get("dateTime", e.get("start", {}).get("date", "")),
                "end": e.get("end", {}).get("dateTime", e.get("end", {}).get("date", "")),
                "location": e.get("location", ""),
                "description": e.get("description", ""),
            }
            for e in events
        ]
    except Exception as e:
        log.error(f"Calendar list_events failed: {e}")
        return []


def format_events_text(events: list[dict]) -> str:
    if not events:
        return "Nenhum evento nos próximos dias."
    lines = []
    for e in events:
        start = e["start"].replace("T", " ")[:16] if "T" in e["start"] else e["start"]
        line = f"• {start} — *{e['summary']}*"
        if e.get("location"):
            line += f" 📍 {e['location']}"
        lines.append(line)
    return "\n".join(lines)
