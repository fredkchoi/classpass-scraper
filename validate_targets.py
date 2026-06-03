"""
Standalone linter for targets.json. Safe to run in CI with no secrets/env vars.
Exits 0 if valid, 1 if any errors are found.
"""

from __future__ import annotations

import json
import sys
from datetime import date, datetime
from zoneinfo import ZoneInfo

TARGETS_FILE = "targets.json"
KNOWN_FIELDS = {
    "venue_id",
    "date",
    "time",
    "class_name_contains",
    "teacher_contains",
    "max_credits",
    "release_at",
    "status",
}
VALID_STATUSES = {"polling"}
MAX_DAYS_OUT = 60


def validate(data: dict) -> tuple[list[str], list[str]]:
    """Return (errors, warnings)."""
    errors: list[str] = []
    warnings: list[str] = []

    if not isinstance(data, dict) or "targets" not in data:
        errors.append("targets.json must be a JSON object with a top-level 'targets' array")
        return errors, warnings

    targets = data["targets"]
    if not isinstance(targets, list):
        errors.append("'targets' must be an array")
        return errors, warnings

    today_utc = datetime.now(ZoneInfo("UTC")).date()
    seen: dict[tuple, int] = {}

    for i, t in enumerate(targets):
        prefix = f"Target {i + 1}"

        if not isinstance(t, dict):
            errors.append(f"{prefix}: must be a JSON object")
            continue

        unknown = set(t.keys()) - KNOWN_FIELDS
        if unknown:
            warnings.append(f"{prefix}: unknown field(s): {', '.join(sorted(unknown))}")

        # venue_id
        vid = t.get("venue_id")
        if vid is None:
            errors.append(f"{prefix}: missing required field 'venue_id'")
        elif not isinstance(vid, int):
            errors.append(f"{prefix}: 'venue_id' must be an integer (got {vid!r})")

        # date
        date_str = t.get("date")
        parsed_date: date | None = None
        if not date_str:
            errors.append(f"{prefix}: missing required field 'date'")
        else:
            try:
                parsed_date = date.fromisoformat(date_str)
            except ValueError:
                errors.append(f"{prefix}: invalid date '{date_str}' (expected YYYY-MM-DD)")
            else:
                if parsed_date <= today_utc:
                    errors.append(f"{prefix}: date {date_str} is today or in the past")
                elif (parsed_date - today_utc).days > MAX_DAYS_OUT:
                    warnings.append(
                        f"{prefix}: date {date_str} is {(parsed_date - today_utc).days} days out "
                        f"(>{MAX_DAYS_OUT}) — booking window likely not open yet"
                    )

        if parsed_date is not None and vid is not None:
            key = (date_str, vid, t.get("time"), t.get("class_name_contains"))
            if key in seen:
                errors.append(
                    f"{prefix}: duplicate of target {seen[key]} (same venue/date/time/class filter)"
                )
            else:
                seen[key] = i + 1

        # time
        time_str = t.get("time")
        if time_str is not None:
            try:
                parts = time_str.split(":")
                if len(parts) != 2:
                    raise ValueError
                h, m = int(parts[0]), int(parts[1])
                if not (0 <= h <= 23 and 0 <= m <= 59):
                    raise ValueError
            except (ValueError, AttributeError):
                errors.append(
                    f"{prefix}: time '{time_str}' must be HH:MM (24hr, e.g. '07:30')"
                )

        # release_at (optional manual override of the API-derived release time)
        ra = t.get("release_at")
        if ra is not None:
            try:
                datetime.fromisoformat(ra)
            except (TypeError, ValueError):
                errors.append(f"{prefix}: 'release_at' must be an ISO datetime string (got {ra!r})")

        # max_credits
        mc = t.get("max_credits")
        if mc is not None and (not isinstance(mc, int) or mc < 1):
            errors.append(f"{prefix}: 'max_credits' must be a positive integer (got {mc!r})")

        # status
        status = t.get("status")
        if status is not None and status not in VALID_STATUSES:
            errors.append(
                f"{prefix}: invalid status '{status}' (expected one of {sorted(VALID_STATUSES)})"
            )

    return errors, warnings


def main() -> int:
    try:
        with open(TARGETS_FILE) as f:
            data = json.load(f)
    except FileNotFoundError:
        print(f"ERROR: {TARGETS_FILE} not found")
        return 1
    except json.JSONDecodeError as e:
        print(f"ERROR: {TARGETS_FILE} is not valid JSON: {e}")
        return 1

    errors, warnings = validate(data)

    for w in warnings:
        print(f"WARNING: {w}")
    for e in errors:
        print(f"ERROR: {e}")

    if errors:
        print(f"\n{len(errors)} error(s) found.")
        return 1

    count = len(data.get("targets", []))
    print(f"OK: {count} target(s) valid" + (f" ({len(warnings)} warning(s))" if warnings else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
