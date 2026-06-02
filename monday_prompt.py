"""
Runs every Monday morning. Sends a summary of upcoming bookings and the
windows that open in the next 2 weeks, with a link to edit targets.json.
"""

import sys
import json
import os
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from notifier import send_email

TARGETS_FILE = os.path.join(os.path.dirname(__file__), "targets.json")
GITHUB_REPOSITORY = os.getenv("GITHUB_REPOSITORY", "")
DEFAULT_LEAD_DAYS = 7


def load_targets() -> list:
    if not os.path.exists(TARGETS_FILE):
        return []
    with open(TARGETS_FILE) as f:
        return json.load(f).get("targets", [])


def write_targets(entries: list):
    with open(TARGETS_FILE, "w") as f:
        json.dump({"targets": entries}, f, indent=2)
    print(f"Saved {len(entries)} target(s).")


def get_upcoming_windows() -> list:
    """Targets whose booking window opens within the next 14 days."""
    today = datetime.now(ZoneInfo("UTC")).date()
    upcoming = []
    for t in load_targets():
        try:
            d = date.fromisoformat(t["date"])
        except (KeyError, ValueError):
            continue
        lead = t.get("lead_days", DEFAULT_LEAD_DAYS)
        days_until_open = (d - today).days - lead
        if 0 <= days_until_open <= 14:
            upcoming.append({**t, "books_in_days": days_until_open})
    return sorted(upcoming, key=lambda t: (t["date"], t.get("time", "")))


def send_summary_email():
    edit_url = f"https://github.com/{GITHUB_REPOSITORY}/edit/main/targets.json" if GITHUB_REPOSITORY else ""
    upcoming = get_upcoming_windows()

    if upcoming:
        lines = []
        for t in upcoming:
            d = date.fromisoformat(t["date"])
            days = t["books_in_days"]
            when = "tonight" if days == 0 else f"in {days} day{'s' if days != 1 else ''}"
            time_label = t.get("time", "any time")
            filt = t.get("class_name_contains") or "any class"
            lines.append(f"  • {d.strftime('%A, %b')} {d.day} — venue {t['venue_id']} — {time_label} — {filt} — opens {when}")
        section = "Upcoming booking windows:\n\n" + "\n".join(lines)
    else:
        section = "No booking windows open in the next 2 weeks."

    edit_section = f"\n\nEdit your targets.json:\n{edit_url}" if edit_url else ""
    body = (
        "ClassPass — weekly schedule summary\n\n"
        + section
        + edit_section
        + "\n\n— Your ClassPass Bot"
    )
    send_email(subject="ClassPass: weekly booking schedule", body=body)
    print("Weekly summary email sent.")


def interactive_prompt():
    targets = load_targets()

    print("\nCurrent targets:")
    if targets:
        for t in targets:
            print(f"  venue {t.get('venue_id')} | {t.get('date')} {t.get('time', '')} | {t.get('class_name_contains', '')}")
    else:
        print("  (none)")

    print("\nAdd new targets. Empty venue_id to finish.")
    new_entries = []
    while True:
        raw = input("Venue ID (blank to stop): ").strip()
        if not raw:
            break
        try:
            venue_id = int(raw)
        except ValueError:
            print("  Must be an integer.")
            continue
        date_str = input("  Date (YYYY-MM-DD): ").strip()
        try:
            date.fromisoformat(date_str)
        except ValueError:
            print("  Invalid date.")
            continue
        time_str = input("  Time (HH:MM, blank for any): ").strip()
        name_filter = input("  Class name contains (blank for any): ").strip()

        entry = {"venue_id": venue_id, "date": date_str}
        if time_str:
            entry["time"] = time_str
        if name_filter:
            entry["class_name_contains"] = name_filter
        new_entries.append(entry)

    if new_entries:
        write_targets(targets + new_entries)
    else:
        print("No changes made.")


def main():
    if sys.stdin.isatty():
        interactive_prompt()
    else:
        send_summary_email()


if __name__ == "__main__":
    main()
