"""
Polls for cancellations on targets where the initial booking failed.
Runs hourly via GitHub Actions.

When a matching available class is found, tries to book it directly. If the
booking succeeds, emails confirmation and drops the target. If it fails
(e.g. the spot was taken between the search and the reserve), keeps polling.

If CLASSPASS_AUTH_TOKEN is not set, falls back to email-only mode (the user
must book manually via the link).

Polling targets are marked with `"status": "polling"` in targets.json by scheduler.py.
"""

from __future__ import annotations

import json
import os
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from notifier import send_email
from availability import find_matches

TARGETS_FILE = os.path.join(os.path.dirname(__file__), "targets.json")
BOOKING_URL_FMT = "https://classpass.com/classes/{schedule_id}"


def load_targets() -> list:
    if not os.path.exists(TARGETS_FILE):
        return []
    with open(TARGETS_FILE) as f:
        return json.load(f).get("targets", [])


def save_targets(targets: list):
    with open(TARGETS_FILE, "w") as f:
        json.dump({"targets": targets}, f, indent=2)


def _is_expired(target: dict) -> bool:
    """True if the target's class date has already passed."""
    try:
        d = date.fromisoformat(target["date"])
    except (KeyError, ValueError):
        return True
    return d < datetime.now(ZoneInfo("UTC")).date()


def _try_auto_book(target: dict) -> dict | None:
    """Attempt to reserve via book.attempt_booking. Returns the result dict or None."""
    from book import attempt_booking
    return attempt_booking(
        venue_id=target["venue_id"],
        date_str=target["date"],
        time_str=target.get("time"),
        class_name_contains=target.get("class_name_contains"),
        teacher_contains=target.get("teacher_contains"),
        max_credits=target.get("max_credits"),
    )


def _email_booked(result: dict):
    m = result.get("_match", {})
    start = m.get("start_time")
    body = (
        f"Booked {m.get('class_name', 'a class')} on {m.get('start_time').strftime('%Y-%m-%d') if start else ''} via cancellation polling!\n\n"
        f"  - Venue: {m.get('venue_name')}\n"
        f"  - Time: {start.strftime('%I:%M %p %Z') if start else ''}\n"
        f"  - Teacher: {m.get('teacher_name')}\n"
        f"  - Cost: {m.get('credits')} credits\n\n"
        "(Caught via the hourly cancellation poller.)\n\n"
        "- Your ClassPass Bot"
    )
    print(body)
    send_email(subject=f"Booked: ClassPass {m.get('start_time').strftime('%Y-%m-%d') if start else ''}", body=body)

    if start and os.getenv("GOOGLE_REFRESH_TOKEN"):
        try:
            from gcal import create_booking_event
            duration = m.get("duration_minutes") or 50
            end = start + timedelta(minutes=duration)
            create_booking_event(
                start_dt=start,
                end_dt=end,
                summary=f"{m.get('class_name')} @ {m.get('venue_name')}",
                location_name=m.get("venue_name", ""),
                tz_name=m.get("venue_tz", "America/New_York"),
            )
        except Exception as e:
            print(f"[gcal] Skipping calendar event: {e}")


def _email_open(matches: list, date_str: str):
    """Fallback email when we can't auto-book (no token configured)."""
    lines = []
    for m in matches:
        url = BOOKING_URL_FMT.format(schedule_id=m["schedule_id"])
        lines.append(
            f"  - {m['start_time'].strftime('%I:%M %p')} - {m['class_name']} "
            f"({m['teacher_name']}) - {m['credits']} credits\n    {url}"
        )
    body = (
        f"A spot opened up for {matches[0]['venue_name']} on {date_str}:\n\n"
        + "\n".join(lines)
        + "\n\n- Your ClassPass Bot"
    )
    send_email(subject=f"Cancellation available: ClassPass {date_str}", body=body)


def main():
    targets = load_targets()
    polling = [t for t in targets if t.get("status") == "polling"]
    if not polling:
        print("No cancellation polling targets.")
        return

    has_token = bool(os.getenv("CLASSPASS_AUTH_TOKEN"))
    still_watching = []
    booked_keys: set = set()

    for target in polling:
        if _is_expired(target):
            print(f"Dropping expired polling target: {target.get('date')}")
            continue

        venue_id = target["venue_id"]
        date_str = target["date"]
        label = f"venue {venue_id} {date_str} {target.get('time') or 'any'} {target.get('class_name_contains') or ''}".strip()
        print(f"Checking cancellations for {label}...")

        matches = find_matches(
            venue_id=venue_id,
            date_str=date_str,
            time_str=target.get("time"),
            class_name_contains=target.get("class_name_contains"),
            teacher_contains=target.get("teacher_contains"),
            only_available=True,
        )
        if not matches:
            print(f"No availability yet for {label}.")
            still_watching.append(target)
            continue

        if has_token:
            try:
                result = _try_auto_book(target)
            except Exception as e:
                print(f"[poller] auto-book raised: {e}")
                result = None

            if result:
                _email_booked(result)
                booked_keys.add((date_str, venue_id))
                continue
            print(f"[poller] auto-book did not complete; keeping target in polling for next hour")
            still_watching.append(target)
        else:
            print(f"No CLASSPASS_AUTH_TOKEN set; emailing match instead of booking.")
            _email_open(matches, date_str)
            booked_keys.add((date_str, venue_id))

    non_polling = [t for t in targets if t.get("status") != "polling"]
    remaining_polling = [
        t for t in still_watching
        if (t["date"], t["venue_id"]) not in booked_keys
    ]
    save_targets(non_polling + remaining_polling)


if __name__ == "__main__":
    main()
