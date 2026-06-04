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
# Tight retry interval for the release-moment fast reserve loop. Popular classes
# sell out in well under a second; we want to maximize attempts per second.
FAST_RETRY_SECONDS = 0.15
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


def _emit_booked_email_and_calendar(target_label, pref, fresh_match, credits, index, total, prefs):
    """Side-effects after a successful reservation: notification email + GCal event."""
    start = fresh_match.get("start_time")
    body = (
        f"Booked {fresh_match.get('class_name', 'a class')} on {pref['date']}!\n\n"
        f"  - Venue: {fresh_match.get('venue_name')}\n"
        f"  - Time: {start.strftime('%I:%M %p %Z') if start else pref['date']}\n"
        f"  - Teacher: {fresh_match.get('teacher_name')}\n"
        f"  - Cost: {credits} credits\n\n"
        f"Booked preference {index} of {total} from your priority list:\n"
        f"{_format_preferences_list(prefs)}\n\n"
        "- Your ClassPass Bot"
    )
    print(body)
    send_email(subject=f"Booked: ClassPass {pref['date']}", body=body)

    if start and os.getenv("GOOGLE_REFRESH_TOKEN"):
        try:
            from gcal import create_booking_event
            duration = fresh_match.get("duration_minutes") or 50
            end = start + timedelta(minutes=duration)
            create_booking_event(
                start_dt=start,
                end_dt=end,
                summary=f"{fresh_match.get('class_name')} @ {fresh_match.get('venue_name')}",
                location_name=fresh_match.get("venue_name", ""),
                tz_name=fresh_match.get("venue_tz", "America/New_York"),
            )
        except Exception as e:
            print(f"[gcal] Skipping calendar event: {e}")


def _book_pref_fast(target_label: str, pref: dict, pre_match: dict, index: int, total: int,
                    prefs: list[dict], balance: int | None,
                    outcomes: list[str] | None = None) -> bool:
    """
    Release-moment booker. Skips the per-attempt search round-trip used by
    `_book_pref` by reusing the schedule_id from the pre-window match, and
    spins reservation POSTs at FAST_RETRY_SECONDS instead of 2s. One up-front
    search refreshes the dynamic credit cost (pre-window the API returns None).
    Falls back to retail_price_in_credits when even the post-release search
    hasn't populated credits yet.

    If `outcomes` is provided, the function records a per-preference failure
    reason at outcomes[index-1] when it returns False, for use in the
    "couldn't book" email.
    """
    from book import reserve

    def _fail(reason: str) -> bool:
        print(f"  pref {index}: {reason}")
        if outcomes is not None and 0 <= index - 1 < len(outcomes):
            outcomes[index - 1] = reason
        return False

    schedule_id = pre_match.get("schedule_id")
    if not schedule_id:
        return _fail("no schedule_id resolved")

    # One refresh to pin current credits cost. Pre-window matches always have
    # credits=None; at/after release the API populates it.
    fresh = _match_for_pref(pref) or pre_match
    credits = (
        fresh.get("credits")
        or fresh.get("retail_price_in_credits")
        or pre_match.get("retail_price_in_credits")
    )
    if not credits:
        return _fail("no credit cost available at release")

    max_c = pref.get("max_credits")
    if max_c is not None and credits > max_c:
        return _fail(f"unaffordable (cost={credits} > max_credits={max_c})")
    if balance is not None and credits > balance:
        return _fail(f"insufficient credits (cost={credits}, balance={balance})")

    pref_label = f"[{index}/{total}] {pref.get('time') or 'any'}"
    print(f"[{target_label}] {pref_label} firing reserve loop (schedule={schedule_id}, credits={credits})")
    deadline = datetime.now() + timedelta(minutes=BOOK_RETRY_MINUTES)
    attempt = 0
    last_msg = ""
    last_price_refresh = 0.0  # monotonic
    while datetime.now() < deadline:
        attempt += 1
        try:
            reserve(schedule_id, credits)
        except Exception as e:
            msg = str(e)[:500].lower()
            last_msg = msg

            # ClassPass error code 5017 = "no purchase option with the specified
            # price is available for the provided schedule ID". Means our credits
            # value is wrong (dynamic pricing). Refresh the schedule to pick up
            # the right price, then retry immediately. The first few attempts
            # right at release commonly hit this if dynamic pricing hadn't
            # populated when we did the up-front search.
            wrong_price = "5017" in msg or "no purchase option" in msg
            if wrong_price:
                now_mono = time.monotonic()
                if now_mono - last_price_refresh > 0.3:  # rate-limit search calls
                    last_price_refresh = now_mono
                    refreshed = _match_for_pref(pref)
                    new_credits = (refreshed or {}).get("credits")
                    if new_credits and new_credits != credits:
                        if balance is not None and new_credits > balance:
                            return _fail(f"insufficient credits (cost={new_credits}, balance={balance})")
                        max_c = pref.get("max_credits")
                        if max_c is not None and new_credits > max_c:
                            return _fail(f"unaffordable (cost={new_credits} > max_credits={max_c})")
                        print(f"  pref {index} attempt {attempt}: price refresh {credits} -> {new_credits}")
                        credits = new_credits
                        continue  # retry immediately with the new price

            # Hard-fail for unrecoverable signals. "no purchase option" appears
            # in the same 5017 body and is handled above; abandon-list strings
            # here should only match true sold-out responses.
            if any(s in msg for s in (
                "out_of_spots", "sold out", "class is full",
                "already_booked", "already reserved", "already_reserved",
            )):
                return _fail(f"reservation rejected: {msg[:140]}")
            # Quiet log for the common HTTP 4xx case (window-not-open retries)
            if attempt == 1 or attempt % 20 == 0:
                print(f"  attempt {attempt}: {msg[:200]}")
            time.sleep(FAST_RETRY_SECONDS)
            continue

        _emit_booked_email_and_calendar(target_label, pref, fresh, credits, index, total, prefs)
        return True

    return _fail(f"all reserve retries exhausted in {BOOK_RETRY_MINUTES} min; last error: {last_msg[:200]}")


