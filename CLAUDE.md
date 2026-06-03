# CLAUDE.md

## General Rules

- **Always update README.md** when making any change that affects features, configuration, environment variables, workflows, scheduling, or usage. Do not wait to be asked.
- **Always update this CLAUDE.md** when making changes that affect the scheduling architecture, booking flow, or key file inventory.
- **Never use em dashes** in responses or any frontend text. Use commas, parentheses, or rewrite the sentence instead.

## Project Overview

Python automation for booking ClassPass sessions across multiple venues. Runs entirely in the cloud, no local machine needed after setup. Sibling to and modeled after `../five-iron-scraper`, but adapted for:
- multiple venues per repo (venue_id stored per-target in `targets.json`)
- venue-local timezones (booking window opens at midnight venue-local, not always ET)
- ClassPass dynamic credit pricing (the cost is re-fetched right before booking)

The booking job is triggered by a Cloudflare Worker cron (more reliable than GHA's native scheduler) which fires a GitHub `repository_dispatch` event every hour; GitHub Actions does the actual Python work. A GHA `schedule:` cron at the same cadence remains as a backup. The scheduler is API-driven: it queries the schedule endpoint for each target to find the exact release time and branches on `availability.status` + `availability.reason`. No per-studio rules are baked into the code.

## Key Files

| File | Purpose |
|---|---|
| `scheduler.py` | Hourly runner. Queries the schedule API per target, branches on `availability.status`/`reason`. Books immediately if `available`, polls if `out_of_spots`, parses `credits_reasons` for the release moment if `before_opening_window` and waits until then if within 75 min |
| `book.py` | Booking logic: `POST /_api/v1/users/{user_id}/reservations` with `{schedule, credits}`. Also exposes `fetch_credit_balance()` which is used both for affordability filtering and as the auth health check |
| `availability.py` | `POST /_api/v3/search/schedules` via `curl_cffi` (impersonates Chrome's TLS handshake to defeat Cloudflare bot detection) |
| `cancellation_poller.py` | Hourly job that watches polling targets. Auto-books via `attempt_booking` when a matching slot opens; emails if no token is configured or booking fails |
| `gcal.py` | Google Calendar event creation after a booking |
| `monday_prompt.py` | Weekly Monday email summarizing upcoming booking windows |
| `targets.json` | What to book. Each target has hoisted common fields (`venue_id`, `date`, etc.) and an optional `preferences` array of priority-ordered alternatives. Each preference can override any field. If `preferences` is absent the target itself is treated as one preference. |
| `validate_targets.py` | Schema linter for `targets.json` (run in CI on every push) |
| `config.py` | Env var loading |
| `notifier.py` | Email notifications via Gmail SMTP |
| `cf-trigger/` | Cloudflare Worker that fires `repository_dispatch` on cron, primary trigger for `hourly-booker.yml` |

## Booking Flow

1. `POST /_api/v3/search/schedules` with `{date, venue: [<id>], ...}` returns class list with `schedule.id` and `availability.credits`
2. `GET /_api/v3/lifecycle/user/{CLASSPASS_USER_ID}/balance` returns `{"data": {"credit_balance": {"credits_remaining": N}}}`. Fetched once at the start of each target's processing in `scheduler.py` and `cancellation_poller.py`; preferences whose `credits` cost exceeds the balance are dropped from the booking + sleep decision passes (logged as `UNAFFORDABLE`). If the balance fetch itself fails, no filtering is applied (fallback to letting the reservation endpoint reject)
3. `POST /_api/v1/users/{CLASSPASS_USER_ID}/reservations` with `{"schedule": <id>, "credits": <credits>}` and `cp-authorization: Token <CLASSPASS_AUTH_TOKEN>`

**Never hardcode credits**, they're dynamic. Always read `availability.credits` from a fresh search response and pass it into the reservation.

**ClassPass is behind Cloudflare**. Use `curl_cffi` with `impersonate="chrome"` for any request that hits classpass.com, plain `requests` gets a 403 "Just a moment..." challenge.

## Auth

`cp-authorization` token is captured manually from the user's browser DevTools and stored as `CLASSPASS_AUTH_TOKEN`. Long-lived (weeks to months). README has the capture steps under "Capturing your ClassPass auth token and user ID".

**There is no auto-refresh.** The login endpoint (`https://classpass.com/api/v2/auth/login`) is hidden behind a service worker that DevTools can't capture the body of, the token never lands in `localStorage` or visible cookies, and brute-forcing the payload shape gets rate-limited quickly. We investigated this in depth and concluded the manual rotation cost (once every few weeks/months) is much lower than the cost of a half-tested auto-refresh failing right before a popular class releases. If we ever revisit, mitmproxy in front of a logged-in browser would be the reliable capture path. Do not waste time poking at the login endpoint without that.

**Staleness detection.** `scheduler.main()` and `cancellation_poller.main()` call `fetch_credit_balance()` once at run start. A `None` return almost always means the token is stale (the balance endpoint requires auth; Cloudflare blips are rare). When that happens we email the user a "rotate CLASSPASS_AUTH_TOKEN" message and continue the run as a soft gate, so a one-off network blip doesn't halt the booker.

## Booking Window

ClassPass venues typically advertise "Opens 7 Days in Advance" (visible in `venue.booking_window` in the search response). The scheduler:
- Defaults to `lead_days = 7`
- Computes window-open time as venue-local midnight `lead_days` before the class date
- Uses `venue.tz` from the search response to pick the timezone
- Per-target `lead_days` override is supported

Whether ClassPass actually releases at midnight venue-local is empirical. If not, the scheduler's booking attempt fails, the target is flagged `"status": "polling"`, and `cancellation_poller.py` keeps trying hourly until it succeeds.

## Scheduling

- **Cloudflare Worker** (primary): fires hourly at `0 * * * *` UTC and POSTs `repository_dispatch` with `event_type: hourly-booker` to GitHub.
- **GHA `schedule:` cron** (backup): same hourly cadence, kept in case the CF Worker fails.
- **Workflow `timeout-minutes: 150`** to accommodate the max in-process wait (75 min window + 5 min book retry buffer plus overhead).
- **Workflow `concurrency:`** serializes runs. Prevents an in-flight sleeping run from racing the next hourly dispatch on the reservation endpoint.
- **Booking semantics (`scheduler.py`)**:
  - Excludes polling targets (those are owned by `cancellation_poller.py`)
  - For each remaining target, expand into a priority-ordered list of preferences (via `expand_preferences`), then query the schedule API per preference
  - If any preference is currently `available`, book in priority order using `_book_pref` (search + reserve each attempt, 2 s retry, 5 min cap)
  - Else find the earliest `before_opening_window` release time across all preferences. If within next 75 min, sleep until then, then fire `_book_pref_fast` per preference (skips the per-attempt search by reusing the pre-window schedule_id; retries every `FAST_RETRY_SECONDS` = 0.15 s). The fast path matters because popular classes sell out in well under a second once the window opens
  - If all preferences are `out_of_spots` (or all booking attempts fail), the whole target moves to `status: polling` and a "couldn't book" email is sent. Failed bookings get written back to `targets.json` for the cancellation poller, which also iterates preferences in priority order
  - Reservation cost (credits) is dynamic and can be `None` pre-window. The fast path does one refresh at release to get fresh credits, falling back to `retail_price_in_credits` if still missing
  - Successful book → confirmation email + optional GCal event via `_emit_booked_email_and_calendar`

## Workflows

| Workflow | Trigger | Purpose |
|---|---|---|
| `hourly-booker.yml` | `repository_dispatch` (from CF Worker) + `schedule` (backup) + `workflow_dispatch` | Hourly booking attempt |
| `cancellation-poller.yml` | `schedule` hourly | Watches polling targets; auto-books matching slots, email fallback if booking fails |
| `monday-prompt.yml` | `schedule` Monday 9am ET | Weekly email reminder to confirm targets |
| `validate-targets.yml` | Push/PR touching `targets.json` | Lints schema; blocks merge on failure |
