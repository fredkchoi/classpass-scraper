"""
ClassPass booking.

Flow:
  1. GET /_api/v3/search/schedules         (no auth)  -> find the schedule id + current credit cost
  2. POST /_api/v1/users/{user_id}/reservations       -> reserve

Auth: `cp-authorization: Token <hex>`. The token is captured from DevTools and
stored in CLASSPASS_AUTH_TOKEN. If it expires mid-run and CLASSPASS_EMAIL/PASSWORD
are also set, the booker will call auth.refresh_token() once and retry.

Credits are the dynamic price returned in the search response — don't hardcode them.
"""

from __future__ import annotations

from curl_cffi import requests
from availability import find_matches
from config import CLASSPASS_AUTH_TOKEN, CLASSPASS_USER_ID

BASE_URL = "https://classpass.com"
BALANCE_URL_FMT = BASE_URL + "/_api/v3/lifecycle/user/{user_id}/balance"

# In-memory token (overridable at runtime if the env-var token expires)
_token: str = CLASSPASS_AUTH_TOKEN


def set_token(token: str):
    global _token
    _token = token


def get_token() -> str:
    return _token


def _headers() -> dict:
    if not _token:
        raise ValueError("CLASSPASS_AUTH_TOKEN is not set — required for booking")
    if not CLASSPASS_USER_ID:
        raise ValueError("CLASSPASS_USER_ID is not set — required for booking")
    return {
        "cp-authorization": f"Token {_token}",
        "content-type": "application/json",
        "platform": "web",
        "accept-language": "en-US",
    }


def _reserve_once(schedule_id: int, credits: int):
    url = f"{BASE_URL}/_api/v1/users/{CLASSPASS_USER_ID}/reservations"
    return requests.post(
        url,
        json={"schedule": schedule_id, "credits": credits},
        headers=_headers(),
        timeout=30,
        impersonate="chrome",
    )


def reserve(schedule_id: int, credits: int) -> dict:
    """
    POST a reservation. On 401/403, try to refresh the token once via auth.py
    and retry. Raises on any other HTTP error.
    """
    resp = _reserve_once(schedule_id, credits)
    if resp.status_code in (401, 403):
        print(f"[book] Got {resp.status_code} — attempting token refresh...")
        try:
            from auth import refresh_token
            new_token = refresh_token()
        except Exception as e:
            print(f"[book] Token refresh raised: {e}")
            new_token = ""
        if new_token:
            set_token(new_token)
            print("[book] Token refreshed, retrying reservation...")
            resp = _reserve_once(schedule_id, credits)

    if not resp.ok:
        print(f"[book] HTTP {resp.status_code}: {resp.text[:500]}")
    resp.raise_for_status()
    return resp.json()


def _balance_once():
    if not CLASSPASS_USER_ID:
        raise ValueError("CLASSPASS_USER_ID is not set, required for balance check")
    return requests.get(
        BALANCE_URL_FMT.format(user_id=CLASSPASS_USER_ID),
        headers=_headers(),
        timeout=15,
        impersonate="chrome",
    )


def fetch_credit_balance() -> int | None:
    """
    Returns the user's current ClassPass credit balance, or None if the request fails.
    Retries once with a refreshed token on 401/403.
    """
    try:
        resp = _balance_once()
    except ValueError as e:
        print(f"[book] balance: {e}")
        return None

    if resp.status_code in (401, 403):
        print(f"[book] balance: got {resp.status_code}, attempting token refresh...")
        try:
            from auth import refresh_token
            new_token = refresh_token()
        except Exception as e:
            print(f"[book] Token refresh raised: {e}")
            new_token = ""
        if new_token:
            set_token(new_token)
            resp = _balance_once()

    if not resp.ok:
        print(f"[book] balance HTTP {resp.status_code}: {resp.text[:200]}")
        return None

    try:
        return resp.json().get("data", {}).get("credit_balance", {}).get("credits_remaining")
    except Exception as e:
        print(f"[book] balance parse error: {e}")
        return None


def attempt_booking(
    venue_id: int,
    date_str: str,
    time_str: str | None = None,
    class_name_contains: str | None = None,
    teacher_contains: str | None = None,
    max_credits: int | None = None,
) -> dict | None:
    """
    Find a matching available class at this venue/date and reserve it.
    `max_credits`: refuse to book if the dynamic credit cost exceeds this cap.
    Returns the reservation response (with `_match` attached) or None.
    """
    matches = find_matches(
        venue_id=venue_id,
        date_str=date_str,
        time_str=time_str,
        class_name_contains=class_name_contains,
        teacher_contains=teacher_contains,
        only_available=True,
    )
    if not matches:
        return None

    cls = matches[0]
    credits = cls.get("credits")
    if credits is None:
        print(f"[book] No credit cost in availability for schedule {cls['schedule_id']} — skipping")
        return None
    if max_credits is not None and credits > max_credits:
        print(f"[book] {cls['class_name']} costs {credits} credits, exceeds cap of {max_credits} — skipping")
        return None

    print(f"Booking {cls['class_name']} on {date_str} at {cls['start_time'].strftime('%I:%M %p')} ({credits} credits)...")
    result = reserve(cls["schedule_id"], credits)
    return {**result, "_match": cls}
