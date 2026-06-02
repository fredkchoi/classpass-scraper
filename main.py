"""
Manual availability check. Polls a venue/date until matching classes appear, then emails you.
"""

import time
from datetime import datetime
from availability import find_matches
from notifier import send_email
from config import POLL_INTERVAL_SECONDS


def _prompt(label: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    raw = input(f"{label}{suffix}: ").strip()
    return raw or default


def main():
    venue_id = int(_prompt("Venue ID"))
    date_str = _prompt("Date (YYYY-MM-DD)")
    time_str = _prompt("Start time HH:MM (blank for any)", "") or None
    name_filter = _prompt("Class name contains (blank for any)", "") or None

    print(f"Polling ClassPass availability for venue {venue_id} on {date_str}...")

    while True:
        matches = find_matches(
            venue_id=venue_id,
            date_str=date_str,
            time_str=time_str,
            class_name_contains=name_filter,
            only_available=True,
        )
        ts = datetime.now().strftime("%H:%M:%S")
        if matches:
            print(f"[{ts}] Found {len(matches)} available class(es)!")

            lines = []
            for m in matches:
                start = m["start_time"].strftime("%I:%M %p")
                lines.append(
                    f"  • {start} — {m['class_name']} ({m['teacher_name']}) — {m['credits']} credits"
                )

            body = (
                f"ClassPass availability at {matches[0]['venue_name']} on {date_str}:\n\n"
                + "\n".join(lines)
                + "\n\n— Your ClassPass Bot"
            )
            send_email(subject=f"ClassPass: {len(matches)} class(es) available {date_str}", body=body)
            break
        else:
            print(f"[{ts}] No matches yet, checking again in {POLL_INTERVAL_SECONDS / 60:.0f} minutes...")
        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
