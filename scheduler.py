"""
Hourly runner triggered by the cf-trigger Cloudflare Worker.

For each non-polling target, this script:
  1. Queries the ClassPass schedule API to find the matching class
  2. Branches on availability.status + availability.reason:
       - status="available" -> book immediately
       - reason="out_of_spots" -> flag as polling for the cancellation poller
       - reason="before_opening_window" -> parse the release time from
         availability.credits_reasons (e.g. "The booking window opens on
         6/4/26, 5:00 AM"), wait until then if within the next ~75 min,
         otherwise skip (the next hourly run picks it up)
  3. Targets whose class isn't yet on the schedule (search returns nothing)
     are skipped silently and retried next hour.

The optional `release_at` field on a target overrides whatever the API says,
useful when ClassPass formats the message differently or you want to test.
"""

from __future__ import annotations

import json
import os
import re
import time
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from notifier import send_email
from availability import fetch_schedule, find_matches, normalize

TARGETS_FILE = os.path.join(os.path.dirname(__file__), "targets.json")
# Maximum lookahead per run. With hourly cron + 75min lookahead we always have
# at least one run that catches the release moment, even with cron drift.
WAIT_WINDOW_MINUTES = 75
# After the release time arrives, retry book this long before giving up.
BOOK_RETRY_MINUTES = 5
RELEASE_REGEX = re.compile(
    r"(\d{1,2})/(\d{1,2})/(\d{2,4}),\s*(\d{1,2}):(\d{2})\s*(AM|PM)",
    re.IGNORECASE,
)


def load_targets() -> list:
    if not os.path.exists(TARGETS_FILE):
        return []
    with open(TARGETS_FILE) as f:
        return json.load(f).get("targets", [])


def save_targets(targets: list):
    with open(TARGETS_FILE, "w") as f:
        json.dump({"targets": targets}, f, indent=2)


def validate_targets(targets: list) -> list:
    """Return a list of human-readable error strings. Empty means all valid."""
    errors = []
    for i, t in enumerate(targets):
        prefix = f"Target {i + 1}"
        if not isinstance(t, dict):
            errors.append(f"{prefix}: must be an object")
            continue

        if not t.get("venue_id"):
            errors.append(f"{prefix}: missing 'venue_id'")
        elif not isinstance(t["venue_id"], int):
            errors.append(f"{prefix}: 'venue_id' must be an integer")

        date_str = t.get("date")
        if not date_str:
            errors.append(f"{prefix}: missing 'date'")
        else:
            try:
                date.fromisoformat(date_str)
            except ValueError:
                errors.append(f"{prefix}: invalid date '{date_str}' (expected YYYY-MM-DD)")

        time_str = t.get("time")
        if time_str is not None:
            try:
                h, m = (int(x) for x in time_str.split(":"))
                if not (0 <= h <= 23 and 0 <= m <= 59):
                    raise ValueError
            except (ValueError, AttributeError):
                errors.append(f"{prefix}: time '{time_str}' must be HH:MM (24hr)")

    return errors


def _parse_release_dt(credits_reasons: list, venue_tz: str) -> datetime | None:
    """
    Extract a tz-aware datetime from a ClassPass `credits_reasons` message like
    'The booking window opens on 6/4/26, 5:00 AM'. Returns None if no match.
    """
    for msg in credits_reasons or []:
        m = RELEASE_REGEX.search(msg)
        if not m:
            continue
        month, day, year, hour, minute, ampm = m.groups()
        year_int = int(year)
        if year_int < 100:
            year_int += 2000
        hour_int = int(hour) % 12
        if ampm.upper() == "PM":
            hour_int += 12
        try:
            return datetime(
                year_int, int(month), int(day),
                hour_int, int(minute), 0,
                tzinfo=ZoneInfo(venue_tz),
            )
        except (ValueError, OverflowError):
            return None
    return None


def _match_for_target(target: dict) -> dict | None:
    """Return the first matching class for this target, or None if no match yet."""
    matches = find_matches(
        venue_id=target["venue_id"],
        date_str=target["date"],
        time_str=target.get("time"),
        class_name_contains=target.get("class_name_contains"),
        teacher_contains=target.get("teacher_contains"),
        only_available=False,
    )
    return matches[0] if matches else None


def wait_until(target_dt: datetime):
    now = datetime.now(target_dt.tzinfo)
    sleep_secs = (target_dt - now).total_seconds()
    if sleep_secs > 0:
        print(f"Waiting {sleep_secs:.0f}s ({sleep_secs / 60:.1f} min) until {target_dt.isoformat()}...")
        time.sleep(sleep_secs)
    else:
        print(f"Already past {target_dt.isoformat()}, proceeding immediately.")


