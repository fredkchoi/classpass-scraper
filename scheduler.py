"""
Hourly runner triggered by the cf-trigger Cloudflare Worker.

Each target may carry a `preferences` array of priority-ordered alternatives
(e.g. preferred time + fallback times). Top-level fields on the target are
inherited by each preference; preference fields override them.

Per-target flow each run:
  1. Expand the target into its priority-ordered preference list
  2. Query the schedule API for each preference
  3. If any preference is currently `available`, attempt to book it in
     priority order (first available wins).
  4. Otherwise, pick the earliest release time across all preferences with
     reason=before_opening_window. If within ~75 min of run start, sleep
     until that exact moment then try each preference in priority order.
  5. If all preferences are out_of_spots (or fail to book), flag the whole
     target as polling for the cancellation watcher.
"""

from __future__ import annotations

import json
import os
import re
import time
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from notifier import send_email
from availability import find_matches

TARGETS_FILE = os.path.join(os.path.dirname(__file__), "targets.json")
WAIT_WINDOW_MINUTES = 75
BOOK_RETRY_MINUTES = 5
RELEASE_REGEX = re.compile(
    r"(\d{1,2})/(\d{1,2})/(\d{2,4}),\s*(\d{1,2}):(\d{2})\s*(AM|PM)",
    re.IGNORECASE,
)
# Fields that a preference can override (or that may live at the target's top level).
PREF_FIELDS = {
    "venue_id",
    "date",
    "time",
    "class_name_contains",
    "teacher_contains",
    "max_credits",
    "release_at",
}


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

        prefs = expand_preferences(t)
        if not prefs:
            errors.append(f"{prefix}: must contain at least one preference (or top-level fields)")
            continue

        for j, p in enumerate(prefs):
            sub = f"{prefix} pref {j + 1}" if t.get("preferences") else prefix
            if not p.get("venue_id"):
                errors.append(f"{sub}: missing 'venue_id'")
            elif not isinstance(p["venue_id"], int):
                errors.append(f"{sub}: 'venue_id' must be an integer")

            if not p.get("date"):
                errors.append(f"{sub}: missing 'date'")
            else:
                try:
                    date.fromisoformat(p["date"])
                except ValueError:
                    errors.append(f"{sub}: invalid date '{p['date']}'")

            if p.get("time") is not None:
                try:
                    h, m = (int(x) for x in p["time"].split(":"))
                    if not (0 <= h <= 23 and 0 <= m <= 59):
                        raise ValueError
                except (ValueError, AttributeError):
                    errors.append(f"{sub}: time '{p['time']}' must be HH:MM (24hr)")

    return errors


def expand_preferences(target: dict) -> list[dict]:
    """
    Resolve a target into a priority-ordered list of full preference dicts.
    Each returned dict has every relevant field already merged from the
    target's top-level plus the per-preference overrides.

    A target without a `preferences` array is treated as a single preference
    using its top-level fields.
    """
    top = {k: v for k, v in target.items() if k in PREF_FIELDS}
    prefs = target.get("preferences")
    if not prefs:
        return [top] if top else []
    return [{**top, **{k: v for k, v in p.items() if k in PREF_FIELDS}} for p in prefs]


def _format_preferences_list(prefs: list[dict]) -> str:
    lines = []
    for i, p in enumerate(prefs, start=1):
        bits = []
        if p.get("time"):
            bits.append(f"time={p['time']}")
        if p.get("class_name_contains"):
            bits.append(f"class~={p['class_name_contains']}")
        if p.get("teacher_contains"):
            bits.append(f"teacher~={p['teacher_contains']}")
        if p.get("max_credits") is not None:
            bits.append(f"max_credits={p['max_credits']}")
        lines.append(f"  {i}. " + (", ".join(bits) if bits else "(default)"))
    return "\n".join(lines)


def _parse_release_dt(credits_reasons: list, venue_tz: str) -> datetime | None:
    """Extract a tz-aware datetime from a credits_reasons message like
    'The booking window opens on 6/4/26, 5:00 AM'."""
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


def _override_release_dt(pref: dict, default_tz: str) -> datetime | None:
    raw = pref.get("release_at")
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo(default_tz))
    return dt