def _pick_polling_reason_line(outcomes: list[str], balance: int | None) -> str:
    """Render the top-line `Reason:` text for the polling email based on the
    actual per-preference outcomes. Distinguishes insufficient-credits from
    sold-out so the user knows what to do."""
    actual = [o for o in outcomes if o]
    if not actual:
        return "Release window opened but no preference could be reserved."
    insufficient = [o for o in actual if "insufficient credits" in o or "unaffordable" in o]
    sold_out = [o for o in actual if "out_of_spots" in o or "reservation rejected" in o]
    if len(insufficient) == len(actual):
        bal_text = f" (balance: {balance})" if balance is not None else ""
        return f"All preferences cost more credits than you have{bal_text}; top up to enable booking."
    if len(sold_out) == len(actual):
        return "All preferences were sold out at or immediately after the release moment."
    if insufficient and sold_out:
        return (
            f"Mix of insufficient credits (current balance: {balance}) and sold-out classes. "
            "Topping up credits would have helped on at least one preference."
        )
    return "No preference could be reserved this run."


def _notify_target_moved_to_polling(target_label: str, prefs: list[dict],
                                    balance: int | None = None,
                                    outcomes: list[str] | None = None,
                                    fallback_reason: str | None = None):
    """Email the user when a target couldn't be booked and is moving to polling.
    Includes the per-preference outcomes so the user sees why each preference
    failed (out_of_spots vs insufficient credits vs reserve timeout)."""
    reason = (
        _pick_polling_reason_line(outcomes, balance) if outcomes
        else (fallback_reason or "No preference could be reserved this run.")
    )

    body = f"Could not auto-book {target_label} during this run.\n\nReason: {reason}\n\n"
    if balance is not None:
        body += f"Current credit balance: {balance}\n\n"

    body += "Preferences attempted (priority order):\n"
    for i, p in enumerate(prefs):
        bits = []
        if p.get("time"):
            bits.append(f"time={p['time']}")
        if p.get("class_name_contains"):
            bits.append(f"class~={p['class_name_contains']}")
        if p.get("teacher_contains"):
            bits.append(f"teacher~={p['teacher_contains']}")
        if p.get("max_credits") is not None:
            bits.append(f"max_credits={p['max_credits']}")
        label = ", ".join(bits) if bits else "(default)"
        outcome = outcomes[i] if outcomes and i < len(outcomes) and outcomes[i] else "(no info)"
        body += f"  {i + 1}. {label}\n     -> {outcome}\n"

    body += (
        "\nThe target is now in polling mode. The cancellation poller will keep "
        "checking hourly in case a spot opens up. To book manually, sign in at "
        "https://classpass.com/.\n\n"
        "- Your ClassPass Bot"
    )
    print(body)
    send_email(subject=f"ClassPass: couldn't book {target_label}", body=body)


def _is_affordable(cls: dict | None, balance: int | None) -> bool:
    """A preference is affordable if balance is unknown, credits cost is unknown,
    or credits cost <= balance. Conservative when info is missing (assumes yes)."""
    if cls is None:
        return False
    credits_cost = cls.get("credits")
    if balance is None or credits_cost is None:
        return True
    return credits_cost <= balance