def _book_with_retries(target: dict, label: str) -> bool:
    """Retry book.attempt_booking for up to BOOK_RETRY_MINUTES."""
    from book import attempt_booking
    deadline = datetime.now() + timedelta(minutes=BOOK_RETRY_MINUTES)
    attempt = 0

    while datetime.now() < deadline:
        attempt += 1
        print(f"[Attempt {attempt}] Booking {label}...")
        try:
            result = attempt_booking(
                venue_id=target["venue_id"],
                date_str=target["date"],
                time_str=target.get("time"),
                class_name_contains=target.get("class_name_contains"),
                teacher_contains=target.get("teacher_contains"),
                max_credits=target.get("max_credits"),
            )
        except Exception as e:
            print(f"[Attempt {attempt}] Error: {e}")
            time.sleep(5)
            continue

        if result:
            m = result.get("_match", {})
            start = m.get("start_time")
            body = (
                f"Booked {m.get('class_name', 'a class')} on {target['date']}!\n\n"
                f"  - Venue: {m.get('venue_name')}\n"
                f"  - Time: {start.strftime('%I:%M %p %Z') if start else target['date']}\n"
                f"  - Teacher: {m.get('teacher_name')}\n"
                f"  - Cost: {m.get('credits')} credits\n\n"
                "- Your ClassPass Bot"
            )
            print(body)
            send_email(subject=f"Booked: ClassPass {target['date']}", body=body)

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

            return True
        time.sleep(3)

    return False


def _override_release_dt(target: dict, default_tz: str) -> datetime | None:
    """If target has a `release_at` field, parse it as a tz-aware datetime."""
    raw = target.get("release_at")
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        print(f"[scheduler] Could not parse release_at '{raw}' for target {target.get('date')}")
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo(default_tz))
    return dt


def process_target(target: dict) -> dict:
    """
    Run the per-target decision loop. Returns the updated target dict (with
    `status: polling` if we need the cancellation poller to take over).
    """
    label = f"venue {target['venue_id']} {target['date']} {target.get('time') or 'any'} {target.get('class_name_contains') or ''}".strip()
    print(f"\n=== {label} ===")

    cls = _match_for_target(target)
    if not cls:
        print("No matching class on the schedule yet (might appear closer to the date).")
        return target

    venue_tz = cls.get("venue_tz", "America/New_York")
    status = cls.get("status")
    reason = cls.get("reason")

    if status == "available":
        print(f"Class is available right now. Booking immediately.")
        if _book_with_retries(target, label):
            return {"_booked": True}
        print("Book failed, flagging as polling.")
        return {**target, "status": "polling"}

    if reason == "out_of_spots":
        print("Class is full. Flagging as polling for cancellation watcher.")
        return {**target, "status": "polling"}

    if reason == "before_opening_window":
        release_dt = _override_release_dt(target, venue_tz) or _parse_release_dt(
            cls.get("credits_reasons", []), venue_tz
        )
        if not release_dt:
            print(f"Release time unknown (credits_reasons={cls.get('credits_reasons')}). "
                  "Skipping; will retry next hour.")
            return target

        now = datetime.now(release_dt.tzinfo)
        delta = (release_dt - now).total_seconds() / 60
        print(f"Release at {release_dt.isoformat()} ({delta:+.1f} min from now).")

        if delta > WAIT_WINDOW_MINUTES:
            print(f"More than {WAIT_WINDOW_MINUTES} min away; skipping this run.")
            return target

        wait_until(release_dt)
        if _book_with_retries(target, label):
            return {"_booked": True}
        print("Book failed after window opened; flagging as polling.")
        return {**target, "status": "polling"}

    print(f"Unhandled state: status={status} reason={reason}; skipping.")
    return target


def main():
    targets = load_targets()

    errors = validate_targets(targets)
    if errors:
        body = "targets.json has validation errors:\n\n" + "\n".join(f"  - {e}" for e in errors)
        body += "\n\n- Your ClassPass Bot"
        print(body)
        send_email(subject="ClassPass: targets.json has errors", body=body)
        return

    if not targets:
        print("No targets to process.")
        return

    actionable = [t for t in targets if t.get("status") != "polling"]
    if not actionable:
        print("No actionable targets (all are in polling state).")
        return

    failed_or_updated = []
    booked_keys: set = set()

    for target in actionable:
        result = process_target(target)
        if result.get("_booked"):
            booked_keys.add((target["date"], target["venue_id"]))
        else:
            failed_or_updated.append(result)

    polling = [t for t in targets if t.get("status") == "polling"]
    remaining = [
        t for t in failed_or_updated
        if (t.get("date"), t.get("venue_id")) not in booked_keys
    ]
    save_targets(polling + remaining)


if __name__ == "__main__":
    main()
