"""
Polls for cancellations on targets the booker couldn't complete (status="polling").
Runs hourly via GitHub Actions.

For each polling target, iterates through the target's preferences in priority
order. The first preference whose class is currently available gets booked.
On book success the target is removed from targets.json; otherwise it stays
polling for the next hour.

If CLASSPASS_AUTH_TOKEN is not set, falls back to email-only mode (the user
must book manually via the link).
"""

from __future__ import annotations

import json
import os
from datetime import date, datetime
from zoneinfo import ZoneInfo

from notifier import send_email
from availability import find_matches
from scheduler import expand_preferences, _format_preferences_list, _book_pref, _maybe_notify_token_stale

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
    """True if every preference's class date has already passed."""
    today = datetime.now(ZoneInfo("UTC")).date()
    prefs = expand_preferences(target)
    if not prefs:
        return True
    for p in prefs:
        try:
            d = date.fromisoformat(p["date"])
        except (KeyError, ValueError):
            continue
        if d >= today:
            return False
    return True


def _email_open_matches(target_label: str, hits: list[tuple[int, dict, dict]], prefs: list[dict]):
    """Fallback email when no auth token is configured."""
    if not hits:
        return
    lines = []
    for idx, _p, m in hits:
        url = BOOKING_URL_FMT.format(schedule_id=m["schedule_id"])
        lines.append(
            f"  - Preference {idx + 1} ({m['start_time'].strftime('%I:%M %p')}): "
            f"{m['class_name']} ({m['teacher_name']}) - {m['credits']} credits\n    {url}"
        )
    body = (
        f"A cancellation opened up for {target_label}:\n\n"
        + "\n".join(lines)
        + "\n\nFrom your priority list:\n"
        + _format_preferences_list(prefs)
        + "\n\n- Your ClassPass Bot"
    )
    send_email(subject=f"Cancellation available: ClassPass {target_label}", body=body)


def _cancel_reminder_note(original_booking: dict) -> str:
    """Format the ACTION REQUIRED line appended to upgrade booking confirmations."""
    time_str = original_booking.get("time", "")
    date_str = original_booking.get("date", "")
    try:
        h, m = (int(x) for x in time_str.split(":"))
        time_display = datetime(2000, 1, 1, h, m).strftime("%I:%M %p").lstrip("0")
    except Exception:
        time_display = time_str
    return (
        f"ACTION REQUIRED: This is an upgrade booking. You still have a reservation "
        f"for the {time_display} class on {date_str} - please cancel it on ClassPass "
        f"(auto-cancel is not yet supported)."
    )


def process_polling_target(target: dict, has_token: bool, balance: int | None) -> dict | None:
    """Return None if the target was booked or should be dropped, else the updated target."""
    prefs = expand_preferences(target)
    if not prefs:
        return None

    target_label = target.get("label") or (
        f"venue {prefs[0]['venue_id']} {prefs[0]['date']}"
    )
    is_upgrade = target.get("status") == "upgrade_polling"
    print(f"\n=== {target_label} ({'upgrade ' if is_upgrade else ''}polling) ===")

    cancel_note: str | None = None
    if is_upgrade:
        original = target.get("original_booking") or {}
        if original.get("time") or original.get("date"):
            cancel_note = _cancel_reminder_note(original)

    hits: list[tuple[int, dict, dict]] = []
    for i, p in enumerate(prefs):
        matches = find_matches(
            venue_id=p["venue_id"],
            date_str=p["date"],
            time_str=p.get("time"),
            class_name_contains=p.get("class_name_contains"),
            teacher_contains=p.get("teacher_contains"),
            only_available=True,
        )
        if matches:
            hits.append((i, p, matches[0]))
            print(f"  [{i + 1}/{len(prefs)}] available!")
        else:
            print(f"  [{i + 1}/{len(prefs)}] no spot yet")

    if not hits:
        return target  # keep polling

    if not has_token:
        _email_open_matches(target_label, hits, prefs)
        return None  # drop after notifying

    # Auto-book in priority order, skipping anything we can't afford
    any_unaffordable = False
    for i, p, m in hits:
        cost = m.get("credits")
        if balance is not None and cost is not None and cost > balance:
            print(f"  Preference {i + 1} unaffordable ({cost} > {balance}), skipping.")
            any_unaffordable = True
            continue
        if _book_pref(target_label, p, i + 1, len(prefs), prefs, cancel_note=cancel_note):
            return None  # booked, drop from polling

    # All booking attempts failed (or were unaffordable); keep polling so a credit
    # refill or another cancellation can rescue the target on a later run.
    msg = "Could not auto-book any preference"
    if any_unaffordable:
        msg += " (some were unaffordable)"
    print(f"{msg}; keeping in polling state.")
    return target


def main():
    targets = load_targets()
    polling = [t for t in targets if t.get("status") in ("polling", "upgrade_polling")]
    if not polling:
        print("No cancellation polling targets.")
        return

    has_token = bool(os.getenv("CLASSPASS_AUTH_TOKEN"))
    balance: int | None = None
    if has_token:
        from book import fetch_credit_balance
        balance = fetch_credit_balance()
        _maybe_notify_token_stale(balance)

    non_polling = [t for t in targets if t.get("status") != "polling"]
    remaining_polling: list = []

    for target in polling:
        if _is_expired(target):
            print(f"Dropping expired polling target: {target.get('label') or target.get('date')}")
            continue
        result = process_polling_target(target, has_token, balance)
        if result is not None:
            remaining_polling.append(result)

    save_targets(non_polling + remaining_polling)


if __name__ == "__main__":
    main()
