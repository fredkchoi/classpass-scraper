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
TARGET_LEVEL_FIELDS = {
    "label",
    "venue_id",
    "date",
    "time",
    "class_name_contains",
    "teacher_contains",
    "max_credits",
    "release_at",
    "preferences",
    "status",
    "original_booking",
}
PREF_LEVEL_FIELDS = {
    "venue_id",
    "date",
    "time",
    "class_name_contains",
    "teacher_contains",
    "max_credits",
    "release_at",
}
VALID_STATUSES = {"polling", "upgrade_polling"}
MAX_DAYS_OUT = 60


def _validate_pref_fields(p: dict, prefix: str, errors: list, warnings: list, today_utc: date):
    vid = p.get("venue_id")
    if vid is None:
        errors.append(f"{prefix}: missing required field 'venue_id'")
    elif not isinstance(vid, int):
        errors.append(f"{prefix}: 'venue_id' must be an integer (got {vid!r})")

    date_str = p.get("date")
    if not date_str:
        errors.append(f"{prefix}: missing required field 'date'")
    else:
        try:
            d = date.fromisoformat(date_str)
        except ValueError:
            errors.append(f"{prefix}: invalid date '{date_str}' (expected YYYY-MM-DD)")
        else:
            if d <= today_utc:
                errors.append(f"{prefix}: date {date_str} is today or in the past")
            elif (d - today_utc).days > MAX_DAYS_OUT:
                warnings.append(
                    f"{prefix}: date {date_str} is {(d - today_utc).days} days out (>{MAX_DAYS_OUT})"
                )

    time_str = p.get("time")
    if time_str is not None:
        try:
            h, m = (int(x) for x in time_str.split(":"))
            if not (0 <= h <= 23 and 0 <= m <= 59):
                raise ValueError
        except (ValueError, AttributeError):
            errors.append(f"{prefix}: time '{time_str}' must be HH:MM (24hr)")

    ra = p.get("release_at")
    if ra is not None:
        try:
            datetime.fromisoformat(ra)
        except (TypeError, ValueError):
            errors.append(f"{prefix}: 'release_at' must be an ISO datetime string (got {ra!r})")

    mc = p.get("max_credits")
    if mc is not None and (not isinstance(mc, int) or mc < 1):
        errors.append(f"{prefix}: 'max_credits' must be a positive integer (got {mc!r})")


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

    for i, t in enumerate(targets):
        prefix = f"Target {i + 1}"

        if not isinstance(t, dict):
            errors.append(f"{prefix}: must be a JSON object")
            continue

        unknown = set(t.keys()) - TARGET_LEVEL_FIELDS
        if unknown:
            warnings.append(f"{prefix}: unknown field(s): {', '.join(sorted(unknown))}")

        status = t.get("status")
        if status is not None and status not in VALID_STATUSES:
            errors.append(
                f"{prefix}: invalid status '{status}' (expected one of {sorted(VALID_STATUSES)})"
            )

        # Expand into preferences and validate each. Top-level fields are inherited
        # by every preference; per-preference fields override.
        top = {k: v for k, v in t.items() if k in PREF_LEVEL_FIELDS}
        prefs = t.get("preferences")

        if prefs is None:
            _validate_pref_fields(top, prefix, errors, warnings, today_utc)
            continue

        if not isinstance(prefs, list) or not prefs:
            errors.append(f"{prefix}: 'preferences' must be a non-empty array")
            continue

        for j, p in enumerate(prefs):
            sub = f"{prefix} pref {j + 1}"
            if not isinstance(p, dict):
                errors.append(f"{sub}: must be a JSON object")
                continue
            unknown_p = set(p.keys()) - PREF_LEVEL_FIELDS
            if unknown_p:
                warnings.append(f"{sub}: unknown field(s): {', '.join(sorted(unknown_p))}")
            merged = {**top, **{k: v for k, v in p.items() if k in PREF_LEVEL_FIELDS}}
            _validate_pref_fields(merged, sub, errors, warnings, today_utc)

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
