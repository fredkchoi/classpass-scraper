"""
ClassPass booking.

Flow:
  1. GET /_api/v3/search/schedules         -> find the schedule id + current credit cost
  2. POST /_api/v1/users/{user_id}/reservations -> reserve

Auth: `cp-authorization: Token <hex>`. The token is captured manually from
DevTools (see README, section "Capturing your ClassPass auth token") and
stored in the CLASSPASS_AUTH_TOKEN env var. It's long-lived (weeks to months).
There is no auto-refresh: the login endpoint's request body is hidden behind
a service worker we can't capture without mitmproxy. If the token goes stale,
the scheduler and cancellation poller will send a one-shot email at run start
flagging that CLASSPASS_AUTH_TOKEN needs to be rotated.

Credits are the dynamic price returned in the search response, don't hardcode them.
"""

from __future__ import annotations

from curl_cffi import requests
from availability import find_matches
from config import CLASSPASS_AUTH_TOKEN, CLASSPASS_USER_ID

BASE_URL = "https://classpass.com"
BALANCE_URL_FMT = BASE_URL + "/_api/v3/lifecycle/user/{user_id}/balance"


def _headers() -> dict:
    if not CLASSPASS_AUTH_TOKEN:
        raise ValueError("CLASSPASS_AUTH_TOKEN is not set, required for booking")
    if not CLASSPASS_USER_ID:
        raise ValueError("CLASSPASS_USER_ID is not set, required for booking")
    return {
        "cp-authorization": f"Token {CLASSPASS_AUTH_TOKEN}",
        "content-type": "application/json",
        "platform": "web",
        "accept-language": "en-US",
    }


def reserve(schedule_id: int, credits: int) -> dict:
    """
    POST a reservation. On non-2xx, raises RuntimeError with the response body
    in the message so callers can branch on specific ClassPass error codes
    (e.g. 5017 = "wrong price for this schedule, try a different credits value").
    Using a plain RuntimeError-with-body instead of `raise_for_status()` because
    the latter only carries the status code, not the body that explains why.
    """
    url = f"{BASE_URL}/_api/v1/users/{CLASSPASS_USER_ID}/reservations"
    resp = requests.post(
        url,
        json={"schedule": schedule_id, "credits": credits},
        headers=_headers(),
        timeout=30,
        impersonate="chrome",
    )
    if not resp.ok:
        body = (resp.text or "")[:500]
        print(f"[book] HTTP {resp.status_code}: {body}")
        raise RuntimeError(f"HTTP {resp.status_code}: {body}")
    return resp.json()


def fetch_credit_balance() -> int | None:
    """
    Returns the user's current ClassPass credit balance, or None if the request fails.
    Also doubles as an auth health check: a None return on a target-bearing run is
    what `scheduler.main()` and `cancellation_poller.main()` use to trigger the
    "token may be stale" email.
    """
    try:
        resp = requests.get(
            BALANCE_URL_FMT.format(user_id=CLASSPASS_USER_ID),
            headers=_headers(),
            timeout=15,
            impersonate="chrome",
        )
    except ValueError as e:
        print(f"[book] balance: {e}")
        return None

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
        print(f"[book] No credit cost in availability for schedule {cls['schedule_id']}, skipping")
        return None
    if max_credits is not None and credits > max_credits:
        print(f"[book] {cls['class_name']} costs {credits} credits, exceeds cap of {max_credits}, skipping")
        return None

    print(f"Booking {cls['class_name']} on {date_str} at {cls['start_time'].strftime('%I:%M %p')} ({credits} credits)...")
    result = reserve(cls["schedule_id"], credits)
    return {**result, "_match": cls}
