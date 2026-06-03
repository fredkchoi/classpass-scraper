"""
ClassPass schedule scraper.

The endpoint is the same one classpass.com uses to render its venue schedule page:
  POST https://classpass.com/_api/v3/search/schedules
  body: {"date": "YYYY-MM-DD", "venue": [<venue_id>], ...}

ClassPass returns 403 to bare requests (Cloudflare bot detection), so we send
browser-like headers and include the cp-authorization token if available.
"""

from __future__ import annotations

import os
from datetime import datetime
from zoneinfo import ZoneInfo

# curl_cffi impersonates Chrome's TLS handshake; plain `requests` gets blocked
# by Cloudflare with a "Just a moment..." challenge.
from curl_cffi import requests

SEARCH_URL = "https://classpass.com/_api/v3/search/schedules"

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36"
)


def _headers() -> dict:
    """
    Headers that mimic the browser's request. ClassPass returns 403 to bare
    requests, so we set the same `platform`, `referer`, and user-agent the
    site uses. If CLASSPASS_AUTH_TOKEN is set we also include it; some
    endpoints work without it but rate limits/anti-bot are friendlier with it.
    """
    headers = {
        "accept": "application/json",
        "accept-language": "en-US",
        "content-type": "application/json",
        "origin": "https://classpass.com",
        "platform": "web",
        "referer": "https://classpass.com/",
        "user-agent": _USER_AGENT,
    }
    token = os.getenv("CLASSPASS_AUTH_TOKEN", "")
    if token:
        headers["cp-authorization"] = f"Token {token}"
    return headers


def fetch_schedule(venue_id: int, date_str: str) -> list:
    """
    Return the raw `schedules` list from the ClassPass search endpoint for one venue/date.
    Returns [] if the request fails or no classes are listed.
    """
    payload = {
        "date": date_str,
        "report_ineligible_classes": True,
        "exclude_past_booking": True,
        "venue": [venue_id],
    }
    try:
        resp = requests.post(SEARCH_URL, json=payload, headers=_headers(), timeout=15, impersonate="chrome")
        if not resp.ok:
            snippet = resp.text[:400].replace("\n", " ")
            print(f"[Error] HTTP {resp.status_code} for venue {venue_id} on {date_str}: {snippet}")
            return []
        return resp.json().get("schedules", [])
    except Exception as e:
        print(f"[Error] Failed to fetch schedule for venue {venue_id} on {date_str}: {e}")
        return []


def _starttime_local(schedule: dict) -> datetime:
    """Convert the schedule's epoch starttime to the venue's local timezone."""
    tz_name = schedule.get("venue", {}).get("tz", "UTC")
    epoch = schedule["starttime"]
    return datetime.fromtimestamp(float(epoch), tz=ZoneInfo(tz_name))


def normalize(schedule: dict) -> dict:
    """Flatten a search result into the fields the rest of the codebase cares about."""
    start_local = _starttime_local(schedule)
    cls = schedule.get("class", {})
    venue = schedule.get("venue", {})
    avail = schedule.get("availability", {})
    return {
        "schedule_id": schedule["id"],
        "class_id": cls.get("id"),
        "class_name": cls.get("name", ""),
        "venue_id": venue.get("id"),
        "venue_name": venue.get("name", ""),
        "venue_tz": venue.get("tz", "UTC"),
        "start_time": start_local,
        "duration_minutes": schedule.get("duration_minutes"),
        "teacher_name": schedule.get("teacher_name") or schedule.get("teacher", {}).get("name", ""),
        "status": avail.get("status"),
        "credits": avail.get("credits"),
        "reason": avail.get("reason"),
        "credits_reasons": avail.get("credits_reasons", []),
        "retail_price_in_credits": schedule.get("retail_price_in_credits"),
    }


def find_matches(
    venue_id: int,
    date_str: str,
    time_str: str | None = None,
    class_name_contains: str | None = None,
    teacher_contains: str | None = None,
    only_available: bool = False,
) -> list:
    """
    Return normalized class entries at this venue/date that match the optional filters.
    - time_str: 'HH:MM' in venue-local time. Exact match on hour+minute.
    - class_name_contains / teacher_contains: case-insensitive substring match.
    - only_available: drop entries whose availability.status != 'available'.
    """
    schedules = fetch_schedule(venue_id, date_str)
    matches = []
    for s in schedules:
        n = normalize(s)
        if time_str:
            try:
                h, m = (int(x) for x in time_str.split(":"))
            except ValueError:
                return []
            if n["start_time"].hour != h or n["start_time"].minute != m:
                continue
        if class_name_contains and class_name_contains.lower() not in n["class_name"].lower():
            continue
        if teacher_contains and teacher_contains.lower() not in (n["teacher_name"] or "").lower():
            continue
        if only_available and n["status"] != "available":
            continue
        matches.append(n)
    return matches
