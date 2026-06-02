"""
Google Calendar integration. Creates an event after a successful booking.

Credentials are loaded from env vars (never hardcoded):
  GOOGLE_CLIENT_ID
  GOOGLE_CLIENT_SECRET
  GOOGLE_REFRESH_TOKEN  (obtained once via setup_gcal.py)
  GOOGLE_CALENDAR_ID    (default: "primary")
  GOOGLE_EVENT_COLOR    (optional)
"""

import os
import requests
from datetime import datetime

_TOKEN_URL = "https://oauth2.googleapis.com/token"
_EVENTS_URL = "https://www.googleapis.com/calendar/v3/calendars/{calendar_id}/events"

_COLOR_IDS = {
    "tomato": "11",
    "flamingo": "4",
    "tangerine": "6",
    "banana": "5",
    "sage": "2",
    "basil": "8",
    "peacock": "7",
    "blueberry": "9",
    "lavender": "1",
    "grape": "3",
    "graphite": "8",
}


def _get_access_token() -> str:
    client_id = os.getenv("GOOGLE_CLIENT_ID", "")
    client_secret = os.getenv("GOOGLE_CLIENT_SECRET", "")
    refresh_token = os.getenv("GOOGLE_REFRESH_TOKEN", "")
    if not all([client_id, client_secret, refresh_token]):
        raise ValueError("GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, and GOOGLE_REFRESH_TOKEN must all be set")

    resp = requests.post(_TOKEN_URL, data={
        "grant_type": "refresh_token",
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": refresh_token,
    }, timeout=15)
    resp.raise_for_status()
    return resp.json()["access_token"]


def create_booking_event(
    start_dt: datetime,
    end_dt: datetime,
    summary: str,
    location_name: str = "",
    tz_name: str = "America/New_York",
) -> str:
    """
    Create a Google Calendar event for a confirmed ClassPass booking.
    `start_dt` and `end_dt` must be timezone-aware.
    Returns the created event URL, or '' on failure.
    """
    calendar_id = os.getenv("GOOGLE_CALENDAR_ID", "primary")
    color_name = os.getenv("GOOGLE_EVENT_COLOR", "peacock").lower()
    color_id = _COLOR_IDS.get(color_name, "7")

    event = {
        "summary": summary,
        "location": location_name,
        "colorId": color_id,
        "start": {"dateTime": start_dt.isoformat(), "timeZone": tz_name},
        "end": {"dateTime": end_dt.isoformat(), "timeZone": tz_name},
    }

    try:
        access_token = _get_access_token()
        resp = requests.post(
            _EVENTS_URL.format(calendar_id=calendar_id),
            json=event,
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=15,
        )
        resp.raise_for_status()
        event_url = resp.json().get("htmlLink", "")
        print(f"[gcal] Event created: {event_url}")
        return event_url
    except Exception as e:
        print(f"[gcal] Failed to create calendar event: {e}")
        return ""