def _match_for_pref(pref: dict) -> dict | None:
    matches = find_matches(
        venue_id=pref["venue_id"],
        date_str=pref["date"],
        time_str=pref.get("time"),
        class_name_contains=pref.get("class_name_contains"),
        teacher_contains=pref.get("teacher_contains"),
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


def _book_pref(target_label: str, pref: dict, index: int, total: int, prefs: list[dict]) -> bool:
    """Try to book one preference with up to BOOK_RETRY_MINUTES of retries."""
    from book import attempt_booking
    pref_label = f"[{index}/{total}] {pref.get('time') or 'any time'}"
    deadline = datetime.now() + timedelta(minutes=BOOK_RETRY_MINUTES)
    attempt = 0

    while datetime.now() < deadline:
        attempt += 1
        print(f"[{target_label}] {pref_label} attempt {attempt}...")
        try:
            result = attempt_booking(
                venue_id=pref["venue_id"],
                date_str=pref["date"],
                time_str=pref.get("time"),
                class_name_contains=pref.get("class_name_contains"),
                teacher_contains=pref.get("teacher_contains"),
                max_credits=pref.get("max_credits"),
            )
        except Exception as e:
            print(f"  Error: {e}")
            time.sleep(3)
            continue

        if result:
            m = result.get("_match", {})
            start = m.get("start_time")
            body = (
                f"Booked {m.get('class_name', 'a class')} on {pref['date']}!\n\n"
                f"  - Venue: {m.get('venue_name')}\n"
                f"  - Time: {start.strftime('%I:%M %p %Z') if start else pref['date']}\n"
                f"  - Teacher: {m.get('teacher_name')}\n"
                f"  - Cost: {m.get('credits')} credits\n\n"
                f"Booked preference {index} of {total} from your priority list:\n"
                f"{_format_preferences_list(prefs)}\n\n"
                "- Your ClassPass Bot"
            )
            print(body)
            send_email(subject=f"Booked: ClassPass {pref['date']}", body=body)

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
        # Empty result means no available match this instant; back off briefly and retry
        time.sleep(2)

    print(f"  {pref_label} failed after {BOOK_RETRY_MINUTES} min of retries.")
    return False


def process_target(target: dict, run_start: datetime) -> dict:
    """Run the per-target decision loop. Returns the updated target dict, or
    `{"_booked": True}` if any preference was successfully booked."""
    prefs = expand_preferences(target)
    target_label = target.get("label") or (
        f"venue {prefs[0]['venue_id']} {prefs[0]['date']}" if prefs else "(empty target)"
    )
    print(f"\n=== {target_label} ===")
    if not prefs:
        return target

    # Match every preference once up front
    matched = []
    for i, p in enumerate(prefs):
        cls = _match_for_pref(p)
        matched.append((i, p, cls))
        if cls:
            print(f"  [{i + 1}/{len(prefs)}] {p.get('time') or 'any'}: status={cls.get('status')} reason={cls.get('reason')}")
        else:
            print(f"  [{i + 1}/{len(prefs)}] {p.get('time') or 'any'}: no class found")

    # First pass: book any preference that's available right now, in priority order
    for i, p, cls in matched:
        if cls and cls.get("status") == "available":
            print(f"Preference {i + 1} is available. Attempting to book.")
            if _book_pref(target_label, p, i + 1, len(prefs), prefs):
                return {"_booked": True}
            print(f"Preference {i + 1} book failed, falling through to next.")

    # Second pass: find earliest release time among prefs still pre-window
    earliest_release: datetime | None = None
    for _i, p, cls in matched:
        if not cls or cls.get("reason") != "before_opening_window":
            continue
        tz = cls.get("venue_tz", "America/New_York")
        rel = _override_release_dt(p, tz) or _parse_release_dt(cls.get("credits_reasons", []), tz)
        if rel and (earliest_release is None or rel < earliest_release):
            earliest_release = rel

    if earliest_release:
        run_start_local = run_start.astimezone(earliest_release.tzinfo)
        delta = (earliest_release - run_start_local).total_seconds() / 60
        print(f"Earliest release across preferences: {earliest_release.isoformat()} ({delta:+.1f} min from run start).")

        if delta > WAIT_WINDOW_MINUTES:
            print(f"More than {WAIT_WINDOW_MINUTES} min away; skipping (next hourly run picks it up).")
            return target

        wait_until(earliest_release)

        # After release, re-attempt every preference in priority order
        for i, p, _cls in matched:
            if _book_pref(target_label, p, i + 1, len(prefs), prefs):
                return {"_booked": True}

        print("No preference booked after release. Flagging target as polling.")
        return {**target, "status": "polling"}

    # No release time found. If every preference is out_of_spots -> polling.
    if matched and all(c and c.get("reason") == "out_of_spots" for _, _, c in matched):
        print("All preferences are out of spots. Flagging target as polling.")
        return {**target, "status": "polling"}

    # Mixed / unknown states (e.g. class not on schedule yet). Try again next hour.
    print("Mixed or unknown states across preferences. Skipping; will retry next hour.")
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

    run_start = datetime.now(ZoneInfo("UTC"))
    new_targets: list = []

    for target in targets:
        if target.get("status") == "polling":
            new_targets.append(target)
            continue
        result = process_target(target, run_start)
        if not result.get("_booked"):
            new_targets.append(result)

    save_targets(new_targets)


if __name__ == "__main__":
    main()
