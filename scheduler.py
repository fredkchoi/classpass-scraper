"""
Nightly runner. Validates targets, then books at the moment the booking window opens.

Triggered by GitHub Actions; can also be run manually. Flow:
  1. Load and validate targets.json. Email errors and exit if invalid.
  2. For each target, determine when its booking window opens (default: 7 days
     before the class date at midnight in the venue's local timezone).
  3. Targets that opened earlier (i.e. window is already live): try immediately.
  4. Targets opening tonight: wait until midnight venue-local, then book.
  5. On success, drop the target from targets.json. On failure, flag it as
     "polling" so cancellation_poller.py keeps watching.
"""

import json
import os
import time
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from notifier import send_email
from availability import fetch_schedule

TARGETS_FILE = os.path.join(os.path.dirname(__file__), "targets.json")
DEFAULT_LEAD_DAYS = 7


def load_targets() -> list:
    if not os.path.exists(TARGETS_FILE):
        return []
    with open(TARGETS_FILE) as f:
        return json.load(f).get("targets", [])


def save_targets(targets: list):
    with open(TARGETS_FILE, "w") as f:
        json.dump({"targets": targets}, f, indent=2)


def validate_targets(targets: list) -> list:
    """Return a list of human-readable error strings. Empty list means all targets valid."""
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


def _venue_tz(venue_id: int, date_str: str) -> ZoneInfo:
    """Look up the venue's timezone from a search response. Falls back to UTC on failure."""
    schedules = fetch_schedule(venue_id, date_str)
    for s in schedules:
        tz = s.get("venue", {}).get("tz")
        if tz:
            return ZoneInfo(tz)
    return ZoneInfo("UTC")


def _window_open_dt(target: dict) -> datetime:
    """Return the timezone-aware datetime at which this target's booking window opens."""
    lead_days = target.get("lead_days", DEFAULT_LEAD_DAYS)
    tz = _venue_tz(target["venue_id"], target["date"])
    class_date = date.fromisoformat(target["date"])
    open_date = class_date - timedelta(days=lead_days)
    return datetime(open_date.year, open_date.month, open_date.day, 0, 0, 0, tzinfo=tz)


def _now(tz: ZoneInfo) -> datetime:
    return datetime.now(tz)


def book_target(target: dict) -> bool:
    """Attempt to book one target. Retries for up to 5 minutes on transient errors."""
    from book import attempt_booking
    venue_id = target["venue_id"]
    date_str = target["date"]
    time_str = target.get("time")
    name_filter = target.get("class_name_contains")
    teacher_filter = target.get("teacher_contains")
    max_credits = target.get("max_credits")

    label = f"venue {venue_id} {date_str} {time_str or 'any time'} {name_filter or 'any class'}".strip()
    deadline = datetime.now() + timedelta(minutes=5)
    attempt = 0

    while datetime.now() < deadline:
        attempt += 1
        print(f"[Attempt {attempt}] Booking {label}...")
        try:
            result = attempt_booking(
                venue_id=venue_id,
                date_str=date_str,
                time_str=time_str,
                class_name_contains=name_filter,
                teacher_contains=teacher_filter,
                max_credits=max_credits,
            )
        except Exception as e:
            print(f"[Attempt {attempt}] Error: {e}")
            time.sleep(10)
            continue

        if result:
            m = result.get("_match", {})
            start = m.get("start_time")
            body = (
                f"Booked {m.get('class_name', 'a class')} on {date_str}!\n\n"
                f"  • Venue: {m.get('venue_name')}\n"
                f"  • Time: {start.strftime('%I:%M %p %Z') if start else date_str}\n"
                f"  • Teacher: {m.get('teacher_name')}\n"
                f"  • Cost: {m.get('credits')} credits\n\n"
                "— Your ClassPass Bot"
            )
            print(body)
            send_email(subject=f"Booked: ClassPass {date_str}", body=body)

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
        time.sleep(5)

    body = f"Failed to book ClassPass: {label} after 5 minutes of retries.\n\n— Your ClassPass Bot"
    print(body)
    send_email(subject=f"FAILED: ClassPass booking {date_str}", body=body)
    return False


def wait_until(target_dt: datetime):
    sleep_secs = (target_dt - datetime.now(target_dt.tzinfo)).total_seconds()
    if sleep_secs > 0:
        print(f"Waiting {sleep_secs:.0f}s ({sleep_secs / 60:.1f} min) until {target_dt.isoformat()}...")
        time.sleep(sleep_secs)
    else:
        print(f"Window already open ({target_dt.isoformat()}) — proceeding immediately.")


def main():
    targets = load_targets()

    errors = validate_targets(targets)
    if errors:
        body = "targets.json has validation errors:\n\n" + "\n".join(f"  • {e}" for e in errors)
        body += "\n\n— Your ClassPass Bot"
        print(body)
        send_email(subject="ClassPass: targets.json has errors", body=body)
        return

    if not targets:
        print("No targets to process.")
        return

    enriched = []
    for t in targets:
        if t.get("status") == "polling":
            continue
        try:
            open_dt = _window_open_dt(t)
            enriched.append((t, open_dt))
        except Exception as e:
            print(f"Error computing window for {t}: {e}")

    if not enriched:
        print("No actionable targets tonight.")
        return

    # Anything that should fire within the next 6 hours counts as "tonight".
    now_utc = datetime.now(ZoneInfo("UTC"))
    cutoff = now_utc + timedelta(hours=6)
    tonight = [(t, dt) for t, dt in enriched if now_utc <= dt <= cutoff]
    already_open = [(t, dt) for t, dt in enriched if dt < now_utc]

    if not tonight and not already_open:
        print(f"Nothing opens in the next 6h. Next windows: {[(t['date'], dt.isoformat()) for t, dt in enriched]}")
        return

    failed = []

    for t, _dt in already_open:
        print(f"Booking already-open target: {t['date']} venue {t['venue_id']}")
        if not book_target(t):
            failed.append({**t, "status": "polling"})

    for t, open_dt in sorted(tonight, key=lambda x: x[1]):
        print(f"Tonight: {t['date']} venue {t['venue_id']} opens at {open_dt.isoformat()}")
        wait_until(open_dt)
        if not book_target(t):
            failed.append({**t, "status": "polling"})

    attempted = {(t["date"], t["venue_id"]) for t, _ in (already_open + tonight)}
    remaining = [t for t in targets if (t.get("date"), t.get("venue_id")) not in attempted]
    save_targets(remaining + failed)


if __name__ == "__main__":
    main()