def process_target(target: dict, run_start: datetime, balance: int | None) -> dict:
    """Run the per-target decision loop. Returns the updated target dict, or
    `{"_booked": True}` if any preference was successfully booked."""
    prefs = expand_preferences(target)
    target_label = target.get("label") or (
        f"venue {prefs[0]['venue_id']} {prefs[0]['date']}" if prefs else "(empty target)"
    )
    print(f"\n=== {target_label} ===")
    if not prefs:
        return target

    # Match every preference once up front. Outcomes are populated as we
    # observe each preference's state; we'll surface them in the polling email
    # if no preference ends up booked.
    matched = []
    outcomes: list[str] = [""] * len(prefs)
    for i, p in enumerate(prefs):
        cls = _match_for_pref(p)
        matched.append((i, p, cls))
        if cls is None:
            outcomes[i] = "no class found matching filters"
            print(f"  [{i + 1}/{len(prefs)}] {p.get('time') or 'any'}: no class found")
            continue
        cost = cls.get("credits")
        status = cls.get("status")
        reason = cls.get("reason")
        if status == "available":
            outcomes[i] = "available"
        elif reason == "out_of_spots":
            outcomes[i] = "out_of_spots"
        elif reason == "before_opening_window":
            outcomes[i] = "before_opening_window (waiting for release)"
        else:
            outcomes[i] = f"status={status} reason={reason}"
        if not _is_affordable(cls, balance):
            outcomes[i] = f"insufficient credits (cost={cost}, balance={balance})"
            tag = f" UNAFFORDABLE(cost={cost} > bal={balance})"
        else:
            tag = ""
        print(f"  [{i + 1}/{len(prefs)}] {p.get('time') or 'any'}: status={status} reason={reason} credits={cost}{tag}")

    # First pass: book any preference that's available right now, in priority order
    for i, p, cls in matched:
        if cls and cls.get("status") == "available" and _is_affordable(cls, balance):
            print(f"Preference {i + 1} is available. Attempting to book.")
            if _book_pref(target_label, p, i + 1, len(prefs), prefs):
                return {"_booked": True}
            outcomes[i] = "available but reservation failed"
            print(f"Preference {i + 1} book failed, falling through to next.")

    # Second pass: find earliest release time among affordable prefs still pre-window
    earliest_release: datetime | None = None
    for _i, p, cls in matched:
        if not cls or cls.get("reason") != "before_opening_window":
            continue
        if not _is_affordable(cls, balance):
            continue
        tz = cls.get("venue_tz", "America/New_York")
        rel = _override_release_dt(p, tz) or _parse_release_dt(cls.get("credits_reasons", []), tz)
        if rel and (earliest_release is None or rel < earliest_release):
            earliest_release = rel

    if earliest_release:
        run_start_local = run_start.astimezone(earliest_release.tzinfo)
        delta = (earliest_release - run_start_local).total_seconds() / 60
        print(f"Earliest release across affordable preferences: {earliest_release.isoformat()} ({delta:+.1f} min from run start).")

        if delta > WAIT_WINDOW_MINUTES:
            print(f"More than {WAIT_WINDOW_MINUTES} min away; skipping (next hourly run picks it up).")
            return target

        wait_until(earliest_release)

        # After release, fire tight reservation loops in priority order. Uses the
        # pre-resolved schedule_id from `matched` to skip a per-attempt search.
        for i, p, cls in matched:
            if not _is_affordable(cls, balance):
                continue
            if _book_pref_fast(target_label, p, cls, i + 1, len(prefs), prefs, balance, outcomes):
                return {"_booked": True}

        print("No preference booked after release. Flagging target as polling.")
        _notify_target_moved_to_polling(target_label, prefs, balance=balance, outcomes=outcomes)
        return {**target, "status": "polling"}

    # No release time found. If every preference is out_of_spots OR unaffordable -> polling.
    if matched and all(
        c and (c.get("reason") == "out_of_spots" or not _is_affordable(c, balance))
        for _, _, c in matched
    ):
        print("All preferences are out of spots or unaffordable. Flagging target as polling.")
        _notify_target_moved_to_polling(target_label, prefs, balance=balance, outcomes=outcomes)
        return {**target, "status": "polling"}

    # Mixed / unknown states (e.g. class not on schedule yet). Try again next hour.
    print("Mixed or unknown states across preferences. Skipping; will retry next hour.")
    return target


def _maybe_notify_token_stale(balance: int | None):
    """If the balance check failed at run start, fire a one-shot email so the user
    knows to rotate CLASSPASS_AUTH_TOKEN. Soft gate: we still let the run continue."""
    if balance is not None:
        print(f"Credit balance: {balance} credits")
        return
    body = (
        "Could not fetch your ClassPass credit balance, which usually means "
        "CLASSPASS_AUTH_TOKEN is stale. Booking attempts will likely fail "
        "until it's rotated.\n\n"
        "How to rotate:\n"
        "  1. Sign in at https://classpass.com\n"
        "  2. Open DevTools (F12), Network tab\n"
        "  3. Click around to trigger any classpass.com/_api/... request\n"
        "  4. Inspect that request's headers. Copy the value after 'Token ' "
        "from the `cp-authorization` request header (a 40-char hex string)\n"
        "  5. Open https://github.com/fredkchoi/classpass-scraper/settings/secrets/actions\n"
        "  6. Edit the CLASSPASS_AUTH_TOKEN secret and paste the new value\n\n"
        "The next hourly run will pick it up automatically; no redeploy needed.\n\n"
        "- Your ClassPass Bot"
    )
    print(body)
    send_email(subject="ClassPass: auth token may be stale", body=body)


def main():
    from book import fetch_credit_balance

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

    balance = fetch_credit_balance()
    _maybe_notify_token_stale(balance)

    run_start = datetime.now(ZoneInfo("UTC"))
    new_targets: list = []

    for target in targets:
        if target.get("status") == "polling":
            new_targets.append(target)
            continue
        result = process_target(target, run_start, balance)
        if not result.get("_booked"):
            new_targets.append(result)

    save_targets(new_targets)


if __name__ == "__main__":
    main()
