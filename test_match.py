"""
One-off smoke test. Confirms the live ClassPass search endpoint returns the
class your target points at. Requires CLASSPASS_AUTH_TOKEN in env or .env.

Usage:
    python test_match.py
"""

from availability import find_matches


def main():
    matches = find_matches(
        venue_id=235412,
        date_str="2026-06-10",
        time_str="20:15",
        class_name_contains="Signature50",
        teacher_contains="Juan Zapata",
    )
    print(f"Found {len(matches)} match(es)")
    for m in matches:
        print(
            f"  schedule_id={m['schedule_id']}  "
            f"{m['start_time']}  "
            f"{m['class_name']}  ({m['teacher_name']})  "
            f"status={m['status']}  credits={m.get('credits')}"
        )


if __name__ == "__main__":
    main()
